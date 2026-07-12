# -*- coding: utf-8 -*-
"""
模块名称: core/execution/__init__.py
核心职责: 订单执行子包的入口，统一导出所有执行相关组件，并处理导入失败场景。
所属层级: core.execution

版本: 2.0 (机构级韧性版)
作者: KHAOS System Architect
许可证: 内部使用 - 机密
创建日期: 2025-04-10
最后修改: 2026-07-12 机构级审计重构
修改记录:
    - 2025-04-10 初始版本
    - 2026-07-12 增加导入保护、版本管理、日志记录、合规文档

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

import logging
import warnings
from typing import List

# 版本信息
__version__ = "2.0.0"
__author__ = "KHAOS System Architect"
__license__ = "UNLICENSED"

logger = logging.getLogger(__name__)

# 存储成功导入的类名，用于动态构建 __all__
_exported: List[str] = []

def _safe_import(module_name: str, class_name: str, is_critical: bool = True):
    """
    安全导入子模块中的类，失败时根据关键性记录错误或警告，并返回 None。
    """
    try:
        mod = __import__(f"{__name__}.{module_name}", fromlist=[class_name])
        cls = getattr(mod, class_name)
        _exported.append(class_name)
        return cls
    except Exception as e:
        msg = f"无法导入执行组件 {class_name} (模块 {module_name}): {e}"
        if is_critical:
            logger.error(msg)
        else:
            logger.warning(msg)
            warnings.warn(msg)
        return None

# 顺序导入各个执行组件，标记为关键
OrderManager = _safe_import("order_manager", "OrderManager", is_critical=True)
OrderValidator = _safe_import("order_validator", "OrderValidator", is_critical=True)
SlippageEstimator = _safe_import("slippage_estimator", "SlippageEstimator", is_critical=True)
TwapExecutor = _safe_import("twap_executor", "TwapExecutor", is_critical=True)
FeeOptimizer = _safe_import("fee_optimizer", "FeeOptimizer", is_critical=True)

# 如果任何关键组件缺失，记录严重错误，但让系统继续启动（可能只影响执行功能）
missing = [name for name in ["OrderManager", "OrderValidator", "SlippageEstimator",
                              "TwapExecutor", "FeeOptimizer"] if name not in _exported]
if missing:
    logger.critical(f"关键执行组件缺失，交易功能将不可用: {missing}")
else:
    logger.debug("执行模块全部加载成功")

# 动态设置 __all__
__all__ = _exported

# 包初始化完成标记
logger.info(f"core.execution 包初始化完成，版本 {__version__}，已加载组件: {__all__}")

# 防止直接运行
if __name__ == "__main__":
    print("此模块不可直接运行，请作为包导入。")
