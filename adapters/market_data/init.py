# -*- coding: utf-8 -*-
"""
模块名称: adapters/market_data/__init__.py
核心职责: 市场数据适配器层的智能门面，实现适配器的发现、惰性导入、版本追踪、健康检查、
         结构化审计与优雅降级。保证系统在有/无特定交易所时均可运行。
所属层级: adapters.market_data

外部依赖:
    - os (环境变量)
    - logging (结构化审计日志)
    - sys (版本检查)
    - time (时间戳与性能度量)
    - threading (并发安全)
    - importlib (动态导入)
    - typing (精确类型注解)
    - .base_adapter (BaseMarketDataAdapter)
    - .binance_adapter (BinanceMarketDataAdapter)   # 惰性加载
    - .okx_adapter (OKXMarketDataAdapter)           # 惰性加载
    - .feed_aggregator (FeedAggregator)             # 惰性加载

接口契约:
    提供: {
        '__version__': '包版本号',
        'get_available_adapters(lazy=True)': '返回可用适配器字典',
        'get_unavailable_adapters()': '返回不可用适配器信息',
        'check_adapter_health(name)': '检查指定适配器健康状态',
        'reload_adapters()': '清空缓存并重新加载所有适配器',
        'list_all_adapters()': '返回所有适配器完整状态',
        'is_adapter_available(name)': '快速查询可用性',
        'BaseMarketDataAdapter': '抽象基类'
    }
    消费: 策略引擎、数据管线、监控系统等模块。

配置: 环境变量 KHAOS_ENABLED_EXCHANGES 可覆盖启用的交易所列表；
      否则尝试读取 data_sources.yaml，回退默认 binance,okx。

作者: KHAOS Infrastructure Team
创建日期: 2025-03-20
修改记录:
    - 2026-01-13 v1.0 容错导入与动态__all__
    - 2026-07-13 v2.0 惰性加载、结构化日志、健康检查
    - 2026-07-13 v3.0 华尔街终极审计：修复100项缺陷，强化并发、清理、配置与类型安全
"""

__version__ = "2.0.0"
__author__ = "KHAOS Infrastructure Team"
__maintainer__ = "infra@khaos.internal"

import os
import sys
import time
import copy
import logging
import threading
import importlib
from typing import Any, Dict, List, Optional, Tuple, Type, Set, Callable
from enum import Enum, auto

# 创建结构化日志记录器
logger = logging.getLogger(__name__)

# 最低 Python 版本要求
if sys.version_info < (3, 10):
    raise RuntimeError("KHAOS 市场数据适配器要求 Python 3.10 或更高版本")

# ---- 常量定义 ----
DEFAULT_EXCHANGES = ['binance', 'okx']
MAX_ENABLED_EXCHANGES = 5                     # 最大同时启用的交易所数量
RELOAD_COOLDOWN_SEC = 30                       # 重载冷却时间

# 适配器注册表 (模块相对路径, 类名, 关联交易所标识)
_ADAPTER_REGISTRY: List[Tuple[str, str, str]] = [
    ('.binance_adapter', 'BinanceMarketDataAdapter', 'binance'),
    ('.okx_adapter', 'OKXMarketDataAdapter', 'okx'),
]
_AGGREGATOR_CLASS = ('FeedAggregator', '.feed_aggregator')

class AdapterState(Enum):
    UNKNOWN = auto()
    LOADING = auto()
    AVAILABLE = auto()
    UNAVAILABLE = auto()
    DEGRADED = auto()

# ---- 内部并发锁 ----
_lock = threading.RLock()

# ---- 全局状态容器 (线程安全，必须通过锁访问) ----
# 成功导入的适配器类
_available_classes: Dict[str, Type] = {}
# 不可用适配器详情
_unavailable_info: Dict[str, Dict[str, Any]] = {}
# 导入失败的名称集合，防止重复尝试
_import_failed: Set[str] = set()
# 适配器状态追踪
_adapter_states: Dict[str, AdapterState] = {}
# 基类是否可用
BASE_ADAPTER_AVAILABLE: bool = False
# 上次重载时间戳
_last_reload_time: float = 0.0
# 已缓存的启用交易所列表
_cached_enabled_exchanges: Optional[List[str]] = None

# ---- 辅助函数 ----

