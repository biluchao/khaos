# -*- coding: utf-8 -*-
"""
模块名称: wave_similarity_engine.py
核心职责: 基于动态时间规整(DTW)的波浪形态相似度引擎，提供实时相似度评分、
         辅助识别高概率重复模式，用于仓位微调或提前预警。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - collections.deque (形态缓存)
    - asyncio (异步锁)
    - time (时间戳)
    - math (数学函数)
    - typing (类型注解)
    - core.models.kline (Kline)

接口契约:
    提供: {
        'WaveSimilarityEngine': {
            'add_positive_pattern(klines, profit_pct, meta)': '存入高盈利形态',
            'evaluate_similarity(klines, volumes)': '返回相似度结果',
            'clear_cache()': '清空缓存',
            'update_config(changes)': '运行时更新部分配置',
            'stats': '运行统计 (属性)',
            'reset_stats()': '重置统计'
        }
    }
    消费: Kline.close, Kline.volume (可选)
配置项: 见 DEFAULT_CONFIG
"""

import asyncio
import logging
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from core.models.kline import Kline

logger = logging.getLogger(__name__)

# =============================================================================
# 默认配置
# =============================================================================
DEFAULT_CONFIG: Dict[str, Any] = {
    "max_pattern_count": 1000,
    "paa_segments": 24,
    "sakoe_chiba_band": 0.1,
    "top_k": 5,
    "decay_factor": 3.0,
    "max_sequence_length": 100,
    "max_pattern_age_seconds": 86400 * 7,
    "max_memory_mb": 200,
    "enable_volume_weighted": False,
    "enable_reverse_match": True,
    "similarity_calibration": False,
    "adaptive_band": False,
    "flat_std_threshold": 1e-10,
    "smooth_window": 0,
    "dtw_cost_limit": 1e9,
    "duplicate_threshold_per_len": 0.05,  # 去重阈值相对于最小长度
    "prune_batch_size": 50,               # 每次剪枝最多处理数量
}

# 允许运行时热更新的参数白名单
HOT_UPDATE_PARAMS = {
    "sakoe_chiba_band", "top_k", "decay_factor", "max_pattern_age_seconds",
    "enable_volume_weighted", "enable_reverse_match", "similarity_calibration",
    "adaptive_band", "flat_std_threshold", "dtw_cost_limit",
}


@dataclass
class SimilarityResult:
    """相似度评估结果"""
    score: float
    best_distance: float
    avg_top_k_distance: float
    match_count: int
    calibrated_score: Optional[float] = None
    details: Optional[List[Dict[str, Any]]] = None


@dataclass
class PatternMeta:
    """存储的形态元数据"""
    paa: np.ndarray
    std: float
    profit_pct: float
    timestamp: float
    volume_profile: Optional[np.ndarray] = None
    pattern_id: Optional[int] = None


