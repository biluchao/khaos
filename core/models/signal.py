# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: signal.py
核心职责: 定义交易信号数据模型，承载策略决策的所有信息。
所属层级: core.models

外部依赖:
    - dataclasses, enum, typing, uuid, datetime, decimal, hashlib, hmac, json

接口契约:
    提供: SignalType, SignalDirection, SignalPriority, SignalStatus,
          Signal, SignalBatch, SignalConfirmation
    消费: 无 (避免循环导入，execution_type 使用字符串对应 OrderType)

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2026-07-07 v34.0: 最终审查，修复序列化、Decimal 精度、布尔解析、状态时间戳等。
"""

import json
import uuid
import hashlib
import hmac
from dataclasses import dataclass, field, asdict
from enum import Enum, IntEnum
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List, Union, Tuple
from datetime import datetime, timezone


# =============================================================================
# 辅助函数
# =============================================================================
def _utc_now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _safe_decimal(value: Union[str, float, int, Decimal, None], allow_none: bool = False) -> Optional[Decimal]:
    """安全转换为 Decimal，并检查 NaN/Inf。若 allow_none 且 value 为 None 返回 None。"""
    if allow_none and value is None:
        return None
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise ValueError("Decimal cannot be NaN or Inf")
        return value
    if value is None:
        raise ValueError("Value is None but not allowed")
    # 处理空字符串或空白
    if isinstance(value, str) and value.strip() == '':
        if allow_none:
            return None
        return Decimal('0')
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Cannot convert '{value}' to Decimal")
    if d.is_nan() or d.is_infinite():
        raise ValueError("Decimal cannot be NaN or Inf")
    return d


def _parse_bool(val: Any, default: bool = False) -> bool:
    """安全解析布尔值。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes')
    if isinstance(val, (int, float)):
        return val != 0
    return default


def _serialize_value(val: Any) -> Any:
    """递归将不可 JSON 序列化对象转为安全类型（Decimal->str, datetime->iso）。"""
    if isinstance(val, Decimal):
        return str(val)
    elif isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [_serialize_value(v) for v in val]
    elif isinstance(val, (datetime,)):
        return val.isoformat()
    elif isinstance(val, (int, float, str, bool, type(None))):
        return val
    else:
        return str(val)  # 未知类型转字符串


# =============================================================================
# 枚举定义
# =============================================================================

class SignalType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    REDUCE = "REDUCE"
    ADD = "ADD"
    MODIFY = "MODIFY"
    NO_ACTION = "NO_ACTION"


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class SignalPriority(IntEnum):
    PANIC_CLOSE = 0
    HARD_STOP = 1
    ESCAPE_CLOSE = 2
    ESCAPE_REDUCE = 3
    RECAPTURE_ENTRY = 4
    CALLBACK_DROP = 5
    NORMAL_ENTRY = 6
    NORMAL_ADD = 7
    RANGE_TRADE = 8
    LOW_PRIORITY = 9


class SignalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    PARTIALLY_EXECUTED = "PARTIALLY_EXECUTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    DEACTIVATED = "DEACTIVATED"          # 止损/止盈未触发即被取消


# =============================================================================
# 默认过期时间（秒）
# =============================================================================
DEFAULT_EXPIRY_MAP = {
    SignalType.ENTRY: 300,
    SignalType.EXIT: 60,
    SignalType.REDUCE: 120,
    SignalType.ADD: 300,
    SignalType.MODIFY: 300,
    SignalType.NO_ACTION: 0,
}


# =============================================================================
# Signal 数据类
# =============================================================================

