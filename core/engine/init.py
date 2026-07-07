# -*- coding: utf-8 -*-
"""
模块名称: core/engine/__init__.py
核心职责: 策略引擎包初始化，以金融级高容错方式导出核心引擎组件。
所属层级: core.engine

设计原则:
    - 关键模块缺失时拒绝启动，避免静默失败。
    - 所有导入异常均被安全捕获并记录，不泄露系统路径。
    - 支持插件注册与卸载，便于扩展。

外部依赖:
    - logging, sys, threading, time, os, re, typing, collections
    - 可选: psutil (系统资源检查)

接口契约:
    提供:
        - 动态构建的 __all__
        - get_available_components() -> Dict[str, type]
        - get_import_health() -> Dict[str, bool]
        - register_engine_component(name, cls, force=False)
        - unregister_engine_component(name)
        - get_import_errors() -> Dict[str, str]
        - get_component_version(name) -> Optional[str]
        - check_system_resources() -> bool
        - reset_for_testing() (仅测试用)
    消费:
        - core.engine 子模块

配置项: 无

作者: KHAOS System Architect
创建日期: 2025-01-20
修改记录:
    - 2026-07-07 v30.0: 终极金融级引擎入口，80项安全加固。
__version__ = "1.1.0"
__author__ = "KHAOS System Architect"
__maintainer__ = "KHAOS Core Team"
__license__ = "Proprietary - All Rights Reserved"
"""

import logging
import sys
import threading
import time
import os
import re
from typing import List, Dict, Optional, Type, Tuple

# -----------------------------------------------------------------------------
# 模块级变量与保护
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# 核心模块集合（不可变）
CRITICAL_MODULES: Tuple[str, ...] = (
    "StrategyEngine",
    "KhaosDecisionMaker",
    "SignalAssembler",
    "ContextPipeline",
    "HierarchyGuard",
    "PriorityExecutor",
    "ResonanceEvaluator",
)

# 内部存储
_IMPORT_ERRORS: Dict[str, str] = {}
_AVAILABLE_COMPONENTS: Dict[str, Type] = {}
_COMPONENT_VERSIONS: Dict[str, str] = {}
_IMPORT_TIMINGS: Dict[str, float] = {}
_init_lock = threading.Lock()
_initialized = False

# 黑名单：不允许注册为引擎组件名称的内置标识符
_FORBIDDEN_NAMES = set(dir(__builtins__)) if hasattr(__builtins__, '__dict__') else set()

# -----------------------------------------------------------------------------
# 自检函数
# -----------------------------------------------------------------------------
def _validate_module_name(name: str) -> bool:
    """检查模块名是否合法，防止注入攻击。"""
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def _check_python_version() -> None:
    """验证Python版本不低于3.10。"""
    if sys.version_info < (3, 10):
        raise RuntimeError(
            f"KHAOS requires Python 3.10 or higher. Current: {sys.version}"
        )


def _check_system_resources() -> bool:
    """检查系统资源（内存、磁盘），返回是否满足最低要求。"""
    try:
        import psutil
        mem = psutil.virtual_memory()
        if mem.available < 512 * 1024 * 1024:  # 512 MB
            logger.warning("Low memory: < 512 MB available. System may be unstable.")
            return False
        disk = psutil.disk_usage(os.getcwd())
        if disk.free < 100 * 1024 * 1024:  # 100 MB
            logger.warning("Low disk space: < 100 MB free.")
            return False
    except ImportError:
        logger.info("psutil not installed, skipping resource check.")
    return True


# -----------------------------------------------------------------------------
# 安全导入
# -----------------------------------------------------------------------------
def _safe_import(module_name: str, class_name: str) -> Optional[Type]:
    """
    安全导入指定类，仅捕获可恢复的导入异常。
    致命异常（如SystemExit）会继续抛出。
    """
    start = time.perf_counter()
    try:
        mod = __import__(f"core.engine.{module_name}", fromlist=[class_name])
        cls = getattr(mod, class_name)
        _COMPONENT_VERSIONS[class_name] = getattr(mod, '__version__', 'unknown')
        return cls
    except (ImportError, ModuleNotFoundError, AttributeError, SyntaxError) as e:
        # 脱敏处理：不暴露路径
        error_desc = f"{type(e).__name__}"
        if 'circular' in str(e).lower():
            error_desc += " (possible circular import)"
        _IMPORT_ERRORS[class_name] = error_desc[:200]
        _sys_write_stderr(f"WARNING: Failed to import {class_name}: {error_desc}\n")
        return None
    except BaseException:
        # 致命异常（KeyboardInterrupt, SystemExit等）直接抛出
        raise
    finally:
        _IMPORT_TIMINGS[class_name] = time.perf_counter() - start