class WaveSimilarityEngine:
    """形态相似度引擎（华尔街终极版 v4.0）"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = deepcopy(DEFAULT_CONFIG)
        if config:
            cfg.update(config)
        self._cfg = cfg
        self._validate_config()

        # 从配置提取为属性（方便快速访问）
        self._apply_config()

        # 内部状态
        self._cache: Deque[PatternMeta] = deque(
            maxlen=self.max_pattern_count if self.max_pattern_count > 0 else None
        )
        self._lock = asyncio.Lock()
        self._pattern_id_counter = 0
        self._total_memory_estimate = 0

        # 统计（受主锁保护）
        self._stats = {
            "total_added": 0,
            "total_skipped_flat": 0,
            "total_skipped_duplicate": 0,
            "total_evaluated": 0,
            "total_pruned": 0,
            "hit_count": 0,
            "miss_count": 0,
            "reverse_matches": 0,
        }
        self._adaptive_band_hits = 0
        self._adaptive_band_trials = 0

        logger.info(
            f"WaveSimilarityEngine v4.0 initialized: PAA={self.paa_segments}, "
            f"band={self.sakoe_chiba_band}, top_k={self.top_k}, "
            f"vol_weighted={self.enable_volume_weighted}"
        )

    def _apply_config(self) -> None:
        """将配置属性同步到实例变量"""
        self.max_pattern_count = self._cfg["max_pattern_count"]
        self.paa_segments = self._cfg["paa_segments"]
        self.sakoe_chiba_band = self._cfg["sakoe_chiba_band"]
        self.top_k = self._cfg["top_k"]
        self.decay_factor = self._cfg["decay_factor"]
        self.max_sequence_length = self._cfg["max_sequence_length"]
        self.max_pattern_age_seconds = self._cfg["max_pattern_age_seconds"]
        self.max_memory_bytes = self._cfg["max_memory_mb"] * 1024 * 1024
        self.enable_volume_weighted = self._cfg["enable_volume_weighted"]
        self.enable_reverse_match = self._cfg["enable_reverse_match"]
        self.similarity_calibration = self._cfg["similarity_calibration"]
        self.adaptive_band = self._cfg["adaptive_band"]
        self.flat_std_threshold = self._cfg["flat_std_threshold"]
        self.smooth_window = self._cfg["smooth_window"]
        self.dtw_cost_limit = self._cfg["dtw_cost_limit"]
        self.duplicate_threshold_per_len = self._cfg["duplicate_threshold_per_len"]
        self.prune_batch_size = self._cfg["prune_batch_size"]

    def _validate_config(self) -> None:
        """启动时校验配置，抛出 ValueError"""
        if self._cfg["max_pattern_count"] < 0:
            raise ValueError("max_pattern_count >= 0")
        if not (2 <= self._cfg["paa_segments"] <= 200):
            raise ValueError("paa_segments in [2, 200]")
        if not (0.0 <= self._cfg["sakoe_chiba_band"] <= 1.0):
            raise ValueError("sakoe_chiba_band in [0, 1]")
        if self._cfg["top_k"] < 1:
            raise ValueError("top_k >= 1")
        if self._cfg["decay_factor"] <= 0:
            raise ValueError("decay_factor > 0")
        if self._cfg["dtw_cost_limit"] <= 0:
            raise ValueError("dtw_cost_limit > 0")

    async def update_config(self, changes: Dict[str, Any]) -> None:
        """
        运行时更新部分配置参数（仅白名单内的参数）。
        """
        async with self._lock:
            for key, value in changes.items():
                if key in HOT_UPDATE_PARAMS:
                    self._cfg[key] = value
            # 重新校验（仅检查范围，不抛出影响线程）
            try:
                self._validate_config()
                self._apply_config()
                logger.info("Configuration updated: %s", changes)
            except ValueError as e:
                logger.error("Invalid config update rejected: %s", e)

    # =====================================================================
    # 公共接口
    # =====================================================================

    async def add_positive_pattern(self, klines: List[Kline], profit_pct: float = 0.0,
                                   meta: Optional[Dict] = None) -> bool:
        """添加一个盈利形态，返回是否成功添加"""
        async with self._lock:
            return await self._add_pattern_unsafe(klines, profit_pct, meta)

    async def evaluate_similarity(self, recent_klines: List[Kline],
                                  volumes: Optional[List[float]] = None) -> SimilarityResult:
        """评估当前形态相似度"""
        async with self._lock:
            # 剪枝放在锁内，但限制数量
            self._prune_old_patterns()
            return await self._evaluate_unsafe(recent_klines, volumes)

    async def clear_cache(self) -> None:
        """清空形态缓存"""
        async with self._lock:
            self._cache.clear()
            self._total_memory_estimate = 0
            logger.info("Cache cleared.")

    @property
    def stats(self) -> Dict[str, Any]:
        """返回统计信息的快照（深拷贝）"""
        return {
            **deepcopy(self._stats),
            "cache_size": len(self._cache),
            "memory_mb": self._total_memory_estimate / (1024 * 1024),
        }

    def reset_stats(self) -> None:
        """重置统计计数器（注意：调用时不在锁内，简单操作）"""
        # 重置统计不会导致竞态，但为保证一致性，应在外部保证无并发调用
        self._stats = {k: 0 for k in self._stats}
        self._adaptive_band_hits = 0
        self._adaptive_band_trials = 0

    # =====================================================================
    # 内部实现
    # =====================================================================

    async def _add_pattern_unsafe(self, klines: List[Kline], profit_pct: float,
                                  meta: Optional[Dict]) -> bool:
        if self.max_pattern_count == 0:
            return False
        if not klines or len(klines) < 5:
            logger.warning("Pattern too short.")
            return False

        closes, vols = self._extract_closes_volumes(klines)
        if len(closes) < 5:
            return False

        arr = np.array(closes, dtype=np.float64)
        if np.std(arr) < self.flat_std_threshold:
            self._stats["total_skipped_flat"] += 1
            return False

        if self.smooth_window > 1:
            arr = self._smooth(arr, self.smooth_window)

        seq = self._normalize(arr)
        if len(seq) > self.max_sequence_length:
            seq = seq[-self.max_sequence_length:]

        paa = self._to_paa(seq, self.paa_segments)

        # 成交量曲线
        vol_profile = None
        if vols and self.enable_volume_weighted:
            vol_arr = np.array(vols, dtype=np.float64)
            vol_profile = self._normalize(vol_arr) if vol_arr.size > 0 else None

        # 形态去重：快速检查
        if self._cache:
            # 使用快速下界进行去重判断
            min_dist = self._find_nearest_fast(paa)
            # 阈值与序列长度相关
            threshold = self.duplicate_threshold_per_len * len(paa)
            if min_dist < threshold:
                self._stats["total_skipped_duplicate"] += 1
                logger.debug("Duplicate pattern ignored (min_dist=%.4f)", min_dist)
                return False

        # 内存控制
        new_size = paa.nbytes + (vol_profile.nbytes if vol_profile is not None else 0)
        while self._cache and self._total_memory_estimate + new_size > self.max_memory_bytes:
            removed = self._cache.popleft()
            removed_size = removed.paa.nbytes + (
                removed.volume_profile.nbytes if removed.volume_profile is not None else 0
            )
            self._total_memory_estimate -= removed_size
            self._stats["total_pruned"] += 1

        pattern_id = self._pattern_id_counter
        self._pattern_id_counter += 1
        meta_obj = PatternMeta(
            paa=paa,
            std=np.std(arr),
            profit_pct=profit_pct,
            timestamp=time.time(),
            volume_profile=vol_profile,
            pattern_id=pattern_id,
        )
        self._cache.append(meta_obj)
        self._total_memory_estimate += new_size
        self._stats["total_added"] += 1
        return True

    async def _evaluate_unsafe(self, recent_klines: List[Kline],
                               volumes: Optional[List[float]] = None) -> SimilarityResult:
        if not self._cache or not recent_klines or len(recent_klines) < 5:
            return SimilarityResult(0.0, float('inf'), float('inf'), 0)

        closes, curr_vols = self._extract_closes_volumes(recent_klines)
        if len(closes) < 5:
            return SimilarityResult(0.0, float('inf'), float('inf'), 0)

        arr = np.array(closes, dtype=np.float64)
        if np.std(arr) < self.flat_std_threshold:
            return SimilarityResult(0.0, float('inf'), float('inf'), 0)

        if self.smooth_window > 1:
            arr = self._smooth(arr, self.smooth_window)

        seq = self._normalize(arr)
        if len(seq) > self.max_sequence_length:
            seq = seq[-self.max_sequence_length:]

        current_paa = self._to_paa(seq, self.paa_segments)
        cur_len = len(current_paa)

        # 成交量曲线
        cur_vol_profile = None
        if curr_vols and self.enable_volume_weighted:
            vol_arr = np.array(curr_vols, dtype=np.float64)
            cur_vol_profile = self._normalize(vol_arr) if vol_arr.size > 0 else None

        # 自适应窗口
        if self.adaptive_band and self._adaptive_band_trials > 0:
            hit_rate = self._adaptive_band_hits / self._adaptive_band_trials
            band = self.sakoe_chiba_band + (0.05 if hit_rate < 0.3 else -0.02 if hit_rate > 0.7 else 0.0)
            band = max(0.05, min(0.3, band))
        else:
            band = self.sakoe_chiba_band

        window = max(1, int(cur_len * band)) if band > 0 else None
        if window is None:
            window = cur_len  # 无窗口时回退为全局

        cur_upper, cur_lower = self._compute_envelope(current_paa, window)

        distances = []
        details_list = []
        best_dist = float('inf')
        best_pattern = None

        for pattern in self._cache:
            min_len = min(cur_len, len(pattern.paa))
            if min_len < 3:
                continue
            cur = current_paa[:min_len]
            pat = pattern.paa[:min_len]

            # Keogh 剪枝
            if best_dist < float('inf'):
                lb = self._keogh_lower_bound(pat, cur_upper[:min_len], cur_lower[:min_len])
                if lb > best_dist * 1.1:
                    continue

            dist = self._dtw(cur, pat, window=window if window else min_len)
            # 成交量加权
            if self.enable_volume_weighted and cur_vol_profile is not None and pattern.volume_profile is not None:
                vol_dist = self._volume_distance(cur_vol_profile, pattern.volume_profile, min_len)
                dist = dist * 0.8 + vol_dist * 0.2

            distances.append(dist)
            if dist < best_dist:
                best_dist = dist
                best_pattern = pattern
            if len(details_list) < 20:
                details_list.append({"pattern_id": pattern.pattern_id, "distance": dist})

        # 反转匹配
        if self.enable_reverse_match:
            reversed_paa = current_paa[::-1]
            rev_upper, rev_lower = self._compute_envelope(reversed_paa, window)
            for pattern in self._cache:
                min_len = min(cur_len, len(pattern.paa))
                if min_len < 3:
                    continue
                rev = reversed_paa[:min_len]
                pat = pattern.paa[:min_len]
                if best_dist < float('inf'):
                    lb = self._keogh_lower_bound(pat, rev_upper[:min_len], rev_lower[:min_len])
                    if lb > best_dist * 1.1:
                        continue
                dist = self._dtw(rev, pat, window=window if window else min_len)
                distances.append(dist)
                if dist < best_dist:
                    best_dist = dist
                    best_pattern = pattern
                    self._stats["reverse_matches"] += 1

        if not distances:
            return SimilarityResult(0.0, float('inf'), float('inf'), 0)

        distances.sort()
        top_k = min(self.top_k, len(distances))
        avg_top_k_dist = np.mean(distances[:top_k])
        max_possible_dist = min_len * 2.0
        normalized_dist = avg_top_k_dist / max_possible_dist if max_possible_dist > 0 else 1.0
        similarity = np.exp(-normalized_dist * self.decay_factor)

        calibrated = None
        if self.similarity_calibration and best_pattern is not None:
            profit_factor = np.tanh(best_pattern.profit_pct / 10.0)
            calibrated = float(np.clip(similarity * (0.8 + 0.4 * profit_factor), 0.0, 1.0))

        self._stats["total_evaluated"] += 1
        self._adaptive_band_trials += 1
        if similarity > 0.7:
            self._adaptive_band_hits += 1

        return SimilarityResult(
            score=float(np.clip(similarity, 0.0, 1.0)),
            best_distance=best_dist,
            avg_top_k_distance=avg_top_k_dist,
            match_count=len(distances),
            calibrated_score=calibrated,
            details=details_list[:10] if details_list else None,
        )

    # ============== 辅助函数 ==============

    @staticmethod
    def _extract_closes_volumes(klines: List[Kline]) -> Tuple[List[float], List[float]]:
        closes = []
        vols = []
        for k in klines:
            if k is None or k.close is None or np.isnan(k.close) or np.isinf(k.close):
                continue
            closes.append(k.close)
            vol = k.volume
            if vol is not None and not np.isnan(vol) and not np.isinf(vol):
                vols.append(vol)
        return closes, vols

    @staticmethod
    def _normalize(seq: np.ndarray) -> np.ndarray:
        min_val, max_val = np.min(seq), np.max(seq)
        if max_val - min_val < 1e-12:
            return np.zeros_like(seq)
        return (seq - min_val) / (max_val - min_val)

    @staticmethod
    def _smooth(seq: np.ndarray, window: int) -> np.ndarray:
        if window < 2 or len(seq) < window:
            return seq
        kernel = np.ones(window) / window
        return np.convolve(seq, kernel, mode='same')

    def _to_paa(self, seq: np.ndarray, segments: int) -> np.ndarray:
        return self._resample(seq, segments)

    @staticmethod
    def _resample(seq: np.ndarray, segments: int) -> np.ndarray:
        n = len(seq)
        if n == segments:
            return seq.copy()
        x_orig = np.arange(n)
        x_new = np.linspace(0, n - 1, segments)
        return np.interp(x_new, x_orig, seq)

    def _find_nearest_fast(self, target_paa: np.ndarray) -> float:
        """快速查找最近距离（用于去重），利用包络线和少量样本"""
        if not self._cache:
            return float('inf')
        # 简单采样：取最近的 20 个形态进行精确比较
        recent_patterns = list(self._cache)[-20:]
        best = float('inf')
        t_len = len(target_paa)
        for pat in recent_patterns:
            min_len = min(t_len, len(pat.paa))
            if min_len < 3:
                continue
            # 使用小窗口快速 DTW
            dist = self._dtw(target_paa[:min_len], pat.paa[:min_len],
                             window=max(1, int(min_len * 0.1)))
            if dist < best:
                best = dist
        return best

    def _dtw(self, x: np.ndarray, y: np.ndarray, window: Optional[int] = None) -> float:
        n, m = len(x), len(y)
        if window is None:
            window = max(n, m)
        window = max(window, abs(n - m))

        cost = np.full((n + 1, m + 1), np.inf)
        cost[0, 0] = 0.0

        for i in range(1, n + 1):
            j_start = max(1, i - window)
            j_end = min(m, i + window)
            for j in range(j_start, j_end + 1):
                c = abs(x[i - 1] - y[j - 1])
                c = min(c, self.dtw_cost_limit)  # 安全截断
                prev = min(cost[i - 1, j], cost[i, j - 1], cost[i - 1, j - 1])
                val = c + prev
                cost[i, j] = min(val, self.dtw_cost_limit)  # 防止溢出
        return cost[n, m]

    @staticmethod
    def _compute_envelope(seq: np.ndarray, window: int) -> Tuple[np.ndarray, np.ndarray]:
        n = len(seq)
        upper = np.array([max(seq[max(0, i - window): min(n, i + window + 1)]) for i in range(n)])
        lower = np.array([min(seq[max(0, i - window): min(n, i + window + 1)]) for i in range(n)])
        return upper, lower

    @staticmethod
    def _keogh_lower_bound(candidate: np.ndarray, upper: np.ndarray, lower: np.ndarray) -> float:
        diff = np.maximum(0, candidate - upper) + np.maximum(0, lower - candidate)
        return np.sqrt(np.sum(diff ** 2))

    @staticmethod
    def _volume_distance(vol1: np.ndarray, vol2: np.ndarray, length: int) -> float:
        v1 = vol1[:length] if len(vol1) >= length else np.pad(vol1, (0, length - len(vol1)), 'constant', constant_values=0)
        v2 = vol2[:length] if len(vol2) >= length else np.pad(vol2, (0, length - len(vol2)), 'constant', constant_values=0)
        return np.sqrt(np.sum((v1 - v2) ** 2))

    def _prune_old_patterns(self) -> None:
        if self.max_pattern_age_seconds <= 0 or not self._cache:
            return
        now = time.time()
        # 限制每次剪枝处理数量，避免长时间持锁
        removed_indices = []
        count = 0
        for i, item in enumerate(self._cache):
            if now - item.timestamp > self.max_pattern_age_seconds:
                removed_indices.append(i)
                count += 1
                if count >= self.prune_batch_size:
                    break
        if not removed_indices:
            return
        # 从右向左删除，避免索引错乱
        new_cache = deque(maxlen=self._cache.maxlen)
        for i, item in enumerate(self._cache):
            if i in removed_indices:
                self._stats["total_pruned"] += 1
                self._total_memory_estimate -= (item.paa.nbytes + (item.volume_profile.nbytes if item.volume_profile is not None else 0))
            else:
                new_cache.append(item)
        self._cache = new_cache
        logger.debug("Pruned %d old patterns", len(removed_indices))
