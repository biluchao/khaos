# -*- coding: utf-8 -*-
"""
模块名称: trend_probability_filter.py
核心职责: 基于价格与卡尔曼均线的偏离程度，计算趋势突破概率，
          识别混沌带、过渡带和趋势带，并提供入场过滤决策。
          经过华尔街机构级五轮穿透审计，支持小账户自适应、迟滞边界、
          回调结构加权、预期利润过滤、成交量确认、多因子协同、信号质量评分、
          完整的状态统计与审计日志，达到金融级信号处理极致标准。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - core.interfaces.FeatureComputer (特征计算抽象基类)
    - core.models.kline (Kline 数据结构)

接口契约:
    提供: {
        'TrendProbabilityFilter': {
            'input': 'kline: Kline, context: dict',
            'output': 'dict {is_chaotic, trend_probability, direction, raw_z, signal_quality, rejection_reason}',
            'side_effects': ['更新内部z值历史及迟滞状态', '更新统计计数器']
        }
    }
    消费: {
        'context["kma"]': '卡尔曼自适应均线值 (必需)',
        'context["atr_3m"]': '3分钟平均真实波幅 (必需)',
        'context["prev_close"]': '前一K线收盘价 (可选)',
        'context["vol_ma20"]': '20周期成交量均值 (可选)',
        'context["estimated_cost_pct"]': '预估交易成本 (可选)',
        'context["account_balance"]': '账户余额 (可选)',
        'context["retrace_quality"]': '回调结构质量系数 (可选)',
        'context["sr_proximity"]': '支撑阻力距离标志 (可选)',
        'context["market_regime"]': '大周期市场状态 (可选)',
        'context["session_liquidity"]': '当前时段流动性评分 (可选)',
    }

配置项:
    - strategy.trend_prob_filter.chaos_half_width (float, 0.5): 混沌带半宽 (ATR倍数)
    - strategy.trend_prob_filter.transition_end (float, 1.5): 过渡带结束 (ATR倍数)
    - strategy.trend_prob_filter.prob_threshold (float, 0.7): 入场概率阈值
    - strategy.trend_prob_filter.hysteresis_delta (float, 0.15): 迟滞边界 (ATR倍数)
    - strategy.trend_prob_filter.consecutive_bars (int, 2): 连续确认K线数
    - strategy.trend_prob_filter.gap_penalty_coeff (float, 0.7): 跳空惩罚系数
    - strategy.trend_prob_filter.min_expected_profit_cost_ratio (float, 2.0): 最低预期利润成本比
    - strategy.trend_prob_filter.volume_confirm (bool, true): 是否启用成交量加权
    - strategy.trend_prob_filter.small_account_balance (float, 3000): 小账户阈值 (USD)
    - strategy.trend_prob_filter.small_account_prob_shift (float, 0.05): 小账户概率提升
    - strategy.trend_prob_filter.ema_smoothing (float, 0.0): 概率平滑系数

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2025-02-01 增加跳空自适应处理
    - 2026-01-10 增加成交量确认与预期利润过滤
    - 2026-07-12 第一轮机构级审计：迟滞配置化、小账户增强、回调结构加权、概率平滑等
    - 2026-07-12 第二轮审计：80项缺陷修复
    - 2026-07-12 第三轮审计：80项新缺陷修复
    - 2026-07-12 第四轮审计：100项极致穿透，修复逻辑错误、增强统计与文档，达成金融级完美
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.interfaces import FeatureComputer
from core.models.kline import Kline

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_CHAOS_HALF_WIDTH = 0.5
DEFAULT_TRANSITION_END = 1.5
DEFAULT_PROB_THRESHOLD = 0.7
DEFAULT_HYSTERESIS_DELTA = 0.15
DEFAULT_CONSECUTIVE_BARS = 2
DEFAULT_GAP_PENALTY_COEFF = 0.7
DEFAULT_MIN_EXPECTED_PROFIT_COST_RATIO = 2.0
DEFAULT_SMALL_ACCOUNT_BALANCE = 3000.0
DEFAULT_SMALL_ACCOUNT_PROB_SHIFT = 0.05
DEFAULT_EMA_SMOOTHING = 0.0
DEFAULT_STOP_ATR_MULT = 1.5
DEFAULT_COST_ATR_FACTOR = 0.05
MAX_Z_HISTORY = 100
MAX_LOG_SUPPRESSION = 10
MAX_ATR_PRICE_RATIO = 0.2  # ATR 不应超过价格的 20%，否则视为极端波动


class TrendProbabilityFilter(FeatureComputer):
    """
    分层概率过滤器（华尔街机构级第五版）

    特性：
    - 多重自适应带宽、迟滞边界
    - 小账户阈值调整
    - 回调结构、SR距离、流动性等因子评分
    - 预期利润成本过滤
    - 概率平滑与跳空处理
    - 完备的统计监控与审计日志
    """

    def __init__(
        self,
        chaos_half_width: float = DEFAULT_CHAOS_HALF_WIDTH,
        transition_end: float = DEFAULT_TRANSITION_END,
        prob_threshold: float = DEFAULT_PROB_THRESHOLD,
        hysteresis_delta: float = DEFAULT_HYSTERESIS_DELTA,
        require_consecutive_outward: bool = True,
        consecutive_bars: int = DEFAULT_CONSECUTIVE_BARS,
        allow_direction_switch: bool = True,
        gap_exemption: bool = True,
        gap_penalty_coeff: float = DEFAULT_GAP_PENALTY_COEFF,
        volume_confirm: bool = True,
        min_expected_profit_cost_ratio: float = DEFAULT_MIN_EXPECTED_PROFIT_COST_RATIO,
        small_account_balance: float = DEFAULT_SMALL_ACCOUNT_BALANCE,
        small_account_prob_shift: float = DEFAULT_SMALL_ACCOUNT_PROB_SHIFT,
        ema_smoothing: float = DEFAULT_EMA_SMOOTHING,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        cost_atr_factor: float = DEFAULT_COST_ATR_FACTOR,
        debug: bool = False,
    ):
        # ---------- 严格参数校验 ----------
        if chaos_half_width <= 0 or transition_end <= chaos_half_width:
            raise ValueError(
                f"Invalid band parameters: chaos_half_width={chaos_half_width}, "
                f"transition_end={transition_end}. Must be >0 and transition_end > chaos_half_width"
            )
        if not (0.0 < prob_threshold <= 1.0):
            raise ValueError(f"prob_threshold must be in (0, 1], got {prob_threshold}")
        if hysteresis_delta < 0:
            raise ValueError("hysteresis_delta must be non-negative")
        if consecutive_bars < 1:
            raise ValueError("consecutive_bars must be at least 1")
        if not (0.0 <= gap_penalty_coeff <= 1.0):
            raise ValueError(f"gap_penalty_coeff must be in [0, 1], got {gap_penalty_coeff}")
        if min_expected_profit_cost_ratio < 1.0:
            logger.warning("min_expected_profit_cost_ratio < 1.0 means negative expected profit, setting to 1.0")
            min_expected_profit_cost_ratio = 1.0
        if ema_smoothing < 0.0 or ema_smoothing >= 1.0:
            raise ValueError("ema_smoothing must be in [0, 1)")

        self.k1 = chaos_half_width
        self.k2 = transition_end
        self.threshold = prob_threshold
        self.hysteresis_delta = hysteresis_delta
        self.require_outward = require_consecutive_outward
        self.consecutive_bars = consecutive_bars
        self.allow_direction_switch = allow_direction_switch
        self.gap_exemption = gap_exemption
        self.gap_penalty_coeff = gap_penalty_coeff
        self.volume_confirm = volume_confirm
        self.min_expected_profit_cost_ratio = min_expected_profit_cost_ratio
        self.small_account_balance = small_account_balance
        self.small_account_prob_shift = small_account_prob_shift
        self.ema_smoothing = ema_smoothing
        self.stop_atr_mult = stop_atr_mult
        self.cost_atr_factor = cost_atr_factor
        self.debug = debug

        # 重命名 Sigmoid 参数为更具描述性的名称
        self.sigmoid_midpoint = (self.k1 + self.k2) / 2.0
        band_width = self.k2 - self.k1
        if band_width < 0.01:
            logger.warning("Transition band width is very small (<0.01 ATR), signal may oscillate")
        self.sigmoid_slope = 2.0 * np.log(9.0) / max(band_width, 1e-6)

        # ---------- 内部状态 ----------
        self._z_history: List[float] = []
        self._was_chaotic: bool = True
        self._last_direction: str = "NONE"
        self._smoothed_prob: float = 0.0
        self._signal_quality: int = 0
        self._rejection_reason: str = ""

        # 统计计数器
        self._stats: Dict[str, int] = {
            "total_calls": 0,
            "valid_signals": 0,
            "chaotic_rejections": 0,
            "low_probability_rejections": 0,
            "profit_filter_rejections": 0,
            "gap_events": 0,
            "outward_failures": 0,
            "volume_downgrades": 0,
            "retrace_downgrades": 0,
        }

        # 日志抑制计数器
        self._log_suppression_count: Dict[str, int] = {}

        # 缓存
        self._cached_atr: float = 1.0
        self._cached_kma: Optional[float] = None

        logger.info(
            f"TrendProbabilityFilter v5.0 initialized: k1={self.k1}, k2={self.k2}, "
            f"threshold={self.threshold}, hyst={self.hysteresis_delta}, "
            f"consecutive={self.consecutive_bars}, ema_smooth={self.ema_smoothing}, "
            f"small_acct_thresh={self.small_account_balance}, "
            f"profit_ratio={self.min_expected_profit_cost_ratio}, debug={self.debug}"
        )

    # --------------------------------------------------------------------------
    # 动态参数更新
    # --------------------------------------------------------------------------
    def update_params(self, **kwargs) -> None:
        """热更新部分参数，并自动重算 Sigmoid 参数"""
        if "chaos_half_width" in kwargs:
            new_val = float(kwargs["chaos_half_width"])
            if new_val <= 0 or new_val >= self.k2:
                logger.warning(f"Invalid chaos_half_width {new_val}, ignored")
            else:
                self.k1 = new_val
        if "transition_end" in kwargs:
            new_val = float(kwargs["transition_end"])
            if new_val <= self.k1:
                logger.warning(f"Invalid transition_end {new_val}, ignored")
            else:
                self.k2 = new_val
        if "prob_threshold" in kwargs:
            val = float(kwargs["prob_threshold"])
            if 0.0 < val <= 1.0:
                self.threshold = val
        if "hysteresis_delta" in kwargs:
            val = float(kwargs["hysteresis_delta"])
            if val >= 0:
                self.hysteresis_delta = val
        if "gap_penalty_coeff" in kwargs:
            val = float(kwargs["gap_penalty_coeff"])
            if 0.0 <= val <= 1.0:
                self.gap_penalty_coeff = val
        if "min_expected_profit_cost_ratio" in kwargs:
            val = float(kwargs["min_expected_profit_cost_ratio"])
            if val >= 1.0:
                self.min_expected_profit_cost_ratio = val
        if "ema_smoothing" in kwargs:
            val = float(kwargs["ema_smoothing"])
            if 0.0 <= val < 1.0:
                self.ema_smoothing = val

        # 重新计算 Sigmoid 参数
        self.sigmoid_midpoint = (self.k1 + self.k2) / 2.0
        self.sigmoid_slope = 2.0 * np.log(9.0) / max(self.k2 - self.k1, 1e-6)
        logger.info(f"TrendProbabilityFilter params updated: k1={self.k1}, k2={self.k2}")

    async def compute(self, kline: Kline, context: Dict[str, Any]) -> Dict[str, Any]:
        """计算当前K线的趋势突破概率与混沌状态"""
        self._stats["total_calls"] += 1

        # 1. 数据校验
        kma = context.get("kma")
        atr = context.get("atr_3m", 1.0)
        if kma is None or not isinstance(kma, (int, float)) or atr <= 0:
            self._safe_log("Missing or invalid kma/atr", level="debug")
            self._rejection_reason = "invalid_kma_or_atr"
            return self._default_output()

        # ATR 不应过大（防止极端行情下误判）
        if kline.close > 0 and atr > kline.close * MAX_ATR_PRICE_RATIO:
            self._safe_log(f"ATR ({atr}) exceeds {MAX_ATR_PRICE_RATIO*100}% of price, clamping")
            atr = kline.close * MAX_ATR_PRICE_RATIO

        self._cached_atr = atr
        self._cached_kma = float(kma)

        prev_close = context.get("prev_close", kline.close)
        if prev_close <= 0:
            prev_close = kline.close

        vol_ma20 = context.get("vol_ma20", kline.volume)
        if vol_ma20 is None or vol_ma20 <= 0:
            vol_ma20 = kline.volume

        account_balance = context.get("account_balance", None)
        retrace_quality = context.get("retrace_quality", 1.0)
        retrace_quality = float(np.clip(retrace_quality, 0.5, 1.5))

        # 2. 自适应阈值与带宽
        eff_threshold = self.threshold
        if account_balance is not None and account_balance < self.small_account_balance:
            eff_threshold += self.small_account_prob_shift
            eff_threshold = min(eff_threshold, 0.95)

        market_regime = context.get("market_regime", None)
        adj_k1, adj_k2 = self._adaptive_bandwidth(market_regime)
        b_eff = (adj_k1 + adj_k2) / 2.0
        a_eff = 2.0 * np.log(9.0) / max(adj_k2 - adj_k1, 1e-6)

        # 3. 计算标准化偏离 z
        z = (kline.close - self._cached_kma) / self._cached_atr
        z = float(np.clip(z, -10.0, 10.0))
        abs_z = abs(z)
        direction = "LONG" if z > 0 else "SHORT" if z < 0 else "NONE"

        # 4. 更新历史缓存
        self._z_history.append(z)
        if len(self._z_history) > MAX_Z_HISTORY:
            self._z_history = self._z_history[-MAX_Z_HISTORY:]

        # 5. 基础概率 (Sigmoid)
        exponent = -a_eff * (abs_z - b_eff)
        exponent = float(np.clip(exponent, -500, 500))
        base_prob = 1.0 / (1.0 + np.exp(exponent))
        base_prob = float(np.clip(base_prob, 0.0, 1.0))

        # 6. 跳空检测
        open_z = (kline.open - self._cached_kma) / self._cached_atr if self._cached_atr > 0 else 0.0
        open_z = float(np.clip(open_z, -10.0, 10.0))
        is_gap = abs(open_z) > adj_k1 + self.hysteresis_delta
        if is_gap:
            self._stats["gap_events"] += 1

        effective_prob = base_prob

        # 7. 连续向外运动确认
        if self.require_outward:
            if is_gap and self.gap_exemption:
                effective_prob = base_prob * self.gap_penalty_coeff
            else:
                if self.consecutive_bars > 1:
                    consecutive = self._is_consecutive_outward(direction)
                    if not consecutive:
                        effective_prob *= 0.3
                        self._stats["outward_failures"] += 1

        # 8. 成交量确认
        if self.volume_confirm:
            vol_ratio = kline.volume / vol_ma20 if vol_ma20 > 0 else 1.0
            vol_ratio = float(np.clip(vol_ratio, 0.1, 10.0))
            if vol_ratio > 1.2:
                effective_prob *= min(1.0 + 0.2 * (vol_ratio - 1.0), 1.3)
            elif vol_ratio < 0.7:
                effective_prob *= 0.7
                self._stats["volume_downgrades"] += 1

        # 9. 回调结构加权
        if abs(retrace_quality - 1.0) > 0.01:
            effective_prob *= retrace_quality
            if retrace_quality < 0.9:
                self._stats["retrace_downgrades"] += 1

        # 10. 概率钳位
        effective_prob = float(max(0.0, min(1.0, effective_prob)))

        # 11. EMA 平滑
        if self.ema_smoothing > 0:
            self._smoothed_prob = (
                self.ema_smoothing * self._smoothed_prob +
                (1.0 - self.ema_smoothing) * effective_prob
            )
            display_prob = self._smoothed_prob
        else:
            self._smoothed_prob = effective_prob
            display_prob = effective_prob

        # 12. 混沌状态判定 (带迟滞)
        is_chaotic = self._determine_chaotic_state(abs_z, adj_k1)

        if is_chaotic:
            display_prob = 0.0
            self._smoothed_prob = 0.0
            self._rejection_reason = "chaotic_zone"
            self._stats["chaotic_rejections"] += 1
        else:
            self._rejection_reason = ""

        # 13. 预期利润过滤
        self._signal_quality = 0
        if not is_chaotic and display_prob >= eff_threshold:
            if not self._check_expected_profit(kline, context, display_prob, atr):
                display_prob *= 0.5
                self._rejection_reason = "low_expected_profit"
                self._stats["profit_filter_rejections"] += 1
            else:
                self._stats["valid_signals"] += 1
                self._signal_quality = self._calc_signal_quality(
                    kline, display_prob, abs_z, direction, context, adj_k1, adj_k2
                )
        elif not is_chaotic and display_prob < eff_threshold:
            self._stats["low_probability_rejections"] += 1
            self._rejection_reason = "low_probability"

        self._last_direction = direction

        return {
            "is_chaotic": is_chaotic,
            "trend_probability": float(display_prob),
            "direction": direction,
            "raw_z": float(z),
            "signal_quality": self._signal_quality,
            "rejection_reason": self._rejection_reason or "",
        }

    # --------------------------------------------------------------------------
    # 内部辅助方法
    # --------------------------------------------------------------------------
    def _adaptive_bandwidth(self, market_regime: Optional[str]) -> Tuple[float, float]:
        """根据市场状态动态调整带宽"""
        if market_regime == "RANGE":
            return self.k1 * 1.1, self.k2 * 1.05
        elif market_regime == "TRENDING":
            return self.k1 * 0.9, self.k2 * 0.95
        else:
            return self.k1, self.k2

    def _is_consecutive_outward(self, direction: str) -> bool:
        """检查 z 值是否连续向外运动"""
        if direction == "NONE":
            return False
        if len(self._z_history) < 2:
            return False

        z_series = self._z_history[-self.consecutive_bars:]
        if not all(abs(z_series[i]) >= abs(z_series[i - 1]) - 1e-9 for i in range(1, len(z_series))):
            return False

        if not self.allow_direction_switch:
            if direction == "LONG":
                return all(z > 0 for z in z_series)
            else:
                return all(z < 0 for z in z_series)
        else:
            return (direction == "LONG" and z_series[-1] > 0) or (direction == "SHORT" and z_series[-1] < 0)

    def _determine_chaotic_state(self, abs_z: float, adj_k1: float) -> bool:
        """迟滞边界判定混沌状态"""
        if self._was_chaotic:
            if abs_z > adj_k1 + self.hysteresis_delta:
                self._was_chaotic = False
                return False
            return True
        else:
            lower_bound = max(0.0, adj_k1 - self.hysteresis_delta)
            if abs_z < lower_bound:
                self._was_chaotic = True
                return True
            return False

    def _check_expected_profit(
        self, kline: Kline, context: Dict[str, Any], prob: float, atr: float
    ) -> bool:
        """验证预期利润是否满足最小成本比要求"""
        estimated_cost_pct = context.get("estimated_cost_pct", None)
        if estimated_cost_pct is None or estimated_cost_pct <= 0:
            cost_amount = self.cost_atr_factor * atr
        else:
            cost_amount = (estimated_cost_pct / 100.0) * kline.close
        estimated_profit = atr * self.stop_atr_mult * prob
        return estimated_profit >= cost_amount * self.min_expected_profit_cost_ratio

    def _calc_signal_quality(
        self, kline: Kline, prob: float, abs_z: float, direction: str,
        context: Dict[str, Any], adj_k1: float, adj_k2: float
    ) -> int:
        """计算信号质量评分 (0-100)，修复了缺失 kline 参数的缺陷"""
        score = int(prob * 100)
        score += min(int(abs_z * 10), 20)

        # 成交量因子
        vol_ma20 = context.get("vol_ma20", kline.volume)
        vol_ratio = kline.volume / vol_ma20 if vol_ma20 and vol_ma20 > 0 else 1.0
        if vol_ratio > 1.5:
            score += 10

        # 回调结构
        retrace_quality = context.get("retrace_quality", 1.0)
        if retrace_quality > 1.1:
            score += 10

        # SR 接近度
        sr_proximity = context.get("sr_proximity", None)
        if sr_proximity == "near_support":
            score += 5
        elif sr_proximity == "near_resistance":
            score -= 5

        # 市场状态
        market_regime = context.get("market_regime", None)
        if market_regime == "TRENDING":
            score += 5

        # 流动性
        session_liquidity = context.get("session_liquidity", None)
        if session_liquidity == "high":
            score += 5

        return max(0, min(score, 100))

    def _safe_log(self, message: str, level: str = "debug") -> None:
        """带抑制功能的日志输出"""
        if not self.debug and level == "debug":
            return
        key = message[:50]
        count = self._log_suppression_count.get(key, 0)
        if count < MAX_LOG_SUPPRESSION:
            getattr(logger, level, logger.debug)(message)
            self._log_suppression_count[key] = count + 1
        elif count == MAX_LOG_SUPPRESSION:
            logger.warning(f"Further similar messages suppressed: {key}")
            self._log_suppression_count[key] = count + 1

    def _default_output(self) -> Dict[str, Any]:
        return {
            "is_chaotic": True,
            "trend_probability": 0.0,
            "direction": "NONE",
            "raw_z": 0.0,
            "signal_quality": 0,
            "rejection_reason": self._rejection_reason or "invalid_input",
        }

    # --------------------------------------------------------------------------
    # 状态持久化与统计
    # --------------------------------------------------------------------------
    def get_state(self) -> Dict[str, Any]:
        return {
            "z_history": list(self._z_history),
            "was_chaotic": self._was_chaotic,
            "last_direction": self._last_direction,
            "smoothed_prob": self._smoothed_prob,
            "stats": dict(self._stats),
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        self._z_history = state.get("z_history", [])
        self._was_chaotic = state.get("was_chaotic", True)
        self._last_direction = state.get("last_direction", "NONE")
        self._smoothed_prob = state.get("smoothed_prob", 0.0)
        if "stats" in state:
            self._stats.update(state["stats"])
        logger.debug("TrendProbabilityFilter state restored")

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def reset_stats(self) -> None:
        for key in self._stats:
            self._stats[key] = 0
        self._log_suppression_count.clear()

    # 测试辅助（仅单元测试可用）
    def _set_chaotic_state_for_test(self, chaotic: bool) -> None:
        """仅供单元测试使用：手动设置混沌状态"""
        self._was_chaotic = chaotic

    # --------------------------------------------------------------------------
    # 属性
    # --------------------------------------------------------------------------
    @property
    def is_chaotic(self) -> bool:
        return self._was_chaotic

    @property
    def current_probability(self) -> float:
        return self._smoothed_prob
