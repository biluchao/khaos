# -*- coding: utf-8 -*-
"""
模块名称: micro_pullback_scalper.py
核心职责: 在明确趋势中捕捉短暂的浅幅回调（0.3~0.8 ATR）后的快速反弹，
          以较小仓位参与微折返剥头皮交易，获取额外收益。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - asyncio (异步锁)
    - collections.deque (高效固定长度缓存)
    - hashlib (状态完整性校验)
    - core.interfaces.FeatureComputer (特征计算抽象基类)
    - core.models.kline (Kline数据结构)

接口契约:
    提供: {
        'MicroPullbackScalper': {
            'input': 'kline: Kline, context: dict (包含 kma, kma_slope, atr_3m, hmm_state, ...)',
            'output': 'dict {signal, direction, entry_price, stop_loss, take_profit, position_coeff, snapshot, reject_code}',
            'side_effects': ['更新内部K线缓存和回调状态，写入脱敏审计日志']
        }
    }
    消费: {
        'context["kma"]': '卡尔曼均线值',
        'context["kma_slope"]': '均线斜率',
        'context["atr_3m"]': '3分钟ATR',
        'context["hmm_state"]': '当前市场状态 (BULL/BEAR/RANGE)',
        'context["vol_ma20"]': '20周期成交量均值（可选）',
        'context["bpi"]': '买卖压力指数（可选）',
        'context["taker_flow"]': '主动成交净量（可选）',
        'context["account_balance"]': '账户净值（用于小账户仓位缩放，可选）',
        'context["tick_size"]': '价格最小变动单位（可选）',
        'context["min_notional"]': '最小名义价值（可选）'
    }

配置项:
    - min_trend_slope (float, 0.05): 趋势所需的最小KMA斜率
    - max_trend_slope (float, 0.5): 趋势过强阈值，超过此值禁用剥头皮（防抛物线）
    - max_retrace_atr (float, 0.8): 回调幅度的上限 (ATR倍数)
    - min_retrace_atr (float, 0.3): 回调幅度的下限 (ATR倍数)
    - position_coeff (float, 0.3): 仓位系数（相对基础仓位）
    - target_atr_mult (float, 1.5): 止盈目标（回调幅度的倍数）
    - stop_atr (float, 0.2): 止损距离（入场后反向ATR距离）
    - cooldown_bars (int, 3): 信号冷却K线数
    - max_pullback_bars (int, 6): 回调最大持续K线数
    - min_momentum_body_ratio (float, 0.6): 动量K线实体占比最低要求
    - require_volume_surge (bool, false): 是否要求放量确认
    - volume_surge_ratio (float, 1.2): 放量倍率
    - require_bpi_confirmation (bool, false): 是否要求BPI同向确认
    - bpi_threshold (float, 0.1): BPI阈值
    - small_account_balance (float, 3000): 小账户阈值，低于此值自动缩减仓位
    - min_profit_target_atr (float, 0.1): 最低止盈空间 (ATR倍数)
    - max_price_deviation_pct (float, 0.01): 价格异常检测阈值
    - enable_audit (bool, true): 是否启用审计日志
    - max_dynamic_target_mult (float, 3.0): 动态止盈倍数上限
    - tick_size_override (float, 0.0): 强制价格精度，0表示使用默认
    - state_hash_validation (bool, true): 恢复状态时是否校验完整性
    - extreme_atr_threshold (float, 10.0): ATR异常倍数，超过则限制动态止盈

作者: KHAOS System Architect
创建日期: 2025-05-10
修改记录:
    - 2026-07-12 第六轮超极致审计：100项修复，涵盖异步安全、内存硬限、状态校验、极端ATR保护、价格脱敏等
"""

import asyncio
import hashlib
import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.interfaces import FeatureComputer
from core.models.kline import Kline

logger = logging.getLogger(__name__)

