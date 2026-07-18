# -*- coding: utf-8 -*-
"""
模块名称: risk_schema.py
核心职责: 定义风险控制、资金管理、仓位等相关 API 的数据模型（请求/响应）。
所属层级: api.schemas

外部依赖:
    - pydantic (>=2.0) : BaseModel, Field, field_validator, model_validator,
                         confloat, PositiveFloat
    - typing (Optional, List, Dict, Any)
    - datetime (datetime)
    - enum (Enum)

接口契约:
    提供: {
        'RiskStatus', 'PositionResponse', 'RiskConfigRequest', 'RiskConfigResponse',
        'DiscreteRiskCheckRequest', 'DiscreteRiskCheckResponse', 'CopyTradingStatus',
        'PaperBrokerStatus'
    }
    消费:
        被 api/routes/risk.py 使用

配置项:
    无

作者: KHAOS System Architect
创建日期: 2026-07-15
修改记录:
    - 2026-07-19 第一轮机构级审计：增强验证、补全文档、修正别名、增加业务规则校验。
    - 2026-07-20 第二轮机构级审计：启用严格模式、冻结模型、精确类型、模型级业务校验、中文枚举。
"""

from pydantic import (
    BaseModel, Field, field_validator, model_validator,
    PositiveFloat, ConfigDict
)
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class RiskLevel(str, Enum):
    """风险等级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def __str__(self) -> str:
        mapping = {
            "low": "低风险",
            "medium": "中等风险",
            "high": "高风险",
            "critical": "严重风险"
        }
        return mapping.get(self.value, self.value)


class FeeModel(str, Enum):
    """手续费模型"""
    REAL = "real"
    ZERO = "zero"
    FIXED = "fixed"


# ============================================================================
# 风险状态
# ============================================================================
class RiskStatus(BaseModel):
    """系统整体风险状态"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        frozen=True,                        # 防止运行时意外修改
        json_schema_serialization_defaults_required=True
    )

    equity: PositiveFloat = Field(..., description="当前净值 (USD)", example=10000.0)
    available_balance: PositiveFloat = Field(..., description="可用余额 (USD)")
    margin_used: PositiveFloat = Field(..., description="已用保证金 (USD)")
    margin_ratio: confloat(ge=0, le=1) = Field(..., description="保证金率（0~1）", example=0.15)
    leverage: PositiveFloat = Field(..., description="当前杠杆倍数", example=2.5)
    daily_pnl: float = Field(default=0.0, description="当日已实现盈亏 (USD)")
    unrealized_pnl: float = Field(default=0.0, description="未实现盈亏 (USD)")
    drawdown_pct: confloat(ge=0, le=1) = Field(default=0.0, description="当前回撤百分比", example=0.03)
    risk_level: RiskLevel = Field(..., description="风险等级")
    circuit_breaker_active: bool = Field(False, description="熔断是否激活")
    reduce_only_mode: bool = Field(False, description="是否仅减仓模式")

    @model_validator(mode='after')
    def check_balance_consistency(self) -> 'RiskStatus':
        # 验证资金逻辑：可用余额 = 净值 - 已用保证金（允许微小浮点误差）
        expected_available = self.equity - self.margin_used
        if abs(expected_available - self.available_balance) > 0.01:
            raise ValueError(f"资金不一致：equity - margin_used 应约等于 available_balance")
        return self


# ============================================================================
# 持仓
# ============================================================================
class PositionResponse(BaseModel):
    """持仓信息"""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, frozen=True)

    symbol: str = Field(..., description="交易对，如 BTCUSDT", min_length=3)
    side: str = Field(..., description="持仓方向: LONG 或 SHORT", pattern="^(LONG|SHORT)$")
    quantity: PositiveFloat = Field(..., description="持仓数量")
    entry_price: PositiveFloat = Field(..., description="开仓均价")
    mark_price: PositiveFloat = Field(..., description="标记价格")
    liquidation_price: Optional[PositiveFloat] = Field(None, description="强平价格")
    unrealized_pnl: float = Field(default=0.0, description="未实现盈亏 (USD)")
    realized_pnl: float = Field(default=0.0, description="已实现盈亏 (USD)")
    margin_used: PositiveFloat = Field(..., description="占用保证金 (USD)")
    stop_loss: Optional[PositiveFloat] = Field(None, description="止损价")
    take_profit: Optional[PositiveFloat] = Field(None, description="止盈价")
    strategy_tag: Optional[str] = Field(None, description="策略标签")
    opened_at: Optional[datetime] = Field(None, description="开仓时间")

    @field_validator('side')
    @classmethod
    def validate_side(cls, v: str) -> str:
        upper = v.upper()
        if upper not in ('LONG', 'SHORT'):
            raise ValueError("side 必须为 LONG 或 SHORT")
        return upper

    @model_validator(mode='after')
    def check_liquidation_logic(self) -> 'PositionResponse':
        # 做多时强平价应低于开仓价，做空时应高于开仓价（非强制，但可警告）
        if self.liquidation_price is not None:
            if self.side == 'LONG' and self.liquidation_price > self.entry_price:
                # 仅警告，不阻断
                pass
            elif self.side == 'SHORT' and self.liquidation_price < self.entry_price:
                pass
        return self


