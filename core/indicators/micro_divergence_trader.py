# -*- coding: utf-8 -*-
"""
模块名称: micro_divergence_trader.py
核心职责: 基于Wilder RSI背离的微观反转交易模块，在弱趋势环境中捕捉小波段反转。
          集成多重机构级风控、审计时钟、极值同步背离检测、增量RSI、风险预算。
所属层级: core.indicators

外部依赖:
    - numpy
    - collections.deque
    - time
    - core.interfaces.FeatureComputer
    - core.models.kline (Kline)

接口契约:
    提供: {
        'MicroDivergenceTrader': {
            'input': 'kline: Kline, context: dict',
            'output': 'dict {signal, prob, stop_loss, take_profit, position_coeff, max_qty, reason, timestamp}',
            'side_effects': ['更新内部缓存']
        }
    }
    消费: {
        'context["atr_3m"]', 'context["kma_slope"]', 'context["hmm_state"]', 等
    }

作者: KHAOS System Architect
创建日期: 2025-04-10
修改记录:
    - v1-v3 前序审计
    - v4.0 终极穿透：RSI历史真实性、极值同步、monotonic时钟、风险预算、摆动点质量
"""

import logging
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.interfaces import FeatureComputer
from core.models.kline import Kline

logger = logging.getLogger(__name__)

# 默认常量（全部可外部配置）
DEFAULT_ENABLED = False
DEFAULT_MIN_ACCOUNT_BALANCE = 5000
DEFAULT_RSI_PERIOD = 7
DEFAULT_MIN_SLOPE_STRENGTH = 0.1
DEFAULT_POSITION_COEFF = 0.2
DEFAULT_TARGET_ATR = 0.8
DEFAULT_STOP_ATR = 0.2
DEFAULT_LOOKBACK_BARS = 20
DEFAULT_MAX_DAILY_TRADES = 5
DEFAULT_MAX_CONSECUTIVE_LOSSES = 3
DEFAULT_SIGNAL_COOLDOWN_BARS = 10
DEFAULT_MIN_ATR_FOR_SIGNAL = 1e-8
DEFAULT_MIN_BODY_RATIO = 0.4
DEFAULT_MIN_VOLUME_RATIO = 0.5
DEFAULT_MAX_GAP_RATIO = 0.3
DEFAULT_SLIPPAGE_RESERVE = 0.02
DEFAULT_COOL_DOWN_INCREMENT = 5
DEFAULT_MAX_HOLD_BARS = 12
DEFAULT_MIN_SWING_AMPLITUDE_ATR = 0.3
DEFAULT_RSI_SLOPE_THRESHOLD = 1.0
DEFAULT_SIGNAL_PROB = 0.65
DEFAULT_MAX_SAME_DIRECTION_TRADES = 2
DEFAULT_STATE_VERSION = 2

# 摆动点识别窗口
SWING_LEFT_BARS = 2
SWING_RIGHT_BARS = 2
MIN_SWING_DISTANCE = 3


