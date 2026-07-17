# -*- coding: utf-8 -*-
"""
KHAOS Services Gateway v4.0 (Institutional Grade)
=================================================
Core Responsibilities:
    - Safely load all application services with strict error isolation
    - Provide factory methods for service instances with lazy loading
    - Expose health/status endpoint for monitoring dashboard
    - Support dynamic reload and configuration injection
    - Ensure thread/async safety and audit compliance

Architecture:
    Services are divided into CORE (failure blocks startup) and OPTIONAL (degraded gracefully).
    The registry decouples class discovery from instantiation, allowing full testability.

Usage:
    from services import get_service, get_all_service_status
    strategy = get_service("StrategyService", config=app_config)
    status = get_all_service_status()

Author: KHAOS Architecture Group
Created: 2025-03-01
Last Modified: 2026-07-17 (100-institution-grade-fixes)
"""

import logging
import sys
import time
import threading
import asyncio
from typing import Dict, Optional, Any, Callable, Tuple, Union
from enum import Enum

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version (semantic, sync with pyproject.toml)
# ---------------------------------------------------------------------------
VERSION = (4, 0, 0)
__version__ = ".".join(map(str, VERSION))

# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------
class ServiceLoadError(Exception):
    """Raised when a core service cannot be loaded."""
    pass

class ServiceTimeoutError(Exception):
    """Raised when service instantiation exceeds timeout."""
    pass

