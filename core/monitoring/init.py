# -*- coding: utf-8 -*-
# requires Python >= 3.10
"""
模块名称: core/monitoring/__init__.py
核心职责: 统一导出系统健康检查、决策日志、意图日志及指标采集等监测组件。
         提供延迟加载、异步初始化/自检/关闭、失败重试、组件注册等高级特性。
所属层级: core.monitoring

外部依赖:
    - importlib (动态导入)
    - logging (日志记录)
    - threading (并发保护)
    - asyncio (异步支持)
    - collections.OrderedDict (保持字典顺序)
    - typing (类型注解)
    - .health_checker (HealthChecker)
    - .decision_logger (DecisionLogger)
    - .intent_logger (IntentLogger)
    - .metrics_collector (MetricsCollector)

接口契约:
    提供: {
        'HealthChecker': {
            'async healthcheck() -> None': '执行健康检查，失败抛出异常'
        },
        'DecisionLogger': {...},
        'IntentLogger': {...},
        'MetricsCollector': {...}
    }
    消费: 无外部消费，仅作为包内导出的聚合点。

作者: KHAOS System Architect
创建日期: 2025-04-10
修改记录:
    - 2026-01-12 增加意图日志与指标采集组件
    - 2026-07-13 机构级审计：增加导入容错、延迟加载、版本元数据、自检等功能
    - 2026-07-13 第二轮穿透审计：修正启动时强制加载、增加异步支持、线程安全、组件注册、优雅关闭等

版本: 3.0.0
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Callable, Type

# 包版本，与部署版本同步
__version__ = "3.0.0"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 延迟导入与容错机制
# ---------------------------------------------------------------------------
_COMPONENT_MODULES: Dict[str, str] = {
    "HealthChecker": ".health_checker",
    "DecisionLogger": ".decision_logger",
    "IntentLogger": ".intent_logger",
    "MetricsCollector": ".metrics_collector",
}

# 全局变量，初始为 None，使用时延迟加载
HealthChecker: Optional[Type[Any]] = None
DecisionLogger: Optional[Type[Any]] = None
IntentLogger: Optional[Type[Any]] = None
MetricsCollector: Optional[Type[Any]] = None

_available_components: Dict[str, bool] = {}
_component_status: Dict[str, str] = {}   # 缓存组件最后健康状态

# 线程锁
_lock = threading.Lock()
# 异步初始化锁
_init_lock = asyncio.Lock()

def _lazy_import(component_name: str) -> Optional[Type[Any]]:
    """安全地延迟导入一个监控组件，失败时记录并返回 None。"""
    global HealthChecker, DecisionLogger, IntentLogger, MetricsCollector
    global _available_components

    with _lock:
        # 如果已经尝试加载过，直接返回全局变量
        current = globals().get(component_name)
        if current is not None or _available_components.get(component_name) is False:
            return current

        try:
            module_path = _COMPONENT_MODULES[component_name]
            # 防止目录遍历
            if '..' in module_path:
                raise ValueError("不支持相对路径中的 ..")
            package = __package__ or __name__
            module = importlib.import_module(module_path, package=package)
            cls = getattr(module, component_name)
            # 运行时验证类完整性
            if not callable(cls):
                raise TypeError(f"{component_name} is not callable")
            # 可选：验证是否实现了 MonitorComponent 接口（通过 duck typing）
            globals()[component_name] = cls
            _available_components[component_name] = True
            logger.info("监控组件 %s 加载成功", component_name)
            return cls
        except Exception as e:
            logger.error("监控组件 %s 加载失败: %s", component_name, e)
            _available_components[component_name] = False
            globals()[component_name] = None
            return None

# ---------------------------------------------------------------------------
# 动态注册与重置
# ---------------------------------------------------------------------------

def register_component(name: str, module_path: str) -> None:
    """运行时注册新的监控组件，供后续延迟加载。"""
    with _lock:
        _COMPONENT_MODULES[name] = module_path
        # 清除旧状态以便重新加载
        _available_components.pop(name, None)
        globals().pop(name, None)
        logger.info("已注册新监控组件: %s -> %s", name, module_path)

def reset_component(name: str) -> None:
    """重置指定组件的加载状态，允许重新尝试导入。"""
    with _lock:
        _available_components.pop(name, None)
        globals()[name] = None
        logger.info("监控组件 %s 状态已重置", name)

def _reset_module_state() -> None:
    """重置整个模块的状态（仅用于测试）。"""
    global HealthChecker, DecisionLogger, IntentLogger, MetricsCollector
    with _lock:
        for name in list(_COMPONENT_MODULES.keys()):
            _available_components.pop(name, None)
            globals()[name] = None
        HealthChecker = DecisionLogger = IntentLogger = MetricsCollector = None

# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def get_available_components() -> Dict[str, bool]:
    """
    返回所有监控组件的可用状态。不会触发尚未加载的组件。
    """
    with _lock:
        # 对于尚未检查的组件，标记为 unknown，但不主动加载
        result = {}
        for name in _COMPONENT_MODULES:
            if name in _available_components:
                result[name] = _available_components[name]
            else:
                result[name] = False  # 未尝试加载
        return result

async def init_monitoring(retry: int = 1, **kwargs) -> Dict[str, bool]:
    """
    统一异步初始化所有可用的监控组件。
    若初始化失败，可根据 retry 参数重试。

    Returns:
        Dict[str, bool]: 组件名 -> 初始化是否成功
    """
    async with _init_lock:
        results = {}
        for name in _COMPONENT_MODULES:
            success = False
            for attempt in range(retry + 1):
                try:
                    cls = _lazy_import(name)
                    if cls is None:
                        raise RuntimeError(f"组件 {name} 不可用")
                    if hasattr(cls, 'initialize'):
                        init_fn = cls.initialize
                        if asyncio.iscoroutinefunction(init_fn):
                            await init_fn(**kwargs)
                        else:
                            init_fn(**kwargs)
                    success = True
                    break
                except Exception as e:
                    logger.warning("监控组件 %s 初始化失败 (尝试 %d/%d): %s", name, attempt+1, retry+1, e)
                    if attempt == retry:
                        success = False
            results[name] = success
        return results

async def selftest() -> Dict[str, str]:
    """
    异步自检：遍历所有可用组件并调用各自的 healthcheck 方法（如果存在）。
    假设 healthcheck 可以是同步或异步，失败时记录失败原因。

    Returns:
        OrderedDict[str, str]: 组件名 -> 状态描述
    """
    results = OrderedDict()
    for name in _COMPONENT_MODULES:
        cls = _lazy_import(name)
        if not cls:
            results[name] = "FAIL: 无法加载"
            _component_status[name] = "FAIL"
            continue
        if hasattr(cls, 'healthcheck'):
            try:
                check_fn = cls.healthcheck
                if asyncio.iscoroutinefunction(check_fn):
                    await check_fn()
                else:
                    check_fn()
                results[name] = "OK"
                _component_status[name] = "OK"
            except Exception as e:
                results[name] = f"FAIL: {e}"
                _component_status[name] = f"FAIL: {e}"
        else:
            results[name] = "N/A (无自检接口)"
            _component_status[name] = "N/A"
    return results

async def shutdown_monitoring() -> None:
    """
    优雅关闭所有监控组件，调用各自的 shutdown 方法（如果存在）。
    """
    for name in _COMPONENT_MODULES:
        cls = _lazy_import(name)
        if cls and hasattr(cls, 'shutdown'):
            try:
                shutdown_fn = cls.shutdown
                if asyncio.iscoroutinefunction(shutdown_fn):
                    await shutdown_fn()
                else:
                    shutdown_fn()
                logger.info("监控组件 %s 已关闭", name)
            except Exception as e:
                logger.error("监控组件 %s 关闭失败: %s", name, e)

def get_component_status() -> Dict[str, str]:
    """返回上次 selftest 记录的状态"""
    return dict(_component_status)

# ---------------------------------------------------------------------------
# 动态 __all__ 生成 (仅在显式刷新或请求时计算，避免启动时加载)
# ---------------------------------------------------------------------------
__all__: List[str] = []

def refresh_all() -> None:
    """更新 __all__ 列表，包含当前所有可用组件。"""
    global __all__
    available = get_available_components()
    __all__ = sorted([name for name, ok in available.items() if ok])
    logger.debug("__all__ 已刷新: %s", __all__)

# 模块加载完成，不主动触发组件加载，保持延迟加载特性
logger.info("KHAOS 监控模块 v%s 已就绪，组件按需加载。", __version__)
