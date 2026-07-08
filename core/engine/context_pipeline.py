# -*- coding: utf-8 -*-
"""
模块名称: context_pipeline.py
核心职责: 上下文构建管道，为每根K线组装完整的决策上下文。
所属层级: core.engine

外部依赖:
    - asyncio, logging, time, math, typing, collections, re, copy, dataclasses
    - core.models (Kline)
    - core.engine.kline_buffer (MultiTimeframeKlineBuffer)
    - core.engine.hierarchy_guard (HierarchyGuard)
    - core.engine.sr_pipeline (SRMappingPipeline)
    - core.engine.market_regime_monitor (MarketRegimeMonitor)
    - core.indicators.kma (KalmanTrendline)
    - core.indicators.hmm_state_detector (HMMStateDetector)

接口契约:
    提供:
        - ContextPipeline: build(symbol, kline) -> Dict[str, Any]
        - get_cached_context, clear_cache, reset, get_metrics, get_status, shutdown
    消费:
        - MultiTimeframeKlineBuffer, HierarchyGuard, SRMappingPipeline, MarketRegimeMonitor, KalmanTrendline, HMMStateDetector

作者: KHAOS System Architect
创建日期: 2025-03-15
修改记录:
    - 2026-07-08 v44.0: 最终机构级版本，包含熔断自愈、LRU缓存、完整降级与可观测性。
__version__ = "44.0.0"
__all__ = ["ContextPipeline"]
"""

import asyncio
import logging
import time
import math
import re
import copy
from typing import Dict, List, Optional, Any, Deque, Tuple
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from core.models import Kline
from core.engine.kline_buffer import MultiTimeframeKlineBuffer
from core.engine.hierarchy_guard import HierarchyGuard
from core.engine.sr_pipeline import SRMappingPipeline
from core.engine.market_regime_monitor import MarketRegimeMonitor
from core.indicators.kma import KalmanTrendline
from core.indicators.hmm_state_detector import HMMStateDetector

logger = logging.getLogger(__name__)

# 默认常量
DEFAULT_MAX_KMA_CACHE_ENTRIES = 2000
DEFAULT_MAX_HMM_CACHE_ENTRIES = 2000
DEFAULT_MAX_ATR_CACHE_ENTRIES = 500
DEFAULT_MAX_SR_CACHE_ENTRIES = 200
DEFAULT_MAX_CONTEXT_CACHE_PER_SYMBOL = 200
DEFAULT_GLOBAL_CONTEXT_CACHE_LIMIT = 2000
DEFAULT_MAX_LONG_TERM_VOL_CACHE_ENTRIES = 100
DEFAULT_MAX_ERROR_COUNTERS = 100
DEFAULT_MAX_VOLUME_SPIKE_COUNTERS = 50
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_RETRY_SEC = 600
DEFAULT_CIRCUIT_BREAKER_SUCCESS_RESET = 3
MAX_FUTURE_KLINES_MS = 5000

REQUIRED_CONTEXT_KEYS = [
    "symbol", "primary_interval", "latest_kline_ohlc", "last_price",
    "kma", "kma_slope", "kma_bandwidth",
    "hmm_state_3m", "hmm_probabilities_3m",
    "atr_3m", "volume_ma20", "volatility_percentile",
    "regime", "sr_levels", "data_quality", "degradation_reasons",
    "degraded_components"
]


@dataclass
class CircuitState:
    """熔断状态跟踪"""
    failure_count: int = 0
    last_failure_time: float = 0.0
    circuit_open_time: float = 0.0
    success_count: int = 0


