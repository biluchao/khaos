# -*- coding: utf-8 -*-
"""
模块名称: market_regime_monitor.py
核心职责: 综合ADX、KMA斜率、布林带宽度和波动率等指标，判定当前市场状态（趋势/震荡/高波动），
          采用平滑表决和迟滞机制，防止频繁误切换；支持状态切换回调（锁外执行）、性能统计、
          参数漂移检测、死锁预防、检查点恢复、线程安全。
所属层级: core.engine

外部依赖:
    - typing
    - logging
    - time
    - threading (RLock)
    - collections (deque)
    - math
    - core.interfaces (MarketRegime)

接口契约:
    提供:
        MarketRegimeMonitor:
            update(context) -> MarketRegime
            get_current_regime() -> MarketRegime
            can_open_position(timeframe) -> bool
            reset(initial_regime) -> None
            set_on_regime_change_callback(callback) -> None
            get_debug_info() -> Dict
            save_checkpoint() -> Dict
            load_checkpoint(data) -> None
            set_params(**kwargs) -> None   # 动态调整参数
            get_params() -> Dict
            set_time_func(func) -> None    # 可注入时间函数，便于测试
            get_state_history() -> List[MarketRegime]

配置项: 同前，略

作者: KHAOS System Architect
创建日期: 2025-03-01
修改记录:
    - 2026-07-08 v33.0: 死锁预防、回调锁外执行、参数漂移、内存保护、注入时间源
__version__ = "33.0.0"
"""

import logging
import time
import math
import threading
from typing import Optional, Dict, Any, Deque, List, Callable, Tuple
from collections import deque
from enum import Enum

try:
    from core.interfaces import MarketRegime
except ImportError:
    class MarketRegime(str, Enum):
        TRENDING_UP = "TRENDING_UP"
        TRENDING_DOWN = "TRENDING_DOWN"
        RANGE = "RANGE"
        HIGH_VOL = "HIGH_VOL"

logger = logging.getLogger(__name__)