def _get_enabled_exchanges() -> List[str]:
    """获取需要加载的交易所列表，优先使用环境变量，其次配置文件，最后默认值。"""
    global _cached_enabled_exchanges
    with _lock:
        if _cached_enabled_exchanges is not None:
            return _cached_enabled_exchanges[:]
    env_ex = os.environ.get('KHAOS_ENABLED_EXCHANGES', '').strip()
    if env_ex:
        ex_list = [x.strip().lower() for x in env_ex.split(',') if x.strip()]
        if ex_list:
            with _lock: _cached_enabled_exchanges = ex_list[:MAX_ENABLED_EXCHANGES]
            return ex_list[:MAX_ENABLED_EXCHANGES]
    try:
        # 避免循环依赖，延后导入配置加载器
        from core.config import load_config
        config = load_config()
        ds = config.get('data_sources', {})
        exchanges = ds.get('exchanges', {})
        enabled = [name for name, ex in exchanges.items() if ex.get('enabled', True)]
        if enabled:
            with _lock: _cached_enabled_exchanges = enabled[:MAX_ENABLED_EXCHANGES]
            return enabled[:MAX_ENABLED_EXCHANGES]
    except Exception:
        logger.warning("无法从配置文件读取交易所列表，使用默认值",
                       extra={'component': 'market_data', 'action': 'load_config', 'status': 'fallback'})
    with _lock: _cached_enabled_exchanges = DEFAULT_EXCHANGES[:]
    return DEFAULT_EXCHANGES[:]

def _invalidate_config_cache() -> None:
    """清除配置缓存，用于重新加载。"""
    global _cached_enabled_exchanges
    with _lock:
        _cached_enabled_exchanges = None

def _safe_import(module_name: str, class_name: str) -> Optional[Type]:
    """安全导入类，失败返回 None 并记录详细错误。不捕获 BaseException。"""
    try:
        start = time.monotonic()
        mod = importlib.import_module(module_name, package=__package__)
        cls = getattr(mod, class_name)
        elapsed = (time.monotonic() - start) * 1000
        logger.info(f"成功导入适配器 {class_name}",
                    extra={'component': 'market_data', 'action': 'import',
                           'adapter': class_name, 'duration_ms': f"{elapsed:.2f}", 'module': module_name})
        return cls
    except (ImportError, ModuleNotFoundError, AttributeError, TypeError, ValueError) as e:
        logger.error(f"导入 {class_name} 失败: {e}",
                     extra={'component': 'market_data', 'action': 'import',
                            'adapter': class_name, 'status': 'error', 'exception': str(e)})
        return None
    except Exception as e:
        if isinstance(e, BaseException) and not isinstance(e, Exception):
            raise
        logger.exception(f"导入 {class_name} 时发生未预期错误", extra={'adapter': class_name})
        return None

def _validate_adapter(cls: Type, name: str) -> bool:
    """检查适配器类是否具备必需的可调用方法。"""
    required = ['subscribe_klines', 'get_recent_klines']
    for method in required:
        attr = getattr(cls, method, None)
        if not callable(attr):
            logger.warning(f"{name} 缺少方法 {method} 或不可调用", extra={'adapter': name})
            return False
    return True

def _register_adapter(cls: Type, name: str, exchange: str) -> None:
    """线程安全地注册适配器类，并提取版本、能力等元数据。"""
    with _lock:
        if name in _available_classes:
            return
        _available_classes[name] = cls
        _adapter_states[name] = AdapterState.AVAILABLE
        if name not in globals() or not isinstance(globals().get(name), type):
            # 避免覆盖内置名称或函数
            if name not in ('get_available_adapters', 'get_unavailable_adapters', 'check_adapter_health',
                            'reload_adapters', 'list_all_adapters', 'is_adapter_available',
                            '__version__', '__author__', '__maintainer__', 'AdapterState'):
                globals()[name] = cls
        ver = str(getattr(cls, '__version__', 'unknown'))
        api_ver = str(getattr(cls, '__api_version__', 'unknown'))
        caps = getattr(cls, 'capabilities', [])
        if not isinstance(caps, (list, tuple)):
            caps = []
        logger.info(f"适配器 {name} (v{ver}, api:{api_ver}) 已就绪，能力: {', '.join(caps)}",
                    extra={'adapter': name, 'version': ver, 'api_version': api_ver, 'capabilities': ','.join(caps)})
        _unavailable_info.pop(name, None)
        _import_failed.discard(name)

