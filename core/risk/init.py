# -*- coding: utf-8 -*-
"""
模块名称: core/risk/__init__.py
核心职责: 风险控制与资金管理子系统的统一入口，负责惰性加载并导出所有风控组件。
所属层级: core.risk

组件速览:
    - RiskFirewall: 三层风控防火墙，拦截所有订单
    - GlobalRiskBus: 全局风险预算总线，协调多策略资金分配
    - PositionSizer: 动态仓位计算器（波动率自适应）
    - HardRiskFilter: 硬编码熔断器，保护账户底线安全
    - ActionArbitrator: 信号冲突仲裁器，确定最终执行动作
    - MaxDrawdownRule: 最大回撤规则
    - DailyLossRule: 日亏损规则
    - VolatilityGuard: 波动率防护罩

外部依赖:
    - logging (日志记录)
    - sys (Python 版本检查)
    - typing (类型注解)
    - time (性能度量)

接口契约:
    提供: 通过惰性加载暴露上述组件，确保导入失败时系统仍可有限降级运行。
    消费: 被 strategy_engine.py 等核心模块调用。

配置项: 无，所有配置由调用方注入。

作者: KHAOS System Architect
创建日期: 2025-03-15
修改记录:
    - 2026-07-11 KHAOS Audit AI: 机构级重构，增加容错、惰性加载、元信息。
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, Optional, List

# 版本与元信息（合规审计必备）
__author__ = "KHAOS Risk Committee"
__version__ = "3.0.0"
__status__ = "Production"
__license__ = "UNLICENSED"
__url__ = "https://internal.khaos.quant/risk"

# 自定义异常
class RiskModuleError(Exception):
    """风险模块发生严重错误，无法继续交易。"""
    pass

# 导出列表（按字母排序）
__all__: List[str] = [
    "ActionArbitrator",
    "DailyLossRule",
    "GlobalRiskBus",
    "HardRiskFilter",
    "MaxDrawdownRule",
    "PositionSizer",
    "RiskFirewall",
    "VolatilityGuard",
]

# 获取日志器
_logger = logging.getLogger(__name__)
_start_time = time.perf_counter()

# ---------- 运行时环境检查 ----------
if sys.version_info < (3, 10):
    _logger.critical("KHAOS 风险模块要求 Python >= 3.10，当前版本 %s 不兼容。", sys.version)
    sys.exit(1)

# ---------- 惰性加载与容错机制 ----------
# 存储已加载的类引用，键为类名，值为类对象（或 None 表示加载失败）
_LOADED: Dict[str, Optional[Any]] = {}
_IMPORT_ERRORS: Dict[str, str] = {}

def __getattr__(name: str) -> Any:
    """
    模块级别的 __getattr__，实现惰性导入。
    当首次访问某个导出类时，才实际导入对应的子模块，降低启动开销并隔离故障。
    """
    if name in _LOADED:
        return _LOADED[name]

    # 映射类名到模块路径
    module_map: Dict[str, str] = {
        "RiskFirewall": ".risk_firewall",
        "GlobalRiskBus": ".global_risk_bus",
        "PositionSizer": ".position_sizer",
        "HardRiskFilter": ".hard_risk_filter",
        "ActionArbitrator": ".action_arbitrator",
        "MaxDrawdownRule": ".max_drawdown_rule",
        "DailyLossRule": ".daily_loss_rule",
        "VolatilityGuard": ".volatility_guard",
    }

    if name not in module_map:
        raise AttributeError(f"模块 {__name__} 没有属性 {name}")

    try:
        # 动态导入子模块，并提取同名类
        module = __import__(module_map[name], fromlist=[name], level=1)
        cls = getattr(module, name)
        _LOADED[name] = cls
        _logger.debug("惰性加载成功: %s (%.2fms 累计)", name, (time.perf_counter() - _start_time) * 1000)
        return cls
    except Exception as e:
        _LOADED[name] = None
        _IMPORT_ERRORS[name] = str(e)
        _logger.error("风险组件 %s 加载失败: %s。该功能将不可用。", name, e)
        # 返回一个占位符类，避免完全崩溃
        return type(name, (), {"__init__": lambda s, *a, **kw: None, "error": True})

# 记录包加载完成的耗时
_load_time_ms = (time.perf_counter() - _start_time) * 1000
_logger.info("core.risk 包初始化完成，耗时 %.2f ms", _load_time_ms)

# 可选：当包被直接执行时打印状态
if __name__ == "__main__":
    print(f"KHAOS Risk Module v{__version__}")
    print(f"已加载组件: {[k for k, v in _LOADED.items() if v is not None]}")
    if _IMPORT_ERRORS:
        print("加载失败的组件:", _IMPORT_ERRORS)