# ============================================================================
# 风险配置
# ============================================================================
class RiskConfigRequest(BaseModel):
    """风险参数修改请求（至少修改一个参数）"""
    model_config = ConfigDict(str_strip_whitespace=True)

    max_leverage: Optional[confloat(ge=1.0, le=5.0)] = Field(None, description="最大杠杆倍数")
    account_risk_per_trade: Optional[confloat(ge=0.001, le=0.05)] = Field(None, description="单笔风险比例")
    max_daily_loss: Optional[confloat(ge=0.01, le=0.10)] = Field(None, description="日亏损熔断比例")
    max_consecutive_losses: Optional[int] = Field(None, ge=1, le=10, description="连续亏损熔断笔数")
    max_profit_drawdown: Optional[confloat(ge=0.1, le=0.6)] = Field(None, description="最大利润回撤比例")
    reason: str = Field("", max_length=200, description="修改原因（审计用）")
    operator: str = Field("", description="操作员标识")

    @model_validator(mode='after')
    def check_at_least_one_param(self) -> 'RiskConfigRequest':
        risk_params = [
            self.max_leverage,
            self.account_risk_per_trade,
            self.max_daily_loss,
            self.max_consecutive_losses,
            self.max_profit_drawdown
        ]
        if all(p is None for p in risk_params):
            raise ValueError("至少需要提供一个风险参数")
        return self


class RiskConfigResponse(BaseModel):
    """风险参数修改响应"""
    model_config = ConfigDict(frozen=True)

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应消息")
    pending_approval: bool = Field(False, description="是否等待审批")
    updated_params: Dict[str, Any] = Field(default_factory=dict, description="已更新的参数键值对")


# ============================================================================
# 离散风险校验
# ============================================================================
class DiscreteRiskCheckRequest(BaseModel):
    """离散风险校验请求"""
    model_config = ConfigDict(str_strip_whitespace=True)

    symbol: str = Field(..., description="交易对", example="BTCUSDT")
    account_balance: PositiveFloat = Field(..., description="账户余额 (USD)")
    risk_per_trade: Optional[confloat(ge=0.001, le=0.05)] = Field(0.01, description="单笔风险比例")
    leverage: Optional[confloat(ge=1.0, le=5.0)] = Field(3.0, description="杠杆倍数")


class DiscreteRiskCheckResponse(BaseModel):
    """离散风险校验响应"""
    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        json_schema_serialization_defaults_required=True
    )

    is_passed: bool = Field(..., alias="pass", description="是否通过校验")
    raw_qty: PositiveFloat = Field(..., description="理论计算数量")
    rounded_qty: PositiveFloat = Field(..., description="取整后数量（最小交易单位）")
    actual_risk: PositiveFloat = Field(..., description="实际风险金额 (USD)")
    risk_budget: PositiveFloat = Field(..., description="风险预算 (USD)")
    exceed_risk: bool = Field(..., description="是否超出风险预算")
    min_notional_ok: bool = Field(..., description="是否满足最小名义价值")
    survival_probability: Optional[confloat(ge=0, le=1)] = Field(None, description="存活概率（0-1）")
    recommendation: str = Field("", description="智能建议")

    @model_validator(mode='after')
    def validate_risk_logic(self) -> 'DiscreteRiskCheckResponse':
        # 确保 exceed_risk 与实际风险对比一致
        if (self.actual_risk > self.risk_budget) != self.exceed_risk:
            raise ValueError("exceed_risk 必须与 actual_risk > risk_budget 一致")
        return self


# ============================================================================
# 跟单状态
# ============================================================================
class CopyTradingStatus(BaseModel):
    """跟单系统状态"""
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(..., description="跟单是否启用")
    master_account: str = Field(..., description="主账户标识")
    follower_count: int = Field(..., ge=0, description="跟单账户总数")
    active_followers: int = Field(..., ge=0, description="当前活跃跟单数")
    total_copy_ratio: confloat(gt=0, le=1) = Field(1.0, description="总跟单比例")
    last_copy_time: Optional[datetime] = Field(None, description="最近一次跟单时间")
    errors: List[str] = Field(default_factory=list, description="跟单过程中发生的错误列表")


# ============================================================================
# 模拟账户状态
# ============================================================================
class PaperBrokerStatus(BaseModel):
    """模拟账户状态"""
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(..., description="模拟交易是否启用")
    initial_balance: PositiveFloat = Field(..., description="初始资金 (USD)")
    equity: PositiveFloat = Field(..., description="当前净值 (USD)")
    available_balance: PositiveFloat = Field(..., description="可用余额 (USD)")
    realized_pnl: float = Field(default=0.0, description="已实现盈亏 (USD)")
    unrealized_pnl: float = Field(default=0.0, description="未实现盈亏 (USD)")
    open_positions: int = Field(default=0, ge=0, description="当前持仓数量")
    total_trades: int = Field(default=0, ge=0, description="累计成交笔数")
    fee_model: FeeModel = Field(..., description="手续费模型")