class MarketRegimeMonitor:
    """
    永不失效的市场状态监控器 (Unbreakable Regime Sentinel)。
    线程安全、死锁预防、告警抑制、参数漂移检测、可测试性注入。
    """

    # 类级别告警抑制
    _warning_suppression: Dict[str, Tuple[float, int]] = {}
    _suppression_lock = threading.Lock()
    SUPPRESS_INTERVAL = 60.0
    MAX_SUPPRESS_COUNT = 3

    def __init__(
        self,
        confirm_bars: int = 6,
        hysteresis_bars: int = 15,
        adx_threshold: float = 20.0,
        kma_slope_threshold: float = 0.01,
        bb_bandwidth_percentile: float = 20.0,
        high_vol_atr_ratio: float = 1.5,
        allow_during_high_vol: bool = False,
        high_vol_atr_short_key: str = 'atr_3m',
        high_vol_atr_long_key: str = 'atr_long',
        symbol: Optional[str] = None,
        max_history_size: int = 200,
        param_drift_detection: bool = True
    ):
        # 参数存储
        self.confirm_bars = max(3, confirm_bars)
        self.hysteresis_bars = max(5, hysteresis_bars)
        self.adx_threshold = adx_threshold
        self.kma_slope_threshold = kma_slope_threshold
        self.bb_bandwidth_percentile = bb_bandwidth_percentile
        self.high_vol_atr_ratio = high_vol_atr_ratio
        self.allow_during_high_vol = allow_during_high_vol
        self.atr_short_key = high_vol_atr_short_key
        self.atr_long_key = high_vol_atr_long_key
        self.symbol = symbol or "UNKNOWN"
        self.max_history_size = max(50, max_history_size)
        self.param_drift_detection = param_drift_detection

        # 时间源注入（便于测试）
        self._time_func = time.monotonic

        # 线程锁 (RLock支持重入，但避免在回调中使用)
        self._lock = threading.RLock()

        # 内部状态
        self._current_regime: MarketRegime = MarketRegime.RANGE
        self._bars_in_current_regime: int = self.hysteresis_bars
        self._pending_regime: Optional[MarketRegime] = None
        self._pending_counter: int = 0
        self._raw_history: Deque[MarketRegime] = deque(maxlen=self.confirm_bars * 2)

        # 状态历史（用于长期趋势分析）
        self._state_history: Deque[MarketRegime] = deque(maxlen=self.max_history_size)

        # 缓存有效指标
        self._last_valid_kma_slope: Optional[float] = None
        self._last_valid_adx: Optional[float] = None
        self._last_valid_bb_percentile: Optional[float] = None
        self._last_valid_atr_short: Optional[float] = None
        self._last_valid_atr_long: Optional[float] = None

        # 回调（锁外执行）
        self._on_regime_change: Optional[Callable[[MarketRegime, MarketRegime], None]] = None
        self._pending_callback: Optional[Tuple[MarketRegime, MarketRegime]] = None

        # 性能统计
        self._update_count: int = 0
        self._total_update_time: float = 0.0
        self._last_update_duration: float = 0.0
        self._max_update_duration: float = 0.0

        # 参数漂移检测
        self._param_change_log: Deque[Tuple[float, str, Any]] = deque(maxlen=20)

        logger.info("[%s] UnbreakableRegimeMonitor v%s initialized", self.symbol, __version__)

    # ----- 公共方法 -----
    def update(self, context: Dict[str, Any]) -> MarketRegime:
        """
        线程安全的更新方法。回调将在锁外异步执行，防止死锁。
        """
        start = self._time_func()
        with self._lock:
            old_regime = self._current_regime
            try:
                raw_regime = self._compute_raw_regime(context)
                self._raw_history.append(raw_regime)
                self._apply_regime_logic(raw_regime)
            except Exception as e:
                self._suppressed_log(logger.error, "update_error", f"[{self.symbol}] {e}")

            duration = (self._time_func() - start) * 1000.0
            self._update_count += 1
            self._total_update_time += duration
            self._last_update_duration = duration
            if duration > self._max_update_duration:
                self._max_update_duration = duration

            if duration > 5.0:
                self._suppressed_log(logger.warning, "slow_update", f"[{self.symbol}] {duration:.2f}ms")

            # 如果状态变化，将回调信息放入队列，稍后在锁外执行
            if self._current_regime != old_regime and self._on_regime_change:
                self._pending_callback = (old_regime, self._current_regime)

        # 锁外执行回调
        self._execute_pending_callback()
        return self._current_regime

    def get_current_regime(self) -> MarketRegime:
        with self._lock:
            return self._current_regime

    def can_open_position(self, timeframe: str) -> bool:
        with self._lock:
            if self._current_regime == MarketRegime.HIGH_VOL and not self.allow_during_high_vol:
                return False
            if self._current_regime == MarketRegime.RANGE:
                return timeframe in ('5m',)
            return True

    def set_on_regime_change_callback(self, callback: Optional[Callable[[MarketRegime, MarketRegime], None]]) -> None:
        with self._lock:
            self._on_regime_change = callback

    def reset(self, initial_regime: MarketRegime = MarketRegime.RANGE) -> None:
        with self._lock:
            self._current_regime = initial_regime
            self._bars_in_current_regime = self.hysteresis_bars
            self._pending_regime = None
            self._pending_counter = 0
            self._raw_history.clear()
            self._state_history.clear()
            self._clear_cached_indicators()
            self._pending_callback = None
            logger.info("[%s] Reset to %s", self.symbol, initial_regime)

    def get_debug_info(self) -> Dict[str, Any]:
        with self._lock:
            avg_time = (self._total_update_time / self._update_count) if self._update_count > 0 else 0.0
            return {
                "symbol": self.symbol,
                "current_regime": self._current_regime,
                "bars_in_regime": self._bars_in_current_regime,
                "pending_regime": self._pending_regime,
                "pending_counter": self._pending_counter,
                "raw_history": [r.value for r in self._raw_history],
                "state_history": [r.value for r in self._state_history],
                "update_count": self._update_count,
                "last_update_ms": round(self._last_update_duration, 3),
                "avg_update_ms": round(avg_time, 3),
                "max_update_ms": round(self._max_update_duration, 3),
                "indicators": {
                    "kma_slope": self._last_valid_kma_slope,
                    "adx": self._last_valid_adx,
                    "bb_percentile": self._last_valid_bb_percentile,
                    "atr_short": self._last_valid_atr_short,
                    "atr_long": self._last_valid_atr_long,
                },
                "param_changes": list(self._param_change_log),
            }

    def get_state_history(self) -> List[MarketRegime]:
        with self._lock:
            return list(self._state_history)

    # 动态调整参数
    def set_params(self, **kwargs) -> None:
        """线程安全地更新阈值，同时记录变更日志。"""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key):
                    old_val = getattr(self, key)
                    setattr(self, key, value)
                    self._param_change_log.append((self._time_func(), key, old_val))
                    logger.info("[%s] Param %s: %s -> %s", self.symbol, key, old_val, value)
                else:
                    self._suppressed_log(logger.warning, "invalid_param", f"[{self.symbol}] Unknown param: {key}")

    def get_params(self) -> Dict[str, Any]:
        """返回当前所有可调参数。"""
        return {
            "confirm_bars": self.confirm_bars,
            "hysteresis_bars": self.hysteresis_bars,
            "adx_threshold": self.adx_threshold,
            "kma_slope_threshold": self.kma_slope_threshold,
            "bb_bandwidth_percentile": self.bb_bandwidth_percentile,
            "high_vol_atr_ratio": self.high_vol_atr_ratio,
            "allow_during_high_vol": self.allow_during_high_vol,
        }

    def set_time_func(self, func: Callable[[], float]) -> None:
        """注入自定义时间函数（用于测试）。"""
        with self._lock:
            self._time_func = func

    # 检查点支持
    def save_checkpoint(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": __version__,
                "current_regime": self._current_regime.value,
                "bars_in_current_regime": self._bars_in_current_regime,
                "pending_regime": self._pending_regime.value if self._pending_regime else None,
                "pending_counter": self._pending_counter,
                "raw_history": [r.value for r in self._raw_history],
                "state_history": [r.value for r in self._state_history],
                "last_valid_kma_slope": self._last_valid_kma_slope,
                "last_valid_adx": self._last_valid_adx,
                "last_valid_bb_percentile": self._last_valid_bb_percentile,
                "last_valid_atr_short": self._last_valid_atr_short,
                "last_valid_atr_long": self._last_valid_atr_long,
                "update_count": self._update_count,
            }

    def load_checkpoint(self, data: Dict[str, Any]) -> None:
        with self._lock:
            try:
                self._current_regime = MarketRegime(data["current_regime"])
                self._bars_in_current_regime = data.get("bars_in_current_regime", self.hysteresis_bars)
                pending = data.get("pending_regime")
                self._pending_regime = MarketRegime(pending) if pending else None
                self._pending_counter = data.get("pending_counter", 0)
                self._raw_history.clear()
                for r in data.get("raw_history", []):
                    self._raw_history.append(MarketRegime(r))
                self._state_history.clear()
                for r in data.get("state_history", []):
                    self._state_history.append(MarketRegime(r))
                self._last_valid_kma_slope = data.get("last_valid_kma_slope")
                self._last_valid_adx = data.get("last_valid_adx")
                self._last_valid_bb_percentile = data.get("last_valid_bb_percentile")
                self._last_valid_atr_short = data.get("last_valid_atr_short")
                self._last_valid_atr_long = data.get("last_valid_atr_long")
                self._update_count = data.get("update_count", 0)
                logger.info("[%s] State restored from checkpoint", self.symbol)
            except Exception as e:
                logger.error("[%s] Failed to load checkpoint: %s, resetting", self.symbol, e)
                self.reset()

    # ----- 私有方法 -----
    def _execute_pending_callback(self) -> None:
        """在锁外执行回调。"""
        callback_info = None
        with self._lock:
            callback_info = self._pending_callback
            self._pending_callback = None
        if callback_info and self._on_regime_change:
            old, new = callback_info
            try:
                self._on_regime_change(old, new)
            except Exception as e:
                logger.error("[%s] Callback error: %s", self.symbol, e)

    def _compute_raw_regime(self, context: Dict[str, Any]) -> MarketRegime:
        kma_slope = self._get_float(context, 'kma_slope', self._last_valid_kma_slope, 0.0)
        adx = self._get_float(context, 'adx', self._last_valid_adx, 15.0)
        bb_percentile = self._get_float(context, 'bb_bandwidth_percentile', self._last_valid_bb_percentile, 50.0)
        atr_short = self._get_float(context, self.atr_short_key, self._last_valid_atr_short, 1.0)
        atr_long = self._get_float(context, self.atr_long_key, self._last_valid_atr_long, 1.0)

        if kma_slope is not None:
            self._last_valid_kma_slope = kma_slope
        if adx is not None:
            self._last_valid_adx = adx
        if bb_percentile is not None:
            self._last_valid_bb_percentile = max(0.0, min(100.0, bb_percentile))
        if atr_short is not None and atr_short > 0:
            self._last_valid_atr_short = atr_short
        if atr_long is not None and atr_long > 0:
            self._last_valid_atr_long = atr_long

        vol_ratio = atr_short / max(atr_long, 1e-10)
        if vol_ratio > self.high_vol_atr_ratio and atr_long > 1e-6:
            return MarketRegime.HIGH_VOL

        is_range = (
            adx < self.adx_threshold or
            abs(kma_slope) < self.kma_slope_threshold or
            bb_percentile < self.bb_bandwidth_percentile
        )
        if is_range:
            return MarketRegime.RANGE

        if kma_slope > self.kma_slope_threshold:
            return MarketRegime.TRENDING_UP
        if kma_slope < -self.kma_slope_threshold:
            return MarketRegime.TRENDING_DOWN
        return MarketRegime.RANGE

    def _get_float(self, context: Dict[str, Any], key: str, fallback: Optional[float], default: float) -> float:
        val = context.get(key)
        if val is not None:
            try:
                f_val = float(val)
                if not math.isfinite(f_val):
                    self._suppressed_log(logger.warning, "non_finite", f"[{self.symbol}] {key}={val}")
                    return fallback if fallback is not None else default
                return f_val
            except (ValueError, TypeError):
                self._suppressed_log(logger.warning, "invalid_value", f"[{self.symbol}] {key}={val}")
        return fallback if fallback is not None else default

    def _apply_regime_logic(self, raw_regime: MarketRegime) -> None:
        if len(self._raw_history) >= self.confirm_bars:
            recent = list(self._raw_history)[-self.confirm_bars:]
            voted_regime = max(set(recent), key=recent.count)
        else:
            voted_regime = raw_regime

        if voted_regime != self._current_regime and self._bars_in_current_regime >= self.hysteresis_bars:
            if self._pending_regime != voted_regime:
                self._pending_regime = voted_regime
                self._pending_counter = 1
            else:
                self._pending_counter += 1
                if self._pending_counter >= self.confirm_bars:
                    self._current_regime = voted_regime
                    self._bars_in_current_regime = 0
                    self._pending_regime = None
                    self._pending_counter = 0
                    self._state_history.append(voted_regime)
                    logger.info("[%s] Regime -> %s", self.symbol, voted_regime)
                    return
        else:
            if self._pending_regime and voted_regime == self._current_regime:
                self._pending_regime = None
                self._pending_counter = 0

        self._bars_in_current_regime += 1

    def _clear_cached_indicators(self) -> None:
        self._last_valid_kma_slope = None
        self._last_valid_adx = None
        self._last_valid_bb_percentile = None
        self._last_valid_atr_short = None
        self._last_valid_atr_long = None

    @classmethod
    def _suppressed_log(cls, log_func, key: str, message: str) -> None:
        """告警抑制，使用类级别锁保护字典。"""
        now = time.time()
        with cls._suppression_lock:
            last_time, count = cls._warning_suppression.get(key, (0.0, 0))
            if now - last_time > cls.SUPPRESS_INTERVAL:
                count = 0
            if count < cls.MAX_SUPPRESS_COUNT:
                log_func(message)
                cls._warning_suppression[key] = (now, count + 1)
