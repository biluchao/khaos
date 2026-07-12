# -*- coding: utf-8 -*-
"""
模块名称: global_risk_bus.py
核心职责: 全局风险总线，对所有策略订单进行实时风控审批，支持日亏损熔断、连续亏损熔断、
          预算预留、逐仓/全仓保证金模型、品种级别敞口控制及完整审计追踪。
所属层级: core.risk

外部依赖:
    - asyncio, math, time, logging, datetime, typing
    - core.models.order.Order
    - core.models.position.Position

接口契约:
    提供: {
        'GlobalRiskBus': {
            'request_approval(...) -> Tuple[bool, str]': 审批订单,
            'release_risk_budget(order_id) -> None': 释放预算,
            'update_daily_pnl(realized_pnl) -> None': 更新已实现盈亏,
            'initialize_daily_equity(equity) -> None': 设置日初权益,
            'force_circuit_breaker(operator) -> None': 强制熔断,
            'reset_circuit_breaker(operator) -> None': 手动重置,
            'get_risk_snapshot() -> dict': 获取风险快照
        }
    }
    消费: Order 与 Position 的字段需包含 order_id, reduce_only, direction, symbol, size, price, current_price 等。

配置项:
    - risk.max_total_delta, risk.max_leverage, risk.max_daily_loss
    - risk.max_single_symbol_exposure_pct, risk.max_margin_utilization_pct
    - risk.cool_down_rules, risk.min_equity, risk.max_order_notional, risk.max_total_positions
    - risk.max_consecutive_losses
    - risk.margin_mode, risk.symbol_max_leverage
"""

from __future__ import annotations
import asyncio
import math
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from core.models.order import Order
from core.models.position import Position

logger = logging.getLogger(__name__)


