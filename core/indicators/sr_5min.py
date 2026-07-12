# -*- coding: utf-8 -*-
"""
模块名称: sr_5min.py
核心职责: 计算5分钟K线周期的动态支撑与阻力线，基于局部摆动点识别与成交量加权聚类
所属层级: core.indicators

外部依赖:
    - numpy (数组计算与聚类)
    - core.interfaces.SupportResistanceComputer (抽象接口)
    - core.models.Kline (K线数据结构)

接口契约:
    提供: {
        'SwingVolumeSR': {
            'input': 'klines: List[Kline], context: dict',
            'output': 'Tuple[List[float], List[float]] -> (supports, resistances)',
            'side_effects': ['无（纯计算）']
        }
    }
    消费: {
        'klines': '最近96根（默认）5分钟K线，必须按开盘时间升序排列',
        'context["atr_5m"]': '5分钟ATR值（推荐），若缺失则使用收盘价标准差估计'
    }

配置项:
    - sr_5m.window (int, 96): 计算所用K线窗口大小（最小值 2*swing_period+1）
    - sr_5m.swing_period (int, 5): 左右确认的极值点周期（至少 1）
    - sr_5m.min_swing_distance_atr (float, 0.3): 有效S/R线与当前价格的最小ATR距离
    - sr_5m.max_sr_lines (int, 3): 最终输出的支撑/阻力线数量
    - sr_5m.cluster_distance_atr (float, 0.3): 聚类时合并价位距离阈值（ATR倍数）
    - sr_5m.max_cluster_distance_atr (float, 1.0): 聚类距离上限（ATR倍数），防止波动率异常放大
    - sr_5m.outlier_atr_mult (float, 5.0): 异常价格过滤的ATR倍数
    - sr_5m.volume_cap_percentile (float, 99.0): 成交量截尾分位数，用于限制极端大成交量影响

作者: KHAOS System Architect
创建日期: 2025-03-20
修改记录:
    - 2026-07-12 完成第一轮100项缺陷修复，版本 v2.0
    - 2026-07-12 完成第二轮100项穿透缺陷修复，版本 v3.0
    - 2026-07-12 完成第三轮100项极致缺陷修复，版本 v4.0
"""

import logging
import numpy as np
from typing import List, Tuple
from core.interfaces import SupportResistanceComputer
from core.models import Kline

logger = logging.getLogger(__name__)

# 常量
MIN_ATR = 1e-8
OUTLIER_ATR_MULT_DEFAULT = 5.0
MAX_VOLUME_CAP_PERCENTILE = 99.0
DEFAULT_MAX_CLUSTER_DIST_ATR = 1.0
ABSOLUTE_MIN_CLUSTER_DIST = 0.0  # 允许无距离限制