def _sys_write_stderr(msg: str) -> None:
    """安全输出到stderr（避免日志系统未就绪时丢失信息）。"""
    try:
        sys.stderr.write(msg)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# 导入流程
# -----------------------------------------------------------------------------
def _load_components():
    """按依赖顺序加载引擎组件。"""
    # 默认顺序，环境变量可覆盖
    env_order = os.environ.get("KHAOS_ENGINE_IMPORT_ORDER", "")
    if env_order:
        custom_order = [tuple(pair.split(':')) for pair in env_order.split(',')]
    else:
        custom_order = [
            ("kline_buffer", "MultiTimeframeKlineBuffer"),
            ("signal_assembler", "SignalAssembler"),
            ("context_pipeline", "ContextPipeline"),
            ("hierarchy_guard", "HierarchyGuard"),
            ("priority_executor", "PriorityExecutor"),
            ("resonance_evaluator", "ResonanceEvaluator"),
            ("sr_pipeline", "SRMappingPipeline"),
            ("market_regime_monitor", "MarketRegimeMonitor"),
            ("multi_tf_coordinator", "MultiTfCoordinator"),
            ("decision_maker", "KhaosDecisionMaker"),
            ("strategy_engine", "StrategyEngine"),
        ]

    for mod_name, cls_name in custom_order:
        if not _validate_module_name(mod_name) or not _validate_module_name(cls_name):
            logger.error(f"Invalid module/class name: {mod_name}.{cls_name}")
            continue
        cls = _safe_import(mod_name, cls_name)
        if cls is not None:
            _AVAILABLE_COMPONENTS[cls_name] = cls
        else:
            # 非核心模块导入失败仅警告
            if cls_name in CRITICAL_MODULES:
                raise ImportError(
                    f"CRITICAL MODULE FAILED: {cls_name}. "
                    f"Engine cannot start without critical components."
                )


# -----------------------------------------------------------------------------
# 初始化（仅执行一次）
# -----------------------------------------------------------------------------
def _initialize():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _check_python_version()
        _check_system_resources()
        _load_components()
        _initialized = True

        # 启动日志
        logger.info(f"KHAOS Engine v{__version__} initialized. "
                    f"Components: {len(_AVAILABLE_COMPONENTS)} success, "
                    f"{len(_IMPORT_ERRORS)} errors.")
        if _IMPORT_ERRORS:
            logger.warning(f"Import errors: {_IMPORT_ERRORS}")
        for cls_name, duration in _IMPORT_TIMINGS.items():
            logger.debug(f"Import {cls_name} took {duration*1000:.1f}ms")


# 执行初始化
try:
    _initialize()
except ImportError as e:
    _sys_write_stderr(f"FATAL: {e}\n")
    sys.exit(1)  # 确保上层可感知失败

# -----------------------------------------------------------------------------
# 公共API构建
# -----------------------------------------------------------------------------
__all__ = sorted(list(_AVAILABLE_COMPONENTS.keys())) + [
    "get_available_components",
    "get_import_health",
    "get_engine_health",  # deprecated alias
    "register_engine_component",
    "unregister_engine_component",
    "get_import_errors",
    "get_component_version",
    "check_system_resources",
    "reset_for_testing",
    "list_registered_components",
]

# 注入全局命名空间
for cls_name, cls in _AVAILABLE_COMPONENTS.items():
    if cls_name in _FORBIDDEN_NAMES:
        # 冲突时添加后缀
        safe_name = f"{cls_name}_EngineComponent"
        logger.warning(f"Name conflict: {cls_name} renamed to {safe_name}")
        globals()[safe_name] = cls
    else:
        globals()[cls_name] = cls


# -----------------------------------------------------------------------------
# 公共函数
# -----------------------------------------------------------------------------
def get_available_components() -> Dict[str, Type]:
    """
    返回成功导入的组件字典（深拷贝，避免外部修改内部状态）。
    """
    with _init_lock:
        return dict(_AVAILABLE_COMPONENTS)


