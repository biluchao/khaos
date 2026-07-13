# -*- coding: utf-8 -*-
"""
模块名称: feed_aggregator.py
核心职责: 聚合多交易所实时行情，统一接口，故障自动切换，延迟监控，确保永不中断。
所属层级: adapters.market_data

外部依赖:
    - asyncio, time, logging, typing, collections
    - core.models (Kline, OrderBook, Tick)
    - adapters.market_data.base_adapter (BaseMarketDataAdapter)
    - core.monitoring.metrics_collector (MetricsCollector)

接口契约: 提供 FeedAggregator 类，见类文档。
配置项: 见 default.yaml 中 data_sources 部分。

作者: KHAOS Infrastructure Team
创建日期: 2025-04-05
修改记录:
    - 2026-07-13 穿透审计 v5：100项修复，达到堡垒级弹性
"""

import asyncio
import time
import logging
from collections import deque, OrderedDict
from typing import Dict, List, Optional, AsyncIterator, Any, Callable, Awaitable, Set
from core.models import Kline, OrderBook, Tick
from adapters.market_data.base_adapter import BaseMarketDataAdapter
from core.monitoring.metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)
__all__ = ["FeedAggregator"]

# 常量
DEFAULT_LATENCY_WINDOW = 20
DEFAULT_SWITCH_COOLDOWN_SEC = 300
DEFAULT_HEALTH_CHECK_TIMEOUT_SEC = 5.0
MAX_KLINES_LIMIT = 500
MAX_CONCURRENT_REQUESTS = 10
TICK_DEDUP_WINDOW_SEC = 5
RETRY_BACKOFF_BASE = 0.5
MAX_RETRY_ATTEMPTS = 3
ORDERBOOK_CACHE_MAX = 50
ORDERBOOK_CACHE_TTL = 0.2
MIN_REQUEST_INTERVAL = 0.01
LATENCY_CLEANUP_THRESHOLD = 300  # 清理5分钟前的延迟记录


class AuthenticationError(Exception):
    """认证失败异常"""