class SwingVolumeSR(SupportResistanceComputer):
    """
    5分钟支撑/阻力计算器：局部摆动点 + 成交量加权聚类。
    输出水平支撑/阻力线，最多 max_sr_lines 条。
    """

    def __init__(self,
                 window: int = 96,
                 swing_period: int = 5,
                 min_swing_distance_atr: float = 0.3,
                 max_sr_lines: int = 3,
                 cluster_distance_atr: float = 0.3,
                 max_cluster_distance_atr: float = DEFAULT_MAX_CLUSTER_DIST_ATR,
                 outlier_atr_mult: float = OUTLIER_ATR_MULT_DEFAULT,
                 volume_cap_percentile: float = MAX_VOLUME_CAP_PERCENTILE):
        """
        初始化摆动点支撑阻力计算器。

        Args:
            window: 计算使用的最近K线根数，至少 2*swing_period+1。
            swing_period: 判定摆动点所需的左右K线根数，至少 1。
            min_swing_distance_atr: 有效S/R线距当前价格的最小ATR倍数。
            max_sr_lines: 最终输出的最多支撑/阻力线数量。
            cluster_distance_atr: 聚类时合并价位的基础距离阈值（ATR倍数）。
            max_cluster_distance_atr: 聚类距离上限（ATR倍数），防止 atr 异常放大时聚类过宽。
            outlier_atr_mult: 价格异常值过滤的ATR倍数。
            volume_cap_percentile: 成交量截尾分位数，限制极端成交量对聚类的影响。
        """
        self.swing_period = max(1, swing_period)
        min_window = 2 * self.swing_period + 1
        self.window = max(min_window, window)
        if window < min_window:
            logger.warning(f"window={window} adjusted to min {min_window}")
        self.min_swing_distance_atr = min_swing_distance_atr
        self.max_sr_lines = max(1, max_sr_lines)
        self.cluster_distance_atr = cluster_distance_atr
        self.max_cluster_distance_atr = max_cluster_distance_atr
        self.outlier_atr_mult = outlier_atr_mult
        self.volume_cap_percentile = min(100.0, max(0.0, volume_cap_percentile))

    async def compute(self, klines: List[Kline], context: dict = None) -> Tuple[List[float], List[float]]:
        """
        异步计算支撑与阻力线（实际计算为同步，满足接口契约）。

        Args:
            klines: 最近的5分钟K线列表，应按开盘时间升序。
            context: 上下文，需包含 'atr_5m' 或 'atr'。

        Returns:
            Tuple[List[float], List[float]]: (supports, resistances)
        """
        if context is None:
            context = {}
        try:
            return self._compute_sync(klines, context)
        except Exception as e:
            logger.error(f"SwingVolumeSR computation failed: {e}", exc_info=True)
            return [], []

    def _compute_sync(self, klines: List[Kline], context: dict) -> Tuple[List[float], List[float]]:
        """计算的核心同步逻辑。"""
        # 1. 数据清洗
        klines = self._clean_klines(klines)
        if len(klines) < self.window:
            logger.debug(f"Valid klines {len(klines)} < required {self.window}")
            return [], []

        window_klines = klines[-self.window:] if len(klines) > self.window else klines

        closes = np.array([k.close for k in window_klines], dtype=np.float64)
        highs = np.array([k.high for k in window_klines], dtype=np.float64)
        lows = np.array([k.low for k in window_klines], dtype=np.float64)
        volumes = np.array([abs(k.volume) if k.volume else 0.0 for k in window_klines], dtype=np.float64)

        # 2. 获取ATR
        atr = self._extract_atr(context, closes)
        if atr <= MIN_ATR:
            logger.debug("ATR too small, cannot compute SR")
            return [], []

        # 3. 成交量截尾
        volumes = self._cap_volumes(volumes)

        # 4. 识别摆动点
        swing_high_indices = self._find_swing_points(highs, mode='high')
        swing_low_indices = self._find_swing_points(lows, mode='low')

        # 5. 提取候选
        resist_candidates = highs[swing_high_indices]
        resist_vols = volumes[swing_high_indices]
        support_candidates = lows[swing_low_indices]
        support_vols = volumes[swing_low_indices]

        # 6. 成交量加权聚类
        resistances = self._cluster_prices(resist_candidates, resist_vols, atr)
        supports = self._cluster_prices(support_candidates, support_vols, atr)

        # 7. 过滤距离价格太近的线
        current_price = closes[-1]
        supports = [s for s in supports
                    if (current_price - s) > self.min_swing_distance_atr * atr
                    and s > 0 and np.isfinite(s)]
        resistances = [r for r in resistances
                       if (r - current_price) > self.min_swing_distance_atr * atr
                       and r > 0 and np.isfinite(r)]

        # 8. 排序并限制数量
        supports = sorted(supports, reverse=True)[:self.max_sr_lines]
        resistances = sorted(resistances)[:self.max_sr_lines]

        logger.debug(f"SR result: supports={supports}, resistances={resistances}")
        return supports, resistances

    def _extract_atr(self, context: dict, closes: np.ndarray) -> float:
        """安全提取ATR，缺失时基于收盘价估计。"""
        atr = None
        if 'atr_5m' in context:
            atr = context['atr_5m']
        elif 'atr' in context:
            atr = context['atr']

        if atr is not None:
            try:
                atr = float(atr)
            except (TypeError, ValueError):
                atr = None
        if atr is None or atr <= 0:
            if len(closes) > 1:
                # 过滤可能的 NaN
                valid_closes = closes[np.isfinite(closes)]
                atr = float(np.nanstd(valid_closes)) if len(valid_closes) > 1 else 0.0
            else:
                atr = 0.0
        atr = max(atr, MIN_ATR)
        return atr

    def _clean_klines(self, klines: List[Kline]) -> List[Kline]:
        """清洗K线数据：去重、排序、过滤无效值（包括NaN/Inf/零价格）。"""
        cleaned = []
        seen_times = set()
        for k in klines:
            if not isinstance(k, Kline):
                continue
            if (k.open_time is None or k.close is None or k.high is None or k.low is None):
                continue
            # 过滤非正价格或非有限数值
            if not (np.isfinite(k.close) and np.isfinite(k.high) and np.isfinite(k.low) and k.close > 0):
                continue
            if k.open_time in seen_times:
                continue
            seen_times.add(k.open_time)
            cleaned.append(k)
        cleaned.sort(key=lambda x: x.open_time)
        return cleaned

    def _cap_volumes(self, volumes: np.ndarray) -> np.ndarray:
        """对成交量进行截尾，防止个别极端大量扭曲聚类。"""
        if len(volumes) == 0 or self.volume_cap_percentile >= 100.0:
            return volumes
        cap_value = np.percentile(volumes, self.volume_cap_percentile)
        if cap_value > 0:
            volumes = np.minimum(volumes, cap_value)
        return volumes

    def _find_swing_points(self, series: np.ndarray, mode: str) -> List[int]:
        """识别局部极值点，去重相邻相同价格的极值点，使用相对容差。"""
        period = self.swing_period
        if len(series) < 2 * period + 1:
            return []
        indices = []
        prev_idx = -period - 1
        for i in range(period, len(series) - period):
            window = series[i - period:i + period + 1]
            is_extreme = False
            if mode == 'high':
                if np.isclose(series[i], np.max(window), rtol=1e-10):
                    is_extreme = True
            else:  # mode == 'low'
                if np.isclose(series[i], np.min(window), rtol=1e-10):
                    is_extreme = True

            if is_extreme:
                # 去重：若价格与上一个极值几乎相同，且索引距离在 period 内，则跳过
                if prev_idx >= 0 and np.isclose(series[i], series[prev_idx], rtol=1e-10) and (i - prev_idx) <= period:
                    continue
                indices.append(i)
                prev_idx = i
        return indices

    def _cluster_prices(self, prices: np.ndarray, volumes: np.ndarray, atr: float) -> List[float]:
        """
        成交量加权聚类：合并距离 < cluster_distance_atr * atr 的价位，
        返回按总成交量降序排列的加权平均价格列表。
        """
        # 过滤无效值
        valid = np.isfinite(prices) & np.isfinite(volumes) & (volumes >= 0)
        prices = prices[valid]
        volumes = volumes[valid]
        if len(prices) == 0:
            return []

        # 过滤价格异常值
        median_price = np.median(prices)
        safe_atr = max(atr, MIN_ATR)
        deviation = np.abs(prices - median_price)
        outlier_mask = deviation < self.outlier_atr_mult * safe_atr
        prices = prices[outlier_mask]
        volumes = volumes[outlier_mask]
        if len(prices) == 0:
            logger.debug("All prices filtered as outliers.")
            return []

        # 排序
        sort_idx = np.argsort(prices)
        sorted_prices = prices[sort_idx]
        sorted_volumes = volumes[sort_idx]

        # 聚类距离：基础距离 * ATR，但限制上限
        cluster_dist = self.cluster_distance_atr * atr
        cluster_dist = min(cluster_dist, self.max_cluster_distance_atr * atr)
        cluster_dist = max(cluster_dist, ABSOLUTE_MIN_CLUSTER_DIST)

        clusters = []
        current_prices = [sorted_prices[0]]
        current_volumes = [sorted_volumes[0]]

        for i in range(1, len(sorted_prices)):
            if sorted_prices[i] - sorted_prices[i - 1] < cluster_dist:
                current_prices.append(sorted_prices[i])
                current_volumes.append(sorted_volumes[i])
            else:
                clusters.append((current_prices, current_volumes))
                current_prices = [sorted_prices[i]]
                current_volumes = [sorted_volumes[i]]
        clusters.append((current_prices, current_volumes))

        # 计算加权平均并排序
        cluster_weighted = []
        for price_list, vol_list in clusters:
            total_vol = np.sum(vol_list)
            if total_vol > 0:
                avg_price = float(np.average(price_list, weights=vol_list))
            else:
                avg_price = float(np.mean(price_list))
            cluster_weighted.append((avg_price, total_vol))

        cluster_weighted.sort(key=lambda x: x[1], reverse=True)
        return [price for price, _ in cluster_weighted]

    def __repr__(self) -> str:
        return (f"SwingVolumeSR(window={self.window}, swing_period={self.swing_period}, "
                f"min_dist={self.min_swing_distance_atr}ATR, max_lines={self.max_sr_lines}, "
                f"cluster_dist={self.cluster_distance_atr}ATR, max_cluster_dist={self.max_cluster_distance_atr}ATR, "
                f"outlier_mult={self.outlier_atr_mult}, vol_cap={self.volume_cap_percentile}%)")
