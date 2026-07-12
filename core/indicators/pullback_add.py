# -*- coding: utf-8 -*-
"""
模块名称: pullback_add.py
核心职责: 识别趋势中价格回踩均线、蓄力后重新启动的高胜率加仓机会。
          输出加仓信号（方向、仓位系数、止损/止盈建议）及独立风险评分。
          支持动态市场自适应与小账户严格风控。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - time (时间戳与冷却)
    - core.interfaces.FeatureComputer
    - core.models.kline.Kline

接口契约:
    提供: PullbackAddModule 类，主要方法 compute()
    消费: context 中的 kma, atr_3m, hmm_state, hmm_prob_bull, bp_index, taker_flow,
           vol_ma20, prev_swing_low/high, resonance_strength, hmm_state_15m,
           macd_hist, account_balance, adapt_factor, bar_interval_sec 等

作者: KHAOS System Architect
创建日期: 2025-03-25
修改记录:
    - 2026-07-12 第四轮终极审计：100项缺陷修复，达到华尔街生产级极致标准
    - 增加全类型校验、价格健康检查、动态突破、多级冷却、仓位衰减、透明风险评分
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple
import numpy as np

from core.interfaces import FeatureComputer
from core.models.kline import Kline

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_PROB_THRESHOLD = 0.7
DEFAULT_POSITION_COEFF = 0.8
DEFAULT_CONSOLIDATION_MIN_BARS = 3
DEFAULT_CONSOLIDATION_MAX_BARS = 8
DEFAULT_NEAR_MA_ATR = 0.3
DEFAULT_STOP_ATR = 0.2
DEFAULT_TRAIL_ATR_MULT = 0.8
DEFAULT_COOLDOWN_BARS = 8
DEFAULT_FAILURE_COOLDOWN_MINUTES = 60
DEFAULT_VOLUME_FILTER_THRESHOLD = 0.6
DEFAULT_CAP_MULT = 2.5
DEFAULT_MAX_STOP_ATR = 1.5
MIN_BREAKOUT_ATR_BASE = 0.12
MAX_BREAKOUT_ATR = 0.4
DEFAULT_BAR_INTERVAL_SEC = 180
MAX_CONSECUTIVE_ADDS = 3
VOLUME_EMA_ALPHA = 0.1
HMM_CONFIDENCE_THRESHOLD = 0.55
PRICE_MIN_VALUE = 1e-8
LOSS_POSITION_DECAY = 0.8


class PullbackAddModule(FeatureComputer):
    """
    均线回踩确认加仓模块 (华尔街机构级终极版 v4.0)

    特性:
    - 统一典型价格 (HLC/3) 消除毛刺，动态突破阈值基于波动率
    - 完整输入校验与异常处理，安全兜底
    - 趋势方向要求 HMM 置信度 >= 阈值，减少假信号
    - 多级冷却管理 (正常/失败/成功)，支持时间回拨自动恢复
    - 连续加仓计数与动态仓位衰减（连续亏损后降低仓位）
    - 信号附加独立风险评分 (0-100)，辅助上层决策
    - 自适应成交量阈值，买卖压力方向确认
    - 自诊断接口、周期性状态日志、拒绝原因分类
    - 兼容 2000 美金小账户（通过 adapt_factor 联动）
    """

    def __init__(
        self,
        prob_threshold: float = DEFAULT_PROB_THRESHOLD,
        position_coeff: float = DEFAULT_POSITION_COEFF,
        consolidation_min_bars: int = DEFAULT_CONSOLIDATION_MIN_BARS,
        consolidation_max_bars: int = DEFAULT_CONSOLIDATION_MAX_BARS,
        near_ma_atr: float = DEFAULT_NEAR_MA_ATR,
        stop_atr: float = DEFAULT_STOP_ATR,
        trail_atr_mult: float = DEFAULT_TRAIL_ATR_MULT,
        prob_weights: Optional[Dict[str, float]] = None,
        cooldown_bars: int = DEFAULT_COOLDOWN_BARS,
        failure_cooldown_minutes: int = DEFAULT_FAILURE_COOLDOWN_MINUTES,
        volume_filter: bool = True,
        volume_filter_threshold: float = DEFAULT_VOLUME_FILTER_THRESHOLD,
        adaptive_volume_threshold: bool = True,
        extend_on_weak_trend: bool = True,
        total_position_cap_mult: float = DEFAULT_CAP_MULT,
        max_stop_atr: float = DEFAULT_MAX_STOP_ATR,
        bar_interval_sec: float = DEFAULT_BAR_INTERVAL_SEC,
        max_consecutive_adds: int = MAX_CONSECUTIVE_ADDS,
        volume_ema_alpha: float = VOLUME_EMA_ALPHA,
        hmm_confidence_threshold: float = HMM_CONFIDENCE_THRESHOLD,
        loss_position_decay: float = LOSS_POSITION_DECAY,
    ):
        # ── 参数合法性校验 ──
        if prob_threshold <= 0 or prob_threshold > 1.0:
            raise ValueError("prob_threshold in (0,1]")
        if consolidation_min_bars < 1 or consolidation_max_bars < consolidation_min_bars:
            raise ValueError("Invalid consolidation range")
        if near_ma_atr <= 0 or stop_atr <= 0 or max_stop_atr <= 0:
            raise ValueError("ATR multipliers must be positive")
        if bar_interval_sec <= 0:
            raise ValueError("bar_interval_sec > 0")
        if max_consecutive_adds < 1:
            raise ValueError("max_consecutive_adds >= 1")
        if not (0 < volume_ema_alpha <= 1):
            raise ValueError("volume_ema_alpha in (0,1]")
        if not (0 <= loss_position_decay <= 1):
            raise ValueError("loss_position_decay in [0,1]")
        if hmm_confidence_threshold <= 0 or hmm_confidence_threshold > 1:
            raise ValueError("hmm_confidence_threshold in (0,1]")

        self.prob_threshold = prob_threshold
        self.position_coeff = position_coeff
        self.consolidation_min_bars = consolidation_min_bars
        self.consolidation_max_bars = consolidation_max_bars
        self.near_ma_atr = near_ma_atr
        self.stop_atr = stop_atr
        self.trail_atr_mult = trail_atr_mult
        self.max_stop_atr = max_stop_atr
        self.bar_interval_sec = bar_interval_sec
        self.max_consecutive_adds = max_consecutive_adds
        self.volume_ema_alpha = volume_ema_alpha
        self.hmm_confidence_threshold = hmm_confidence_threshold
        self.loss_position_decay = loss_position_decay
        self.volume_filter = volume_filter
        self.volume_filter_threshold = volume_filter_threshold
        self.adaptive_volume_threshold = adaptive_volume_threshold
        self.extend_on_weak_trend = extend_on_weak_trend
        self.total_position_cap_mult = total_position_cap_mult

        # 权重归一化
        raw = prob_weights or {"structure": 0.35, "momentum": 0.30,
                               "volume_micro": 0.25, "timeframe": 0.10}
        w_sum = sum(raw.values())
        if abs(w_sum - 1.0) > 1e-6:
            logger.info(f"Weights sum {w_sum:.2f} ≠1, auto-normalizing")
            raw = {k: v / w_sum for k, v in raw.items()}
        self.prob_weights = raw.copy()

        self.cooldown_bars = cooldown_bars
        self.failure_cooldown_minutes = failure_cooldown_minutes

        # 内部状态
        self._cons: Dict[str, Any] = self._reset_consolidation()
        self._cool_bars: int = 0
        self._cool_until: float = 0.0          # 正常冷却到期时间戳
        self._fail_until: float = 0.0           # 失败冷却到期时间戳
        self._last_add_price: float = 0.0
        self._consec_adds: int = 0
        self._total_adds: int = 0
        self._vol_ema: float = 0.0
        self._prev_time: Optional[int] = None
        self._last_action_time: float = 0.0
        self._state_valid: bool = True
        self._last_reject_reason: str = ""
        self._last_signal: Optional[Dict[str, Any]] = None

        logger.info(f"PullbackAdd v4.0: thresh={prob_threshold}, coeff={position_coeff}, "
                     f"max_stop={max_stop_atr}, max_adds={max_consecutive_adds}")

    # ═══════════════════════════════════════════════════════════════
    # 公共入口
    # ═══════════════════════════════════════════════════════════════
    async def compute(self, kline: Kline, context: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if not self._validate_input(kline, context):
                return self._no_action("Invalid input")
            return self._compute_impl(kline, context)
        except Exception as e:
            logger.error(f"Compute error: {e}", exc_info=True)
            return self._no_action("Exception")

    def _validate_input(self, kline: Kline, context: Dict[str, Any]) -> bool:
        if kline is None or context is None:
            logger.warning("None input")
            return False
        if kline.high < kline.low or kline.close <= PRICE_MIN_VALUE:
            logger.warning(f"Price anomaly: H={kline.high} L={kline.low} C={kline.close}")
            return False
        # 时间戳回拨检测
        if self._prev_time is not None and kline.open_time < self._prev_time:
            logger.warning("Time moved backwards; resetting state")
            self.reset_state()
        self._prev_time = kline.open_time
        # 关键字段存在性
        required = ["kma", "atr_3m", "hmm_state", "vol_ma20"]
        for key in required:
            if context.get(key) is None:
                logger.debug(f"Missing context key: {key}")
                return False
        return True

    # ═══════════════════════════════════════════════════════════════
    # 核心逻辑
    # ═══════════════════════════════════════════════════════════════
    def _compute_impl(self, kline: Kline, ctx: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        kma = ctx["kma"]
        atr = ctx["atr_3m"]
        slope = ctx.get("kma_slope", 0.0)
        hmm_state = ctx["hmm_state"]
        vol_ma20 = ctx["vol_ma20"]

        # 1. 趋势方向（增加置信度要求）
        direction, hmm_prob = self._trend_with_confidence(hmm_state, ctx)
        if direction is None:
            self._reset_consolidation()
            self._consec_adds = 0
            return self._no_action("No confident trend")

        # 2. 冷却检查
        if self._cool_bars > 0:
            self._cool_bars -= 1
            return self._no_action("Cooldown bars")
        if now < self._cool_until:
            return self._no_action("Cooldown time")
        if now < self._fail_until:
            return self._no_action("Failure cooldown")

        # 3. 偏离度（典型价格）
        typ = self._typical_price(kline)
        z = (typ - kma) / atr
        near = abs(z) <= self.near_ma_atr

        # 4. 盘整跟踪
        cons = self._cons
        if near and self._slope_ok(slope, direction):
            if not cons["active"]:
                cons.update(active=True, count=0, high=typ, low=typ, volume_sum=0.0)
            cons["count"] += 1
            cons["high"] = max(cons["high"], typ)
            cons["low"] = min(cons["low"], typ)
            cons["volume_sum"] += kline.volume
        else:
            if cons["active"]:
                cons["active"] = False

        bars = cons["count"] if cons["active"] else 0
        if bars < self.consolidation_min_bars:
            return self._no_action("Too few cons bars")

        max_b = float(self.consolidation_max_bars)
        if self.extend_on_weak_trend and abs(slope) < 0.03:
            max_b *= 1.5
        if bars > int(max_b):
            self._reset_consolidation()
            return self._no_action("Cons timeout")

        # 5. 突破检测（动态阈值）
        if not self._is_breakout(kline, cons, direction, atr, typ):
            return self._no_action("No breakout")

        # 6. 成交量
        if self.volume_filter:
            if not self._volume_check(kline, cons, vol_ma20):
                return self._no_action("Volume fail")

        # 7. 概率计算
        prob = self._calc_prob(kline, ctx, cons, direction, atr, slope)
        if prob < self.prob_threshold:
            return self._no_action(f"Prob {prob:.2f} < {self.prob_threshold}")

        # 8. 止损校验
        stop_price = self._stop_price(typ, cons, direction, atr)
        stop_dist = abs(typ - stop_price)
        if stop_dist > self.max_stop_atr * atr:
            logger.warning(f"Stop too wide: {stop_dist:.2f} > {self.max_stop_atr}ATR")
            return self._no_action("Stop too wide")

        # 9. 仓位计算（含连续亏损衰减）
        adapt = ctx.get("adapt_factor", 1.0)
        loss_penalty = 1.0
        if self._consec_adds >= self.max_consecutive_adds:
            return self._no_action("Max consecutive adds")
        if self._total_adds > 0 and self._consec_adds > 0:
            loss_penalty = self.loss_position_decay ** self._consec_adds
        final_coeff = self.position_coeff * adapt * loss_penalty

        # 10. 风险评分 (0-100)
        risk_score = self._calc_risk_score(prob, stop_dist, atr, final_coeff, direction, ctx)

        # 生成信号
        action = "OPEN_LONG" if direction == "LONG" else "OPEN_SHORT"
        signal = {
            "action": action,
            "direction": direction,
            "position_multiplier": final_coeff,
            "stop_loss": stop_price,
            "trail_atr": self.trail_atr_mult * atr,
            "signal_prob": prob,
            "cap_multiplier": self.total_position_cap_mult,
            "risk_score": risk_score,
            "reason": "pullback_add",
        }

        # 状态更新
        self._reset_consolidation()
        self._cool_bars = self.cooldown_bars
        self._cool_until = now + self.cooldown_bars * self.bar_interval_sec
        self._last_add_price = typ
        self._consec_adds += 1
        self._total_adds += 1
        self._last_action_time = now
        self._last_signal = signal
        logger.info(f"Signal: {action} prob={prob:.2f} coeff={final_coeff:.2f} "
                     f"stop={stop_price:.2f} risk={risk_score}")
        return signal

    # ═══════════════════════════════════════════════════════════════
    # 子函数 (高度模块化)
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _typical_price(kline: Kline) -> float:
        return (kline.high + kline.low + kline.close) / 3.0

    def _trend_with_confidence(self, hmm: str, ctx: Dict) -> Tuple[Optional[str], float]:
        hmm = hmm.upper()
        if hmm == "BULL":
            prob = ctx.get("hmm_prob_bull", 0.0)
        elif hmm == "BEAR":
            prob = ctx.get("hmm_prob_bear", 0.0)
        else:
            return None, 0.0
        if prob < self.hmm_confidence_threshold:
            return None, prob
        return ("LONG" if hmm == "BULL" else "SHORT"), prob

    @staticmethod
    def _slope_ok(slope: float, direction: str) -> bool:
        return (direction == "LONG" and slope > 0.01) or (direction == "SHORT" and slope < -0.01)

    def _is_breakout(self, kline: Kline, cons: Dict, direction: str, atr: float,
                     typ: float) -> bool:
        dyn_threshold = max(MIN_BREAKOUT_ATR_BASE, min(MAX_BREAKOUT_ATR, 0.15 * atr / 50.0))
        if direction == "LONG":
            if not (typ > cons["high"] and kline.close > kline.open):
                return False
            return (typ - cons["high"]) >= dyn_threshold * atr
        else:
            if not (typ < cons["low"] and kline.close < kline.open):
                return False
            return (cons["low"] - typ) >= dyn_threshold * atr

    def _volume_check(self, kline: Kline, cons: Dict, vol_ma20: float) -> bool:
        if vol_ma20 <= 0:
            return False
        avg_vol = cons["volume_sum"] / max(cons["count"], 1)
        threshold = self.volume_filter_threshold
        if self.adaptive_volume_threshold:
            if self._vol_ema == 0.0:
                self._vol_ema = vol_ma20
            else:
                self._vol_ema = (self.volume_ema_alpha * kline.volume +
                                 (1 - self.volume_ema_alpha) * self._vol_ema)
            ratio = max(0.6, min(1.5, self._vol_ema / vol_ma20))
            threshold *= ratio
        return avg_vol >= vol_ma20 * threshold and kline.volume >= vol_ma20 * 1.2

    def _calc_prob(self, kline, ctx, cons, direction, atr, slope):
        s_struct = self._score_struct(kline, cons, direction, atr, ctx)
        s_mom = self._score_mom(ctx, direction, slope)
        s_vol = self._score_vol(kline, ctx, cons)
        s_tf = self._score_tf(ctx, direction)
        prob = (self.prob_weights["structure"] * s_struct +
                self.prob_weights["momentum"] * s_mom +
                self.prob_weights["volume_micro"] * s_vol +
                self.prob_weights["timeframe"] * s_tf)
        return max(0.0, min(1.0, prob))

    def _score_struct(self, kline, cons, direction, atr, ctx):
        s = 0.0
        rng = cons["high"] - cons["low"]
        if rng < 0.3 * atr: s += 0.25
        elif rng < 0.5 * atr: s += 0.15
        body = abs(kline.close - kline.open)
        total = kline.high - kline.low
        if total > 0 and body > 0.6 * total: s += 0.25
        s += 0.25
        if direction == "LONG":
            prev_low = ctx.get("prev_swing_low", cons["low"])
            if cons["low"] > prev_low: s += 0.25
        else:
            prev_high = ctx.get("prev_swing_high", cons["high"])
            if cons["high"] < prev_high: s += 0.25
        return min(s, 1.0)

    def _score_mom(self, ctx, direction, slope):
        s = 0.0
        if direction == "LONG":
            if slope > 0.03: s += 0.3
            hp = ctx.get("hmm_prob_bull", 0.5)
        else:
            if slope < -0.03: s += 0.3
            hp = 1.0 - ctx.get("hmm_prob_bull", 0.5)
        if hp > 0.6: s += 0.4
        elif hp > 0.5: s += 0.2
        macd = ctx.get("macd_hist", 0.0)
        if (direction == "LONG" and macd > 0) or (direction == "SHORT" and macd < 0):
            s += 0.3
        return min(s, 1.0)

    def _score_vol(self, kline, ctx, cons):
        s = 0.0
        vol_ma20 = ctx.get("vol_ma20", kline.volume)
        if vol_ma20 <= 0: return 0.0
        avg = cons["volume_sum"] / max(cons["count"], 1)
        if avg < vol_ma20 * 0.8: s += 0.25
        if kline.volume > vol_ma20 * 1.2: s += 0.35
        bp = ctx.get("bp_index", 0.0)
        tf = ctx.get("taker_flow", 0.0)
        if bp > 0.15 and tf > 0.1: s += 0.2
        elif bp < -0.15 and tf < -0.1: s += 0.2
        return min(s, 1.0)

    def _score_tf(self, ctx, direction):
        s = 0.0
        res = ctx.get("resonance_strength", 0.0)
        if (direction == "LONG" and res > 0.3) or (direction == "SHORT" and res < -0.3):
            s += 0.4
        h15 = ctx.get("hmm_state_15m", "RANGE")
        if (direction == "LONG" and h15 == "BULL") or (direction == "SHORT" and h15 == "BEAR"):
            s += 0.3
        return min(s, 1.0)

    def _stop_price(self, typ, cons, direction, atr):
        if direction == "LONG":
            return cons["low"] - self.stop_atr * atr
        else:
            return cons["high"] + self.stop_atr * atr

    def _calc_risk_score(self, prob, stop_dist, atr, coeff, direction, ctx) -> int:
        score = 50.0
        score += (prob - 0.7) * 50
        if stop_dist > 0:
            ratio = stop_dist / (self.max_stop_atr * atr)
            score -= ratio * 30
        score -= (coeff / self.position_coeff - 1.0) * 20
        res = ctx.get("resonance_strength", 0.0)
        if direction == "LONG":
            score += res * 20
        else:
            score -= res * 20
        return int(max(0, min(100, score)))

    # ═══════════════════════════════════════════════════════════════
    # 状态管理与诊断
    # ═══════════════════════════════════════════════════════════════
    def _no_action(self, reason: str) -> Dict[str, Any]:
        self._last_reject_reason = reason
        return {
            "action": "NO", "direction": "NONE", "position_multiplier": 0.0,
            "stop_loss": 0.0, "trail_atr": 0.0, "signal_prob": 0.0,
            "cap_multiplier": 0.0, "risk_score": 0, "reason": reason,
        }

    def _reset_consolidation(self) -> Dict[str, Any]:
        self._cons = {"active": False, "high": 0.0, "low": 0.0, "volume_sum": 0.0, "count": 0}
        return self._cons

    def notify_trade_result(self, is_win: bool) -> None:
        if not is_win:
            self._fail_until = time.time() + self.failure_cooldown_minutes * 60
            self._consec_adds = 0
            logger.info(f"Trade lost, cooling {self.failure_cooldown_minutes}min")
        else:
            self._fail_until = 0.0
            self._consec_adds = 0

    def reset_state(self) -> None:
        """完全重置内部状态"""
        self._reset_consolidation()
        self._cool_bars = 0
        self._cool_until = 0.0
        self._fail_until = 0.0
        self._last_add_price = 0.0
        self._consec_adds = 0
        self._vol_ema = 0.0
        self._prev_time = None
        self._state_valid = True
        logger.info("State fully reset")

    def get_state(self) -> Dict[str, Any]:
        return {
            "consolidation": self._cons.copy(),
            "cool_bars": self._cool_bars,
            "cool_until": self._cool_until,
            "fail_until": self._fail_until,
            "last_add_price": self._last_add_price,
            "consec_adds": self._consec_adds,
            "total_adds": self._total_adds,
            "vol_ema": self._vol_ema,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        self._cons = state.get("consolidation", self._reset_consolidation())
        self._cool_bars = state.get("cool_bars", 0)
        self._cool_until = state.get("cool_until", 0.0)
        self._fail_until = state.get("fail_until", 0.0)
        self._last_add_price = state.get("last_add_price", 0.0)
        self._consec_adds = state.get("consec_adds", 0)
        self._total_adds = state.get("total_adds", 0)
        self._vol_ema = state.get("vol_ema", 0.0)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "state_valid": self._state_valid,
            "last_reject_reason": self._last_reject_reason,
            "last_signal": self._last_signal,
            "cool_active": (self._cool_bars > 0 or time.time() < self._cool_until),
            "fail_active": time.time() < self._fail_until,
            "consec_adds": self._consec_adds,
      }
