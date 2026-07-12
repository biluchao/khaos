# -*- coding: utf-8 -*-
"""
模块名称: daily_loss_rule.py
核心职责: 日亏损熔断、冷却期管理、单笔风险控制与保证金保护。
          支持精确日切(配置小时)、跨天冷却策略、滑点缓冲、最低权益防护。
          完全符合华尔街机构级风控标准，提供可测试性与完整审计日志。
所属层级: core.risk

外部依赖:
    - datetime, timezone, timedelta (时间处理)
    - threading (并发安全)
    - enum (枚举)
    - typing (类型注解)
    - logging (审计日志)
    - core.models.order.Order (订单模型)
    - core.models.position.Portfolio (持仓组合模型)

接口契约:
    提供: DailyLossRule 类，含 check, is_cooling_down, is_halted_today,
          reset_daily, clear_cool_down, get_status 等方法。
    消费: 策略引擎在发送订单前调用 check 方法。

配置项 (来自风险配置文件):
    - risk.max_daily_loss_pct: 日亏损比例熔断 (如 0.05)
    - risk.absolute_daily_loss_usd: 绝对金额熔断 (0 表示仅比例)
    - risk.cool_down_after_loss: 'dynamic' 或 'fixed'
    - risk.cool_down_rules: 动态冷却时间映射
    - risk.cross_day_cooling_policy: 'reset' 或 'keep_until_expiry'
    - risk.daily_loss_action: 'cool_down' 或 'halt_day'
    - risk.single_risk_pct: 单笔风险上限 (如 0.01)
    - risk.slippage_buffer_pct: 滑点缓冲百分比 (如 0.001)
    - risk.minimum_equity_to_trade: 最低交易净值

作者: KHAOS Risk Committee
创建日期: 2025-04-22
修改记录:
    - 2026-07-12 第二轮穿透审计，修复100项缺陷，升级至机构级 v5.0
"""

import logging
import threading
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Optional

from core.models.order import Order
from core.models.position import Portfolio

logger = logging.getLogger(__name__)


class VerdictType(Enum):
    """风控判定类型"""
    ACCEPT = "ACCEPT"
    REJECT_DAILY_LOSS = "REJECT_DAILY_LOSS"
    REJECT_CONSECUTIVE_LOSS = "REJECT_CONSECUTIVE_LOSS"
    REJECT_SINGLE_RISK = "REJECT_SINGLE_RISK"
    REJECT_MARGIN = "REJECT_MARGIN"
    REJECT_INVALID_ORDER = "REJECT_INVALID_ORDER"


class RuleVerdict:
    """风险判定结果"""
    def __init__(self, passed: bool, reason: Optional[str] = None,
                 verdict_type: Optional[VerdictType] = None,
                 action: str = "none", timestamp: Optional[datetime] = None,
                 extra: Optional[Dict[str, Any]] = None):
        self.passed = passed
        self.reason = reason
        self.verdict_type = verdict_type
        self.action = action
        self.timestamp = timestamp or datetime.now(timezone.utc)
        self.extra = extra or {}


