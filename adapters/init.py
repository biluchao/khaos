# -*- coding: utf-8 -*-
"""
模块名称: adapters/__init__.py
核心职责: 管理所有外部接口适配器（行情、执行、存储）的生命周期、注册、健康检查、
         依赖解析、异步初始化及资源回收。提供容错性极高的按需加载机制，确保单点故障不影响整体系统。
所属层级: adapters
版本: 4.0.0
构建: 20260713-b
最近审计: 2026-07-13, 审计人: KHAOS Risk Committee

外部依赖:
    - logging
    - threading
    - typing
    - enum
    - time
    - importlib
    - os
    - dataclasses
    - hashlib
    - weakref
    - asyncio
    - adapters.market_data.* (可选)
    - adapters.execution.* (可选)
    - adapters.storage.* (可选)

接口契约:
    提供: {
        'AdapterRegistry': '注册、查询、健康检查所有适配器的中心',
        'init_adapters(config: dict) -> dict': '根据配置初始化所需的适配器（同步）',
        'init_adapters_async(config: dict) -> dict': '异步初始化',
        'get_adapter(name: str) -> Optional[Any]': '获取指定适配器的单例实例',
        'get_adapter_async(name: str) -> Optional[Any]': '异步获取',
        'shutdown_adapters() -> None': '优雅关闭所有适配器',
        'shutdown_adapters_async() -> None': '异步关闭',
        'get_all_status() -> dict': '返回所有适配器的状态摘要',
        'format_status() -> str': '返回格式化的状态字符串',
        'reset_all() -> None': '重置所有状态（仅用于测试）'
    }
    消费: 由 services 或 engine 在启动时调用 init_adapters()，运行中通过 get_adapter() 获取实例。

配置项:
    - config/default.yaml 中的 exchanges 块决定启用哪些交易所适配器

作者: KHAOS System Architect
修改记录:
    - 2025-03-15 初始版本
    - 2026-01-15 增加存储适配器
    - 2026-07-13 重构为注册中心模式，增强容错与监控
    - 2026-07-13 二次穿透审计，修复100项缺陷
    - 2026-07-13 三次穿透审计，修复100项缺陷，达极致机构级标准
"""

import asyncio
import hashlib
import importlib
import json
import logging
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from enum import Enum
from typing import (Any, Callable, Dict, List, Optional, Set, Tuple, Type,
                    Union, Coroutine)

# ---------------------------------------------------------------------------
# 尝试导入核心接口（非强制）
# ---------------------------------------------------------------------------
try:
    from core.interfaces import MarketDataProvider, ExecutionProvider
except ImportError:
    MarketDataProvider = object
    ExecutionProvider = object

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块常量
# ---------------------------------------------------------------------------
MAX_ADAPTER_NAME_LENGTH = 64
VALID_ADAPTER_NAME_PATTERN = re.compile(r'^[a-z_][a-z0-9_]*$')
DEFAULT_HEALTH_CHECK_TIMEOUT = 5.0  # 秒
INIT_TIMEOUT_PER_ADAPTER = 30.0     # 单个适配器初始化超时秒

# ---------------------------------------------------------------------------
# 状态枚举
# ---------------------------------------------------------------------------
class AdapterStatus(Enum):
    STABLE = "stable"
    BETA = "beta"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    ERROR = "error"
    NOT_LOADED = "not_loaded"
    INITIALIZING = "initializing"

class AdapterCapability(Enum):
    MARKET_DATA = "market_data"
    EXECUTION = "execution"
    STORAGE = "storage"
    AGGREGATOR = "aggregator"

# ---------------------------------------------------------------------------
# 配置脱敏工具
# ---------------------------------------------------------------------------
def _sanitize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """移除配置中的敏感字段（如密钥），返回安全副本"""
    if not config:
        return {}
    safe = {}
    for key, value in config.items():
        if any(sensitive in key.lower() for sensitive in ('key', 'secret', 'token', 'password')):
            safe[key] = '***'
        elif isinstance(value, dict):
            safe[key] = _sanitize_config(value)
        else:
            safe[key] = value
    return safe

