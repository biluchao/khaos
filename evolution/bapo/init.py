# -*- coding: utf-8 -*-
"""
模块名称: evolution.bapo
核心职责: 贝叶斯参数优化子系统入口。提供惰性加载、完整性校验、动态注册和健康检查。
所属层级: evolution.bapo

外部依赖:
    - numpy (数值计算)
    - scipy (高斯过程)
    - GPyOpt >= 1.2.6 (或 BoTorch)
    - core.engine (策略引擎接口，间接依赖)
    - importlib (惰性导入)
    - threading (并发安全)

接口契约:
    提供: {
        'BayesianOptimizer': '贝叶斯优化主类，封装高斯过程、采集函数与迭代逻辑',
        'ObjectiveFunction': '定义并计算优化目标（夏普、卡尔玛等）',
        'ReplayEngine': '高速影子信号回放引擎，支持并行评估参数组合',
        'verify_modules() -> dict': '检查所有子模块是否可成功加载',
        'check_dependencies() -> Tuple[bool, List[str]]': '验证必需和可选依赖',
        'health() -> dict': '返回模块健康状态',
        'register_class(name: str, module_path: str, cls: type) -> None': '动态注册额外的优化组件',
        'unregister_class(name: str) -> None': '移除注册的组件并清理缓存'
    }
    消费: 由 evolution_service 调度，定期或在手动触发时执行。

版本: 2.0.0
状态: 生产就绪
作者: KHAOS Evolution Team
邮箱: evolution@khaos.internal
版权: © 2025-2026 KHAOS Engineering. All rights reserved.
许可: UNLICENSED
创建日期: 2025-09-01
修改记录:
    - 2026-01-16 第四轮机构级审计：增加注册、健康检查、CLI、线程安全、动态版本等
    - 2026-01-15 第三轮审计：惰性加载、完整性验证、日志系统
    - 2025-09-01 初始版本

使用示例::

    from evolution.bapo import BayesianOptimizer, verify_modules
    status = verify_modules()
    if all(v == "OK" for v in status.values()):
        opt = BayesianOptimizer(config)
        opt.run()
"""

import importlib
import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple, Callable

# ---------- 元数据 ----------
try:
    from importlib.metadata import version as _get_version
    __version__ = _get_version("khaos-bapo")
except Exception:
    __version__ = "2.0.0"

__author__ = "KHAOS Evolution Team"
__email__ = "evolution@khaos.internal"
__copyright__ = "Copyright © 2025-2026 KHAOS Engineering"
__license__ = "UNLICENSED"
__status__ = "Production"

# ---------- 日志配置 ----------
logger = logging.getLogger(__name__)
if not logger.handlers:  # 避免重复添加
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("BAPO_LOG_LEVEL", "INFO"))

# ---------- 常量与配置 ----------
_PUBLIC_CLASSES: Dict[str, str] = {
    "BayesianOptimizer": ".bayesian_optimizer",
    "ObjectiveFunction": ".objective_function",
    "ReplayEngine": ".replay_engine",
}
_STRICT_MODE: bool = os.environ.get("BAPO_STRICT_MODE", "true").lower() == "true"
_MAX_CACHE_SIZE: int = 10  # 最多缓存多少个已加载模块

# 线程安全锁
_lock = threading.Lock()
# 缓存最近加载的模块 (LRU 手动实现)
_loaded_modules: Dict[str, Any] = {}
_module_access_order: List[str] = []

# 为静态类型检查提供真实导入
if sys.version_info >= (3, 8):
    from typing import TYPE_CHECKING
else:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from .bayesian_optimizer import BayesianOptimizer  # noqa: F401
    from .objective_function import ObjectiveFunction  # noqa: F401
    from .replay_engine import ReplayEngine            # noqa: F401

# ---------- 惰性加载机制 ----------
__all__: List[str] = list(_PUBLIC_CLASSES.keys())


def __getattr__(name: str) -> Any:
    """
    动态导入子模块，实现惰性加载。
    首次访问 evolution.bapo.BayesianOptimizer 时，才真正加载对应的子模块。
    """
    if name not in _PUBLIC_CLASSES:
        raise AttributeError(f"module 'evolution.bapo' has no attribute '{name}'")

    module_path = _PUBLIC_CLASSES[name]
    full_module = f"{__package__ or 'evolution.bapo'}{module_path}"

    with _lock:
        # 检查缓存
        if full_module in _loaded_modules:
            mod = _loaded_modules[full_module]
        else:
            try:
                mod = importlib.import_module(module_path, package=__package__)
                _loaded_modules[full_module] = mod
                _module_access_order.append(full_module)
                # 淘汰最旧的缓存
                if len(_loaded_modules) > _MAX_CACHE_SIZE:
                    oldest = _module_access_order.pop(0)
                    _loaded_modules.pop(oldest, None)
                logger.debug(f"惰性加载模块: {full_module}")
            except (ImportError, ModuleNotFoundError) as e:
                logger.error(f"无法加载进化子模块 {full_module}: {e}", exc_info=True)
                raise ImportError(
                    f"贝叶斯优化模块 {name} 加载失败，请检查依赖是否安装: {e}"
                ) from e

        # 从模块中提取目标类
        cls = getattr(mod, name, None)
        if cls is None and _STRICT_MODE:
            available = [attr for attr in dir(mod) if not attr.startswith('_')]
            logger.error(f"模块 {full_module} 中未找到类 {name}，可用符号: {available}")
            raise AttributeError(
                f"模块 {full_module} 缺少预期的类 {name}，请检查版本兼容性"
            )

        if cls is not None:
            # 绑定到当前模块命名空间，后续访问无需再次导入
            sys.modules[__name__].__dict__[name] = cls
        return cls


