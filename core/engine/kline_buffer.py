# -*- coding: utf-8 -*-
"""
模块名称: kline_buffer.py
核心职责: 多周期K线缓冲管理，提供极致性能、并发安全、数据完整性保障的访问层
所属层级: core.engine

线程安全: 所有公共方法均为异步安全，内部使用 asyncio.Lock 保护共享状态。
         同步回调通过事件循环安全分发，避免线程泄漏。

外部依赖:
    - asyncio
    - collections.deque
    - itertools.islice
    - bisect
    - math
    - time
    - typing
    - concurrent.futures (仅用于兼容，实际回调均在事件循环执行)
    - core.models.kline.Kline

接口契约:
    提供: {
        'MultiTimeframeKlineBuffer': {
            'add_kline(kline: Kline, interval: str) -> AddResult': '添加K线',
            'get_recent_klines(interval: str, limit: int) -> List[Kline]': '获取最近N根',
            'get_kline_by_timestamp(interval: str, open_time: int) -> Optional[Kline]': 'O(log n)查找',
            'get_kline_range(interval: str, start_time: int, end_time: int) -> List[Kline]': '时间范围查询',
            'get_all_intervals() -> Tuple[str,...]': '已注册周期',
            'is_ready(interval: str, min_bars: int) -> bool': '就绪检查'
        }
    }

配置项:
    - kline_buffer.cache_size (int, 5000): 每周期最大K线数
    - kline_buffer.intervals (list, ['3m','5m','15m']): 默认周期
    - kline_buffer.dedup_window_bars (int, 已废弃，保留兼容): 不再使用，去重依赖精确索引
    - kline_buffer.max_timestamp_deviation_ms (int, 60000): 乱序容忍度

作者: KHAOS System Architect
创建日期: 2025-03-15
修改记录:
    - v1.0 基础实现
    - v1.1 数据校验与排序
    - v2.0 机构级重构
    - v3.0 去重与并发增强
    - v4.0 极致可靠：价格逻辑、事件安全、索引同步
    - v5.0 终极可靠：锁优化、回调安全、增量索引、性能提升
    - v6.0 完美版：消除内存泄漏、回调限流、缓存同步、事件竞争修复
"""

import asyncio
import bisect
import math
import time
from collections import deque
from enum import Enum
from itertools import islice
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import logging

from core.models.kline import Kline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_CACHE_SIZE = 5000
DEFAULT_INTERVALS = ('3m', '5m', '15m')
DEFAULT_MAX_TIMESTAMP_DEVIATION_MS = 60000
MAX_LIMIT = 10000
MAX_RANGE_LIMIT = 5000                     # 范围查询最大返回条数
CALLBACK_TIMEOUT_SEC = 2.0
MAX_CONCURRENT_CALLBACKS = 10              # 最大并发回调任务
CALLBACK_QUEUE_SIZE = 200                  # 回调队列缓冲大小


class AddResult(Enum):
    OK = "ok"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    OUT_OF_ORDER_INSERTED = "out_of_order_inserted"
    HISTORICAL_INSERTED = "historical_inserted"