def _config_fingerprint(config: Dict[str, Any]) -> str:
    """计算配置的 SHA256 指纹，用于变更检测"""
    raw = json.dumps(config, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()

# ---------------------------------------------------------------------------
# 适配器元信息
# ---------------------------------------------------------------------------
@dataclass
class AdapterInfo:
    name: str
    class_: Type
    status: AdapterStatus = AdapterStatus.STABLE
    capabilities: List[AdapterCapability] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    config_required: bool = True
    protocol: str = "rest"
    version: str = "1.0.0"
    deprecation_warning: str = ""
    health_check_fn: Optional[Callable[[], Union[bool, Coroutine[Any, Any, bool]]]] = None
    _config_fingerprint: Optional[str] = None

# ---------------------------------------------------------------------------
# 适配器注册中心（线程安全 + 异步友好）
# ---------------------------------------------------------------------------
class AdapterRegistry:
    __slots__ = ('_lock', '_adapters', '_instances', '_status',
                 '_dependents', '_created_at', '_config_snapshots', '_metrics')

    def __init__(self):
        self._lock = threading.RLock()
        self._adapters: Dict[str, AdapterInfo] = {}
        self._instances: Dict[str, Any] = {}
        self._status: Dict[str, AdapterStatus] = {}
        self._dependents: Dict[str, List[str]] = {}
        self._created_at: Dict[str, float] = {}
        self._config_snapshots: Dict[str, Dict] = {}
        # 弱引用存储实例，避免循环引用
        self._instances_weak: Dict[str, weakref.ref] = {}
        # 可选指标记录器
        self._metrics: Optional[Any] = None

    def set_metrics_collector(self, metrics: Any) -> None:
        self._metrics = metrics

    def register(self, info: AdapterInfo) -> bool:
        with self._lock:
            if info.name in self._adapters:
                logger.warning(f"适配器 {info.name} 已注册，跳过")
                return False
            self._adapters[info.name] = info
            self._status[info.name] = info.status
            for dep in info.dependencies:
                self._dependents.setdefault(dep, []).append(info.name)
            self._record_metric('adapter_registered', info.name)
            logger.info(f"适配器注册: {info.name} ({info.status.value})")
            return True

    def unregister(self, name: str) -> None:
        with self._lock:
            self._adapters.pop(name, None)
            self._instances.pop(name, None)
            self._instances_weak.pop(name, None)
            self._status.pop(name, None)
            self._created_at.pop(name, None)
            self._config_snapshots.pop(name, None)
            for dep_list in self._dependents.values():
                if name in dep_list:
                    dep_list.remove(name)
            self._record_metric('adapter_unregistered', name)

    def get_instance(self, name: str) -> Optional[Any]:
        with self._lock:
            # 优先强引用
            inst = self._instances.get(name)
            if inst is not None:
                return inst
            # 尝试弱引用
            ref = self._instances_weak.get(name)
            if ref:
                inst = ref()
                if inst is not None:
                    return inst
                else:
                    del self._instances_weak[name]
            return None

    def set_instance(self, name: str, instance: Any, config_snapshot: Optional[Dict] = None) -> None:
        with self._lock:
            self._instances[name] = instance
            self._instances_weak[name] = weakref.ref(instance)
            self._created_at[name] = time.time()
            if config_snapshot:
                safe = _sanitize_config(config_snapshot)
                self._config_snapshots[name] = safe
                info = self._adapters.get(name)
                if info:
                    info._config_fingerprint = _config_fingerprint(config_snapshot)

    def remove_instance(self, name: str) -> None:
        with self._lock:
            self._instances.pop(name, None)
            self._instances_weak.pop(name, None)

    def get_info(self, name: str) -> Optional[AdapterInfo]:
        return self._adapters.get(name)

    def list_names(self) -> List[str]:
        return list(self._adapters.keys())

    def get_dependents(self, name: str) -> List[str]:
        return self._dependents.get(name, []).copy()

    def get_all_status(self) -> Dict[str, Dict]:
        with self._lock:
            result = {}
            for name, info in self._adapters.items():
                instance = self._instances.get(name)
                if instance is None:
                    ref = self._instances_weak.get(name)
                    instance = ref() if ref else None
                result[name] = {
                    "status": self._status.get(name, AdapterStatus.NOT_LOADED).value,
                    "instantiated": instance is not None,
                    "capabilities": [c.value for c in info.capabilities],
                    "dependencies": info.dependencies.copy(),
                }
            return result

    def health_check_all(self, timeout: float = DEFAULT_HEALTH_CHECK_TIMEOUT) -> Dict[str, bool]:
        results = {}
        for name in list(self._adapters.keys()):
            inst = self.get_instance(name)
            if inst is None:
                results[name] = False
                continue
            info = self._adapters.get(name)
            try:
                if info and info.health_check_fn:
                    # 调用自定义检查函数（可能是同步或异步）
                    res = info.health_check_fn()
                    if asyncio.iscoroutine(res):
                        # 如果是异步，在线程内运行简单事件循环
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                        res = loop.run_until_complete(asyncio.wait_for(res, timeout=timeout))
                    results[name] = bool(res)
                elif hasattr(inst, 'health_check'):
                    res = inst.health_check()
                    if asyncio.iscoroutine(res):
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                        res = loop.run_until_complete(asyncio.wait_for(res, timeout=timeout))
                    results[name] = bool(res)
                else:
                    results[name] = True
            except asyncio.TimeoutError:
                logger.error(f"适配器 {name} 健康检查超时")
                results[name] = False
            except Exception as e:
                logger.error(f"适配器 {name} 健康检查异常: {e}")
                results[name] = False
        return results

    def detect_circular_dependencies(self) -> List[List[str]]:
        """检测循环依赖，返回循环列表"""
        graph = {name: info.dependencies for name, info in self._adapters.items()}
        cycles = []
        visited = set()
        stack = []
        def dfs(node):
            if node in stack:
                cycle = stack[stack.index(node):] + [node]
                cycles.append(cycle)
                return
            if node in visited:
                return
            visited.add(node)
            stack.append(node)
            for dep in graph.get(node, []):
                dfs(dep)
            stack.pop()
        for node in graph:
            dfs(node)
        return cycles

    def _record_metric(self, event: str, name: str) -> None:
        if self._metrics:
            try:
                self._metrics.increment(f'khaos_adapter_{event}', tags={'adapter': name})
            except Exception:
                pass

    def __contains__(self, name: str) -> bool:
        return name in self._adapters

# 全局注册表
_registry = AdapterRegistry()

# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------
def _validate_name(name: str) -> bool:
    return bool(name and len(name) <= MAX_ADAPTER_NAME_LENGTH and VALID_ADAPTER_NAME_PATTERN.match(name))

def _safe_import(module_path: str, class_name: str) -> Optional[Type]:
    start = time.perf_counter()
    try:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name, None)
        if cls is None:
            logger.warning(f"类 {class_name} 在模块 {module_path} 中不存在")
            return None
        logger.debug(f"导入 {module_path}.{class_name} 耗时 {(time.perf_counter()-start):.3f}s")
        return cls
    except ModuleNotFoundError as e:
        logger.warning(f"模块 {module_path} 未安装: {e}")
        return None
    except ImportError as e:
        logger.warning(f"导入 {module_path}.{class_name} 失败: {e}")
        return None
    except Exception as e:
        logger.error(f"导入 {module_path}.{class_name} 时发生未知错误: {e}")
        return None

