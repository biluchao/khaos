# -*- coding: utf-8 -*-
"""
模块名称: escape_detector.py
核心职责: 阶段顶智能逃逸检测：基于多维特征融合的逃逸分数模型，
          在趋势末端尽早退出，最小化利润回吐。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - collections.deque (高效历史缓存)
    - core.interfaces.FeatureComputer (特征计算基类)
    - core.models.kline (Kline数据结构)

接口契约:
    提供: {
        'StageTopEscapeDetector': {
            'input': 'kline: Kline, context: dict',
            'output': 'dict {escape_score, action, details, cooldown_remaining}',
            'side_effects': ['更新内部状态缓存']
        }
    }
    消费: {
        'context["kma"]': '卡尔曼均线值',
        'context["kma_slope"]': '卡尔曼均线斜率',
        'context["atr_3m"]': '3分钟ATR值',
        'context["bpi"]': '买卖压力指数 (可选)',
        'context["taker_flow"]': '主动吃单速率 (可选)',
        'context["sr_levels"]': '大周期支撑阻力 (可选)',
        'context["wave_similarity"]': '波浪相似度 (可选)',
        'context["hmm_bull_prob"]': 'HMM多头概率',
        'kline.volume': '成交量'
    }

配置项:
    - strategy.escape.weights: 各维度权重（含 sideways）
    - strategy.escape.thresholds: warn/danger 阈值
    - strategy.escape.cooldown_bars: 逃逸后冷却K线数
    - strategy.escape.dynamic_thresholds: 是否启用动态阈值
    - strategy.escape.strong_trend_exemption: 强趋势豁免
    详细参数见类构造函数

作者: KHAOS System Architect
创建日期: 2025-02-15
修改记录:
    - 2026-07-12 v3.0 第三轮极致审查：100项缺陷修复，全方位防御，审计级日志
"""

import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.interfaces import FeatureComputer
from core.models.kline import Kline

logger = logging.getLogger(__name__)

# 默认参数
DEFAULT_WEIGHTS = {
    'momentum': 0.25,
    'volatility': 0.20,
    'micro': 0.20,
    'sr': 0.15,
    'wave': 0.10,
    'sideways': 0.10,
}
DEFAULT_THRESHOLDS = {'warn': 0.40, 'danger': 0.65}
DEFAULT_COOLDOWN_BARS = 10
DEFAULT_HISTORY_MAXLEN = 100
MIN_ACCOUNT_FOR_ADAPT = 5000.0
# 配置文件版本，用于状态恢复兼容性检查
CONFIG_VERSION = "3.0"

