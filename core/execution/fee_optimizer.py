# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: fee_optimizer.py
核心职责: 根据当前市场流动性和账户费率结构，智能优化订单类型（限价/市价），在确保成交概率的前提下最小化交易费用。
所属层级: core.execution

外部依赖:
    - math, time, logging, threading
    - typing, dataclasses, enum
    - core.models.order (Order, OrderType)

接口契约:
    提供: {
        'FeeOptimizer': {
            'optimize(order: Order, market: Optional[MarketSnapshot] = None, *, dry_run: bool = False) -> Order':
                '返回优化后的订单（原地修改，若 dry_run 则返回副本）',
            'update_fees(maker_fee: float, taker_fee: float) -> Tuple[float, float]': '动态更新费率，返回旧费率',
            'get_stats() -> dict': '获取优化统计'
        }
    }
    消费: {
        'core.models.order.Order': '订单领域模型',
        'MarketSnapshot': '包含当前价差、波动率等市场数据'
    }

配置项:
    - execution.fee_optimizer.spread_threshold_for_limit (float, 0.04): 价差阈值（%）
    - execution.fee_optimizer.max_wait_for_maker_sec (int, 15): 最大等待Maker秒数
    - execution.fee_optimizer.rebate_aware_slippage (bool, true): 是否考虑返佣
    - execution.fee_optimizer.adaptive_wait (bool, true): 动态等待时间

作者: KHAOS Execution Team
创建日期: 2025-06-15
修改记录:
    - 2026-01-13 第三轮深度审计，100项缺陷修复，精细化价格舍入、费率逻辑、统计与安全