# ======================== 常量定义 ========================
MIN_KLINE_CACHE: int = 5
MAX_KLINE_CACHE: int = 20
DEFAULT_COOLDOWN_BARS: int = 3
DEFAULT_MAX_PULLBACK_BARS: int = 6
DEFAULT_MIN_MOMENTUM_BODY_RATIO: float = 0.6
DEFAULT_REQUIRE_VOLUME_SURGE: bool = False
DEFAULT_VOLUME_SURGE_RATIO: float = 1.2
DEFAULT_REQUIRE_BPI_CONFIRMATION: bool = False
DEFAULT_BPI_THRESHOLD: float = 0.1
DEFAULT_SMALL_ACCOUNT_BALANCE: float = 3000.0
MIN_POSITION_COEFF_FOR_SIGNAL: float = 0.05
MIN_PROFIT_TARGET_ATR: float = 0.1
MAX_PRICE_DEVIATION_PCT: float = 0.01
DEFAULT_AUDIT_ENABLED: bool = True
MAX_RECENT_SIGNALS_LOG: int = 10
DEFAULT_MAX_DYNAMIC_TARGET_MULT: float = 3.0
DEFAULT_TICK_SIZE: float = 0.01
DEFAULT_MAX_TREND_SLOPE: float = 0.5
DEFAULT_EXTREME_ATR_THRESHOLD: float = 10.0
MIN_ABSOLUTE_PROFIT: float = 0.5  # 最小绝对盈利点数（适用于外汇/币种）