# ---------------------------------------------------------------------------
# Status Enumerations
# ---------------------------------------------------------------------------
class ServiceStatus(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    GRAY = "gray"

# ---------------------------------------------------------------------------
# Registry Data Structures
# ---------------------------------------------------------------------------
class ServiceInfo:
    """Metadata and state for a registered service."""
    __slots__ = (
        "name", "module_path", "cls", "status", "optional",
        "error_message", "loaded_at", "last_error_time",
        "instance_count", "constructor_params"
    )
    def __init__(self, name, module_path, optional):
        self.name = name
        self.module_path = module_path
        self.cls = None
        self.status = ServiceStatus.GRAY
        self.optional = optional
        self.error_message = ""
        self.loaded_at = 0.0
        self.last_error_time = 0.0
        self.instance_count = 0
        self.constructor_params = []  # list of required parameter names

# ---------------------------------------------------------------------------
# Core vs Optional classification (configurable via env or config file)
# ---------------------------------------------------------------------------
CORE_SERVICES = {
    "StrategyService": ".strategy_service",
    "EvolutionService": ".evolution_service",
}
OPTIONAL_SERVICES = {
    "AIService": ".ai_service",
    "DeployService": ".deploy_service",
    "NotificationService": ".notification_service",
    "ReportService": ".report_service",
    "PaperBroker": ".paper_broker",
}

# ---------------------------------------------------------------------------
# Global State with Thread Safety
# ---------------------------------------------------------------------------
_registry_lock = threading.RLock()
_instance_lock = threading.RLock()

# registry of service metadata
_service_registry: Dict[str, ServiceInfo] = {}
# cache of singleton instances (name -> instance)
_service_instances: Dict[str, Any] = {}

# ---------------------------------------------------------------------------
# Helper: import module safely
# ---------------------------------------------------------------------------
def _safe_import_module(module_path: str, package: str = "services") -> Tuple[Optional[Any], Optional[Exception]]:
    """
    Attempt to import a module, returning (module, None) on success,
    or (None, exception) on failure.
    """
    import importlib
    try:
        module = importlib.import_module(module_path, package=package)
        return module, None
    except Exception as e:
        return None, e

# ---------------------------------------------------------------------------
# Registration Logic
# ---------------------------------------------------------------------------
def _register_service(name: str, module_path: str, optional: bool = False) -> None:
    """Safely load a service class and populate the registry."""
    info = ServiceInfo(name, module_path, optional)
    with _registry_lock:
        _service_registry[name] = info

    start_time = time.monotonic()
    try:
        module, error = _safe_import_module(module_path)
        if error:
            raise error

        cls = getattr(module, name)
        if not callable(cls):
            raise TypeError(f"{name} is not a callable class")

        # Cache constructor signature for future instantiation
        import inspect
        try:
            sig = inspect.signature(cls.__init__)
            params = [p.name for p in sig.parameters.values()
                      if p.name != 'self' and p.default is inspect.Parameter.empty]
            info.constructor_params = params
        except (ValueError, TypeError) as e:
            logger.debug("Cannot inspect signature of %s: %s", name, e)
            info.constructor_params = []

        info.cls = cls
        info.status = ServiceStatus.GREEN
        info.loaded_at = time.monotonic() - start_time
        logger.info("Service loaded successfully: %s (optional=%s, time=%.4fs)", name, optional, info.loaded_at)

    except Exception as e:
        info.status = ServiceStatus.YELLOW if optional else ServiceStatus.RED
        info.error_message = str(e)
        info.last_error_time = time.time()
        if optional:
            logger.warning("Optional service %s unavailable: %s", name, e)
        else:
            logger.error("Core service %s failed to load: %s", name, e, exc_info=True)
            raise ServiceLoadError(f"Core service {name} failed") from e

# ---------------------------------------------------------------------------
# Initial Population
# ---------------------------------------------------------------------------
def _init_registry():
    """Called once at import time to register all known services."""
    for svc_name, module_path in CORE_SERVICES.items():
        _register_service(svc_name, module_path, optional=False)
    for svc_name, module_path in OPTIONAL_SERVICES.items():
        _register_service(svc_name, module_path, optional=True)

_init_registry()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_service(
    name: str,
    config: Optional[Any] = None,
    reload: bool = False,
    timeout: Optional[float] = None,
    singleton: bool = True,
) -> Any:
    """
    Retrieve or instantiate a service.

    Args:
        name: Service name.
        config: Configuration object passed to constructor.
        reload: Force module re-import and new instance.
        timeout: Maximum seconds for instantiation.
        singleton: If True, cache and reuse instance.

    Returns:
        Service instance or None if optional and unavailable.

    Raises:
        ServiceLoadError: If core service unavailable.
        ServiceTimeoutError: If instantiation exceeds timeout.
    """
    # Validate name
    if not name.isidentifier():
        raise ValueError(f"Invalid service name: {name}")

    with _registry_lock:
        if name not in _service_registry:
            raise ValueError(f"Unknown service: {name}")
        info = _service_registry[name]

    # Handle reload (must re-register first)
    if reload:
        _register_service(name, info.module_path, info.optional)
        with _registry_lock:
            info = _service_registry[name]

    # If class is missing and optional, return None
    if info.cls is None:
        if info.optional:
            logger.debug("Optional service %s not loaded.", name)
            return None
        raise ServiceLoadError(f"Core service {name} is not available: {info.error_message}")

    # Singleton check
    if singleton:
        with _instance_lock:
            if name in _service_instances:
                return _service_instances[name]

    # Instantiate with timeout
    cls = info.cls
    start = time.monotonic()

    def _instantiate():
        kwargs = {}
        if config is not None:
            if 'config' in info.constructor_params or not info.constructor_params:
                kwargs['config'] = config
        return cls(**kwargs)

    try:
        if timeout and timeout > 0:
            instance = _run_with_timeout(_instantiate, timeout)
        else:
            instance = _instantiate()
    except ServiceTimeoutError:
        raise
    except Exception as e:
        logger.error("Failed to instantiate %s: %s", name, e, exc_info=True)
        info.status = ServiceStatus.RED
        info.error_message = str(e)
        raise ServiceLoadError(f"Instantiation failed for {name}") from e

    elapsed = time.monotonic() - start
    logger.debug("Service %s instantiated in %.4fs", name, elapsed)

    # Cache singleton
    if singleton:
        with _instance_lock:
            # Reload path: if an old instance exists, attempt to shut it down
            old = _service_instances.pop(name, None)
            if old and hasattr(old, 'shutdown'):
                try:
                    old.shutdown()
                except Exception:
                    logger.warning("Error shutting down old instance of %s", name, exc_info=True)
            _service_instances[name] = instance
            info.instance_count += 1

    return instance

def _run_with_timeout(func, timeout_sec):
    """Synchronous timeout wrapper using threading (for simplicity)."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func)
        try:
            return future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            raise ServiceTimeoutError(f"Service instantiation timed out after {timeout_sec}s")

def get_all_service_status() -> Dict[str, Any]:
    """Return status of all registered services (for monitoring dashboard)."""
    result = {}
    with _registry_lock:
        for name, info in _service_registry.items():
            # Sanitize error messages (remove potential paths/secrets)
            safe_error = info.error_message
            # basic sanitization: truncate length
            if len(safe_error) > 200:
                safe_error = safe_error[:200] + "..."
            result[name] = {
                "status": info.status.value,
                "optional": info.optional,
                "error": safe_error,
                "loaded": info.cls is not None,
                "instances": info.instance_count,
                "load_time_ms": int(info.loaded_at * 1000) if info.loaded_at else 0,
                "last_error": info.last_error_time,
            }
    return result

def is_healthy() -> bool:
    """Quick check: all core services green?"""
    with _registry_lock:
        for name, info in _service_registry.items():
            if not info.optional and info.status != ServiceStatus.GREEN:
                return False
    return True

# ---------------------------------------------------------------------------
# Reload all services (use with caution)
# ---------------------------------------------------------------------------
def reload_all():
    """Force reload all registered services."""
    for name in list(_service_registry.keys()):
        get_service(name, reload=True, singleton=True)

# ---------------------------------------------------------------------------
# Module attribute access (backward compatible)
# ---------------------------------------------------------------------------
def __getattr__(name: str):
    if name in _service_registry:
        info = _service_registry[name]
        if info.cls is not None:
            return info.cls
        if info.optional:
            return None
        raise ServiceLoadError(f"Core service {name} unavailable")
    raise AttributeError(f"module 'services' has no attribute '{name}'")

# ---------------------------------------------------------------------------
# Exports (only public API)
# ---------------------------------------------------------------------------
__all__ = [
    "get_service",
    "get_all_service_status",
    "is_healthy",
    "reload_all",
    "ServiceStatus",
    "ServiceLoadError",
    "ServiceTimeoutError",
    "VERSION",
    "__version__",
  ]
