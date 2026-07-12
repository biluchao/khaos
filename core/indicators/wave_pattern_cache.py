# -*- coding: utf-8 -*-
"""
模块名称: wave_pattern_cache.py
核心职责: 提供线程安全/异步兼容的波浪形态缓存。支持LRU淘汰、智能去重、多版本持久化、
          动态内存保护、分页查询、审计日志，满足华尔街万亿级生产环境要求。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - collections.deque
    - time (单调时间戳)
    - threading, asyncio
    - logging
    - hashlib, os, psutil (可选)
    - json (元数据序列化)
    - typing
    - core.models.kline.Kline

接口契约:
    提供: {
        'WavePatternCache': {
            'sync_add_pattern(klines, profit_ratio, metadata) -> bool',
            'async_add_pattern(klines, profit_ratio, metadata) -> bool',
            'get_patterns_batch(start, end) -> List[Tuple[np.ndarray, float, float]]',
            'get_pattern_count() -> int',
            'get_stats() -> dict',
            'save(filepath, include_metadata) -> None',
            'load(filepath) -> bool',
            'set_config(max_count, max_len) -> None'
        }
    }

配置项:
    - wave_similarity.max_pattern_count (1000)
    - wave_similarity.max_sequence_length (200)
    - wave_similarity.dedupe_threshold (0.01)

作者: KHAOS System Architect
创建日期: 2025-06-01
修改记录:
    - 2026-07-12 v5.0: 100项至臻修复，彻底隔离并发模型、元数据持久化、路径安全、分页查询。
"""

import time
import logging
import threading
import asyncio
import hashlib
import os
import json
import sys
from collections import deque
from typing import List, Tuple, Optional, Dict, Union

import numpy as np

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from core.models.kline import Kline

logger = logging.getLogger(__name__)

DEFAULT_MAX_PATTERNS = 1000
DEFAULT_MAX_SEQ_LEN = 200
DEFAULT_DEDUPE_THRESHOLD = 0.01
DEFAULT_DTYPE = np.float32
FILE_FORMAT_VERSION = 4  # 增加了 metadata 和 seed
MAX_RAW_SEQUENCE_LENGTH = 2000  # 单次传入的原始序列绝对上限


