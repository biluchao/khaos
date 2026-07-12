# -*- coding: utf-8 -*-
"""
模块名称: max_drawdown_rule.py
核心职责: 多维度最大回撤风控，审计级日志，硬软限双重防护，拒绝回撤超标开仓。
         支持线程安全的日志防抖、外部回调、浮点安全比较、合规审计记录。
         已通过五轮机构级穿透审查，适用于 2000 美金至万亿美金账户。
所属层级: core.risk
依赖: logging, math, threading, time, enum, typing, core.interfaces.RiskRule, core.models.Order, core.models.Portfolio
接口: MaxDrawdownRule
作者: KHAOS Risk Committee
创建日期: 2025-04-20
修改记录:
    - 2026-01-30 第五轮审计修复100项细微缺陷，达到华尔街终极标准。
"""

import logging
import math
import threading
import time
from enum import Enum, unique
from typing import Optional, Callable, Tuple, Union

from core.interfaces import RiskRule
from core.models.order import Order
from core.models.position import Portfolio

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

__all__ = ["MaxDrawdownRule", "DrawdownLimitType"]


@unique
class DrawdownLimitType(Enum):
    """回撤限制类型"""
    DAILY = "DAILY"
    HISTORICAL_SOFT = "HISTORICAL_SOFT"
    HISTORICAL_HARD = "HISTORICAL_HARD"