def __dir__() -> List[str]:
    """返回所有可导出的公共符号，用于交互式环境和 tab 补全。"""
    base = list(__all__)
    base.extend(["verify_modules", "check_dependencies", "health",
                  "register_class", "unregister_class", "get_version"])
    return sorted(base)


# ---------- 缓存管理 ----------
def _invalidate_cache(module_name: Optional[str] = None) -> None:
    """清除缓存的模块，以便重新加载。若 module_name 为 None，则清空全部。"""
    with _lock:
        if module_name:
            _loaded_modules.pop(module_name, None)
            if module_name in _module_access_order:
                _module_access_order.remove(module_name)
            # 同时清除绑定到当前模块的属性
            for name, path in _PUBLIC_CLASSES.items():
                if path == module_name.replace(__package__ or "", ""):
                    sys.modules[__name__].__dict__.pop(name, None)
        else:
            _loaded_modules.clear()
            _module_access_order.clear()
            for name in _PUBLIC_CLASSES:
                sys.modules[__name__].__dict__.pop(name, None)


# ---------- 公共 API ----------
def verify_modules(raise_on_error: bool = False) -> Dict[str, str]:
    """
    检查所有子模块是否可成功加载，返回每个模块的状态。

    Args:
        raise_on_error: 若为 True，遇到第一个失败即抛出异常。

    Returns:
        dict: {模块名: 'OK' 或 'FAILED: 错误信息'}
    """
    status: Dict[str, str] = {}
    for class_name, module_path in _PUBLIC_CLASSES.items():
        try:
            full_module = f"{__package__}{module_path}"
            mod = importlib.import_module(module_path, package=__package__)
            if not hasattr(mod, class_name) and _STRICT_MODE:
                raise AttributeError(f"缺少类 {class_name}")
            status[class_name] = "OK"
        except Exception as e:
            status[class_name] = f"FAILED: {e}"
            if raise_on_error:
                raise
    return status


def check_dependencies() -> Tuple[bool, List[str], List[str]]:
    """
    验证核心与可选依赖是否可用。

    Returns:
        (全部满足, 缺失的必需依赖列表, 缺失的可选依赖列表)
    """
    required = ["numpy", "scipy"]
    optional = ["GPyOpt", "botorch"]
    missing_req = []
    missing_opt = []

    for lib in required:
        try:
            importlib.import_module(lib)
        except ImportError:
            missing_req.append(lib)

    for lib in optional:
        try:
            importlib.import_module(lib)
        except ImportError:
            missing_opt.append(lib)

    success = len(missing_req) == 0
    if not success:
        logger.error(f"缺少核心依赖: {missing_req}")
    if missing_opt:
        logger.warning(f"缺少可选依赖 (部分功能受限): {missing_opt}")

    return success, missing_req, missing_opt


def health() -> Dict[str, Any]:
    """返回模块健康状态摘要，供监控系统使用。"""
    deps_ok, missing_req, missing_opt = check_dependencies()
    module_status = verify_modules()
    return {
        "version": __version__,
        "status": "healthy" if (deps_ok and all(v == "OK" for v in module_status.values())) else "degraded",
        "dependencies": {
            "required": deps_ok,
            "missing_required": missing_req,
            "missing_optional": missing_opt,
        },
        "modules": module_status,
    }


def get_version() -> str:
    """返回当前包的版本号。"""
    return __version__


def register_class(name: str, module_path: str, cls: type) -> None:
    """
    动态注册一个公共类，可用于测试或扩展。
    注意：注册后需确保对应的模块可导入，该类会被添加到惰性加载映射中。

    Args:
        name: 暴露给外部的名称
        module_path: 点号开头的相对模块路径
        cls: 实际的类（必须与模块中的类一致）
    """
    if not name or not module_path:
        raise ValueError("名称和模块路径不能为空")
    with _lock:
        _PUBLIC_CLASSES[name] = module_path
        # 同时更新 __all__
        if name not in __all__:
            __all__.append(name)
        logger.info(f"注册公共类: {name} -> {module_path}")


def unregister_class(name: str) -> None:
    """移除动态注册的公共类并清理相关缓存。"""
    with _lock:
        if name in _PUBLIC_CLASSES:
            module_path = _PUBLIC_CLASSES.pop(name)
            full_module = f"{__package__}{module_path}"
            _invalidate_cache(full_module)
            if name in __all__:
                __all__.remove(name)
            logger.info(f"取消注册公共类: {name}")


# ---------- CLI 支持 ----------
if __name__ == "__main__":
    # 允许通过 python -m evolution.bapo 执行健康检查
    import json
    print(json.dumps(health(), indent=2, ensure_ascii=False))