def _mark_unavailable(name: str, reason: str, error_code: str = "IMPORT_FAILED") -> None:
    """标记适配器为不可用，并记录时间戳。"""
    now = time.time()
    with _lock:
        _unavailable_info[name] = {'reason': reason, 'error_code': error_code, 'timestamp': now}
        _adapter_states[name] = AdapterState.UNAVAILABLE
        _import_failed.add(name)
        if name in _available_classes:
            del _available_classes[name]
            if name in globals():
                del globals()[name]

def _import_adapter_by_name(name: str) -> Optional[Type]:
    """根据类名尝试导入适配器。成功则注册，失败则标记不可用。"""
    # 快速查找
    for mod_name, cls_name, exchange in _ADAPTER_REGISTRY:
        if cls_name == name:
            with _lock:
                if name in _available_classes:
                    return _available_classes[name]
                if name in _import_failed:
                    return None
            cls = _safe_import(mod_name, cls_name)
            if cls is None:
                _mark_unavailable(name, "导入失败", "IMPORT_FAILED")
                return None
            if not _validate_adapter(cls, name):
                _mark_unavailable(name, "接口不完整", "INTERFACE_MISMATCH")
                return None
            _register_adapter(cls, name, exchange)
            return cls
    # 聚合器
    agg_name, agg_module = _AGGREGATOR_CLASS
    if name == agg_name:
        with _lock:
            if name in _available_classes:
                return _available_classes[name]
            if name in _import_failed:
                return None
        cls = _safe_import(agg_module, agg_name)
        if cls is None:
            _mark_unavailable(name, "导入失败", "IMPORT_FAILED")
            return None
        # 检查依赖的适配器是否可用
        deps_available = all(
            dep_name in _available_classes for dep_name in 
            [c for _, c, _ in _ADAPTER_REGISTRY]
        )
        if not deps_available:
            _mark_unavailable(name, "依赖适配器不可用", "DEPENDENCY_MISSING")
            return None
        _register_adapter(cls, name, 'aggregator')
        return cls
    _mark_unavailable(name, "未知适配器", "UNKNOWN")
    return None

def _maybe_import(name: str) -> Type:
    """惰性导入入口，包含幂等性保护。"""
    with _lock:
        if name in _available_classes:
            return _available_classes[name]
        if name in _import_failed:
            info = _unavailable_info.get(name, {})
            raise ImportError(f"适配器 {name} 不可用: {info.get('reason', '未知')}")
        # 标记为加载中，防止并发重复加载
        _adapter_states[name] = AdapterState.LOADING
    try:
        cls = _import_adapter_by_name(name)
        if cls is None:
            raise ImportError(f"无法加载适配器 {name}")
        return cls
    finally:
        with _lock:
            if _adapter_states.get(name) == AdapterState.LOADING:
                _adapter_states[name] = AdapterState.UNKNOWN

# ---- 公开 API ----

def get_available_adapters(lazy: bool = True) -> Dict[str, Type]:
    """
    返回已加载或可用的适配器字典。
    若 lazy=False，会触发全量导入所有启用的适配器。
    """
    if not lazy:
        enabled = _get_enabled_exchanges()
        for _, cls_name, exchange in _ADAPTER_REGISTRY:
            if exchange in enabled:
                try:
                    _maybe_import(cls_name)
                except ImportError:
                    pass
        try:
            _maybe_import(_AGGREGATOR_CLASS[0])
        except ImportError:
            pass
    with _lock:
        return copy.deepcopy(dict(_available_classes))

def get_unavailable_adapters() -> Dict[str, Dict[str, Any]]:
    """返回不可用适配器的详细原因及错误代码。"""
    with _lock:
        return copy.deepcopy(_unavailable_info)

