# -*- coding: utf-8 -*-
"""
模块名称: core/execution/__init__.py
核心职责: 订单执行子包的入口，统一导出所有执行相关组件，并处理导入失败场景。
所属层级: core.execution

版本: 2.0 (机构级韧性版)
作者: KHAOS System Architect
许可证: 内部使用 - 机密
创建日期: 2025-04-10
最后修改: 2026-07-29 机构级审计强化（导入状态一致性、可观测性、失败安全）
修改记录:
    - 2025-04-10 初始版本
    - 2026-07-12 增加导入保护、版本管理、日志记录、合规文档
    - 2026-07-29 强化导入失败后状态一致性、异常信息完整性、健康检查接口、重复导入保护

外部依赖:
    各子模块可能依赖 adapters.execution (交易所接口)、core.models.order 等。
    包内部模块禁止循环引用本包。

接口契约:
    提供: {
        'OrderManager': '订单生命周期管理',
        'OrderValidator': '订单合法性校验',
        'SlippageEstimator': '滑点预估',
        'TwapExecutor': 'TWAP 算法执行器',
        'FeeOptimizer': '手续费优化'
    }
    消费: 被策略引擎、风控模块直接调用。

使用示例:
    from core.execution import OrderManager, FeeOptimizer
    manager = OrderManager(...)
    optimizer = FeeOptimizer(...)

Python 版本要求: >= 3.8
"""

from __future__ import annotations

import logging
import sys
import threading
import warnings
from typing import Any, Dict, List, Optional, Type

# 版本信息
__version__ = "2.0.0"
__author__ = "KHAOS System Architect"
__license__ = "UNLICENSED"

logger = logging.getLogger(__name__)

# 组件注册表：单一数据源，避免三处硬编码不同步
_COMPONENT_SPECS: List[Dict[str, Any]] = [
    {"module": "order_manager", "class": "OrderManager", "critical": True},
    {"module": "order_validator", "class": "OrderValidator", "critical": True},
    {"module": "slippage_estimator", "class": "SlippageEstimator", "critical": True},
    {"module": "twap_executor", "class": "TwapExecutor", "critical": True},
    {"module": "fee_optimizer", "class": "FeeOptimizer", "critical": True},
]

# 运行时状态
_exported: List[str] = []
_import_errors: Dict[str, BaseException] = {}
_lock = threading.Lock()
_initialized = False

def _safe_import(module_name: str, class_name: str, is_critical: bool = True) -> Optional[Type]:
    """
    安全导入子模块中的类。
    失败时记录完整异常，返回 None，并根据关键性决定日志级别。
    """
    full_name = f"{__name__}.{module_name}"
    try:
        # 优先使用 importlib，兼容性更好，且便于未来扩展
        import importlib
        mod = importlib.import_module(full_name)
        cls = getattr(mod, class_name)
        if not isinstance(cls, type):
            raise TypeError(f"{class_name} 不是可实例化的类型，实际类型: {type(cls)}")
        _exported.append(class_name)
        return cls
    except BaseException as e:  # 捕获所有，包括 SystemExit 以外的极端情况
        # 保留完整异常对象供诊断
        _import_errors[class_name] = e
        msg = (
            f"无法导入执行组件 {class_name} (模块 {full_name}): "
            f"{type(e).__name__}: {e}"
        )
        if is_critical:
            logger.error(msg, exc_info=True)
        else:
            logger.warning(msg, exc_info=True)
            warnings.warn(msg, RuntimeWarning, stacklevel=2)
        return None

def _do_import() -> None:
    """实际执行导入，受锁保护，防止重复与并发。"""
    global _initialized
    with _lock:
        if _initialized:
            return
        _exported.clear()
        _import_errors.clear()

        for spec in _COMPONENT_SPECS:
            cls = _safe_import(spec["module"], spec["class"], spec["critical"])
            # 将结果绑定到模块全局命名空间（保持原有导出语义）
            globals()[spec["class"]] = cls

        missing = [s["class"] for s in _COMPONENT_SPECS if s["class"] not in _exported]
        if missing:
            logger.critical(
                "关键执行组件缺失，交易功能将不可用: %s | 详细错误: %s",
                missing,
                {k: f"{type(v).__name__}: {v}" for k, v in _import_errors.items()},
            )
        else:
            logger.debug("执行模块全部加载成功")

        # 动态设置 __all__（只包含成功导入的组件，保持原语义）
        globals()["__all__"] = list(_exported)

        logger.info(
            "core.execution 包初始化完成，版本 %s，已加载组件: %s，缺失: %s",
            __version__,
            _exported,
            missing,
        )
        _initialized = True

# 执行导入（只在第一次真正需要时）
_do_import()

# 对外健康检查接口（新增，不破坏原有契约）
def is_execution_ready() -> bool:
    """返回所有关键组件是否全部成功加载。"""
    return len(_exported) == len(_COMPONENT_SPECS) and not _import_errors

def get_missing_components() -> List[str]:
    """返回当前缺失的组件名称列表。"""
    return [s["class"] for s in _COMPONENT_SPECS if s["class"] not in _exported]

def get_import_errors() -> Dict[str, str]:
    """返回组件名到错误描述的映射，便于运维诊断。"""
    return {k: f"{type(v).__name__}: {v}" for k, v in _import_errors.items()}

# 防止直接运行
if __name__ == "__main__":
    print("此模块不可直接运行，请作为包导入。")
    sys.exit(1)