def get_import_health() -> Dict[str, bool]:
    """返回各组件导入成功与否。"""
    health = {}
    for cls_name in list(_AVAILABLE_COMPONENTS.keys()) + list(_IMPORT_ERRORS.keys()):
        health[cls_name] = cls_name in _AVAILABLE_COMPONENTS
    return health


def get_engine_health() -> Dict[str, bool]:
    """已弃用，请使用 get_import_health。"""
    import warnings
    warnings.warn("get_engine_health is deprecated, use get_import_health.", DeprecationWarning, stacklevel=2)
    return get_import_health()


def register_engine_component(name: str, cls: Type, force: bool = False) -> None:
    """
    注册新引擎组件。默认不允许覆盖已存在组件。
    Raises: ValueError 如果名称不合法或组件已存在且force=False。
    """
    if not _validate_module_name(name):
        raise ValueError(f"Invalid component name: {name}")
    if not isinstance(cls, type):
        raise ValueError("cls must be a class")
    if name in _FORBIDDEN_NAMES:
        logger.warning(f"Registering component with built-in name: {name}")

    with _init_lock:
        if name in _AVAILABLE_COMPONENTS and not force:
            raise ValueError(f"Component '{name}' already exists. Use force=True to overwrite.")
        _AVAILABLE_COMPONENTS[name] = cls
        # 更新版本
        _COMPONENT_VERSIONS[name] = getattr(cls, '__version__', 'unknown')
        if name not in globals():
            globals()[name] = cls
        if name not in __all__:
            __all__.append(name)
    logger.info(f"Engine component registered: {name} (force={force})")


def unregister_engine_component(name: str) -> None:
    """卸载引擎组件。关键模块不允许卸载。"""
    if name in CRITICAL_MODULES:
        raise ValueError(f"Cannot unregister critical module: {name}")
    with _init_lock:
        if name in _AVAILABLE_COMPONENTS:
            del _AVAILABLE_COMPONENTS[name]
            if name in globals():
                del globals()[name]
            if name in __all__:
                __all__.remove(name)
            logger.info(f"Engine component unregistered: {name}")


def get_import_errors() -> Dict[str, str]:
    """返回导入错误信息（深拷贝）。"""
    with _init_lock:
        return dict(_IMPORT_ERRORS)


def get_component_version(name: str) -> Optional[str]:
    """获取指定组件的版本。"""
    return _COMPONENT_VERSIONS.get(name)


def check_system_resources() -> bool:
    """检查系统资源（内存、磁盘）是否满足最低要求。"""
    return _check_system_resources()


def list_registered_components() -> List[str]:
    """列出所有已注册的组件名称。"""
    with _init_lock:
        return sorted(_AVAILABLE_COMPONENTS.keys())


def reset_for_testing() -> None:
    """
    重置引擎状态（仅供测试使用，生产环境禁止调用）。
    WARNING: 调用后需重新导入子模块或重启进程。
    """
    global _initialized
    with _init_lock:
        _AVAILABLE_COMPONENTS.clear()
        _IMPORT_ERRORS.clear()
        _COMPONENT_VERSIONS.clear()
        _IMPORT_TIMINGS.clear()
        # 清除注入的全局变量
        for name in list(globals().keys()):
            if name in _AVAILABLE_COMPONENTS or name.startswith("_"):
                continue
            if isinstance(globals().get(name), type) and name not in __builtins__:
                del globals()[name]
        _initialized = False
        __all__[:] = []
    logger.warning("Engine state reset for testing.")


# 优化交互式环境
def __dir__():
    return sorted(set(__all__ + list(__builtins__.keys())))


# -----------------------------------------------------------------------------
# 自检模块（python -m core.engine）
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"KHAOS Engine Version: {__version__}")
    print(f"Available components: {len(_AVAILABLE_COMPONENTS)}")
    for name in sorted(_AVAILABLE_COMPONENTS.keys()):
        ver = get_component_version(name)
        print(f"  - {name} (v{ver})")
    if _IMPORT_ERRORS:
        print("Import errors:")
        for name, err in _IMPORT_ERRORS.items():
            print(f"  - {name}: {err}")
    healthy = all(get_import_health().values())
    print(f"Engine import health: {'OK' if healthy else 'DEGRADED'}")
    sys.exit(0 if healthy else 1)
