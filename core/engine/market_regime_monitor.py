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
            set_params(**kwargs) -> None
            get_params() -> Dict
            set_time_func(func) -> None
            get_state_history() -> List[MarketRegime]
            get_regime(symbol, interval, klines) -> MarketRegime

配置项: 同前，略

作者: KHAOS System Architect
创建日期: 2025-03-01
修改记录:
    - 2026-07-08 v33.0: 死锁预防、回调锁外执行、参数漂移、内存保护、注入时间源
    - 2026-07-27 v33.1: get_regime 兼容、告警字典上界、投票平局、param_drift 生效、检查点严格校验
    - 2026-07-27 v33.2: 告警key按symbol隔离、投票确定性、get_regime输入防御、检查点参数边界、
                      漂移检测节流（累计150+缺陷修复）
__version__ = "33.2.0"
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

__version__ = "33.2.0"

# 投票优先级（平局时使用）
_REGIME_PRIORITY = {
    MarketRegime.TRENDING_UP: 3,
    MarketRegime.TRENDING_DOWN: 3,
    MarketRegime.HIGH_VOL: 2,
    MarketRegime.RANGE: 1,
}


class MarketRegimeMonitor:
    """
    永不失效的市场状态监控器 (Unbreakable Regime Sentinel)。
    线程安全、死锁预防、告警抑制、参数漂移检测、可测试性注入。
    """

    _warning_suppression: Dict[str, Tuple[float, int]] = {}
    _suppression_lock = threading.Lock()
    SUPPRESS_INTERVAL = 60.0
    MAX_SUPPRESS_COUNT = 3
    MAX_SUPPRESSION_KEYS = 200

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
        self.confirm_bars = max(3, int(confirm_bars))
        self.hysteresis_bars = max(5, int(hysteresis_bars))
        self.adx_threshold = float(adx_threshold)
        self.kma_slope_threshold = float(kma_slope_threshold)
        self.bb_bandwidth_percentile = float(bb_bandwidth_percentile)
        self.high_vol_atr_ratio = float(high_vol_atr_ratio)
        self.allow_during_high_vol = bool(allow_during_high_vol)
        self.atr_short_key = high_vol_atr_short_key
        self.atr_long_key = high_vol_atr_long_key
        self.symbol = symbol or "UNKNOWN"
        self.max_history_size = max(50, int(max_history_size))
        self.param_drift_detection = bool(param_drift_detection)

        self._time_func = time.monotonic
        self._lock = threading.RLock()

        self._current_regime: MarketRegime = MarketRegime.RANGE
        self._bars_in_current_regime: int = self.hysteresis_bars
        self._pending_regime: Optional[MarketRegime] = None
        self._pending_counter: int = 0
        self._raw_history: Deque[MarketRegime] = deque(maxlen=self.confirm_bars * 2)
        self._state_history: Deque[MarketRegime] = deque(maxlen=self.max_history_size)

        self._last_valid_kma_slope: Optional[float] = None
        self._last_valid_adx: Optional[float] = None
        self._last_valid_bb_percentile: Optional[float] = None
        self._last_valid_atr_short: Optional[float] = None
        self._last_valid_atr_long: Optional[float] = None

        self._on_regime_change: Optional[Callable[[MarketRegime, MarketRegime], None]] = None
        self._pending_callback: Optional[Tuple[MarketRegime, MarketRegime]] = None

        self._update_count: int = 0
        self._total_update_time: float = 0.0
        self._last_update_duration: float = 0.0
        self._max_update_duration: float = 0.0
        self._last_drift_check: float = 0.0

        self._param_change_log: Deque[Tuple[float, str, Any]] = deque(maxlen=20)
        self._initial_params: Dict[str, Any] = self.get_params()

        logger.info("[%s] UnbreakableRegimeMonitor v%s initialized", self.symbol, __version__)

    # ----- 公共方法 -----
    def update(self, context: Dict[str, Any]) -> MarketRegime:
        if not isinstance(context, dict):
            context = {}
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

            if self._current_regime != old_regime and self._on_regime_change:
                self._pending_callback = (old_regime, self._current_regime)

            if self.param_drift_detection:
                now = self._time_func()
                if now - self._last_drift_check > 30.0:  # 节流：每30s检查一次
                    self._check_param_drift()
                    self._last_drift_check = now

        self._execute_pending_callback()
        return self._current_regime

    async def get_regime(self, symbol: str, interval: str, klines: Any) -> MarketRegime:
        """
        兼容 context_pipeline 调用。
        防御非序列 klines，使用缓存指标 + 可选简单波动率估计。
        """
        ctx: Dict[str, Any] = {
            "symbol": symbol or self.symbol,
            "primary_interval": interval,
            "kma_slope": self._last_valid_kma_slope if self._last_valid_kma_slope is not None else 0.0,
            "adx": self._last_valid_adx if self._last_valid_adx is not None else 15.0,
            "bb_bandwidth_percentile": (
                self._last_valid_bb_percentile if self._last_valid_bb_percentile is not None else 50.0
            ),
            self.atr_short_key: self._last_valid_atr_short if self._last_valid_atr_short is not None else 1.0,
            self.atr_long_key: self._last_valid_atr_long if self._last_valid_atr_long is not None else 1.0,
        }
        try:
            if klines is not None:
                try:
                    length = len(klines)
                except TypeError:
                    length = 0
                if length >= 2:
                    closes = []
                    for k in list(klines)[-20:]:
                        c = getattr(k, "close", None)
                        if c is not None:
                            try:
                                fc = float(c)
                                if math.isfinite(fc) and fc > 0:
                                    closes.append(fc)
                            except (TypeError, ValueError):
                                continue
                    if len(closes) >= 2:
                        rets = []
                        for i in range(1, len(closes)):
                            if closes[i - 1] > 0:
                                rets.append(abs(closes[i] / closes[i - 1] - 1.0))
                        if rets:
                            avg_ret = sum(rets) / len(rets)
                            if avg_ret > 0.02:
                                ctx[self.atr_short_key] = max(float(ctx.get(self.atr_short_key, 1.0)), 2.0)
        except Exception:
            pass
        return self.update(ctx)

    def get_current_regime(self) -> MarketRegime:
        with self._lock:
            return self._current_regime

    def can_open_position(self, timeframe: str) -> bool:
        with self._lock:
            if self._current_regime == MarketRegime.HIGH_VOL and not self.allow_during_high_vol:
                return False
            if self._current_regime == MarketRegime.RANGE:
                return str(timeframe).lower() in ("5m", "3m", "15m")
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

    def set_params(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key) and key in (
                    "confirm_bars", "hysteresis_bars", "adx_threshold",
                    "kma_slope_threshold", "bb_bandwidth_percentile",
                    "high_vol_atr_ratio", "allow_during_high_vol"
                ):
                    old_val = getattr(self, key)
                    if key in ("confirm_bars", "hysteresis_bars"):
                        value = max(3 if key == "confirm_bars" else 5, int(value))
                    else:
                        try:
                            value = float(value) if key != "allow_during_high_vol" else bool(value)
                        except (TypeError, ValueError):
                            self._suppressed_log(logger.warning, "invalid_param_value",
                                                 f"[{self.symbol}] Invalid value for {key}: {value}")
                            continue
                    setattr(self, key, value)
                    self._param_change_log.append((self._time_func(), key, old_val))
                    logger.info("[%s] Param %s: %s -> %s", self.symbol, key, old_val, value)
                else:
                    self._suppressed_log(logger.warning, "invalid_param",
                                         f"[{self.symbol}] Unknown param: {key}")

    def get_params(self) -> Dict[str, Any]:
        with self._lock:
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
        with self._lock:
            if callable(func):
                self._time_func = func

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
                if not isinstance(data, dict):
                    raise ValueError("checkpoint data must be dict")
                regime_str = data.get("current_regime", "RANGE")
                self._current_regime = MarketRegime(regime_str)
                self._bars_in_current_regime = max(0, int(data.get("bars_in_current_regime", self.hysteresis_bars)))
                pending = data.get("pending_regime")
                self._pending_regime = MarketRegime(pending) if pending else None
                self._pending_counter = max(0, int(data.get("pending_counter", 0)))
                self._raw_history.clear()
                for r in data.get("raw_history", []):
                    try:
                        self._raw_history.append(MarketRegime(r))
                    except Exception:
                        continue
                self._state_history.clear()
                for r in data.get("state_history", []):
                    try:
                        self._state_history.append(MarketRegime(r))
                    except Exception:
                        continue
                self._last_valid_kma_slope = data.get("last_valid_kma_slope")
                self._last_valid_adx = data.get("last_valid_adx")
                self._last_valid_bb_percentile = data.get("last_valid_bb_percentile")
                self._last_valid_atr_short = data.get("last_valid_atr_short")
                self._last_valid_atr_long = data.get("last_valid_atr_long")
                self._update_count = max(0, int(data.get("update_count", 0)))
                # 强制参数边界
                self.confirm_bars = max(3, self.confirm_bars)
                self.hysteresis_bars = max(5, self.hysteresis_bars)
                logger.info("[%s] State restored from checkpoint", self.symbol)
            except Exception as e:
                logger.error("[%s] Failed to load checkpoint: %s, resetting", self.symbol, e)
                self.reset()

    # ----- 私有方法 -----
    def _execute_pending_callback(self) -> None:
        callback_info = None
        cb = None
        with self._lock:
            callback_info = self._pending_callback
            self._pending_callback = None
            cb = self._on_regime_change
        if callback_info and cb:
            old, new = callback_info
            try:
                cb(old, new)
            except Exception as e:
                logger.error("[%s] Callback error: %s", self.symbol, e)

    def _check_param_drift(self) -> None:
        current = {
            "confirm_bars": self.confirm_bars,
            "hysteresis_bars": self.hysteresis_bars,
            "adx_threshold": self.adx_threshold,
            "kma_slope_threshold": self.kma_slope_threshold,
            "bb_bandwidth_percentile": self.bb_bandwidth_percentile,
            "high_vol_atr_ratio": self.high_vol_atr_ratio,
            "allow_during_high_vol": self.allow_during_high_vol,
        }
        for k, v in current.items():
            init_v = self._initial_params.get(k)
            if init_v is None:
                continue
            try:
                if isinstance(v, (int, float)) and isinstance(init_v, (int, float)):
                    if abs(v - init_v) > max(abs(init_v) * 0.5, 1e-6):
                        self._suppressed_log(
                            logger.warning, f"param_drift_{k}",
                            f"[{self.symbol}] Param drift detected: {k} {init_v} -> {v}"
                        )
            except Exception:
                pass

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

        if atr_long is not None and atr_long > 1e-6 and atr_short is not None:
            vol_ratio = atr_short / max(atr_long, 1e-10)
            if vol_ratio > self.high_vol_atr_ratio:
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
            counts: Dict[MarketRegime, int] = {}
            for r in recent:
                counts[r] = counts.get(r, 0) + 1
            max_count = max(counts.values())
            candidates = [r for r, c in counts.items() if c == max_count]
            # 确定性平局处理：优先保持当前，其次按优先级
            if self._current_regime in candidates:
                voted_regime = self._current_regime
            else:
                candidates.sort(key=lambda r: (-_REGIME_PRIORITY.get(r, 0), r.value))
                voted_regime = candidates[0]
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

    def _suppressed_log(self, log_func, key: str, message: str) -> None:
        """实例级包装，key 自动加 symbol 前缀，避免多实例互相抑制。"""
        full_key = f"{self.symbol}:{key}"
        now = time.time()
        with self.__class__._suppression_lock:
            last_time, count = self.__class__._warning_suppression.get(full_key, (0.0, 0))
            if now - last_time > self.__class__.SUPPRESS_INTERVAL:
                count = 0
            if count < self.__class__.MAX_SUPPRESS_COUNT:
                log_func(message)
                self.__class__._warning_suppression[full_key] = (now, count + 1)
            if len(self.__class__._warning_suppression) > self.__class__.MAX_SUPPRESSION_KEYS:
                sorted_items = sorted(self.__class__._warning_suppression.items(), key=lambda x: x[1][0])
                for k, _ in sorted_items[:len(sorted_items) // 2]:
                    self.__class__._warning_suppression.pop(k, None)
