# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: core/__init__.py
核心职责: KHAOS 核心层入口，统一暴露公共接口，提供真正的延迟加载和版本管理。
所属层级: core

设计原则:
    - 延迟加载：本模块不主动导入任何子模块（包括 interfaces），所有接口在首次访问时才加载。
    - 类型检查：通过 TYPE_CHECKING 提供静态类型支持，不影响运行时性能。
    - 接口同步：PUBLIC_INTERFACES 硬编码于此，CI 门禁负责验证与 core.interfaces.__all__ 的一致性。
    - 线程安全：使用锁保护缓存，支持并发访问。
    - 最小依赖：仅依赖标准库。

外部依赖:
    - importlib, logging, threading, typing

接口契约:
    提供:
        - __version__: 核心层版本字符串
        - PUBLIC_INTERFACES: 所有公共接口名称元组
        - 通过模块属性访问核心类、枚举、异常
        - clear_cache, verify_public_interface 等辅助函数
    消费: 无

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2026-07-07 v31.0: 最终机构级，移除顶层导入，实现真正的延迟加载，硬编码接口列表。
"""

import importlib
import logging
import sys
import threading
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple, Union

# -----------------------------------------------------------------------------
# 版本管理：多级回退策略
# -----------------------------------------------------------------------------
def _resolve_version() -> str:
    """尝试从包元数据获取版本，否则从 pyproject.toml 读取，最后回退到 'unknown'."""
    try:
        from importlib.metadata import version as _get_pkg_version, PackageNotFoundError
        return _get_pkg_version("khaos")
    except (ImportError, PackageNotFoundError):
        pass

    # 尝试读取 pyproject.toml (粗略解析)
    try:
        import tomllib  # Python 3.11+
        with open("pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version", "unknown")
    except Exception:
        pass

    return "unknown"

__version__: str = _resolve_version()

# -----------------------------------------------------------------------------
# 日志
# -----------------------------------------------------------------------------
_logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# 公共接口列表（硬编码，与 core.interfaces.__all__ 保持同步）
# 修改此列表时请同时更新 core/interfaces.py 的 __all__
# -----------------------------------------------------------------------------
PUBLIC_INTERFACES: Tuple[str, ...] = (
    # 抽象基类
    "Auditable",
    "Cloneable",
    "ConfigProvider",
    "DecisionMaker",
    "Diagnosable",
    "EvolutionTask",
    "ExecutionAdapter",
    "FeatureComputer",
    "KHAOSSystem",
    "MarketDataProvider",
    "MetricsProvider",
    "NotificationService",
    "Resettable",
    "RiskRule",
    "ServiceLifecycle",
    "SignalConflictResolver",
    "StatefulComponent",
    "SupportResistanceComputer",
    "WaveSimilarityEngine",
    # 枚举
    "ComponentLifecycle",
    "ConnectionState",
    "HealthStatus",
    "MarketRegime",
    "MetricType",
    "NotificationPriority",
    "OrderAction",
    "OrderType",
    "SignalPriority",
    "SuggestedAction",
    # 数据类
    "AuditEntry",
    "DataHealth",
    "DiagnosticFinding",
    "DiagnosticReport",
    "HealthCheckResult",
    "OrderConfirmation",
    "SRLevel",
    # 异常
    "CheckpointVersionMismatchException",
    "ConfigurationException",
    "DataSourceException",
    "DecisionTimeoutException",
    "KHAOSException",
    "OrderExecutionException",
    "RiskViolationException",
    "StateRecoveryException",
)

# -----------------------------------------------------------------------------
# 延迟导入映射（所有接口均来自 .interfaces 子模块）
# -----------------------------------------------------------------------------
_LAZY_IMPORT_MAP: Dict[str, str] = {name: ".interfaces" for name in PUBLIC_INTERFACES}

# -----------------------------------------------------------------------------
# 缓存与线程安全
# -----------------------------------------------------------------------------
_loaded_cache: Dict[str, object] = {}
_cache_lock = threading.Lock()

# -----------------------------------------------------------------------------
# 类型检查时静态导入（仅工具可见，不增加运行时负担）
# -----------------------------------------------------------------------------
if TYPE_CHECKING:
    from core.interfaces import (
        MarketDataProvider,
        FeatureComputer,
        SupportResistanceComputer,
        DecisionMaker,
        SignalConflictResolver,
        ExecutionAdapter,
        RiskRule,
        EvolutionTask,
        WaveSimilarityEngine,
        StatefulComponent,
        NotificationService,
        ConfigProvider,
        ServiceLifecycle,
        Auditable,
        Diagnosable,
        MetricsProvider,
        Cloneable,
        Resettable,
        KHAOSSystem,
        MarketRegime,
        OrderAction,
        SignalPriority,
        OrderType,
        ConnectionState,
        NotificationPriority,
        HealthStatus,
        ComponentLifecycle,
        SuggestedAction,
        MetricType,
        SRLevel,
        OrderConfirmation,
        DataHealth,
        DiagnosticFinding,
        DiagnosticReport,
        AuditEntry,
        HealthCheckResult,
        KHAOSException,
        DataSourceException,
        StateRecoveryException,
        CheckpointVersionMismatchException,
        DecisionTimeoutException,
        OrderExecutionException,
        RiskViolationException,
        ConfigurationException,
    )

# -----------------------------------------------------------------------------
# 自定义异常
# -----------------------------------------------------------------------------
class CoreImportError(ImportError):
    """核心接口导入异常。"""
    def __init__(self, name: str, detail: str = ""):
        self.name = name
        self.detail = detail
        super().__init__(f"Failed to import core interface '{name}': {detail}")

# -----------------------------------------------------------------------------
# 延迟加载核心逻辑
# -----------------------------------------------------------------------------
def __getattr__(name: str) -> object:
    """
    延迟加载公共接口。首次访问时从 .interfaces 导入并缓存。

    参数:
        name: 接口名称。

    返回:
        导入的类、枚举、异常或数据类。

    抛出:
        CoreImportError: 名称不存在或导入失败。
    """
    # 允许特殊属性正常传递，避免破坏 importlib 内部机制
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)

    if name not in PUBLIC_INTERFACES:
        raise CoreImportError(
            name,
            f"Unknown interface. Available: {PUBLIC_INTERFACES}"
        )

    with _cache_lock:
        if name in _loaded_cache:
            return _loaded_cache[name]

        module_path = _LAZY_IMPORT_MAP.get(name, ".interfaces")
        try:
            module = importlib.import_module(module_path, __package__)
            attr = getattr(module, name)
        except (ModuleNotFoundError, ImportError) as e:
            _logger.error("Failed to lazy-load %s from %s: %s", name, module_path, e)
            raise CoreImportError(name, str(e)) from e
        except AttributeError:
            _logger.error("Attribute %s not found in %s", name, module_path)
            raise CoreImportError(name, f"Attribute not found in {module_path}")

        _loaded_cache[name] = attr
        _logger.debug("Lazy-loaded core attribute: %s", name)
        return attr

# -----------------------------------------------------------------------------
# dir() 支持
# -----------------------------------------------------------------------------
def __dir__() -> List[str]:
    """返回所有可访问的公共属性名称（用于自动补全）。"""
    return sorted(PUBLIC_INTERFACES)

# -----------------------------------------------------------------------------
# 缓存管理
# -----------------------------------------------------------------------------
def clear_cache() -> None:
    """清除延迟加载缓存，释放内存。通常在热重载时调用。"""
    with _cache_lock:
        _loaded_cache.clear()
        _logger.info("Core lazy-load cache cleared.")

def loaded_attributes() -> Tuple[str, ...]:
    """返回当前已加载的属性名称元组。"""
    with _cache_lock:
        return tuple(_loaded_cache.keys())

# -----------------------------------------------------------------------------
# 公共接口验证（用于诊断和CI）
# -----------------------------------------------------------------------------
def verify_public_interface(
    timeout: Optional[float] = 10.0,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    验证所有公共接口都能成功加载。

    参数:
        timeout: 每个接口导入的最大等待时间（秒）。
        progress_callback: 每加载一个接口时调用的回调。

    返回:
        True 如果全部加载成功。

    抛出:
        RuntimeError: 如果存在导入失败。
    """
    errors = []
    total = len(PUBLIC_INTERFACES)
    for idx, name in enumerate(PUBLIC_INTERFACES):
        try:
            __getattr__(name)
            if progress_callback:
                progress_callback(name)
        except Exception as e:
            errors.append(f"{name}: {e}")
    if errors:
        raise RuntimeError(
            f"Interface verification failed ({len(errors)}/{total}):\n" +
            "\n".join(errors)
        )
    return True

# -----------------------------------------------------------------------------
# 导出的公共符号
# -----------------------------------------------------------------------------
__all__: List[str] = list(PUBLIC_INTERFACES)