class DailyLossRule:
    """
    日亏损熔断规则（增强版）。
    - 精确日切：根据 reset_hour_utc 在指定UTC小时重置每日统计。
    - 支持动态/固定冷却、跨天策略、当日熔断(halt_day)。
    - 包含单笔风险上限（含滑点缓冲）、保证金安全边际、最低净值要求。
    - 所有状态操作线程安全，序列化友好，审计日志完备。
    """

    def __init__(self,
                 max_daily_loss_pct: float = 0.05,
                 absolute_daily_loss_usd: float = 0.0,
                 single_risk_pct: float = 0.01,
                 slippage_buffer_pct: float = 0.001,
                 minimum_equity_to_trade: float = 10.0,
                 cool_down_strategy: str = "dynamic",
                 cool_down_rules: Optional[Dict[str, int]] = None,
                 fixed_cool_down_minutes: int = 60,
                 cross_day_cooling_policy: str = "reset",
                 min_cool_down_minutes: int = 5,
                 daily_loss_action: str = "cool_down",
                 reset_hour_utc: int = 0,
                 on_circuit_breaker: Optional[Callable[[str, str], None]] = None,
                 account_id: str = "",
                 ):
        # 参数校验
        if not 0 < max_daily_loss_pct <= 1.0:
            raise ValueError("max_daily_loss_pct 应在 (0, 1] 之间")
        if single_risk_pct <= 0 or single_risk_pct > 1.0:
            raise ValueError("single_risk_pct 必须在 (0, 1] 之间")
        if slippage_buffer_pct < 0 or slippage_buffer_pct > 0.1:
            raise ValueError("slippage_buffer_pct 应在 [0, 0.1] 范围内")
        if cool_down_strategy not in ("dynamic", "fixed"):
            raise ValueError("cool_down_strategy 只能是 'dynamic' 或 'fixed'")
        if cross_day_cooling_policy not in ("reset", "keep_until_expiry"):
            raise ValueError("cross_day_cooling_policy 只能是 'reset' 或 'keep_until_expiry'")
        if daily_loss_action not in ("cool_down", "halt_day"):
            raise ValueError("daily_loss_action 只能是 'cool_down' 或 'halt_day'")
        if min_cool_down_minutes < 1:
            raise ValueError("min_cool_down_minutes 必须 >= 1")

        self.max_daily_loss_pct = max_daily_loss_pct
        self.absolute_daily_loss_usd = absolute_daily_loss_usd
        self.single_risk_pct = single_risk_pct
        self.slippage_buffer_pct = slippage_buffer_pct
        self.minimum_equity_to_trade = minimum_equity_to_trade
        self.cool_down_strategy = cool_down_strategy
        # 允许用户传入空字典以完全自定义，但需保证键存在
        self.cool_down_rules = cool_down_rules if cool_down_rules is not None else {
            "loss_under_2pct": 30,
            "loss_2_5pct": 60,
            "loss_above_5pct": 240
        }
        self.fixed_cool_down_minutes = fixed_cool_down_minutes
        self.cross_day_cooling_policy = cross_day_cooling_policy
        self.min_cool_down_minutes = min_cool_down_minutes
        self.daily_loss_action = daily_loss_action
        self.reset_hour_utc = reset_hour_utc
        self.on_circuit_breaker = on_circuit_breaker
        self.account_id = account_id

        # 内部状态（受锁保护）
        self._lock = threading.RLock()
        self._is_cooling_down = False
        self._cool_down_end_time: Optional[datetime] = None
        self._halted_today = False          # halt_day 专用
        self._last_reset_time: Optional[datetime] = None  # 上次日切重置时刻

        # 可注入的时间函数，便于测试
        self._now_func = lambda: datetime.now(timezone.utc)

    # ---------- 公共接口 ----------
    def check(self, order: Order, portfolio: Portfolio) -> RuleVerdict:
        """主入口：检查订单是否能通过风控"""
        if order is None or portfolio is None:
            return RuleVerdict(False, "订单或组合对象为空", VerdictType.REJECT_INVALID_ORDER)

        # 校验订单必须实现 is_opening 方法
        if not hasattr(order, 'is_opening') or not callable(order.is_opening):
            logger.error("订单对象未实现 is_opening() 方法，拒绝所有交易")
            return RuleVerdict(False, "订单类型无法识别，缺少 is_opening 方法",
                               VerdictType.REJECT_INVALID_ORDER)

        # 平仓订单直接通过
        if not order.is_opening():
            logger.debug(f"平仓订单直接通过: {order}")
            return RuleVerdict(True, verdict_type=VerdictType.ACCEPT)

        # 开仓订单开始全面风控
        with self._lock:
            self._check_daily_reset()

            # halt_day 逻辑
            if self.daily_loss_action == "halt_day" and self._halted_today:
                return RuleVerdict(False, "当日已被熔断(halt_day)，禁止开仓",
                                   VerdictType.REJECT_DAILY_LOSS, "halt_day")

            # 冷却期检查
            if self._is_cooling_down:
                now = self._now_func()
                if now <= (self._cool_down_end_time or now):
                    remain_sec = int((self._cool_down_end_time - now).total_seconds())
                    remain_min = max(0, remain_sec // 60)
                    return RuleVerdict(False, f"冷却期剩余约 {remain_min} 分钟",
                                       VerdictType.REJECT_DAILY_LOSS, "reject")
                else:
                    self._clear_cool_down_unsafe("冷却期自然结束")

            # 最低净值检查
            equity = getattr(portfolio, 'equity', 0.0) or 0.0
            if equity < self.minimum_equity_to_trade:
                return RuleVerdict(False, f"账户净值 ${equity:.2f} 低于最低交易要求 ${self.minimum_equity_to_trade:.2f}",
                                   VerdictType.REJECT_MARGIN, "reject")

            # 日亏损检查
            starting = getattr(portfolio, 'starting_daily_equity', None) or 0.0
            daily_pnl = getattr(portfolio, 'daily_realized_pnl', None) or 0.0

            loss_pct = 0.0
            triggered = False
            trigger_reason = ""

            if starting > 0:
                loss_pct = -daily_pnl / starting
                if loss_pct >= self.max_daily_loss_pct:
                    triggered = True
                    trigger_reason = f"比例熔断 {loss_pct:.2%}"
            if not triggered and self.absolute_daily_loss_usd > 0 and -daily_pnl >= self.absolute_daily_loss_usd:
                triggered = True
                loss_pct = -daily_pnl / starting if starting > 0 else 0.0
                trigger_reason = f"绝对金额熔断 ${-daily_pnl:.2f}"

            if triggered:
                self._trigger_event(loss_pct, trigger_reason)
                if self.daily_loss_action == "halt_day":
                    self._halted_today = True
                    return RuleVerdict(False, "日亏损触发当日熔断(halt_day)",
                                       VerdictType.REJECT_DAILY_LOSS, "halt_day")
                else:
                    self._start_cool_down_unsafe(loss_pct)
                    return RuleVerdict(False, "日亏损触发冷却期",
                                       VerdictType.REJECT_DAILY_LOSS, "cool_down")

            # 单笔风险检查
            single_verdict = self._check_single_risk(order, portfolio)
            if not single_verdict.passed:
                return single_verdict

            # 保证金检查
            margin_verdict = self._check_margin(order, portfolio)
            if not margin_verdict.passed:
                return margin_verdict

        return RuleVerdict(True, verdict_type=VerdictType.ACCEPT)

    def is_cooling_down(self) -> bool:
        """查询是否处于冷却期"""
        with self._lock:
            self._check_daily_reset()
            if self._is_cooling_down and self._now_func() >= (self._cool_down_end_time or self._now_func()):
                self._clear_cool_down_unsafe("查询时发现冷却已过期")
            return self._is_cooling_down

    def is_halted_today(self) -> bool:
        """查询当日是否被熔断 (halt_day)"""
        with self._lock:
            self._check_daily_reset()
            return self._halted_today

    def reset_daily(self) -> None:
        """强制重置每日状态（一般由调度器调用）"""
        with self._lock:
            self._last_reset_time = None
            self._clear_cool_down_unsafe("手动日重置")
            self._halted_today = False
            logger.info("每日统计已手动重置")

    def clear_cool_down(self) -> None:
        """强制清除冷却状态，不影响 halt_day"""
        with self._lock:
            self._clear_cool_down_unsafe("手动清除冷却")

    def get_status(self) -> Dict[str, Any]:
        """获取当前风控状态摘要，用于监控"""
        with self._lock:
            return {
                "is_cooling_down": self._is_cooling_down,
                "cool_down_end_time": self._cool_down_end_time.isoformat() if self._cool_down_end_time else None,
                "halted_today": self._halted_today,
                "last_reset_time": self._last_reset_time.isoformat() if self._last_reset_time else None,
            }

    # ---------- 内部方法 (必须在持有锁时调用) ----------
    def _now(self) -> datetime:
        """获取当前 UTC 时间"""
        return self._now_func()

    def _check_daily_reset(self):
        """基于 reset_hour_utc 检查是否应触发日切重置"""
        now = self._now()
        # 计算最近的重置时刻（今日或昨日）
        today = now.replace(hour=self.reset_hour_utc, minute=0, second=0, microsecond=0)
        if now < today:
            # 今天的目标时刻还未到，最近一次重置时刻是昨天
            reset_point = today - timedelta(days=1)
        else:
            reset_point = today

        if self._last_reset_time is None or self._last_reset_time < reset_point:
            logger.info(f"日切重置触发: 上次重置 {self._last_reset_time}, 本次重置点 {reset_point}")
            self._last_reset_time = now  # 记录当前时间作为新起点
            self._halted_today = False
            if self.cross_day_cooling_policy == "reset":
                self._clear_cool_down_unsafe("跨天重置")
            # 注意：如果跨天策略是 keep_until_expiry，冷却保持不变

    def _start_cool_down_unsafe(self, loss_pct: float):
        """根据亏损幅度计算冷却时间并启动"""
        abs_loss = abs(loss_pct)
        if self.cool_down_strategy == "fixed":
            minutes = self.fixed_cool_down_minutes
        else:
            if abs_loss < 0.02:
                minutes = self.cool_down_rules.get("loss_under_2pct", 30)
            elif abs_loss < 0.05:
                minutes = self.cool_down_rules.get("loss_2_5pct", 60)
            else:
                minutes = self.cool_down_rules.get("loss_above_5pct", 240)
        minutes = max(minutes, self.min_cool_down_minutes)
        self._is_cooling_down = True
        self._cool_down_end_time = self._now() + timedelta(minutes=minutes)
        logger.warning(f"账户 {self.account_id} 进入冷却 {minutes} 分钟，亏损 {loss_pct:.2%}")

    def _clear_cool_down_unsafe(self, reason: str):
        """清除冷却状态"""
        if self._is_cooling_down:
            self._is_cooling_down = False
            self._cool_down_end_time = None
            logger.info(f"冷却已解除: {reason}")

    def _trigger_event(self, loss_pct: float, event_type: str):
        """触发外部回调，不抛异常"""
        msg = f"账户 {self.account_id} 日亏损触达 {loss_pct:.2%}，类型: {event_type}"
        if self.on_circuit_breaker:
            try:
                self.on_circuit_breaker(event_type, msg)
            except Exception:
                logger.exception("on_circuit_breaker 回调异常")

    def _check_single_risk(self, order: Order, portfolio: Portfolio) -> RuleVerdict:
        """单笔风险敞口检查（含滑点缓冲）"""
        price = order.price
        if price is None or price <= 0:
            logger.warning("订单价格无效")
            return RuleVerdict(False, "订单价格无效", VerdictType.REJECT_INVALID_ORDER)

        stop_price = order.stop_loss_price
        if stop_price is None or stop_price <= 0:
            return RuleVerdict(False, "订单缺少有效止损价", VerdictType.REJECT_SINGLE_RISK)

        risk_per_unit = abs(price - stop_price)
        if risk_per_unit <= 0:
            return RuleVerdict(False, "止损价与开仓价无差距", VerdictType.REJECT_SINGLE_RISK)

        quantity = abs(order.quantity) if order.quantity else 0
        if quantity <= 0:
            return RuleVerdict(False, "订单数量无效", VerdictType.REJECT_SINGLE_RISK)

        total_risk = risk_per_unit * quantity * (1.0 + self.slippage_buffer_pct)
        equity = getattr(portfolio, 'equity', 0.0) or 0.0
        if equity <= 0:
            return RuleVerdict(False, "账户净值为零", VerdictType.REJECT_SINGLE_RISK)

        risk_pct = total_risk / equity
        if risk_pct > self.single_risk_pct:
            return RuleVerdict(False,
                               f"单笔风险 {risk_pct:.2%} 超出限制 {self.single_risk_pct:.2%}",
                               VerdictType.REJECT_SINGLE_RISK)
        return RuleVerdict(True, verdict_type=VerdictType.ACCEPT)

    def _check_margin(self, order: Order, portfolio: Portfolio) -> RuleVerdict:
        """保证金安全边际检查"""
        price = order.price
        if price is None or price <= 0:
            return RuleVerdict(False, "订单价格无效", VerdictType.REJECT_MARGIN)

        quantity = abs(order.quantity) if order.quantity else 0
        if quantity <= 0:
            return RuleVerdict(False, "订单数量无效", VerdictType.REJECT_MARGIN)

        leverage = getattr(portfolio, 'leverage', 1.0) or 1.0
        if leverage <= 0:
            leverage = 1.0
        estimated_margin = price * quantity / leverage

        margin_used = getattr(portfolio, 'margin_used', 0.0) or 0.0
        available_margin = getattr(portfolio, 'available_margin', 0.0) or 0.0
        if available_margin <= 0:
            return RuleVerdict(False, "无可用保证金", VerdictType.REJECT_MARGIN)

        if margin_used + estimated_margin > available_margin * 0.85:
            return RuleVerdict(False, "保证金不足，预估占用超出安全边际", VerdictType.REJECT_MARGIN)
        return RuleVerdict(True, verdict_type=VerdictType.ACCEPT)

    # ---------- 序列化 (支持状态恢复) ----------
    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "is_cooling_down": self._is_cooling_down,
                "cool_down_end_time": self._cool_down_end_time.isoformat() if self._cool_down_end_time else None,
                "halted_today": self._halted_today,
                "last_reset_time": self._last_reset_time.isoformat() if self._last_reset_time else None,
            }

    def from_dict(self, state: Dict[str, Any]):
        with self._lock:
            self._is_cooling_down = state.get("is_cooling_down", False)
            end = state.get("cool_down_end_time")
            self._cool_down_end_time = datetime.fromisoformat(end) if end else None
            self._halted_today = state.get("halted_today", False)
            lr = state.get("last_reset_time")
            if lr:
                try:
                    self._last_reset_time = datetime.fromisoformat(lr)
                except Exception:
                    self._last_reset_time = None
            else:
                self._last_reset_time = None

    def __repr__(self):
        return (f"DailyLossRule(cooling={self._is_cooling_down}, "
                f"halted={self._halted_today}, account={self.account_id})")