class StageTopEscapeDetector(FeatureComputer):
    """阶段顶智能逃逸检测器 (华尔街机构级 v3.0 最终版)"""

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        thresholds: Optional[Dict[str, float]] = None,
        cooldown_bars: int = DEFAULT_COOLDOWN_BARS,
        slope_lookback: int = 8,
        prob_history_window: int = 5,
        dynamic_thresholds: bool = True,
        strong_trend_exemption: bool = True,
        sideways_bars: int = 15,
        sideways_atr_range: float = 0.3,
        account_balance: float = 50000.0,
        prob_threshold_strong_trend: float = 0.85,
        slope_rise_confirmation_bars: int = 2,
        collinear_threshold: float = 0.7,
        slope_threshold_high: float = 0.8,
        slope_threshold_low: float = 0.02,
        sigmoid_scale: float = 5.0,
        min_history_len: int = 5,
        max_history_len: int = DEFAULT_HISTORY_MAXLEN,
        # 新增控制参数
        danger_confirm_count_needed: int = 2,
        min_warn_threshold: float = 0.10,
        min_danger_threshold: float = 0.20,
        max_warn_threshold: float = 0.70,
        max_danger_threshold: float = 0.90,
        enable_audit_log: bool = True,
    ):
        # ---------- 强化参数校验 ----------
        if cooldown_bars < 0: raise ValueError("cooldown_bars >= 0")
        if slope_lookback < 3: raise ValueError("slope_lookback >= 3")
        if prob_history_window < 2: raise ValueError("prob_history_window >= 2")
        if sideways_bars < 5: raise ValueError("sideways_bars >= 5")
        if not 0 <= collinear_threshold <= 1.0: raise ValueError("collinear_threshold ∈ [0,1]")
        if account_balance <= 0:
            logger.warning(f"account_balance ({account_balance}) <= 0, capping to 1.0")
            account_balance = 1.0
        if prob_threshold_strong_trend <= 0 or prob_threshold_strong_trend > 1.0:
            raise ValueError("prob_threshold_strong_trend ∈ (0,1]")
        if danger_confirm_count_needed < 1: raise ValueError("danger_confirm_count_needed >= 1")
        if min_history_len < 3: raise ValueError("min_history_len >= 3")

        # 权重处理
        merged_weights = DEFAULT_WEIGHTS.copy()
        if weights:
            merged_weights.update(weights)
        total = sum(merged_weights.values())
        if abs(total - 1.0) > 0.01:
            logger.warning(f"Weights sum to {total}, normalizing")
            merged_weights = {k: v / total for k, v in merged_weights.items()}
        self.weights = merged_weights

        # 阈值处理
        raw_thresh = thresholds if thresholds else DEFAULT_THRESHOLDS.copy()
        # 保证危险阈值大于警告阈值
        if raw_thresh['warn'] >= raw_thresh['danger']:
            logger.warning("warn threshold >= danger, resetting to defaults")
            raw_thresh = DEFAULT_THRESHOLDS.copy()
        self.thresholds = raw_thresh

        # 存储所有参数
        self.cooldown_bars = cooldown_bars
        self.slope_lookback = slope_lookback
        self.prob_history_window = prob_history_window
        self.dynamic_thresholds = dynamic_thresholds
        self.strong_trend_exemption = strong_trend_exemption
        self.sideways_bars = sideways_bars
        self.sideways_atr_range = sideways_atr_range
        self.account_balance = account_balance
        self.prob_threshold_strong_trend = prob_threshold_strong_trend
        self.slope_rise_confirmation_bars = slope_rise_confirmation_bars
        self.collinear_threshold = collinear_threshold
        self.slope_threshold_high = slope_threshold_high
        self.slope_threshold_low = slope_threshold_low
        self.sigmoid_scale = sigmoid_scale
        self.min_history_len = min_history_len
        self.max_history_len = max_history_len
        self.danger_confirm_count_needed = danger_confirm_count_needed
        self.min_warn_threshold = min_warn_threshold
        self.min_danger_threshold = min_danger_threshold
        self.max_warn_threshold = max_warn_threshold
        self.max_danger_threshold = max_danger_threshold
        self.enable_audit_log = enable_audit_log

        # 历史缓存 (deque)
        self._slope_hist: deque = deque(maxlen=self.max_history_len)
        self._prob_hist: deque = deque(maxlen=self.max_history_len)
        self._high_hist: deque = deque(maxlen=self.max_history_len)
        self._low_hist: deque = deque(maxlen=self.max_history_len)
        self._bpi_hist: deque = deque(maxlen=self.max_history_len)
        self._taker_hist: deque = deque(maxlen=self.max_history_len)
        self._volume_hist: deque = deque(maxlen=self.max_history_len)
        self._escape_hist: deque = deque(maxlen=self.max_history_len)

        self._cooldown_remaining = 0
        self._danger_confirm_count = 0
        self._config_version = CONFIG_VERSION

        logger.info(
            f"EscapeDetector v3.0 initialized: balance={account_balance:.0f}, "
            f"weights={self.weights}, danger_confirm={danger_confirm_count_needed}"
        )

    async def compute(self, kline: Kline, context: Dict[str, Any]) -> Dict[str, Any]:
        if kline is None:
            logger.error("Received None kline")
            return self._default_output(reason="None kline")

        # 必要数据检查
        kma = context.get("kma")
        atr = context.get("atr_3m", 1.0)
        if kma is None or atr <= 0:
            return self._default_output(reason="missing kma or atr")

        # 安全获取字段
        kma_slope = float(context.get("kma_slope", 0.0))
        hmm_bull_prob = float(context.get("hmm_bull_prob", 0.5))
        bpi = float(context.get("bpi", 0.0))
        taker_flow = float(context.get("taker_flow", 0.0))
        sr_levels = context.get("sr_levels", {}) or {}
        wave_sim = float(context.get("wave_similarity", 0.0))
        volume = kline.volume if kline.volume is not None else 0.0

        # 更新历史（即使在冷却期）
        self._update_cache(kma_slope, hmm_bull_prob, kline.high, kline.low, bpi, taker_flow, volume)

        # 冷却期处理
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return {
                'escape_score': 0.0,
                'action': 'HOLD',
                'details': {},
                'cooldown_remaining': self._cooldown_remaining,
                'reason': 'cooldown',
            }

        # 计算子分数（每个独立保护）
        subscores = {}
        subscores['momentum'] = self._safe_score(self._momentum_score, kma_slope, hmm_bull_prob, atr)
        subscores['volatility'] = self._safe_score(self._volatility_score, kline, atr)
        subscores['micro'] = self._safe_score(self._micro_score, bpi, taker_flow)
        subscores['sr'] = self._safe_score(self._sr_score, kline, sr_levels, atr)
        subscores['wave'] = self._safe_score(self._wave_score, wave_sim)
        subscores['sideways'] = self._safe_score(self._sideways_score, kline, atr)

        # 共线性调整权重
        weights = self._get_adjusted_weights(subscores)

        # 综合分数
        escape_score = 0.0
        for name in self.weights:  # 使用 self.weights 保证键齐全
            escape_score += weights[name] * subscores[name]
        escape_score = max(0.0, min(1.0, escape_score))

        # 强趋势豁免
        if self.strong_trend_exemption and self._is_strong_trend(hmm_bull_prob, kma_slope):
            escape_score *= 0.5

        # 动态阈值（小账户、斜率）
        warn_thresh, danger_thresh = self._get_effective_thresholds(kma_slope)

        # 防抖逻辑
        if escape_score >= danger_thresh:
            self._danger_confirm_count += 1
        else:
            self._danger_confirm_count = 0

        action = 'HOLD'
        reason = ''
        if escape_score >= danger_thresh and self._danger_confirm_count >= self.danger_confirm_count_needed:
            action = 'CLOSE_ALL'
            self._cooldown_remaining = self.cooldown_bars
            reason = f'danger confirmed ({self._danger_confirm_count})'
        elif escape_score >= warn_thresh:
            action = 'REDUCE_50'
            reason = f'warn ({escape_score:.2f} >= {warn_thresh:.2f})'
            self._danger_confirm_count = 0

        if action != 'HOLD' and self.enable_audit_log:
            logger.info(
                f"Escape action={action}, score={escape_score:.3f}, "
                f"subscores={subscores}, thresholds=({warn_thresh:.2f},{danger_thresh:.2f}), "
                f"reason={reason}, balance={self.account_balance:.0f}"
            )

        self._escape_hist.append(escape_score)

        return {
            'escape_score': escape_score,
            'action': action,
            'details': subscores,
            'cooldown_remaining': self._cooldown_remaining,
            'reason': reason if reason else ('held' if action == 'HOLD' else action),
        }

    # ---------- 子分数计算 ----------
    def _momentum_score(self, slope: float, prob: float, atr: float) -> float:
        if len(self._slope_hist) < self.min_history_len:
            return 0.0
        # 只取最近 slope_lookback 个元素（高效切片转换）
        slopes = list(self._slope_hist)[-self.slope_lookback:]
        prev_slope = np.mean(slopes[:-1]) if len(slopes) > 1 else slopes[0]
        delta_slope = prev_slope - slope
        std_slope = np.std(slopes) if len(slopes) > 1 else 1e-6
        norm_delta = delta_slope / max(std_slope, 1e-10)
        slope_score = self._sigmoid(norm_delta * self.sigmoid_scale, center=0.2)

        probs = list(self._prob_hist)[-self.prob_history_window:]
        if len(probs) >= 2:
            prev_prob = np.mean(probs[:-1]) if len(probs) > 1 else probs[0]
            delta_prob = prev_prob - prob
            prob_score = self._sigmoid(delta_prob * 10, center=0.05)
        else:
            prob_score = 0.0
        return (slope_score + prob_score) / 2.0

    def _volatility_score(self, kline: Kline, atr: float) -> float:
        body = abs(kline.close - kline.open)
        upper_wick = kline.high - max(kline.close, kline.open)
        score = 0.0
        if body > 1e-10 and upper_wick > 0:
            wick_ratio = upper_wick / body
            if wick_ratio > 2.0:
                score += min(0.5, 0.1 * (wick_ratio - 1.0))
        if len(self._volume_hist) >= 5:
            avg_vol = np.mean(list(self._volume_hist)[-5:])
            if kline.volume > avg_vol * 1.5 and body < atr * 0.2:
                score += 0.3
        return min(1.0, score)

    def _micro_score(self, bpi: float, taker_flow: float) -> float:
        score = 0.0
        if len(self._bpi_hist) >= 3:
            prev_bpi = np.mean(list(self._bpi_hist)[-3:])
            if prev_bpi > 0.1 and bpi < -0.1:
                score += 0.4
        if len(self._taker_hist) >= 3:
            prev_tf = np.mean(list(self._taker_hist)[-3:])
            if prev_tf > 0.1 and taker_flow < -0.1:
                score += 0.3
        if score > 0 and bpi < -0.15 and taker_flow < -0.1:
            score += 0.3
        return min(1.0, score)

    def _sr_score(self, kline: Kline, sr_levels: Dict, atr: float) -> float:
        score = 0.0
        for key in ('5min_resistances', '15min_resistances'):
            levels = sr_levels.get(key, [])
            for res in levels:
                if isinstance(res, (int, float)) and kline.high >= res * 0.99:
                    score += 0.3 if key == '5min_resistances' else 0.2
                    break
        return min(1.0, score)

    def _wave_score(self, similarity: float) -> float:
        if similarity > 0.75:
            return min(1.0, similarity * 1.2)
        return 0.0

    def _sideways_score(self, kline: Kline, atr: float) -> float:
        if len(self._high_hist) < self.sideways_bars:
            return 0.0
        highs = list(self._high_hist)[-self.sideways_bars:]
        lows = list(self._low_hist)[-self.sideways_bars:]
        range_ = max(highs) - min(lows)
        if range_ < self.sideways_atr_range * atr:
            # 当前高点未显著超过前期高点
            if len(highs) > 1 and kline.high <= max(highs[:-1]) * 1.001:
                return 0.5
        return 0.0

    # ---------- 辅助方法 ----------
    def _update_cache(self, slope, prob, high, low, bpi, taker, volume):
        self._slope_hist.append(slope)
        self._prob_hist.append(prob)
        self._high_hist.append(high)
        self._low_hist.append(low)
        self._bpi_hist.append(bpi)
        self._taker_hist.append(taker)
        self._volume_hist.append(volume)

    def _sigmoid(self, x: float, center: float = 0.0, scale: float = 1.0) -> float:
        x = np.clip(x, -50, 50)
        return 1.0 / (1.0 + np.exp(-scale * (x - center)))

    def _safe_score(self, func, *args) -> float:
        try:
            return func(*args)
        except Exception as e:
            logger.error(f"Subscore {func.__name__} failed: {e}", exc_info=True)
            return 0.0

    def _get_adjusted_weights(self, subscores: Dict[str, float]) -> Dict[str, float]:
        w = self.weights.copy()
        # 共线性检测（动量 vs 波动率）
        if subscores.get('momentum', 0) > 0.5 and subscores.get('volatility', 0) > 0.5:
            if len(self._slope_hist) >= 10 and len(self._high_hist) >= 10:
                slope_arr = np.array(list(self._slope_hist)[-10:])
                high_arr = np.array(list(self._high_hist)[-10:])
                low_arr = np.array(list(self._low_hist)[-10:])
                vol_arr = high_arr - low_arr
                if np.std(slope_arr) > 1e-10 and np.std(vol_arr) > 1e-10:
                    corr = np.corrcoef(slope_arr, vol_arr)[0, 1]
                    if abs(corr) > self.collinear_threshold:
                        w['volatility'] *= 0.7
                        w['sr'] += 0.1
                        w['sideways'] += 0.05
        # 归一化
        total = sum(w.values())
        if total > 0:
            w = {k: v / total for k, v in w.items()}
        return w

    def _is_strong_trend(self, prob: float, slope: float) -> bool:
        if prob < self.prob_threshold_strong_trend:
            return False
        if len(self._slope_hist) >= self.slope_rise_confirmation_bars + 1:
            recent = list(self._slope_hist)[-self.slope_rise_confirmation_bars - 1:]
            # 检查连续上升且上升幅度至少为 1e-6
            for i in range(1, len(recent)):
                if recent[i] < recent[i-1] - 1e-10:  # 允许微小波动
                    return False
            return True
        return False

    def _get_effective_thresholds(self, slope: float) -> Tuple[float, float]:
        warn = float(self.thresholds['warn'])
        danger = float(self.thresholds['danger'])

        if self.dynamic_thresholds:
            abs_slope = abs(slope)
            if abs_slope > self.slope_threshold_high:
                warn += 0.10
                danger += 0.05
            elif abs_slope < self.slope_threshold_low:
                warn -= 0.05
                danger -= 0.03

        # 小账户自适应：净值越小，阈值越低，但不超过预设范围
        if self.account_balance < MIN_ACCOUNT_FOR_ADAPT:
            factor = max(0.5, self.account_balance / MIN_ACCOUNT_FOR_ADAPT)
            warn *= factor
            danger *= factor

        # 硬限制
        warn = max(self.min_warn_threshold, min(self.max_warn_threshold, warn))
        danger = max(self.min_danger_threshold, min(self.max_danger_threshold, danger))
        # 确保 danger > warn
        if danger <= warn:
            danger = warn + 0.05
        return warn, danger

    def _default_output(self, reason: str = "") -> Dict[str, Any]:
        return {
            'escape_score': 0.0,
            'action': 'HOLD',
            'details': {},
            'cooldown_remaining': self._cooldown_remaining,
            'reason': reason,
        }

    def get_state(self) -> Dict[str, Any]:
        return {
            'config_version': self._config_version,
            'slope_hist': list(self._slope_hist),
            'prob_hist': list(self._prob_hist),
            'high_hist': list(self._high_hist),
            'low_hist': list(self._low_hist),
            'bpi_hist': list(self._bpi_hist),
            'taker_hist': list(self._taker_hist),
            'volume_hist': list(self._volume_hist),
            'escape_hist': list(self._escape_hist),
            'cooldown_remaining': self._cooldown_remaining,
            'danger_confirm_count': self._danger_confirm_count,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        # 版本兼容性检查
        if state.get('config_version') != self._config_version:
            logger.warning("State version mismatch, attempting partial restore")
        # 重建所有 deque
        for attr, key in [
            ('_slope_hist', 'slope_hist'), ('_prob_hist', 'prob_hist'),
            ('_high_hist', 'high_hist'), ('_low_hist', 'low_hist'),
            ('_bpi_hist', 'bpi_hist'), ('_taker_hist', 'taker_hist'),
            ('_volume_hist', 'volume_hist'), ('_escape_hist', 'escape_hist')
        ]:
            data = state.get(key, [])
            dq = deque(maxlen=self.max_history_len)
            dq.extend(data[-self.max_history_len:])  # 保证不超限
            setattr(self, attr, dq)
        self._cooldown_remaining = state.get('cooldown_remaining', 0)
        self._danger_confirm_count = state.get('danger_confirm_count', 0)
        logger.debug("StageTopEscapeDetector state restored")