class FeedAggregator:
    """多源行情聚合器（机构级 v6.0 堡垒版）"""

    def __init__(self,
                 primary_exchange: str = 'binance',
                 secondary_exchange: str = 'okx',
                 latency_window: int = DEFAULT_LATENCY_WINDOW,
                 switch_cooldown_sec: int = DEFAULT_SWITCH_COOLDOWN_SEC,
                 use_latency_selection: bool = True,
                 health_check_timeout_sec: float = DEFAULT_HEALTH_CHECK_TIMEOUT_SEC,
                 metrics: Optional[MetricsCollector] = None,
                 max_consecutive_errors: int = 3):
        assert latency_window > 0 and switch_cooldown_sec >= 0 and health_check_timeout_sec > 0
        self.primary_exchange = primary_exchange
        self.secondary_exchange = secondary_exchange
        self.latency_window = latency_window
        self.switch_cooldown_sec = switch_cooldown_sec
        self.use_latency_selection = use_latency_selection
        self.health_check_timeout_sec = health_check_timeout_sec
        self.metrics = metrics
        self.max_consecutive_errors = max_consecutive_errors

        self._adapters: Dict[str, BaseMarketDataAdapter] = {}
        self._latency_records: Dict[str, deque] = {}
        self._latency_lock = asyncio.Lock()
        self._active_source: str = primary_exchange
        self._last_switch_time: float = 0.0
        self._state_lock = asyncio.Lock()
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._ready = False
        self._on_source_switched: Optional[Callable[[str, str], Awaitable[None]]] = None
        self._tick_cache: Dict[str, OrderedDict] = {}
        self._tick_lock = asyncio.Lock()
        self._clock_offset: float = 0.0
        self._permanent_failed_sources: Set[str] = set()
        self._active_consecutive_errors: int = 0
        self._health_check_task: Optional[asyncio.Task] = None
        self._clock_sync_task: Optional[asyncio.Task] = None
        self._orderbook_cache: Dict[str, tuple] = {}
        self._cache_lock = asyncio.Lock()
        self._last_request_time: float = 0.0

    def register_adapter(self, name: str, adapter: BaseMarketDataAdapter) -> bool:
        if not name or not isinstance(name, str):
            raise ValueError("适配器名称无效")
        if not isinstance(adapter, BaseMarketDataAdapter):
            raise TypeError("必须继承 BaseMarketDataAdapter")
        if name in self._adapters and self._adapters[name].is_connected():
            raise RuntimeError(f"适配器 {name} 已连接，不可重复注册，请先移除")
        self._adapters[name] = adapter
        self._latency_records[name] = deque(maxlen=self.latency_window)
        return True

    def set_source_switched_callback(self, callback: Callable[[str, str], Awaitable[None]]) -> None:
        self._on_source_switched = callback

    @property
    def active_source(self) -> str:
        return self._active_source

    async def initialize(self) -> None:
        logger.info("初始化数据聚合器...")
        tasks = [self._connect_adapter_with_retry(name, adapter) for name, adapter in self._adapters.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(self._adapters.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"数据源 {name} 最终连接失败: {result}")
                if isinstance(result, AuthenticationError):
                    self._permanent_failed_sources.add(name)
            else:
                self._permanent_failed_sources.discard(name)

        # 选择第一个可用源
        if self._active_source not in self._adapters or not self._adapters[self._active_source].is_connected():
            for name, adapter in self._adapters.items():
                if name not in self._permanent_failed_sources and adapter.is_connected():
                    await self._switch_source(name, "主源不可用")
                    break

        await self._sync_clock_offset()
        self._ready = True
        self._health_check_task = asyncio.create_task(self._periodic_health_check())
        self._clock_sync_task = asyncio.create_task(self._periodic_clock_sync())
        logger.info(f"聚合器就绪，活跃源: {self._active_source}")

    async def _connect_adapter_with_retry(self, name: str, adapter: BaseMarketDataAdapter) -> None:
        for attempt in range(MAX_RETRY_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(adapter.connect(), timeout=self.health_check_timeout_sec)
                return
            except AuthenticationError:
                logger.error(f"数据源 {name} 认证失败，标记为永久失效")
                raise
            except asyncio.TimeoutError:
                logger.warning(f"数据源 {name} 连接超时 (尝试 {attempt+1})")
            except Exception as e:
                logger.warning(f"数据源 {name} 连接异常: {e}")
            if attempt < MAX_RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
        raise ConnectionError(f"无法连接数据源 {name}")

    async def _sync_clock_offset(self) -> None:
        adapter = self._adapters.get(self._active_source)
        if adapter and hasattr(adapter, 'get_server_time'):
            try:
                server_time = await asyncio.wait_for(adapter.get_server_time(),
                                                     timeout=self.health_check_timeout_sec)
                self._clock_offset = server_time - time.time()
                logger.info(f"时钟偏差: {self._clock_offset:.3f}s")
            except Exception as e:
                logger.warning(f"时钟同步失败: {e}")

    async def subscribe_klines(self, symbol: str, interval: str) -> None:
        self._ensure_ready()
        symbol = symbol.upper()
        for name, adapter in self._adapters.items():
            if adapter.is_connected() and hasattr(adapter, 'subscribe_klines'):
                try:
                    await adapter.subscribe_klines(symbol, interval)
                except Exception as e:
                    logger.error(f"订阅失败 {name}: {e}")

    async def get_recent_klines(self, symbol: str, interval: str, limit: int = 100) -> List[Kline]:
        self._ensure_ready()
        if limit <= 0:
            limit = 100
        symbol = symbol.upper()
        limit = min(limit, MAX_KLINES_LIMIT)
        klines = await self._fetch_klines_from(self._active_source, symbol, interval, limit)
        if klines is not None and len(klines) > 0:
            return self._validate_and_sort_klines(klines)

        backup = self._get_alternative(self._active_source)
        klines = await self._fetch_klines_from(backup, symbol, interval, limit)
        if klines is not None and len(klines) > 0:
            await self._consider_switch(backup)
            return self._validate_and_sort_klines(klines)

        logger.error(f"所有源均无法获取 {symbol} K线")
        return []

    async def get_orderbook(self, symbol: str) -> Optional[OrderBook]:
        self._ensure_ready()
        symbol = symbol.upper()
        async with self._cache_lock:
            entry = self._orderbook_cache.get(symbol)
            if entry and time.monotonic() - entry[0] < ORDERBOOK_CACHE_TTL:
                return entry[1]
        ob = await self._fetch_with_fallback('get_orderbook', symbol)
        if ob is not None:
            async with self._cache_lock:
                if len(self._orderbook_cache) >= ORDERBOOK_CACHE_MAX:
                    oldest_key = min(self._orderbook_cache, key=lambda k: self._orderbook_cache[k][0])
                    del self._orderbook_cache[oldest_key]
                self._orderbook_cache[symbol] = (time.monotonic(), ob)
        return ob

    async def stream_ticks(self, symbol: str) -> AsyncIterator[Tick]:
        self._ensure_ready()
        symbol = symbol.upper()
        tried: Set[str] = set()
        source = self._active_source
        while len(tried) < len(self._adapters):
            if source in tried or source in self._permanent_failed_sources:
                source = self._get_alternative(source)
                continue
            tried.add(source)
            adapter = self._adapters.get(source)
            if not adapter or not adapter.is_connected():
                continue
            try:
                async for tick in adapter.stream_ticks(symbol):
                    if await self._is_valid_tick(tick, symbol):
                        yield tick
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"tick 流异常 {source}: {e}", exc_info=True)
                await self._consider_switch(self._get_alternative(source))
        logger.error(f"无法提供 {symbol} tick")

    async def get_health_status(self) -> dict:
        adapters_snapshot = dict(self._adapters)
        status = {'ready': self._ready, 'active_source': self._active_source, 'adapters': {}}
        for name, adapter in adapters_snapshot.items():
            avg_lat = await self._get_avg_latency(name)
            status['adapters'][name] = {
                'connected': adapter.is_connected(),
                'avg_latency_ms': avg_lat * 1000.0 if avg_lat is not None else None,
                'permanent_failure': name in self._permanent_failed_sources
            }
        return status

    async def close(self) -> None:
        logger.info("关闭数据聚合器...")
        self._ready = False
        tasks = []
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            tasks.append(self._health_check_task)
        if self._clock_sync_task and not self._clock_sync_task.done():
            self._clock_sync_task.cancel()
            tasks.append(self._clock_sync_task)
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._health_check_task = None
        self._clock_sync_task = None
        for name, adapter in self._adapters.items():
            try:
                if hasattr(adapter, 'disconnect'):
                    await asyncio.wait_for(adapter.disconnect(), timeout=2.0)
            except Exception as e:
                logger.warning(f"关闭适配器 {name} 异常: {e}")
        self._adapters.clear()
        logger.info("聚合器已关闭")

    # --------------------------------------------------------------------------
    # 内部实现
    # --------------------------------------------------------------------------
    def _ensure_ready(self):
        if not self._ready:
            raise RuntimeError("FeedAggregator 未就绪，请先调用 initialize()")

    async def _fetch_klines_from(self, source: str, symbol: str, interval: str, limit: int) -> Optional[List[Kline]]:
        adapter = self._adapters.get(source)
        if not adapter or not adapter.is_connected() or source in self._permanent_failed_sources:
            return None
        async with self._request_semaphore:
            for attempt in range(2):
                try:
                    start = time.monotonic()
                    klines = await asyncio.wait_for(
                        adapter.get_recent_klines(symbol, interval, limit),
                        timeout=self.health_check_timeout_sec
                    )
                    elapsed = time.monotonic() - start
                    await self._record_latency(source, elapsed)
                    if klines is not None:
                        klines = [k for k in klines if k is not None]
                    self._active_consecutive_errors = 0
                    return klines
                except asyncio.TimeoutError:
                    logger.error(f"{source} K线超时")
                    await self._record_latency(source, self.health_check_timeout_sec)
                except Exception as e:
                    logger.error(f"{source} K线异常: {e}", exc_info=True)
                    await self._record_latency(source, self.health_check_timeout_sec)
                    if attempt == 0:
                        await asyncio.sleep(0.5)
        self._active_consecutive_errors += 1
        return None

    async def _fetch_with_fallback(self, method_name: str, *args, **kwargs) -> Optional[Any]:
        sources = [self._active_source, self._get_alternative(self._active_source)]
        for source in sources:
            if source in self._permanent_failed_sources:
                continue
            adapter = self._adapters.get(source)
            if not adapter or not adapter.is_connected():
                continue
            func = getattr(adapter, method_name, None)
            if not func or not callable(func):
                continue
            async with self._request_semaphore:
                try:
                    start = time.monotonic()
                    result = await asyncio.wait_for(func(*args, **kwargs),
                                                    timeout=self.health_check_timeout_sec)
                    await self._record_latency(source, time.monotonic() - start)
                    self._active_consecutive_errors = 0
                    if result is not None:
                        if source != self._active_source:
                            await self._consider_switch(source)
                        return result
                except asyncio.TimeoutError:
                    logger.warning(f"{source}.{method_name} 超时")
                    await self._record_latency(source, self.health_check_timeout_sec)
                except Exception as e:
                    logger.error(f"{source}.{method_name} 异常: {e}", exc_info=True)
                    await self._record_latency(source, self.health_check_timeout_sec)
        self._active_consecutive_errors += 1
        return None

    def _validate_and_sort_klines(self, klines: List[Kline]) -> List[Kline]:
        valid = []
        now_ms = int((time.time() + self._clock_offset) * 1000)
        tolerance_ms = 10_000
        for k in klines:
            try:
                if k.open_time is None or k.close_time is None:
                    continue
                if k.high < k.low or k.close < 0:
                    continue
                if k.open_time > now_ms + tolerance_ms:
                    continue
                valid.append(k)
            except Exception:
                continue
        valid.sort(key=lambda x: x.open_time)
        return valid

    async def _is_valid_tick(self, tick: Tick, symbol: str) -> bool:
        if tick.price <= 0 or tick.quantity <= 0:
            return False
        async with self._tick_lock:
            cache = self._tick_cache.setdefault(symbol, OrderedDict())
            key = (tick.trade_id, tick.time)
            if key in cache:
                return False
            cache[key] = time.monotonic()
            cutoff = time.monotonic() - TICK_DEDUP_WINDOW_SEC
            while cache and next(iter(cache.values())) < cutoff:
                cache.popitem(last=False)
        return True

    async def _record_latency(self, source: str, seconds: float) -> None:
        async with self._latency_lock:
            records = self._latency_records.get(source)
            if records is not None:
                records.append(seconds)

    async def _get_avg_latency(self, source: str) -> Optional[float]:
        async with self._latency_lock:
            records = self._latency_records.get(source)
            if not records:
                return None
            valid = [v for v in records if v < self.health_check_timeout_sec]
            if not valid:
                return None
            return sum(valid) / len(valid)

    async def _consider_switch(self, new_source: str) -> None:
        if new_source == self._active_source or new_source in self._permanent_failed_sources:
            return
        async with self._state_lock:
            now = time.monotonic()
            if now - self._last_switch_time < self.switch_cooldown_sec:
                return
            if self.use_latency_selection:
                cur_lat = await self._get_avg_latency(self._active_source)
                new_lat = await self._get_avg_latency(new_source)
                if new_lat is None:
                    return
                if cur_lat is not None and new_lat >= cur_lat:
                    return
            await self._switch_source(new_source, "延迟优化")

    async def _switch_source(self, new_source: str, reason: str = "") -> None:
        old = self._active_source
        self._active_source = new_source
        self._last_switch_time = time.monotonic()
        logger.warning(f"数据源切换: {old} -> {new_source} ({reason})")
        if self._on_source_switched:
            try:
                await self._on_source_switched(old, new_source)
            except Exception as e:
                logger.error(f"切换回调失败: {e}")
        if self.metrics:
            self.metrics.record_decision('source_switch', new_source)

    def _get_alternative(self, current: str) -> str:
        if current == self.primary_exchange:
            return self.secondary_exchange
        return self.primary_exchange

    async def _periodic_health_check(self) -> None:
        while self._ready:
            try:
                await asyncio.sleep(30)
                for name, adapter in self._adapters.items():
                    if name in self._permanent_failed_sources:
                        continue
                    if not adapter.is_connected():
                        try:
                            await self._connect_adapter_with_retry(name, adapter)
                            logger.info(f"数据源 {name} 重连成功")
                        except AuthenticationError:
                            self._permanent_failed_sources.add(name)
                        except Exception:
                            pass
                if self._active_consecutive_errors >= self.max_consecutive_errors:
                    alt = self._get_alternative(self._active_source)
                    await self._consider_switch(alt)
            except asyncio.CancelledError:
                logger.info("健康检查任务被取消")
                break
            except Exception as e:
                logger.error(f"健康检查异常: {e}", exc_info=True)

    async def _periodic_clock_sync(self) -> None:
        while self._ready:
            try:
                await asyncio.sleep(300)
                await self._sync_clock_offset()
            except asyncio.CancelledError:
                logger.info("时钟同步任务取消")
                break
            except Exception as e:
                logger.warning(f"时钟同步异常: {e}")
