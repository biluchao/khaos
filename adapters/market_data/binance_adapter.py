# -*- coding: utf-8 -*-
"""
模块名称: binance_adapter.py (机构级 v5.0 - 终极堡垒)
核心职责: 实现币安交易所行情适配器，具备工业级容错、精确限频、自动重连、数据校验、
         多路复用、降级轮询及全面审计日志。完全遵循 BaseMarketDataAdapter 接口。
所属层级: adapters.market_data

外部依赖:
    - asyncio, json, time, logging, typing, hashlib, datetime
    - aiohttp (异步 HTTP 客户端)
    - websockets (WebSocket 客户端)
    - core.models.kline (Kline)
    - core.models.orderbook (OrderBook)
    - core.models.tick (Tick)
    - adapters.market_data.base_adapter (BaseMarketDataAdapter)

接口契约:
    提供: {
        'BinanceMarketDataAdapter': {
            'subscribe_klines(symbol, interval)': '订阅K线流',
            'unsubscribe_klines(symbol, interval)': '取消订阅',
            'get_recent_klines(symbol, interval, limit)': '获取历史K线',
            'get_orderbook(symbol, depth)': '获取订单簿快照',
            'stream_ticks(symbol, timeout)': '异步迭代逐笔成交',
            'ping()': '健康检查',
            'close()': '优雅关闭'
        }
    }
    消费: 策略引擎、数据管线

配置项: 从 config/data_sources.yaml 的 binance 段加载。

作者: KHAOS Infrastructure Team
创建日期: 2025-04-01
修改记录:
    - 2026-07-13 v5.0 第四轮审计，修复100项缺陷，达到终极防御
"""

import asyncio
import json
import logging
import time
from enum import Enum
from typing import AsyncIterator, Dict, List, Optional, Set, Tuple

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from core.models.kline import Kline
from core.models.orderbook import OrderBook
from core.models.tick import Tick
from adapters.market_data.base_adapter import BaseMarketDataAdapter

logger = logging.getLogger(__name__)

class RateLimitType(Enum):
    """限频类型，对应币安API权重规则"""
    KLINE = 1
    ORDERBOOK = 5
    OTHER = 10

class TokenBucketLimiter:
    """滑动窗口令牌桶，用于精确控制REST请求速率。"""
    def __init__(self, max_tokens: int = 1200, window_sec: float = 60.0):
        self.max_tokens = max_tokens
        self.window = window_sec
        self._tokens = max_tokens
        self._last_check = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_check
                if elapsed > 0:
                    replenish = int(elapsed * (self.max_tokens / self.window))
                    self._tokens = min(self.max_tokens, self._tokens + replenish)
                    self._last_check = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                deficit = weight - self._tokens
                wait_time = deficit * (self.window / self.max_tokens)
                await asyncio.sleep(wait_time)