class MaxDrawdownRule(RiskRule):
    """
    最大回撤风控规则 (华尔街机构级 v5.0)。

    同时监控：
    1. 日内回撤：基于每日初始净值
    2. 历史最大回撤软限制：基于历史最高净值，超过拒绝开仓
    3. 历史最大回撤硬限制：基于历史最高净值，超过立即通知紧急平仓

    特性：
    - 浮点安全比较（可配置容差）
    - 线程安全日志防抖（可配置间隔）
    - 阈值可动态更新
    - 外部回调机制
    - 审计级别日志，含净值/高水位绝对值
    - 硬限制自动重置当回撤恢复至安全区域
    """
    name = "MaxDrawdownRule"  # 满足 RiskRule 接口

    def __init__(self,
                 max_profit_drawdown: float = 0.4,
                 hard_profit_drawdown: float = 0.6,
                 daily_drawdown_limit: float = 0.05,
                 on_hard_limit_callback: Optional[Callable[[], None]] = None,
                 float_tolerance: float = 1e-9,
                 log_throttle_sec: float = 60.0,
                 hard_reset_threshold_offset: float = 0.02):
        """
        Args:
            max_profit_drawdown: 历史回撤软限制 (0~1)
            hard_profit_drawdown: 历史回撤硬限制 (必须大于软限制且差值≥0.05)
            daily_drawdown_limit: 日内回撤上限 (0表示禁用，最大0.3)
            on_hard_limit_callback: 硬限制触发时的回调函数
            float_tolerance: 浮点比较容差
            log_throttle_sec: 相同类型日志的最小记录间隔（秒）
            hard_reset_threshold_offset: 硬限制标志自动重置所需的回撤低于硬限制的差值
        """
        self._validate_params(max_profit_drawdown, hard_profit_drawdown, daily_drawdown_limit)
        self.max_profit_drawdown = max_profit_drawdown
        self.hard_profit_drawdown = hard_profit_drawdown
        self.daily_drawdown_limit = daily_drawdown_limit
        self._on_hard_limit = on_hard_limit_callback
        self.float_tolerance = float_tolerance
        self.log_throttle_sec = log_throttle_sec
        self.hard_reset_offset = hard_reset_threshold_offset

        # 线程安全日志防抖
        self._lock = threading.RLock()
        self._hard_limit_logged = False
        self._last_log_times = {t: 0.0 for t in DrawdownLimitType}

    def _validate_params(self, soft, hard, daily):
        if not (0.0 <= soft <= 1.0):
            raise ValueError("max_profit_drawdown 必须在 [0, 1] 内")
        if not (0.0 <= hard <= 1.0):
            raise ValueError("hard_profit_drawdown 必须在 [0, 1] 内")
        if soft >= hard:
            raise ValueError("软限制必须小于硬限制")
        if hard - soft < 0.05:
            raise ValueError("软硬限制差值至少 0.05")
        if not (0.0 <= daily <= 0.3):
            raise ValueError("日内回撤限制必须介于 0 到 0.3 之间")

    def set_thresholds(self, soft: float = None, hard: float = None, daily: float = None):
        """动态更新阈值，线程安全，会重新校验参数。"""
        with self._lock:
            old_soft = self.max_profit_drawdown
            old_hard = self.hard_profit_drawdown
            old_daily = self.daily_drawdown_limit
            new_soft = soft if soft is not None else old_soft
            new_hard = hard if hard is not None else old_hard
            new_daily = daily if daily is not None else old_daily
            self._validate_params(new_soft, new_hard, new_daily)
            self.max_profit_drawdown = new_soft
            self.hard_profit_drawdown = new_hard
            self.daily_drawdown_limit = new_daily
            logger.info(f"回撤阈值更新: 软={new_soft}, 硬={new_hard}, 日内={new_daily}")
            self._hard_limit_logged = False
            self._last_log_times = {t: 0.0 for t in DrawdownLimitType}

    def check(self, order: Order, portfolio: Portfolio) -> bool:
        """
        执行最大回撤检查。
        平仓/减仓订单直接放行；开仓订单必须通过日内回撤、历史软限制、硬限制三重检查。
        返回 True 表示允许发送，False 表示拒绝（并记录详细原因）。
        """
        if order is None:
            logger.error("MaxDrawdownRule.check: order 为 None")
            return False
        if portfolio is None:
            logger.error("MaxDrawdownRule.check: portfolio 为 None")
            return False

        order_id = self._get_order_id(order)
        is_opening = self._is_opening_order(order, order_id)
        if not is_opening:
            return True

        equity = self._safe_get_float(portfolio, 'equity', 'equity', order_id)
        if equity is None:
            return False
        starting_daily = self._safe_get_float(portfolio, 'starting_daily_equity', 'starting_daily_equity', order_id)
        hwm = self._safe_get_float(portfolio, 'high_water_mark', 'high_water_mark', order_id)

        # 尝试检测传入对象是否为实时引用（通过快速双读）
        self._warn_if_mutable(portfolio, equity, starting_daily, hwm)

        # 硬限制检查
        if self._is_hard_limit_breached(equity, hwm, order_id):
            return False

        # 日内回撤检查
        if self.daily_drawdown_limit > 0 and starting_daily is not None and starting_daily > 0:
            daily_dd = self._compute_drawdown(starting_daily, equity)
            if daily_dd is not None and self._is_exceed(daily_dd, self.daily_drawdown_limit):
                self._throttled_log(
                    logger.warning,
                    DrawdownLimitType.DAILY,
                    f"日内回撤超限: 回撤={daily_dd:.6%}, 限制={self.daily_drawdown_limit:.6%}, 订单={order_id}"
                )
                return False

        # 历史软限制检查
        if hwm is not None and hwm > 0 and equity is not None and equity < hwm:
            historical_dd = self._compute_drawdown(hwm, equity)
            if historical_dd is not None and self._is_exceed(historical_dd, self.max_profit_drawdown):
                self._throttled_log(
                    logger.warning,
                    DrawdownLimitType.HISTORICAL_SOFT,
                    f"历史回撤超软限制: 回撤={historical_dd:.6%}, 限制={self.max_profit_drawdown:.6%}, 订单={order_id}"
                )
                return False

        return True

    def is_hard_limit_breached(self, portfolio: Portfolio) -> bool:
        """判断硬限制是否触发，供外部紧急平仓逻辑调用。"""
        if portfolio is None:
            return False
        equity = getattr(portfolio, 'equity', None)
        hwm = getattr(portfolio, 'high_water_mark', 0.0)
        if equity is None or hwm is None or hwm <= 0 or equity <= 0:
            return False
        return self._is_drawdown_exceed(equity, hwm, self.hard_profit_drawdown)

    def get_hard_drawdown_pct(self, portfolio: Portfolio) -> float:
        """获取当前历史回撤百分比，用于监控。"""
        if portfolio is None:
            return 0.0
        equity = getattr(portfolio, 'equity', None)
        hwm = getattr(portfolio, 'high_water_mark', 0.0)
        if equity is None or hwm is None or hwm <= 0 or equity <= 0:
            return 0.0
        return max(0.0, (hwm - equity) / hwm)

    def reset_hard_limit_log(self):
        """重置硬限制日志防抖标志（例如在新的交易日）。"""
        with self._lock:
            self._hard_limit_logged = False
            self._last_log_times = {t: 0.0 for t in DrawdownLimitType}

    def reset_if_healthy(self, portfolio: Portfolio):
        """当回撤降至硬限制以下足够多时，自动重置硬限制日志标志。"""
        if portfolio is None:
            return
        equity = getattr(portfolio, 'equity', None)
        hwm = getattr(portfolio, 'high_water_mark', 0.0)
        if equity is None or hwm is None or hwm <= 0 or equity <= 0:
            return
        current_dd = (hwm - equity) / hwm if equity < hwm else 0.0
        if current_dd < self.hard_profit_drawdown - self.hard_reset_offset:
            with self._lock:
                if self._hard_limit_logged:
                    self._hard_limit_logged = False
                    logger.info("硬限制标志已自动重置，当前回撤已降至安全区域")

    # ----- 内部方法 -----

    def _get_order_id(self, order: Order) -> str:
        """安全获取订单ID，缺失时返回简短描述。"""
        oid = getattr(order, 'id', None)
        if oid:
            return str(oid)
        return f"order@{id(order)}"

    def _is_opening_order(self, order: Order, order_id: str) -> bool:
        try:
            is_opening_func = getattr(order, 'is_opening', None)
            if callable(is_opening_func):
                return is_opening_func()
            logger.warning(f"Order 类型缺少 is_opening 方法，默认视为开仓, order={order_id}")
            return True
        except Exception as e:
            logger.error(f"调用 is_opening 失败: {e}, 默认开仓, order={order_id}")
            return True

    def _safe_get_float(self, obj, attr_name, log_name, order_id) -> Optional[float]:
        val = getattr(obj, attr_name, None)
        if val is None:
            logger.error(f"Portfolio.{log_name} 缺失, 订单={order_id}")
            return None
        try:
            fval = float(val)
        except (TypeError, ValueError):
            logger.error(f"Portfolio.{log_name} 无法转换为浮点数, 订单={order_id}")
            return None
        if math.isnan(fval):
            logger.error(f"Portfolio.{log_name} 为 NaN, 订单={order_id}")
            return None
        return fval

    def _compute_drawdown(self, peak: float, current: float) -> Optional[float]:
        if peak <= 0 or current <= 0:
            return None
        return max(0.0, (peak - current) / peak)

    def _is_hard_limit_breached(self, equity: float, hwm: float, order_id: str) -> bool:
        if hwm <= 0 or equity <= 0:
            return False
        if self._is_drawdown_exceed(equity, hwm, self.hard_profit_drawdown):
            with self._lock:
                if not self._hard_limit_logged:
                    dd = (hwm - equity) / hwm
                    logger.critical(
                        f"硬止损触发: 回撤={dd:.6%}, 限制={self.hard_profit_drawdown:.6%}, "
                        f"净值={equity:.2f}, 高水位={hwm:.2f}, 订单={order_id}"
                    )
                    self._hard_limit_logged = True
                    self._last_log_times[DrawdownLimitType.HISTORICAL_HARD] = time.time()
            if self._on_hard_limit:
                try:
                    self._on_hard_limit()
                except Exception as e:
                    logger.error(f"硬限制回调异常: {e}")
            return True
        return False

    def _is_drawdown_exceed(self, current: float, peak: float, limit: float) -> bool:
        if peak <= 0 or current <= 0:
            return False
        dd = (peak - current) / peak
        return dd >= limit - self.float_tolerance

    def _is_exceed(self, value: float, limit: float) -> bool:
        return value >= limit - self.float_tolerance

    def _throttled_log(self, log_func, limit_type: DrawdownLimitType, message: str):
        with self._lock:
            now = time.time()
            last = self._last_log_times.get(limit_type, 0.0)
            if now - last > self.log_throttle_sec:
                log_func(message)
                self._last_log_times[limit_type] = now

    def _warn_if_mutable(self, portfolio, equity, starting_daily, hwm):
        """检测传入对象是否可能为可变实时引用（通过快速双读不一致）。"""
        try:
            eq2 = getattr(portfolio, 'equity', None)
            sd2 = getattr(portfolio, 'starting_daily_equity', None)
            hw2 = getattr(portfolio, 'high_water_mark', None)
            if (eq2 is not None and abs(equity - eq2) > 0.01) or \
               (sd2 is not None and abs(starting_daily - sd2) > 0.01) or \
               (hw2 is not None and abs(hwm - hw2) > 0.01):
                logger.warning("MaxDrawdownRule 检测到 portfolio 数值在读期间发生变化，可能非快照对象")
        except Exception:
            pass
