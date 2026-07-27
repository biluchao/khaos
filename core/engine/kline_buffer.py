# -*- coding: utf-8 -*-
"""
模块名称: kline_buffer.py
核心职责: 多周期K线缓冲管理，提供极致性能、并发安全、数据完整性保障的访问层
所属层级: core.engine

线程安全: 所有公共方法均为异步安全，内部使用 asyncio.Lock 保护共享状态。
         同步回调通过事件循环安全分发，无线程泄漏风险。

外部依赖:
    - asyncio
    - collections.deque
    - itertools.islice
    - bisect
    - math
    - time
    - typing
    - core.models.kline.Kline

接口契约:
    提供: {
        'MultiTimeframeKlineBuffer': {
            'add_kline(kline: Kline, interval: str) -> AddResult': '添加K线',
            'get_recent_klines(interval: str, limit: int) -> List[Kline]': '获取最近N根',
            'get_kline_by_timestamp(interval: str, open_time: int) -> Optional[Kline]': 'O(log n)查找',
            'get_kline_range(interval: str, start_time: int, end_time: int) -> List[Kline]': '时间范围查询',
            'get_all_intervals() -> Tuple[str,...]': '已注册周期',
            'is_ready(interval: str, min_bars: int) -> bool': '就绪检查',
            'get_klines(symbol, interval, limit) -> List[Kline]': '兼容系统调用（忽略symbol）'
        }
    }

配置项:
    - kline_buffer.cache_size (int, 5000): 每周期最大K线数 (必须>0)
    - kline_buffer.intervals (list, ['3m','5m','15m']): 默认周期
    - kline_buffer.max_timestamp_deviation_ms (int, 60000): 乱序容忍度

作者: KHAOS System Architect
创建日期: 2025-03-15
修改记录:
    - v1.0 \~ v7.1 多轮演进
    - 2026-07-27 v7.2: wait 竞态消除、回调自愈、索引强制同步、周期数量上限、
                      insert 优化、stats 键完整性、strict_mode 增强（累计150+缺陷修复）
__version__ = "7.2.0"
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
MAX_RANGE_LIMIT = 5000
CALLBACK_TIMEOUT_SEC = 2.0
MAX_CONCURRENT_CALLBACKS = 10
CALLBACK_QUEUE_SIZE = 200
PERF_RESET_INTERVAL = 10000
MAX_REGISTERED_INTERVALS = 64


class AddResult(Enum):
    OK = "ok"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    OUT_OF_ORDER_INSERTED = "out_of_order_inserted"
    HISTORICAL_INSERTED = "historical_inserted"


class MultiTimeframeKlineBuffer:
    """
    多周期K线缓冲管理器 (v7.2)

    核心改进：
    - wait_until_ready 竞态窗口关闭。
    - 回调消费者异常后自动重建。
    - 索引/时间戳缓存所有路径强制同步。
    - 注册周期数量硬限。
    - stats 键始终完整。
    - strict_mode 增强。
    """

    __version__ = "7.2.0"

    def __init__(self,
                 cache_size: int = DEFAULT_CACHE_SIZE,
                 intervals: Optional[Union[List[str], Tuple[str, ...]]] = None,
                 max_timestamp_deviation_ms: int = DEFAULT_MAX_TIMESTAMP_DEVIATION_MS,
                 strict_mode: bool = False):
        if cache_size < 1:
            raise ValueError("cache_size must be >= 1")

        self.cache_size = cache_size
        self.intervals = list(intervals) if intervals else list(DEFAULT_INTERVALS)
        if len(self.intervals) > MAX_REGISTERED_INTERVALS:
            raise ValueError(f"Too many intervals (max {MAX_REGISTERED_INTERVALS})")
        self.max_timestamp_deviation_ms = max(0, int(max_timestamp_deviation_ms))
        self.strict_mode = bool(strict_mode)

        self._buffers: Dict[str, deque] = {i: deque() for i in self.intervals}
        self._index: Dict[str, Dict[int, Kline]] = {i: {} for i in self.intervals}
        self._timestamp_cache: Dict[str, List[int]] = {i: [] for i in self.intervals}

        self._callbacks: Dict[str, List[Callable]] = {i: [] for i in self.intervals}
        self._callback_queues: Dict[str, asyncio.Queue] = {
            i: asyncio.Queue(CALLBACK_QUEUE_SIZE) for i in self.intervals
        }
        self._callback_tasks: Dict[str, asyncio.Task] = {}

        self._ready_conditions: Dict[str, asyncio.Condition] = {
            i: asyncio.Condition() for i in self.intervals
        }

        self._last_update: Dict[str, float] = {i: 0.0 for i in self.intervals}
        self.stats: Dict[str, Dict[str, int]] = {
            i: self._fresh_stats() for i in self.intervals
        }
        self._perf_stats: Dict[str, Dict[str, float]] = {i: {} for i in self.intervals}
        self._add_counter: Dict[str, int] = {i: 0 for i in self.intervals}

        self._lock = asyncio.Lock()
        self._log_throttle: Dict[str, float] = {}

        for interval in self.intervals:
            self._start_callback_consumer(interval)

    @staticmethod
    def _fresh_stats() -> Dict[str, int]:
        return {"added": 0, "duplicates": 0, "invalid": 0, "out_of_order": 0, "historical": 0}

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def register_interval(self, interval: str) -> None:
        interval = interval.lower()
        async with self._lock:
            if len(self._buffers) >= MAX_REGISTERED_INTERVALS and interval not in self._buffers:
                raise RuntimeError(f"Max registered intervals ({MAX_REGISTERED_INTERVALS}) exceeded")
            self._register_interval_unsafe(interval)

    def _register_interval_unsafe(self, interval: str) -> None:
        if interval not in self._buffers:
            self._buffers[interval] = deque()
            self._index[interval] = {}
            self._timestamp_cache[interval] = []
            self._callbacks[interval] = []
            self._ready_conditions[interval] = asyncio.Condition()
            self.stats[interval] = self._fresh_stats()
            self._perf_stats[interval] = {}
            self._last_update[interval] = 0.0
            self._add_counter[interval] = 0
            if interval not in self.intervals:
                self.intervals.append(interval)
            self._callback_queues[interval] = asyncio.Queue(CALLBACK_QUEUE_SIZE)
            self._start_callback_consumer(interval)
            logger.info("Registered interval: %s", interval)

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
            perf.setdefault("total_add_us", 0.0)
            perf["total_add_us"] += elapsed * 1_000_000
            perf.setdefault("add_count", 0)
            perf["add_count"] += 1
            self._add_counter[interval] = self._add_counter.get(interval, 0) + 1
            if self._add_counter[interval] >= PERF_RESET_INTERVAL:
                self._perf_stats[interval].clear()
                self._add_counter[interval] = 0
                logger.debug("Perf stats reset for interval %s", interval)
            return result

    def _ensure_interval_exists(self, interval: str) -> None:
        if interval not in self._buffers:
            if len(self._buffers) >= MAX_REGISTERED_INTERVALS:
                raise RuntimeError(f"Max registered intervals ({MAX_REGISTERED_INTERVALS}) exceeded")
            self._register_interval_unsafe(interval)

    async def get_recent_klines(self, interval: str, limit: int) -> List[Kline]:
        interval = interval.lower()
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit < 0:
            limit = 0
        if limit > MAX_LIMIT:
            limit = MAX_LIMIT
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            if limit == 0 or limit >= len(buf):
                return list(buf)
            return list(islice(buf, len(buf) - limit, None))

    async def get_klines(self, symbol: Any, interval: str, limit: int = 0) -> List[Kline]:
        """
        兼容 context_pipeline 等调用方。
        本缓冲为单实例多周期设计，symbol 参数被忽略（由上层保证 per-symbol 实例）。
        """
        return await self.get_recent_klines(interval, limit)

    async def get_kline_by_timestamp(self, interval: str, open_time: int) -> Optional[Kline]:
        interval = interval.lower()
        async with self._lock:
            return self._index.get(interval, {}).get(open_time)

    async def get_kline_range(self, interval: str, start_time: int, end_time: int) -> List[Kline]:
        interval = interval.lower()
        if end_time < start_time:
            logger.warning("get_kline_range [%s]: end_time < start_time", interval)
            return []
        async with self._lock:
            buf = self._buffers.get(interval, deque())
            if not buf:
                return []
            times = self._timestamp_cache.get(interval, [])
            if len(times) != len(buf):
                times = [k.open_time for k in buf]
                self._timestamp_cache[interval] = times
            lo = bisect.bisect_left(times, start_time)
            result = []
            for k in islice(buf, lo, lo + MAX_RANGE_LIMIT):
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
            return len(self._buffers.get(interval, deque())) >= min_bars

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
                    self._clear_interval(interval)
                    logger.info("Cleared buffer: %s", interval)
            else:
                for i in list(self._buffers.keys()):
                    self._clear_interval(i)
                logger.info("Cleared all buffers")

    def _clear_interval(self, interval: str) -> None:
        self._buffers[interval].clear()
        self._index[interval].clear()
        self._timestamp_cache[interval].clear()
        self._last_update[interval] = 0.0
        self.stats[interval] = self._fresh_stats()
        self._perf_stats[interval].clear()
        self._add_counter[interval] = 0
        self._stop_callback_consumer(interval)
        self._callback_queues[interval] = asyncio.Queue(CALLBACK_QUEUE_SIZE)
        self._start_callback_consumer(interval)

    async def add_callback(self, interval: str, callback: Callable[[Kline], None]) -> None:
        interval = interval.lower()
        async with self._lock:
            if interval not in self._callbacks:
                self._callbacks[interval] = []
            self._callbacks[interval].append(callback)

    async def wait_until_ready(self, interval: str, min_bars: int, timeout: float = 30.0) -> bool:
        """原子化就绪等待，消除竞态窗口。"""
        interval = interval.lower()
        if min_bars <= 0:
            return True

        async with self._lock:
            if interval not in self._ready_conditions:
                self._ready_conditions[interval] = asyncio.Condition()
            cond = self._ready_conditions[interval]
            if len(self._buffers.get(interval, deque())) >= min_bars:
                return True

        try:
            # 使用 buffer 锁 + condition 的组合语义
            async with cond:
                def _ready() -> bool:
                    # 注意：此 lambda 在 condition 锁下执行，
                    # 但 buffer 长度变化由 add 在 buffer 锁内完成后 notify，
                    # 因此在实践中是安全的。额外一次快速检查。
                    return len(self._buffers.get(interval, deque())) >= min_bars
                await asyncio.wait_for(cond.wait_for(_ready), timeout=timeout)
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
            closes = [k.close for k in buf if k.close is not None]
            if not closes:
                return {"count": len(buf)}
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
                "close_time": getattr(k, "close_time", None),
                "open": k.open,
                "high": k.high,
                "low": k.low,
                "close": k.close,
                "volume": getattr(k, "volume", 0),
            } for k in buf]
            return pd.DataFrame(data)

    async def export_state(self) -> Dict[str, Any]:
        async with self._lock:
            state = {}
            for i in self.intervals:
                state[i] = {
                    "klines": [k.to_dict() for k in self._buffers[i] if hasattr(k, "to_dict")],
                    "stats": dict(self.stats.get(i, self._fresh_stats())),
                }
            return state

    async def import_state(self, state: Dict[str, Any]) -> None:
        async with self._lock:
            for i, data in state.items():
                if i not in self._buffers:
                    if len(self._buffers) >= MAX_REGISTERED_INTERVALS:
                        logger.warning("Skip import for %s: max intervals reached", i)
                        continue
                    self._register_interval_unsafe(i)
                try:
                    raw_klines = data.get("klines", [])
                    klines = []
                    for d in raw_klines:
                        try:
                            k = Kline.from_dict(d)
                            if k is not None and self._validate_kline(k):
                                klines.append(k)
                        except Exception:
                            continue
                    seen = {}
                    for k in klines:
                        seen[k.open_time] = k
                    sorted_klines = sorted(seen.values(), key=lambda x: x.open_time)
                    if len(sorted_klines) > self.cache_size:
                        sorted_klines = sorted_klines[-self.cache_size:]
                    self._buffers[i] = deque(sorted_klines)
                    self._rebuild_index(i)
                    self._timestamp_cache[i] = [k.open_time for k in self._buffers[i]]
                    self.stats[i] = data.get("stats", self._fresh_stats())
                    # 确保键完整
                    for key in ("added", "duplicates", "invalid", "out_of_order", "historical"):
                        self.stats[i].setdefault(key, 0)
                    self._last_update[i] = time.time()
                    self._add_counter[i] = len(self._buffers[i])
                    # 触发就绪通知
                    cond = self._ready_conditions.get(i)
                    if cond:
                        self._schedule_cond_notify(cond)
                except Exception:
                    logger.exception("Failed to import state for interval %s, skipping", i)

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
        parts = ", ".join(f"{i}={len(self._buffers.get(i, deque()))}" for i in self.intervals[:5])
        return f"<KlineBuffer({parts})>"

    def __str__(self) -> str:
        return self.__repr__()

    # -----------------------------------------------------------------------
    # 回调子系统
    # -----------------------------------------------------------------------
    def _start_callback_consumer(self, interval: str) -> None:
        old = self._callback_tasks.get(interval)
        if old and not old.done():
            old.cancel()

        async def _supervised():
            try:
                await self._consume_callbacks(interval)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Callback consumer for %s crashed, will restart", interval)
                await asyncio.sleep(0.2)
                # 自动重启
                if interval in self._callback_queues:
                    self._start_callback_consumer(interval)

        self._callback_tasks[interval] = asyncio.create_task(
            _supervised(),
            name=f"kline_cb_{interval}"
        )

    def _stop_callback_consumer(self, interval: str) -> None:
        task = self._callback_tasks.pop(interval, None)
        if task and not task.done():
            task.cancel()
        queue = self._callback_queues.get(interval)
        if queue:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def _consume_callbacks(self, interval: str) -> None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_CALLBACKS)
        queue = self._callback_queues.get(interval)
        if queue is None:
            return
        while True:
            try:
                kline = await queue.get()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Callback queue error for %s", interval)
                await asyncio.sleep(0.05)
                continue
            cbs = list(self._callbacks.get(interval, []))
            async with sem:
                for cb in cbs:
                    try:
                        await asyncio.wait_for(
                            self._execute_callback(cb, kline),
                            timeout=CALLBACK_TIMEOUT_SEC
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Callback timed out for interval %s", interval)
                    except Exception:
                        logger.exception("Callback error for interval %s", interval)

    async def _execute_callback(self, cb: Callable, kline: Kline) -> None:
        if asyncio.iscoroutinefunction(cb):
            await cb(kline)
        else:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, cb, kline)

    # -----------------------------------------------------------------------
    # 内部数据管理
    # -----------------------------------------------------------------------
    def _validate_kline(self, k: Kline) -> bool:
        try:
            if k.open_time is None or k.close_time is None:
                return False
            if k.close_time <= k.open_time:
                return False
            if k.high is None or k.low is None or k.open is None or k.close is None:
                return False
            if k.high < k.low:
                return False
            tolerance = max(1e-8 * max(abs(k.high), 1.0), 1e-12)
            if k.high < max(k.open, k.close) - tolerance:
                return False
            if k.low > min(k.open, k.close) + tolerance:
                return False
            for val in (k.open, k.high, k.low, k.close, getattr(k, "volume", 0)):
                if val is None:
                    return False
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    return False
                if val < 0:
                    return False
            if self.strict_mode:
                if not isinstance(k.open_time, (int, float)) or k.open_time <= 0:
                    return False
                if not isinstance(k.close_time, (int, float)) or k.close_time <= 0:
                    return False
                # 额外：价格不能全为 0
                if k.open == 0 and k.high == 0 and k.low == 0 and k.close == 0:
                    return False
            return True
        except Exception:
            return False

    def _add_kline_unsafe(self, kline: Kline, interval: str, allow_historical: bool) -> AddResult:
        buf = self._buffers[interval]
        idx = self._index[interval]
        open_time = kline.open_time

        if open_time in idx:
            self.stats[interval]["duplicates"] += 1
            return AddResult.DUPLICATE

        if buf:
            last_time = buf[-1].open_time
            if open_time < last_time:
                if not allow_historical:
                    deviation = last_time - open_time
                    if deviation > self.max_timestamp_deviation_ms:
                        self._log_throttled(
                            interval,
                            f"Out-of-order kline discarded: {open_time} < {last_time}",
                            logging.WARNING
                        )
                        self.stats[interval]["invalid"] += 1
                        return AddResult.INVALID
                self._insert_sorted(interval, kline, allow_historical)
                return (
                    AddResult.HISTORICAL_INSERTED if allow_historical
                    else AddResult.OUT_OF_ORDER_INSERTED
                )

        # 正常追加 + 手动容量控制
        if len(buf) >= self.cache_size:
            removed = buf.popleft()
            idx.pop(removed.open_time, None)
            times = self._timestamp_cache[interval]
            if times and times[0] == removed.open_time:
                times.pop(0)
            else:
                # 强制同步
                self._timestamp_cache[interval] = [k.open_time for k in buf]
        buf.append(kline)
        idx[open_time] = kline
        self._timestamp_cache[interval].append(open_time)

        # 最终一致性检查
        if len(self._timestamp_cache[interval]) != len(buf):
            self._timestamp_cache[interval] = [k.open_time for k in buf]

        self.stats[interval]["added"] += 1
        self._last_update[interval] = time.time()

        try:
            self._callback_queues[interval].put_nowait(kline)
        except asyncio.QueueFull:
            logger.warning("Callback queue full for interval %s, dropping notification", interval)

        cond = self._ready_conditions.get(interval)
        if cond:
            self._schedule_cond_notify(cond)

        return AddResult.OK

    def _insert_sorted(self, interval: str, kline: Kline, is_historical: bool) -> None:
        """使用 timestamp_cache 做 bisect，最小化临时 list 开销。"""
        buf = self._buffers[interval]
        idx = self._index[interval]
        times = self._timestamp_cache[interval]
        open_time = kline.open_time

        if len(times) != len(buf):
            times = [k.open_time for k in buf]
            self._timestamp_cache[interval] = times

        pos = bisect.bisect_left(times, open_time)
        temp = list(buf)
        temp.insert(pos, kline)
        times.insert(pos, open_time)

        while len(temp) > self.cache_size:
            removed = temp.pop(0)
            times.pop(0)
            idx.pop(removed.open_time, None)

        self._buffers[interval] = deque(temp)
        self._timestamp_cache[interval] = times
        idx[open_time] = kline

        # 如果索引膨胀，强制重建
        if len(idx) > len(temp) + 5:
            self._rebuild_index(interval)

        if is_historical:
            self.stats[interval]["historical"] += 1
        else:
            self.stats[interval]["out_of_order"] += 1
        self.stats[interval]["added"] += 1
        self._last_update[interval] = time.time()

        try:
            self._callback_queues[interval].put_nowait(kline)
        except asyncio.QueueFull:
            logger.warning("Callback queue full for interval %s, dropping notification", interval)

        cond = self._ready_conditions.get(interval)
        if cond:
            self._schedule_cond_notify(cond)

    def _rebuild_index(self, interval: str) -> None:
        idx = self._index[interval]
        idx.clear()
        for k in self._buffers[interval]:
            idx[k.open_time] = k

    def _schedule_cond_notify(self, cond: asyncio.Condition) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _notify():
            try:
                async with cond:
                    cond.notify_all()
            except Exception:
                logger.exception("Error notifying condition")

        try:
            loop.create_task(_notify())
        except Exception:
            pass

    def _log_throttled(self, key: str, msg: str, level: int) -> None:
        now = time.time()
        last = self._log_throttle.get(key, 0.0)
        if now - last > 5.0:
            self._log_throttle[key] = now
            logger.log(level, msg)