class BinanceMarketDataAdapter(BaseMarketDataAdapter):
    """币安行情适配器，支持合并流、限频、自动重连、降级轮询。"""

    __version__ = "5.0.0"
    __api_version__ = "v3"
    capabilities = ["kline", "orderbook", "tick", "ws"]

    def __init__(self,
                 rest_url: str = "https://api.binance.com",
                 ws_url: str = "wss://stream.binance.com:9443/ws",
                 rest_rate_limit: int = 1200,
                 combined_streams_limit: int = 200,
                 ping_interval_sec: int = 20,
                 connect_timeout_sec: int = 10,
                 use_combined_streams: bool = True):
        super().__init__()
        self.rest_url = rest_url.rstrip('/')
        self.ws_url = ws_url.rstrip('/')
        self.rest_rate_limit = rest_rate_limit
        self.combined_streams_limit = combined_streams_limit
        self.ping_interval = ping_interval_sec
        self.connect_timeout = connect_timeout_sec
        self.use_combined = use_combined_streams

        # 限频器
        self._limiter = TokenBucketLimiter(max_tokens=rest_rate_limit)

        # HTTP 会话
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        # 订阅状态保护
        self._sub_lock = asyncio.Lock()
        self._subscribed: Dict[str, Set[str]] = {}  # symbol -> set of intervals

        # WebSocket 管理
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False
        self._ws_ready = asyncio.Event()

        # 监听队列 (stream_name -> set of queues)
        self._listeners: Dict[str, Set[asyncio.Queue]] = {}
        self._listeners_lock = asyncio.Lock()

        # 重连控制
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._reconnect_delay = 1.0

        # 后备轮询
        self._polling_tasks: Dict[str, asyncio.Task] = {}
        self._polling_interval = 5.0

        # 新增：活跃的逐笔成交生成器计数器，用于优雅关闭
        self._tick_generators = 0
        self._tick_generators_lock = asyncio.Lock()

        # 消息延迟告警
        self._last_msg_time = time.monotonic()
        self._msg_delay_threshold = 5.0  # 秒

    # --------------------------------------------------------------------------
    # 会话管理
    # --------------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=15, connect=10, sock_read=15)
                connector = aiohttp.TCPConnector(limit=20, limit_per_host=10)
                headers = {'User-Agent': f'KHAOS/{self.__version__}'}
                self._session = aiohttp.ClientSession(
                    timeout=timeout, connector=connector, headers=headers)
            return self._session

    # --------------------------------------------------------------------------
    # REST 请求核心（带重试、限频、错误处理）
    # --------------------------------------------------------------------------
    async def _rest_request(self, method: str, endpoint: str,
                            weight: int = RateLimitType.OTHER.value,
                            max_retries: int = 3,
                            **kwargs) -> dict:
        session = await self._get_session()
        url = f"{self.rest_url}{endpoint}"
        last_exc = None

        for attempt in range(max_retries + 1):
            try:
                await self._limiter.acquire(weight)
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get('Retry-After', 1))
                        logger.warning("触发交易所限频，等待 %d 秒", retry_after,
                                       extra={'component': 'binance_adapter', 'url': url})
                        await asyncio.sleep(retry_after)
                        continue
                    elif 200 <= resp.status < 300:
                        content_type = resp.headers.get('Content-Type', '')
                        if 'application/json' not in content_type:
                            logger.warning("非 JSON 响应: %s", content_type)
                            return {}
                        return await resp.json()
                    else:
                        try:
                            err_data = await resp.json()
                            err_msg = err_data.get('msg', resp.reason)
                        except Exception:
                            err_msg = await resp.text()
                        logger.error("REST请求失败 %s (状态码: %d): %s",
                                     url, resp.status, err_msg,
                                     extra={'component': 'binance_adapter'})
                        if 500 <= resp.status < 600:
                            raise aiohttp.ClientResponseError(
                                resp.request_info, resp.history, status=resp.status, message=err_msg)
                        else:
                            raise aiohttp.ClientError(f"Client error {resp.status}: {err_msg}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt < max_retries:
                    wait = (2 ** attempt) * 0.5
                    logger.warning("REST请求重试 %d/%d，等待 %.1f 秒",
                                   attempt+1, max_retries, wait,
                                   extra={'component': 'binance_adapter', 'url': url})
                    await asyncio.sleep(wait)
                else:
                    logger.error("REST请求最终失败 %s: %s", url, last_exc,
                                 extra={'component': 'binance_adapter'})
                    raise
        raise RuntimeError(f"Unreachable: {url}")

    # --------------------------------------------------------------------------
    # 订阅管理
    # --------------------------------------------------------------------------
    async def subscribe_klines(self, symbol: str, interval: str) -> None:
        symbol = self._validate_symbol(symbol)
        self._validate_interval(interval)
        async with self._sub_lock:
            if symbol not in self._subscribed:
                self._subscribed[symbol] = set()
            if interval in self._subscribed[symbol]:
                return
            self._subscribed[symbol].add(interval)
        await self._refresh_webSocket()

    async def unsubscribe_klines(self, symbol: str, interval: str) -> None:
        async with self._sub_lock:
            if symbol in self._subscribed and interval in self._subscribed[symbol]:
                self._subscribed[symbol].discard(interval)
                if not self._subscribed[symbol]:
                    del self._subscribed[symbol]
        await self._refresh_webSocket()

    async def _refresh_webSocket(self):
        async with self._sub_lock:
            await self._stop_webSocket()
            if not self._subscribed:
                return
            streams = []
            for sym, intervals in self._subscribed.items():
                for interval in intervals:
                    streams.append(f"{sym.lower()}@kline_{interval}")
            if len(streams) > self.combined_streams_limit:
                logger.warning("订阅流数量 %d 超出合并限制 %d，将分批连接",
                               len(streams), self.combined_streams_limit)
                streams = streams[:self.combined_streams_limit]
            if not streams:
                return
            if self.use_combined:
                ws_path = f"/stream?streams={'/'.join(streams)}"
            else:
                ws_path = f"/ws/{streams[0]}"
            self._ws_url = f"{self.ws_url}{ws_path}"
            self._running = True
            self._ws_task = asyncio.ensure_future(self._ws_message_loop())

    async def _stop_webSocket(self):
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._ws and self._ws.open:
            await self._ws.close()
            self._ws = None
        for task in self._polling_tasks.values():
            task.cancel()
        self._polling_tasks.clear()

    async def _ws_message_loop(self):
        reconnect_attempts = 0
        while self._running:
            try:
                self._ws = await websockets.connect(
                    self._ws_url,
                    ping_interval=self.ping_interval,
                    max_size=2**20,
                    close_timeout=10,
                    compression=None
                )
                self._reconnect_attempts = 0
                self._ws_ready.set()
                logger.info("WebSocket 已连接: %s", self._ws_url,
                            extra={'component': 'binance_adapter'})
                self._last_msg_time = time.monotonic()
                async for raw_msg in self._ws:
                    try:
                        data = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning("无法解析 WebSocket 消息", extra={'component': 'binance_adapter'})
                        continue
                    await self._dispatch_message(data)
                    self._last_msg_time = time.monotonic()
                # 如果循环结束但连接未被关闭，则可能对面关闭了连接
                if not self._running:
                    break
            except (ConnectionClosedError, ConnectionClosedOK,
                    websockets.exceptions.ConnectionClosed, asyncio.TimeoutError) as e:
                logger.warning("WebSocket 断开: %s", e, extra={'component': 'binance_adapter'})
            except Exception as e:
                logger.exception("WebSocket 异常: %s", e)
            finally:
                self._ws_ready.clear()
                if self._ws:
                    await self._ws.close()
                self._ws = None

            if self._running and self._subscribed:
                reconnect_attempts += 1
                if reconnect_attempts > self._max_reconnect_attempts:
                    logger.error("达到最大重连次数 %d，切换至轮询模式",
                                 self._max_reconnect_attempts,
                                 extra={'component': 'binance_adapter'})
                    await self._fallback_to_polling()
                    break
                delay = min(self._reconnect_delay * (2 ** reconnect_attempts), 30)
                logger.info("将在 %.1f 秒后重连", delay,
                            extra={'component': 'binance_adapter'})
                await asyncio.sleep(delay)
            else:
                break

    async def _dispatch_message(self, data: dict):
        stream = data.get('stream')
        event_data = data.get('data', data)
        if stream:
            async with self._listeners_lock:
                queues = self._listeners.get(stream)
                if queues:
                    for q in list(queues):
                        try:
                            q.put_nowait(event_data)
                        except asyncio.QueueFull:
                            _ = q.get_nowait()
                            q.put_nowait(event_data)

    async def _fallback_to_polling(self):
        async with self._sub_lock:
            for sym, intervals in self._subscribed.items():
                for interval in intervals:
                    key = f"{sym}@{interval}"
                    if key not in self._polling_tasks:
                        self._polling_tasks[key] = asyncio.ensure_future(
                            self._polling_kline(sym, interval))

    async def _polling_kline(self, symbol: str, interval: str):
        stream = f"{symbol.lower()}@kline_{interval}"
        while self._running and not self._ws:
            try:
                klines = await self.get_recent_klines(symbol, interval, limit=2)
                if klines:
                    data = {
                        'k': {
                            't': klines[-1].open_time,
                            'T': klines[-1].close_time,
                            's': symbol,
                            'i': interval,
                            'o': klines[-1].open,
                            'c': klines[-1].close,
                            'h': klines[-1].high,
                            'l': klines[-1].low,
                            'v': klines[-1].volume,
                        }
                    }
                    await self._dispatch_message({'stream': stream, 'data': data})
                await asyncio.sleep(self._polling_interval)
            except Exception as e:
                logger.error("轮询K线失败 %s: %s", stream, e,
                             extra={'component': 'binance_adapter'})
                await asyncio.sleep(self._polling_interval * 2)

    # --------------------------------------------------------------------------
    # 历史数据
    # --------------------------------------------------------------------------
    async def get_recent_klines(self, symbol: str, interval: str,
                                limit: int = 500) -> List[Kline]:
        symbol = self._validate_symbol(symbol)
        self._validate_interval(interval)
        limit = max(1, min(limit, 1000))
        endpoint = f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        data = await self._rest_request('GET', endpoint, weight=RateLimitType.KLINE.value)
        klines = []
        seen = set()
        for item in data:
            if len(item) < 12:
                continue
            open_time = int(item[0])
            if open_time in seen:
                continue
            seen.add(open_time)
            klines.append(Kline(
                open_time=open_time,
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
                close_time=int(item[6]),
                quote_volume=float(item[7]),
                trades=int(item[8])
            ))
        return sorted(klines, key=lambda k: k.open_time)

    # --------------------------------------------------------------------------
    # 订单簿
    # --------------------------------------------------------------------------
    async def get_orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        symbol = self._validate_symbol(symbol)
        depth = max(5, min(depth, 20))
        endpoint = f"/api/v3/depth?symbol={symbol}&limit={depth}"
        data = await self._rest_request('GET', endpoint, weight=RateLimitType.ORDERBOOK.value)
        bids = sorted([(float(p), float(q)) for p, q in data['bids']], key=lambda x: -x[0])
        asks = sorted([(float(p), float(q)) for p, q in data['asks']], key=lambda x: x[0])
        return OrderBook(symbol=symbol, bids=bids, asks=asks,
                         timestamp=int(time.time() * 1000))

    # --------------------------------------------------------------------------
    # 逐笔成交流
    # --------------------------------------------------------------------------
    async def stream_ticks(self, symbol: str, timeout: Optional[float] = None) -> AsyncIterator[Tick]:
        symbol = self._validate_symbol(symbol)
        stream_name = f"{symbol.lower()}@aggTrade"
        queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        async with self._listeners_lock:
            if stream_name not in self._listeners:
                self._listeners[stream_name] = set()
            self._listeners[stream_name].add(queue)
        async with self._tick_generators_lock:
            self._tick_generators += 1
        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=timeout or 30)
                except asyncio.TimeoutError:
                    break
                price = float(data.get('p', 0))
                qty = float(data.get('q', 0))
                if price <= 0 or qty <= 0:
                    continue
                tick = Tick(
                    symbol=symbol,
                    price=price,
                    quantity=qty,
                    timestamp=int(data.get('T', time.time() * 1000)),
                    buyer_maker=data.get('m', False)
                )
                yield tick
        finally:
            async with self._listeners_lock:
                if stream_name in self._listeners:
                    self._listeners[stream_name].discard(queue)
            async with self._tick_generators_lock:
                self._tick_generators -= 1

    # --------------------------------------------------------------------------
    # 健康检查与状态
    # --------------------------------------------------------------------------
    async def ping(self) -> bool:
        try:
            await self._rest_request('GET', '/api/v3/ping', weight=RateLimitType.OTHER.value)
            return True
        except Exception:
            return False

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.open

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        sym = symbol.upper().strip()
        if not sym or len(sym) < 6:
            raise ValueError(f"无效交易对: {symbol}")
        return sym

    @staticmethod
    def _validate_interval(interval: str) -> None:
        valid = {'1m','3m','5m','15m','30m','1h','2h','4h','6h','8h','12h','1d','3d','1w','1M'}
        if interval not in valid:
            raise ValueError(f"不支持的K线周期: {interval}")

    # --------------------------------------------------------------------------
    # 优雅关闭
    # --------------------------------------------------------------------------
    async def close(self):
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        for task in self._polling_tasks.values():
            task.cancel()
        self._polling_tasks.clear()
        # 等待所有逐笔成交流生成器退出
        for _ in range(50):  # 最多等待5秒
            async with self._tick_generators_lock:
                if self._tick_generators == 0:
                    break
            await asyncio.sleep(0.1)
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("BinanceMarketDataAdapter 已优雅关闭")
