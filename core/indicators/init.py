# -*- coding: utf-8 -*-
"""
模块名称: core/indicators/__init__.py
核心职责: 作为 KHAOS 量化策略指标的统一入口，提供安全、可配置、可观测的懒加载机制。
所属层级: core.indicators

外部依赖:
    - numpy, pandas (基础科学计算)
    - hmmlearn (隐马尔可夫模型，可选)
    - scipy (信号处理，可选)
    详细许可证信息见各模块文件。

接口契约:
    提供: 通过属性访问延迟加载的指标类，以及工具函数 list_modules, reload_module, debug_info。
    消费: 被 core/engine/decision_maker.py 等策略组件调用。

使用示例:
    from core.indicators import KalmanTrendline
    kma = KalmanTrendline(q_ratio=0.01)

配置项:
    - 环境变量 KHAOS_DISABLE_INDICATORS 可设置逗号分隔的模块名列表，禁用特定模块。
    - 配置文件 config/strategy.yaml 中的 indicators.enabled 可用于开关。

作者: KHAOS System Architect
创建日期: 2025-03-15
修改记录:
    - 2026-01-10 增加新增指标模块
    - 2026-07-12 实施全面懒加载、错误隔离、占位符机制，通过机构级审计
"""

import importlib
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Type

__version__ = "2.0.0"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------
class IndicatorNotAvailable(Exception):
    """指标模块加载失败且无法恢复时抛出"""
    pass

# ---------------------------------------------------------------------------
# 模块注册表 (默认导出列表)
# ---------------------------------------------------------------------------
_MODULE_MAP: Dict[str, str] = {
    # 趋势跟踪核心
    "KalmanTrendline": ".kma",
    "HMMStateDetector": ".hmm_state_detector",
    "HMMTrainer": ".hmm_trainer",
    "TrendProbabilityFilter": ".trend_probability_filter",

    # 逃逸与再捕捉
    "StageTopEscapeDetector": ".escape_detector",
    "SwingRecaptureModule": ".swing_recapture",

    # 回调与加仓
    "CallbackDropModule": ".callback_drop",
    "PullbackAddModule": ".pullback_add",

    # 微结构剥头皮
    "MicroPullbackScalper": ".micro_pullback_scalper",
    "MicroDivergenceTrader": ".micro_divergence_trader",

    # 震荡策略
    "RangeOscillationGrid": ".range_grid",
    "VolumeProfileMeanReversion": ".volume_profile_mr",
    "VolatilitySqueezePreBreakout": ".vol_squeeze_breakout",
    "MicroScalpOnOBI": ".micro_scalp_obi",

    # 支撑阻力与自适应
    "SwingVolumeSR": ".sr_5min",
    "StructureFibSR": ".sr_15min",
    "AdaptiveStageSR": ".adaptive_stage_sr",

    # 形态相似度
    "WaveSimilarityEngine": ".wave_similarity_engine",
    "WavePatternCache": ".wave_pattern_cache",
}

# 被视为核心的模块，若加载失败将阻止系统启动
CORE_MODULES = {"KalmanTrendline", "HMMStateDetector", "TrendProbabilityFilter"}

# 线程锁，保护懒加载过程
_lock = threading.RLock()

# 缓存：类名 -> 已加载的类对象 (成功) 或 占位符 (失败) 或 None (未加载)
_class_cache: Dict[str, Optional[Type[Any]]] = {}

# 环境变量黑名单：逗号分隔的模块名，这些模块将永不加载
_disable_list = set(
    name.strip() for name in os.environ.get("KHAOS_DISABLE_INDICATORS", "").split(",") if name.strip()
)

# 占位符类，当模块不可用时返回该类的实例，调用时抛出明确的异常
class _MissingIndicator:
    def __init__(self, name: str):
        self._name = name
    def __call__(self, *args, **kwargs):
        raise IndicatorNotAvailable(f"指标模块 {self._name} 不可用，请检查依赖或配置。")
    def __getattr__(self, item):
        raise IndicatorNotAvailable(f"指标模块 {self._name} 不可用。")