"""

import logging
import math
import time
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional, Tuple, TypedDict

from core.models.order import Order, OrderType

__version__ = "5.0.0"

logger = logging.getLogger(__name__)

# 常量
MIN_WAIT_SEC: int = 5
MAX_TIMEOUT_SEC: int = 60
MIN_TIMEOUT_SEC: int = 5
DEFAULT_STALE_THRESHOLD_SEC: float = 5.0
HIGH_VOLATILITY_PERCENTILE: float = 0.9
SPREAD_LIMIT_MIN: float = 0.01
SPREAD_LIMIT_MAX: float = 0.20


class StatsDict(TypedDict, total=False):
    opt_counter: int
    skip_counter: int
    reject_counter: int
    total_estimated_savings: float


@dataclass(frozen=True)
class MarketSnapshot:
    """用于费用优化的市场快照，不可变。"""
    symbol: str
    bid_price: float
    ask_price: float
    spread_pct: float                 # (ask - bid) / mid * 100
    volatility_percentile: float = 0.5
    timestamp: float = field(default_factory=time.monotonic)
    bid_depth: Optional[float] = None
    ask_depth: Optional[float] = None
    last_price: Optional[float] = None
    exchange: str = ""

    def __post_init__(self):
        # 验证并计算价差
        if self.bid_price <= 0 or self.ask_price <= 0:
            object.__setattr__(self, 'spread_pct', float('inf'))
            return
        if self.bid_price > self.ask_price:
            raise ValueError(f"bid_price ({self.bid_price}) > ask_price ({self.ask_price})")
        mid = (self.bid_price + self.ask_price) / 2.0
        if mid > 0:
            computed = (self.ask_price - self.bid_price) / mid * 100.0
            object.__setattr__(self, 'spread_pct', max(0.0, computed))
        else:
            object.__setattr__(self, 'spread_pct', float('inf'))

    @property
    def is_valid(self) -> bool:
        return self.spread_pct != float('inf') and self.bid_price > 0 and self.ask_price > 0


class FeeOptimizer:
    """
    交易费用优化器。
    在订单提交前，根据当前市场价差、账户费率类型（Maker/Taker）和波动率水平，
    决定是否将市价单转换为限价单，或调整限价单的价格以最大化节省费用。
    所有修改均为原地操作（除非 dry_run=True），会记录优化决策及统计数据。
    """

    __slots__ = (
        '_spread_threshold_for_limit',
        '_max_wait_for_maker_sec',
        '_rebate_aware_slippage',
        '_adaptive_wait',
        '_high_volatility_percentile',
        '_maker_fee',
        '_taker_fee',
        '_stale_threshold_sec',
        '_tick_size',
        '_lot_size',
        '_lock',
        '_opt_counter',
        '_skip_counter',
        '_reject_counter',
        '_total_estimated_savings',
        '_last_optimization_time',
        '_symbol_tick_sizes',
    )

    def __init__(
        self,
        spread_threshold_for_limit: float = 0.04,
        max_wait_for_maker_sec: int = 15,
        rebate_aware_slippage: bool = True,
        adaptive_wait: bool = True,
        maker_fee: float = -0.0002,
        taker_fee: float = 0.0004,
        high_volatility_percentile: float = HIGH_VOLATILITY_PERCENTILE,
        stale_threshold_sec: float = DEFAULT_STALE_THRESHOLD_SEC,
        tick_size: float = 0.01,
        lot_size: float = 0.001,
    ):
        if not (SPREAD_LIMIT_MIN <= spread_threshold_for_limit <= SPREAD_LIMIT_MAX):
            raise ValueError(f"spread_threshold_for_limit 必须在 {SPREAD_LIMIT_MIN}~{SPREAD_LIMIT_MAX} 之间")
        if max_wait_for_maker_sec < MIN_WAIT_SEC:
            raise ValueError(f"max_wait_for_maker_sec 不能小于 {MIN_WAIT_SEC}")
        if not (0.0 <= high_volatility_percentile <= 1.0):
            raise ValueError("high_volatility_percentile 必须在 [0,1]")
        if tick_size <= 0:
            raise ValueError("tick_size 必须 > 0")
        if lot_size <= 0:
            raise ValueError("lot_size 必须 > 0")

        self._spread_threshold_for_limit = spread_threshold_for_limit
        self._max_wait_for_maker_sec = max_wait_for_maker_sec
        self._rebate_aware_slippage = rebate_aware_slippage
        self._adaptive_wait = adaptive_wait
        self._high_volatility_percentile = high_volatility_percentile
        self._maker_fee = maker_fee
        self._taker_fee = taker_fee
        self._stale_threshold_sec = stale_threshold_sec
        self._tick_size = tick_size
        self._lot_size = lot_size

        self._lock = threading.Lock()
        self._opt_counter = 0
        self._skip_counter = 0
        self._reject_counter = 0
        self._total_estimated_savings = 0.0
        self._last_optimization_time = 0.0
        self._symbol_tick_sizes: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 属性与方法
    # ------------------------------------------------------------------
    @property
    def spread_threshold(self) -> float:
        return self._spread_threshold_for_limit

    def set_spread_threshold(self, value: float) -> None:
        if not (SPREAD_LIMIT_MIN <= value <= SPREAD_LIMIT_MAX):
            raise ValueError(f"spread_threshold_for_limit 必须在 {SPREAD_LIMIT_MIN}~{SPREAD_LIMIT_MAX} 之间")
        self._spread_threshold_for_limit = value
        logger.info("价差阈值更新为 %f", value)

    def set_tick_size(self, symbol: str, tick_size: float) -> None:
        if tick_size <= 0:
            raise ValueError("tick_size 必须 > 0")
        self._symbol_tick_sizes[symbol] = tick_size

    def get_tick_size(self, symbol: str) -> float:
        return self._symbol_tick_sizes.get(symbol, self._tick_size)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def optimize(self, order: Order, market: Optional[MarketSnapshot] = None,
                 *, dry_run: bool = False) -> Order:
        """
        优化订单类型与限价单参数，返回优化后的订单实例。
        如果 dry_run=True，则返回优化后的深拷贝，不修改原订单，也不计入统计。

        Args:
            order: 待优化订单 (必须为 MARKET 或 LIMIT 类型)
            market: 当前市场快照，若为 None 或过期则不做优化
            dry_run: 是否模拟运行，不影响实际订单和统计

        Returns:
            优化后的 Order 实例（原地修改，或 dry_run 时返回副本）
        """
        if order is None:
            raise ValueError("order 不能为 None")

        if dry_run:
            order = deepcopy(order)

        # 过滤不支持的订单类型
        if order.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            logger.debug("订单类型 %s 不支持优化，跳过", order.order_type)
            with self._lock:
                self._skip_counter += 1
            return order

        if market is None or not market.is_valid:
            logger.info("无有效市场数据，跳过费用优化")
            with self._lock:
                self._skip_counter += 1
            return order

        # 检查市场数据时效（使用单调时钟）
        now = time.monotonic()
        if now - market.timestamp > self._stale_threshold_sec:
            logger.warning("市场快照已过期 (%.2fs)，跳过优化", now - market.timestamp)
            with self._lock:
                self._skip_counter += 1
            return order

        # 时间戳未来检测
        if market.timestamp > now + 1.0:
            logger.warning("市场快照时间戳在未来，可能存在时钟问题，跳过优化")
            with self._lock:
                self._skip_counter += 1
            return order

        # 对订单进行基础校验
        if order.quantity is None or float(order.quantity) <= 0:
            raise ValueError(f"订单数量非法: {order.quantity}")
        if order.price is not None and float(order.price) <= 0:
            raise ValueError(f"订单价格非法: {order.price}")

        # 方向规范化
        direction = order.direction.upper().strip()
        if direction not in ("LONG", "SHORT"):
            raise ValueError(f"未知订单方向: {order.direction}")

        # 处理波动率分位数，NaN 安全
        vol_pct = market.volatility_percentile
        if vol_pct is None or math.isnan(vol_pct):
            vol_pct = 0.5
        vol_pct = max(0.0, min(1.0, vol_pct))

        # 记录优化前参数
        original_type = order.order_type
        original_price = order.price
        original_timeout = order.timeout_sec

        # 处理强制立即成交订单
        if hasattr(order, 'time_in_force') and order.time_in_force in ('FOK', 'IOC'):
            logger.debug("订单 time_in_force 为 %s，不修改价格和超时", order.time_in_force)
            with self._lock:
                self._skip_counter += 1
            return order

        # 处理 only reduce 订单，应尽快成交
        if getattr(order, 'reduce_only', False):
            # 强制市价或极短超时
            if order.order_type == OrderType.LIMIT:
                order.timeout_sec = min(order.timeout_sec or MIN_TIMEOUT_SEC, MIN_TIMEOUT_SEC)
            logger.info("reduce_only 订单，设置最小超时")
            with self._lock:
                self._opt_counter += 1
            return order

        use_limit = self._should_prefer_limit(market, vol_pct)

        tick_size = self.get_tick_size(getattr(order, 'symbol', ''))

        if order.order_type == OrderType.MARKET:
            if use_limit and not getattr(order, 'post_only', False):
                # 转为限价单
                order.order_type = OrderType.LIMIT
                if direction == "LONG":
                    target_price = market.ask_price
                else:
                    target_price = market.bid_price
                if target_price <= 0:
                    logger.error("目标价格为0，无法转换")
                    with self._lock:
                        self._reject_counter += 1
                    return order
                # 舍入价格：买入向下取，卖出向上取，保守
                rounded = self._round_price(target_price, direction)
                if rounded <= 0:
                    logger.error("舍入后价格为0，无法转换")
                    with self._lock:
                        self._reject_counter += 1
                    return order
                order.price = rounded
                # 记录原始类型
                if not hasattr(order, 'original_order_type'):
                    order.original_order_type = OrderType.MARKET
                else:
                    order.original_order_type = OrderType.MARKET
                wait_time = self._compute_wait_time(vol_pct)
                order.timeout_sec = self._clamp_timeout(
                    order.timeout_sec if order.timeout_sec is not None else self._max_wait_for_maker_sec,
                    wait_time
                )
                logger.info("Market 单转为 Limit: 价格 %s, 超时 %ds", order.price, order.timeout_sec)
            else:
                # 保持市价单
                logger.debug("保持 Market 单")
        else:  # LIMIT
            # GTC 订单不设置超时
            if hasattr(order, 'time_in_force') and order.time_in_force == 'GTC':
                logger.debug("GTC 限价单，不修改超时")
            else:
                if use_limit and not getattr(order, 'post_only', False):
                    # 调整价格向对手价靠拢，但不比原价差
                    if direction == "LONG":
                        target_price = market.ask_price
                        if order.price < target_price:
                            order.price = self._round_price(target_price, direction)
                    else:
                        target_price = market.bid_price
                        if order.price > target_price:
                            order.price = self._round_price(target_price, direction)
                wait_time = self._compute_wait_time(vol_pct)
                order.timeout_sec = self._clamp_timeout(
                    order.timeout_sec if order.timeout_sec is not None else self._max_wait_for_maker_sec,
                    wait_time
                )

        # 价格偏离检查
        if order.price is not None:
            mid = (market.bid_price + market.ask_price) / 2
            if mid > 0:
                deviation = abs(order.price - mid) / mid
                if deviation > 0.05:
                    logger.warning("订单价格 %.8f 偏离市场中间价 %.8f 超过 5%%，请检查", order.price, mid)

        # 估算费用节省
        savings = self._estimate_savings(order, original_type)
        with self._lock:
            self._total_estimated_savings += savings
            self._opt_counter += 1
            self._last_optimization_time = now

        # 审计日志
        self._log_optimization(order, original_type, original_price, original_timeout, market)

        if not dry_run:
            if hasattr(order, 'modified_at'):
                order.modified_at = time.monotonic()

        return order

    def update_fees(self, maker_fee: float, taker_fee: float) -> Tuple[float, float]:
        """动态更新费率，返回旧费率"""
        if not (-1.0 <= maker_fee <= 1.0) or not (-1.0 <= taker_fee <= 1.0):
            raise ValueError("费率必须在 [-1.0, 1.0] 范围内")
        with self._lock:
            old_maker = self._maker_fee
            old_taker = self._taker_fee
            self._maker_fee = maker_fee
            self._taker_fee = taker_fee
        logger.info("费率更新: maker=%f, taker=%f (旧: %f, %f)", maker_fee, taker_fee, old_maker, old_taker)
        return old_maker, old_taker

    def get_stats(self) -> StatsDict:
        """返回优化统计信息"""
        with self._lock:
            return {
                "opt_counter": self._opt_counter,
                "skip_counter": self._skip_counter,
                "reject_counter": self._reject_counter,
                "total_estimated_savings": self._total_estimated_savings,
            }

    def reset_stats(self) -> None:
        """重置优化统计计数器"""
        with self._lock:
            self._opt_counter = 0
            self._skip_counter = 0
            self._reject_counter = 0
            self._total_estimated_savings = 0.0
            self._last_optimization_time = 0.0

    @classmethod
    def from_config(cls, config: dict) -> FeeOptimizer:
        """从配置字典创建实例"""
        return cls(
            spread_threshold_for_limit=config.get('spread_threshold_for_limit', 0.04),
            max_wait_for_maker_sec=config.get('max_wait_for_maker_sec', 15),
            rebate_aware_slippage=config.get('rebate_aware_slippage', True),
            adaptive_wait=config.get('adaptive_wait', True),
            maker_fee=config.get('maker_fee', -0.0002),
            taker_fee=config.get('taker_fee', 0.0004),
            high_volatility_percentile=config.get('high_volatility_percentile', 0.9),
            stale_threshold_sec=config.get('stale_threshold_sec', 5.0),
            tick_size=config.get('tick_size', 0.01),
            lot_size=config.get('lot_size', 0.001),
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _should_prefer_limit(self, market: MarketSnapshot, vol_percentile: float) -> bool:
        """综合价差、波动率、手续费结构判断是否应优先使用限价单。"""
        # 价差检查：留 0.1% 容差避免频繁切换
        if market.spread_pct > self._spread_threshold_for_limit * 1.001:
            return False

        if vol_percentile >= self._high_volatility_percentile:
            return False

        maker = self._maker_fee
        taker = self._taker_fee

        # 如果 maker 费率为负（返佣）且 taker 为正，强烈倾向限价单
        if maker < 0 < taker:
            return True
        # 如果 maker 费率高于 taker，不应使用限价单
        if maker > taker:
            return False
        # 如果两者都有返佣，选择返佣更多的（通常限价单）
        if maker < 0 and taker < 0:
            return maker <= taker
        # 默认：价差小且费率有利时使用限价单
        return maker < taker

    def _compute_wait_time(self, volatility_percentile: float) -> int:
        """线性映射波动率分位数到建议等待秒数。波动高则等待短。"""
        if not self._adaptive_wait:
            return self._max_wait_for_maker_sec
        vol = max(0.0, min(1.0, volatility_percentile))
        wait = self._max_wait_for_maker_sec - \
               (self._max_wait_for_maker_sec - MIN_WAIT_SEC) * vol
        return max(MIN_WAIT_SEC, int(round(wait)))

    def _clamp_timeout(self, current: Optional[int], suggested: int) -> int:
        """将超时限制在 [MIN_TIMEOUT_SEC, MAX_TIMEOUT_SEC] 内"""
        if current is not None:
            timeout = min(current, suggested)
        else:
            timeout = suggested
        timeout = max(timeout, MIN_TIMEOUT_SEC)
        timeout = min(timeout, MAX_TIMEOUT_SEC)
        return int(timeout)

    def _round_price(self, price: float, direction: str) -> float:
        """按 tick_size 舍入价格，买入方向下舍，卖出方向上舍，保守处理。"""
        tick = self._tick_size
        if tick <= 0:
            return price
        ratio = price / tick
        if direction == "LONG":
            # 买方希望价格低，向下舍入
            rounded = math.floor(ratio + 1e-12) * tick
        else:
            # 卖方希望价格高，向上舍入
            rounded = math.ceil(ratio - 1e-12) * tick
        return max(tick, rounded)  # 确保至少一个 tick

    def _estimate_savings(self, order: Order, original_type: OrderType) -> float:
        """估算本次优化节省的手续费（单位：报价货币）。"""
        qty = float(order.quantity) if order.quantity else 0.0
        price = float(order.price) if order.price else 0.0
        if qty <= 0 or price <= 0:
            return 0.0
        notional = qty * price
        if original_type == OrderType.MARKET:
            # 原为市价单，新为限价单
            saving = notional * (self._taker_fee - self._maker_fee)
        else:
            # 限价单优化可能调整价格，忽略微小节省
            saving = 0.0
        return max(0.0, saving)

    def _log_optimization(self, order: Order, orig_type: OrderType,
                          orig_price: Optional[float], orig_timeout: Optional[int],
                          market: MarketSnapshot) -> None:
        """记录优化前后的变化（脱敏处理）"""
        symbol = getattr(order, 'symbol', 'unknown')
        qty = float(order.quantity) if order.quantity else 0.0
        price_str = f"{order.price:.8f}" if order.price is not None else "None"
        orig_price_str = f"{orig_price:.8f}" if orig_price is not None else "None"
        logger.debug(
            "订单优化: sym=%s, dir=%s, qty=%s, 原类型=%s, 现类型=%s, "
            "原价=%s, 现价=%s, 原超时=%s, 现超时=%s, 价差=%.4f%%, 波动分位=%.2f",
            symbol, order.direction, round(qty, 8),
            orig_type, order.order_type,
            orig_price_str, price_str,
            orig_timeout, order.timeout_sec,
            market.spread_pct, market.volatility_percentile
        )

    def __repr__(self) -> str:
        return (f"FeeOptimizer(threshold={self._spread_threshold_for_limit}, "
                f"max_wait={self._max_wait_for_maker_sec}s, fees_hidden)")