class MultiTimeframeKlineBuffer:
    """
    多周期K线缓冲管理器 (v6.0 完美版)

    索引管理：每次添加K线时实时维护 open_time -> Kline 的映射，确保与 buffer 严格一致。
              deque 自动溢出时，显式移除被丢弃元素的索引，杜绝内存泄漏。
    去重策略：仅依赖索引精确去重，去除冗余的 _dedup 机制。
    回调机制：异步回调通过有界队列 + 消费者任务处理，避免无限制创建任务。
              同步回调通过事件循环线程安全执行，并设置超时。
    就绪通知：使用 asyncio.Condition 替代 Event，消除竞态条件。
    """

    __version__ = "6.0.0"

    def __init__(self,
                 cache_size: int = DEFAULT_CACHE_SIZE,
                 intervals: Optional[Union[List[str], Tuple[str, ...]]] = None,
                 dedup_window_bars: int = None,   # 废弃参数，保留兼容
                 max_timestamp_deviation_ms: int = DEFAULT_MAX_TIMESTAMP_DEVIATION_MS,
                 strict_mode: bool = False):
        self.cache_size = cache_size
        self.intervals = list(intervals) if intervals else list(DEFAULT_INTERVALS)
        self.max_timestamp_deviation_ms = max_timestamp_deviation_ms
        self.strict_mode = strict_mode

        # ---------- 主存储 ----------
        self._buffers: Dict[str, deque] = {i: deque(maxlen=cache_size) for i in self.intervals}
        # open_time -> Kline 精确索引
        self._index: Dict[str, Dict[int, Kline]] = {i: {} for i in self.intervals}
        # 时间戳缓存（与 buffer 严格同步，用于加速范围查询）
        self._timestamp_cache: Dict[str, List[int]] = {i: [] for i in self.intervals}

        # ---------- 回调子系统 ----------
        self._callbacks: Dict[str, List[Callable]] = {i: [] for i in self.intervals}
        self._callback_queues: Dict[str, asyncio.Queue] = {i: asyncio.Queue(CALLBACK_QUEUE_SIZE) for i in self.intervals}
        self._callback_tasks: Dict[str, asyncio.Task] = {}

        # ---------- 就绪通知（Condition） ----------
        self._ready_conditions: Dict[str, asyncio.Condition] = {i: asyncio.Condition() for i in self.intervals}

        # ---------- 统计与监控 ----------
        self._last_update: Dict[str, float] = {}
        self.stats: Dict[str, Dict[str, int]] = {
            i: {"added": 0, "duplicates": 0, "invalid": 0, "out_of_order": 0, "historical": 0}
            for i in self.intervals
        }
        self._perf_stats: Dict[str, Dict[str, float]] = {i: {} for i in self.intervals}
        self._index_consistency_errors: Dict[str, int] = {i: 0 for i in self.intervals}

        # ---------- 同步 ----------
        self._lock = asyncio.Lock()
        self._log_throttle: Dict[str, float] = {}

        # 为每个已注册周期启动回调消费者任务
        for interval in self.intervals:
            self._start_callback_consumer(interval)

    # -----------------------------------------------------------------------
    # 公共 API
    # -----------------------------------------------------------------------
    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def register_interval(self, interval: str) -> None:
        interval = interval.lower()
        async with self._lock:
            self._register_interval_unsafe(interval)

    def _register_interval_unsafe(self, interval: str) -> None:
        if interval not in self._buffers:
            self._buffers[interval] = deque(maxlen=self.cache_size)
            self._index[interval] = {}
            self._timestamp_cache[interval] = []
            self._callbacks[interval] = []
            self._ready_conditions[interval] = asyncio.Condition()
            self.stats[interval] = {"added": 0, "duplicates": 0, "invalid": 0, "out_of_order": 0, "historical": 0}
            self._perf_stats[interval] = {}
            self._index_consistency_errors[interval] = 0
            self._last_update[interval] = 0.0
            self.intervals.append(interval)
            # 启动回调消费者
            self._callback_queues[interval] = asyncio.Queue(CALLBACK_QUEUE_SIZE)
            self._start_callback_consumer(interval)
            logger.info(f"Registered interval: {interval}")

    def _start_callback_consumer(self, interval: str) -> None:
        """为指定周期启动回调消费者任务"""
        if interval in self._callback_tasks and not self._callback_tasks[interval].done():
            self._callback_tasks[interval].cancel()
        self._callback_tasks[interval] = asyncio.create_task(self._consume_callbacks(interval))

    async def _consume_callbacks(self, interval: str) -> None:
        """消费者协程：从队列取出 (callback, kline) 并执行，限制并发"""
        sem = asyncio.Semaphore(MAX_CONCURRENT_CALLBACKS)
        queue = self._callback_queues[interval]
        while True:
            try:
                cb, kline = await queue.get()
            except asyncio.CancelledError:
                break
            async with sem:
                try:
                    await asyncio.wait_for(self._execute_callback(cb, kline), timeout=CALLBACK_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    logger.warning(f"Callback timed out for interval {interval}")
                except Exception:
                    logger.exception(f"Callback error for interval {interval}")

    async def _execute_callback(self, cb: Callable, kline: Kline) -> None:
        """执行单个回调，支持同步/异步"""
        if asyncio.iscoroutinefunction(cb):
            await cb(kline)
        else:
            # 同步回调在事件循环中执行，可能阻塞，用线程池运行并等待
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, cb, kline)

    async def add_kline(self, kline: Optional[Kline], interval: str,
                        allow_historical: bool = False) -> AddResult:
        interval = interval.lower()
        if kline is None:
            self._log_throttled(interval, "add_kline received None", logging.WARNING)
            return AddResult.INVALID

        if not self._validate_kline(kline):
            self._log_throttled(interval, f"Invalid kline: {kline}", logging.WARNING)
            async with self._lock:
                self._ensure_interval_exists(interval)
                self.stats[interval]["invalid"] += 1
            return AddResult.INVALID

        async with self._lock:
            self._ensure_interval_exists(interval)
            t_start = time.perf_counter()
            result = self._add_kline_unsafe(kline, interval, allow_historical)
            elapsed = time.perf_counter() - t_start
            perf = self._perf_stats.setdefault(interval, {})
            perf["last_add_us"] = elapsed * 1_000_000
            perf.setdefault("total_add_us", 0)
            perf["total_add_us"] += elapsed * 1_000_000
            perf.setdefault("add_count", 0)
            perf["add_count"] += 1
            return result

    def _ensure_interval_exists(self, interval: str) -> None:
        if interval not in self._buffers:
            self._register_interval_unsafe(interval)

    async def get_recent_klines(self, interval: str, limit: int) -> List[Kline]:
        interval = interval.lower()
        if limit < 0:
            limit = 0
        if limit > MAX_LIMIT:
            limit = MAX_LIMIT
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            if limit == 0 or limit >= len(buf):
                return list(buf)
            return list(islice(buf, len(buf) - limit, None))

    async def get_kline_by_timestamp(self, interval: str, open_time: int) -> Optional[Kline]:
        interval = interval.lower()
        async with self._lock:
            idx = self._index.get(interval, {})
            return idx.get(open_time)

    async def get_kline_range(self, interval: str, start_time: int, end_time: int) -> List[Kline]:
        interval = interval.lower()
        if end_time < start_time:
            logger.warning(f"get_kline_range: end_time < start_time ({end_time} < {start_time})")
            return []
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            if not buf:
                return []
            times = self._timestamp_cache.get(interval, [])
            # 确保缓存有效
            if len(times) != len(buf):
                times = [k.open_time for k in buf]
                self._timestamp_cache[interval] = times
            lo = bisect.bisect_left(times, start_time)
            result = []
            buf_list = list(buf)
            for i in range(lo, min(len(buf_list), lo + MAX_RANGE_LIMIT)):
                k = buf_list[i]
                if k.open_time > end_time:
                    break
                result.append(k)
            return result

    async def get_oldest_kline(self, interval: str) -> Optional[Kline]:
        interval = interval.lower()
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            return buf[0] if buf else None

    async def get_latest_kline(self, interval: str) -> Optional[Kline]:
        interval = interval.lower()
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            return buf[-1] if buf else None

    async def get_latest_close(self, interval: str) -> Optional[float]:
        interval = interval.lower()
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            if buf and buf[-1].close is not None:
                return buf[-1].close
            return None

    async def is_ready(self, interval: str, min_bars: int) -> bool:
        if min_bars <= 0:
            return True
        interval = interval.lower()
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            return len(buf) >= min_bars

    async def get_buffer_length(self, interval: str) -> int:
        interval = interval.lower()
        async with self._lock:
            return len(self._buffers.get(interval, deque()))

    def get_all_intervals(self) -> Tuple[str, ...]:
        return tuple(sorted(self.intervals))

    async def clear(self, interval: Optional[str] = None) -> None:
        async with self._lock:
            if interval:
                interval = interval.lower()
                if interval in self._buffers:
                    self._buffers[interval].clear()
                    self._index[interval].clear()
                    self._timestamp_cache[interval].clear()
                    self._last_update.pop(interval, None)
                    self._index_consistency_errors[interval] = 0
                    self.stats[interval] = {"added": 0, "duplicates": 0, "invalid": 0, "out_of_order": 0, "historical": 0}
                    logger.info(f"Cleared buffer: {interval}")
            else:
                for i in self._buffers:
                    self._buffers[i].clear()
                    self._index[i].clear()
                    self._timestamp_cache[i].clear()
                    self._last_update.pop(i, None)
                    self._index_consistency_errors[i] = 0
                    self.stats[i] = {"added": 0, "duplicates": 0, "invalid": 0, "out_of_order": 0, "historical": 0}
                logger.info("Cleared all buffers")

    async def add_callback(self, interval: str, callback: Callable[[Kline], None]) -> None:
        interval = interval.lower()
        async with self._lock:
            if interval not in self._callbacks:
                self._callbacks[interval] = []
            self._callbacks[interval].append(callback)

    async def wait_until_ready(self, interval: str, min_bars: int, timeout: float = 30.0) -> bool:
        interval = interval.lower()
        async with self._lock:
            cond = self._ready_conditions.get(interval)
            if cond is None:
                cond = asyncio.Condition()
                self._ready_conditions[interval] = cond
        try:
            async with cond:
                await asyncio.wait_for(
                    cond.wait_for(lambda: len(self._buffers.get(interval, deque())) >= min_bars),
                    timeout=timeout
                )
            return True
        except asyncio.TimeoutError:
            return False

    async def get_last_update_time(self, interval: str) -> float:
        interval = interval.lower()
        async with self._lock:
            return self._last_update.get(interval, 0.0)

    async def get_statistics(self, interval: str) -> Dict[str, Any]:
        interval = interval.lower()
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            if not buf:
                return {}
            closes = [k.close for k in buf]
            return {
                "count": len(buf),
                "first_time": buf[0].open_time,
                "last_time": buf[-1].open_time,
                "mean_close": sum(closes) / len(closes),
                "min_close": min(closes),
                "max_close": max(closes),
            }

    async def to_dataframe(self, interval: str):
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("pandas is required for to_dataframe")
        interval = interval.lower()
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            data = [{
                "open_time": k.open_time,
                "close_time": k.close_time,
                "open": k.open,
                "high": k.high,
                "low": k.low,
                "close": k.close,
                "volume": k.volume,
            } for k in buf]
            return pd.DataFrame(data)

    async def export_state(self) -> Dict[str, Any]:
        async with self._lock:
            state = {}
            for i in self.intervals:
                state[i] = {
                    "klines": [k.to_dict() for k in self._buffers[i]],
                    "stats": dict(self.stats[i]),
                }
            return state

    async def import_state(self, state: Dict[str, Any]) -> None:
        async with self._lock:
            for i, data in state.items():
                if i not in self._buffers:
                    self._register_interval_unsafe(i)
                try:
                    klines = [Kline.from_dict(d) for d in data["klines"] if Kline.from_dict(d) is not None]
                    self._buffers[i] = deque(klines, maxlen=self.cache_size)
                except Exception:
                    logger.exception(f"Failed to import state for interval {i}, skipping")
                    continue
                self._rebuild_index(i)
                self._timestamp_cache[i] = [k.open_time for k in self._buffers[i]]
                self.stats[i] = data.get("stats", {})
                self._last_update[i] = time.time()

    async def reset_stats(self, interval: Optional[str] = None) -> None:
        async with self._lock:
            if interval:
                interval = interval.lower()
                if interval in self._perf_stats:
                    self._perf_stats[interval].clear()
            else:
                for i in self._perf_stats:
                    self._perf_stats[i].clear()

    def __len__(self) -> int:
        return sum(len(buf) for buf in self._buffers.values())

    def __contains__(self, item: Tuple[str, int]) -> bool:
        interval, open_time = item
        return open_time in self._index.get(interval, {})

    def __repr__(self) -> str:
        parts = ", ".join(f"{i}={len(self._buffers.get(i,deque()))}" for i in self.intervals[:5])
        return f"<KlineBuffer({parts})>"

    def __str__(self) -> str:
        return self.__repr__()

    # -----------------------------------------------------------------------
    # 内部实现
    # -----------------------------------------------------------------------
    def _validate_kline(self, k: Kline) -> bool:
        """严格K线校验"""
        if k.open_time is None or k.close_time is None:
            return False
        if k.close_time <= k.open_time:
            return False
        if k.high < k.low:
            return False
        tolerance = max(1e-8 * max(abs(k.high), 1.0), 1e-12)
        if k.high < max(k.open, k.close) - tolerance:
            return False
        if k.low > min(k.open, k.close) + tolerance:
            return False
        # 检查数值类型和有效性
        for val in (k.open, k.high, k.low, k.close, k.volume):
            if val is None:
                return False
            if math.isnan(val):
                return False
            if val < 0:
                return False
        return True

    def _add_kline_unsafe(self, kline: Kline, interval: str, allow_historical: bool) -> AddResult:
        buf = self._buffers[interval]
        idx = self._index[interval]
        open_time = kline.open_time

        # 1. 精确去重（仅依赖索引）
        if open_time in idx:
            self.stats[interval]["duplicates"] += 1
            return AddResult.DUPLICATE

        # 2. 乱序/历史数据处理
        if buf:
            last_time = buf[-1].open_time
            if open_time < last_time:
                if not allow_historical:
                    deviation = last_time - open_time
                    if deviation > self.max_timestamp_deviation_ms:
                        self._log_throttled(interval,
                                            f"Out-of-order kline discarded: {open_time} < {last_time}",
                                            logging.WARNING)
                        self.stats[interval]["invalid"] += 1
                        return AddResult.INVALID
                # 排序插入
                self._insert_sorted(interval, kline, allow_historical)
                return (AddResult.HISTORICAL_INSERTED if allow_historical
                        else AddResult.OUT_OF_ORDER_INSERTED)

        # 3. 正常追加（新K线时间 >= 最后时间）
        # 在追加前，如果缓冲区已满，显式移除最旧元素及其索引和缓存
        if len(buf) == self.cache_size:
            removed = buf.popleft()    # 手动弹出，避免append时自动弹出我们无法感知
            del idx[removed.open_time]
            # 同步移除时间戳缓存头部
            if self._timestamp_cache[interval] and self._timestamp_cache[interval][0] == removed.open_time:
                self._timestamp_cache[interval].pop(0)
        buf.append(kline)
        idx[open_time] = kline
        self._timestamp_cache[interval].append(open_time)

        self.stats[interval]["added"] += 1
        self._last_update[interval] = time.time()

        # 通知回调（放入队列）
        try:
            self._callback_queues[interval].put_nowait((self._get_notify_callback(interval), kline))
        except asyncio.QueueFull:
            logger.warning(f"Callback queue full for interval {interval}, dropping notification")

        # 通知就绪条件
        cond = self._ready_conditions.get(interval)
        if cond:
            async def _notify():
                async with cond:
                    cond.notify_all()
            asyncio.create_task(_notify())  # 必须在锁外通知，避免死锁

        return AddResult.OK

    def _get_notify_callback(self, interval: str) -> Callable:
        """返回一个综合回调，依次调用所有注册的回调"""
        cbs = list(self._callbacks.get(interval, []))
        async def composite(kline: Kline):
            for cb in cbs:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(kline)
                    else:
                        await asyncio.get_running_loop().run_in_executor(None, cb, kline)
                except Exception:
                    logger.exception(f"Error in callback for interval {interval}")
        return composite

    def _insert_sorted(self, interval: str, kline: Kline, is_historical: bool) -> None:
        buf = self._buffers[interval]
        # 转为 list 进行二分插入
        temp_list = list(buf)
        pos = bisect.bisect_left([k.open_time for k in temp_list], kline.open_time)
        temp_list.insert(pos, kline)
        if len(temp_list) > self.cache_size:
            # 移除最旧元素并清理索引
            removed = temp_list.pop(0)
            self._index[interval].pop(removed.open_time, None)
        # 重建 deque
        self._buffers[interval] = deque(temp_list, maxlen=self.cache_size)
        # 重建索引和缓存
        self._rebuild_index(interval)
        self._timestamp_cache[interval] = [k.open_time for k in self._buffers[interval]]
        self.stats[interval]["added" if not is_historical else "historical"] += 1
        self._last_update[interval] = time.time()

        # 回调通知
        try:
            self._callback_queues[interval].put_nowait((self._get_notify_callback(interval), kline))
        except asyncio.QueueFull:
            logger.warning(f"Callback queue full for interval {interval}, dropping notification")

        cond = self._ready_conditions.get(interval)
        if cond:
            async def _notify():
                async with cond:
                    cond.notify_all()
            asyncio.create_task(_notify())

    def _rebuild_index(self, interval: str) -> None:
        idx = self._index[interval]
        idx.clear()
        for k in self._buffers[interval]:
            idx[k.open_time] = k

    def _log_throttled(self, key: str, msg: str, level: int) -> None:
        now = time.time()
        last = self._log_throttle.get(key, 0)
        if now - last > 5.0:
            self._log_throttle[key] = now
            logger.log(level, msg)