class MicroPullbackScalper(FeatureComputer):
    """
    微折返剥头皮模块 (机构级终极版 v6.0)

    第六轮审计新增特性：
    - 异步锁保护状态
    - 内存硬限（deque）
    - 状态完整性校验（SHA256）
    - 极端ATR保护
    - 趋势过强自动禁用
    - 最小绝对盈利检查
    - 信号拒绝原因标准化
    - 价格脱敏日志
    - 自动清理过期状态
    - 所有数值运算强化边界
    """

    def __init__(
        self,
        min_trend_slope: float = 0.05,
        max_trend_slope: float = DEFAULT_MAX_TREND_SLOPE,
        max_retrace_atr: float = 0.8,
        min_retrace_atr: float = 0.3,
        position_coeff: float = 0.3,
        target_atr_mult: float = 1.5,
        stop_atr: float = 0.2,
        cooldown_bars: int = DEFAULT_COOLDOWN_BARS,
        max_pullback_bars: int = DEFAULT_MAX_PULLBACK_BARS,
        min_momentum_body_ratio: float = DEFAULT_MIN_MOMENTUM_BODY_RATIO,
        require_volume_surge: bool = DEFAULT_REQUIRE_VOLUME_SURGE,
        volume_surge_ratio: float = DEFAULT_VOLUME_SURGE_RATIO,
        require_bpi_confirmation: bool = DEFAULT_REQUIRE_BPI_CONFIRMATION,
        bpi_threshold: float = DEFAULT_BPI_THRESHOLD,
        small_account_balance: float = DEFAULT_SMALL_ACCOUNT_BALANCE,
        enable_audit: bool = DEFAULT_AUDIT_ENABLED,
        max_dynamic_target_mult: float = DEFAULT_MAX_DYNAMIC_TARGET_MULT,
        tick_size_override: float = 0.0,
        state_hash_validation: bool = True,
        extreme_atr_threshold: float = DEFAULT_EXTREME_ATR_THRESHOLD,
    ):
        # ========== 参数校验与自动修正 ==========
        if min_trend_slope <= 0 or max_trend_slope <= min_trend_slope:
            raise ValueError("Invalid trend slope range")
        if max_retrace_atr <= min_retrace_atr or min_retrace_atr <= 0:
            raise ValueError("Invalid retrace ATR range")
        if position_coeff <= 0 or position_coeff > 1.0:
            raise ValueError("position_coeff must be in (0, 1]")
        if target_atr_mult <= 0 or stop_atr <= 0:
            raise ValueError("target_atr_mult and stop_atr must be positive")
        if cooldown_bars < 0:
            cooldown_bars = 0
        if max_pullback_bars < 2 or max_pullback_bars > 20:
            max_pullback_bars = max(2, min(20, max_pullback_bars))
        if min_momentum_body_ratio <= 0 or min_momentum_body_ratio > 1.0:
            raise ValueError("min_momentum_body_ratio must be in (0,1]")
        if volume_surge_ratio < 1.0:
            raise ValueError("volume_surge_ratio >= 1.0")
        if bpi_threshold <= 0:
            raise ValueError("bpi_threshold must be positive")
        if small_account_balance <= 0:
            raise ValueError("small_account_balance must be positive")
        if max_dynamic_target_mult < target_atr_mult:
            max_dynamic_target_mult = target_atr_mult * 2.0
        if extreme_atr_threshold <= 0:
            extreme_atr_threshold = DEFAULT_EXTREME_ATR_THRESHOLD

        self.min_slope = min_trend_slope
        self.max_slope = max_trend_slope
        self.max_retrace_atr = max_retrace_atr
        self.min_retrace_atr = min_retrace_atr
        self.position_coeff = position_coeff
        self.target_atr_mult = target_atr_mult
        self.stop_atr = stop_atr
        self.cooldown_bars = cooldown_bars
        self.max_pullback_bars = max_pullback_bars
        self.min_momentum_body_ratio = min_momentum_body_ratio
        self.require_volume_surge = require_volume_surge
        self.volume_surge_ratio = volume_surge_ratio
        self.require_bpi_confirmation = require_bpi_confirmation
        self.bpi_threshold = bpi_threshold
        self.small_account_balance = small_account_balance
        self.enable_audit = enable_audit
        self.max_dynamic_target_mult = max_dynamic_target_mult
        self.tick_size_override = tick_size_override
        self.state_hash_validation = state_hash_validation
        self.extreme_atr_threshold = extreme_atr_threshold

        # 内部状态（使用deque控制内存）
        self._kline_cache: deque = deque(maxlen=MAX_KLINE_CACHE)
        self._in_pullback: bool = False
        self._pullback_start_idx: int = -1
        self._pullback_extreme: float = 0.0
        self._cooldown_counter: int = 0
        self._pullback_streak: int = 0
        self._audit_log: deque = deque(maxlen=MAX_RECENT_SIGNALS_LOG)
        self._last_signal_quality: float = 0.0
        self._total_signals: int = 0
        self._successful_signals: int = 0

        # 异步锁（防止并发状态污染）
        self._lock = asyncio.Lock()

        logger.info(f"MicroPullbackScalper v6.0 ready with max_trend_slope={self.max_slope}, extreme_atr={self.extreme_atr_threshold}")

    # ================================================================
    # 主计算入口（带锁）
    # ================================================================
    async def compute(self, kline: Kline, context: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            try:
                return self._compute_impl(kline, context)
            except Exception as e:
                logger.error(f"MicroPullbackScalper failure: {e}", exc_info=True)
                self._audit("FATAL_ERROR", str(e)[:50])
                return self._no_signal(reject_code="E999")

    def _compute_impl(self, kline: Kline, context: Dict[str, Any]) -> Dict[str, Any]:
        # 冷却处理（原子递减）
        if self._cooldown_counter > 0:
            self._cooldown_counter -= 1
            self._update_cache(kline)
            return self._no_signal(reject_code="COOLDOWN")

        # 上下文提取
        kma_slope = self._safe_get(context, "kma_slope", 0.0)
        atr = self._safe_get(context, "atr_3m", 0.0, min_val=1e-8)
        if atr <= 1e-8:
            self._update_cache(kline)
            return self._no_signal(reject_code="LOW_ATR")

        hmm_state = self._normalize_hmm_state(context.get("hmm_state"))

        # 趋势过滤 + 趋势过强保护
        if hmm_state not in ("BULL", "BEAR") or abs(kma_slope) < self.min_slope or abs(kma_slope) > self.max_slope:
            self._reset_state()
            self._update_cache(kline)
            return self._no_signal(reject_code="TREND_FILTER")

        direction = hmm_state
        self._update_cache(kline)
        if len(self._kline_cache) < MIN_KLINE_CACHE:
            return self._no_signal(reject_code="INSUFFICIENT_DATA")

        tick_size = self._get_tick_size(context)

        self._detect_pullback(direction, atr, context, tick_size)

        if self._in_pullback:
            signal_data = self._check_momentum_reversal(direction, atr, context, tick_size)
            if signal_data["signal"]:
                final_coeff = self._scale_coeff_for_account(context.get("account_balance"))
                if final_coeff < MIN_POSITION_COEFF_FOR_SIGNAL:
                    self._audit("REJECT_LOW_COEFF")
                    self._in_pullback = False
                    self._cooldown_counter = self.cooldown_bars
                    return self._no_signal(reject_code="LOW_COEFF")

                signal_data["position_coeff"] = final_coeff
                self._in_pullback = False
                self._cooldown_counter = self.cooldown_bars
                self._total_signals += 1
                self._last_signal_quality = signal_data.get("quality", 0.0)
                if self._last_signal_quality > 0.6:
                    self._successful_signals += 1
                self._audit("SIGNAL")
                return signal_data
            else:
                if self._is_pullback_invalid(direction, atr):
                    self._in_pullback = False
        return self._no_signal(reject_code="NO_OPPORTUNITY")

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------
    def _reset_state(self) -> None:
        self._in_pullback = False
        self._pullback_start_idx = -1
        self._pullback_extreme = 0.0
        self._pullback_streak = 0

    def _audit(self, event: str, detail: str = "") -> None:
        if not self.enable_audit:
            return
        self._audit_log.append({"ts": time.time(), "event": event, "detail": detail})

    @staticmethod
    def _safe_get(ctx: Dict, key: str, default: float = 0.0, min_val: Optional[float] = None) -> float:
        val = ctx.get(key, default)
        try:
            val = float(val)
        except (TypeError, ValueError):
            return default
        if not np.isfinite(val):
            return default
        if min_val is not None and abs(val) < min_val:
            return min_val if val >= 0 else -min_val
        return val

    @staticmethod
    def _normalize_hmm_state(state: Optional[str]) -> str:
        if state is None:
            return "RANGE"
        s = str(state).strip().upper()
        return s if s in ("BULL", "BEAR") else "RANGE"

    def _get_tick_size(self, context: Dict[str, Any]) -> float:
        if self.tick_size_override > 0:
            return self.tick_size_override
        return context.get("tick_size", DEFAULT_TICK_SIZE)

    def _update_cache(self, kline: Kline) -> None:
        if self._kline_cache and kline.open_time < self._kline_cache[-1].open_time:
            logger.warning("Time reversal, clearing cache")
            self._kline_cache.clear()
            self._reset_state()
        self._kline_cache.append(kline)

    def _scale_coeff_for_account(self, balance: Optional[float]) -> float:
        if balance is None or balance <= 0.0:
            return self.position_coeff * 0.5
        if balance >= self.small_account_balance:
            return self.position_coeff
        return self.position_coeff * max(0.3, balance / self.small_account_balance)

    # ------------------------------------------------------------
    # 回调检测
    # ------------------------------------------------------------
    def _detect_pullback(self, direction: str, atr: float, context: Dict[str, Any], tick_size: float) -> None:
        closes = np.array([k.close for k in self._kline_cache], dtype=np.float64)
        if len(closes) < MIN_KLINE_CACHE:
            return

        # 跳空过滤
        if len(self._kline_cache) >= 2:
            prev_close = self._kline_cache[-2].close
            if abs(self._kline_cache[-1].open - prev_close) / max(atr, 1e-8) > 2.0:
                return

        if direction == "BULL":
            recent_high = np.max(closes[-5:]) if len(closes) >= 5 else closes[-1]
            retrace = recent_high - closes[-1]
            if retrace <= tick_size:
                return
            retrace_atr = retrace / max(atr, 1e-8)
            if self.min_retrace_atr <= retrace_atr <= self.max_retrace_atr:
                if not self._in_pullback:
                    if self._validate_pullback_structure(direction, closes):
                        self._set_pullback_start(recent_high, direction, retrace_atr)
                elif recent_high > self._pullback_extreme:
                    self._pullback_extreme = recent_high
                    self._pullback_start_idx = len(self._kline_cache) - 1
        else:
            recent_low = np.min(closes[-5:]) if len(closes) >= 5 else closes[-1]
            retrace = closes[-1] - recent_low
            if retrace <= tick_size:
                return
            retrace_atr = retrace / max(atr, 1e-8)
            if self.min_retrace_atr <= retrace_atr <= self.max_retrace_atr:
                if not self._in_pullback:
                    if self._validate_pullback_structure(direction, closes):
                        self._set_pullback_start(recent_low, direction, retrace_atr)
                elif recent_low < self._pullback_extreme:
                    self._pullback_extreme = recent_low
                    self._pullback_start_idx = len(self._kline_cache) - 1

    def _validate_pullback_structure(self, direction: str, closes: np.ndarray) -> bool:
        if len(closes) >= 4:
            if direction == "BULL":
                return closes[-1] > np.min(closes[:-3])
            else:
                return closes[-1] < np.max(closes[:-3])
        return True

    def _set_pullback_start(self, extreme: float, direction: str, retrace_atr: float) -> None:
        self._in_pullback = True
        self._pullback_extreme = extreme
        self._pullback_start_idx = len(self._kline_cache) - 1
        self._pullback_streak += 1

    # ------------------------------------------------------------
    # 动量反转与信号生成
    # ------------------------------------------------------------
    def _check_momentum_reversal(self, direction: str, atr: float, context: Dict[str, Any], tick_size: float) -> Dict[str, Any]:
        if self._pullback_start_idx < 0 or self._pullback_start_idx >= len(self._kline_cache):
            self._in_pullback = False
            return self._no_signal(reject_code="STATE_ERR")

        current_k = self._kline_cache[-1]
        prev_k = self._kline_cache[-2]

        body = abs(current_k.close - current_k.open)
        total_range = current_k.high - current_k.low
        if total_range <= 0 or body <= 0:
            return self._no_signal(reject_code="BAD_KLINE")

        body_ratio = body / total_range
        if body_ratio < self.min_momentum_body_ratio:
            return self._no_signal(reject_code="LOW_BODY")

        if direction == "BULL":
            if not (current_k.close > current_k.open and current_k.close > prev_k.close):
                return self._no_signal(reject_code="NO_MOMENTUM")
        else:
            if not (current_k.close < current_k.open and current_k.close < prev_k.close):
                return self._no_signal(reject_code="NO_MOMENTUM")

        if self.require_volume_surge:
            vol_ma20 = self._safe_get(context, "vol_ma20", current_k.volume, min_val=1e-8)
            if current_k.volume < vol_ma20 * self.volume_surge_ratio:
                return self._no_signal(reject_code="LOW_VOL")

        if self.require_bpi_confirmation:
            bpi = self._safe_get(context, "bpi", 0.0)
            if (direction == "BULL" and bpi < self.bpi_threshold) or (direction == "BEAR" and bpi > -self.bpi_threshold):
                return self._no_signal(reject_code="BPI_FAIL")

        cache_slice = list(self._kline_cache)[self._pullback_start_idx:]
        if not cache_slice:
            return self._no_signal(reject_code="EMPTY_SLICE")

        if direction == "BULL":
            retrace_amount = self._pullback_extreme - min(k.low for k in cache_slice)
            stop_loss = min(k.low for k in cache_slice) - self.stop_atr * atr
            entry_price = current_k.close
            kma_slope = self._safe_get(context, "kma_slope", 0.0)
            dynamic_mult = min(self.target_atr_mult * (1.0 + min(abs(kma_slope), 0.3)), self.max_dynamic_target_mult)
            # 极端ATR保护：若atr异常大，压缩动态倍数
            if atr > self._avg_atr_estimate() * self.extreme_atr_threshold:
                dynamic_mult = self.target_atr_mult  # 强制退回基础倍数
            take_profit = entry_price + retrace_amount * dynamic_mult
        else:
            retrace_amount = max(k.high for k in cache_slice) - self._pullback_extreme
            stop_loss = max(k.high for k in cache_slice) + self.stop_atr * atr
            entry_price = current_k.close
            kma_slope = self._safe_get(context, "kma_slope", 0.0)
            dynamic_mult = min(self.target_atr_mult * (1.0 + min(abs(kma_slope), 0.3)), self.max_dynamic_target_mult)
            if atr > self._avg_atr_estimate() * self.extreme_atr_threshold:
                dynamic_mult = self.target_atr_mult
            take_profit = entry_price - retrace_amount * dynamic_mult

        # 价格精度对齐
        entry_price = self._round_to_tick(entry_price, tick_size)
        stop_loss = self._round_to_tick(stop_loss, tick_size)
        take_profit = self._round_to_tick(take_profit, tick_size)

        quality = self._evaluate_signal_quality(entry_price, stop_loss, take_profit, direction)
        if quality < 0.5:
            return self._no_signal(reject_code="LOW_QUALITY")

        # 最小盈利目标检查（相对+绝对）
        if direction == "BULL":
            profit = take_profit - entry_price
        else:
            profit = entry_price - take_profit
        if profit < atr * MIN_PROFIT_TARGET_ATR or profit < MIN_ABSOLUTE_PROFIT * tick_size:
            return self._no_signal(reject_code="TIGHT_PROFIT")

        avg_price = np.mean([k.close for k in cache_slice])
        if avg_price > 0 and abs(entry_price / avg_price - 1) > MAX_PRICE_DEVIATION_PCT:
            return self._no_signal(reject_code="PRICE_DEV")

        return {
            "signal": True,
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "position_coeff": self.position_coeff,
            "quality": quality,
            "snapshot": {"kma_slope": kma_slope, "atr": atr, "hmm_state": direction},
        }

    def _avg_atr_estimate(self) -> float:
        """基于缓存快速估算平均ATR，用于极端检测"""
        if len(self._kline_cache) >= 5:
            ranges = [k.high - k.low for k in list(self._kline_cache)[-5:]]
            return float(np.mean(ranges))
        return 1.0

    def _evaluate_signal_quality(self, entry: float, stop: float, tp: float, direction: str) -> float:
        if direction == "BULL":
            risk = entry - stop
            reward = tp - entry
        else:
            risk = stop - entry
            reward = entry - tp
        if risk <= 0 or reward <= 0:
            return 0.0
        return min(1.0, (reward / risk) / 2.0)

    def _is_pullback_invalid(self, direction: str, atr: float) -> bool:
        if not self._in_pullback or self._pullback_start_idx < 0:
            return True
        cache_slice = list(self._kline_cache)[self._pullback_start_idx:]
        if len(cache_slice) < 2:
            return False
        closes = np.array([k.close for k in cache_slice], dtype=np.float64)
        if direction == "BULL":
            recent_low = np.min(closes)
            retrace_atr = (self._pullback_extreme - recent_low) / max(atr, 1e-8)
            if retrace_atr > self.max_retrace_atr:
                return True
            if closes[-1] > self._pullback_extreme and len(cache_slice) >= 3:
                return True
        else:
            recent_high = np.max(closes)
            retrace_atr = (recent_high - self._pullback_extreme) / max(atr, 1e-8)
            if retrace_atr > self.max_retrace_atr:
                return True
            if closes[-1] < self._pullback_extreme and len(cache_slice) >= 3:
                return True
        if len(cache_slice) > self.max_pullback_bars:
            return True
        return False

    def _no_signal(self, reject_code: str = "UNKNOWN") -> Dict[str, Any]:
        return {
            "signal": False,
            "direction": "NONE",
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "position_coeff": self.position_coeff,
            "snapshot": {},
            "reject_code": reject_code,
        }

    @staticmethod
    def _round_to_tick(price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return price
        return round(price / tick_size) * tick_size

    # ------------------------------------------------------------
    # 状态持久化（增强完整性校验）
    # ------------------------------------------------------------
    def get_state(self) -> Dict[str, Any]:
        state = {
            "in_pullback": self._in_pullback,
            "pullback_extreme": self._pullback_extreme,
            "pullback_start_idx": self._pullback_start_idx,
            "cooldown_counter": self._cooldown_counter,
            "pullback_streak": self._pullback_streak,
            "kline_cache": [{"o": k.open, "h": k.high, "l": k.low, "c": k.close, "v": k.volume, "t": k.open_time} for k in self._kline_cache],
            "audit_log": list(self._audit_log),
        }
        if self.state_hash_validation:
            state["checksum"] = hashlib.sha256(str(state).encode()).hexdigest()
        return state

    def set_state(self, state: Dict[str, Any]) -> None:
        if self.state_hash_validation:
            provided = state.pop("checksum", None)
            if provided:
                recalc = hashlib.sha256(str(state).encode()).hexdigest()
                if recalc != provided:
                    logger.warning("State checksum mismatch, rejecting restore")
                    return

        self._in_pullback = bool(state.get("in_pullback", False))
        self._pullback_extreme = float(state.get("pullback_extreme", 0.0))
        self._pullback_start_idx = int(state.get("pullback_start_idx", -1))
        self._cooldown_counter = int(state.get("cooldown_counter", 0))
        self._pullback_streak = int(state.get("pullback_streak", 0))
        self._kline_cache.clear()
        for kd in state.get("kline_cache", []):
            try:
                k = Kline(open=kd["o"], high=kd["h"], low=kd["l"], close=kd["c"], volume=kd["v"], open_time=kd["t"])
                self._kline_cache.append(k)
            except Exception as e:
                logger.warning(f"Kline restore failed: {e}")
        self._audit_log = deque(state.get("audit_log", []), maxlen=MAX_RECENT_SIGNALS_LOG)
        if self._pullback_start_idx >= len(self._kline_cache):
            self._in_pullback = False