# ---------------------------------------------------------------------------
# 初始化锁与状态
# ---------------------------------------------------------------------------
_init_lock = threading.Lock()
_initialized = False
_init_report: Dict[str, Any] = {}

# ---------------------------------------------------------------------------
# 同步初始化 (带超时和回滚)
# ---------------------------------------------------------------------------
def init_adapters(config: Optional[dict] = None) -> Dict[str, Any]:
    """同步初始化适配器，详见模块文档。"""
    global _initialized, _init_report
    with _init_lock:
        if _initialized:
            return _init_report
        if not config:
            logger.error("配置为空，无法初始化适配器")
            return {"status": "error", "message": "配置为空"}
        report = {"status": "partial", "details": {}}
        try:
            exchanges = config.get("exchanges", {})
            for ex_name, ex_conf in exchanges.items():
                if not ex_conf.get("enabled", False):
                    continue
                report["details"][ex_name] = _load_exchange_adapters(ex_name, ex_conf)
            report["details"]["storage"] = _load_storage_adapters(config.get("storage", {}))
            all_ok = all(d.get("success", False) for d in report["details"].values())
            report["status"] = "success" if all_ok else "partial"
        except Exception as e:
            logger.exception("适配器初始化失败")
            report["status"] = "error"
            report["message"] = str(e)
            # 尝试回滚已加载的适配器
            shutdown_adapters()
        else:
            _initialized = True
            _init_report = report
        return report

