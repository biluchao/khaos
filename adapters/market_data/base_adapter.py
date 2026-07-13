# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: base_adapter.py
核心职责: 市场数据适配器抽象基类，统一所有交易所适配器的接口契约、生命周期管理、
         错误处理与可观测性标准。提供资源安全、心跳管理、配置脱敏及类型安全的默认实现。
所属层级: adapters.market_data

外部依赖:
    - asyncio (异步与超时)
    - abc (ABC, abstractmethod)
    - copy (深拷贝)
    - logging (日志)
    - time (时间戳)
    - typing (类型注解)
    - typing_extensions (TypedDict, Required)

接口契约: 详见各抽象方法文档。

作者: KHAOS Infrastructure Team
创建日期: 2025-03-20
修改记录:
    - v1.0-v4.0 多轮机构级增强
    - v5.0 第五轮：分离验证逻辑、关闭事件、心跳增强等
    - v6.0 第六轮：修复致命初始化缺失、并发安全、心跳退避、配置递归脱敏、订阅锁等100项
"""

import asyncio
import copy
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import (
    Any, AsyncIterator, Callable, ClassVar, Dict, List, Optional, Union, final, Set
)
from typing_extensions import TypedDict, Required

# ---- 兼容处理：若 typing_extensions 不可用，Required 降级 ----
try:
    from typing_extensions import Required
except ImportError:
    class _Required:
        pass
    Required = _Required  # type: ignore

# ---- 领域模型回退定义 (避免循环依赖，提供类型安全) ----
class Kline(TypedDict, total=False):
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

class OrderBookLevel(TypedDict):
    price: float
    quantity: float

class OrderBook(TypedDict):
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]

class Tick(TypedDict):
    price: float
    quantity: float
    timestamp: int

# ---- 配置与结果类型 ----

class AdapterConfig(TypedDict, total=False):
    base_url: Required[str]
    api_key: str
    secret: str
    passphrase: Optional[str]
    ws_url: str
    timeout: float
    retry_count: int
    verify_ssl: bool
    debug: bool
    extra: Dict[str, Any]

class PingResult(TypedDict):
    status: str                 # 'healthy' | 'unhealthy' | 'timeout' | 'not_implemented'
    latency_ms: float
    message: str

@dataclass
class ServerTimeResult:
    server_time_ms: float
    local_offset_ms: float

class Subscription:
    """订阅句柄，用于取消订阅或查询状态"""
    __slots__ = ('id', '_cancel', 'active')

    def __init__(self, sub_id: str, cancel_cb: Optional[Callable] = None):
        self.id = sub_id
        self._cancel = cancel_cb
        self.active = True

    async def cancel(self) -> None:
        if self._cancel and self.active:
            try:
                if asyncio.iscoroutinefunction(self._cancel):
                    await self._cancel()
                else:
                    self._cancel()
            except Exception as e:
                logging.getLogger(__name__).error(f"Cancel subscription {self.id} failed: {e}")
            finally:
                self.active = False

    def __repr__(self) -> str:
        return f"<Subscription id={self.id} active={self.active}>"

@dataclass
class SymbolInfo:
    symbol: str = ""
    base_asset: str = ""
    quote_asset: str = ""
    min_qty: float = 0.0
    step_size: float = 0.0
    min_notional: Optional[float] = None
    price_precision: int = 0
    qty_precision: int = 0

# ---- 抽象基类 ----

class BaseMarketDataAdapter(ABC):
    """
    市场数据适配器抽象基类。
    所有交易所适配器必须实现此接口，以保证系统可替换性、资源安全性和可观测性。
    """

    __version__: ClassVar[str] = "1.0.0"
    __api_version__: ClassVar[str] = "unknown"
    capabilities: ClassVar[Dict[str, Any]] = {
        'ws': True, 'rest': True, 'level2': False,
        'trades_stream': False, 'historical': True,
    }
    supported_intervals: ClassVar[List[str]] = ['1m', '3m', '5m', '15m', '1h', '4h', '1d']
    max_kline_limit: ClassVar[int] = 1000
    max_orderbook_depth: ClassVar[int] = 50
    max_preload_days: ClassVar[int] = 30
    heartbeat_max_failures: ClassVar[int] = 5
    user_agent: ClassVar[str] = f"KHAOS/{__version__}"

    def __init__(self, config: AdapterConfig) -> None:
        if not isinstance(config, dict):
            raise TypeError("config must be a dict")
        self._config = self._validate_and_normalize_config(config)
        self._is_closed = False
        self._closed_event = asyncio.Event()
        self._logger = logging.getLogger(f"{self.__class__.__name__}")
        self._metrics_lock = asyncio.Lock()
        self._subscription_lock = asyncio.Lock()
        self._metrics_collector: Any = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._connection_ready = asyncio.Event()
        self._active_subscriptions: Dict[str, Subscription] = {}

    # ---------- 生命周期管理 ----------

    @abstractmethod
    async def close(self) -> None:
        """
        优雅关闭适配器，释放所有连接、取消心跳和后台任务。
        实现必须幂等：重复调用不能抛出异常。
        """
        if self._is_closed:
            return
        self._is_closed = True
        self._closed_event.set()
        self._logger.info("Closing adapter...")
        # 并发取消所有活跃订阅
        async with self._subscription_lock:
            cancel_tasks = [sub.cancel() for sub in list(self._active_subscriptions.values())]
            self._active_subscriptions.clear()
        if cancel_tasks:
            await asyncio.gather(*cancel_tasks, return_exceptions=True)
        # 停止心跳
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            finally:
                self._heartbeat_task = None
        self._connection_ready.clear()
        self._logger.info("Adapter closed")

    async def shutdown(self) -> None:
        """公共关闭方法，捕获异常避免上层中断。"""
        try:
            await self.close()
        except Exception as e:
            self._logger.error(f"Shutdown error: {e}")

    async def __aenter__(self) -> BaseMarketDataAdapter:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type:
            self._logger.warning(f"Exception in context: {exc_type.__name__}: {exc_val}")
        await self.close()
        # 重要：不返回 True，允许异常继续传播以进行上层处理

    def __del__(self) -> None:
        """尝试在对象销毁时清理资源（不能依赖，仅兜底）。"""
        if not self._is_closed:
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self.close())
                else:
                    loop.run_until_complete(self.close())
            except Exception:
                pass

    # ---------- 连接与健康检查 ----------

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @final
    async def ping(self, timeout: float = 5.0) -> PingResult:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        start = time.monotonic()
        try:
            await asyncio.wait_for(self._send_ping(), timeout=timeout)
            latency = (time.monotonic() - start) * 1000.0
            return PingResult(status='healthy', latency_ms=latency, message='')
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000.0
            return PingResult(status='timeout', latency_ms=elapsed, message='timeout')
        except NotImplementedError:
            return PingResult(status='not_implemented', latency_ms=0.0, message='_send_ping not implemented')
        except asyncio.CancelledError:
            self._logger.debug("Ping cancelled")
            return PingResult(status='unhealthy', latency_ms=0.0, message='ping cancelled')
        except Exception as e:
            self._logger.error(f"Ping failed: {e}")
            return PingResult(status='unhealthy', latency_ms=0.0, message=str(e))

    async def _send_ping(self) -> None:
        """子类应覆盖以实现具体的连通性测试（如获取服务器时间）。"""
        raise NotImplementedError("Subclass must implement _send_ping method")

    @final
    async def wait_for_ready(self, timeout: float = 30.0) -> None:
        if self._is_closed:
            raise RuntimeError("Adapter already closed")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            await asyncio.wait_for(
                asyncio.shield(self._connection_ready.wait()),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError("Wait for ready timed out")
        if self._is_closed:
            raise RuntimeError("Adapter closed while waiting for ready")

    def _mark_ready(self) -> None:
        """子类在连接建立成功后调用，幂等。"""
        if not self._connection_ready.is_set():
            self._connection_ready.set()
            self._logger.debug("Adapter marked as ready")

    def _reset_connection_state(self) -> None:
        """重置连接状态，通常在断开或重连前调用。"""
        self._connection_ready.clear()
        self._logger.debug("Connection state reset")

    @abstractmethod
    async def reconnect(self) -> None:
        """强制重新连接。子类应调用 _reset_connection_state 并在成功后调用 _mark_ready。"""
        ...

    # ---------- 数据订阅 ----------

    def _validate_interval(self, interval: str) -> None:
        if not isinstance(interval, str):
            raise TypeError("interval must be a string")
        if interval not in self.supported_intervals:
            raise ValueError(f"Unsupported interval: {interval}")

    async def _add_subscription(self, sub: Subscription) -> None:
        async with self._subscription_lock:
            if sub.id in self._active_subscriptions:
                self._logger.warning(f"Duplicate subscription id: {sub.id}")
            self._active_subscriptions[sub.id] = sub

    async def _remove_subscription(self, sub_id: str) -> None:
        async with self._subscription_lock:
            self._active_subscriptions.pop(sub_id, None)

    @abstractmethod
    async def subscribe_klines(self, symbol: str, interval: str, **kwargs: Any) -> Subscription:
        """订阅K线数据流。子类应调用 _validate_interval 并在成功后将订阅加入 _add_subscription。"""
        ...

    @abstractmethod
    async def subscribe_trades(self, symbol: str, min_volume: float = 0.0, **kwargs: Any) -> Subscription:
        """订阅逐笔成交流。"""
        ...

    @abstractmethod
    async def get_recent_klines(self, symbol: str, interval: str, limit: int) -> List[Kline]:
        """获取最近 limit 根K线。"""
        ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: Optional[int] = None) -> OrderBook:
        """获取订单簿快照。"""
        ...

    @abstractmethod
    async def stream_ticks(self, symbol: str, **kwargs: Any) -> AsyncIterator[Tick]:
        """返回逐笔成交的异步迭代器。"""
        ...

    # ---------- 工具方法 ----------

    @abstractmethod
    async def get_server_time(self) -> ServerTimeResult:
        """获取交易所服务器时间及本地偏差（毫秒）。"""
        ...

    @abstractmethod
    async def get_symbols(self, filter: str = "") -> List[str]:
        """获取可用交易对列表，可提供过滤字符串。"""
        ...

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        """获取交易对的详细规则，若不存在返回 None。"""
        ...

    async def preload_historical_data(self, symbols: List[str], intervals: List[str], days: int = 7) -> None:
        """可选的预热数据方法，子类可按需覆盖。"""
        if days > self.max_preload_days:
            raise ValueError(f"days cannot exceed {self.max_preload_days}")

    # ---------- 心跳管理 ----------

    def start_heartbeat(self, interval_sec: float) -> None:
        """启动后台心跳任务（必须在异步上下文中调用）。如果已存在则先停止旧的。"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(interval_sec), name=f"{self.__class__.__name__}_heartbeat"
        )

    async def stop_heartbeat(self) -> None:
        """停止心跳并等待任务结束。"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            finally:
                self._heartbeat_task = None

    async def _heartbeat_loop(self, interval: float) -> None:
        """心跳循环，包含指数退避和最大失败次数。"""
        failure_count = 0
        max_failures = self.heartbeat_max_failures
        while not self._is_closed:
            try:
                await asyncio.sleep(interval)
                if self._is_closed:
                    break
                await self._send_heartbeat()
                failure_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                failure_count += 1
                if failure_count >= max_failures:
                    self._logger.critical(f"Heartbeat failed {failure_count} times, stopping heartbeat")
                    break
                backoff = min(interval, 2 ** (failure_count - 1))
                self._logger.warning(f"Heartbeat failed ({failure_count}/{max_failures}), retry in {backoff}s: {e}")
                await asyncio.sleep(backoff)

    async def _send_heartbeat(self) -> None:
        """子类可覆盖以定义心跳动作（默认发送 ping）。"""
        await self._send_ping()

    # ---------- 配置与元数据 ----------

    @classmethod
    def get_capabilities(cls) -> Dict[str, Any]:
        """返回适配器功能集的深拷贝。"""
        return copy.deepcopy(cls.capabilities)

    @classmethod
    def get_supported_intervals(cls) -> List[str]:
        return cls.supported_intervals.copy()

    @property
    def raw_config(self) -> dict:
        """⚠️ 返回未脱敏配置的浅拷贝，谨慎使用。"""
        return self._config.copy()

    @property
    def config(self) -> dict:
        """返回脱敏后的配置副本。"""
        return self.sanitize_config(self._config)

    def export_config(self) -> dict:
        return self.config

    @staticmethod
    def sanitize_config(config: dict, depth: int = 0, max_depth: int = 5) -> dict:
        """递归脱敏配置，处理字典和列表中的敏感字段。"""
        if depth > max_depth or not isinstance(config, dict):
            return config
        sensitive_keys = {'api_key', 'secret', 'passphrase'}
        safe = {}
        for k, v in config.items():
            if k in sensitive_keys:
                safe[k] = '***'
            elif isinstance(v, dict):
                safe[k] = BaseMarketDataAdapter.sanitize_config(v, depth + 1, max_depth)
            elif isinstance(v, list):
                safe[k] = [
                    BaseMarketDataAdapter.sanitize_config(item, depth + 1, max_depth) if isinstance(item, dict) else item
                    for item in v
                ]
            else:
                safe[k] = v
        return safe

    def update_config(self, new_config: AdapterConfig) -> None:
        """更新配置（需在连接关闭状态下调用）。"""
        if not self._is_closed:
            raise RuntimeError("Cannot update config while adapter is active")
        self._config = self._validate_and_normalize_config(new_config)

    # ---------- 监控 ----------

    def set_metrics_collector(self, collector: Any) -> None:
        self._metrics_collector = collector

    def get_metrics(self) -> Dict[str, Union[int, float]]:
        return {
            'connection_ready': 1 if self._connection_ready.is_set() else 0,
            'active_subscriptions': len(self._active_subscriptions),
        }

    # ---------- 内部辅助 ----------

    def _validate_and_normalize_config(self, config: AdapterConfig) -> Dict[str, Any]:
        cfg = dict(config)
        if not cfg.get('base_url'):
            raise ValueError("Missing base_url in config")
        base = cfg['base_url']
        if not (base.startswith('http://') or base.startswith('https://')):
            raise ValueError("base_url must start with http:// or https://")
        cfg.setdefault('timeout', 10.0)
        cfg.setdefault('retry_count', 3)
        cfg.setdefault('verify_ssl', True)
        cfg.setdefault('debug', False)
        if cfg['timeout'] <= 0:
            raise ValueError("timeout must be positive")
        return cfg

    @classmethod
    def normalize_symbol(cls, symbol: str) -> str:
        if not symbol:
            return ""
        return symbol.replace(' ', '').replace('/', '').upper()

    async def _record_metric(self, name: str, value: Union[int, float]) -> None:
        if self._metrics_collector:
            async with self._metrics_lock:
                try:
                    self._metrics_collector.record(name, value)
                except Exception:
                    pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(ready={self._connection_ready.is_set()}, closed={self._is_closed})>"

__all__ = [
    'BaseMarketDataAdapter',
    'AdapterConfig',
    'PingResult',
    'ServerTimeResult',
    'Subscription',
    'SymbolInfo',
    'Kline',
    'OrderBook',
    'Tick',
]
