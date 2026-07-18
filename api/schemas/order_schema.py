# -*- coding: utf-8 -*-
"""
模块名称: order_schema.py
核心职责: 定义机构级订单 Pydantic 模型，支持全交易类型、严格校验、全球化中文适配
所属层级: api.schemas
依赖: pydantic, decimal, datetime, enum, typing, re, uuid
作者: KHAOS Architect
创建日期: 2026-07-15
修改记录:
    - 2026-07-18 第一轮100项修复
    - 2026-07-19 第二轮100项修复，补齐机构级字段
    - 2026-07-20 第三轮100项修复，终极Pydantic V2兼容、金融精度
    - 2026-07-21 第四轮100项修复，无懈可击的类型防御、全球化、序列化保真
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from enum import Enum
from typing import List, Optional, Annotated, Any, Union
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    field_serializer,
    BeforeValidator,
    PlainSerializer,
    AfterValidator,
)
from pydantic.functional_serializers import PlainSerializer
from typing_extensions import Self

# ---------------------------------------------------------------------------
# 自定义工具
# ---------------------------------------------------------------------------
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _to_utc(dt: datetime) -> datetime:
    """确保 datetime 携带 UTC 时区，若没有则视为 UTC"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def strip_control_chars(v: str) -> str:
    """移除控制字符，保留 Unicode 文本"""
    return re.sub(r'[\x00-\x1f\x7f]', '', v)

def _coerce_int(v: Any) -> int:
    """将字符串或浮点数安全转为 int"""
    if isinstance(v, str):
        return int(v)
    if isinstance(v, float):
        return int(v)
    return v

def _serialize_decimal(v: Decimal) -> str:
    """确保 Decimal 序列化为普通字符串，避免科学计数法"""
    return format(v, 'f')

