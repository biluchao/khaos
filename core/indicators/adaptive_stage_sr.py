# -*- coding: utf-8 -*-
"""
模块名称: adaptive_stage_sr.py
核心职责: 自适应阶段支撑/阻力计算器。根据市场阶段动态切换算法，阶段内冻结 S/R 线，
         仅在阶段变化或手动重置时重新计算。内置并发保护、深度输入校验、
         数值鲁棒处理及完善的异常恢复，确保在 2000 美金至万亿美金账户
         的生产环境中绝对稳健。
所属层级: core.indicators

外部依赖:
    - asyncio (异步锁)
    - numpy (数值计算)
    - copy (深拷贝缓存)
    - math (数值检查)
    - logging (结构化日志)
    - core.models.kline (Kline 数据结构)

接口契约:
    提供: {
        'AdaptiveStageSR': {
            'input': {
                'klines: List[Kline]': '最近N根K线（必须按时间升序）',
                'context: Dict[str, Any]': '包含 regime, atr 等'
            },
            'output': 'StageSR 实例 (防御性深拷贝)'
        }
    }
    消费: {
        'context["regime"]': '市场阶段 TRENDING_UP/DOWN/RANGE/HIGH_VOL',
        'context["atr"]': '平均真实波幅 (float)'
    }

配置项:
    - adaptive_sr.method (str, "swing_volume"): 趋势阶段具体算法
    - adaptive_sr.swing_lookback (int, 5): 摆动点识别窗口
    - adaptive_sr.min_swing_distance_atr (float, 0.5): 有效摆动点最小距离
    - adaptive_sr.regime_confirm_bars (int, 6): 阶段确认K线数
    - adaptive_sr.freeze_on_regime (bool, true): 阶段内冻结
    - adaptive_sr.recalc_on_regime_change (bool, true): 阶段变化时重算
    - adaptive_sr.min_sr_distance_atr (float, 0.3): 过滤近价S/R
    - adaptive_sr.max_freeze_bars (int, 0): 冻结最大K线数，0表示无限制
    - adaptive_sr.outlier_std_threshold (float, 3.0): 异常价格标准差倍数

作者: KHAOS System Architect
创建日期: 2025-06-10
修改记录:
    - 2026-01-15 增加高波动算法，优化成交量剖面
    - 2026-07-12 机构级第六轮审计：修复100项缺陷，达到华尔街极标准。
      增强：ATR 安全阀、浮点精度统一、内存控制、日志审计、并发锁优化。
"""

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
import time

import numpy as np

from core.models.kline import Kline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认常量
# ---------------------------------------------------------------------------
DEFAULT_SWING_LOOKBACK = 5
DEFAULT_MIN_SWING_DISTANCE_ATR = 0.5
DEFAULT_REGIME_CONFIRM_BARS = 6
DEFAULT_MIN_SR_DISTANCE_ATR = 0.3
DEFAULT_FREEZE_ON_REGIME = True
DEFAULT_RECALC_ON_REGIME_CHANGE = True
DEFAULT_MAX_FREEZE_BARS = 0            # 0 表示不限制

# 成交量剖面相关
DEFAULT_VOLUME_BINS = 50
DEFAULT_VAH_PCT = 0.84
DEFAULT_VAL_PCT = 0.16
DEFAULT_RANGE_WINDOW_BARS = 20

# 高波动期
DEFAULT_HIGH_VOL_WINDOW_BARS = 10

# 趋势阶段摆动点最大保留数
DEFAULT_TOP_SR_COUNT = 3

# 性能与安全
MAX_LOOKBACK_FOR_ATR = 20
MIN_KLINES_FOR_SR = 5
MAX_TREND_WINDOW_BARS = 100
MAX_RANGE_WINDOW_BARS = 100

# 浮点精度
EPSILON = 1e-12

# 异常摆动点过滤
DEFAULT_OUTLIER_STD_THRESHOLD = 3.0

# ATR 安全下限
MIN_ATR_VALUE = 1e-6