class GlobalRiskBus:
    """
    机构级全局风险总线，单例模式用于所有策略。
    """

    def __init__(self,
                 max_total_delta: float = 3.0,
                 max_leverage: float = 3.0,
                 max_daily_loss_pct: float = 0.05,
                 max_single_symbol_exposure_pct: float = 0.4,
                 max_margin_utilization_pct: float = 0.85,
                 cool_down_rules: Optional[Dict[float, int]] = None,
                 min_equity: float = 200.0,
                 max_order_notional: float = float('inf'),
                 max_total_positions: int = 100,
                 margin_mode: str = 'cross',
                 symbol_max_leverage: Optional[Dict[str, float]] = None,
                 use_net_exposure: bool = False,
                 max_consecutive_losses: int = 5):
        self.max_total_delta = max_total_delta
        self.max_leverage = max_leverage
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_single_symbol_exposure_pct = max_single_symbol_exposure_pct
        self.max_margin_utilization_pct = max_margin_utilization_pct
        self.cool_down_rules = cool_down_rules if cool_down_rules else {0.02: 30, 0.05: 60, 0.10: 240}
        self.min_equity = min_equity
        self.max_order_notional = max_order_notional
        self.max_total_positions = max_total_positions
        self.margin_mode = margin_mode
        self.symbol_max_leverage = symbol_max_leverage if symbol_max_leverage else {}
        self.use_net_exposure = use_net_exposure
        self.max_consecutive_losses = max_consecutive_losses

        # 每日统计
        self._daily_pnl: float = 0.0
        self._daily_start_equity: float = 0.0
        self._last_reset_ts: float = time.time()
        self._consecutive_losses: int = 0

        # 熔断状态
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_until: Optional[datetime] = None

        # 预算预留 (order_id -> (notional, delta, symbol, timestamp))
        self._pending_budget: Dict[str, Tuple[float, float, str, float]] = {}
        self._pending_notional: float = 0.0
        self._pending_delta: float = 0.0

        self._lock = asyncio.Lock()

    async def request_approval(self,
                               order: Order,
                               current_positions: List[Position],
                               equity: float,
                               estimated_price: Optional[float] = None,
                               unrealized_pnl: float = 0.0,
                               used_margin_override: Optional[float] = None,
                               order_margin_override: Optional[float] = None) -> Tuple[bool, str]:
        """
        审批订单，平仓/减仓豁免熔断与部分限制。
        """
        async with self._lock:
            # 基础校验
            if equity <= 0:
                return False, "账户净值非正，拒绝交易"
            price = estimated_price if estimated_price else order.price
            if order.size <= 0 or price <= 0:
                return False, "订单数量或价格非法"
            if order.direction not in ('LONG', 'SHORT', 'CLOSE'):
                return False, f"不支持的方向: {order.direction}"

            is_reduce = getattr(order, 'reduce_only', False) or order.direction == 'CLOSE'

            # 净值最低限制
            if equity < self.min_equity:
                return False, f"净值 {equity:.2f} 低于最低要求 {self.min_equity}"

            # 自动初始化日初权益
            if self._daily_start_equity <= 0:
                self._daily_start_equity = equity
                logger.info(f"日初权益自动初始化: {equity:.2f}")

            # 熔断检查（减仓豁免）
            if not is_reduce and self._circuit_breaker_active:
                if self._circuit_breaker_until and datetime.now(timezone.utc) < self._circuit_breaker_until:
                    return False, f"风险熔断中，将持续至 {self._circuit_breaker_until.isoformat()}"
                else:
                    self._circuit_breaker_active = False

            # 连续亏损熔断（减仓豁免）
            if not is_reduce and self._consecutive_losses >= self.max_consecutive_losses:
                self._activate_circuit_breaker(0.0, reason=f"连续亏损{self._consecutive_losses}笔")
                return False, f"连续亏损熔断 ({self._consecutive_losses}笔)"

            # 日亏损检查（包含未实现亏损）
            if not is_reduce and self._daily_start_equity > 0:
                total_loss = -self._daily_pnl - unrealized_pnl
                if total_loss < 0:
                    loss_pct = abs(total_loss) / self._daily_start_equity
                    if loss_pct > self.max_daily_loss_pct:
                        self._activate_circuit_breaker(loss_pct)
                        return False, f"总亏损 {loss_pct:.2%} 超过日限制 {self.max_daily_loss_pct:.2%}"

            # 清理过期预算
            self._cleanup_expired_reservations()

            # 订单去重（同一 ID 重复审批则先释放旧预算）
            if order.order_id in self._pending_budget:
                self._release_budget_internal(order.order_id)

            # 计算总风险指标
            total_notional, total_delta, symbol_exposure = self._compute_aggregates(current_positions)
            total_notional += self._pending_notional
            total_delta += self._pending_delta

            price = estimated_price if estimated_price else order.price
            order_notional = abs(order.size * price)
            order_delta = order.size * price * (1 if order.direction == 'LONG' else -1)

            # 最大订单名义价值
            if order_notional > self.max_order_notional:
                return False, f"单笔名义价值 {order_notional:.2f} > {self.max_order_notional}"

            # 最大持仓数
            if len(current_positions) >= self.max_total_positions and not is_reduce:
                return False, f"持仓数量 {len(current_positions)} 已达上限"

            # 总杠杆
            proposed_notional = total_notional + order_notional
            proposed_leverage = proposed_notional / equity
            if proposed_leverage > self.max_leverage:
                return False, f"总杠杆 {proposed_leverage:.2f} > {self.max_leverage}"

            # 净 Delta
            proposed_delta = total_delta + order_delta
            if abs(proposed_delta) / equity > self.max_total_delta:
                return False, f"净Delta {abs(proposed_delta)/equity:.2f} > {self.max_total_delta}"

            # 单品种敞口（含 pending 中同品种）
            pending_symbol_notional = sum(entry[0] for entry in self._pending_budget.values() if entry[2] == order.symbol)
            symbol_notional = symbol_exposure.get(order.symbol, 0.0) + pending_symbol_notional + order_notional
            if symbol_notional / equity > self.max_single_symbol_exposure_pct:
                return False, f"品种 {order.symbol} 敞口 {symbol_notional/equity:.2%} > {self.max_single_symbol_exposure_pct%}"

            # 保证金占用
            symbol_lev = self.symbol_max_leverage.get(order.symbol, self.max_leverage)
            if used_margin_override is not None and order_margin_override is not None:
                used_margin = used_margin_override
                required_margin = order_margin_override
            else:
                used_margin = total_notional / self.max_leverage  # 简化
                required_margin = order_notional / symbol_lev
            margin_util = (used_margin + required_margin) / equity
            if margin_util > self.max_margin_utilization_pct:
                return False, f"保证金占用率 {margin_util:.2%} > {self.max_margin_utilization_pct%}"

            # 预留预算
            self._reserve_budget(order.order_id, order_notional, order_delta, order.symbol)
            logger.info(f"订单 {order.order_id} 审批通过: 杠杆{proposed_leverage:.2f}, 品种敞口{symbol_notional/equity:.2%}")
            return True, f"通过 (杠杆{proposed_leverage:.2f})"

    def _reserve_budget(self, order_id: str, notional: float, delta: float, symbol: str):
        self._pending_budget[order_id] = (notional, delta, symbol, time.time())
        self._pending_notional += notional
        self._pending_delta += delta

    def _release_budget_internal(self, order_id: str):
        if order_id in self._pending_budget:
            notional, delta, _, _ = self._pending_budget.pop(order_id)
            self._pending_notional -= notional
            self._pending_delta -= delta

    async def release_risk_budget(self, order_id: str):
        """外部释放预算（订单成交/取消后调用）"""
        async with self._lock:
            self._release_budget_internal(order_id)

    def _cleanup_expired_reservations(self, timeout_sec: int = 120):
        now = time.time()
        expired = [oid for oid, (_, _, _, ts) in self._pending_budget.items() if now - ts > timeout_sec]
        for oid in expired:
            logger.warning(f"释放过期预算预留: {oid}")
            self._release_budget_internal(oid)

    def _compute_aggregates(self, positions: List[Position]) -> Tuple[float, float, Dict[str, float]]:
        total_notional = 0.0
        total_delta = 0.0
        symbol_exposure: Dict[str, float] = {}
        for pos in positions:
            if not getattr(pos, 'is_active', True):
                continue
            notional = abs(pos.size * pos.current_price)
            total_notional += notional
            total_delta += pos.size * pos.current_price * (1 if pos.direction == 'LONG' else -1)
            symbol_exposure[pos.symbol] = symbol_exposure.get(pos.symbol, 0.0) + notional
        return total_notional, total_delta, symbol_exposure

    def _activate_circuit_breaker(self, loss_pct: float, reason: str = ""):
        self._circuit_breaker_active = True
        minutes = 60
        for pct, dur in sorted(self.cool_down_rules.items(), reverse=True):
            if loss_pct >= pct:
                minutes = dur
                break
        self._circuit_breaker_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        msg = f"熔断触发: {reason} 亏损{loss_pct:.2%}, 冷却{minutes}分钟"
        logger.warning(msg)

    async def force_circuit_breaker(self, operator: str = "system"):
        async with self._lock:
            self._circuit_breaker_active = True
            self._circuit_breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
            logger.warning(f"管理员 {operator} 强制熔断")

    async def reset_circuit_breaker(self, operator: str):
        async with self._lock:
            self._circuit_breaker_active = False
            self._circuit_breaker_until = None
            self._consecutive_losses = 0
            logger.info(f"管理员 {operator} 重置熔断")

    async def update_daily_pnl(self, realized_pnl: float) -> None:
        """更新已实现盈亏及连续亏损计数"""
        if not math.isfinite(realized_pnl):
            return
        async with self._lock:
            self._daily_pnl += realized_pnl
            # 连续亏损计数
            if realized_pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0
            # 每日重置
            today = datetime.now(timezone.utc).date()
            last_reset_day = datetime.utcfromtimestamp(self._last_reset_ts).date()
            if today != last_reset_day:
                self._daily_pnl = realized_pnl
                self._daily_start_equity = 0.0
                self._consecutive_losses = 0
                self._last_reset_ts = time.time()
            # 清理过期预算
            self._cleanup_expired_reservations()

    async def initialize_daily_equity(self, equity: float):
        async with self._lock:
            self._daily_start_equity = equity
            self._last_reset_ts = time.time()

    async def get_risk_snapshot(self) -> dict:
        async with self._lock:
            return {
                "daily_pnl": self._daily_pnl,
                "daily_start_equity": self._daily_start_equity,
                "circuit_breaker_active": self._circuit_breaker_active,
                "consecutive_losses": self._consecutive_losses,
                "pending_notional": self._pending_notional,
                "pending_delta": self._pending_delta,
            }

    async def is_circuit_breaker_triggered(self) -> bool:
        return self._circuit_breaker_active