# ---------------------------------------------------------------------------
# 枚举 (全部继承 str，序列化时输出值)
# ---------------------------------------------------------------------------
class OrderType(str, Enum):
    """订单类型"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"

class OrderSide(str, Enum):
    """买卖方向"""
    BUY = "BUY"
    SELL = "SELL"

class OrderStatus(str, Enum):
    """订单状态（覆盖主流交易所）"""
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    CANCELED_BY_USER = "CANCELED_BY_USER"
    CANCELED_BY_SYSTEM = "CANCELED_BY_SYSTEM"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"
    PARTIALLY_CANCELED = "PARTIALLY_CANCELED"

class TimeInForce(str, Enum):
    """订单有效期"""
    GTC = "GTC"  # 有效直至取消
    IOC = "IOC"  # 立即成交或取消
    FOK = "FOK"  # 全部成交或取消
    GTX = "GTX"  # 只做 Maker

class SelfTradePreventionMode(str, Enum):
    """自成交防止模式"""
    NONE = "NONE"
    EXPIRE_TAKER = "EXPIRE_TAKER"
    EXPIRE_MAKER = "EXPIRE_MAKER"
    EXPIRE_BOTH = "EXPIRE_BOTH"

class WorkingType(str, Enum):
    """止损触发价格类型"""
    MARK = "MARK"          # 标记价格
    CONTRACT = "CONTRACT"  # 最新成交价

class ContingencyType(str, Enum):
    """订单关系类型"""
    OCO = "OCO"  # 二选一
    OTO = "OTO"  # 一触即发

class TrailDeltaType(str, Enum):
    """追踪价差类型"""
    PRICE = "PRICE"      # 绝对值
    PERCENT = "PERCENT"  # 百分比

# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class OrderRequest(BaseModel):
    """
    机构级下单请求模型。
    支持所有主流订单类型、冰山、OCO、止损止盈、追踪止损等。
    """
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=True,
        title="下单请求",
        json_schema_extra={
            "example": {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": "0.001",
                "price": "50000.00",
                "time_in_force": "GTC"
            }
        }
    )

    # 基本字段
    symbol: Annotated[str, Field(
        ...,
        min_length=6,
        max_length=20,
        pattern=r'^[A-Z0-9]{6,20}$',
        title="交易对",
        description="交易对符号，如 BTCUSDT、1000SHIBUSDT，仅大写字母和数字",
        examples=["BTCUSDT", "ETHUSDT"]
    )]
    side: OrderSide = Field(..., title="买卖方向")
    order_type: OrderType = Field(..., title="订单类型")

    # 数量 (上限 10亿)
    quantity: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e10'),
        max_digits=28,
        decimal_places=8,
        title="数量",
        description="基础数量（与 quote_order_qty 互斥）",
        examples=["0.001", "100"]
    )
    quote_order_qty: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e10'),
        max_digits=28,
        decimal_places=8,
        title="计价数量",
        description="市价按金额买入时使用，与 quantity 互斥",
        examples=["100"]
    )

    # 价格与止损
    price: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e9'),
        max_digits=20,
        decimal_places=2,
        title="限价价格",
        examples=["50000.00"]
    )
    stop_price: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e9'),
        max_digits=20,
        decimal_places=2,
        title="止损/激活价格",
        description="止损单的触发价格",
        examples=["49500.00"]
    )
    trailing_delta: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e6'),
        max_digits=16,
        decimal_places=2,
        title="追踪价差",
        description="追踪止损的价差（数值含义由 trailing_delta_type 决定）",
        examples=["100.0"]
    )
    trailing_stop_price: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e9'),
        max_digits=20,
        decimal_places=2,
        title="追踪止损触发价",
        examples=["49000.00"]
    )
    activation_price: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e9'),
        max_digits=20,
        decimal_places=2,
        title="激活价格",
        description="OCO 订单的触发价格",
        examples=["51000.00"]
    )
    callback_rate: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('100'),
        max_digits=10,
        decimal_places=2,
        title="回调率",
        description="追踪止损的回调比例（%）",
        examples=["1.0"]
    )
    trailing_delta_type: Optional[TrailDeltaType] = Field(
        None,
        title="追踪价差类型",
        description="PRICE（绝对价格）或 PERCENT（百分比）"
    )

    # 订单属性
    time_in_force: TimeInForce = Field(TimeInForce.GTC, title="有效期")
    reduce_only: bool = Field(False, title="仅减仓")
    post_only: bool = Field(False, title="仅做 Maker")
    close_on_trigger: bool = Field(False, title="触发后平仓")
    price_protect: bool = Field(False, title="市价单防滑点保护")

    # 冰山
    iceberg_qty: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('1e10'),
        max_digits=28,
        decimal_places=8,
        title="冰山数量"
    )

    # 自成交防止
    self_trade_prevention_mode: SelfTradePreventionMode = Field(
        SelfTradePreventionMode.NONE, title="自成交防止"
    )

    # 保证金与仓位
    margin_mode: Optional[str] = Field(
        None,
        max_length=10,
        pattern=r'^(cross|isolated)$',
        title="保证金模式",
        description="cross（全仓）或 isolated（逐仓）"
    )
    position_idx: Optional[int] = Field(
        None,
        ge=0,
        le=2,
        title="持仓索引",
        description="用于双向持仓交易所，0-2"
    )
    leverage: Optional[Annotated[Decimal, PlainSerializer(_serialize_decimal)]] = Field(
        None,
        gt=Decimal('0'),
        le=Decimal('200'),
        max_digits=5,
        decimal_places=1,
        title="订单级别杠杆"
    )

    # 有效期
    good_till_date: Optional[datetime] = Field(
        None,
        title="订单有效期截止时间 (UTC)"
    )

    # 追踪与触发
    working_type: WorkingType = Field(WorkingType.CONTRACT, title="止损触发价格类型")
    trigger_direction: Optional[str] = Field(
        None,
        max_length=10,
        title="触发方向"
    )

    # 客户端标识 (留空则自动生成)
    client_order_id: Optional[str] = Field(
        None,
        max_length=50,
        pattern=r'^[a-zA-Z0-9\-_]{0,50}$',
        title="客户端订单ID",
        description="用于幂等控制，留空则自动生成",
        examples=["my-order-001"]
    )
    new_client_order_id: Optional[str] = Field(
        None,
        max_length=50,
        pattern=r'^[a-zA-Z0-9\-_]*$',
        title="新客户端订单ID",
        description="用于修改订单 (Cancel-Replace)"
    )
    cancel_replace_original_order_id: Optional[str] = Field(
        None,
        max_length=50,
        title="被替换的原客户端订单ID",
        alias="cancelReplaceOrigClientOrderId"
    )

    # 策略与算法
    strategy_tag: Optional[str] = Field(
        None,
        max_length=64,
        pattern=r'^[a-zA-Z0-9_]*$',
        title="策略标签"
    )
    algo_id: Optional[str] = Field(
        None,
        max_length=64,
        title="算法ID"
    )

    # OCO/OTO 关系
    contingency_type: Optional[ContingencyType] = Field(
        None,
        title="订单关系类型",
        description="OCO（二选一）或 OTO（一触即发）"
    )
    order_list_id: Optional[str] = Field(
        None,
        max_length=50,
        title="OCO 组ID"
    )
    list_client_order_id: Optional[str] = Field(
        None,
        max_length=50,
        title="OCO 客户端组ID"
    )

    # 杂项
    settle_ccy: Optional[str] = Field(
        None,
        max_length=10,
        title="结算货币"
    )
    recv_window: Annotated[int, BeforeValidator(_coerce_int)] = Field(
        5000,
        ge=1000,
        le=60000,
        title="时间戳窗口(ms)",
        description="允许的交易所时间与本地时间偏差"
    )

    # ---------- 序列化 ----------
    @field_serializer('good_till_date')
    def serialize_good_till_date(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return _to_utc(dt).isoformat()

    # ---------- 字段清洗 ----------
    @field_validator('symbol', 'margin_mode', 'settle_ccy', 'client_order_id',
                     'new_client_order_id', 'cancel_replace_original_order_id',
                     'strategy_tag', 'algo_id', 'order_list_id',
                     'list_client_order_id', 'trigger_direction')
    @classmethod
    def strip_control_chars(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            return strip_control_chars(v)
        return v

    @field_validator('margin_mode')
    @classmethod
    def lower_margin_mode(cls, v: str | None) -> str | None:
        return v.lower() if v else v

    # ---------- 跨字段逻辑校验 ----------
    @model_validator(mode='after')
    def validate_quantity_logic(self) -> Self:
        """数量和计价数量规则"""
        if self.quantity is not None and self.quote_order_qty is not None:
            raise ValueError('quantity 和 quote_order_qty 不能同时提供')
        # 对于需要数量的订单类型
        if self.order_type in (OrderType.LIMIT, OrderType.LIMIT_MAKER,
                               OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT,
                               OrderType.TRAILING_STOP_MARKET):
            if self.quantity is None:
                raise ValueError(f'{self.order_type.value} 订单必须提供 quantity')
        # 市价单至少需要一个
        if self.order_type in (OrderType.MARKET, OrderType.STOP_MARKET,
                               OrderType.TAKE_PROFIT_MARKET):
            if self.quantity is None and self.quote_order_qty is None:
                raise ValueError('市价单必须提供 quantity 或 quote_order_qty')
        return self

    @model_validator(mode='after')
    def validate_price_presence(self) -> Self:
        """需要价格的订单类型"""
        if self.order_type in (OrderType.LIMIT, OrderType.LIMIT_MAKER,
                               OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT):
            if self.price is None:
                raise ValueError(f'{self.order_type.value} 订单必须提供 price')
        return self

    @model_validator(mode='after')
    def validate_trigger_required(self) -> Self:
        """止损止盈类订单需要触发价"""
        if self.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT,
                               OrderType.TAKE_PROFIT, OrderType.TAKE_PROFIT_LIMIT,
                               OrderType.TAKE_PROFIT_MARKET, OrderType.TRAILING_STOP_MARKET):
            if not any([self.stop_price, self.trailing_delta,
                       self.trailing_stop_price, self.activation_price]):
                raise ValueError(f'{self.order_type.value} 必须提供触发价格/价差')
        return self

    @model_validator(mode='after')
    def validate_trailing_exclusions(self) -> Self:
        """追踪止损相关互斥"""
        if self.trailing_delta is not None and self.stop_price is not None:
            raise ValueError('trailing_delta 和 stop_price 不能同时提供')
        if self.trailing_delta is not None and self.trailing_stop_price is not None:
            raise ValueError('trailing_delta 和 trailing_stop_price 不能同时提供')
        if self.trailing_stop_price is not None and self.stop_price is not None:
            raise ValueError('trailing_stop_price 和 stop_price 不能同时提供')
        return self

    @model_validator(mode='after')
    def validate_iceberg(self) -> Self:
        """冰山订单校验"""
        if self.iceberg_qty is not None:
            if self.quantity is None:
                raise ValueError('冰山订单必须提供 quantity')
            if self.iceberg_qty > self.quantity:
                raise ValueError('iceberg_qty 不能大于 quantity')
        return self

    @model_validator(mode='after')
    def validate_time_in_force(self) -> Self:
        """有效期与订单类型兼容性"""
        if self.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
            if self.order_type not in (OrderType.LIMIT, OrderType.LIMIT_MAKER,
                                       OrderType.STOP_LIMIT):
                raise ValueError('IOC/FOK 仅适用于限价单')
        if self.time_in_force == TimeInForce.GTX:
            if self.order_type not in (OrderType.LIMIT, OrderType.LIMIT_MAKER):
                raise ValueError('GTX 仅适用于限价单')
            object.__setattr__(self, 'post_only', True)  # 自动设置
        return self

    @model_validator(mode='after')
    def validate_post_only(self) -> Self:
        """post_only 与 LIMIT_MAKER 强制绑定"""
        if self.order_type == OrderType.LIMIT_MAKER:
            object.__setattr__(self, 'post_only', True)
        # 市价单不允许 post_only
        if self.order_type in (OrderType.MARKET, OrderType.STOP_MARKET,
                               OrderType.TAKE_PROFIT_MARKET):
            if self.post_only:
                raise ValueError('市价单不能设置 post_only=True')
        return self

    @model_validator(mode='after')
    def validate_price_protect(self) -> Self:
        """price_protect 仅市价单"""
        if self.price_protect:
            if self.order_type not in (OrderType.MARKET, OrderType.STOP_MARKET,
                                       OrderType.TAKE_PROFIT_MARKET):
                raise ValueError('price_protect 仅适用于市价单')
        return self

    @model_validator(mode='after')
    def validate_oco(self) -> Self:
        """OCO 需要触发条件"""
        if self.contingency_type == ContingencyType.OCO:
            if not self.stop_price and not self.activation_price:
                raise ValueError('OCO 订单必须提供 stop_price 或 activation_price')
        return self

    @model_validator(mode='after')
    def validate_good_till_date(self) -> Self:
        """有效期不能在过去"""
        if self.good_till_date:
            now = _utc_now()
            gtd = _to_utc(self.good_till_date)
            if gtd <= now:
                raise ValueError('good_till_date 不能是过去时间')
        return self

    @field_validator('quantity', 'quote_order_qty', 'iceberg_qty')
    @classmethod
    def check_qty_precision(cls, v: Decimal | None) -> Decimal | None:
        if v is not None:
            if v.as_tuple().exponent < -8:
                raise ValueError('数量最多 8 位小数')
        return v

    @field_validator('price', 'stop_price', 'trailing_delta', 'trailing_stop_price',
                     'activation_price', 'callback_rate')
    @classmethod
    def check_price_precision(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v.as_tuple().exponent < -2:
            raise ValueError('价格最多 2 位小数')
        return v


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
class OrderResponse(BaseModel):
    """订单完整响应模型，包含执行细节、费用、状态等。"""
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        populate_by_name=True,
        use_enum_values=True,
        title="订单响应",
        json_schema_extra={
            "example": {
                "order_id": "123456",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "status": "FILLED",
                "quantity": "0.001",
                "executed_quantity": "0.001",
                "price": "50000.00",
                "avg_fill_price": "50000.00",
                "created_at": "2026-07-21T10:00:00Z"
            }
        }
    )

    order_id: str = Field(..., title="交易所订单ID")
    client_order_id: Optional[str] = Field(None, title="客户端订单ID")
    symbol: str = Field(..., title="交易对")
    exchange: Optional[str] = Field(None, title="交易所")

    side: OrderSide = Field(..., title="买卖方向")
    order_type: OrderType = Field(..., title="订单类型")
    status: OrderStatus = Field(..., title="订单状态")

    quantity: Decimal = Field(..., gt=0, title="原始数量")
    executed_quantity: Decimal = Field(Decimal('0'), ge=0, title="已成交数量")
    remaining_quantity: Decimal = Field(Decimal('0'), ge=0, title="剩余数量")
    quote_order_qty: Optional[Decimal] = Field(None, title="计价数量")
    cumulative_quote_qty: Optional[Decimal] = Field(None, title="累计计价成交量")

    price: Optional[Decimal] = Field(None, gt=0, title="限价价格")
    avg_fill_price: Optional[Decimal] = Field(None, gt=0, title="平均成交价")
    stop_price: Optional[Decimal] = Field(None, title="止损价")
    trailing_delta: Optional[Decimal] = Field(None, title="追踪价差")
    trailing_stop_price: Optional[Decimal] = Field(None, title="追踪止损触发价")
    activation_price: Optional[Decimal] = Field(None, title="激活价")
    callback_rate: Optional[Decimal] = Field(None, title="回调率")
    trailing_delta_type: Optional[TrailDeltaType] = Field(None, title="追踪价差类型")

    time_in_force: TimeInForce = Field(TimeInForce.GTC, title="有效期")
    is_working: bool = Field(True, title="是否活跃")
    reduce_only: bool = Field(False, title="仅减仓")
    post_only: bool = Field(False, title="仅做 Maker")
    close_on_trigger: bool = Field(False, title="触发后平仓")
    iceberg_qty: Optional[Decimal] = Field(None, title="冰山数量")
    self_trade_prevention_mode: SelfTradePreventionMode = Field(
        SelfTradePreventionMode.NONE, title="自成交防止"
    )
    margin_mode: Optional[str] = Field(None, title="保证金模式")
    position_idx: Optional[int] = Field(None, ge=0, le=2, title="持仓索引")
    leverage: Optional[Decimal] = Field(None, gt=0, title="订单级别杠杆")
    working_type: WorkingType = Field(WorkingType.CONTRACT, title="止损触发价格类型")
    trigger_direction: Optional[str] = Field(None, title="触发方向")
    contingency_type: Optional[ContingencyType] = Field(None, title="订单关系类型")
    order_list_id: Optional[str] = Field(None, title="OCO 组ID")
    list_client_order_id: Optional[str] = Field(None, title="OCO 客户端组ID")
    settle_ccy: Optional[str] = Field(None, title="结算货币")

    good_till_date: Optional[datetime] = Field(None, title="有效期截止时间 (UTC)")

    new_client_order_id: Optional[str] = Field(None, title="新客户端订单ID")
    cancel_replace_original_order_id: Optional[str] = Field(
        None, title="被替换的原客户端订单ID", alias="cancelReplaceOrigClientOrderId"
    )
    strategy_tag: Optional[str] = Field(None, title="策略标签")
    strategy_version: Optional[str] = Field(None, title="策略版本")
    algo_id: Optional[str] = Field(None, title="算法ID")

    created_at: Optional[datetime] = Field(None, title="创建时间 (UTC)")
    update_time: Optional[datetime] = Field(None, title="最后更新时间 (UTC)")
    last_trade_time: Optional[datetime] = Field(None, title="最后成交时间 (UTC)")

    commission: Optional[Decimal] = Field(None, title="手续费")
    commission_asset: Optional[str] = Field(None, title="手续费资产")
    realized_pnl: Optional[Decimal] = Field(None, title="已实现盈亏")

    trade_ids: List[str] = Field(default_factory=list, title="成交ID列表")

    error_message: Optional[str] = Field(None, title="错误信息")
    cancel_reason: Optional[str] = Field(None, title="取消原因")
    partial_fill_timeout_sec: Optional[int] = Field(None, title="部分成交超时(秒)")

    # ---------- 序列化 ----------
    @field_serializer('good_till_date', 'created_at', 'update_time', 'last_trade_time')
    def serialize_datetimes(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return _to_utc(dt).isoformat()

    # ---------- 校验 ----------
    @model_validator(mode='after')
    def validate_fill_price(self) -> Self:
        if self.executed_quantity > 0 and self.avg_fill_price is None:
            raise ValueError('已成交数量>0 时必须提供 avg_fill_price')
        return self

    @model_validator(mode='after')
    def validate_trade_ids(self) -> Self:
        if self.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            if not self.trade_ids:
                raise ValueError('成交或部分成交状态必须提供 trade_ids')
        return self

    @field_validator('quantity', 'executed_quantity', 'quote_order_qty', 'iceberg_qty')
    @classmethod
    def check_qty_precision(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v.as_tuple().exponent < -8:
            raise ValueError('数量最多 8 位小数')
        return v

    @field_validator('price', 'avg_fill_price', 'stop_price', 'trailing_delta',
                     'trailing_stop_price', 'activation_price', 'callback_rate')
    @classmethod
    def check_price_precision(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v.as_tuple().exponent < -2:
            raise ValueError('价格最多 2 位小数')
        return v


# ---------------------------------------------------------------------------
# 辅助模型
# ---------------------------------------------------------------------------
class OrderListResponse(BaseModel):
    """分页订单列表"""
    model_config = ConfigDict(extra="forbid", title="订单列表")
    orders: List[OrderResponse]
    total: int = 0
    page: int = 1
    page_size: int = 50
    total_pages: int = 0


class CancelOrderRequest(BaseModel):
    """取消订单请求"""
    model_config = ConfigDict(extra="forbid", frozen=True, title="取消订单请求")
    order_id: Optional[str] = Field(None, title="交易所订单ID")
    client_order_id: Optional[str] = Field(None, title="客户端订单ID")
    symbol: Optional[str] = Field(None, title="交易对")

    @model_validator(mode='after')
    def check_id(self) -> Self:
        if not self.order_id and not self.client_order_id:
            raise ValueError('必须提供 order_id 或 client_order_id')
        return self


class CancelOrderResponse(BaseModel):
    """取消订单响应"""
    model_config = ConfigDict(extra="forbid", title="取消订单响应")
    success: bool = True
    message: str = ""
    results: List[OrderResponse] = Field(default_factory=list, title="被取消的订单")


class OrderBatchRequest(BaseModel):
    """批量下单请求"""
    model_config = ConfigDict(extra="forbid", title="批量下单请求")
    batch_id: Optional[str] = Field(None, title="批次ID")
    orders: List[OrderRequest] = Field(..., min_length=1, max_length=20)


class OrderBatchEntry(BaseModel):
    """批量响应条目"""
    model_config = ConfigDict(extra="forbid")
    request_index: int = Field(..., title="请求索引 (从0开始)")
    response: Optional[OrderResponse] = None
    error: Optional[str] = None


class OrderBatchResponse(BaseModel):
    """批量下单响应"""
    model_config = ConfigDict(extra="forbid", title="批量下单响应")
    batch_id: Optional[str] = None
    success: bool = True
    results: List[OrderBatchEntry] = Field(default_factory=list)
    error_count: int = 0


class OrderErrorResponse(BaseModel):
    """统一错误响应"""
    error_code: str
    message: str
    details: Optional[str] = None


__all__ = [
    "OrderType",
    "OrderSide",
    "OrderStatus",
    "TimeInForce",
    "SelfTradePreventionMode",
    "WorkingType",
    "ContingencyType",
    "TrailDeltaType",
    "OrderRequest",
    "OrderResponse",
    "OrderListResponse",
    "CancelOrderRequest",
    "CancelOrderResponse",
    "OrderBatchRequest",
    "OrderBatchEntry",
    "OrderBatchResponse",
    "OrderErrorResponse",
  ]
