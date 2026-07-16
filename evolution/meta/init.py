# -*- coding: utf-8 -*-
"""
模块名称: evolution/meta/__init__.py
核心职责: 元学习子包入口，提供按需惰性加载的元学习器、少样本适配器及跨品种编码器。
         通过 __getattr__ 实现真正的延迟导入，同时提供依赖检查、接口验证与可用性查询。
所属层级: evolution.meta

外部依赖:
    - torch >= 1.12.0 (可选，实际使用时才需要)
    - packaging (可选，用于版本比较，缺省使用内置方法)

接口契约:
    提供: {
        'MetaLearner': '基于MAML的元学习器，快速适应新品种',
        'FewShotAdapter': '少样本适配器，支持内循环梯度更新',
        'CrossAssetEncoder': '跨品种特征编码器',
        'get_available_modules': '() -> List[str] 返回所有可用的模块名',
        'MetaLearnerError': '元学习模块专用异常'
    }
    消费: 无外部消费。

版本: 2.0.0
作者: KHAOS Evolution Team
审计者: KHAOS Audit AI
审计日期: 2026-07-12
"""
import importlib
import logging
import sys
import time
from typing import List, Optional, Tuple

__version__ = "2.0.0"
__author__ = "KHAOS Evolution Team"
__license__ = "SEE LICENSE IN LICENSE.md"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局依赖检查 (包导入时执行一次)
# ---------------------------------------------------------------------------
_TORCH_AVAILABLE = False
_TORCH_VERSION_OK = False

def _check_dependency_version(package: str, min_version: str) -> Tuple[bool, str]:
    """
    检查包是否安装且版本 >= min_version。
    返回 (是否满足, 消息)
    """
    try:
        mod = importlib.import_module(package)
        current = getattr(mod, '__version__', '0.0.0')
        # 尝试使用 packaging，若不可用则回退到简单的元组比较
        try:
            from packaging import version as pkg_version
            ok = pkg_version.parse(current) >= pkg_version.parse(min_version)
        except ImportError:
            # 回退方案：将版本号分割为数字元组
            def _parse(v):
                return tuple(map(int, v.split('.')))[:3]
            ok = _parse(current) >= _parse(min_version)
        msg = f"{package} {current} {'>=' if ok else '<'} {min_version}"
        return ok, msg
    except ImportError:
        return False, f"{package} 未安装"
    except Exception as e:
        return False, f"检查 {package} 版本时出错: {e}"

_torch_ok, _torch_msg = _check_dependency_version('torch', '1.12.0')
if _torch_ok:
    _TORCH_AVAILABLE = True
    _TORCH_VERSION_OK = True
    logger.info("torch 可用: %s", _torch_msg)
else:
    logger.warning("torch 不可用或版本不足: %s。元学习组件将不可用。建议: pip install torch>=1.12.0", _torch_msg)

# ---------------------------------------------------------------------------
# 惰性加载映射
# ---------------------------------------------------------------------------
_LAZY_CLASSES = {
    'MetaLearner': '.meta_learner',
    'FewShotAdapter': '.few_shot_adapter',
    'CrossAssetEncoder': '.cross_asset_encoder',
}

# 接口校验要求 (可从环境变量配置)
_STRICT_CHECK = (sys.env.get('KHAOS_META_STRICT_CHECK', 'false').lower() == 'true')
_REQUIRED_METHODS = {
    'MetaLearner': ['adapt', 'predict'],
    'FewShotAdapter': ['adapt', 'predict'],
    'CrossAssetEncoder': ['encode'],
}

# 内部缓存
_module_cache = {}
_available_cache: Optional[List[str]] = None

# ---------------------------------------------------------------------------
# 公开异常
# ---------------------------------------------------------------------------
class MetaLearnerError(ImportError):
    """元学习模块专用异常，在惰性加载失败时抛出"""
    pass

# ---------------------------------------------------------------------------
# 惰性加载核心 (通过 __getattr__)
# ---------------------------------------------------------------------------
def __getattr__(name: str):
    """按需导入元学习组件，实现延迟加载。"""
    if name in _module_cache:
        return _module_cache[name]

    if name not in _LAZY_CLASSES:
        raise AttributeError(f"module 'evolution.meta' has no attribute '{name}'")

    module_path = _LAZY_CLASSES[name]

    # 环境依赖检查
    if not _TORCH_AVAILABLE:
        raise MetaLearnerError(f"无法导入 {name}: torch 不可用。请安装 torch>=1.12.0")

    start = time.monotonic()
    try:
        module = importlib.import_module(module_path, package=__name__)
        cls = getattr(module, name)
    except ImportError as e:
        raise MetaLearnerError(f"导入 {name} 失败: {e}") from e
    except AttributeError as e:
        raise MetaLearnerError(f"{name} 不存在于 {module_path}: {e}") from e
    except Exception as e:
        raise MetaLearnerError(f"导入 {name} 时发生未知错误: {e}") from e

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("元学习组件 %s 导入成功，耗时 %.2f ms", name, elapsed_ms)

    # 版本记录
    mod_version = getattr(cls, '__version__', '未知')
    logger.debug("%s 版本: %s", name, mod_version)

    # 接口校验 (宽松模式下仅警告)
    required = _REQUIRED_METHODS.get(name, [])
    if required:
        missing = [m for m in required if not hasattr(cls, m)]
        if missing:
            msg = f"{name} 缺少方法: {missing}"
            if _STRICT_CHECK:
                raise MetaLearnerError(msg)
            else:
                logger.warning("%s，将在非严格模式下继续使用", msg)

    _module_cache[name] = cls
    # 动态注入模块全局 (便于直接访问)
    if name not in globals():
        globals()[name] = cls
    return cls

def __dir__() -> List[str]:
    """返回本模块的公开属性列表，包含可用组件。"""
    base = list(globals().keys())
    available = get_available_modules()
    return sorted(set(base) | set(available))

# ---------------------------------------------------------------------------
# 可用性查询
# ---------------------------------------------------------------------------
def get_available_modules() -> List[str]:
    """返回当前环境下可以惰性加载的元学习模块名称列表 (不触发实际导入)"""
    global _available_cache
    if _available_cache is None:
        if not _TORCH_AVAILABLE:
            _available_cache = []
        else:
            # 使用静态探测，不实际导入
            _available_cache = []
            for name, rel_path in _LAZY_CLASSES.items():
                # 尝试解析模块路径
                try:
                    importlib.util.find_spec(rel_path, package=__name__)
                    # 仅检查模块存在，不检查接口
                    _available_cache.append(name)
                except (ValueError, ImportError):
                    pass
    return _available_cache.copy()

# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def refresh_components() -> None:
    """清空缓存，下次访问将重新导入。用于依赖安装后热刷新。"""
    global _module_cache, _available_cache
    _module_cache.clear()
    _available_cache = None
    # 清理模块全局变量中的注入
    for name in _LAZY_CLASSES:
        if name in globals():
            del globals()[name]
    logger.info("元学习组件缓存已刷新")

# 导出列表（静态部分 + 动态组件名）
__all__ = ['MetaLearnerError', 'get_available_modules', 'refresh_components']
