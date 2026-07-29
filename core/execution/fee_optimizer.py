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
    - 2026-07-29 第四轮生产级加固：数值安全、类型防御、并发细化、边界完整
    - 2026-07-29 机构级审计加固 (v5.2.0)：激活死配置、资源泄漏防护、统计溢出保护
    - 2026-07-29 机构级穿透审计 (v5.3.0)：Maker定价方向、部分变异回滚、费率快照
    - 2026-07-29 机构级再审计 (v5.4.0)：dry_run统计契约、savings仅在真实转换时累计、
      零价差不转maker、TIF精确匹配、配置快照完整、计数器路径统一
"""

import logging
import math
import time
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional, Tuple, TypedDict, Any

from core.models.order import Order, OrderType

__version__ = "5.4.0"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MIN_WAIT_SEC: int = 5
MAX_TIMEOUT_SEC: int = 60
MIN_TIMEOUT_SEC: int = 5
DEFAULT_STALE_THRESHOLD_SEC: float = 5.0
HIGH_VOLATILITY_PERCENTILE: float = 0.9
SPREAD_LIMIT_MIN: float = 0.01
SPREAD_LIMIT_MAX: float = 0.20

# 数值与资源安全常量（机构级）
_EPS: float = 1e-12
_MAX_REASONABLE_NOTIONAL: float = 1e15
_MAX_PRICE_DEVIATION: float = 0.05
_MAX_ABS_FEE: float = 1.0
_FUTURE_TIMESTAMP_TOLERANCE_SEC: float = 2.0
_MAX_SYMBOL_TICK_CACHE: int = 4096
_MAX_SAVINGS_ACCUM: float = 1e18
_BID_ASK_CROSS_TOLERANCE: float = 1e-8

# 立即成交类 TIF（精确 token，避免子串误匹配）
_IMMEDIATE_TIF_TOKENS = frozenset({
    'FOK', 'IOC', 'FILLORKILL', 'IMMEDIATEORCANCEL',
    'FILL_OR_KILL', 'IMMEDIATE_OR_CANCEL',
})


class StatsDict(TypedDict, total=False):
    opt_counter: int
    skip_counter: int
    reject_counter: int
    total_estimated_savings: float


def _safe_float(value: Any, default: float = 0.0, *, allow_negative: bool = False) -> float:
    """安全转换为 float，处理 None / Decimal / str / NaN / Inf / Overflow。"""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    if not allow_negative and f < 0.0:
        return default
    return f


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _normalize_tif(tif: Any) -> str:
    """将 time_in_force（Enum/str/None）规范为无分隔大写 token。"""
    if tif is None:
        return ""
    # Enum: prefer .name then .value
    raw = getattr(tif, 'name', None) or getattr(tif, 'value', None) or tif
    s = str(raw).upper().strip()
    # 去掉枚举类前缀 "TIMEINFORCE." 等
    if '.' in s:
        s = s.rsplit('.', 1)[-1]
    return s.replace('-', '_').replace(' ', '_')


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

    def __post_init__(self) -> None:
        bid = _safe_float(self.bid_price, default=0.0, allow_negative=False)
        ask = _safe_float(self.ask_price, default=0.0, allow_negative=False)
        if bid <= 0.0 or ask <= 0.0:
            object.__setattr__(self, 'spread_pct', float('inf'))
            return
        if bid > ask:
            if (bid - ask) / max(ask, _EPS) > _BID_ASK_CROSS_TOLERANCE:
                raise ValueError(f"bid_price ({bid}) > ask_price ({ask})")
            object.__setattr__(self, 'spread_pct', 0.0)
            return
        mid = (bid + ask) * 0.5
        if mid <= 0.0 or math.isinf(mid) or math.isnan(mid):
            object.__setattr__(self, 'spread_pct', float('inf'))
            return
        computed = (ask - bid) / mid * 100.0
        if math.isnan(computed) or math.isinf(computed):
            object.__setattr__(self, 'spread_pct', float('inf'))
        else:
            object.__setattr__(self, 'spread_pct', max(0.0, computed))

    @property
    def is_valid(self) -> bool:
        return (
            self.spread_pct != float('inf')
            and _safe_float(self.bid_price) > 0.0
            and _safe_float(self.ask_price) > 0.0
            and not math.isnan(self.spread_pct)
        )

    @property
    def mid_price(self) -> float:
        """安全中间价，无效时返回 0.0。"""
        bid = _safe_float(self.bid_price)
        ask = _safe_float(self.ask_price)
        if bid <= 0.0 or ask <= 0.0:
            return 0.0
        mid = (bid + ask) * 0.5
        if math.isnan(mid) or math.isinf(mid):
            return 0.0
        return mid


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
        st = _safe_float(spread_threshold_for_limit, default=0.04)
        if not (SPREAD_LIMIT_MIN <= st <= SPREAD_LIMIT_MAX):
            raise ValueError(
                f"spread_threshold_for_limit 必须在 {SPREAD_LIMIT_MIN}\~{SPREAD_LIMIT_MAX} 之间"
            )
        mwait = int(max(MIN_WAIT_SEC, _safe_float(max_wait_for_maker_sec, default=15)))
        if mwait < MIN_WAIT_SEC:
            raise ValueError(f"max_wait_for_maker_sec 不能小于 {MIN_WAIT_SEC}")
        hvp = _clamp(_safe_float(high_volatility_percentile, default=0.9), 0.0, 1.0)
        ts = _safe_float(tick_size, default=0.01)
        if ts <= 0.0:
            raise ValueError("tick_size 必须 > 0")
        ls = _safe_float(lot_size, default=0.001)
        if ls <= 0.0:
            raise ValueError("lot_size 必须 > 0")
        mf = _clamp(
            _safe_float(maker_fee, default=-0.0002, allow_negative=True),
            -_MAX_ABS_FEE, _MAX_ABS_FEE,
        )
        tf = _clamp(
            _safe_float(taker_fee, default=0.0004, allow_negative=True),
            -_MAX_ABS_FEE, _MAX_ABS_FEE,
        )
        sts = _safe_float(stale_threshold_sec, default=DEFAULT_STALE_THRESHOLD_SEC)
        if sts <= 0.0:
            sts = DEFAULT_STALE_THRESHOLD_SEC

        self._spread_threshold_for_limit = st
        self._max_wait_for_maker_sec = mwait
        self._rebate_aware_slippage = bool(rebate_aware_slippage)
        self._adaptive_wait = bool(adaptive_wait)
        self._high_volatility_percentile = hvp
        self._maker_fee = mf
        self._taker_fee = tf
        self._stale_threshold_sec = sts
        self._tick_size = ts
        self._lot_size = ls

        self._lock = threading.RLock()
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
        with self._lock:
            return self._spread_threshold_for_limit

    def set_spread_threshold(self, value: float) -> None:
        v = _safe_float(value, default=self._spread_threshold_for_limit)
        if not (SPREAD_LIMIT_MIN <= v <= SPREAD_LIMIT_MAX):
            raise ValueError(
                f"spread_threshold_for_limit 必须在 {SPREAD_LIMIT_MIN}\~{SPREAD_LIMIT_MAX} 之间"
            )
        with self._lock:
            self._spread_threshold_for_limit = v
        logger.info("价差阈值更新为 %f", v)

    def set_tick_size(self, symbol: str, tick_size: float) -> None:
        ts = _safe_float(tick_size)
        if ts <= 0.0:
            raise ValueError("tick_size 必须 > 0")
        sym = str(symbol or "").strip()
        if not sym:
            raise ValueError("symbol 不能为空")
        with self._lock:
            if (
                len(self._symbol_tick_sizes) >= _MAX_SYMBOL_TICK_CACHE
                and sym not in self._symbol_tick_sizes
            ):
                try:
                    oldest = next(iter(self._symbol_tick_sizes))
                    del self._symbol_tick_sizes[oldest]
                except StopIteration:
                    pass
            self._symbol_tick_sizes[sym] = ts

    def get_tick_size(self, symbol: str) -> float:
        sym = str(symbol or "").strip()
        with self._lock:
            return self._symbol_tick_sizes.get(sym, self._tick_size)

    # ------------------------------------------------------------------
    # 统计辅助（dry_run 契约：绝不计入统计）
    # ------------------------------------------------------------------
    def _bump_skip(self, dry_run: bool) -> None:
        if dry_run:
            return
        with self._lock:
            self._skip_counter += 1

    def _bump_reject(self, dry_run: bool) -> None:
        if dry_run:
            return
        with self._lock:
            self._reject_counter += 1

    def _bump_opt(self, dry_run: bool, savings: float = 0.0) -> None:
        if dry_run:
            return
        with self._lock:
            if savings > 0.0:
                new_total = self._total_estimated_savings + savings
                if (
                    new_total > _MAX_SAVINGS_ACCUM
                    or math.isinf(new_total)
                    or math.isnan(new_total)
                ):
                    self._total_estimated_savings = _MAX_SAVINGS_ACCUM
                else:
                    self._total_estimated_savings = new_total
            self._opt_counter += 1
            self._last_optimization_time = time.monotonic()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def optimize(
        self,
        order: Order,
        market: Optional[MarketSnapshot] = None,
        *,
        dry_run: bool = False,
    ) -> Order:
        """
        优化订单类型与限价单参数，返回优化后的订单实例。
        如果 dry_run=True，则返回优化后的深拷贝，不修改原订单，也不计入统计。
        """
        if order is None:
            raise ValueError("order 不能为 None")

        if dry_run:
            try:
                order = deepcopy(order)
            except Exception as e:
                logger.warning(
                    "dry_run deepcopy 失败 (%s)，回退并跳过优化", type(e).__name__
                )
                # dry_run 不计入统计
                return order

        try:
            ot = order.order_type
        except Exception:
            logger.debug("无法获取 order.order_type，跳过优化")
            self._bump_skip(dry_run)
            return order

        if ot not in (OrderType.MARKET, OrderType.LIMIT):
            logger.debug("订单类型 %s 不支持优化，跳过", ot)
            self._bump_skip(dry_run)
            return order

        if market is None or not getattr(market, 'is_valid', False):
            logger.info("无有效市场数据，跳过费用优化")
            self._bump_skip(dry_run)
            return order

        now = time.monotonic()
        try:
            mts = float(market.timestamp)
        except (TypeError, ValueError, OverflowError):
            mts = now - self._stale_threshold_sec - 1.0

        age = now - mts
        if age > self._stale_threshold_sec:
            logger.warning("市场快照已过期 (%.2fs)，跳过优化", age)
            self._bump_skip(dry_run)
            return order

        if mts > now + _FUTURE_TIMESTAMP_TOLERANCE_SEC:
            logger.warning(
                "市场快照时间戳在未来 (%.2fs)，可能存在时钟问题，跳过优化",
                mts - now,
            )
            self._bump_skip(dry_run)
            return order

        qty = _safe_float(getattr(order, 'quantity', None), default=0.0)
        if qty <= 0.0:
            raise ValueError(f"订单数量非法: {getattr(order, 'quantity', None)}")

        if self._lot_size > 0.0 and qty < self._lot_size * 0.5:
            logger.debug(
                "订单数量 %.8f 显著小于 lot_size %.8f，可能被交易所拒绝",
                qty, self._lot_size,
            )

        price_raw = getattr(order, 'price', None)
        if price_raw is not None:
            p = _safe_float(price_raw, default=0.0)
            if p <= 0.0:
                raise ValueError(f"订单价格非法: {price_raw}")

        direction = str(getattr(order, 'direction', '') or '').upper().strip()
        if direction in ("BUY", "LONG", "B"):
            direction = "LONG"
        elif direction in ("SELL", "SHORT", "S"):
            direction = "SHORT"
        else:
            raise ValueError(f"未知订单方向: {getattr(order, 'direction', None)}")

        vol_pct = _clamp(
            _safe_float(getattr(market, 'volatility_percentile', 0.5), default=0.5),
            0.0, 1.0,
        )

        original_type = ot
        original_price = getattr(order, 'price', None)
        original_timeout = getattr(order, 'timeout_sec', None)

        tif_token = _normalize_tif(getattr(order, 'time_in_force', None))
        # 精确 token 匹配 + 去下划线后再匹配，避免子串误伤
        tif_compact = tif_token.replace('_', '')
        if tif_token in _IMMEDIATE_TIF_TOKENS or tif_compact in _IMMEDIATE_TIF_TOKENS:
            logger.debug("订单 time_in_force 为 %s，不修改价格和超时", tif_token)
            self._bump_skip(dry_run)
            return order

        reduce_only = bool(getattr(order, 'reduce_only', False))
        if reduce_only:
            if ot == OrderType.LIMIT:
                try:
                    cur_to = (
                        int(original_timeout)
                        if original_timeout is not None
                        else MIN_TIMEOUT_SEC
                    )
                    order.timeout_sec = min(cur_to, MIN_TIMEOUT_SEC)
                except (TypeError, ValueError, OverflowError):
                    order.timeout_sec = MIN_TIMEOUT_SEC
            logger.info("reduce_only 订单，设置最小超时")
            self._bump_opt(dry_run, savings=0.0)
            return order

        # 配置/费率完整快照，保证单次 optimize 决策一致
        with self._lock:
            maker_fee_snap = self._maker_fee
            taker_fee_snap = self._taker_fee
            rebate_aware_snap = self._rebate_aware_slippage
            threshold_snap = self._spread_threshold_for_limit
            high_vol_snap = self._high_volatility_percentile
            adaptive_wait_snap = self._adaptive_wait
            max_wait_snap = self._max_wait_for_maker_sec

        use_limit = self._should_prefer_limit(
            market,
            vol_pct,
            maker_fee=maker_fee_snap,
            taker_fee=taker_fee_snap,
            rebate_aware=rebate_aware_snap,
            spread_threshold=threshold_snap,
            high_vol_percentile=high_vol_snap,
        )

        symbol = str(getattr(order, 'symbol', '') or '')
        tick_size = self.get_tick_size(symbol)
        post_only = bool(getattr(order, 'post_only', False))

        bid = _safe_float(market.bid_price)
        ask = _safe_float(market.ask_price)
        spread_abs = ask - bid if (ask > 0.0 and bid > 0.0) else 0.0

        # 预计算，成功后再一次性写入
        new_type = ot
        new_price = original_price
        new_timeout: Optional[int] = None
        did_convert = False
        did_adjust = False
        is_gtc = False

        if ot == OrderType.MARKET:
            if use_limit and not post_only:
                # 零价差：挂任何价格都可能立即成交为 taker，保持 Market
                if spread_abs <= tick_size * 0.5:
                    logger.debug("价差过窄（≤0.5 tick），保持 Market 单以避免 taker 化")
                    self._bump_skip(dry_run)
                    return order

                # Maker 定价：买挂 bid，卖挂 ask
                target_price = bid if direction == "LONG" else ask
                if target_price <= 0.0:
                    logger.error("目标价格为0，无法转换")
                    self._bump_reject(dry_run)
                    return order

                rounded = self._round_price(target_price, direction, tick_size)
                if rounded <= 0.0:
                    logger.error("舍入后价格为0，无法转换")
                    self._bump_reject(dry_run)
                    return order

                # 二次校验：舍入后不得穿越价差
                if direction == "LONG" and ask > 0.0 and rounded >= ask:
                    rounded = self._round_price(rounded - tick_size, direction, tick_size)
                    if rounded <= 0.0 or rounded >= ask:
                        logger.debug("无法找到不穿越价差的 maker 价格，保持 Market")
                        self._bump_skip(dry_run)
                        return order
                elif direction == "SHORT" and bid > 0.0 and rounded <= bid:
                    rounded = self._round_price(rounded + tick_size, direction, tick_size)
                    if rounded <= 0.0 or rounded <= bid:
                        logger.debug("无法找到不穿越价差的 maker 价格，保持 Market")
                        self._bump_skip(dry_run)
                        return order

                new_type = OrderType.LIMIT
                new_price = rounded
                wait_time = self._compute_wait_time(
                    vol_pct,
                    adaptive_wait=adaptive_wait_snap,
                    max_wait=max_wait_snap,
                )
                cur_to = None
                try:
                    if original_timeout is not None:
                        cur_to = int(original_timeout)
                except (TypeError, ValueError, OverflowError):
                    cur_to = None
                new_timeout = self._clamp_timeout(cur_to, wait_time)
                did_convert = True
            else:
                logger.debug("保持 Market 单")
        else:  # LIMIT
            is_gtc = (tif_token == 'GTC' or tif_compact == 'GTC')
            if is_gtc:
                logger.debug("GTC 限价单，不修改超时")
            else:
                wait_time = self._compute_wait_time(
                    vol_pct,
                    adaptive_wait=adaptive_wait_snap,
                    max_wait=max_wait_snap,
                )
                cur_to = None
                try:
                    if original_timeout is not None:
                        cur_to = int(original_timeout)
                except (TypeError, ValueError, OverflowError):
                    cur_to = None
                new_timeout = self._clamp_timeout(cur_to, wait_time)

            if use_limit and not post_only and spread_abs > tick_size * 0.5:
                try:
                    cur_price = (
                        _safe_float(order.price) if order.price is not None else 0.0
                    )
                except Exception:
                    cur_price = 0.0
                if direction == "LONG" and cur_price > 0.0:
                    # 向对手方靠拢但不触及 ask
                    passive_cap = ask - tick_size if ask > tick_size else bid
                    if passive_cap > 0.0 and cur_price < passive_cap:
                        candidate = self._round_price(passive_cap, direction, tick_size)
                        if (
                            candidate > 0.0
                            and candidate < ask
                            and candidate >= cur_price
                        ):
                            new_price = candidate
                            did_adjust = True
                elif direction == "SHORT" and cur_price > 0.0:
                    passive_floor = bid + tick_size if bid > 0.0 else ask
                    if passive_floor > 0.0 and cur_price > passive_floor:
                        candidate = self._round_price(passive_floor, direction, tick_size)
                        if (
                            candidate > 0.0
                            and candidate > bid
                            and candidate <= cur_price
                        ):
                            new_price = candidate
                            did_adjust = True

        # ---- 一次性写入 + 失败回滚 ----
        try:
            if did_convert:
                order.order_type = new_type
                order.price = new_price
                order.timeout_sec = new_timeout
                try:
                    order.original_order_type = OrderType.MARKET
                except Exception:
                    pass
                logger.info(
                    "Market 单转为 Limit(maker): 价格 %s, 超时 %ds",
                    order.price, order.timeout_sec,
                )
            elif ot == OrderType.LIMIT:
                if did_adjust and new_price is not None:
                    order.price = new_price
                if not is_gtc and new_timeout is not None:
                    order.timeout_sec = new_timeout
        except Exception as e:
            logger.error("订单字段写入失败 (%s)，尝试回滚", type(e).__name__)
            try:
                order.order_type = original_type
                order.price = original_price
                order.timeout_sec = original_timeout
            except Exception:
                pass
            self._bump_reject(dry_run)
            return order

        # 价格偏离检查
        try:
            final_price = _safe_float(getattr(order, 'price', None))
            mid = market.mid_price
            if final_price > 0.0 and mid > 0.0:
                deviation = abs(final_price - mid) / mid
                if deviation > _MAX_PRICE_DEVIATION:
                    logger.warning(
                        "订单价格 %.8f 偏离市场中间价 %.8f 超过 %.1f%%，请检查",
                        final_price, mid, _MAX_PRICE_DEVIATION * 100.0,
                    )
        except Exception:
            pass

        # 仅在真实发生 MARKET→LIMIT 转换时累计费用节省
        savings = 0.0
        if did_convert:
            savings = self._estimate_savings(
                order, original_type, qty, maker_fee_snap, taker_fee_snap,
                converted=True,
            )
        self._bump_opt(dry_run, savings=savings)

        self._log_optimization(
            order, original_type, original_price, original_timeout, market
        )

        if not dry_run:
            try:
                if hasattr(order, 'modified_at'):
                    order.modified_at = time.monotonic()
            except Exception:
                pass

        return order

    def update_fees(self, maker_fee: float, taker_fee: float) -> Tuple[float, float]:
        """动态更新费率，返回旧费率"""
        mf = _clamp(
            _safe_float(maker_fee, allow_negative=True),
            -_MAX_ABS_FEE, _MAX_ABS_FEE,
        )
        tf = _clamp(
            _safe_float(taker_fee, allow_negative=True),
            -_MAX_ABS_FEE, _MAX_ABS_FEE,
        )
        with self._lock:
            old_maker = self._maker_fee
            old_taker = self._taker_fee
            self._maker_fee = mf
            self._taker_fee = tf
        logger.info(
            "费率更新: maker=%f, taker=%f (旧: %f, %f)",
            mf, tf, old_maker, old_taker,
        )
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
        """从配置字典创建实例（防御性提取）"""
        if not isinstance(config, dict):
            config = {}
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

    def _should_prefer_limit(
        self,
        market: MarketSnapshot,
        vol_percentile: float,
        *,
        maker_fee: Optional[float] = None,
        taker_fee: Optional[float] = None,
        rebate_aware: Optional[bool] = None,
        spread_threshold: Optional[float] = None,
        high_vol_percentile: Optional[float] = None,
    ) -> bool:
        """综合价差、波动率、手续费结构判断是否应优先使用限价单。"""
        try:
            spread = _safe_float(market.spread_pct, default=float('inf'))
            thr = (
                spread_threshold
                if spread_threshold is not None
                else self._spread_threshold_for_limit
            )
            if spread > thr * 1.001 or math.isinf(spread):
                return False

            hvp = (
                high_vol_percentile
                if high_vol_percentile is not None
                else self._high_volatility_percentile
            )
            if vol_percentile >= hvp:
                return False

            maker = maker_fee if maker_fee is not None else self._maker_fee
            taker = taker_fee if taker_fee is not None else self._taker_fee
            rebate = (
                rebate_aware
                if rebate_aware is not None
                else self._rebate_aware_slippage
            )

            if rebate and maker < 0.0 and taker > maker:
                return True
            if maker < 0.0 < taker:
                return True
            if maker > taker:
                return False
            if maker < 0.0 and taker < 0.0:
                return maker <= taker
            return maker < taker
        except Exception:
            return False

    def _compute_wait_time(
        self,
        volatility_percentile: float,
        *,
        adaptive_wait: Optional[bool] = None,
        max_wait: Optional[int] = None,
    ) -> int:
        """线性映射波动率分位数到建议等待秒数。波动高则等待短。"""
        use_adaptive = (
            adaptive_wait if adaptive_wait is not None else self._adaptive_wait
        )
        max_w = max_wait if max_wait is not None else self._max_wait_for_maker_sec
        if not use_adaptive:
            return int(max_w)
        vol = _clamp(_safe_float(volatility_percentile, default=0.5), 0.0, 1.0)
        wait = max_w - (max_w - MIN_WAIT_SEC) * vol
        return max(MIN_WAIT_SEC, int(round(wait)))

    def _clamp_timeout(self, current: Optional[int], suggested: int) -> int:
        """将超时限制在 [MIN_TIMEOUT_SEC, MAX_TIMEOUT_SEC] 内"""
        try:
            if current is not None:
                timeout = min(int(current), int(suggested))
            else:
                timeout = int(suggested)
        except (TypeError, ValueError, OverflowError):
            timeout = int(suggested)
        timeout = max(timeout, MIN_TIMEOUT_SEC)
        timeout = min(timeout, MAX_TIMEOUT_SEC)
        return int(timeout)

    def _round_price(
        self,
        price: float,
        direction: str,
        tick_size: Optional[float] = None,
    ) -> float:
        """按 tick_size 舍入：买入下舍、卖出上舍，保守处理。"""
        tick = _safe_float(
            tick_size if tick_size is not None else self._tick_size,
            default=self._tick_size,
        )
        if tick <= 0.0 or math.isnan(tick) or math.isinf(tick):
            return max(0.0, _safe_float(price))
        p = _safe_float(price)
        if p <= 0.0:
            return tick
        ratio = p / tick
        if direction == "LONG":
            rounded_ratio = math.floor(ratio + _EPS)
        else:
            rounded_ratio = math.ceil(ratio - _EPS)
        rounded = rounded_ratio * tick
        if rounded <= 0.0 or math.isnan(rounded) or math.isinf(rounded):
            return tick
        return rounded

    def _estimate_savings(
        self,
        order: Order,
        original_type: OrderType,
        qty: float,
        maker_fee: Optional[float] = None,
        taker_fee: Optional[float] = None,
        *,
        converted: bool = False,
    ) -> float:
        """估算本次优化节省的手续费。仅在真实 MARKET→LIMIT 转换时产生非零值。"""
        try:
            if not converted or original_type != OrderType.MARKET:
                return 0.0
            # 确认当前已是 LIMIT
            try:
                if getattr(order, 'order_type', None) != OrderType.LIMIT:
                    return 0.0
            except Exception:
                return 0.0
            price = _safe_float(getattr(order, 'price', None))
            if qty <= 0.0 or price <= 0.0:
                return 0.0
            notional = qty * price
            if (
                notional > _MAX_REASONABLE_NOTIONAL
                or math.isinf(notional)
                or math.isnan(notional)
            ):
                return 0.0
            mf = maker_fee if maker_fee is not None else self._maker_fee
            tf = taker_fee if taker_fee is not None else self._taker_fee
            saving = notional * (tf - mf)
            return max(0.0, _safe_float(saving))
        except Exception:
            return 0.0

    def _log_optimization(
        self,
        order: Order,
        orig_type: OrderType,
        orig_price: Optional[float],
        orig_timeout: Optional[int],
        market: MarketSnapshot,
    ) -> None:
        """记录优化前后的变化（脱敏；日志永不成为故障点）"""
        try:
            symbol = str(getattr(order, 'symbol', 'unknown') or 'unknown')
            qty = _safe_float(getattr(order, 'quantity', 0.0))
            price_str = (
                f"{_safe_float(order.price):.8f}"
                if getattr(order, 'price', None) is not None
                else "None"
            )
            orig_price_str = (
                f"{_safe_float(orig_price):.8f}" if orig_price is not None else "None"
            )
            logger.debug(
                "订单优化: sym=%s, dir=%s, qty=%s, 原类型=%s, 现类型=%s, "
                "原价=%s, 现价=%s, 原超时=%s, 现超时=%s, 价差=%.4f%%, 波动分位=%.2f",
                symbol,
                getattr(order, 'direction', '?'),
                round(qty, 8),
                orig_type,
                getattr(order, 'order_type', '?'),
                orig_price_str,
                price_str,
                orig_timeout,
                getattr(order, 'timeout_sec', None),
                _safe_float(getattr(market, 'spread_pct', 0.0)),
                _safe_float(getattr(market, 'volatility_percentile', 0.5)),
            )
        except Exception:
            pass

    def __repr__(self) -> str:
        return (
            f"FeeOptimizer(threshold={self._spread_threshold_for_limit}, "
            f"max_wait={self._max_wait_for_maker_sec}s, fees_hidden, v={__version__})"
        )