# ---------------------------------------------------------------------------
# 懒加载机制
# ---------------------------------------------------------------------------
def _load_module(name: str) -> Type[Any]:
    """
    动态加载单个指标模块。成功时返回类对象，失败时返回占位符。
    该函数由 __getattr__ 调用，确保线程安全。
    """
    if name in _disable_list:
        logger.warning(f"指标 {name} 已被环境变量禁用，返回占位符。")
        return _MissingIndicator(name)

    module_path = _MODULE_MAP.get(name)
    if not module_path:
        raise AttributeError(f"未知指标: {name}")

    try:
        start = time.monotonic()
        full_module = __name__ + module_path
        mod = importlib.import_module(module_path, package=__name__)
        cls = getattr(mod, name)
        elapsed = (time.monotonic() - start) * 1000
        logger.info(f"指标 {name} 加载成功 (耗时 {elapsed:.2f}ms)")
        return cls
    except (ImportError, AttributeError) as e:
        logger.error(f"指标 {name} 加载失败: {e}", exc_info=True)
        # 若为核心模块，可选择抛出致命错误
        if name in CORE_MODULES:
            raise SystemExit(f"核心指标 {name} 加载失败，系统无法启动。") from e
        return _MissingIndicator(name)

def __getattr__(name: str) -> Any:
    """模块级属性访问，实现懒加载。"""
    if name.startswith("_"):
        raise AttributeError(name)

    with _lock:
        if name not in _class_cache:
            _class_cache[name] = _load_module(name)
        result = _class_cache[name]
        if result is None or isinstance(result, _MissingIndicator):
            raise IndicatorNotAvailable(f"指标 {name} 不可用。")
        return result

# ---------------------------------------------------------------------------
# 公开 API 列表 (保持兼容)
# ---------------------------------------------------------------------------
__all__ = list(_MODULE_MAP.keys())

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def list_modules() -> List[str]:
    """返回所有已注册的指标名称列表。"""
    return list(_MODULE_MAP.keys())

def module_status(name: str) -> str:
    """查询指定指标的加载状态。"""
    with _lock:
        if name in _class_cache:
            obj = _class_cache[name]
            if obj is None:
                return "error"
            if isinstance(obj, type) and obj is not _MissingIndicator:
                return "loaded"
            return "disabled"
        return "pending"

def reload_module(name: str) -> None:
    """强制重新加载指定模块，清除缓存。"""
    with _lock:
        _class_cache.pop(name, None)
        # 强制重新导入
        mod_path = _MODULE_MAP.get(name)
        if mod_path:
            full = __name__ + mod_path
            try:
                importlib.invalidate_caches()
                importlib.reload(importlib.import_module(full))
                logger.info(f"模块 {name} 已重新加载。")
            except Exception as e:
                logger.error(f"重新加载 {name} 失败: {e}")

def debug_info() -> Dict[str, Any]:
    """返回所有模块的详细状态信息，用于调试和监控。"""
    info = {}
    for name in __all__:
        with _lock:
            obj = _class_cache.get(name)
            if obj is None:
                status = "error"
            elif isinstance(obj, _MissingIndicator):
                status = "disabled"
            elif isinstance(obj, type):
                status = "loaded"
            else:
                status = "pending"
        info[name] = {
            "status": status,
            "module": _MODULE_MAP.get(name, "unknown"),
            "core": name in CORE_MODULES,
        }
    return info

# ---------------------------------------------------------------------------
# 类型存根 (TYPE_CHECKING)
# ---------------------------------------------------------------------------
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .kma import KalmanTrendline
    from .hmm_state_detector import HMMStateDetector
    from .hmm_trainer import HMMTrainer
    from .trend_probability_filter import TrendProbabilityFilter
    from .escape_detector import StageTopEscapeDetector
    from .swing_recapture import SwingRecaptureModule
    from .callback_drop import CallbackDropModule
    from .pullback_add import PullbackAddModule
    from .micro_pullback_scalper import MicroPullbackScalper
    from .micro_divergence_trader import MicroDivergenceTrader
    from .range_grid import RangeOscillationGrid
    from .volume_profile_mr import VolumeProfileMeanReversion
    from .vol_squeeze_breakout import VolatilitySqueezePreBreakout
    from .micro_scalp_obi import MicroScalpOnOBI
    from .sr_5min import SwingVolumeSR
    from .sr_15min import StructureFibSR
    from .adaptive_stage_sr import AdaptiveStageSR
    from .wave_similarity_engine import WaveSimilarityEngine
    from .wave_pattern_cache import WavePatternCache