@dataclass
class Signal:
    """交易信号实体。非线程安全，建议策略引擎单线程使用。"""
    SIGNAL_VERSION = 4

    # 标识
    signal_id: str = field(default_factory=lambda: f"sig_{uuid.uuid4().hex}")
    idempotency_key: str = field(default_factory=lambda: f"idem_{uuid.uuid4().hex}")

    # 交易品种与方向
    symbol: str = ""
    direction: SignalDirection = SignalDirection.NEUTRAL
    signal_type: SignalType = SignalType.NO_ACTION
    position_side: Optional[str] = None          # LONG / SHORT (合约)

    # 优先级与状态
    priority: SignalPriority = SignalPriority.NORMAL_ENTRY
    status: SignalStatus = SignalStatus.PENDING
    status_updated_at: float = field(default_factory=_utc_now_ts)

    # 仓位控制 (Decimal)
    quantity_ratio: Decimal = Decimal('0')        # 相对基础仓位比例 (0~1)
    target_quantity: Optional[Decimal] = None     # 绝对目标数量（可选）
    min_execution_qty: Optional[Decimal] = None
    min_qty: Optional[Decimal] = None
    step_size: Optional[Decimal] = None

    # 价格参数 (Decimal)
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    take_profit_price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    trailing_stop_atr: Optional[Decimal] = None

    # 执行控制
    execution_type: str = "LIMIT"                  # 对应 OrderType 字符串
    max_slippage_pct: float = 0.1
    max_execution_delay_sec: float = 5.0
    is_urgent: bool = False
    allow_algo: bool = True
    allow_hedge: bool = False

    # 风险相关
    risk_budget_used: float = 0.0
    max_loss_pct: float = 1.0
    risk_checked: bool = False
    risk_passed: bool = False
    compliance_checked: bool = False
    leverage_impact: float = 0.0
    margin_impact_pct: float = 0.0
    expected_pnl: Optional[Decimal] = None
    estimated_slippage: Decimal = Decimal('0')
    total_cost_estimate: Decimal = Decimal('0')

    # 置信度
    confidence: Decimal = Decimal('0')

    # 时间与有效期
    timestamp: float = field(default_factory=_utc_now_ts)
    expires_at: Optional[float] = None
    cooldown_sec: float = 0.0
    max_hold_bars: int = 0            # 0 表示不限
    execute_after: Optional[float] = None
    kline_close_time: Optional[int] = None

    # 来源与关联
    strategy_id: str = ""
    strategy_version: str = ""
    source_module: str = ""
    parent_signal_id: Optional[str] = None
    order_id: Optional[str] = None
    exchange: str = ""
    created_by: str = "system"
    is_manual: bool = False
    is_composite: bool = False
    is_simulation: bool = False

    # 审计与追踪
    reason: str = ""
    tags: List[str] = field(default_factory=list)
    data_hash: Optional[str] = None
    snapshot: Dict[str, Any] = field(default_factory=dict)
    reject_reason: Optional[str] = None
    conflicts_with: List[str] = field(default_factory=list)
    signature: Optional[str] = None

    # 扩展元数据
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # --- 基本校验 ---
        if not self.symbol:
            raise ValueError("symbol cannot be empty")
        if self.signal_type == SignalType.NO_ACTION:
            self.quantity_ratio = Decimal('0')
        if self.signal_type == SignalType.ENTRY and self.direction == SignalDirection.NEUTRAL:
            raise ValueError("ENTRY signal must have LONG or SHORT direction")
        # position_side 校验
        if self.position_side is not None and self.position_side not in ('LONG', 'SHORT'):
            raise ValueError("position_side must be 'LONG', 'SHORT' or None")

        # --- Decimal 转换与校验 ---
        self.quantity_ratio = _safe_decimal(self.quantity_ratio)
        if self.quantity_ratio < 0:
            raise ValueError("quantity_ratio must be >= 0")
        if self.quantity_ratio > 1.0:
            self.quantity_ratio = Decimal('1')
        self.confidence = _safe_decimal(self.confidence)
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("confidence must be between 0 and 1")

        # 可选 Decimal 字段
        for field_name in ('limit_price', 'stop_price', 'take_profit_price', 'trigger_price',
                           'min_execution_qty', 'min_qty', 'step_size', 'expected_pnl',
                           'trailing_stop_atr', 'target_quantity'):
            val = getattr(self, field_name)
            if val is not None:
                setattr(self, field_name, _safe_decimal(val, allow_none=False))

        # 非可选 Decimal
        self.estimated_slippage = _safe_decimal(self.estimated_slippage)
        self.total_cost_estimate = _safe_decimal(self.total_cost_estimate)
        if self.estimated_slippage < 0 or self.total_cost_estimate < 0:
            raise ValueError("estimated_slippage and total_cost_estimate must be >= 0")

        # Float 范围校验
        if not 0 <= self.max_slippage_pct <= 1:
            raise ValueError("max_slippage_pct must be 0-1")
        if not 0 <= self.max_loss_pct <= 1:
            raise ValueError("max_loss_pct must be 0-1")
        if not 0 <= self.risk_budget_used <= 1:
            raise ValueError("risk_budget_used must be 0-1")
        if not 0 <= self.margin_impact_pct <= 100:
            raise ValueError("margin_impact_pct must be 0-100")
        if not 0 <= self.max_execution_delay_sec <= 300:
            raise ValueError("max_execution_delay_sec must be 0-300")
        if self.leverage_impact < 0:
            raise ValueError("leverage_impact must be >= 0")
        if self.cooldown_sec < 0 or self.max_hold_bars < 0:
            raise ValueError("cooldown_sec and max_hold_bars must be >= 0")

        # tags 去重
        self.tags = list(dict.fromkeys(self.tags))

        # 默认过期时间
        if self.expires_at is None:
            duration = DEFAULT_EXPIRY_MAP.get(self.signal_type, 300)
            self.expires_at = self.timestamp + duration

    # ------------------------ 状态转换方法 ------------------------
    def _update_status(self, new_status: SignalStatus) -> None:
        self.status = new_status
        self.status_updated_at = _utc_now_ts()

    def approve(self) -> None:
        if self.status != SignalStatus.PENDING:
            raise ValueError(f"Cannot approve from {self.status.value}")
        self._update_status(SignalStatus.APPROVED)

    def reject(self, reason: str) -> None:
        if self.status not in (SignalStatus.PENDING, SignalStatus.APPROVED):
            raise ValueError(f"Cannot reject from {self.status.value}")
        self._update_status(SignalStatus.REJECTED)
        self.reject_reason = reason

    def cancel(self, reason: str) -> None:
        if self.status in (SignalStatus.EXECUTED, SignalStatus.REJECTED, SignalStatus.CANCELLED):
            raise ValueError(f"Cannot cancel from {self.status.value}")
        self._update_status(SignalStatus.CANCELLED)
        self.reject_reason = reason

    def mark_executing(self) -> None:
        if self.status != SignalStatus.APPROVED:
            raise ValueError("Must be APPROVED to start executing")
        self._update_status(SignalStatus.EXECUTING)

    def attach_order(self, order_id: str) -> None:
        """关联执行的订单ID。"""
        self.order_id = order_id

    def set_data_hash(self, data: Dict[str, Any]) -> None:
        self.data_hash = self.compute_data_hash(data)

    @staticmethod
    def compute_data_hash(data: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()

    # ------------------------ 过期判断 ------------------------
    def is_expired(self, current_time: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        now = current_time or _utc_now_ts()
        return now > self.expires_at

    # ------------------------ 签名 ------------------------
    def sign(self, secret: str) -> None:
        """对关键字段签名，Decimal 转固定精度字符串防歧义。"""
        def fmt(d: Optional[Decimal]) -> str:
            return d.quantize(Decimal('0.00000001')) if d is not None else 'None'
        payload = (f"{self.signal_id}:{self.symbol}:{self.direction.value}:"
                   f"{self.signal_type.value}:{fmt(self.quantity_ratio)}:{fmt(self.limit_price)}:"
                   f"{fmt(self.stop_price)}:{self.timestamp}:{self.priority.value}")
        self.signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def verify_signature(self, secret: str) -> bool:
        if not self.signature:
            return False
        def fmt(d: Optional[Decimal]) -> str:
            return d.quantize(Decimal('0.00000001')) if d is not None else 'None'
        payload = (f"{self.signal_id}:{self.symbol}:{self.direction.value}:"
                   f"{self.signal_type.value}:{fmt(self.quantity_ratio)}:{fmt(self.limit_price)}:"
                   f"{fmt(self.stop_price)}:{self.timestamp}:{self.priority.value}")
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    # ------------------------ 序列化 ------------------------
    def to_dict(self, safe: bool = False, include_snapshot: bool = True) -> Dict[str, Any]:
        def _opt(d: Optional[Decimal]) -> Optional[str]:
            return str(d) if d is not None else None

        d = {
            'signal_id': self.signal_id,
            'idempotency_key': self.idempotency_key,
            'symbol': self.symbol,
            'direction': self.direction.value,
            'signal_type': self.signal_type.value,
            'position_side': self.position_side,
            'priority': self.priority.value,
            'status': self.status.value,
            'status_updated_at': self.status_updated_at,
            'quantity_ratio': str(self.quantity_ratio),
            'target_quantity': _opt(self.target_quantity),
            'min_execution_qty': _opt(self.min_execution_qty),
            'min_qty': _opt(self.min_qty),
            'step_size': _opt(self.step_size),
            'limit_price': _opt(self.limit_price),
            'stop_price': _opt(self.stop_price),
            'take_profit_price': _opt(self.take_profit_price),
            'trigger_price': _opt(self.trigger_price),
            'trailing_stop_atr': _opt(self.trailing_stop_atr),
            'execution_type': self.execution_type,
            'max_slippage_pct': self.max_slippage_pct,
            'max_execution_delay_sec': self.max_execution_delay_sec,
            'is_urgent': self.is_urgent,
            'allow_algo': self.allow_algo,
            'allow_hedge': self.allow_hedge,
            'risk_budget_used': self.risk_budget_used,
            'max_loss_pct': self.max_loss_pct,
            'risk_checked': self.risk_checked,
            'risk_passed': self.risk_passed,
            'compliance_checked': self.compliance_checked,
            'leverage_impact': self.leverage_impact,
            'margin_impact_pct': self.margin_impact_pct,
            'expected_pnl': _opt(self.expected_pnl),
            'estimated_slippage': str(self.estimated_slippage),
            'total_cost_estimate': str(self.total_cost_estimate),
            'confidence': str(self.confidence),
            'timestamp': self.timestamp,
            'expires_at': self.expires_at,
            'cooldown_sec': self.cooldown_sec,
            'max_hold_bars': self.max_hold_bars,
            'execute_after': self.execute_after,
            'kline_close_time': self.kline_close_time,
            'strategy_id': '' if safe else self.strategy_id,
            'strategy_version': self.strategy_version,
            'source_module': '' if safe else self.source_module,
            'parent_signal_id': self.parent_signal_id,
            'order_id': self.order_id,
            'exchange': self.exchange,
            'created_by': '' if safe else self.created_by,
            'is_manual': self.is_manual,
            'is_composite': self.is_composite,
            'is_simulation': self.is_simulation,
            'reason': self.reason,
            'tags': self.tags,
            'data_hash': self.data_hash,
            'snapshot': _serialize_value(self.snapshot) if include_snapshot else {},
            'reject_reason': self.reject_reason,
            'conflicts_with': self.conflicts_with,
            'signature': self.signature,
            'metadata': _serialize_value(self.metadata) if not safe else self._safe_metadata(),
        }
        return d

    def _safe_metadata(self) -> Dict[str, Any]:
        safe_keys = {'market_regime', 'volatility', 'trend_strength'}
        return {k: v for k, v in self.metadata.items() if k in safe_keys}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Signal':
        def _d(val):
            if val is None:
                return None
            if isinstance(val, str) and val.strip() == '':
                return None
            return _safe_decimal(val, allow_none=True)

        def _d_required(val, default='0'):
            # 对于非可选 Decimal 字段，必须返回值
            if val is None or (isinstance(val, str) and val.strip() == ''):
                val = default
            return _safe_decimal(val, allow_none=False)

        # 安全解析枚举
        def _enum(enum_cls, val, default):
            try:
                return enum_cls(val)
            except (ValueError, KeyError):
                return default

        return cls(
            signal_id=data.get('signal_id', f"sig_{uuid.uuid4().hex}"),
            idempotency_key=data.get('idempotency_key', f"idem_{uuid.uuid4().hex}"),
            symbol=data.get('symbol', ''),
            direction=_enum(SignalDirection, data.get('direction', 'NEUTRAL'), SignalDirection.NEUTRAL),
            signal_type=_enum(SignalType, data.get('signal_type', 'NO_ACTION'), SignalType.NO_ACTION),
            position_side=data.get('position_side'),
            priority=_enum(SignalPriority, data.get('priority', 6), SignalPriority.NORMAL_ENTRY),
            status=_enum(SignalStatus, data.get('status', 'PENDING'), SignalStatus.PENDING),
            status_updated_at=data.get('status_updated_at', _utc_now_ts()),
            quantity_ratio=_d_required(data.get('quantity_ratio'), '0'),
            target_quantity=_d(data.get('target_quantity')),
            min_execution_qty=_d(data.get('min_execution_qty')),
            min_qty=_d(data.get('min_qty')),
            step_size=_d(data.get('step_size')),
            limit_price=_d(data.get('limit_price')),
            stop_price=_d(data.get('stop_price')),
            take_profit_price=_d(data.get('take_profit_price')),
            trigger_price=_d(data.get('trigger_price')),
            trailing_stop_atr=_d(data.get('trailing_stop_atr')),
            execution_type=data.get('execution_type', 'LIMIT'),
            max_slippage_pct=float(data.get('max_slippage_pct', 0.1)),
            max_execution_delay_sec=float(data.get('max_execution_delay_sec', 5.0)),
            is_urgent=_parse_bool(data.get('is_urgent', False)),
            allow_algo=_parse_bool(data.get('allow_algo', True)),
            allow_hedge=_parse_bool(data.get('allow_hedge', False)),
            risk_budget_used=float(data.get('risk_budget_used', 0.0)),
            max_loss_pct=float(data.get('max_loss_pct', 1.0)),
            risk_checked=_parse_bool(data.get('risk_checked', False)),
            risk_passed=_parse_bool(data.get('risk_passed', False)),
            compliance_checked=_parse_bool(data.get('compliance_checked', False)),
            leverage_impact=float(data.get('leverage_impact', 0.0)),
            margin_impact_pct=float(data.get('margin_impact_pct', 0.0)),
            expected_pnl=_d(data.get('expected_pnl')),
            estimated_slippage=_d_required(data.get('estimated_slippage'), '0'),
            total_cost_estimate=_d_required(data.get('total_cost_estimate'), '0'),
            confidence=_d_required(data.get('confidence'), '0'),
            timestamp=data.get('timestamp', _utc_now_ts()),
            expires_at=data.get('expires_at'),
            cooldown_sec=float(data.get('cooldown_sec', 0.0)),
            max_hold_bars=int(data.get('max_hold_bars', 0)),
            execute_after=data.get('execute_after'),
            kline_close_time=data.get('kline_close_time'),
            strategy_id=data.get('strategy_id', ''),
            strategy_version=data.get('strategy_version', ''),
            source_module=data.get('source_module', ''),
            parent_signal_id=data.get('parent_signal_id'),
            order_id=data.get('order_id'),
            exchange=data.get('exchange', ''),
            created_by=data.get('created_by', 'system'),
            is_manual=_parse_bool(data.get('is_manual', False)),
            is_composite=_parse_bool(data.get('is_composite', False)),
            is_simulation=_parse_bool(data.get('is_simulation', False)),
            reason=data.get('reason', ''),
            tags=data.get('tags') if isinstance(data.get('tags'), list) else [],
            data_hash=data.get('data_hash'),
            snapshot=data.get('snapshot') if isinstance(data.get('snapshot'), dict) else {},
            reject_reason=data.get('reject_reason'),
            conflicts_with=data.get('conflicts_with') if isinstance(data.get('conflicts_with'), list) else [],
            signature=data.get('signature'),
            metadata=data.get('metadata') if isinstance(data.get('metadata'), dict) else {},
        )

    def clone(self) -> 'Signal':
        """创建深拷贝信号，生成新的 signal_id 和 idempotency_key。"""
        import copy
        new_sig = copy.deepcopy(self)
        new_sig.signal_id = f"sig_{uuid.uuid4().hex}"
        new_sig.idempotency_key = f"idem_{uuid.uuid4().hex}"
        return new_sig

    def __hash__(self):
        return hash(self.signal_id)

    def __eq__(self, other):
        return isinstance(other, Signal) and self.signal_id == other.signal_id

    def __repr__(self) -> str:
        conf_str = f"{float(self.confidence):.2f}" if self.confidence is not None else "N/A"
        return (f"Signal({self.signal_id[:8]} {self.symbol} {self.direction.value} "
                f"{self.signal_type.value} pri={self.priority.name} conf={conf_str})")


# =============================================================================
# SignalBatch
# =============================================================================

@dataclass
class SignalBatch:
    """批量信号，用于原子提交。"""
    batch_id: str = field(default_factory=lambda: f"batch_{uuid.uuid4().hex}")
    signals: List[Signal] = field(default_factory=list)
    created_at: float = field(default_factory=_utc_now_ts)
    is_atomic: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            'batch_id': self.batch_id,
            'signals': [s.to_dict() for s in self.signals],
            'created_at': self.created_at,
            'is_atomic': self.is_atomic,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SignalBatch':
        raw_signals = data.get('signals', [])
        if not isinstance(raw_signals, list):
            raw_signals = []
        signals = [Signal.from_dict(s) for s in raw_signals]
        return cls(
            batch_id=data.get('batch_id', f"batch_{uuid.uuid4().hex}"),
            signals=signals,
            created_at=data.get('created_at', _utc_now_ts()),
            is_atomic=_parse_bool(data.get('is_atomic', True)),
        )

    def __hash__(self):
        return hash(self.batch_id)

    def __eq__(self, other):
        return isinstance(other, SignalBatch) and self.batch_id == other.batch_id

    def __repr__(self) -> str:
        return f"SignalBatch({self.batch_id[:8]} cnt={len(self.signals)})"


# =============================================================================
# SignalConfirmation
# =============================================================================

@dataclass
class SignalConfirmation:
    """信号执行确认。"""
    signal_id: str
    status: SignalStatus
    order_id: Optional[str] = None
    filled_qty: Decimal = Decimal('0')
    avg_price: Optional[Decimal] = None
    commission: Decimal = Decimal('0')
    position_side: Optional[str] = None
    message: str = ""
    timestamp: float = field(default_factory=_utc_now_ts)

    def __post_init__(self):
        self.filled_qty = _safe_decimal(self.filled_qty)
        if self.avg_price is not None:
            self.avg_price = _safe_decimal(self.avg_price)
        self.commission = _safe_decimal(self.commission)
        if isinstance(self.status, str):
            self.status = SignalStatus(self.status)
        if self.position_side is not None and self.position_side not in ('LONG', 'SHORT'):
            raise ValueError("position_side must be 'LONG', 'SHORT' or None")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'signal_id': self.signal_id,
            'status': self.status.value,
            'order_id': self.order_id,
            'filled_qty': str(self.filled_qty),
            'avg_price': str(self.avg_price) if self.avg_price is not None else None,
            'commission': str(self.commission),
            'position_side': self.position_side,
            'message': self.message,
            'timestamp': self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SignalConfirmation':
        def _d(val):
            return _safe_decimal(val, allow_none=True) if val is not None else None
        return cls(
            signal_id=data.get('signal_id', ''),
            status=SignalStatus(data.get('status', 'PENDING')),
            order_id=data.get('order_id'),
            filled_qty=_d(data.get('filled_qty', '0')) or Decimal('0'),
            avg_price=_d(data.get('avg_price')),
            commission=_d(data.get('commission', '0')) or Decimal('0'),
            position_side=data.get('position_side'),
            message=data.get('message', ''),
            timestamp=data.get('timestamp', _utc_now_ts()),
        )

    def __hash__(self):
        return hash((self.signal_id, self.timestamp))

    def __eq__(self, other):
        return isinstance(other, SignalConfirmation) and self.signal_id == other.signal_id and self.timestamp == other.timestamp

    def __repr__(self) -> str:
        return f"SignalConfirmation({self.signal_id[:8]} {self.status.value})"


# =============================================================================
# 兼容性检查
# =============================================================================
def check_compatibility(version: int) -> bool:
    """检查信号模型版本兼容性。"""
    return version <= Signal.SIGNAL_VERSION
