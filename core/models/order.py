# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: order.py
核心职责: 定义订单相关数据模型，管理从请求到成交的全生命周期。
所属层级: core.models

设计说明:
    - 所有金额/数量使用 Decimal 确保精确计算。
    - 数据类实例非线程安全，仅限策略引擎单线程主循环使用。
    - 时间戳统一为 UTC 浮点秒 (time.time()) 或 datetime.timestamp()。

外部依赖:
    - dataclasses, enum, typing, uuid, datetime, decimal, re

接口契约:
    提供: OrderSide, OrderType, OrderStatus, TimeInForce, MarginMode, WorkingType,
          ContingencyType, OrderSource, Fill, OrderHistoryEntry, OrderRequest,
          OrderConfirmation, CancelRequest, ModifyRequest, OrderBatchRequest, OrderBatchResponse, Order
    消费: 无

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2026-07-07 v31.0: 最终审查，完善序列化、校验、状态机、并发文档。
"""

import re
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Dict, Any, List, Union
from datetime import datetime, timezone

__all__ = [
    'OrderSide', 'OrderType', 'OrderStatus', 'TimeInForce', 'MarginMode',
    'WorkingType', 'ContingencyType', 'OrderSource',
    'Fill', 'OrderHistoryEntry',
    'OrderRequest', 'OrderConfirmation',
    'CancelRequest', 'ModifyRequest',
    'OrderBatchRequest', 'OrderBatchResponse',
    'Order',
]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _utc_now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _safe_decimal(value: Union[str, float, int, Decimal]) -> Decimal:
    """将输入安全转换为 Decimal，NaN/Inf 抛出异常。"""
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise ValueError("Decimal cannot be NaN or Inf")
        return value
    d = Decimal(str(value))
    if d.is_nan() or d.is_infinite():
        raise ValueError("Decimal cannot be NaN or Inf")
    return d


# =============================================================================
# 枚举定义
# =============================================================================

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    OCO = "OCO"
    TWAP = "TWAP"
    ICEBERG = "ICEBERG"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PARTIALLY_CANCELLED = "PARTIALLY_CANCELLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    DEACTIVATED = "DEACTIVATED"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTD = "GTD"


class MarginMode(str, Enum):
    ISOLATED = "ISOLATED"
    CROSSED = "CROSSED"


class WorkingType(str, Enum):
    MARK_PRICE = "MARK_PRICE"
    CONTRACT_PRICE = "CONTRACT_PRICE"


class ContingencyType(str, Enum):
    OCO = "OCO"
    OTO = "OTO"
    NORMAL = "NORMAL"


class OrderSource(str, Enum):
    MANUAL = "MANUAL"
    STRATEGY = "STRATEGY"
    SYSTEM = "SYSTEM"


# =============================================================================
# 成交与历史数据类
# =============================================================================

@dataclass
class Fill:
    """单笔成交明细"""
    trade_id: str
    price: Decimal = field(default_factory=lambda: Decimal('0'))
    qty: Decimal = field(default_factory=lambda: Decimal('0'))
    commission: Decimal = field(default_factory=lambda: Decimal('0'))
    commission_asset: str = ""
    timestamp: float = field(default_factory=_utc_now_ts)

    def __post_init__(self):
        self.price = _safe_decimal(self.price)
        self.qty = _safe_decimal(self.qty)
        self.commission = _safe_decimal(self.commission)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'trade_id': self.trade_id,
            'price': str(self.price),
            'qty': str(self.qty),
            'commission': str(self.commission),
            'commission_asset': self.commission_asset,
            'timestamp': self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Fill':
        return cls(
            trade_id=data['trade_id'],
            price=_safe_decimal(data.get('price', '0')),
            qty=_safe_decimal(data.get('qty', '0')),
            commission=_safe_decimal(data.get('commission', '0')),
            commission_asset=data.get('commission_asset', ''),
            timestamp=data.get('timestamp', _utc_now_ts()),
        )

    def __eq__(self, other):
        return isinstance(other, Fill) and self.trade_id == other.trade_id

    def __hash__(self):
        return hash(self.trade_id)


@dataclass
class OrderHistoryEntry:
    """订单状态变更记录"""
    timestamp: float = field(default_factory=_utc_now_ts)
    from_status: OrderStatus = OrderStatus.NEW
    to_status: OrderStatus = OrderStatus.NEW
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp,
            'from': self.from_status.value,
            'to': self.to_status.value,
            'details': self._serialize_details(self.details),
        }

    @staticmethod
    def _serialize_details(d: Dict) -> Dict:
        """递归将 Decimal 转为字符串"""
        result = {}
        for k, v in d.items():
            if isinstance(v, Decimal):
                result[k] = str(v)
            elif isinstance(v, dict):
                result[k] = OrderHistoryEntry._serialize_details(v)
            else:
                result[k] = v
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OrderHistoryEntry':
        return cls(
            timestamp=data.get('timestamp', _utc_now_ts()),
            from_status=OrderStatus(data['from']),
            to_status=OrderStatus(data['to']),
            details=data.get('details', {}),
        )

    def __eq__(self, other):
        return (isinstance(other, OrderHistoryEntry) and
                self.timestamp == other.timestamp and
                self.from_status == other.from_status and
                self.to_status == other.to_status)


# =============================================================================
# OrderRequest
# =============================================================================

@dataclass
class OrderRequest:
    """提交订单的请求参数，包含完整校验。"""
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Optional[Decimal] = None
    quote_order_qty: Optional[Decimal] = None
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    post_only: bool = False
    client_order_id: Optional[str] = None
    leverage: int = 1
    margin_mode: MarginMode = MarginMode.ISOLATED
    working_type: WorkingType = WorkingType.CONTRACT_PRICE
    position_side: Optional[str] = None
    activation_price: Optional[Decimal] = None
    trailing_delta: Optional[Decimal] = None
    iceberg_qty: Optional[Decimal] = None
    contingency_type: ContingencyType = ContingencyType.NORMAL
    parent_order_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 交易规则（不发送到交易所）
    min_qty: Optional[Decimal] = None
    step_size: Optional[Decimal] = None
    min_notional: Optional[Decimal] = None

    def __post_init__(self):
        # 生成 client_order_id
        if not self.client_order_id:
            self.client_order_id = f"khaos_{uuid.uuid4().hex[:16]}_{int(_utc_now_ts()*1000)}"
        # 基本校验
        if not re.match(r'^[A-Z0-9]{6,12}$', self.symbol):
            raise ValueError(f"Invalid symbol: {self.symbol}")
        # 数量与金额：若同时提供，以 quantity 为准，忽略 quote_order_qty
        if self.quantity is not None and self.quote_order_qty is not None:
            self.quote_order_qty = None
        if self.quantity is None and self.quote_order_qty is None:
            raise ValueError("Either quantity or quote_order_qty must be provided")
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.quote_order_qty is not None and self.quote_order_qty <= 0:
            raise ValueError("quote_order_qty must be positive")
        # 价格逻辑
        requires_price = (self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT))
        if requires_price and (self.price is None or self.price <= 0):
            raise ValueError(f"{self.order_type.value} requires a positive price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.STOP_LOSS):
            if self.stop_price is None or self.stop_price <= 0:
                raise ValueError(f"{self.order_type.value} requires a positive stop_price")
        # 市价单 price 可为 0 或 None，强制设为 0
        if self.order_type == OrderType.MARKET and self.price is None:
            self.price = Decimal('0')
        # 对齐 step_size
        if self.quantity is not None and self.min_qty is not None and self.step_size is not None:
            if self.quantity < self.min_qty:
                raise ValueError(f"quantity {self.quantity} below min_qty {self.min_qty}")
            steps = (self.quantity / self.step_size).to_integral_value(ROUND_DOWN)
            aligned_qty = self.step_size * steps
            if aligned_qty != self.quantity:
                # 自动调整而非抛出异常
                self.quantity = aligned_qty
        # 名义价值检查
        if self.min_notional is not None:
            if self.price and self.quantity:
                if self.price * self.quantity < self.min_notional:
                    raise ValueError(f"Notional {self.price*self.quantity} < min {self.min_notional}")
            elif self.quote_order_qty and self.quote_order_qty < self.min_notional:
                raise ValueError(f"Quote order qty {self.quote_order_qty} < min notional {self.min_notional}")
        # post_only 只能与 GTC/GTD 配合
        if self.post_only and self.time_in_force not in (TimeInForce.GTC, TimeInForce.GTD):
            raise ValueError("post_only requires GTC or GTD")
        # 清理 tag 类字符（防注入）
        self.metadata = {k: v for k, v in self.metadata.items() if isinstance(k, str) and len(k) < 50}
        # Decimal NaN 检查
        for field_name in ('quantity', 'quote_order_qty', 'price', 'stop_price', 'activation_price', 'trailing_delta', 'iceberg_qty'):
            val = getattr(self, field_name)
            if val is not None:
                try:
                    setattr(self, field_name, _safe_decimal(val))
                except InvalidOperation:
                    raise ValueError(f"{field_name} is not a valid decimal")

    def to_dict(self) -> Dict[str, Any]:
        d = {
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'time_in_force': self.time_in_force.value,
            'reduce_only': self.reduce_only,
            'post_only': self.post_only,
        }
        if self.quantity is not None:
            d['quantity'] = str(self.quantity)
        if self.quote_order_qty is not None:
            d['quote_order_qty'] = str(self.quote_order_qty)
        if self.price is not None and self.order_type != OrderType.MARKET:
            d['price'] = str(self.price)
        if self.stop_price is not None:
            d['stop_price'] = str(self.stop_price)
        if self.leverage != 1:
            d['leverage'] = self.leverage
        d['margin_mode'] = self.margin_mode.value
        d['working_type'] = self.working_type.value
        if self.position_side:
            d['position_side'] = self.position_side
        if self.activation_price:
            d['activation_price'] = str(self.activation_price)
        if self.trailing_delta:
            d['trailing_delta'] = str(self.trailing_delta)
        if self.iceberg_qty:
            d['iceberg_qty'] = str(self.iceberg_qty)
        if self.contingency_type != ContingencyType.NORMAL:
            d['contingency_type'] = self.contingency_type.value
        if self.parent_order_id:
            d['parent_order_id'] = self.parent_order_id
        d['client_order_id'] = self.client_order_id
        # metadata 不包含交易规则
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OrderRequest':
        def _d(val):
            return _safe_decimal(val) if val is not None else None
        return cls(
            symbol=data['symbol'],
            side=OrderSide(data['side']),
            order_type=OrderType(data['order_type']),
            quantity=_d(data.get('quantity')),
            quote_order_qty=_d(data.get('quote_order_qty')),
            price=_d(data.get('price')),
            stop_price=_d(data.get('stop_price')),
            time_in_force=TimeInForce(data.get('time_in_force', 'GTC')),
            reduce_only=data.get('reduce_only', False),
            post_only=data.get('post_only', False),
            client_order_id=data.get('client_order_id'),
            leverage=data.get('leverage', 1),
            margin_mode=MarginMode(data.get('margin_mode', 'ISOLATED')),
            working_type=WorkingType(data.get('working_type', 'CONTRACT_PRICE')),
            position_side=data.get('position_side'),
            activation_price=_d(data.get('activation_price')),
            trailing_delta=_d(data.get('trailing_delta')),
            iceberg_qty=_d(data.get('iceberg_qty')),
            contingency_type=ContingencyType(data.get('contingency_type', 'NORMAL')),
            parent_order_id=data.get('parent_order_id'),
            metadata=data.get('metadata', {}),
        )

    def __repr__(self) -> str:
        return f"OrderRequest({self.side.value} {self.quantity or self.quote_order_qty} {self.symbol} {self.order_type.value})"


# =============================================================================
# CancelRequest / ModifyRequest
# =============================================================================

@dataclass
class CancelRequest:
    order_id: str
    symbol: str
    client_order_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'client_order_id': self.client_order_id,
        }


@dataclass
class ModifyRequest:
    order_id: str
    symbol: str
    price: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    client_order_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            'order_id': self.order_id,
            'symbol': self.symbol,
        }
        if self.price is not None:
            d['price'] = str(self.price)
        if self.quantity is not None:
            d['quantity'] = str(self.quantity)
        if self.stop_price is not None:
            d['stop_price'] = str(self.stop_price)
        if self.client_order_id:
            d['client_order_id'] = self.client_order_id
        return d


# =============================================================================
# 批量订单模型
# =============================================================================

@dataclass
class OrderBatchRequest:
    orders: List[OrderRequest] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {'orders': [o.to_dict() for o in self.orders]}


@dataclass
class OrderBatchResponse:
    confirmations: List[OrderConfirmation] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)


# =============================================================================
# OrderConfirmation
# =============================================================================

@dataclass
class OrderConfirmation:
    """交易所返回的订单确认/成交信息。"""
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    price: Decimal = Decimal('0')
    avg_fill_price: Optional[Decimal] = None
    filled_qty: Decimal = Decimal('0')
    total_qty: Decimal = Decimal('0')
    quote_filled_qty: Decimal = Decimal('0')
    commission: Decimal = Decimal('0')
    commission_asset: str = ""
    created_at: float = field(default_factory=_utc_now_ts)
    updated_at: float = field(default_factory=_utc_now_ts)
    request_hash: Optional[str] = None
    position_side: Optional[str] = None
    working_type: WorkingType = WorkingType.CONTRACT_PRICE
    fills: List[Fill] = field(default_factory=list)
    raw_status: str = ""                     # 交易所原始状态字串

    def __post_init__(self):
        self.price = _safe_decimal(self.price)
        self.filled_qty = _safe_decimal(self.filled_qty)
        self.total_qty = _safe_decimal(self.total_qty)
        self.quote_filled_qty = _safe_decimal(self.quote_filled_qty)
        self.commission = _safe_decimal(self.commission)
        if self.avg_fill_price is not None:
            self.avg_fill_price = _safe_decimal(self.avg_fill_price)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'order_id': self.order_id,
            'client_order_id': self.client_order_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'status': self.status.value,
            'price': str(self.price),
            'avg_fill_price': str(self.avg_fill_price) if self.avg_fill_price is not None else None,
            'filled_qty': str(self.filled_qty),
            'total_qty': str(self.total_qty),
            'quote_filled_qty': str(self.quote_filled_qty),
            'commission': str(self.commission),
            'commission_asset': self.commission_asset,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'position_side': self.position_side,
            'working_type': self.working_type.value,
            'fills': [f.to_dict() for f in self.fills],
            'raw_status': self.raw_status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OrderConfirmation':
        def _d(v):
            return _safe_decimal(v) if v is not None else None
        fills = [Fill.from_dict(f) for f in data.get('fills', [])]
        return cls(
            order_id=data['order_id'],
            client_order_id=data.get('client_order_id', ''),
            symbol=data.get('symbol', ''),
            side=OrderSide(data['side']),
            order_type=OrderType(data['order_type']),
            status=OrderStatus(data['status']),
            price=_d(data.get('price', '0')),
            avg_fill_price=_d(data.get('avg_fill_price')),
            filled_qty=_d(data.get('filled_qty', '0')),
            total_qty=_d(data.get('total_qty', '0')),
            quote_filled_qty=_d(data.get('quote_filled_qty', '0')),
            commission=_d(data.get('commission', '0')),
            commission_asset=data.get('commission_asset', ''),
            created_at=data.get('created_at', _utc_now_ts()),
            updated_at=data.get('updated_at', _utc_now_ts()),
            request_hash=data.get('request_hash'),
            position_side=data.get('position_side'),
            working_type=WorkingType(data.get('working_type', 'CONTRACT_PRICE')),
            fills=fills,
            raw_status=data.get('raw_status', ''),
        )

    def to_safe_dict(self) -> Dict[str, Any]:
        d = self.to_dict()
        d.pop('client_order_id', None)
        d.pop('request_hash', None)
        return d

    def __repr__(self) -> str:
        return f"OrderConfirmation({self.order_id} {self.side.value} {self.status.value})"


# =============================================================================
# Order 内部订单实体
# =============================================================================

VALID_TRANSITIONS = {
    OrderStatus.NEW: {OrderStatus.PENDING, OrderStatus.REJECTED, OrderStatus.DEACTIVATED},
    OrderStatus.PENDING: {OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.DEACTIVATED},
    OrderStatus.ACKNOWLEDGED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.PENDING_CANCEL, OrderStatus.DEACTIVATED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.PARTIALLY_CANCELLED, OrderStatus.PENDING_CANCEL},
    OrderStatus.PENDING_CANCEL: {OrderStatus.CANCELLED, OrderStatus.PARTIALLY_CANCELLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED},
    OrderStatus.PARTIALLY_CANCELLED: set(),
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
    OrderStatus.DEACTIVATED: set(),
}


@dataclass
class Order:
    """
    内部订单实体，非线程安全，仅限策略引擎单线程主循环使用。
    """
    _client_order_id: str = field(default_factory=lambda: f"khaos_{uuid.uuid4().hex}")
    order_id: Optional[str] = None
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.LIMIT
    quantity: Decimal = Decimal('0')
    quote_order_qty: Optional[Decimal] = None
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    activation_price: Optional[Decimal] = None
    trailing_delta: Optional[Decimal] = None
    iceberg_qty: Optional[Decimal] = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: Decimal = Decimal('0')
    quote_filled_qty: Decimal = Decimal('0')
    avg_fill_price: Optional[Decimal] = None
    last_fill_price: Optional[Decimal] = None
    commission: Decimal = Decimal('0')
    commission_asset: str = ""
    created_at: float = field(default_factory=_utc_now_ts)
    updated_at: float = field(default_factory=_utc_now_ts)
    filled_at: Optional[float] = None
    expire_time: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    post_only: bool = False
    leverage: int = 1
    margin_mode: MarginMode = MarginMode.ISOLATED
    working_type: WorkingType = WorkingType.CONTRACT_PRICE
    position_side: Optional[str] = None
    contingency_type: ContingencyType = ContingencyType.NORMAL
    parent_order_id: Optional[str] = None
    source: OrderSource = OrderSource.STRATEGY
    tag: str = ""
    strategy_id: str = ""
    updated_by: str = ""
    error_message: Optional[str] = None
    cancel_reason: Optional[str] = None
    status_reason: Optional[str] = None
    history: List[OrderHistoryEntry] = field(default_factory=list)
    fills: List[Fill] = field(default_factory=list)

    @property
    def client_order_id(self) -> str:
        return self._client_order_id

    def __post_init__(self):
        # 长度限制
        if len(self.tag) > 50:
            raise ValueError("tag too long (max 50)")
        if len(self.strategy_id) > 50:
            raise ValueError("strategy_id too long (max 50)")
        # 初始化数值
        self.quantity = _safe_decimal(self.quantity)
        self.filled_qty = _safe_decimal(self.filled_qty)
        self.quote_filled_qty = _safe_decimal(self.quote_filled_qty)
        self.commission = _safe_decimal(self.commission)
        # 历史限制
        if len(self.history) > 100:
            self.history = self.history[-100:]

    # ------------------------ 状态更新 ------------------------
    def update_status(self, new_status: OrderStatus, **kwargs) -> None:
        """更新订单状态，校验转换合法性。"""
        if new_status == self.status:
            return
        if new_status not in VALID_TRANSITIONS.get(self.status, set()):
            raise ValueError(f"Invalid state transition from {self.status.value} to {new_status.value}")
        old_status = self.status
        self.status = new_status
        self.updated_at = _utc_now_ts()
        self.history.append(OrderHistoryEntry(
            timestamp=self.updated_at,
            from_status=old_status,
            to_status=new_status,
            details=kwargs,
        ))
        if len(self.history) > 100:
            self.history = self.history[-100:]
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

    # ------------------------ 成交应用 ------------------------
    def apply_fill(self, fill: Fill) -> None:
        """应用成交，自动更新数量和均价，处理竞态。"""
        # 允许在 PENDING_CANCEL 或 PARTIALLY_FILLED 等状态成交
        if self.status not in (OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL):
            raise ValueError(f"Cannot apply fill in state {self.status.value}")
        self.fills.append(fill)
        self.filled_qty += fill.qty
        if self.filled_qty > self.quantity:
            self.filled_qty = self.quantity  # 钳位，忽略超量
        self.quote_filled_qty += fill.price * fill.qty
        self.last_fill_price = fill.price
        if self.filled_qty > 0:
            total_cost = sum(f.price * f.qty for f in self.fills)
            self.avg_fill_price = (total_cost / self.filled_qty).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
        else:
            self.avg_fill_price = None
        self.commission += fill.commission
        if self.filled_qty >= self.quantity:
            self.update_status(OrderStatus.FILLED, filled_at=_utc_now_ts())
        else:
            self.update_status(OrderStatus.PARTIALLY_FILLED)
        self.updated_at = _utc_now_ts()

    # ------------------------ 属性 ------------------------
    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.ACKNOWLEDGED,
                               OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL)

    @property
    def is_open(self) -> bool:
        """可接受成交（不包括 PENDING_CANCEL）。"""
        return self.status in (OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING)

    @property
    def is_final(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.PARTIALLY_CANCELLED,
                               OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.DEACTIVATED)

    @property
    def remaining_qty(self) -> Decimal:
        return max(Decimal('0'), self.quantity - self.filled_qty)

    @property
    def is_reduce_only(self) -> bool:
        return self.reduce_only

    @property
    def notional(self) -> Decimal:
        if self.filled_qty > 0 and self.avg_fill_price:
            return self.avg_fill_price * self.filled_qty
        if self.price and self.quantity:
            return self.price * self.quantity
        return Decimal('0')

    # ------------------------ 构造与序列化 ------------------------
    @classmethod
    def from_request(cls, request: OrderRequest, source: OrderSource = OrderSource.STRATEGY,
                     tag: str = "", strategy_id: str = "") -> 'Order':
        return cls(
            _client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity or Decimal('0'),
            quote_order_qty=request.quote_order_qty,
            price=request.price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
            reduce_only=request.reduce_only,
            post_only=request.post_only,
            leverage=request.leverage,
            margin_mode=request.margin_mode,
            working_type=request.working_type,
            position_side=request.position_side,
            activation_price=request.activation_price,
            trailing_delta=request.trailing_delta,
            iceberg_qty=request.iceberg_qty,
            contingency_type=request.contingency_type,
            parent_order_id=request.parent_order_id,
            source=source,
            tag=tag,
            strategy_id=strategy_id,
            metadata=request.metadata,  # 修复：传递 metadata
        )

    def to_dict(self, safe: bool = False, include_details: bool = True) -> Dict[str, Any]:
        d = {
            'client_order_id': self.client_order_id,
            'order_id': self.order_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'status': self.status.value,
            'quantity': str(self.quantity),
            'filled_qty': str(self.filled_qty),
            'remaining_qty': str(self.remaining_qty),
            'avg_fill_price': str(self.avg_fill_price) if self.avg_fill_price is not None else None,
            'commission': str(self.commission),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'reduce_only': self.reduce_only,
            'leverage': self.leverage,
        }
        if include_details:
            d['fills'] = [f.to_dict() for f in self.fills]
            d['history'] = [h.to_dict() for h in self.history]
        if safe:
            d.pop('strategy_id', None)
            d.pop('tag', None)
            d.pop('fills', None)
            d.pop('history', None)
        else:
            d['strategy_id'] = self.strategy_id
            d['tag'] = self.tag
        return d

    def copy(self) -> 'Order':
        """创建深拷贝（数据层面）。"""
        import copy
        return copy.deepcopy(self)

    def __repr__(self) -> str:
        return (f"Order({self.client_order_id[:8]} {self.side.value} {self.status.value} "
                f"qty:{self.quantity}/{self.filled_qty})")

    def __hash__(self):
        return hash(self.client_order_id)

    def __eq__(self, other):
        return isinstance(other, Order) and self.client_order_id == other.client_order_id
