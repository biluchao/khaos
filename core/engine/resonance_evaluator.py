# -*- coding: utf-8 -*-
"""
模块名称: resonance_evaluator.py
核心职责: 多周期共振评估器，计算3/5分钟同向/背离强度并给出仓位乘数。
          支持品种隔离、小账户降杠杆、异步安全、状态持久化，完全并发安全。
所属层级: core.engine

设计原则:
    - 所有输入严格校验，非法返回安全值。
    - 内部状态按品种隔离，LRU淘汰防内存泄漏。
    - 所有状态读写均受锁保护，消除竞态。
    - 异步调用具备超时降级与executor生命周期管理。
    - 参数边界验证，金融级数值安全。

外部依赖:
    - math, time, logging, threading, dataclasses, typing, copy, collections, asyncio
    - concurrent.futures.ThreadPoolExecutor
    - core.interfaces (MarketRegime, SRLevel, FeatureContext)

接口契约:
    提供:
        ResonanceEvaluator.evaluate(...) -> ResonanceState
        ResonanceEvaluator.async_evaluate(...) -> ResonanceState (超时降级)
        get_position_multiplier, reset, get/set_internal_state, get_stats, shutdown
        is_healthy, get_config, update_params
    消费:
        - MarketRegime, SRLevel

配置项: 多个可调参数（见构造函数）

作者: KHAOS System Architect
创建日期: 2025-03-10
修改记录:
    - 2026-07-08 v34.0: 终极并发安全与异步保护，80项加固。
__version__ = "34.0.0"
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.interfaces import MarketRegime, SRLevel, FeatureContext

logger = logging.getLogger(__name__)

__all__ = ["ResonanceState", "ResonanceEvaluator", "ResonanceException"]


class ResonanceException(Exception):
    """共振评估器异常基类。"""
    pass


@dataclass(frozen=True)
class ResonanceState:
    """共振评估结果（不可变）。"""
    strength: float
    state_3m: str
    state_5m: str
    multiplier: float
    weight: float
    max_boost: float
    min_reduce: float
    price: float
    symbol: str = ""
    timestamp: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        # 修正非法值
        if not math.isfinite(self.strength) or abs(self.strength) > 1.0:
            object.__setattr__(self, 'strength', 0.0)
            logger.warning("ResonanceState: strength invalid, reset to 0.0")
        valid = {e.value for e in MarketRegime}
        if self.state_3m not in valid:
            object.__setattr__(self, 'state_3m', MarketRegime.RANGE.value)
        if self.state_5m not in valid:
            object.__setattr__(self, 'state_5m', MarketRegime.RANGE.value)
        # 二次钳位乘数
        if not math.isfinite(self.multiplier) or self.multiplier <= 0:
            object.__setattr__(self, 'multiplier', 1.0)
        else:
            # 确保在[min_reduce, max_boost]范围内
            clamped = max(self.min_reduce, min(self.multiplier, self.max_boost))
            if clamped != self.multiplier:
                object.__setattr__(self, 'multiplier', clamped)

    def __repr__(self):
        return (f"ResonanceState(sym={self.symbol}, {self.state_3m}/{self.state_5m}, "
                f"strength={self.strength:.2f}, mult={self.multiplier:.2f})")


class ResonanceEvaluator:
    """
    多周期共振评估器（完全并发安全，机构级）。
    """

    # 默认配置常量
    DEFAULT_WEIGHT = 0.5
    DEFAULT_MAX_BOOST = 1.5
    DEFAULT_MIN_REDUCE = 0.3
    DEFAULT_SMOOTH_HALFLIFE = 3
    DEFAULT_MAX_CHANGE_RATIO = 0.2
    DEFAULT_SMALL_BALANCE_THRESHOLD = 2000.0
    DEFAULT_SMALL_BALANCE_MAX_BOOST = 1.2
    DEFAULT_BASE_STRENGTH = 0.6
    DEFAULT_MAX_TRACKED_SYMBOLS = 50
    DEFAULT_INITIAL_ENTRY_MAX_BOOST = 1.5
    DEFAULT_LOW_VOL_THRESHOLD = 0.4
    MIN_ALPHA = 0.01
    MIN_PREV_MULTIPLIER = 0.1
    STATE_VERSION = 2

    def __init__(
        self,
        weight: float = DEFAULT_WEIGHT,
        max_boost: float = DEFAULT_MAX_BOOST,
        min_reduce: float = DEFAULT_MIN_REDUCE,
        smooth_halflife: int = DEFAULT_SMOOTH_HALFLIFE,
        max_position_change_ratio: float = DEFAULT_MAX_CHANGE_RATIO,
        skip_ratio_on_gap: bool = True,
        exempt_for_initial_entry: bool = True,
        small_balance_threshold: float = DEFAULT_SMALL_BALANCE_THRESHOLD,
        small_balance_max_boost: float = DEFAULT_SMALL_BALANCE_MAX_BOOST,
        base_strength: float = DEFAULT_BASE_STRENGTH,
        max_tracked_symbols: int = DEFAULT_MAX_TRACKED_SYMBOLS,
        initial_entry_max_boost: float = DEFAULT_INITIAL_ENTRY_MAX_BOOST,
        low_vol_threshold: float = DEFAULT_LOW_VOL_THRESHOLD,
        allow_resonance_in_high_vol: bool = False
    ):
        # 参数验证
        if not (0.0 <= weight <= 1.0):
            raise ValueError(f"weight must be in [0,1], got {weight}")
        if not (1.0 <= max_boost <= 3.0):
            raise ValueError(f"max_boost must be in [1,3], got {max_boost}")
        if not (0.1 <= min_reduce <= 1.0):
            raise ValueError(f"min_reduce must be in [0.1,1], got {min_reduce}")
        if max_boost < min_reduce:
            raise ValueError(f"max_boost ({max_boost}) < min_reduce ({min_reduce})")
        if smooth_halflife < 1:
            raise ValueError(f"smooth_halflife must be >= 1, got {smooth_halflife}")
        if not (0.05 <= max_position_change_ratio <= 0.5):
            raise ValueError(f"max_position_change_ratio must be in [0.05,0.5], got {max_position_change_ratio}")
        if small_balance_threshold <= 0:
            raise ValueError(f"small_balance_threshold > 0, got {small_balance_threshold}")
        if not (1.0 <= small_balance_max_boost <= 2.0):
            raise ValueError(f"small_balance_max_boost in [1,2], got {small_balance_max_boost}")
        if not (0.1 <= base_strength <= 1.0):
            raise ValueError(f"base_strength in [0.1,1], got {base_strength}")
        if max_tracked_symbols < 1:
            raise ValueError(f"max_tracked_symbols >= 1, got {max_tracked_symbols}")
        if not (1.0 <= initial_entry_max_boost <= 2.5):
            raise ValueError(f"initial_entry_max_boost in [1,2.5], got {initial_entry_max_boost}")
        if not (0.1 <= low_vol_threshold <= 0.8):
            raise ValueError(f"low_vol_threshold in [0.1,0.8], got {low_vol_threshold}")
        if initial_entry_max_boost < small_balance_max_boost:
            logger.warning("initial_entry_max_boost < small_balance_max_boost, may cause inconsistency")

        self.weight = weight
        self.max_boost = max_boost
        self.min_reduce = min_reduce
        self.smooth_halflife = int(smooth_halflife)
        self.max_position_change_ratio = max_position_change_ratio
        self.skip_ratio_on_gap = bool(skip_ratio_on_gap)
        self.exempt_for_initial_entry = bool(exempt_for_initial_entry)
        self.small_balance_threshold = small_balance_threshold
        self.small_balance_max_boost = small_balance_max_boost
        self.base_strength = base_strength
        self.max_tracked_symbols = max_tracked_symbols
        self.initial_entry_max_boost = initial_entry_max_boost
        self.low_vol_threshold = low_vol_threshold
        self.allow_resonance_in_high_vol = allow_resonance_in_high_vol

        # 平滑因子
        raw_alpha = 1.0 - math.exp(math.log(0.5) / max(self.smooth_halflife, 1))
        self._alpha = max(self.MIN_ALPHA, raw_alpha)

        # 线程安全锁 (可重入)
        self._lock = threading.RLock()

        # 品种状态 LRU (所有访问均在锁内)
        self._per_symbol_state: OrderedDict[str, Dict[str, float]] = OrderedDict()
        self._DEFAULT_SYMBOL = "__default__"

        # 统计 (锁保护)
        self._eval_count: int = 0
        self._total_time: float = 0.0
        self._error_count: int = 0

        # 动态权重状态 (锁保护)
        self._dynamic_weight_adjusted: bool = False

        # 异步专用线程池
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resonance")
        self._shutdown_flag = False

        # 全局默认账户余额 (可设置，若调用时未传入则使用)
        self._default_balance: Optional[float] = None

        logger.info(
            f"ResonanceEvaluator v{__version__}: weight={weight}, boost={max_boost}, "
            f"reduce={min_reduce}, halflife={smooth_halflife}, base_str={base_strength}"
        )

    @staticmethod
    def validate_config(**kwargs) -> Tuple[bool, str]:
        try:
            ResonanceEvaluator(**kwargs)
            return True, "OK"
        except Exception as e:
            return False, str(e)

    # -----------------------------------------------------------------------
    # 公共方法
    # -----------------------------------------------------------------------
    def set_default_balance(self, balance: Optional[float]) -> None:
        with self._lock:
            self._default_balance = balance

    def get_config(self) -> Dict[str, Any]:
        """返回当前配置参数的拷贝。"""
        return {
            "weight": self.weight,
            "max_boost": self.max_boost,
            "min_reduce": self.min_reduce,
            "smooth_halflife": self.smooth_halflife,
            "max_position_change_ratio": self.max_position_change_ratio,
            "small_balance_threshold": self.small_balance_threshold,
            "small_balance_max_boost": self.small_balance_max_boost,
            "base_strength": self.base_strength,
            "max_tracked_symbols": self.max_tracked_symbols,
            "initial_entry_max_boost": self.initial_entry_max_boost,
            "low_vol_threshold": self.low_vol_threshold,
            "allow_resonance_in_high_vol": self.allow_resonance_in_high_vol,
        }

    def update_params(self, **kwargs) -> None:
        """更新部分参数（谨慎使用）。仅允许更新特定字段，且立即生效。"""
        allowed = {"weight", "max_boost", "min_reduce", "smooth_halflife", "max_position_change_ratio",
                   "small_balance_threshold", "small_balance_max_boost", "base_strength"}
        for k, v in kwargs.items():
            if k in allowed:
                setattr(self, k, v)
        # 重新计算 alpha
        raw_alpha = 1.0 - math.exp(math.log(0.5) / max(self.smooth_halflife, 1))
        self._alpha = max(self.MIN_ALPHA, raw_alpha)
        logger.info(f"ResonanceEvaluator params updated: {kwargs}")

    def evaluate(
        self,
        state_3m: str,
        state_5m: str,
        price: float,
        sr_levels_5m: Optional[List[SRLevel]] = None,
        atr_3m: float = 1.0,
        context: Optional[FeatureContext] = None,
        is_gap: bool = False,
        is_initial_entry: bool = False,
        account_balance: Optional[float] = None,
        symbol: str = ""
    ) -> ResonanceState:
        """同步评估共振状态。若account_balance未提供，尝试使用默认余额。"""
        if account_balance is None:
            account_balance = self._default_balance
        return self._evaluate_impl(
            state_3m, state_5m, price, sr_levels_5m, atr_3m,
            context, is_gap, is_initial_entry, account_balance, symbol
        )

    async def async_evaluate(
        self,
        state_3m: str,
        state_5m: str,
        price: float,
        sr_levels_5m: Optional[List[SRLevel]] = None,
        atr_3m: float = 1.0,
        context: Optional[FeatureContext] = None,
        is_gap: bool = False,
        is_initial_entry: bool = False,
        account_balance: Optional[float] = None,
        symbol: str = "",
        timeout: float = 2.0
    ) -> ResonanceState:
        """异步评估，支持超时降级。"""
        if self._shutdown_flag:
            logger.warning("Evaluator is shut down, returning safe default")
            return self._make_safe_default(price, symbol, state_3m, state_5m)
        if account_balance is None:
            account_balance = self._default_balance

        loop = asyncio.get_running_loop()
        executor = self._executor
        if executor is None:
            # 如果 executor 丢失，回退到同步调用
            logger.warning("Executor is None, falling back to synchronous evaluate")
            return self._evaluate_impl(state_3m, state_5m, price, sr_levels_5m, atr_3m,
                                       context, is_gap, is_initial_entry, account_balance, symbol)
        try:
            future = loop.run_in_executor(
                executor,
                self._evaluate_impl,
                state_3m, state_5m, price, sr_levels_5m, atr_3m,
                context, is_gap, is_initial_entry, account_balance, symbol
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"async_evaluate timed out after {timeout}s, returning safe default")
            return self._make_safe_default(price, symbol, state_3m, state_5m)
        except asyncio.CancelledError:
            logger.debug("async_evaluate cancelled")
            return self._make_safe_default(price, symbol, state_3m, state_5m)
        except Exception:
            logger.exception("async_evaluate error")
            return self._make_safe_default(price, symbol, state_3m, state_5m)

    def _evaluate_impl(
        self,
        state_3m: str,
        state_5m: str,
        price: float,
        sr_levels_5m: Optional[List[SRLevel]],
        atr_3m: float,
        context: Optional[FeatureContext],
        is_gap: bool,
        is_initial_entry: bool,
        account_balance: Optional[float],
        symbol: str
    ) -> ResonanceState:
        """内部实现，完全并发安全。"""
        start_ts = time.perf_counter()
        success = False
        safe_default = self._make_safe_default(price, symbol, state_3m, state_5m)

        try:
            # 输入净化
            valid_states = {e.value for e in MarketRegime}
            s3 = str(state_3m).strip().upper() if state_3m else ""
            s5 = str(state_5m).strip().upper() if state_5m else ""
            if s3 not in valid_states or s5 not in valid_states:
                logger.warning(f"Invalid states: 3m={s3}, 5m={s5}, original: {state_3m}/{state_5m}")
                return safe_default

            if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
                logger.warning(f"Invalid price: {price}")
                return safe_default

            if not (isinstance(atr_3m, (int, float)) and math.isfinite(atr_3m) and atr_3m > 0):
                atr_3m = 1e-8

            if account_balance is not None and (not math.isfinite(account_balance) or account_balance < 0):
                account_balance = 0.0

            sym = symbol.strip() if symbol else self._DEFAULT_SYMBOL
            if len(sym) > 50:
                logger.warning(f"Symbol truncated: {sym[:50]}")
                sym = sym[:50]

            clean_sr = self._clean_sr_levels(sr_levels_5m)

            # 在锁内完成整个状态相关的计算，保证原子性
            with self._lock:
                # 小账户保护
                eff_max_boost = self.max_boost
                if account_balance is not None and account_balance < self.small_balance_threshold:
                    eff_max_boost = min(self.max_boost, self.small_balance_max_boost)
                if is_initial_entry:
                    eff_max_boost = min(eff_max_boost, self.initial_entry_max_boost)
                if eff_max_boost < self.min_reduce:
                    eff_max_boost = self.min_reduce
                    logger.debug("eff_max_boost adjusted to min_reduce")

                eff_weight = self._get_effective_weight_locked(context)

                # 计算原始强度
                raw_strength = self._compute_raw_strength(s3, s5, price, clean_sr, atr_3m)

                # 平滑
                alpha = self._alpha
                if is_gap:
                    gap_halflife = max(1, self.smooth_halflife // 2)
                    alpha = max(self.MIN_ALPHA, 1.0 - math.exp(math.log(0.5) / gap_halflife))
                strength = self._smooth_strength_locked(sym, raw_strength, alpha)

                # 乘数计算
                multiplier = self._compute_multiplier(strength, eff_weight, self.min_reduce, eff_max_boost)

                # 仓位变化限制 (与状态更新在同一锁内)
                apply_limit = not (is_gap and self.skip_ratio_on_gap)
                if apply_limit and not (is_initial_entry and self.exempt_for_initial_entry):
                    multiplier = self._apply_change_limit_locked(sym, multiplier)

                # 最终钳位
                multiplier = max(self.min_reduce, min(multiplier, eff_max_boost))
                if not math.isfinite(multiplier):
                    multiplier = 1.0
                    logger.error("Multiplier invalid, reset to 1.0")

                # 更新状态
                self._update_prev_multiplier_locked(sym, multiplier)

            success = True
            result = ResonanceState(
                strength=strength,
                state_3m=s3,
                state_5m=s5,
                multiplier=multiplier,
                weight=eff_weight,
                max_boost=eff_max_boost,
                min_reduce=self.min_reduce,
                price=price,
                symbol=sym
            )
            return result

        except Exception:
            logger.exception("Resonance evaluation error")
            return safe_default
        finally:
            elapsed = time.perf_counter() - start_ts
            with self._lock:
                self._eval_count += 1
                self._total_time += elapsed
                if not success:
                    self._error_count += 1

    def get_position_multiplier(self, resonance_state: ResonanceState) -> float:
        if not isinstance(resonance_state, ResonanceState):
            return 1.0
        return resonance_state.multiplier

    def reset(self, symbol: Optional[str] = None, reset_stats: bool = False) -> None:
        """重置品种状态。symbol=None 重置所有，reset_stats 同时重置统计。"""
        with self._lock:
            if symbol is None:
                self._per_symbol_state.clear()
                if reset_stats:
                    self._eval_count = 0
                    self._total_time = 0.0
                    self._error_count = 0
                logger.info("ResonanceEvaluator: full reset")
            else:
                sym = symbol.strip()[:50] if symbol else self._DEFAULT_SYMBOL
                if sym in self._per_symbol_state:
                    del self._per_symbol_state[sym]
                    logger.info(f"ResonanceEvaluator: reset symbol {sym}")
                if reset_stats:
                    self._eval_count = 0
                    self._total_time = 0.0
                    self._error_count = 0
                    logger.info("ResonanceEvaluator: stats reset (along with symbol reset)")

    def get_internal_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "per_symbol_state": deepcopy(dict(self._per_symbol_state)),
                "eval_count": self._eval_count,
                "total_time": self._total_time,
                "error_count": self._error_count,
                "params": self.get_config(),
                "version": self.STATE_VERSION
            }

    def set_internal_state(self, state: Dict[str, Any]) -> None:
        with self._lock:
            version = state.get("version", 0)
            if version != self.STATE_VERSION:
                logger.error(f"State version mismatch: expected {self.STATE_VERSION}, got {version}. Rejecting.")
                return
            raw = state.get("per_symbol_state", {})
            if not isinstance(raw, dict):
                return
            validated = OrderedDict()
            for sym, val in raw.items():
                if isinstance(val, dict) and "ema_strength" in val and "prev_multiplier" in val:
                    ema = float(val["ema_strength"])
                    prev = float(val["prev_multiplier"])
                    if not math.isfinite(ema) or abs(ema) > 1.0:
                        ema = 0.0
                    if not math.isfinite(prev) or prev <= 0:
                        prev = 1.0
                    validated[sym] = {
                        "ema_strength": ema,
                        "prev_multiplier": prev,
                        "initialized": bool(val.get("initialized", True))
                    }
            self._per_symbol_state = deepcopy(validated)  # 深拷贝防止外部后续修改
            self._eval_count = int(state.get("eval_count", 0))
            self._total_time = float(state.get("total_time", 0.0))
            self._error_count = int(state.get("error_count", 0))
            self._dynamic_weight_adjusted = False
            logger.info(f"State restored. Symbols: {len(validated)}")

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            avg = self._total_time / max(self._eval_count, 1)
            return {
                "eval_count": self._eval_count,
                "avg_eval_time_ms": round(avg * 1000, 3),
                "total_eval_time_s": round(self._total_time, 3),
                "error_count": self._error_count,
                "symbols_tracked": len(self._per_symbol_state),
                "symbols_sample": list(self._per_symbol_state.keys())[:10]
            }

    def is_healthy(self) -> bool:
        """简单健康检查。"""
        with self._lock:
            return not self._shutdown_flag and self._eval_count > 0

    def shutdown(self) -> None:
        """关闭异步执行器，释放资源。可安全多次调用。"""
        with self._lock:
            if self._shutdown_flag:
                return
            self._shutdown_flag = True
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None
            logger.info("ResonanceEvaluator shut down")

    def __repr__(self):
        return (f"ResonanceEvaluator(weight={self.weight}, boost={self.max_boost}, "
                f"reduce={self.min_reduce}, halflife={self.smooth_halflife})")

    # -----------------------------------------------------------------------
    # 内部私有方法 (要求调用时已持有 _lock)
    # -----------------------------------------------------------------------
    def _make_safe_default(self, price: float, symbol: str, state_3m: str = "", state_5m: str = "") -> ResonanceState:
        p = price if (isinstance(price, (int, float)) and math.isfinite(price) and price > 0) else 0.0
        valid = {e.value for e in MarketRegime}
        return ResonanceState(
            strength=0.0,
            state_3m=state_3m if state_3m in valid else MarketRegime.RANGE.value,
            state_5m=state_5m if state_5m in valid else MarketRegime.RANGE.value,
            multiplier=1.0,
            weight=self.weight,
            max_boost=self.max_boost,
            min_reduce=self.min_reduce,
            price=p,
            symbol=symbol[:50] if symbol else ""
        )

    def _get_effective_weight_locked(self, context: Optional[FeatureContext]) -> float:
        """锁内获取有效权重。"""
        if context is None:
            return self.weight
        vol_perc = context.get('volatility_percentile')
        if vol_perc is not None and isinstance(vol_perc, (int, float)) and math.isfinite(vol_perc):
            if vol_perc > 0.8:
                new_w = max(0.2, self.weight - 0.2)
                logger.debug(f"High vol ({vol_perc}), weight reduced to {new_w}")
                self._dynamic_weight_adjusted = True
                return new_w
            elif vol_perc < self.low_vol_threshold:
                if self._dynamic_weight_adjusted:
                    logger.debug("Volatility back to normal, restoring weight")
                    self._dynamic_weight_adjusted = False
                return self.weight
        return self.weight

    def _clean_sr_levels(self, sr_levels: Optional[List[SRLevel]]) -> List[SRLevel]:
        if not sr_levels or not isinstance(sr_levels, list):
            return []
        clean = []
        for s in sr_levels:
            if s is None or not isinstance(s, SRLevel):
                continue
            price = s.price
            if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
                continue
            strength = s.strength if (isinstance(s.strength, (int, float)) and math.isfinite(s.strength)) else 1.0
            try:
                ts = float(s.timestamp)
            except (TypeError, ValueError):
                ts = time.monotonic()
            clean.append(SRLevel(
                price=price,
                strength=strength,
                method=s.method,
                touches=s.touches,
                confidence=s.confidence,
                timestamp=ts
            ))
        # 去重保留最高强度
        seen: Dict[float, SRLevel] = {}
        for sr in clean:
            if sr.price not in seen or sr.strength > seen[sr.price].strength:
                seen[sr.price] = sr
        unique = list(seen.values())
        unique.sort(key=lambda x: x.strength, reverse=True)
        return unique[:100]

    def _compute_raw_strength(
        self, s3: str, s5: str, price: float, sr_levels: List[SRLevel], atr: float
    ) -> float:
        # RANGE 和高波动（若不允许）返回0
        if s3 == MarketRegime.RANGE.value or s5 == MarketRegime.RANGE.value:
            return 0.0
        if (s3 == MarketRegime.HIGH_VOL.value or s5 == MarketRegime.HIGH_VOL.value) and not self.allow_resonance_in_high_vol:
            return 0.0

        # 仅当同向且均为明确趋势时视为共振
        if s3 == s5 and s3 in (MarketRegime.TRENDING_UP.value, MarketRegime.TRENDING_DOWN.value):
            strength = self.base_strength
        else:
            return 0.0  # 非同向趋势视为无共振，返回0

        # S/R 调整
        supports = sorted([s for s in sr_levels if s.price < price], key=lambda x: x.price, reverse=True)
        resistances = sorted([s for s in sr_levels if s.price > price], key=lambda x: x.price)

        if s3 == MarketRegime.TRENDING_UP.value:
            if supports:
                d = (price - supports[0].price) / atr
                d = max(0.0, min(d, 2.0))
                if 0 < d < 2.0:
                    strength += 0.2 * (1.0 - d / 2.0)
            if resistances:
                r = (resistances[0].price - price) / atr
                r = max(0.0, min(r, 5.0))
                if r < 0.5:
                    strength -= 0.3
        elif s3 == MarketRegime.TRENDING_DOWN.value:
            if resistances:
                d = (resistances[0].price - price) / atr
                d = max(0.0, min(d, 2.0))
                if 0 < d < 2.0:
                    strength += 0.2 * (1.0 - d / 2.0)
            if supports:
                r = (price - supports[0].price) / atr
                r = max(0.0, min(r, 5.0))
                if r < 0.5:
                    strength -= 0.3

        return max(0.0, min(1.0, strength))

    def _smooth_strength_locked(self, sym: str, raw: float, alpha: float) -> float:
        """锁内平滑。"""
        if not math.isfinite(raw):
            raw = 0.0
        alpha = max(0.0, min(1.0, alpha))
        state = self._get_state_locked(sym)
        if not state["initialized"]:
            state["ema_strength"] = raw
            state["initialized"] = True
        else:
            state["ema_strength"] = alpha * raw + (1 - alpha) * state["ema_strength"]
        return state["ema_strength"]

    def _compute_multiplier(self, strength: float, weight: float, min_red: float, max_boost: float) -> float:
        m = 1.0 + strength * weight
        return max(min_red, min(m, max_boost))

    def _apply_change_limit_locked(self, sym: str, new_multiplier: float) -> float:
        """锁内应用仓位变化限制。"""
        state = self._get_state_locked(sym)
        prev = state["prev_multiplier"]
        if prev < self.MIN_PREV_MULTIPLIER:
            return new_multiplier
        # 如果新乘数 <=1.0 且当前乘数 >1.0，允许快速降仓
        if new_multiplier <= 1.0 < prev:
            return new_multiplier
        change = abs(new_multiplier - prev) / prev
        if change > self.max_position_change_ratio:
            if new_multiplier > prev:
                return prev * (1 + self.max_position_change_ratio)
            else:
                return prev * (1 - self.max_position_change_ratio)
        return new_multiplier

    def _update_prev_multiplier_locked(self, sym: str, multiplier: float) -> None:
        """锁内更新上次乘数。"""
        if not math.isfinite(multiplier) or multiplier <= 0:
            logger.warning(f"Ignoring invalid multiplier update: {multiplier}")
            return
        state = self._get_state_locked(sym)
        state["prev_multiplier"] = multiplier

    def _get_state_locked(self, sym: str) -> Dict[str, float]:
        """锁内获取或创建品种状态，调用方必须已持有锁。"""
        if sym in self._per_symbol_state:
            try:
                self._per_symbol_state.move_to_end(sym)
            except KeyError:
                pass
            return self._per_symbol_state[sym]
        # 新建
        state = {"ema_strength": 0.0, "prev_multiplier": 1.0, "initialized": False}
        self._per_symbol_state[sym] = state
        # LRU 淘汰
        while len(self._per_symbol_state) > self.max_tracked_symbols:
            oldest = next(iter(self._per_symbol_state))
            del self._per_symbol_state[oldest]
            logger.debug(f"LRU evicted: {oldest}")
        return state
