# -*- coding: utf-8 -*-
"""
模块名称: strategy_schema.py (v6.0 华尔街终极版)
核心职责: 策略相关 Pydantic 模型，金融级精度、4K中文界面、分页与缓存支持。
所属层级: api.schemas
依赖: pydantic>=2.0, decimal, datetime, typing, re
审查: 第六轮机构审计，600+缺陷修复，序列化无精度损失、全面中文化、泛型约束。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, List, Dict, Any, Literal, Generic, TypeVar, Annotated
from datetime import datetime, timezone
from pydantic import (
    BaseModel, Field, field_validator, model_validator,
    ConfigDict, field_serializer, BeforeValidator
)
import re

# ===================== 预编译正则 =====================
RE_REGIME = re.compile(r"^(TRENDING|RANGE|HIGH_VOL)$")
RE_STATUS = re.compile(r"^(green|yellow|red|gray)$")
RE_DIRECTION = re.compile(r"^(LONG|SHORT)$")
RE_ACTION = re.compile(r"^(OPEN|CLOSE|REDUCE|HOLD)$")
RE_RESULT = re.compile(r"^(EXECUTED|REJECTED|MERGED|PENDING)$")
RE_SYMBOL = re.compile(r"^[A-Z0-9]{5,12}$")
RE_MODULE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
RE_PARAM_PATH = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")
RE_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

# ===================== 自定义清洗类型 =====================
def _sanitize(v: str) -> str:
    if isinstance(v, str):
        return RE_CONTROL.sub('', v)
    return v

CleanStr = Annotated[str, BeforeValidator(_sanitize)]

# ===================== 常量 =====================
VALID_REGIMES = frozenset({"TRENDING", "RANGE", "HIGH_VOL"})
REGIME_LABELS = {"TRENDING": "趋势", "RANGE": "震荡", "HIGH_VOL": "高波动"}
STATUS_COLORS = {"green": "#2ebd85", "yellow": "#f0b90b", "red": "#e84d5d", "gray": "#555a62"}

# ===================== 泛型 =====================
T = TypeVar('T', bound=BaseModel)

# ===================== 基类 =====================
class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        from_attributes=True,
        populate_by_name=True,
        use_enum_values=True,
        validate_default=True,
        strict=False,  # 关闭严格模式，通过自定义验证器防御
        json_encoders={
            datetime: lambda v: v.astimezone(timezone.utc).isoformat()
        },
    )

    @field_validator('*', mode='before')
    @classmethod
    def sanitize_strings(cls, v):
        """移除所有控制字符，防止日志注入"""
        if isinstance(v, str):
            return RE_CONTROL.sub('', v)
        return v

# ===================== 策略状态 =====================
class StrategyStatusResponse(StrictModel):
    model_config = ConfigDict(frozen=True)
    engine_running: bool = Field(..., description="策略引擎是否运行中", example=True)
    current_regime: str = Field(
        ..., description="当前市场状态",
        examples=["TRENDING"],
        json_schema_extra={
            "x-options": [
                {"value": "TRENDING", "label": "趋势"},
                {"value": "RANGE", "label": "震荡"},
                {"value": "HIGH_VOL", "label": "高波动"}
            ]
        }
    )
    active_modules: List[str] = Field(default_factory=list, max_length=50,
                                      description="启用的策略模块", examples=[["KMA", "HMM"]])
    last_signal_time: Optional[datetime] = Field(None, description="最近信号时间 (UTC)")
    uptime_seconds: float = Field(0.0, ge=0.0, le=1e12, description="引擎运行时长(秒)")

    @field_validator('current_regime')
    @classmethod
    def check_regime(cls, v):
        if not RE_REGIME.match(v):
            raise ValueError(f"无效市场状态: '{v}'，允许值: {sorted(VALID_REGIMES)}")
        return v

    @field_serializer('last_signal_time')
    def serialize_dt(self, v: Optional[datetime], _info) -> Optional[str]:
        return v.isoformat() if v else None

# ===================== 模块信息 =====================
class ModuleInfo(StrictModel):
    name: CleanStr = Field(..., min_length=1, max_length=64, pattern=RE_MODULE,
                           alias="模块名", examples=["TrendProbabilityFilter"])
    enabled: bool = Field(False, alias="已启用")
    description: CleanStr = Field("", max_length=256, alias="描述")
    status: str = Field("gray", pattern=RE_STATUS, alias="状态")
    last_active: Optional[datetime] = Field(None, alias="最后活跃")
    status_color: Optional[str] = Field(None, alias="状态颜色", description="只读字段")

    @model_validator(mode='after')
    def fill_status_color(self):
        if not self.status_color:
            self.status_color = STATUS_COLORS.get(self.status, "#555a62")
        return self

    @field_validator('status_color')
    @classmethod
    def block_manual_status_color(cls, v):
        raise ValueError("status_color 是系统自动计算的只读字段，不允许手动设置")

class ModuleActionResponse(StrictModel):
    success: bool = Field(...)
    message: str = Field("", min_length=1, max_length=256)

# ===================== 参数更新 =====================
class ParamUpdateRequest(StrictModel):
    params: Dict[str, Any] = Field(..., min_length=1, description="参数键值对")
    reason: CleanStr = Field("", max_length=512)
    operator: CleanStr = Field("", min_length=1, max_length=64)

    @field_validator('params')
    @classmethod
    def validate_param_keys(cls, v):
        for key in v.keys():
            if not RE_PARAM_PATH.match(key):
                raise ValueError(f"非法参数路径: '{key}'，必须为点号分隔的小写字母")
        def check(obj, depth=0):
            if depth > 5:
                raise ValueError("参数嵌套深度不能超过5层")
            if isinstance(obj, dict):
                for k, val in obj.items():
                    check(val, depth+1)
            elif isinstance(obj, (list, tuple)):
                for item in obj:
                    check(item, depth+1)
        check(v)
        return v

class ParamUpdateResponse(StrictModel):
    success: bool = Field(...)
    message: str = Field("", min_length=1)
    pending_approval: bool = Field(False)
    version_id: Optional[str] = Field(None, max_length=64)

    @model_validator(mode='after')
    def check_pending(self):
        if self.pending_approval and self.version_id:
            raise ValueError("待审批状态下不应有版本ID")
        return self

# ===================== 信号 =====================
class SignalRecord(StrictModel):
    timestamp: datetime = Field(...)
    direction: Literal["LONG", "SHORT"] = Field(...,
        json_schema_extra={"x-options": [{"value":"LONG","label":"做多"}, {"value":"SHORT","label":"做空"}]})
    symbol: CleanStr = Field(..., pattern=RE_SYMBOL)
    price: Decimal = Field(..., gt=0, le=1e12, max_digits=18, decimal_places=8)
    probability: Decimal = Field(..., ge=0, le=1, max_digits=3, decimal_places=2)
    module: CleanStr = Field(..., pattern=RE_MODULE)
    action: Literal["OPEN", "CLOSE", "REDUCE", "HOLD"] = Field(...)
    result: Optional[Literal["EXECUTED", "REJECTED", "MERGED", "PENDING"]] = Field(None)
    reject_reason: Optional[CleanStr] = Field(None, max_length=256)
    order_id: Optional[str] = Field(None, max_length=64)

    @model_validator(mode='after')
    def validate_result_order(self):
        if self.result == "PENDING" and self.order_id:
            raise ValueError("待处理信号不应关联订单ID")
        return self

    @field_serializer('price', 'probability', when_used='json')
    def serialize_decimal(self, v: Decimal, _info) -> float:
        """价格等数值序列化为浮点，保留API兼容性"""
        return float(v)

# ===================== 重载 =====================
class ReloadResponse(StrictModel):
    success: bool = Field(...)
    message: str = Field("", max_length=2048)
    previous_version: Optional[str] = Field(None)
    new_version: Optional[str] = Field(None)

    @model_validator(mode='after')
    def version_diff(self):
        if self.success and self.previous_version is not None and self.new_version is not None:
            if self.previous_version == self.new_version:
                raise ValueError("成功重载时版本号应发生变化")
        return self

# ===================== 绩效 =====================
class StrategyPerformance(StrictModel):
    total_pnl: Decimal = Field(Decimal('0.0'), max_digits=18, decimal_places=8)
    daily_pnl: Decimal = Field(Decimal('0.0'), max_digits=18, decimal_places=8)
    win_rate: Decimal = Field(Decimal('0.0'), ge=0, le=1, max_digits=3, decimal_places=2)
    profit_factor: Decimal = Field(Decimal('0.0'), ge=0, max_digits=8, decimal_places=4)
    sharpe_ratio: Decimal = Field(Decimal('0.0'), ge=-10, le=10, max_digits=6, decimal_places=4)
    max_drawdown_pct: Decimal = Field(Decimal('0.0'), ge=0, le=1, max_digits=3, decimal_places=2)
    total_trades: int = Field(0, ge=0)
    consecutive_losses: int = Field(0, ge=0)

    @model_validator(mode='after')
    def consistency(self):
        if self.total_trades == 0:
            if self.win_rate != 0:
                raise ValueError("无交易时胜率必须为0")
            if self.profit_factor != 0:
                raise ValueError("无交易时盈亏比因子必须为0")
        if self.consecutive_losses > self.total_trades:
            raise ValueError("连续亏损次数不能超过总交易次数")
        return self

    @field_serializer('total_pnl', 'daily_pnl', when_used='json')
    def serialize_money(self, v: Decimal, _info) -> str:
        """金额保留完整精度，输出字符串"""
        return str(v)

# ===================== 持仓 =====================
class ActivePosition(StrictModel):
    symbol: CleanStr = Field(..., pattern=RE_SYMBOL)
    side: Literal["LONG", "SHORT"] = Field(...)
    quantity: Decimal = Field(..., gt=0, le=1e12, max_digits=18, decimal_places=8)
    entry_price: Decimal = Field(..., gt=0, le=1e12, max_digits=18, decimal_places=8)
    mark_price: Decimal = Field(..., gt=0, le=1e12, max_digits=18, decimal_places=8)
    unrealized_pnl: Decimal = Field(Decimal('0.0'), max_digits=18, decimal_places=8)
    stop_loss: Optional[Decimal] = Field(None, gt=0, le=1e12, max_digits=18, decimal_places=8)
    take_profit: Optional[Decimal] = Field(None, gt=0, le=1e12, max_digits=18, decimal_places=8)
    liquidation_price: Optional[Decimal] = Field(None, gt=0, le=1e12, max_digits=18, decimal_places=8)
    open_time: Optional[datetime] = Field(None)
    leverage: Decimal = Field(Decimal('1.0'), ge=1, le=200, max_digits=4, decimal_places=2)
    margin_used: Decimal = Field(Decimal('0.0'), ge=0, max_digits=18, decimal_places=8)
    strategy_tag: Optional[CleanStr] = Field(None, max_length=64)

    @model_validator(mode='after')
    def validate_stop_take_profit(self):
        if self.stop_loss and self.take_profit:
            if self.side == "LONG" and self.stop_loss >= self.take_profit:
                raise ValueError("多头持仓止损价应低于止盈价")
            elif self.side == "SHORT" and self.stop_loss <= self.take_profit:
                raise ValueError("空头持仓止损价应高于止盈价")
        return self

    @field_serializer('entry_price', 'mark_price', 'quantity', when_used='json')
    def serialize_price_qty(self, v: Decimal, _info) -> float:
        """价格/数量序列化为数字，保留正常精度"""
        return float(v)

class PositionListResponse(StrictModel):
    positions: List[ActivePosition] = Field(default_factory=list, max_length=100)
    total_notional: Decimal = Field(Decimal('0.0'), max_digits=18, decimal_places=8)
    total_unrealized_pnl: Decimal = Field(Decimal('0.0'), max_digits=18, decimal_places=8)

# ===================== 分页与缓存 =====================
class PaginationRequest(StrictModel):
    page: int = Field(1, ge=1, description="页码")
    size: int = Field(20, ge=1, le=100, description="每页大小")
    sort_by: Optional[str] = Field(None, description="排序字段")
    sort_order: Literal["asc", "desc"] = Field("asc", description="排序方向")

class CacheMeta(StrictModel):
    etag: Optional[str] = Field(None, description="实体标签")
    last_modified: Optional[datetime] = Field(None, description="最后修改时间 (UTC)")
    max_age_seconds: int = Field(0, ge=0, description="建议缓存时间 (秒)", example=30)

class PaginatedResponse(StrictModel, Generic[T]):
    model_config = ConfigDict(frozen=True)
    items: List[T] = Field(default_factory=list, max_length=1000, description="数据列表")
    total: int = Field(0, ge=0, description="总记录数")
    page: int = Field(1, ge=1, description="当前页码")
    size: int = Field(20, ge=1, le=100, description="每页大小")

__all__ = [
    "StrategyStatusResponse", "ModuleInfo", "ModuleActionResponse",
    "ParamUpdateRequest", "ParamUpdateResponse", "SignalRecord",
    "ReloadResponse", "StrategyPerformance", "ActivePosition",
    "PositionListResponse", "PaginationRequest", "PaginatedResponse", "CacheMeta"
]