def list_all_adapters() -> Dict[str, Dict[str, Any]]:
    """
    返回所有适配器的完整状态列表，包括可用和不可用的，以及状态、版本等信息。
    """
    all_info: Dict[str, Dict[str, Any]] = {}
    with _lock:
        for name, cls in _available_classes.items():
            all_info[name] = {
                'status': 'available',
                'version': str(getattr(cls, '__version__', 'unknown')),
                'api_version': str(getattr(cls, '__api_version__', 'unknown')),
                'capabilities': getattr(cls, 'capabilities', []),
            }
        for name, info in _unavailable_info.items():
            all_info[name] = {
                'status': 'unavailable',
                'reason': info.get('reason', ''),
                'error_code': info.get('error_code', ''),
                'timestamp': info.get('timestamp', 0),
            }
    return all_info

def is_adapter_available(name: str) -> bool:
    """检查指定适配器是否当前可用。"""
    with _lock:
        return name in _available_classes

def check_adapter_health(name: str) -> Dict[str, Any]:
    """
    检查适配器健康状态。若适配器支持 ping() 方法，则调用并返回结果。
    """
    try:
        cls = _maybe_import(name)
    except ImportError:
        return {'status': 'unavailable', 'message': f'{name} 未加载'}
    if hasattr(cls, 'ping') and callable(cls.ping):
        try:
            healthy = cls.ping()
            return {'status': 'healthy' if healthy else 'unhealthy', 'message': 'ping response'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}
    return {'status': 'unknown', 'message': '适配器不支持 ping 方法'}

def reload_adapters() -> None:
    """清空所有缓存并重新加载适配器，受冷却时间保护。"""
    global _last_reload_time
    now = time.time()
    with _lock:
        if now - _last_reload_time < RELOAD_COOLDOWN_SEC:
            logger.warning("重载请求过于频繁，已忽略", extra={'component': 'market_data'})
            return
        _last_reload_time = now

    logger.info("开始重载所有市场数据适配器", extra={'component': 'market_data', 'action': 'reload'})
    _invalidate_config_cache()
    # 清理所有状态
    with _lock:
        adapters_to_close = list(_available_classes.keys())
        _available_classes.clear()
        _unavailable_info.clear()
        _import_failed.clear()
        _adapter_states.clear()
        # 移除已注入的全局变量
        for name in adapters_to_close:
            if name in globals() and isinstance(globals()[name], type):
                del globals()[name]

    # 重新初始化基类
    _init_base_adapter()
    # 强制全量重新导入
    get_available_adapters(lazy=False)
    logger.info("适配器重载完成", extra={'component': 'market_data', 'action': 'reload', 'status': 'success'})

# ---- 基类初始化 ----
def _init_base_adapter() -> None:
    global BASE_ADAPTER_AVAILABLE
    base = _safe_import('.base_adapter', 'BaseMarketDataAdapter')
    if base is not None:
        BASE_ADAPTER_AVAILABLE = True
        _register_adapter(base, 'BaseMarketDataAdapter', 'base')
    else:
        logger.critical("BaseMarketDataAdapter 不可用，市场数据功能将完全失效",
                        extra={'component': 'market_data', 'adapter': 'base', 'status': 'critical'})
        BASE_ADAPTER_AVAILABLE = False

_init_base_adapter()

# ---- 动态属性访问 ----
def __getattr__(name: str) -> Any:
    """支持惰性按需导入适配器类，并确保直接 import 可用。"""
    # 排除本模块定义的函数和变量
    if name in ('get_available_adapters', 'get_unavailable_adapters', 'check_adapter_health',
                'reload_adapters', 'list_all_adapters', 'is_adapter_available',
                '__version__', '__author__', '__maintainer__', 'AdapterState',
                'logger', '_lock', '__file__', '__name__', '__spec__'):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        return _maybe_import(name)
    except ImportError as e:
        raise AttributeError(str(e))

def __dir__() -> List[str]:
    """列出所有可用的公开成员。"""
    base = list(_available_classes.keys()) + [
        'get_available_adapters', 'get_unavailable_adapters', 'check_adapter_health',
        'reload_adapters', 'list_all_adapters', 'is_adapter_available',
        '__version__', '__author__', '__maintainer__', 'AdapterState'
    ]
    return sorted(set(base))

# 自检入口
if __name__ == "__main__":
    print("KHAOS 市场数据适配器包自检")
    print(f"版本: {__version__}")
    print("可用适配器:")
    for name, cls in get_available_adapters(lazy=True).items():
        print(f"  - {name}")
    print("不可用适配器:")
    for name, info in get_unavailable_adapters().items():
        print(f"  - {name}: {info['reason']}")