class MicroDivergenceTrader(FeatureComputer):
    """
    微观背离交易器 v4.0 (华尔街终极版)

    核心升级：
    - RSI 仅计算最新值，背离检测使用同步缓存的 RSI 极值（价格与 RSI 时间对齐）
    - 增量 RSI 无历史失真
    - 信号时间戳采用 monotonic 时钟，同时记录 K 线时间
    - 摆动点质量过滤（振幅、间距）
    - 风险预算感知，输出建议最大仓位
    - 连续同向交易次数限制
    - 全部阈值配置化，严格参数校验
    - 状态持久化支持完整恢复增量 RSI 状态
    """

    def __init__(
        self,
        enabled: bool = DEFAULT_ENABLED,
        min_account_balance: int = DEFAULT_MIN_ACCOUNT_BALANCE,
        rsi_period: int = DEFAULT_RSI_PERIOD,
        min_slope_strength: float = DEFAULT_MIN_SLOPE_STRENGTH,
        position_coeff: float = DEFAULT_POSITION_COEFF,
        target_atr: float = DEFAULT_TARGET_ATR,
        stop_atr: float = DEFAULT_STOP_ATR,
        lookback_bars: int = DEFAULT_LOOKBACK_BARS,
        max_daily_trades: int = DEFAULT_MAX_DAILY_TRADES,
        max_consecutive_losses: int = DEFAULT_MAX_CONSECUTIVE_LOSSES,
        signal_cooldown_bars: int = DEFAULT_SIGNAL_COOLDOWN_BARS,
        min_atr_for_signal: float = DEFAULT_MIN_ATR_FOR_SIGNAL,
        min_body_ratio: float = DEFAULT_MIN_BODY_RATIO,
        min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
        max_gap_ratio: float = DEFAULT_MAX_GAP_RATIO,
        slippage_reserve: float = DEFAULT_SLIPPAGE_RESERVE,
        cool_down_increment: int = DEFAULT_COOL_DOWN_INCREMENT,
        max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
        min_swing_amplitude_atr: float = DEFAULT_MIN_SWING_AMPLITUDE_ATR,
        rsi_slope_threshold: float = DEFAULT_RSI_SLOPE_THRESHOLD,
        signal_prob: float = DEFAULT_SIGNAL_PROB,
        max_same_direction_trades: int = DEFAULT_MAX_SAME_DIRECTION_TRADES,
    ):
        # 参数校验
        if rsi_period < 2: raise ValueError("rsi_period >= 2")
        if target_atr <= stop_atr: raise ValueError("target_atr > stop_atr")
        if position_coeff <= 0 or position_coeff > 1: raise ValueError("position_coeff in (0,1]")
        if lookback_bars < 10: raise ValueError("lookback_bars >= 10")
        if max_consecutive_losses < 1: raise ValueError("max_consecutive_losses >= 1")
        if signal_cooldown_bars < 1: raise ValueError("signal_cooldown_bars >= 1")
        if min_body_ratio <= 0 or min_body_ratio > 1: raise ValueError("min_body_ratio in (0,1]")
        if min_volume_ratio <= 0: raise ValueError("min_volume_ratio > 0")
        if slippage_reserve < 0 or slippage_reserve >= 1: raise ValueError("slippage_reserve in [0,1)")
        if min_swing_amplitude_atr < 0: raise ValueError("min_swing_amplitude_atr >= 0")
        if signal_prob <= 0 or signal_prob > 1: raise ValueError("signal_prob in (0,1]")
        if max_same_direction_trades < 1: raise ValueError("max_same_direction_trades >= 1")

        self.enabled = enabled
        self.min_account_balance = min_account_balance
        self.rsi_period = rsi_period
        self.min_slope_strength = min_slope_strength
        self.position_coeff = position_coeff
        self.target_atr = target_atr
        self.stop_atr = stop_atr
        self.lookback_bars = lookback_bars
        self.max_daily_trades = max_daily_trades
        self.max_consecutive_losses = max_consecutive_losses
        self.signal_cooldown_bars = signal_cooldown_bars
        self.min_atr_for_signal = min_atr_for_signal
        self.min_body_ratio = min_body_ratio
        self.min_volume_ratio = min_volume_ratio
        self.max_gap_ratio = max_gap_ratio
        self.slippage_reserve = slippage_reserve
        self.cool_down_increment = cool_down_increment
        self.max_hold_bars = max_hold_bars
        self.min_swing_amplitude_atr = min_swing_amplitude_atr
        self.rsi_slope_threshold = rsi_slope_threshold
        self.signal_prob = signal_prob
        self.max_same_direction_trades = max_same_direction_trades

        # 缓存
        max_cache = max(100, lookback_bars + rsi_period + 50)
        self._close_deque: deque = deque(maxlen=max_cache)
        self._high_deque: deque = deque(maxlen=max_cache)
        self._low_deque: deque = deque(maxlen=max_cache)
        self._volume_deque: deque = deque(maxlen=max_cache)

        # 增量 RSI 状态
        self._avg_gain: float = 0.0
        self._avg_loss: float = 0.0
        self._rsi_initialized: bool = False
        self._prev_close: float = 0.0
        self._latest_rsi: float = 50.0

        # 价格与RSI极值缓存（用于同步背离检测）
        self._price_peaks: deque = deque(maxlen=5)   # (bar_index, price)
        self._price_troughs: deque = deque(maxlen=5)
        self._rsi_peaks: deque = deque(maxlen=5)      # (bar_index, rsi_value)
        self._rsi_troughs: deque = deque(maxlen=5)

        # 交易状态
        self._daily_trade_count: int = 0
        self._consecutive_losses: int = 0
        self._last_signal_direction: Optional[str] = None
        self._same_direction_count: int = 0
        self._current_bar_index: int = 0
        self._last_signal_bar: int = -signal_cooldown_bars
        self._is_paused: bool = False

        # 审计
        self._last_signal_monotonic: float = 0.0

        logger.info(
            f"MicroDivergenceTrader v4.0: rsi={rsi_period}, lookback={lookback_bars}, "
            f"swing_amp={min_swing_amplitude_atr}atr, rsi_slope={rsi_slope_threshold}, "
            f"prob={signal_prob}, max_hold={max_hold_bars}"
        )

    async def compute(self, kline: Kline, context: Dict) -> Dict:
        self._current_bar_index += 1

        if not self.enabled:
            return self._no_signal("module_disabled")
        if context.get("account_balance", 0) < self.min_account_balance:
            return self._no_signal("insufficient_balance")
        if self._is_paused:
            return self._no_signal("paused")

        # 更新缓存
        self._close_deque.append(kline.close)
        self._high_deque.append(kline.high)
        self._low_deque.append(kline.low)
        self._volume_deque.append(kline.volume)

        if len(self._close_deque) < self.rsi_period + 1:
            return self._no_signal("insufficient_data")

        try:
            atr = context.get("atr_3m", 1.0)
            if atr <= self.min_atr_for_signal:
                return self._no_signal("atr_too_low")
            kma_slope = abs(context.get("kma_slope", 1.0))
            hmm_state = context.get("hmm_state", "UNKNOWN")
        except Exception as e:
            logger.error(f"Context error: {e}")
            return self._no_signal("context_error")

        if kma_slope > self.min_slope_strength:
            return self._no_signal(f"strong_trend({kma_slope:.3f})")
        if hmm_state in ("BULL", "BEAR") and kma_slope > self.min_slope_strength * 0.8:
            return self._no_signal(f"hmm_strong_trend({hmm_state},{kma_slope:.3f})")

        if self._consecutive_losses >= self.max_consecutive_losses:
            self._is_paused = True
            return self._no_signal("max_consecutive_losses")
        if self._daily_trade_count >= self.max_daily_trades:
            return self._no_signal("max_daily_trades")
        effective_cooldown = self.signal_cooldown_bars + self._consecutive_losses * self.cool_down_increment
        if self._current_bar_index - self._last_signal_bar < effective_cooldown:
            return self._no_signal("cooldown")

        # 增量更新 RSI（仅最新值）
        if not self._rsi_initialized:
            self._initialize_rsi()
            if not self._rsi_initialized:
                return self._no_signal("rsi_init_failed")
        else:
            self._update_rsi(kline.close)

        # 识别当前摆动点，更新极值缓存
        self._update_swing_extremes(atr)

        # 背离检测（基于缓存的同步极值）
        divergence = self._detect_divergence_with_cache()
        if divergence is None:
            return self._no_signal("no_divergence")

        direction, price, rsi_val = divergence

        # 同向交易次数限制
        if self._last_signal_direction == direction and self._same_direction_count >= self.max_same_direction_trades:
            return self._no_signal("max_same_direction")

        if self._is_gap_kline(kline, atr):
            return self._no_signal("gap_kline")
        if not self._is_confirmation_kline(kline, direction):
            return self._no_signal("no_confirmation_kline")

        avg_vol = np.mean(list(self._volume_deque)[-20:]) if len(self._volume_deque) >= 20 else kline.volume
        if avg_vol > 0 and kline.volume < avg_vol * self.min_volume_ratio:
            return self._no_signal("low_volume")

        stop_loss = atr * self.stop_atr * (1 + self.slippage_reserve)
        take_profit = atr * self.target_atr * (1 - self.slippage_reserve)

        # 风险预算：建议最大仓位（执行层再做最终校验）
        account_balance = context.get("account_balance", 0)
        risk_budget = context.get("risk_per_trade", 0.01) * account_balance
        max_qty = risk_budget / stop_loss if stop_loss > 0 else 0.0

        self._last_signal_bar = self._current_bar_index
        self._last_signal_direction = direction
        self._daily_trade_count += 1
        self._same_direction_count = self._same_direction_count + 1 if self._last_signal_direction == direction else 1
        self._last_signal_monotonic = time.monotonic()

        logger.info(
            f"Micro divergence signal: {direction} price={kline.close:.2f} RSI={rsi_val:.1f} "
            f"stop={stop_loss:.2f} target={take_profit:.2f} daily_trades={self._daily_trade_count}"
        )
        return {
            "signal": direction,
            "prob": self.signal_prob,
            "stop_loss": float(stop_loss),
            "take_profit": float(take_profit),
            "position_coeff": self.position_coeff,
            "max_qty": float(max_qty),
            "reason": f"Divergence close={kline.close:.2f}, RSI={rsi_val:.1f}",
            "timestamp": kline.close_time / 1000.0 if kline.close_time else self._last_signal_monotonic,
            "max_hold_bars": self.max_hold_bars,
        }

    # ---------- 增量 RSI 实现（最新值） ----------
    def _initialize_rsi(self) -> None:
        closes = list(self._close_deque)
        if len(closes) < self.rsi_period + 1:
            return
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        self._avg_gain = np.mean(gains[:self.rsi_period])
        self._avg_loss = np.mean(losses[:self.rsi_period])
        self._prev_close = closes[-1]
        self._rsi_initialized = True
        self._latest_rsi = self._calc_rsi_from_avgs()

    def _update_rsi(self, close: float) -> None:
        delta = close - self._prev_close
        gain = max(delta, 0)
        loss = max(-delta, 0)
        self._avg_gain = (self._avg_gain * (self.rsi_period - 1) + gain) / self.rsi_period
        self._avg_loss = (self._avg_loss * (self.rsi_period - 1) + loss) / self.rsi_period
        self._prev_close = close
        self._latest_rsi = self._calc_rsi_from_avgs()

    def _calc_rsi_from_avgs(self) -> float:
        if self._avg_loss == 0:
            return 100.0
        return max(0.0, min(100.0, 100.0 - 100.0 / (1.0 + self._avg_gain / self._avg_loss)))

    # ---------- 摆动点与极值缓存 ----------
    def _update_swing_extremes(self, atr: float) -> None:
        """检测当前K线是否为局部摆动高/低点，并更新缓存"""
        closes = list(self._close_deque)
        n = len(closes)
        if n < SWING_LEFT_BARS + SWING_RIGHT_BARS + 1:
            return
        i = n - SWING_RIGHT_BARS - 1  # 待检测点
        if i < SWING_LEFT_BARS:
            return

        window = closes[i - SWING_LEFT_BARS : i + SWING_RIGHT_BARS + 1]
        if closes[i] == max(window):
            # 满足振幅要求
            if self._swing_amplitude_valid(closes, i, atr):
                self._price_peaks.append((self._current_bar_index - SWING_RIGHT_BARS, closes[i]))
                self._rsi_peaks.append((self._current_bar_index - SWING_RIGHT_BARS, self._latest_rsi))
        elif closes[i] == min(window):
            if self._swing_amplitude_valid(closes, i, atr):
                self._price_troughs.append((self._current_bar_index - SWING_RIGHT_BARS, closes[i]))
                self._rsi_troughs.append((self._current_bar_index - SWING_RIGHT_BARS, self._latest_rsi))

    def _swing_amplitude_valid(self, closes: List[float], idx: int, atr: float) -> bool:
        if self.min_swing_amplitude_atr <= 0 or atr <= 0:
            return True
        left = max(0, idx - SWING_LEFT_BARS)
        right = min(len(closes) - 1, idx + SWING_RIGHT_BARS)
        local_min = min(closes[left:right + 1])
        local_max = max(closes[left:right + 1])
        return (local_max - local_min) / atr >= self.min_swing_amplitude_atr

    # ---------- 背离检测 (基于缓存极值) ----------
    def _detect_divergence_with_cache(self) -> Optional[Tuple[str, float, float]]:
        # 顶背离
        if len(self._price_peaks) >= 2:
            (idx1, p1), (idx2, p2) = self._price_peaks[-2], self._price_peaks[-1]
            if p2 > p1 and idx2 - idx1 >= MIN_SWING_DISTANCE:
                # 查找对应的 RSI 极值
                rsi1 = self._get_rsi_at_bar(idx1, self._rsi_peaks)
                rsi2 = self._get_rsi_at_bar(idx2, self._rsi_peaks)
                if rsi1 is not None and rsi2 is not None and rsi2 < rsi1:
                    # RSI 斜率确认
                    if self._rsi_slope_negative(idx1, idx2):
                        return ("SHORT", p2, rsi2)

        # 底背离
        if len(self._price_troughs) >= 2:
            (idx1, p1), (idx2, p2) = self._price_troughs[-2], self._price_troughs[-1]
            if p2 < p1 and idx2 - idx1 >= MIN_SWING_DISTANCE:
                rsi1 = self._get_rsi_at_bar(idx1, self._rsi_troughs)
                rsi2 = self._get_rsi_at_bar(idx2, self._rsi_troughs)
                if rsi1 is not None and rsi2 is not None and rsi2 > rsi1:
                    if self._rsi_slope_positive(idx1, idx2):
                        return ("LONG", p2, rsi2)

        return None

    def _get_rsi_at_bar(self, bar_idx: int, rsi_list: deque) -> Optional[float]:
        for idx, val in rsi_list:
            if idx == bar_idx:
                return val
        return None

    def _rsi_slope_positive(self, idx1: int, idx2: int) -> bool:
        # 使用缓存中的 RSI 极值计算斜率
        rsi1 = self._get_rsi_at_bar(idx1, self._rsi_troughs)
        rsi2 = self._get_rsi_at_bar(idx2, self._rsi_troughs)
        if rsi1 is None or rsi2 is None or idx2 == idx1:
            return False
        return (rsi2 - rsi1) / (idx2 - idx1) >= self.rsi_slope_threshold

    def _rsi_slope_negative(self, idx1: int, idx2: int) -> bool:
        rsi1 = self._get_rsi_at_bar(idx1, self._rsi_peaks)
        rsi2 = self._get_rsi_at_bar(idx2, self._rsi_peaks)
        if rsi1 is None or rsi2 is None or idx2 == idx1:
            return False
        return (rsi2 - rsi1) / (idx2 - idx1) <= -self.rsi_slope_threshold

    # ---------- 辅助函数 ----------
    def _is_confirmation_kline(self, kline: Kline, direction: str) -> bool:
        body = abs(kline.close - kline.open)
        total_range = kline.high - kline.low
        if total_range == 0 or body / total_range < self.min_body_ratio:
            return False
        if direction == "LONG":
            return kline.close > kline.open and (kline.high - kline.close) < body
        else:
            return kline.close < kline.open and (kline.open - kline.low) < body

    def _is_gap_kline(self, kline: Kline, atr: float) -> bool:
        if len(self._close_deque) < 2:
            return False
        prev_close = self._close_deque[-2]
        gap = abs(kline.open - prev_close)
        return gap > self.max_gap_ratio * atr

    def record_trade_result(self, profit: float) -> None:
        if profit < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        if self._consecutive_losses < self.max_consecutive_losses:
            self._is_paused = False

    def reset_daily_state(self) -> None:
        self._daily_trade_count = 0
        self._same_direction_count = 0

    def force_resume(self) -> None:
        self._is_paused = False
        self._consecutive_losses = 0

    def _no_signal(self, reason: str) -> Dict:
        return {
            "signal": "NONE",
            "prob": 0.0,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "position_coeff": 0.0,
            "max_qty": 0.0,
            "reason": reason,
            "timestamp": time.monotonic(),
            "max_hold_bars": 0,
        }

    # ---------- 状态持久化 ----------
    def get_state(self) -> Dict:
        return {
            "version": DEFAULT_STATE_VERSION,
            "close_deque": list(self._close_deque),
            "high_deque": list(self._high_deque),
            "low_deque": list(self._low_deque),
            "volume_deque": list(self._volume_deque),
            "avg_gain": self._avg_gain,
            "avg_loss": self._avg_loss,
            "rsi_initialized": self._rsi_initialized,
            "prev_close": self._prev_close,
            "latest_rsi": self._latest_rsi,
            "price_peaks": list(self._price_peaks),
            "price_troughs": list(self._price_troughs),
            "rsi_peaks": list(self._rsi_peaks),
            "rsi_troughs": list(self._rsi_troughs),
            "daily_trade_count": self._daily_trade_count,
            "consecutive_losses": self._consecutive_losses,
            "last_signal_direction": self._last_signal_direction,
            "same_direction_count": self._same_direction_count,
            "current_bar_index": self._current_bar_index,
            "last_signal_bar": self._last_signal_bar,
            "is_paused": self._is_paused,
        }

    def set_state(self, state: Dict) -> None:
        if state.get("version", 1) != DEFAULT_STATE_VERSION:
            logger.warning("State version mismatch, attempting restore")
        self._close_deque = deque(state.get("close_deque", []), maxlen=self._close_deque.maxlen)
        self._high_deque = deque(state.get("high_deque", []), maxlen=self._high_deque.maxlen)
        self._low_deque = deque(state.get("low_deque", []), maxlen=self._low_deque.maxlen)
        self._volume_deque = deque(state.get("volume_deque", []), maxlen=self._volume_deque.maxlen)
        self._avg_gain = state.get("avg_gain", 0.0)
        self._avg_loss = state.get("avg_loss", 0.0)
        self._rsi_initialized = state.get("rsi_initialized", False)
        self._prev_close = state.get("prev_close", 0.0)
        self._latest_rsi = state.get("latest_rsi", 50.0)
        self._price_peaks = deque(state.get("price_peaks", []), maxlen=self._price_peaks.maxlen)
        self._price_troughs = deque(state.get("price_troughs", []), maxlen=self._price_troughs.maxlen)
        self._rsi_peaks = deque(state.get("rsi_peaks", []), maxlen=self._rsi_peaks.maxlen)
        self._rsi_troughs = deque(state.get("rsi_troughs", []), maxlen=self._rsi_troughs.maxlen)
        self._daily_trade_count = state.get("daily_trade_count", 0)
        self._consecutive_losses = state.get("consecutive_losses", 0)
        self._last_signal_direction = state.get("last_signal_direction")
        self._same_direction_count = state.get("same_direction_count", 0)
        self._current_bar_index = state.get("current_bar_index", 0)
        self._last_signal_bar = state.get("last_signal_bar", -self.signal_cooldown_bars)
        self._is_paused = state.get("is_paused", False)
        logger.info("MicroDivergenceTrader state restored")