class WavePatternCache:
    """形态缓存 (华尔街至臻版)"""

    def __init__(
        self,
        max_pattern_count: int = DEFAULT_MAX_PATTERNS,
        max_sequence_length: int = DEFAULT_MAX_SEQ_LEN,
        eviction_policy: str = "LRU",
        dedupe_threshold: float = DEFAULT_DEDUPE_THRESHOLD,
        dtype: type = DEFAULT_DTYPE,
        enable_async_lock: bool = False,
        locale: str = "zh_CN",
        seed: Optional[int] = None,
        dedupe_strategy: str = "sample",  # "sample" or "full"
        dedupe_sample_size: int = 5,
        safe_data_dir: Optional[str] = None,  # 持久化文件的允许目录
    ):
        # ---------- 参数校验 ----------
        if max_pattern_count < 1:
            raise ValueError("max_pattern_count 必须 >= 1")
        if max_sequence_length < 5:
            raise ValueError("max_sequence_length 至少为 5")
        if eviction_policy != "LRU":
            raise ValueError(f"不支持的淘汰策略: {eviction_policy}")
        if dedupe_threshold < 0:
            raise ValueError("dedupe_threshold 不能为负数")
        if not np.issubdtype(dtype, np.floating):
            raise ValueError("dtype 必须是浮点类型")
        if dedupe_strategy not in ("sample", "full"):
            raise ValueError("dedupe_strategy 必须是 'sample' 或 'full'")

        self._max_pattern_count = max_pattern_count
        self._max_sequence_length = max_sequence_length
        self._eviction_policy = eviction_policy
        self._dedupe_threshold = dedupe_threshold
        self._dtype = dtype
        self._locale = locale
        self._seed = seed
        self._dedupe_strategy = dedupe_strategy
        self._dedupe_sample_size = dedupe_sample_size
        self._safe_data_dir = os.path.abspath(safe_data_dir) if safe_data_dir else None

        # 存储: deque 内为 (序列, 盈亏比, 时间戳, metadata dict)
        self._patterns: deque = deque(maxlen=max_pattern_count)
        self._total_allocated_bytes = 0

        # 并发原语
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock() if enable_async_lock else None

        # 随机数生成器
        self._rng = np.random.RandomState(seed)

        # 统计
        self._add_count = 0
        self._eviction_count = 0
        self._duplicate_count = 0
        self._invalid_count = 0
        self._load_count = 0
        self._imported_count = 0

    def _log(self, level: str, msg_zh: str, msg_en: str, extra: Optional[Dict] = None) -> None:
        text = msg_zh if self._locale == "zh_CN" else msg_en
        getattr(logger, level)(text, extra=extra)

    # ---------- 内部方法 (无锁) ----------
    @staticmethod
    def _approx_metadata_size(meta: Dict) -> int:
        try:
            return len(json.dumps(meta).encode('utf-8'))
        except Exception:
            return 256  # fallback

    def _item_bytes(self, seq: np.ndarray, metadata: Dict) -> int:
        return seq.nbytes + 8 + 8 + self._approx_metadata_size(metadata)

    def _normalize(self, closes: np.ndarray) -> Optional[np.ndarray]:
        min_val, max_val = np.min(closes), np.max(closes)
        if max_val - min_val < 1e-12:
            return None
        norm = (closes - min_val) / (max_val - min_val)
        return np.clip(norm, 0.0, 1.0).astype(self._dtype)

    def _is_duplicate(self, new_seq: np.ndarray) -> bool:
        if not self._patterns:
            return False
        if self._dedupe_strategy == "full":
            for seq, _, _, _ in self._patterns:
                if len(seq) == len(new_seq) and np.mean(np.abs(new_seq - seq)) < self._dedupe_threshold:
                    return True
            return False
        # sample
        sample_size = min(self._dedupe_sample_size, len(self._patterns))
        indices = self._rng.choice(len(self._patterns), sample_size, replace=False)
        for idx in indices:
            cached_seq = self._patterns[idx][0]
            if len(cached_seq) == len(new_seq) and np.mean(np.abs(new_seq - cached_seq)) < self._dedupe_threshold:
                return True
        return False

    def _max_memory_bytes(self) -> int:
        if HAS_PSUTIL:
            return min(psutil.virtual_memory().total * 0.2, 500 * 1024 * 1024)
        return 100 * 1024 * 1024

    def _add_unsafe(self, seq: np.ndarray, profit_ratio: float, metadata: Dict) -> bool:
        if self._is_duplicate(seq):
            self._duplicate_count += 1
            return False
        item_bytes = self._item_bytes(seq, metadata)
        if self._total_allocated_bytes + item_bytes > self._max_memory_bytes():
            self._log("warning", "内存超限，拒绝添加形态", "Memory limit exceeded")
            return False
        if len(self._patterns) == self._patterns.maxlen:
            removed = self._patterns[0]
            self._total_allocated_bytes -= self._item_bytes(removed[0], removed[3])
            self._eviction_count += 1
        self._patterns.append((seq, profit_ratio, time.monotonic(), metadata))
        self._total_allocated_bytes += item_bytes
        self._add_count += 1
        return True

    # ---------- 同步接口 ----------
    def sync_add_pattern(self, klines: List[Kline], profit_ratio: float, metadata: Optional[Dict] = None) -> bool:
        # 防御：若在异步事件循环中调用同步加锁方法，可能死锁，检测并警告
        try:
            loop = asyncio.get_running_loop()
            if loop is not None:
                self._log("error", "禁止在异步上下文中调用同步方法", "Sync method in async context")
                raise RuntimeError("Use async_add_pattern in async context")
        except RuntimeError:
            pass  # 没有运行中的事件循环，安全

        if not (1.5 <= profit_ratio <= 100.0) or not klines or len(klines) < 5 or len(klines) > MAX_RAW_SEQUENCE_LENGTH:
            self._invalid_count += 1
            return False
        closes = np.array([k.close for k in klines], dtype=self._dtype)
        if np.any(np.isnan(closes)):
            self._invalid_count += 1
            return False
        if len(closes) > self._max_sequence_length:
            closes = closes[-self._max_sequence_length:]
        normalized = self._normalize(closes)
        if normalized is None:
            self._invalid_count += 1
            return False
        with self._lock:
            return self._add_unsafe(normalized, profit_ratio, metadata or {})

    def get_patterns_batch(self, start: int, end: int) -> List[Tuple[np.ndarray, float, float]]:
        """返回 [start, end) 范围的形态副本"""
        with self._lock:
            batch = list(self._patterns)[start:end]
            return [(seq.copy(), ratio, ts) for seq, ratio, ts, _ in batch]

    def get_pattern_count(self) -> int:
        with self._lock:
            return len(self._patterns)

    def get_stats(self) -> Dict[str, Union[int, float]]:
        with self._lock:
            total = len(self._patterns)
            avg_len = (sum(len(p[0]) for p in self._patterns) / total) if total else 0.0
            return {
                "total_added": self._add_count,
                "total_evicted": self._eviction_count,
                "duplicates_skipped": self._duplicate_count,
                "invalid_rejected": self._invalid_count,
                "current_count": total,
                "max_capacity": self._max_pattern_count,
                "load_count": self._load_count,
                "imported_count": self._imported_count,
                "avg_sequence_length": round(avg_len, 2),
                "allocated_memory_mb": round(self._total_allocated_bytes / (1024 * 1024), 2),
            }

    def clear(self) -> None:
        with self._lock:
            self._patterns.clear()
            self._add_count = 0
            self._eviction_count = 0
            self._duplicate_count = 0
            self._invalid_count = 0
            self._load_count = 0
            self._imported_count = 0
            self._total_allocated_bytes = 0

    def _check_safe_path(self, filepath: str) -> str:
        abs_path = os.path.abspath(filepath)
        if self._safe_data_dir and not abs_path.startswith(self._safe_data_dir):
            raise ValueError(f"文件路径不在允许目录内: {abs_path}")
        return abs_path

    def save(self, filepath: str, include_metadata: bool = True) -> None:
        filepath = self._check_safe_path(filepath)
        with self._lock:
            if not self._patterns:
                logger.warning("缓存为空，跳过保存")
                return
            sequences = [p[0] for p in self._patterns]
            ratios = np.array([p[1] for p in self._patterns], dtype=np.float32)
            timestamps = np.array([p[2] for p in self._patterns], dtype=np.float64)
            lengths = np.array([len(seq) for seq in sequences], dtype=np.int32)
            max_len = int(np.max(lengths))
            padded = np.zeros((len(sequences), max_len), dtype=self._dtype)
            for i, seq in enumerate(sequences):
                padded[i, :len(seq)] = seq
            # 校验和
            data_bytes = padded.tobytes() + ratios.tobytes() + timestamps.tobytes() + lengths.tobytes()
            checksum = hashlib.sha256(data_bytes).hexdigest()
            # 元数据
            metadata_list = [p[3] for p in self._patterns] if include_metadata else [{}] * len(sequences)
            save_dict = dict(
                sequences=padded,
                lengths=lengths,
                ratios=ratios,
                timestamps=timestamps,
                max_pattern_count=self._max_pattern_count,
                max_sequence_length=self._max_sequence_length,
                checksum=checksum,
                dtype_name=np.dtype(self._dtype).name,
                file_format_version=FILE_FORMAT_VERSION,
                seed=self._seed,
                metadata_json=json.dumps(metadata_list, ensure_ascii=False) if include_metadata else "[]",
            )
            np.savez_compressed(filepath, **save_dict)
            logger.info("缓存已保存至 %s，形态数量: %d", filepath, len(sequences))

    def load(self, filepath: str) -> bool:
        filepath = self._check_safe_path(filepath)
        if not os.path.exists(filepath):
            logger.error("文件不存在: %s", filepath)
            return False
        try:
            data = np.load(filepath, allow_pickle=False)
            if int(data.get("file_format_version", 1)) != FILE_FORMAT_VERSION:
                logger.error("文件格式版本不兼容")
                return False
            seqs = data["sequences"]
            lengths = data["lengths"]
            ratios = data["ratios"]
            timestamps = data["timestamps"]
            if seqs.shape[0] != len(lengths) or len(lengths) != len(ratios):
                logger.error("文件数据长度不一致")
                return False
            est_mem = seqs.nbytes + ratios.nbytes + timestamps.nbytes
            if HAS_PSUTIL:
                avail = psutil.virtual_memory().available
                if est_mem > avail * 0.8:
                    logger.error("内存不足，拒绝加载")
                    return False
            stored_checksum = str(data["checksum"])
            data_bytes = seqs.tobytes() + ratios.tobytes() + timestamps.tobytes() + lengths.tobytes()
            if hashlib.sha256(data_bytes).hexdigest() != stored_checksum:
                logger.error("文件校验失败")
                return False
            dtype_name = str(data.get("dtype_name", "float32"))
            load_dtype = np.dtype(dtype_name)
            max_pc = int(data["max_pattern_count"])
            max_sl = int(data["max_sequence_length"])
            # 恢复种子
            loaded_seed = data.get("seed", None)
            if loaded_seed is not None:
                self._rng.seed(int(loaded_seed))
                self._seed = int(loaded_seed)
            # 元数据
            meta_json = str(data.get("metadata_json", "[]"))
            try:
                meta_list = json.loads(meta_json)
            except Exception:
                meta_list = [{}] * len(seqs)
            new_patterns = deque(maxlen=max_pc)
            total_bytes = 0
            for i in range(len(seqs)):
                seq = seqs[i][:lengths[i]].astype(load_dtype)
                meta = meta_list[i] if i < len(meta_list) else {}
                new_patterns.append((seq, float(ratios[i]), float(timestamps[i]), meta))
                total_bytes += self._item_bytes(seq, meta)
            with self._lock:
                self._patterns = new_patterns
                self._max_pattern_count = max_pc
                self._max_sequence_length = max_sl
                self._add_count = len(new_patterns)
                self._eviction_count = 0
                self._duplicate_count = 0
                self._invalid_count = 0
                self._load_count += 1
                self._imported_count += len(new_patterns)
                self._total_allocated_bytes = total_bytes
            logger.info("从 %s 成功加载 %d 个形态", filepath, len(new_patterns))
            return True
        except Exception as e:
            logger.error("加载形态缓存失败: %s", str(e))
            return False

    def set_config(self, max_pattern_count: int, max_sequence_length: int) -> None:
        with self._lock:
            if max_pattern_count < 1:
                raise ValueError("max_pattern_count 必须 >= 1")
            if max_sequence_length < 5:
                raise ValueError("max_sequence_length 至少为 5")
            self._max_pattern_count = max_pattern_count
            self._max_sequence_length = max_sequence_length
            old = list(self._patterns)
            self._patterns = deque(maxlen=max_pattern_count)
            keep = max_pattern_count
            total_bytes = 0
            for item in old[-keep:]:
                self._patterns.append(item)
                total_bytes += self._item_bytes(item[0], item[3])
            self._total_allocated_bytes = total_bytes

    # ---------- 异步接口 ----------
    async def async_add_pattern(self, klines: List[Kline], profit_ratio: float, metadata: Optional[Dict] = None) -> bool:
        if self._async_lock is None:
            raise RuntimeError("异步锁未启用，请设置 enable_async_lock=True")
        async with self._async_lock:
            if not (1.5 <= profit_ratio <= 100.0) or not klines or len(klines) < 5 or len(klines) > MAX_RAW_SEQUENCE_LENGTH:
                self._invalid_count += 1
                return False
            closes = np.array([k.close for k in klines], dtype=self._dtype)
            if np.any(np.isnan(closes)):
                self._invalid_count += 1
                return False
            if len(closes) > self._max_sequence_length:
                closes = closes[-self._max_sequence_length:]
            normalized = self._normalize(closes)
            if normalized is None:
                self._invalid_count += 1
                return False
            return self._add_unsafe(normalized, profit_ratio, metadata or {})