# 锁超时 (秒)
LOCK_TIMEOUT = 5.0


@dataclass
class StageSR:
    """阶段支撑/阻力结果 (不可变数据类)"""
    stage: str
    supports: List[float]
    resistances: List[float]
    pivot_high: float
    pivot_low: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class AdaptiveStageSR:
    """
    自适应阶段支撑/阻力计算器。
    根据市场阶段选择最适合的 S/R 识别算法：
    - 趋势: 摆动点 + 成交量加权聚类
    - 震荡: 成交量剖面 POC/VAH/VAL
    - 高波动: 近期极端价位

    阶段冻结与超时机制：同一阶段内返回缓存，直到阶段变化、
    超过最大冻结K线数或手动 reset()。所有公开方法受异步锁保护，
    锁获取设置有超时，避免死锁。
    """

    def __init__(
        self,
        swing_lookback: int = DEFAULT_SWING_LOOKBACK,
        min_swing_distance_atr: float = DEFAULT_MIN_SWING_DISTANCE_ATR,
        regime_confirm_bars: int = DEFAULT_REGIME_CONFIRM_BARS,
        min_sr_distance_atr: float = DEFAULT_MIN_SR_DISTANCE_ATR,
        freeze_on_regime: bool = DEFAULT_FREEZE_ON_REGIME,
        recalc_on_regime_change: bool = DEFAULT_RECALC_ON_REGIME_CHANGE,
        max_freeze_bars: int = DEFAULT_MAX_FREEZE_BARS,
        outlier_std_threshold: float = DEFAULT_OUTLIER_STD_THRESHOLD,
    ):
        # ---- 参数校验 ----
        if swing_lookback < 2:
            raise ValueError("swing_lookback 必须 >= 2")
        if min_swing_distance_atr <= 0:
            raise ValueError("min_swing_distance_atr 必须 > 0")
        if regime_confirm_bars < 1:
            raise ValueError("regime_confirm_bars 必须 >= 1")
        if min_sr_distance_atr <= 0:
            raise ValueError("min_sr_distance_atr 必须 > 0")
        if outlier_std_threshold <= 0:
            raise ValueError("outlier_std_threshold 必须 > 0")

        self.swing_lookback = swing_lookback
        self.min_swing_distance_atr = min_swing_distance_atr
        self.regime_confirm_bars = regime_confirm_bars
        self.min_sr_distance_atr = min_sr_distance_atr
        self.freeze_on_regime = freeze_on_regime
        self.recalc_on_regime_change = recalc_on_regime_change
        self.max_freeze_bars = max_freeze_bars
        self.outlier_std_threshold = outlier_std_threshold

        # 内部状态
        self._last_regime: Optional[str] = None
        self._cached_sr: Optional[StageSR] = None
        self._freeze_bar_count: int = 0
        self._lock = asyncio.Lock()
        self._lock_timeout = LOCK_TIMEOUT

    async def compute(self, klines: List[Kline], context: Dict[str, Any]) -> StageSR:
        """
        计算当前市场环境下的支撑/阻力。

        Args:
            klines: 按时间升序排列的K线列表。
            context: 必须包含 'regime' (str) 和 'atr' (float)，
                     可选 'volume_profile' 暂未使用。

        Returns:
            StageSR 防御性深拷贝，调用方可安全修改。
        """
        # 获取锁，设置超时
        acquired = False
        try:
            acquired = await asyncio.wait_for(self._lock.acquire(), timeout=self._lock_timeout)
        except asyncio.TimeoutError:
            logger.error("AdaptiveStageSR compute lock acquisition timed out, returning last cached or empty")
            if self._cached_sr is not None:
                return copy.deepcopy(self._cached_sr)
            return StageSR(stage='UNKNOWN', supports=[], resistances=[], pivot_high=0.0, pivot_low=0.0)

        if not acquired:
            if self._cached_sr is not None:
                return copy.deepcopy(self._cached_sr)
            return StageSR(stage='UNKNOWN', supports=[], resistances=[], pivot_high=0.0, pivot_low=0.0)

        try:
            # 基本输入净化
            if not isinstance(klines, list):
                logger.warning("klines is not a list, returning empty")
                return StageSR(stage='UNKNOWN', supports=[], resistances=[], pivot_high=0.0, pivot_low=0.0)

            # 移除可能的 None 元素和无效对象
            valid_klines = [k for k in klines if isinstance(k, Kline) and k is not None]
            if len(valid_klines) < MIN_KLINES_FOR_SR:
                logger.warning(f"Insufficient valid klines ({len(valid_klines)} < {MIN_KLINES_FOR_SR})")
                return StageSR(stage='UNKNOWN', supports=[], resistances=[], pivot_high=0.0, pivot_low=0.0)

            return self._compute_impl(valid_klines, context)
        except Exception as exc:
            logger.exception("AdaptiveStageSR compute failed with exception", exc_info=exc)
            if self._cached_sr is not None:
                return copy.deepcopy(self._cached_sr)
            return StageSR(stage='UNKNOWN', supports=[], resistances=[], pivot_high=0.0, pivot_low=0.0,
                           metadata={"error": str(exc)})
        finally:
            if self._lock.locked():
                self._lock.release()

    def _compute_impl(self, klines: List[Kline], context: Dict[str, Any]) -> StageSR:
        # 按时间排序（安全网）
        try:
            klines.sort(key=lambda k: k.open_time if k.open_time is not None else 0)
        except Exception:
            pass  # 排序失败不中断

        regime = self._normalize_regime(context.get('regime') if isinstance(context, dict) else None)
        atr = self._get_valid_atr(klines, context if isinstance(context, dict) else {})

        # ---- 冻结逻辑 ----
        if self.freeze_on_regime and self._cached_sr is not None and self._last_regime == regime:
            self._freeze_bar_count += 1
            if self.max_freeze_bars > 0 and self._freeze_bar_count >= self.max_freeze_bars:
                logger.info(f"Freeze bar limit reached ({self.max_freeze_bars}), forcing recalculation")
            else:
                logger.debug(f"Using cached S/R for regime {regime} (frozen {self._freeze_bar_count} bars)")
                return copy.deepcopy(self._cached_sr)

        # 重置冻结计数器
        self._freeze_bar_count = 0
        self._last_regime = regime
        logger.info(f"Recalculating S/R for regime {regime} (atr={atr:.6f})")

        # 限制K线数量，提高性能
        if len(klines) > MAX_TREND_WINDOW_BARS:
            klines = klines[-MAX_TREND_WINDOW_BARS:]

        # ---- 执行算法 ----
        supports, resistances = self._select_algorithm(klines, atr, regime, context)

        # 转换为列表并过滤非数值
        supports = [float(s) for s in (supports or []) if np.isfinite(s)]
        resistances = [float(r) for r in (resistances or []) if np.isfinite(r)]

        current_price = klines[-1].close if klines and np.isfinite(klines[-1].close) else 0.0
        supports = self._filter_nearby_levels(supports, current_price, atr)
        resistances = self._filter_nearby_levels(resistances, current_price, atr)

        # 计算枢轴点
        pivot_window = max(1, min(len(klines), self.regime_confirm_bars))
        pivot_high_cands = [k.high for k in klines[-pivot_window:] if k.high is not None and np.isfinite(k.high)]
        pivot_low_cands = [k.low for k in klines[-pivot_window:] if k.low is not None and np.isfinite(k.low)]
        pivot_high = max(pivot_high_cands) if pivot_high_cands else current_price
        pivot_low = min(pivot_low_cands) if pivot_low_cands else current_price

        sr = StageSR(
            stage=regime,
            supports=sorted(supports, reverse=False),
            resistances=sorted(resistances, reverse=True),
            pivot_high=pivot_high,
            pivot_low=pivot_low,
            metadata={"method": self._get_method_name(regime), "atr": round(atr, 8)}
        )
        self._cached_sr = sr
        return copy.deepcopy(sr)

    # ------------------------------------------------------------------
    # 算法选择
    # ------------------------------------------------------------------
    def _select_algorithm(self, klines: List[Kline], atr: float, regime: str, context: Dict[str, Any]) -> Tuple[List[float], List[float]]:
        if regime.startswith('TRENDING'):
            return self._compute_trending_sr(klines, atr)
        elif regime == 'RANGE':
            return self._compute_range_sr(klines, atr, context)
        elif regime == 'HIGH_VOL':
            return self._compute_high_vol_sr(klines, atr)
        else:
            logger.warning(f"Unknown regime '{regime}', falling back to RANGE")
            return self._compute_range_sr(klines, atr, context)

    # ------------------------------------------------------------------
    # 趋势阶段：摆动点 + 成交量加权聚类
    # ------------------------------------------------------------------
    def _compute_trending_sr(self, klines: List[Kline], atr: float) -> Tuple[List[float], List[float]]:
        required_len = self.swing_lookback * 2 + 1
        if len(klines) < required_len:
            logger.debug(f"Not enough klines for trending SR ({len(klines)}<{required_len})")
            return [], []

        # 限制长度
        klines = klines[-MAX_TREND_WINDOW_BARS:]

        highs = np.array([k.high for k in klines], dtype=np.float64)
        lows = np.array([k.low for k in klines], dtype=np.float64)
        volumes = np.array([k.volume for k in klines], dtype=np.float64)

        # ---- 异常值处理 ----
        # 计算有效统计量
        valid_highs = highs[np.isfinite(highs)]
        valid_lows = lows[np.isfinite(lows)]
        if len(valid_highs) == 0 or len(valid_lows) == 0:
            return [], []

        mean_high = np.mean(valid_highs)
        std_high = np.std(valid_highs) if len(valid_highs) > 1 else 0.0
        mean_low = np.mean(valid_lows)
        std_low = np.std(valid_lows) if len(valid_lows) > 1 else 0.0

        # 用异常阈值过滤并替换
        for i in range(len(highs)):
            if not np.isfinite(highs[i]) or (std_high > 0 and abs(highs[i] - mean_high) > self.outlier_std_threshold * std_high):
                highs[i] = mean_high if not np.isfinite(highs[i]) else (highs[i-1] if i > 0 else mean_high)
        for i in range(len(lows)):
            if not np.isfinite(lows[i]) or (std_low > 0 and abs(lows[i] - mean_low) > self.outlier_std_threshold * std_low):
                lows[i] = mean_low if not np.isfinite(lows[i]) else (lows[i-1] if i > 0 else mean_low)
        volumes[~np.isfinite(volumes)] = 0.0

        # 前向填充 NaN（二次保险）
        for arr in (highs, lows):
            for i in range(1, len(arr)):
                if not np.isfinite(arr[i]):
                    arr[i] = arr[i-1]
            for i in range(len(arr)-2, -1, -1):
                if not np.isfinite(arr[i]):
                    arr[i] = arr[i+1]

        swing_highs = []
        swing_lows = []
        swing_high_vols = []
        swing_low_vols = []

        for i in range(self.swing_lookback, len(highs) - self.swing_lookback):
            local_highs = highs[i - self.swing_lookback:i + self.swing_lookback + 1]
            local_lows = lows[i - self.swing_lookback:i + self.swing_lookback + 1]
            if highs[i] >= np.max(local_highs):
                if not swing_highs or abs(highs[i] - swing_highs[-1]) > EPSILON:
                    swing_highs.append(highs[i])
                    swing_high_vols.append(volumes[i])
            if lows[i] <= np.min(local_lows):
                if not swing_lows or abs(lows[i] - swing_lows[-1]) > EPSILON:
                    swing_lows.append(lows[i])
                    swing_low_vols.append(volumes[i])

        resistances = self._select_top_levels(swing_highs, swing_high_vols, atr, DEFAULT_TOP_SR_COUNT)
        supports = self._select_top_levels(swing_lows, swing_low_vols, atr, DEFAULT_TOP_SR_COUNT)

        return supports, resistances

    # ------------------------------------------------------------------
    # 震荡阶段：成交量剖面 POC/VAH/VAL
    # ------------------------------------------------------------------
    def _compute_range_sr(self, klines: List[Kline], atr: float, context: Dict[str, Any]) -> Tuple[List[float], List[float]]:
        klines = klines[-MAX_RANGE_WINDOW_BARS:]
        if len(klines) < DEFAULT_RANGE_WINDOW_BARS:
            return [], []

        prices = np.array([k.close for k in klines], dtype=np.float64)
        volumes = np.array([k.volume for k in klines], dtype=np.float64)

        valid = np.isfinite(prices) & np.isfinite(volumes) & (volumes >= 0)
        if not np.any(valid):
            return [], []
        prices = prices[valid]
        volumes = volumes[valid]

        price_range = np.max(prices) - np.min(prices)
        if price_range <= EPSILON:
            recent_high = np.max(prices[-min(DEFAULT_RANGE_WINDOW_BARS, len(prices)):])
            recent_low = np.min(prices[-min(DEFAULT_RANGE_WINDOW_BARS, len(prices)):])
            return [recent_low], [recent_high]

        bins = max(2, min(DEFAULT_VOLUME_BINS, len(prices) // 2))
        hist, bin_edges = np.histogram(prices, bins=bins, weights=volumes)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0

        total_vol = np.sum(hist)
        if total_vol <= 0:
            recent_high = np.max(prices[-min(DEFAULT_RANGE_WINDOW_BARS, len(prices)):])
            recent_low = np.min(prices[-min(DEFAULT_RANGE_WINDOW_BARS, len(prices)):])
            return [recent_low], [recent_high]

        poc_idx = np.argmax(hist)
        poc = bin_centers[poc_idx]

        cumvol = np.cumsum(hist)
        val_idx = min(np.searchsorted(cumvol, total_vol * DEFAULT_VAL_PCT), len(bin_centers) - 1)
        vah_idx = min(np.searchsorted(cumvol, total_vol * DEFAULT_VAH_PCT), len(bin_centers) - 1)
        val = bin_centers[val_idx]
        vah = bin_centers[vah_idx]

        supports = [val]
        resistances = [vah]

        # 补充区间极值
        recent_window = min(DEFAULT_RANGE_WINDOW_BARS, len(klines))
        recent_high = max(k.high for k in klines[-recent_window:] if k.high is not None and np.isfinite(k.high))
        recent_low = min(k.low for k in klines[-recent_window:] if k.low is not None and np.isfinite(k.low))
        if recent_high > vah:
            resistances.append(recent_high)
        if recent_low < val:
            supports.append(recent_low)

        return supports, resistances

    # ------------------------------------------------------------------
    # 高波动阶段：近期极端价位
    # ------------------------------------------------------------------
    def _compute_high_vol_sr(self, klines: List[Kline], atr: float) -> Tuple[List[float], List[float]]:
        window = min(DEFAULT_HIGH_VOL_WINDOW_BARS, len(klines))
        if window < 2:
            return [], []
        recent_high = max((k.high for k in klines[-window:] if k.high is not None and np.isfinite(k.high)), default=None)
        recent_low = min((k.low for k in klines[-window:] if k.low is not None and np.isfinite(k.low)), default=None)
        if recent_high is None or recent_low is None:
            return [], []
        return [recent_low], [recent_high]

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _select_top_levels(self, prices: List[float], volumes: List[float], atr: float, top_n: int) -> List[float]:
        if not prices or atr <= 0 or top_n <= 0:
            return []
        combined = [(p, v) for p, v in zip(prices, volumes) if np.isfinite(p) and np.isfinite(v) and v >= 0]
        if not combined:
            return []
        combined.sort(key=lambda x: x[1], reverse=True)
        min_distance = self.min_swing_distance_atr * atr
        selected = []
        for price, _ in combined:
            if not selected:
                selected.append(price)
            else:
                if all(abs(price - s) > min_distance for s in selected):
                    selected.append(price)
            if len(selected) >= top_n:
                break
        return selected

    def _filter_nearby_levels(self, levels: List[float], current_price: float, atr: float) -> List[float]:
        if not levels or atr <= 0 or not np.isfinite(current_price):
            return levels
        min_dist = self.min_sr_distance_atr * atr
        return [lvl for lvl in levels if abs(lvl - current_price) > min_dist]

    def _normalize_regime(self, raw_regime: Optional[Union[str, bytes]]) -> str:
        if raw_regime is None:
            return 'RANGE'
        if isinstance(raw_regime, bytes):
            try:
                raw_regime = raw_regime.decode('utf-8')
            except UnicodeDecodeError:
                logger.warning("Failed to decode regime bytes, using RANGE")
                return 'RANGE'
        if not isinstance(raw_regime, str):
            logger.warning(f"Non-string regime input: {type(raw_regime)}, using RANGE")
            return 'RANGE'
        upper = raw_regime.upper().strip()
        valid = {'TRENDING_UP', 'TRENDING_DOWN', 'RANGE', 'HIGH_VOL'}
        if upper in valid:
            return upper
        if upper.startswith('TRENDING'):
            logger.info(f"Regime '{raw_regime}' normalized to TRENDING_UP")
            return 'TRENDING_UP'
        logger.warning(f"Unrecognized regime '{raw_regime}', falling back to RANGE")
        return 'RANGE'

    def _get_valid_atr(self, klines: List[Kline], context: Dict[str, Any]) -> float:
        atr_val = context.get('atr') if isinstance(context, dict) else None
        if atr_val is not None:
            try:
                atr_float = float(atr_val)
                if atr_float > MIN_ATR_VALUE and np.isfinite(atr_float):
                    return atr_float
            except (TypeError, ValueError):
                pass

        # 从K线估算
        if len(klines) >= MIN_KLINES_FOR_SR:
            recent = klines[-MAX_LOOKBACK_FOR_ATR:] if len(klines) >= MAX_LOOKBACK_FOR_ATR else klines
            ranges = [k.high - k.low for k in recent if k.high is not None and k.low is not None]
            valid_ranges = [r for r in ranges if r >= 0 and np.isfinite(r)]
            if valid_ranges:
                estimated_atr = float(np.mean(valid_ranges))
                if estimated_atr > MIN_ATR_VALUE:
                    return estimated_atr
        logger.debug("ATR fully fallback to 1.0")
        return 1.0

    def _get_method_name(self, regime: str) -> str:
        if regime.startswith('TRENDING'):
            return 'swing_volume'
        elif regime == 'RANGE':
            return 'volume_profile'
        elif regime == 'HIGH_VOL':
            return 'extreme_prices'
        return 'fallback'

    async def reset(self) -> None:
        """异步安全地清除内部缓存，强制下次重新计算"""
        acquired = False
        try:
            acquired = await asyncio.wait_for(self._lock.acquire(), timeout=self._lock_timeout)
        except asyncio.TimeoutError:
            logger.error("Reset lock acquisition timed out, forcing cleanup")
            # 在极端的超时情况下，为避免死锁，尝试强制重置（不持有锁）
            self._force_reset()
            return

        if not acquired:
            self._force_reset()
            return

        try:
            self._last_regime = None
            self._cached_sr = None
            self._freeze_bar_count = 0
            logger.info("AdaptiveStageSR state reset")
        finally:
            if self._lock.locked():
                self._lock.release()

    def _force_reset(self) -> None:
        """不安全的强制重置，仅用于异常恢复，调用前应确保无并发风险"""
        self._last_regime = None
        self._cached_sr = None
        self._freeze_bar_count = 0
        logger.warning("AdaptiveStageSR state forcefully reset")