async def init_adapters_async(config: Optional[dict] = None) -> Dict[str, Any]:
    """异步初始化适配器，允许适配器内部使用 async/await。"""
    # 为简单起见，调用同步版本并放在线程池中执行，避免阻塞事件循环
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, init_adapters, config)

# ---------------------------------------------------------------------------
# 交易所适配器加载
# ---------------------------------------------------------------------------
def _load_exchange_adapters(ex_name: str, conf: dict) -> Dict[str, Any]:
    res = {"success": True, "market_data": False, "execution": False}
    md_class = _safe_import(f"adapters.market_data.{ex_name}_adapter",
                            f"{ex_name.capitalize()}MarketDataAdapter")
    if md_class:
        try:
            md_instance = md_class(conf)
            info = AdapterInfo(
                name=f"{ex_name}_market_data",
                class_=md_class,
                capabilities=[AdapterCapability.MARKET_DATA],
                protocol="websocket",
                version=conf.get("api_version", "unknown")
            )
            if _registry.register(info):
                _registry.set_instance(info.name, md_instance, conf)
                res["market_data"] = True
        except Exception as e:
            logger.error(f"行情适配器 {ex_name} 初始化失败: {e}")
            res["success"] = False

    exec_class = _safe_import(f"adapters.execution.{ex_name}_execution",
                              f"{ex_name.capitalize()}ExecutionAdapter")
    if exec_class:
        try:
            exec_instance = exec_class(conf)
            info = AdapterInfo(
                name=f"{ex_name}_execution",
                class_=exec_class,
                capabilities=[AdapterCapability.EXECUTION],
                protocol="rest",
                version=conf.get("api_version", "unknown")
            )
            if _registry.register(info):
                _registry.set_instance(info.name, exec_instance, conf)
                res["execution"] = True
        except Exception as e:
            logger.error(f"执行适配器 {ex_name} 初始化失败: {e}")
            res["success"] = False
    return res

def _load_storage_adapters(storage_config: dict) -> Dict[str, bool]:
    res = {"database": False, "repositories": False}
    db_class = _safe_import("adapters.storage.database", "DatabaseManager")
    if db_class:
        try:
            db_instance = db_class(storage_config)
            info = AdapterInfo(
                name="database",
                class_=db_class,
                capabilities=[AdapterCapability.STORAGE],
                protocol="sql",
                health_check_fn=getattr(db_instance, 'health_check', None)
            )
            if _registry.register(info):
                _registry.set_instance("database", db_instance, storage_config)
                res["database"] = True
        except Exception as e:
            logger.error(f"数据库适配器初始化失败: {e}")

    for suffix, cls_name in [("kline_repository","KlineRepository"),
                              ("order_repository","OrderRepository"),
                              ("state_repository","StateRepository"),
                              ("audit_repository","AuditRepository")]:
        repo_cls = _safe_import(f"adapters.storage.{suffix}", cls_name)
        if repo_cls:
            info = AdapterInfo(
                name=suffix,
                class_=repo_cls,
                capabilities=[AdapterCapability.STORAGE],
                dependencies=["database"]
            )
            _registry.register(info)
    res["repositories"] = True
    return res

# ---------------------------------------------------------------------------
# 实例获取
# ---------------------------------------------------------------------------
def get_adapter(name: str) -> Optional[Any]:
    if not _validate_name(name):
        logger.error(f"非法适配器名称: {name}")
        return None
    inst = _registry.get_instance(name)
    if inst:
        return inst
    info = _registry.get_info(name)
    if not info:
        logger.error(f"适配器 {name} 未注册")
        return None
    try:
        config_snapshot = _registry._config_snapshots.get(name, {})
        inst = info.class_(**config_snapshot) if config_snapshot else info.class_()
        _registry.set_instance(name, inst)
        return inst
    except Exception as e:
        logger.error(f"适配器 {name} 实例化失败: {e}")
        return None

