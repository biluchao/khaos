# -*- coding: utf-8 -*-
"""
KHAOS API 数据模型中心 (v1.0)
===============================
集中管理所有 Pydantic 模型，提供统一导入出口、全局校验配置、版本管理及容错降级。
遵循华尔街机构级标准，适用于 100 美金至万亿美金账户的量化交易系统。

使用示例：
    from api.schemas import OrderRequest, OrderResponse, OrderSide

作者: KHAOS Data Architecture Team
创建日期: 2026-07-15
最后审查: 2026-07-18
"""

import logging
import time
from typing import TYPE_CHECKING, List, Dict, Any, Optional, Set

# Pydantic v2 兼容性检查
try:
    import pydantic
    PYDANTIC_MAJOR = int(pydantic.__version__.split('.')[0])
    if PYDANTIC_MAJOR < 2:
        logging.error("KHAOS requires Pydantic v2+. Please upgrade.")
except ImportError:
    logging.error("Pydantic is not installed. Models will not be available.")
    pydantic = None

from pydantic import BaseModel, ConfigDict

# 配置日志
logger = logging.getLogger(__name__)
_start_time = time.time()

# 全局 Schema 版本
__schema_version__ = "1.0.0"

# ---------------------------------------------------------------
# 全局 Pydantic 配置基类
# ---------------------------------------------------------------
class KhaosBaseModel(BaseModel):
    """所有数据模型的基类，强制禁止额外字段，开启严格模式"""
    model_config = ConfigDict(
        extra='forbid',          # 禁止未定义的字段，防止静默忽略错误参数
        str_strip_whitespace=True,  # 自动去除字符串首尾空格
        validate_assignment=True,   # 赋值时校验
        use_enum_values=True,       # 枚举序列化为值
        populate_by_name=True,      # 允许字段别名
    )

# ---------------------------------------------------------------
# 通用工具模型
# ---------------------------------------------------------------
class ErrorResponse(KhaosBaseModel):
    """统一错误响应"""
    code: int
    message: str
    details: Optional[Any] = None
    request_id: Optional[str] = None
    locale: str = "zh-CN"

class Pagination(KhaosBaseModel):
    """分页参数"""
    page: int = 1
    size: int = 20
    total: Optional[int] = None

class HealthResponse(KhaosBaseModel):
    """健康检查响应"""
    status: str
    version: str
    uptime_seconds: float
    modules: Dict[str, str]

# ---------------------------------------------------------------
# 安全导入子模块（防御式加载）
# ---------------------------------------------------------------
_IMPORT_ERRORS: List[str] = []

def _safe_import(module_name: str, symbols: List[str]) -> Dict[str, Any]:
    """安全导入子模块，捕获异常并记录"""
    try:
        module = __import__(f".{module_name}", fromlist=symbols, package=__package__)
        return {sym: getattr(module, sym) for sym in symbols}
    except Exception as e:
        logger.error(f"Failed to import {module_name}: {e}", exc_info=True)
        _IMPORT_ERRORS.append(module_name)
        return {}

# 实际导入（可根据需要动态加载，但为了启动时验证，还是提前导入）
_ORDER_SCHEMA = _safe_import("order_schema", [
    "OrderType", "OrderSide", "OrderStatus",
    "OrderRequest", "OrderResponse", "OrderListResponse",
    "CancelOrderRequest", "CancelOrderResponse"
])

_STRATEGY_SCHEMA = _safe_import("strategy_schema", [
    "StrategyStatus", "ModuleInfo", "ModuleActionResponse",
    "SignalRecord", "ParamUpdateRequest", "ParamUpdateResponse"
])

_RISK_SCHEMA = _safe_import("risk_schema", [
    "RiskBudgetConfig", "LeverageConfig", "LossLimits",
    "PositionSizingConfig", "CopyTradingConfig", "PaperBrokerConfig"
])

_CONFIG_SCHEMA = _safe_import("config_schema", [
    "GlobalConfig", "StrategyConfig", "RiskConfig",
    "ExecutionConfig", "DataSourcesConfig", "EvolutionConfig"
])

_SIGNAL_SCHEMA = _safe_import("signal_schema", [
    "SignalRecord", "SignalDirection", "SignalAction"
])

# 若关键模块导入失败，记录严重错误
if _IMPORT_ERRORS:
    logger.warning(f"Some schema modules failed to load: {_IMPORT_ERRORS}")

# ---------------------------------------------------------------
# 导出所有公共符号（精确控制）
# ---------------------------------------------------------------
__all__ = [
    # 基类
    "KhaosBaseModel", "ErrorResponse", "Pagination", "HealthResponse",
    # 订单
    "OrderType", "OrderSide", "OrderStatus",
    "OrderRequest", "OrderResponse", "OrderListResponse",
    "CancelOrderRequest", "CancelOrderResponse",
    # 策略
    "StrategyStatus", "ModuleInfo", "ModuleActionResponse",
    "ParamUpdateRequest", "ParamUpdateResponse",
    # 信号
    "SignalRecord", "SignalDirection", "SignalAction",
    # 风险
    "RiskBudgetConfig", "LeverageConfig", "LossLimits",
    "PositionSizingConfig", "CopyTradingConfig", "PaperBrokerConfig",
    # 配置
    "GlobalConfig", "StrategyConfig", "RiskConfig",
    "ExecutionConfig", "DataSourcesConfig", "EvolutionConfig",
    # 版本
    "__schema_version__",
]

# ---------------------------------------------------------------
# 运行时后处理：处理交叉引用模型重建
# ---------------------------------------------------------------
# 部分模型可能因为循环引用需要重建，例如 OrderResponse 中可能引用 StrategyStatus
# 这里统一触发 model_rebuild（Pydantic v2）
def _rebuild_models():
    """重建那些定义时尚未解析的模型"""
    rebuild_targets = []
    for name in __all__:
        obj = globals().get(name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and name != "KhaosBaseModel":
            try:
                obj.model_rebuild()
            except Exception:
                pass

if not _IMPORT_ERRORS:
    _rebuild_models()

# ---------------------------------------------------------------
# 启动性能日志
# ---------------------------------------------------------------
_elapsed = time.time() - _start_time
logger.info(f"api.schemas initialized in {_elapsed:.3f}s (loaded {len(__all__)} symbols, {len(_IMPORT_ERRORS)} errors)")

# ---------------------------------------------------------------
# 测试辅助：提供一个简易工厂
# ---------------------------------------------------------------
class TestModelFactory:
    """为单元测试提供快速构建模型实例的工厂方法"""
    @staticmethod
    def create_order_request(symbol: str = "BTCUSDT", side: str = "buy") -> Any:
        return OrderRequest(symbol=symbol, side=side, order_type="market", quantity=0.001)

    @staticmethod
    def create_health_response(status: str = "ok") -> HealthResponse:
        return HealthResponse(status=status, version=__schema_version__, uptime_seconds=0.0, modules={})