class ContextPipeline:
    """
    上下文构建管道，为每根K线组装完整的市场上下文。
    具备自适应熔断、智能缓存、完整降级链路和自愈能力。
    """

    __slots__ = [
        'kline_buffer', 'hierarchy_guard', 'sr_pipeline', 'regime_monitor',
        'kma_computer', 'hmm_detector', 'primary_interval', 'secondary_intervals',
        'sr_mapping_enabled', 'context_cache_size', 'global_context_cache_limit',
        'atr_period', 'volume_ma_period', 'percentile_lookback', 'volume_ema_alpha',
        'kline_fetch_timeout', 'kma_compute_timeout', 'hmm_predict_timeout',
        'sr_compute_timeout', 'regime_timeout', 'max_fetch_bars', 'tick_size',
        'circuit_breaker_threshold', 'circuit_breaker_retry_sec', 'circuit_success_reset',
        'strict_time_check', 'max_future_klines_ms',
        '_context_caches', '_kma_cache', '_kma_max_entries', '_hmm_cache', '_hmm_max_entries',
        '_atr_cache', '_atr_max_entries', '_sr_cache', '_sr_max_entries', '_sr_ttl_sec',
        '_atr_history', '_last_atr_open_time', '_long_term_avg_vol', '_long_term_avg_vol_max',
        '_volume_spike_counters', '_volume_spike_max',
        '_error_counters', '_max_error_counters',
        '_kma_circuit', '_hmm_circuit',
        '_build_count', '_total_build_time_ms', '_max_build_time_ms',
        '_cache_hit_kma', '_cache_hit_hmm', '_cache_miss_kma', '_cache_miss_hmm',
        '_sr_cache_hit', '_last_cleanup_time', '_last_status', '_context_cache_total',
    ]

    def __init__(
        self,
        kline_buffer: MultiTimeframeKlineBuffer,
        hierarchy_guard: HierarchyGuard,
        sr_pipeline: Optional[SRMappingPipeline],
        regime_monitor: MarketRegimeMonitor,
        kma_computer: KalmanTrendline,
        hmm_detector: HMMStateDetector,
        primary_interval: str = "3m",
        secondary_intervals: Optional[List[str]] = None,
        sr_mapping_enabled: bool = True,
        context_cache_size: int = DEFAULT_MAX_CONTEXT_CACHE_PER_SYMBOL,
        global_context_cache_limit: int = DEFAULT_GLOBAL_CONTEXT_CACHE_LIMIT,
        atr_period: int = 14,
        volume_ma_period: int = 20,
        percentile_lookback: int = 200,
        max_percentile_lookback: int = 5000,
        volume_ema_alpha: float = 0.1,
        kline_fetch_timeout: float = 3.0,
        kma_compute_timeout: float = 1.0,
        hmm_predict_timeout: float = 1.0,
        sr_compute_timeout: float = 5.0,
        regime_timeout: float = 2.0,
        max_fetch_bars: int = 500,
        tick_size: Optional[float] = None,
        circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
        circuit_breaker_retry_sec: float = DEFAULT_CIRCUIT_BREAKER_RETRY_SEC,
        circuit_success_reset: int = DEFAULT_CIRCUIT_BREAKER_SUCCESS_RESET,
        strict_time_check: bool = True,
    ):
        if kline_buffer is None:
            raise ValueError("kline_buffer is required")
        if hierarchy_guard is None:
            raise ValueError("hierarchy_guard is required")
        if regime_monitor is None:
            raise ValueError("regime_monitor is required")
        if kma_computer is None:
            raise ValueError("kma_computer is required")
        if hmm_detector is None:
            raise ValueError("hmm_detector is required")
        if sr_mapping_enabled and sr_pipeline is None:
            raise ValueError("sr_pipeline is required when sr_mapping_enabled=True")

        self.kline_buffer = kline_buffer
        self.hierarchy_guard = hierarchy_guard
        self.sr_pipeline = sr_pipeline
        self.regime_monitor = regime_monitor
        self.kma_computer = kma_computer
        self.hmm_detector = hmm_detector
        self.primary_interval = self._validate_interval(primary_interval)
        # 去重并排除主周期
        raw_secondary = secondary_intervals or ["5m", "15m"]
        self.secondary_intervals = list(set(tf for tf in raw_secondary if tf != self.primary_interval))
        for tf in self.secondary_intervals:
            self._validate_interval(tf)
        self.sr_mapping_enabled = sr_mapping_enabled
        self.context_cache_size = context_cache_size
        self.global_context_cache_limit = global_context_cache_limit
        self.atr_period = atr_period
        self.volume_ma_period = volume_ma_period
        self.percentile_lookback = min(percentile_lookback, max_percentile_lookback)
        if self.percentile_lookback < 10:
            raise ValueError("percentile_lookback must be at least 10")
        self.volume_ema_alpha = max(0.01, min(1.0, volume_ema_alpha))
        self.kline_fetch_timeout = kline_fetch_timeout
        self.kma_compute_timeout = kma_compute_timeout
        self.hmm_predict_timeout = hmm_predict_timeout
        self.sr_compute_timeout = sr_compute_timeout
        self.regime_timeout = regime_timeout
        self.max_fetch_bars = max_fetch_bars
        self.tick_size = tick_size
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_retry_sec = circuit_breaker_retry_sec
        self.circuit_success_reset = circuit_success_reset
        self.strict_time_check = strict_time_check
        self.max_future_klines_ms = MAX_FUTURE_KLINES_MS

        # 缓存初始化
        self._context_caches: Dict[str, OrderedDict] = {}
        self._context_cache_total = 0

        self._kma_cache: OrderedDict = OrderedDict()
        self._kma_max_entries = DEFAULT_MAX_KMA_CACHE_ENTRIES
        self._hmm_cache: OrderedDict = OrderedDict()
        self._hmm_max_entries = DEFAULT_MAX_HMM_CACHE_ENTRIES

        self._atr_cache: OrderedDict = OrderedDict()
        self._atr_max_entries = DEFAULT_MAX_ATR_CACHE_ENTRIES

        self._sr_cache: OrderedDict = OrderedDict()
        self._sr_max_entries = DEFAULT_MAX_SR_CACHE_ENTRIES
        self._sr_ttl_sec = 30.0

        self._atr_history: Dict[Tuple[str, str], Deque[float]] = {}
        self._last_atr_open_time: Dict[Tuple[str, str], int] = {}

        self._long_term_avg_vol: OrderedDict = OrderedDict()
        self._long_term_avg_vol_max = DEFAULT_MAX_LONG_TERM_VOL_CACHE_ENTRIES

        self._volume_spike_counters: Dict[Tuple[str, str], int] = {}
        self._volume_spike_max = DEFAULT_MAX_VOLUME_SPIKE_COUNTERS

        self._error_counters: Dict[str, int] = {}
        self._max_error_counters = DEFAULT_MAX_ERROR_COUNTERS

        # 熔断状态
        self._kma_circuit: Dict[Tuple[str, str], CircuitState] = {}
        self._hmm_circuit: Dict[Tuple[str, str], CircuitState] = {}

        # 统计
        self._build_count = 0
        self._total_build_time_ms = 0.0
        self._max_build_time_ms = 0.0
        self._cache_hit_kma = 0
        self._cache_hit_hmm = 0
        self._cache_miss_kma = 0
        self._cache_miss_hmm = 0
        self._sr_cache_hit = 0
        self._last_cleanup_time = time.monotonic()
        self._last_status: Dict[str, Any] = {}

    # =========================================================================
    # 公共接口
    # =========================================================================
    async def build(self, symbol: str, kline: Kline) -> Dict[str, Any]:
        if kline is None:
            raise ValueError("kline cannot be None")
        self._validate_symbol(symbol)
        if not self._is_valid_kline(kline):
            raise ValueError(f"Invalid kline for {symbol}")

        start_time = time.perf_counter()
        self._build_count += 1

        open_time_ms = int(kline.open_time)
        context = self._init_context(symbol, kline, open_time_ms)
        degradation_reasons: List[str] = []
        degraded_components: Dict[str, bool] = {}

        try:
            primary_klines = await self._fetch_klines(symbol, self.primary_interval)
            if primary_klines is None:
                primary_klines = []
            else:
                primary_klines = list(primary_klines)
            primary_klines = self._ensure_time_order(primary_klines)
            primary_klines = self._filter_future_klines(primary_klines)

            secondary_klines: Dict[str, List[Kline]] = {}
            for tf in self.secondary_intervals:
                kls = await self._fetch_klines(symbol, tf)
                if kls is not None:
                    kls = self._ensure_time_order(list(kls))
                    kls = self._filter_future_klines(kls)
                    secondary_klines[tf] = kls
                else:
                    secondary_klines[tf] = []

            # 数据不足
            if len(primary_klines) < 2:
                degradation_reasons.append("insufficient_primary_klines")
                degraded_components["primary_data"] = True
                for tf in self.secondary_intervals:
                    self._set_default_tf_context(context, tf, kline)
                context = self._finalize_context(context, degradation_reasons, degraded_components, "degraded")
                self._validate_context_completeness(context)
                self._cache_context(symbol, open_time_ms, self._sanitize_for_cache(context))
                return context

            # 1. 主周期 KMA
            kma_primary = await self._get_or_compute_kma(
                symbol, self.primary_interval, kline, primary_klines,
                degradation_reasons, degraded_components
            )
            context["kma"] = kma_primary["level"]
            context["kma_slope"] = kma_primary["slope"]
            context["kma_bandwidth"] = kma_primary["bandwidth"]

            # 2. 主周期 HMM
            hmm_primary = await self._get_or_compute_hmm(
                symbol, self.primary_interval, kline, primary_klines,
                kma_primary["level"], degradation_reasons, degraded_components
            )
            context["hmm_state_3m"] = hmm_primary["state"]
            context["hmm_probabilities_3m"] = hmm_primary["probabilities"]

            # 3. 辅助周期
            for tf in self.secondary_intervals:
                tf_klines = secondary_klines.get(tf, [])
                if len(tf_klines) < 2:
                    self._set_default_tf_context(context, tf, kline)
                    continue
                latest_tf_kline = tf_klines[-1]
                kma_tf = await self._get_or_compute_kma(
                    symbol, tf, latest_tf_kline, tf_klines,
                    degradation_reasons, degraded_components
                )
                context[f"kma_{tf}"] = kma_tf["level"]
                context[f"kma_slope_{tf}"] = kma_tf["slope"]

                hmm_tf = await self._get_or_compute_hmm(
                    symbol, tf, latest_tf_kline, tf_klines,
                    kma_tf["level"], degradation_reasons, degraded_components
                )
                context[f"hmm_state_{tf}"] = hmm_tf["state"]
                context[f"hmm_probabilities_{tf}"] = hmm_tf["probabilities"]
                context[f"atr_{tf}"] = self._get_or_compute_atr(symbol, tf, tf_klines, degradation_reasons)

            # 4. ATR
            context["atr_3m"] = self._get_or_compute_atr(
                symbol, self.primary_interval, primary_klines, degradation_reasons
            )

            # 5. S/R
            context["sr_levels"] = {}
            if self.sr_mapping_enabled:
                await self._inject_sr_levels(
                    symbol, primary_klines, secondary_klines, context,
                    degradation_reasons, degraded_components
                )

            # 6. 市场阶段
            context["regime"] = await self._get_regime(
                symbol, primary_klines, degradation_reasons, degraded_components
            )

            # 7. 成交量
            context["volume_ma20"] = self._safe_volume_ma(
                symbol, self.primary_interval, primary_klines, degradation_reasons
            )

            # 8. 波动率分位数
            context["volatility_percentile"] = self._calculate_volatility_percentile(
                symbol, self.primary_interval, primary_klines, open_time_ms, degradation_reasons
            )

            # 聚合降级
            context = self._finalize_context(
                context, degradation_reasons, degraded_components,
                "degraded" if degradation_reasons else "normal"
            )
            self._validate_context_completeness(context)
            self._cache_context(symbol, open_time_ms, self._sanitize_for_cache(context))

        except asyncio.CancelledError:
            logger.info(f"Context build cancelled for {symbol}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error building context for {symbol}: {e}")
            degradation_reasons.append("build_exception")
            context = self._finalize_context(context, degradation_reasons, degraded_components, "degraded")
            self._validate_context_completeness(context)
        finally:
            await self._cleanup_if_needed()
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self._total_build_time_ms += elapsed_ms
            if elapsed_ms > self._max_build_time_ms:
                self._max_build_time_ms = elapsed_ms
            self._last_status = {
                "last_symbol": symbol,
                "last_quality": context.get("data_quality"),
                "last_elapsed_ms": elapsed_ms,
                "circuit_breakers": {
                    "kma_open": sum(1 for cs in self._kma_circuit.values() if self._is_circuit_open(cs)),
                    "hmm_open": sum(1 for cs in self._hmm_circuit.values() if self._is_circuit_open(cs))
                }
            }
        return context

    async def shutdown(self) -> None:
        self.clear_cache()
        logger.info("ContextPipeline shut down.")

    def get_cached_context(self, symbol: str, open_time: float) -> Optional[Dict[str, Any]]:
        open_time_ms = int(open_time)
        cache = self._context_caches.get(symbol)
        if cache:
            entry = cache.get(open_time_ms)
            return copy.deepcopy(entry) if entry else None
        return None

    def clear_cache(self, symbol: Optional[str] = None) -> None:
        if symbol:
            self._context_caches.pop(symbol, None)
            self._context_cache_total = sum(len(c) for c in self._context_caches.values())
            for cache_attr in ['_kma_cache', '_hmm_cache', '_atr_cache', '_sr_cache',
                               '_atr_history', '_last_atr_open_time', '_long_term_avg_vol',
                               '_volume_spike_counters', '_kma_circuit', '_hmm_circuit']:
                cache = getattr(self, cache_attr)
                if isinstance(cache, OrderedDict):
                    keys_to_del = [k for k in cache if (isinstance(k, tuple) and k[0] == symbol)]
                    for k in keys_to_del:
                        del cache[k]
                elif isinstance(cache, dict):
                    keys_to_del = [k for k in cache if k[0] == symbol]
                    for k in keys_to_del:
                        del cache[k]
        else:
            self._context_caches.clear()
            self._context_cache_total = 0
            self._kma_cache.clear()
            self._hmm_cache.clear()
            self._atr_cache.clear()
            self._sr_cache.clear()
            self._atr_history.clear()
            self._last_atr_open_time.clear()
            self._long_term_avg_vol.clear()
            self._volume_spike_counters.clear()
            self._kma_circuit.clear()
            self._hmm_circuit.clear()
        self._error_counters.clear()
        logger.info(f"Cache cleared for symbol={symbol or 'all'}")

    def reset(self) -> None:
        self.clear_cache()
        self._build_count = 0
        self._total_build_time_ms = 0.0
        self._max_build_time_ms = 0.0
        self._cache_hit_kma = 0
        self._cache_hit_hmm = 0
        self._cache_miss_kma = 0
        self._cache_miss_hmm = 0
        self._sr_cache_hit = 0
        self._error_counters.clear()
        self._last_cleanup_time = time.monotonic()
        self._last_status.clear()

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "build_count": self._build_count,
            "avg_build_time_ms": round(self._total_build_time_ms / max(self._build_count, 1), 2),
            "max_build_time_ms": round(self._max_build_time_ms, 2),
            "cache_hit_kma": self._cache_hit_kma,
            "cache_hit_hmm": self._cache_hit_hmm,
            "cache_miss_kma": self._cache_miss_kma,
            "cache_miss_hmm": self._cache_miss_hmm,
            "sr_cache_hit": self._sr_cache_hit,
            "kma_cache_size": len(self._kma_cache),
            "hmm_cache_size": len(self._hmm_cache),
            "atr_cache_size": len(self._atr_cache),
            "sr_cache_size": len(self._sr_cache),
            "context_cache_total": self._context_cache_total,
            "circuit_breakers": {
                "kma_open": sum(1 for cs in self._kma_circuit.values() if self._is_circuit_open(cs)),
                "hmm_open": sum(1 for cs in self._hmm_circuit.values() if self._is_circuit_open(cs))
            }
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "primary_interval": self.primary_interval,
            "secondary_intervals": self.secondary_intervals,
            "sr_mapping_enabled": self.sr_mapping_enabled,
            "cache_sizes": {
                "kma": len(self._kma_cache),
                "hmm": len(self._hmm_cache),
                "atr": len(self._atr_cache),
                "sr": len(self._sr_cache),
                "context_total": self._context_cache_total,
            },
            "last_build": self._last_status,
        }

    # =========================================================================
    # 内部辅助方法
    # =========================================================================
    @staticmethod
    def _validate_interval(interval: str) -> str:
        if not re.match(r'^(1m|3m|5m|15m|30m|1h|2h|4h|1d)$', interval):
            raise ValueError(f"Invalid interval: {interval}")
        return interval

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if not re.match(r'^[A-Za-z0-9._-]{3,30}$', symbol):
            raise ValueError(f"Invalid symbol format: {symbol}")

    @staticmethod
    def _calc_atr_ttl(interval: str) -> float:
        seconds = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}.get(interval, 600)
        return max(5.0, seconds * 0.5)

    def _is_circuit_open(self, state: Optional[CircuitState]) -> bool:
        if state is None or state.failure_count < self.circuit_breaker_threshold:
            return False
        return (time.monotonic() - state.circuit_open_time) < self.circuit_breaker_retry_sec

    def _init_context(self, symbol: str, kline: Kline, open_time_ms: int) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "primary_interval": self.primary_interval,
            "latest_kline_ohlc": {"open": kline.open, "high": kline.high, "low": kline.low, "close": kline.close},
            "last_price": kline.close,
            "open_time_ms": open_time_ms,
            "open_time": kline.open_time,
            "volume": getattr(kline, 'volume', 0) or 0,
            "data_quality": "normal",
            "degradation_reasons": [],
            "degraded_components": {},
        }

    def _finalize_context(
        self, context: Dict, degradation_reasons: List[str],
        degraded_components: Dict[str, bool], data_quality: str
    ) -> Dict:
        seen = set()
        unique = []
        for r in degradation_reasons:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        context["degradation_reasons"] = unique
        context["degraded_components"] = dict(degraded_components)
        context["data_quality"] = data_quality if unique else "normal"
        return context

    def _sanitize_for_cache(self, context: Dict) -> Dict:
        ctx = context.copy()
        ctx.pop("latest_kline_ohlc", None)
        ctx.pop("sr_levels", None)  # 移除大对象以节省缓存
        return ctx

    def _filter_future_klines(self, klines: List[Kline]) -> List[Kline]:
        now_ms = time.time() * 1000
        return [k for k in klines if k.open_time <= now_ms + self.max_future_klines_ms]

    def _ensure_time_order(self, klines: List[Kline]) -> List[Kline]:
        if not klines:
            return []
        # 使用 (open_time, close, volume) 组合键去重
        unique_dict: Dict[Tuple[int, float, float], Kline] = {}
        for k in klines:
            if k.open_time is None:
                continue
            key = (int(k.open_time), round(k.close, 6), round(k.volume, 6) if k.volume else 0.0)
            unique_dict[key] = k
        return sorted(unique_dict.values(), key=lambda x: x.open_time)

    async def _fetch_klines(self, symbol: str, interval: str) -> Optional[List[Kline]]:
        try:
            limit = min(self.percentile_lookback, self.max_fetch_bars)
            return await asyncio.wait_for(
                self.kline_buffer.get_klines(symbol, interval, limit=limit),
                timeout=self.kline_fetch_timeout,
            )
        except asyncio.TimeoutError:
            self._log_error_throttled(f"Timeout fetching {interval} klines for {symbol}", "fetch_timeout")
            return None
        except Exception as e:
            self._log_error_throttled(f"Error fetching {interval} klines for {symbol}: {e}", "fetch_error")
            return None

    def _log_error_throttled(self, msg: str, key: str):
        self._error_counters[key] = self._error_counters.get(key, 0) + 1
        if self._error_counters[key] % 10 == 1:
            logger.error(msg)
        else:
            logger.debug(msg)
        if len(self._error_counters) > self._max_error_counters:
            # 仅保留最近使用的20个不同类型的错误
            sorted_items = sorted(self._error_counters.items(), key=lambda x: -x[1])
            self._error_counters = dict(sorted_items[:20])
            logger.warning("Error counters trimmed to top 20.")

    def _set_default_tf_context(self, context: Dict, tf: str, kline: Kline):
        context[f"kma_{tf}"] = kline.close
        context[f"kma_slope_{tf}"] = 0.0
        context[f"hmm_state_{tf}"] = "RANGE"
        context[f"hmm_probabilities_{tf}"] = {"RANGE": 1.0}
        context[f"atr_{tf}"] = -1.0  # 标记不可用

    def _validate_context_completeness(self, context: Dict[str, Any]) -> None:
        defaults = {
            "kma": context.get("last_price", 0.0),
            "kma_slope": 0.0,
            "kma_bandwidth": 0.0,
            "hmm_state_3m": "RANGE",
            "hmm_probabilities_3m": {"RANGE": 1.0},
            "atr_3m": 0.0,
            "volume_ma20": 1.0,
            "volatility_percentile": 50.0,
            "regime": "RANGE",
            "sr_levels": {},
            "degradation_reasons": [],
            "degraded_components": {},
        }
        for key in REQUIRED_CONTEXT_KEYS:
            if key not in context:
                context[key] = defaults.get(key)
        if context.get("atr_3m") == 0.0 and "atr_defaulted" not in context.get("degradation_reasons", []):
            context["degradation_reasons"].append("atr_defaulted")
            context["degraded_components"]["atr"] = True

    # =========================================================================
    # 缓存管理
    # =========================================================================
    def _add_to_cache(self, cache: OrderedDict, key: Any, value: Any, max_entries: int):
        if max_entries <= 0:
            return
        # 移到末尾 (LRU)
        if key in cache:
            del cache[key]
        cache[key] = value
        while len(cache) > max_entries:
            cache.popitem(last=False)

    async def _cleanup_if_needed(self):
        now = time.monotonic()
        if now - self._last_cleanup_time < 120.0:
            return
        self._last_cleanup_time = now

        # ATR 过期清理
        for key, entry in list(self._atr_cache.items()):
            if isinstance(entry, tuple) and len(entry) >= 3:
                _, ts, ttl = entry
                if now - ts > ttl:
                    del self._atr_cache[key]
            else:
                del self._atr_cache[key]

        # S/R 过期清理
        for key, entry in list(self._sr_cache.items()):
            if isinstance(entry, tuple) and len(entry) == 2:
                _, ts = entry
                if now - ts > self._sr_ttl_sec:
                    del self._sr_cache[key]
            else:
                del self._sr_cache[key]

        # 全局上下文缓存限制
        while self._context_cache_total > self.global_context_cache_limit:
            worst_symbol = max(self._context_caches, key=lambda s: len(self._context_caches[s]), default=None)
            if worst_symbol:
                self._context_caches[worst_symbol].popitem(last=False)
                self._context_cache_total -= 1
            else:
                break

        # 长期成交量缓存
        while len(self._long_term_avg_vol) > self._long_term_avg_vol_max:
            self._long_term_avg_vol.popitem(last=False)

        # 成交量尖峰计数器清理（基于简单 FIFO）
        if len(self._volume_spike_counters) > self._volume_spike_max:
            while len(self._volume_spike_counters) > self._volume_spike_max:
                self._volume_spike_counters.popitem(last=False)

    def _cache_context(self, symbol: str, open_time_ms: int, context: Dict[str, Any]):
        if symbol not in self._context_caches:
            self._context_caches[symbol] = OrderedDict()
        cache = self._context_caches[symbol]
        if open_time_ms in cache:
            self._context_cache_total -= 1
        cache[open_time_ms] = context
        self._context_cache_total += 1
        while len(cache) > self.context_cache_size:
            cache.popitem(last=False)
            self._context_cache_total -= 1

    # =========================================================================
    # KMA (带熔断自愈)
    # =========================================================================
    async def _get_or_compute_kma(
        self, symbol: str, interval: str, kline: Kline,
        klines: List[Kline], degradation: List[str],
        degraded_comp: Dict[str, bool]
    ) -> Dict[str, float]:
        cache_key = (symbol, interval, int(kline.open_time))
        if cache_key in self._kma_cache:
            self._cache_hit_kma += 1
            self._add_to_cache(self._kma_cache, cache_key, self._kma_cache[cache_key], self._kma_max_entries)
            return self._kma_cache[cache_key]
        self._cache_miss_kma += 1

        circuit_key = (symbol, interval)
        state = self._kma_circuit.get(circuit_key)
        now = time.monotonic()

        if state and self._is_circuit_open(state):
            logger.warning(f"KMA circuit breaker open for {symbol} {interval}, returning close price.")
            degradation.append("kma_circuit_open")
            degraded_comp[f"kma_{interval}"] = True
            result = {"level": kline.close, "slope": 0.0, "bandwidth": 0.0}
            self._add_to_cache(self._kma_cache, cache_key, result, self._kma_max_entries)
            return result

        result = {"level": kline.close, "slope": 0.0, "bandwidth": 0.0}
        success = False
        try:
            recent_vol = self._get_or_compute_atr(symbol, interval, klines, degradation)
            ctx = {"recent_volatility": recent_vol, "klines": klines[-100:]}
            raw = await asyncio.wait_for(self.kma_computer.compute(kline, ctx), timeout=self.kma_compute_timeout)
            if raw and isinstance(raw, dict):
                kma_level = float(raw.get("kma", kline.close))
                kma_slope = float(raw.get("kma_slope", 0.0))
                denom = max(kline.close * 0.01, 1e-4)
                bandwidth = abs(kline.close - kma_level) / denom
                result = {"level": kma_level, "slope": kma_slope, "bandwidth": min(bandwidth, 100.0)}
                success = True
            else:
                degradation.append(f"kma_empty_result_{interval}")
                degraded_comp[f"kma_{interval}"] = True
        except asyncio.TimeoutError:
            logger.warning(f"KMA timeout {symbol} {interval}")
            degradation.append(f"kma_timeout_{interval}")
            degraded_comp[f"kma_{interval}"] = True
        except Exception as e:
            logger.error(f"KMA error {symbol} {interval}: {e}")
            degradation.append(f"kma_error_{interval}")
            degraded_comp[f"kma_{interval}"] = True

        # 更新熔断状态
        if success:
            if circuit_key in self._kma_circuit:
                cs = self._kma_circuit[circuit_key]
                cs.success_count += 1
                if cs.success_count >= self.circuit_success_reset:
                    del self._kma_circuit[circuit_key]
                    logger.info(f"KMA circuit breaker reset for {symbol} {interval}")
        else:
            if circuit_key not in self._kma_circuit:
                self._kma_circuit[circuit_key] = CircuitState()
            cs = self._kma_circuit[circuit_key]
            cs.failure_count += 1
            cs.last_failure_time = now
            if cs.failure_count >= self.circuit_breaker_threshold and not self._is_circuit_open(cs):
                cs.circuit_open_time = now
                logger.warning(f"KMA circuit breaker opened for {symbol} {interval}")

        self._add_to_cache(self._kma_cache, cache_key, result, self._kma_max_entries)
        return result

    # =========================================================================
    # HMM (带熔断自愈)
    # =========================================================================
    async def _get_or_compute_hmm(
        self, symbol: str, interval: str, kline: Kline,
        klines: List[Kline], kma_level: float,
        degradation: List[str], degraded_comp: Dict[str, bool]
    ) -> Dict[str, Any]:
        cache_key = (symbol, interval, int(kline.open_time))
        if cache_key in self._hmm_cache:
            self._cache_hit_hmm += 1
            self._add_to_cache(self._hmm_cache, cache_key, self._hmm_cache[cache_key], self._hmm_max_entries)
            return copy.deepcopy(self._hmm_cache[cache_key])
        self._cache_miss_hmm += 1

        circuit_key = (symbol, interval)
        state = self._hmm_circuit.get(circuit_key)
        now = time.monotonic()

        if state and self._is_circuit_open(state):
            logger.warning(f"HMM circuit breaker open for {symbol} {interval}, returning RANGE.")
            degradation.append("hmm_circuit_open")
            degraded_comp[f"hmm_{interval}"] = True
            result = {"state": "RANGE", "probabilities": {"RANGE": 1.0}}
            self._add_to_cache(self._hmm_cache, cache_key, result, self._hmm_max_entries)
            return result

        result = {"state": "RANGE", "probabilities": {"RANGE": 1.0}}
        success = False
        try:
            is_trained = False
            try:
                trained_attr = getattr(self.hmm_detector, 'is_trained', False)
                is_trained = trained_attr() if callable(trained_attr) else bool(trained_attr)
            except Exception:
                pass

            if not is_trained:
                logger.debug(f"HMM not trained for {symbol} {interval}")
                degradation.append(f"hmm_not_trained_{interval}")
                degraded_comp[f"hmm_{interval}"] = True
                self._add_to_cache(self._hmm_cache, cache_key, result, self._hmm_max_entries)
                return result

            features = self._extract_hmm_features(kline, klines, kma_level, interval, symbol, degradation)
            if features:
                state_raw, probs = await asyncio.wait_for(self.hmm_detector.predict(features), timeout=self.hmm_predict_timeout)
                state_str = self._normalize_hmm_state(state_raw)
                result["state"] = state_str
                if probs:
                    cleaned = {}
                    for k, v in probs.items():
                        if isinstance(v, (int, float)) and not math.isnan(v) and not math.isinf(v):
                            cleaned[str(k)] = float(v)
                    if not cleaned:
                        degradation.append(f"hmm_probs_all_invalid_{interval}")
                        degraded_comp[f"hmm_{interval}"] = True
                    result["probabilities"] = cleaned
                else:
                    degradation.append(f"hmm_probs_none_{interval}")
                    degraded_comp[f"hmm_{interval}"] = True
                success = True
            else:
                degradation.append(f"hmm_features_empty_{interval}")
                degraded_comp[f"hmm_{interval}"] = True
        except asyncio.TimeoutError:
            logger.warning(f"HMM timeout {symbol} {interval}")
            degradation.append(f"hmm_timeout_{interval}")
            degraded_comp[f"hmm_{interval}"] = True
        except Exception as e:
            logger.error(f"HMM error {symbol} {interval}: {e}")
            degradation.append(f"hmm_error_{interval}")
            degraded_comp[f"hmm_{interval}"] = True

        if success:
            if circuit_key in self._hmm_circuit:
                cs = self._hmm_circuit[circuit_key]
                cs.success_count += 1
                if cs.success_count >= self.circuit_success_reset:
                    del self._hmm_circuit[circuit_key]
                    logger.info(f"HMM circuit breaker reset for {symbol} {interval}")
        else:
            if circuit_key not in self._hmm_circuit:
                self._hmm_circuit[circuit_key] = CircuitState()
            cs = self._hmm_circuit[circuit_key]
            cs.failure_count += 1
            cs.last_failure_time = now
            if cs.failure_count >= self.circuit_breaker_threshold and not self._is_circuit_open(cs):
                cs.circuit_open_time = now
                logger.warning(f"HMM circuit breaker opened for {symbol} {interval}")

        self._add_to_cache(self._hmm_cache, cache_key, result, self._hmm_max_entries)
        return result

    def _normalize_hmm_state(self, state: Any) -> str:
        if state is None:
            return "RANGE"
        if isinstance(state, str):
            s = state.upper()
            if "BULL" in s: return "BULL"
            if "BEAR" in s: return "BEAR"
            if "RANGE" in s: return "RANGE"
            self._log_error_throttled(f"Unknown HMM state string: {state}", "hmm_state_string")
            return "RANGE"
        if hasattr(state, 'value'):
            return self._normalize_hmm_state(state.value)
        self._log_error_throttled(f"Unknown HMM state type: {type(state)}", "hmm_state_type")
        return "RANGE"

    # =========================================================================
    # ATR
    # =========================================================================
    def _get_or_compute_atr(
        self, symbol: str, interval: str, klines: List[Kline],
        degradation: List[str]
    ) -> float:
        cache_key = (symbol, interval)
        now = time.monotonic()
        ttl = self._calc_atr_ttl(interval)
        if cache_key in self._atr_cache:
            entry = self._atr_cache[cache_key]
            if isinstance(entry, tuple) and len(entry) >= 3:
                val, ts, cached_ttl = entry
                if now - ts < cached_ttl:
                    return val
        atr = self._calculate_atr(klines, self.atr_period)
        if atr is None or atr <= 0.0:
            atr = self._get_dynamic_min_atr(klines)
            degradation.append("atr_insufficient_data")
        self._add_to_cache(self._atr_cache, cache_key, (atr, now, ttl), self._atr_max_entries)
        return atr

    def _get_dynamic_min_atr(self, klines: List[Kline]) -> float:
        if not klines:
            return max(self.tick_size or 0.01, 0.01)
        closes = [k.close for k in klines[-20:] if k.close is not None and k.close > 0]
        if not closes:
            return max(self.tick_size or 0.01, 0.01)
        median_price = sorted(closes)[len(closes)//2]
        return max(median_price * 0.0001, self.tick_size or 0.01)

    # =========================================================================
    # S/R
    # =========================================================================
    async def _inject_sr_levels(
        self, symbol: str, primary_klines: List[Kline],
        secondary_klines: Dict[str, List[Kline]],
        context: Dict, degradation: List[str], degraded_comp: Dict[str, bool]
    ):
        filtered_sr: Dict[str, Any] = {}
        try:
            cache_key = (symbol, tuple(sorted(self.secondary_intervals)))
            now = time.monotonic()
            if cache_key in self._sr_cache:
                entry = self._sr_cache[cache_key]
                if isinstance(entry, tuple) and len(entry) == 2:
                    sr_data, ts = entry
                    if now - ts < self._sr_ttl_sec:
                        context["sr_levels"] = sr_data
                        self._sr_cache_hit += 1
                        return
                else:
                    del self._sr_cache[cache_key]

            sr_levels = await asyncio.wait_for(
                self.sr_pipeline.compute_all(symbol, primary_klines, secondary_klines),
                timeout=self.sr_compute_timeout,
            )
            if sr_levels is not None:
                try:
                    filtered_sr = self.hierarchy_guard.filter_sr_levels(
                        self.primary_interval, copy.deepcopy(sr_levels)
                    )
                except Exception as e:
                    logger.error(f"filter_sr_levels failed: {e}")
                    degradation.append("sr_filter_error")
                    degraded_comp["sr"] = True
                    filtered_sr = {}
                self._add_to_cache(self._sr_cache, cache_key, (filtered_sr, now), self._sr_max_entries)
                context["sr_levels"] = filtered_sr
            else:
                degradation.append("sr_compute_null")
                degraded_comp["sr"] = True
        except asyncio.TimeoutError:
            logger.warning(f"S/R timeout for {symbol}")
            degradation.append("sr_timeout")
            degraded_comp["sr"] = True
            self._add_to_cache(self._sr_cache, cache_key, (filtered_sr, now), self._sr_max_entries)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"S/R error for {symbol}: {e}")
            degradation.append("sr_error")
            degraded_comp["sr"] = True

    async def _get_regime(
        self, symbol: str, klines: List[Kline],
        degradation: List[str], degraded_comp: Dict[str, bool]
    ) -> str:
        try:
            regime = await asyncio.wait_for(
                self.regime_monitor.get_regime(symbol, self.primary_interval, klines),
                timeout=self.regime_timeout,
            )
            if regime is None:
                degradation.append("regime_null")
                degraded_comp["regime"] = True
                return "RANGE"
            return str(regime.value) if hasattr(regime, 'value') else str(regime)
        except asyncio.TimeoutError:
            logger.warning(f"Regime timeout for {symbol}")
            degradation.append("regime_timeout")
            degraded_comp["regime"] = True
            return "RANGE"
        except Exception as e:
            logger.error(f"Regime error for {symbol}: {e}")
            degradation.append("regime_error")
            degraded_comp["regime"] = True
            return "RANGE"

    # =========================================================================
    # 成交量
    # =========================================================================
    def _safe_volume_ma(
        self, symbol: str, interval: str, klines: List[Kline],
        degradation: List[str]
    ) -> float:
        ma = self._calculate_volume_ma(klines, self.volume_ma_period)
        key = (symbol, interval)
        if ma is not None and ma > 0 and not math.isnan(ma) and not math.isinf(ma):
            old = self._long_term_avg_vol.get(key)
            if old is not None and old > 0:
                if ma > old * 10.0 and (ma - old) > max(1000, old * 2.0):
                    spike_count = self._volume_spike_counters.get(key, 0) + 1
                    self._volume_spike_counters[key] = spike_count
                    if spike_count >= 3:
                        self._long_term_avg_vol[key] = old * (1 - self.volume_ema_alpha) + ma * self.volume_ema_alpha
                        self._volume_spike_counters[key] = 0
                    else:
                        degradation.append("volume_ma_spike_rejected")
                        return old
                else:
                    self._volume_spike_counters[key] = 0
                    self._long_term_avg_vol[key] = old * (1 - self.volume_ema_alpha) + ma * self.volume_ema_alpha
            else:
                self._long_term_avg_vol[key] = ma
            while len(self._long_term_avg_vol) > self._long_term_avg_vol_max:
                self._long_term_avg_vol.popitem(last=False)
            return self._long_term_avg_vol[key]
        if key in self._long_term_avg_vol:
            return self._long_term_avg_vol[key]
        degradation.append("volume_ma_unavailable")
        return 1.0

    # =========================================================================
    # 纯计算函数
    # =========================================================================
    def _calculate_atr(self, klines: List[Kline], period: int = 14) -> Optional[float]:
        if len(klines) < 2:
            return None
        tr_values = []
        for i in range(1, min(len(klines), period + 1)):
            prev = klines[-i - 1]
            curr = klines[-i]
            high = getattr(curr, 'high', 0.0) or 0.0
            low = getattr(curr, 'low', 0.0) or 0.0
            prev_close = getattr(prev, 'close', 0.0) or 0.0
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            if math.isnan(tr) or math.isinf(tr):
                continue
            tr_values.append(tr)
        if not tr_values:
            return None
        return sum(tr_values) / len(tr_values)

    def _calculate_volume_ma(self, klines: List[Kline], period: int = 20) -> Optional[float]:
        if len(klines) < period:
            return None
        volumes = []
        for k in klines[-period:]:
            vol = getattr(k, 'volume', None)
            if vol is not None and vol >= 0 and not math.isnan(vol) and not math.isinf(vol):
                volumes.append(vol)
        if not volumes:
            return None
        avg = sum(volumes) / len(volumes)
        if math.isnan(avg) or math.isinf(avg):
            return None
        return avg

    def _calculate_volatility_percentile(
        self, symbol: str, interval: str, klines: List[Kline],
        open_time_ms: int, degradation: List[str]
    ) -> float:
        if len(klines) < self.atr_period + 1:
            return 50.0
        current_atr = self._get_or_compute_atr(symbol, interval, klines, degradation)
        if current_atr is None or current_atr <= 0:
            return 50.0

        hist_key = (symbol, interval)
        last_ms = self._last_atr_open_time.get(hist_key, 0)
        if open_time_ms > last_ms:
            if hist_key not in self._atr_history:
                self._atr_history[hist_key] = deque(maxlen=self.percentile_lookback)
            self._atr_history[hist_key].append(current_atr)
            self._last_atr_open_time[hist_key] = open_time_ms

        history = self._atr_history.get(hist_key)
        if not history or len(history) < 10:
            return 50.0

        median_atr = sorted(history)[len(history)//2]
        if median_atr == 0:
            return 50.0

        if current_atr < median_atr * 0.1:
            return 5.0

        if len(history) >= 3:
            recent_three = list(history)[-3:]
            if all(v > median_atr * 5.0 for v in recent_three):
                valid_history = [v for v in history if v <= median_atr * 5.0]
            else:
                valid_history = [v for v in history if v <= current_atr * 10.0]
        else:
            valid_history = [v for v in history if v <= current_atr * 10.0]

        if not valid_history:
            degradation.append("volatility_percentile_no_valid_history")
            return 50.0

        sorted_history = sorted(valid_history)
        count = sum(1 for v in sorted_history if v <= current_atr)
        percentile = (count / len(sorted_history)) * 100.0
        return min(100.0, max(0.0, percentile))

    def _extract_hmm_features(
        self, kline: Kline, klines: List[Kline], kma_level: float,
        interval: str, symbol: str, degradation: List[str]
    ) -> Dict[str, float]:
        if len(klines) < 20:
            # 尝试扩大窗口至50根
            extended = klines
            if len(extended) >= 20:
                pass  # 实际上还是会返回空，但逻辑保留
            return {}
        try:
            closes = [k.close for k in klines[-20:] if k.close is not None and k.close > 0]
            volumes = [k.volume for k in klines[-20:] if getattr(k, 'volume', None) is not None and k.volume >= 0]
            if len(closes) < 2 or not volumes:
                return {}
            log_ret = 0.0
            if closes[-2] > 0 and closes[-1] > 0:
                log_ret = math.log(closes[-1] / closes[-2])
            atr = self._get_or_compute_atr(symbol, interval, klines, degradation)
            if atr <= 0:
                atr = 1e-8
            range_norm = min(max(kline.high - kline.low, 0) / atr, 10.0)
            deviation = (kline.close - kma_level) / atr
            deviation = max(min(deviation, 10.0), -10.0)
            avg_vol = sum(volumes) / len(volumes)
            vol_ratio = kline.volume / max(avg_vol, 1e-10) if kline.volume is not None else 1.0
            return {
                "log_ret": 0.0 if math.isnan(log_ret) or math.isinf(log_ret) else log_ret,
                "range_norm": range_norm,
                "deviation": deviation,
                "vol_ratio": min(vol_ratio, 10.0),
            }
        except Exception as e:
            logger.error(f"Failed to extract HMM features for {symbol}: {e}")
            return {}

    # =========================================================================
    # K线验证
    # =========================================================================
    def _is_valid_kline(self, kline: Kline) -> bool:
        try:
            if kline.close is None or kline.close <= 0:
                return False
            if kline.high is None or kline.low is None or kline.open is None:
                return False
            if kline.high < kline.low:
                return False
            if kline.volume is not None and kline.volume < 0:
                return False
            if kline.open_time is None or kline.open_time <= 0:
                return False
            if not self.strict_time_check:
                return True
            now_ms = time.time() * 1000
            if kline.open_time < (now_ms - 7 * 86400_000) or kline.open_time > (now_ms + 1 * 86400_000):
                logger.warning(f"Kline open_time out of range: {kline.open_time}")
                return False
            return True
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"<ContextPipeline primary={self.primary_interval} intervals={self.secondary_intervals}>"