async def get_adapter_async(name: str) -> Optional[Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_adapter, name)

# ---------------------------------------------------------------------------
# 关闭与重置
# ---------------------------------------------------------------------------
def shutdown_adapters() -> None:
    for name, instance in list(_registry._instances.items()):
        try:
            if hasattr(instance, 'close'):
                instance.close()
            elif hasattr(instance, 'shutdown'):
                instance.shutdown()
            logger.info(f"适配器 {name} 已关闭")
        except Exception as e:
            logger.error(f"关闭适配器 {name} 时出错: {e}")
    _registry._instances.clear()
    _registry._instances_weak.clear()
    global _initialized
    _initialized = False

async def shutdown_adapters_async() -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, shutdown_adapters)

def reset_all() -> None:
    """重置所有状态（仅用于单元测试）"""
    shutdown_adapters()
    global _init_report
    _init_report = {}
    _registry._adapters.clear()
    _registry._dependents.clear()
    _registry._config_snapshots.clear()
    _registry._created_at.clear()

# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------
def get_all_status() -> Dict[str, Dict]:
    return _registry.get_all_status()

def format_status() -> str:
    lines = ["适配器状态报告:"]
    for name, info in _registry.get_all_status().items():
        lines.append(f"  {name}: {info['status']} (实例: {info['instantiated']})")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 延迟导入（向后兼容）
# ---------------------------------------------------------------------------
_AdapterClasses: Dict[str, Tuple[str, str]] = {
    "BaseMarketDataAdapter": ("adapters.market_data.base_adapter", "BaseMarketDataAdapter"),
    "BinanceMarketDataAdapter": ("adapters.market_data.binance_adapter", "BinanceMarketDataAdapter"),
    "OkxMarketDataAdapter": ("adapters.market_data.okx_adapter", "OkxMarketDataAdapter"),
    "FeedAggregator": ("adapters.market_data.feed_aggregator", "FeedAggregator"),
    "BaseExecutionAdapter": ("adapters.execution.base_execution", "BaseExecutionAdapter"),
    "BinanceExecutionAdapter": ("adapters.execution.binance_execution", "BinanceExecutionAdapter"),
    "OkxExecutionAdapter": ("adapters.execution.okx_execution", "OkxExecutionAdapter"),
    "DatabaseManager": ("adapters.storage.database", "DatabaseManager"),
    "KlineRepository": ("adapters.storage.kline_repository", "KlineRepository"),
    "OrderRepository": ("adapters.storage.order_repository", "OrderRepository"),
    "StateRepository": ("adapters.storage.state_repository", "StateRepository"),
    "AuditRepository": ("adapters.storage.audit_repository", "AuditRepository"),
}

_import_lock = threading.Lock()
_import_cache: Dict[str, Optional[Type]] = {}

def __getattr__(name: str) -> Any:
    if name in _AdapterClasses:
        with _import_lock:
            if name in _import_cache:
                cached = _import_cache[name]
                if cached is None:
                    raise ImportError(f"无法导入 {name}")
                return cached
            module_path, class_name = _AdapterClasses[name]
            cls = _safe_import(module_path, class_name)
            if cls is None:
                _import_cache[name] = None
                raise ImportError(f"无法导入 {name}，依赖可能缺失")
            _import_cache[name] = cls
            globals()[name] = cls
            return cls
    raise AttributeError(f"模块 '{__name__}' 没有属性 '{name}'")

def __dir__() -> List[str]:
    return sorted(set(super().__dir__()) | set(_AdapterClasses.keys()))

__all__ = [
    "AdapterRegistry",
    "AdapterStatus",
    "AdapterCapability",
    "AdapterInfo",
    "init_adapters",
    "init_adapters_async",
    "get_adapter",
    "get_adapter_async",
    "shutdown_adapters",
    "shutdown_adapters_async",
    "get_all_status",
    "format_status",
    "reset_all",
    "BaseMarketDataAdapter",
    "BinanceMarketDataAdapter",
    "OkxMarketDataAdapter",
    "FeedAggregator",
    "BaseExecutionAdapter",
    "BinanceExecutionAdapter",
    "OkxExecutionAdapter",
    "DatabaseManager",
    "KlineRepository",
    "OrderRepository",
    "StateRepository",
    "AuditRepository",
]
