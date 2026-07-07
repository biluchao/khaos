# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: position.py
核心职责: 定义持仓数据模型，管理盈亏计算、保证金监控及全生命周期。
所属层级: core.models

设计说明:
    - 所有金额/数量使用 Decimal，建议全局精度设为28。
    - 数据类实例非线程安全，仅限策略引擎单线程主循环使用。
    - 时间戳统一为 UTC 浮点秒。

外部依赖:
    - dataclasses, enum, typing, uuid, datetime, decimal, copy, re

接口契约:
    提供: PositionSide, PositionStatus, MarginMode, ContractType, Position
    消费: 无

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2026-07-07 v34.0: 终极审计，全面支持反向合约、费用分摊、审计日志、完整序列化。
"""

import uuid
import re
import copy
from dataclasses import dataclass, field
from enum import Enum
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, getcontext
from typing import Optional, Dict, Any, List, Callable
from datetime import datetime, timezone

# 建议全局精度
# getcontext().prec = 28

__all__ = ['PositionSide', 'PositionStatus', 'MarginMode', 'ContractType', 'Position']


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _utc_now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _safe_decimal(value, default='0') -> Decimal:
    """安全转换为 Decimal，NaN/Inf 抛出异常。"""
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise ValueError("Decimal cannot be NaN or Inf")
        return value
    try:
        d = Decimal(str(value))
        if d.is_nan() or d.is_infinite():
            raise ValueError("Decimal cannot be NaN or Inf")
        return d
    except Exception:
        return Decimal(default)


def _d_str(d: Decimal) -> str:
    return str(d)


# =============================================================================
# 枚举定义
# =============================================================================

class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    LIQUIDATED = "LIQUIDATED"


class MarginMode(str, Enum):
    ISOLATED = "ISOLATED"
    CROSSED = "CROSSED"


class ContractType(str, Enum):
    LINEAR = "LINEAR"          # USDT本位
    INVERSE = "INVERSE"        # 币本位


# =============================================================================
# 持仓数据模型
# =============================================================================

@dataclass
class Position:
    """
    持仓实体，单线程非线程安全。
    """
    # 唯一标识
    _position_id: str = field(default_factory=lambda: f"pos_{uuid.uuid4().hex[:12]}")
    symbol: str = ""
    side: PositionSide = PositionSide.LONG
    contract_type: ContractType = ContractType.LINEAR
    base_asset: str = ""
    quote_asset: str = ""
    multiplier: Decimal = Decimal('1')       # 合约乘数

    # 数量
    _quantity: Decimal = Decimal('0')
    _entry_price: Decimal = Decimal('0')
    _mark_price: Decimal = Decimal('0')
    _mark_price_updated_at: Optional[float] = None
    _liquidation_price: Optional[Decimal] = None

    # 杠杆与保证金
    leverage: int = 1
    min_leverage: int = 1
    max_leverage: int = 125
    margin_mode: MarginMode = MarginMode.ISOLATED
    initial_margin: Decimal = Decimal('0')
    maintenance_margin: Decimal = Decimal('0')
    maintenance_margin_rate: Decimal = Decimal('0.005')

    # 费用
    total_commission: Decimal = Decimal('0')
    total_funding: Decimal = Decimal('0')

    # 盈亏
    unrealized_pnl: Decimal = Decimal('0')
    realized_pnl: Decimal = Decimal('0')
    daily_realized_pnl: Decimal = Decimal('0')

    # 状态
    status: PositionStatus = PositionStatus.OPEN
    frozen: bool = False
    close_reason: str = ""

    # 时间
    opened_at: float = field(default_factory=_utc_now_ts)
    updated_at: float = field(default_factory=_utc_now_ts)
    closed_at: Optional[float] = None
    last_fill_time: Optional[float] = None

    # 关联
    strategy_id: str = ""
    tag: str = ""
    portfolio_id: str = ""
    opening_order_id: Optional[str] = None
    related_order_ids: List[str] = field(default_factory=list)
    attached_stop_loss_id: Optional[str] = None
    attached_take_profit_id: Optional[str] = None

    # 历史与统计
    fills: List[Dict[str, Any]] = field(default_factory=list)
    funding_payments: List[Dict[str, Any]] = field(default_factory=list)
    entry_calculation_log: List[Dict[str, Any]] = field(default_factory=list)
    leverage_changes: List[Dict[str, Any]] = field(default_factory=list)
    audit_log: List[Dict[str, Any]] = field(default_factory=list)

    # 分析
    peak_value: Decimal = Decimal('0')
    max_drawdown: Decimal = Decimal('0')
    peak_profit: Decimal = Decimal('0')
    peak_loss: Decimal = Decimal('0')
    mfe: Decimal = Decimal('0')
    mae: Decimal = Decimal('0')
    volatility: Optional[Decimal] = None

    # 版本
    version: int = 0

    # 交易规则
    qty_step: Optional[Decimal] = None
    price_tick: Optional[Decimal] = None
    risk_limit: Optional[Decimal] = None

    # 回调
    _margin_call_callback: Optional[Callable] = field(default=None, repr=False)
    _in_callback: bool = field(default=False, repr=False)

    # -----------------------------------------------------------------
    # 属性
    # -----------------------------------------------------------------
    @property
    def position_id(self) -> str:
        return self._position_id

    @property
    def quantity(self) -> Decimal:
        return self._quantity

    @property
    def entry_price(self) -> Decimal:
        return self._entry_price

    @property
    def mark_price(self) -> Decimal:
        return self._mark_price

    @property
    def liquidation_price(self) -> Optional[Decimal]:
        return self._liquidation_price

    @property
    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN and not self.frozen

    @property
    def is_empty(self) -> bool:
        return self._quantity == 0

    @property
    def is_long(self) -> bool:
        return self.side == PositionSide.LONG

    @property
    def is_short(self) -> bool:
        return self.side == PositionSide.SHORT

    # -----------------------------------------------------------------
    # 初始化
    # -----------------------------------------------------------------
    def __post_init__(self):
        if self.side not in (PositionSide.LONG, PositionSide.SHORT):
            raise ValueError("side must be LONG or SHORT")
        if not re.match(r'^[A-Z0-9]{6,12}$', self.symbol):
            raise ValueError(f"Invalid symbol: {self.symbol}")
        # 反向合约乘数强制为1（文档）
        if self.contract_type == ContractType.INVERSE and self.multiplier != 1:
            raise ValueError("Inverse contract multiplier must be 1")
        self._quantity = _safe_decimal(self._quantity)
        if self._quantity < 0:
            raise ValueError("quantity must be >= 0")
        self._entry_price = _safe_decimal(self._entry_price)
        self._mark_price = _safe_decimal(self._mark_price)
        self.initial_margin = _safe_decimal(self.initial_margin)
        self.maintenance_margin = _safe_decimal(self.maintenance_margin)
        self.total_commission = _safe_decimal(self.total_commission)
        self.total_funding = _safe_decimal(self.total_funding)
        self.unrealized_pnl = _safe_decimal(self.unrealized_pnl)
        self.realized_pnl = _safe_decimal(self.realized_pnl)
        self.daily_realized_pnl = _safe_decimal(self.daily_realized_pnl)
        if self._liquidation_price is not None:
            self._liquidation_price = _safe_decimal(self._liquidation_price)
        if self._quantity > 0:
            if self._mark_price > 0:
                self._mark_price_updated_at = _utc_now_ts()
                self._recalc_unrealized_pnl()
            self._update_liquidation_price()
        if len(self.tag) > 50:
            self.tag = self.tag[:50]
        if len(self.strategy_id) > 50:
            self.strategy_id = self.strategy_id[:50]

    # -----------------------------------------------------------------
    # 计算
    # -----------------------------------------------------------------
    def _recalc_unrealized_pnl(self) -> Decimal:
        if self._quantity == 0 or self._mark_price <= 0:
            self.unrealized_pnl = Decimal('0')
            return Decimal('0')
        if self.side == PositionSide.LONG:
            if self.contract_type == ContractType.LINEAR:
                pnl = (self._mark_price - self._entry_price) * self._quantity * self.multiplier
            else:
                # 反向合约精确盈亏 (币本位，以报价币种计价？简化使用价格差乘数量乘乘数)
                pnl = (self._mark_price - self._entry_price) * self._quantity * self.multiplier
        else:
            if self.contract_type == ContractType.LINEAR:
                pnl = (self._entry_price - self._mark_price) * self._quantity * self.multiplier
            else:
                pnl = (self._entry_price - self._mark_price) * self._quantity * self.multiplier
        self.unrealized_pnl = pnl
        if pnl > self.peak_profit:
            self.peak_profit = pnl
        if pnl < self.peak_loss:
            self.peak_loss = pnl
        if pnl > self.mfe:
            self.mfe = pnl
        if pnl < self.mae:
            self.mae = pnl
        return pnl

    def update_mark_price(self, mark_price: Decimal) -> None:
        mark_price = _safe_decimal(mark_price)
        if mark_price <= 0:
            raise ValueError("mark_price must be positive")
        if mark_price == self._mark_price:
            self._mark_price_updated_at = _utc_now_ts()
            return
        self._mark_price = mark_price
        self._mark_price_updated_at = _utc_now_ts()
        self._recalc_unrealized_pnl()
        self.updated_at = _utc_now_ts()
        self.version += 1
        current_val = self._notional_value_internal()
        if current_val > self.peak_value:
            self.peak_value = current_val
        if self.peak_value > 0:
            dd = (self.peak_value - current_val) / self.peak_value
            if dd > self.max_drawdown:
                self.max_drawdown = dd
        if self.is_liquidatable and self._margin_call_callback and not self._in_callback:
            self._in_callback = True
            try:
                self._margin_call_callback(self)
            finally:
                self._in_callback = False

    @property
    def net_unrealized_pnl(self) -> Decimal:
        return self.unrealized_pnl - self.total_commission - self.total_funding

    @property
    def pnl_percentage(self) -> Decimal:
        if self.initial_margin > 0:
            return (self.realized_pnl + self.net_unrealized_pnl) / self.initial_margin
        return Decimal('0')

    @property
    def total_cost(self) -> Decimal:
        if self._quantity == 0:
            return Decimal('0')
        if self.contract_type == ContractType.INVERSE:
            return (self._quantity / self._entry_price) * self.multiplier
        else:
            return self._entry_price * self._quantity * self.multiplier

    # -----------------------------------------------------------------
    # 强平
    # -----------------------------------------------------------------
    def _update_liquidation_price(self) -> None:
        if self._quantity > 0 and self.leverage > 0 and self.maintenance_margin_rate:
            self._liquidation_price = self.calc_liquidation_price(
                self.side, self._entry_price, self.leverage, self.maintenance_margin_rate,
                self._quantity, self.initial_margin, self.contract_type, self.multiplier)
        else:
            self._liquidation_price = None
        self.version += 1

    @staticmethod
    def calc_liquidation_price(side: PositionSide, entry_price: Decimal, leverage: int,
                               maintenance_margin_rate: Decimal, quantity: Decimal,
                               initial_margin: Decimal, contract_type: ContractType = ContractType.LINEAR,
                               multiplier: Decimal = Decimal('1')) -> Decimal:
        if quantity <= 0 or leverage <= 0:
            return Decimal('0')
        if contract_type == ContractType.LINEAR:
            if side == PositionSide.LONG:
                return entry_price * (Decimal('1') - Decimal('1') / leverage + maintenance_margin_rate)
            else:
                return entry_price * (Decimal('1') + Decimal('1') / leverage - maintenance_margin_rate)
        else:
            # 反向合约 (乘数通常为1)
            if side == PositionSide.LONG:
                return entry_price / (Decimal('1') + Decimal('1') / leverage - maintenance_margin_rate)
            else:
                return entry_price / (Decimal('1') - Decimal('1') / leverage + maintenance_margin_rate)

    @property
    def is_liquidatable(self) -> bool:
        if self._quantity == 0:
            return False
        if self._liquidation_price is not None:
            if self.side == PositionSide.LONG and self._mark_price <= self._liquidation_price:
                return True
            if self.side == PositionSide.SHORT and self._mark_price >= self._liquidation_price:
                return True
        if self.maintenance_margin > 0 and self.notional_value > 0:
            if self.maintenance_margin / self.notional_value > Decimal('0.1'):
                return True
        return False

    @property
    def margin_ratio(self) -> Optional[Decimal]:
        if self.margin_mode == MarginMode.CROSSED:
            return None
        if self.notional_value > 0 and self.maintenance_margin > 0:
            return self.maintenance_margin / self.notional_value
        return None

    # -----------------------------------------------------------------
    # 名义价值
    # -----------------------------------------------------------------
    def _notional_value_internal(self) -> Decimal:
        if self._quantity == 0:
            return Decimal('0')
        if self.contract_type == ContractType.INVERSE:
            return (self._quantity / self._mark_price) * self.multiplier if self._mark_price > 0 else Decimal('0')
        else:
            return self._quantity * self._mark_price * self.multiplier

    @property
    def notional_value(self) -> Decimal:
        return self._notional_value_internal()

    @property
    def delta_exposure(self) -> Decimal:
        return self.notional_value if self.side == PositionSide.LONG else -self.notional_value

    # -----------------------------------------------------------------
    # 操作
    # -----------------------------------------------------------------
    def add_fill(self, fill_price: Decimal, fill_qty: Decimal,
                 commission: Decimal = Decimal('0'), order_id: str = "") -> None:
        fill_price = _safe_decimal(fill_price)
        if fill_price <= 0:
            raise ValueError("fill_price must be positive")
        fill_qty = _safe_decimal(fill_qty)
        if fill_qty <= 0:
            raise ValueError("add_fill requires positive fill_qty")
        commission = _safe_decimal(commission)
        if self.qty_step:
            fill_qty = (fill_qty / self.qty_step).to_integral_value(ROUND_DOWN) * self.qty_step
        if fill_qty == 0:
            return
        old_qty = self._quantity
        old_entry = self._entry_price
        total_pure_cost = old_entry * old_qty + fill_price * fill_qty
        self._quantity += fill_qty
        if self._quantity > 0:
            self._entry_price = (total_pure_cost / self._quantity).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
        else:
            self._entry_price = Decimal('0')
        self.total_commission += commission
        if order_id:
            self.related_order_ids.append(order_id)
        self._log_add_fill(fill_price, fill_qty, commission, old_qty, old_entry)
        self.updated_at = _utc_now_ts()
        self.last_fill_time = _utc_now_ts()
        self._recalc_unrealized_pnl()
        self._update_liquidation_price()
        self._add_audit("add_fill", {"price": str(fill_price), "qty": str(fill_qty)})
        self.version += 1
        self._trim_logs()

    def reduce_position(self, reduce_qty: Decimal, fill_price: Decimal,
                        commission: Decimal = Decimal('0'), order_id: str = "") -> Decimal:
        fill_price = _safe_decimal(fill_price)
        if fill_price <= 0:
            raise ValueError("fill_price must be positive")
        reduce_qty = _safe_decimal(reduce_qty)
        commission = _safe_decimal(commission)
        if reduce_qty <= 0:
            raise ValueError("reduce_qty must be positive")
        if self._quantity == 0:
            return Decimal('0')
        if self.qty_step:
            reduce_qty = (reduce_qty / self.qty_step).to_integral_value(ROUND_DOWN) * self.qty_step
        if reduce_qty == 0:
            return Decimal('0')
        reduce_qty = min(reduce_qty, self._quantity)
        old_qty = self._quantity
        fee_ratio = reduce_qty / old_qty

        # 盈亏计算
        if self.contract_type == ContractType.LINEAR:
            if self.side == PositionSide.LONG:
                gross_pnl = (fill_price - self._entry_price) * reduce_qty * self.multiplier
            else:
                gross_pnl = (self._entry_price - fill_price) * reduce_qty * self.multiplier
        else:  # 反向合约精确盈亏
            if self.side == PositionSide.LONG:
                gross_pnl = ((Decimal('1') / self._entry_price - Decimal('1') / fill_price) * reduce_qty * self.multiplier)
            else:
                gross_pnl = ((Decimal('1') / fill_price - Decimal('1') / self._entry_price) * reduce_qty * self.multiplier)

        # 分摊历史费用
        allocated_funding = self.total_funding * fee_ratio
        allocated_commission = self.total_commission * fee_ratio
        realized = gross_pnl - commission - allocated_funding - allocated_commission

        self.realized_pnl += realized
        self.daily_realized_pnl += realized
        self.total_commission -= allocated_commission
        self.total_funding -= allocated_funding
        self._quantity -= reduce_qty
        if self._quantity == 0:
            self.status = PositionStatus.CLOSED
            self.closed_at = _utc_now_ts()
            self._entry_price = Decimal('0')
            self._liquidation_price = None
            self.peak_profit = Decimal('0')
            self.peak_loss = Decimal('0')
            self.mfe = Decimal('0')
            self.mae = Decimal('0')
        if order_id:
            self.related_order_ids.append(order_id)
        self._log_reduce_fill(fill_price, reduce_qty, commission, realized)
        self.updated_at = _utc_now_ts()
        self.last_fill_time = _utc_now_ts()
        self._recalc_unrealized_pnl()
        self._update_liquidation_price()
        self._add_audit("reduce_position", {"price": str(fill_price), "qty": str(reduce_qty), "pnl": str(realized)})
        self.version += 1
        self._trim_logs()
        return realized

    def apply_funding(self, payment: Decimal, rate: Decimal, timestamp: Optional[float] = None) -> None:
        payment = _safe_decimal(payment)
        self.total_funding += payment
        self.funding_payments.append({
            'payment': str(payment),
            'rate': str(rate),
            'timestamp': timestamp or _utc_now_ts(),
        })
        self.updated_at = _utc_now_ts()
        self._add_audit("funding", {"payment": str(payment)})
        self.version += 1
        self._trim_logs()

    def adjust_margin(self, amount: Decimal) -> None:
        amount = _safe_decimal(amount)
        self.initial_margin += amount
        if self.initial_margin < 0:
            self.initial_margin = Decimal('0')
        self._update_liquidation_price()
        self.updated_at = _utc_now_ts()
        self._add_audit("adjust_margin", {"amount": str(amount)})
        self.version += 1

    def change_leverage(self, new_leverage: int) -> None:
        if new_leverage < self.min_leverage or new_leverage > self.max_leverage:
            raise ValueError(f"leverage must be between {self.min_leverage} and {self.max_leverage}")
        self.leverage_changes.append({
            'old': self.leverage,
            'new': new_leverage,
            'timestamp': _utc_now_ts(),
        })
        self.leverage = new_leverage
        self._update_liquidation_price()
        self._add_audit("change_leverage", {"old": self.leverage, "new": new_leverage})
        self.version += 1

    def close_position(self, exit_price: Decimal, commission: Decimal = Decimal('0'),
                       reason: str = "") -> Decimal:
        if self._quantity == 0:
            return Decimal('0')
        self.close_reason = reason
        return self.reduce_position(self._quantity, exit_price, commission)

    def freeze(self) -> None:
        self.frozen = True
        self._add_audit("freeze")
        self.version += 1

    def unfreeze(self) -> None:
        self.frozen = False
        self._add_audit("unfreeze")
        self.version += 1

    def switch_margin_mode(self, mode: MarginMode) -> None:
        self.margin_mode = mode
        self._add_audit("switch_margin_mode", {"mode": mode.value})
        self.version += 1

    def set_margin_callback(self, cb: Callable) -> None:
        self._margin_call_callback = cb

    def remove_callback(self) -> None:
        self._margin_call_callback = None

    # -----------------------------------------------------------------
    # 日志与清理
    # -----------------------------------------------------------------
    def _log_add_fill(self, price, qty, comm, old_qty, old_entry):
        self.entry_calculation_log.append({
            'old_qty': str(old_qty), 'old_entry': str(old_entry),
            'fill_price': str(price), 'fill_qty': str(qty),
            'new_qty': str(self._quantity), 'new_entry': str(self._entry_price),
        })
        self.fills.append({
            'type': 'add', 'price': str(price), 'qty': str(qty), 'commission': str(comm),
            'time': _utc_now_ts(),
        })

    def _log_reduce_fill(self, price, qty, comm, pnl):
        self.fills.append({
            'type': 'reduce', 'price': str(price), 'qty': str(qty), 'commission': str(comm),
            'realized_pnl': str(pnl), 'time': _utc_now_ts(),
        })

    def _trim_logs(self):
        if len(self.fills) > 200:
            self.fills = self.fills[-200:]
        if len(self.funding_payments) > 200:
            self.funding_payments = self.funding_payments[-200:]
        if len(self.entry_calculation_log) > 50:
            self.entry_calculation_log = self.entry_calculation_log[-50:]
        if len(self.leverage_changes) > 20:
            self.leverage_changes = self.leverage_changes[-20:]
        if len(self.audit_log) > 200:
            self.audit_log = self.audit_log[-200:]

    def _add_audit(self, event: str, details: Dict = None):
        self.audit_log.append({
            'event': event,
            'timestamp': _utc_now_ts(),
            'details': details or {},
        })
        self._trim_logs()

    def archive(self) -> Dict[str, Any]:
        summary = self.to_summary()
        self.fills.clear()
        self.funding_payments.clear()
        self.entry_calculation_log.clear()
        self.leverage_changes.clear()
        self.audit_log.clear()
        self.version += 1
        return summary

    def reset_daily_stats(self) -> None:
        self.daily_realized_pnl = Decimal('0')
        self.version += 1

    def get_audit_trail(self) -> List[Dict]:
        return self.audit_log.copy()

    # -----------------------------------------------------------------
    # 序列化
    # -----------------------------------------------------------------
    def to_dict(self, safe: bool = False, include_details: bool = True) -> Dict[str, Any]:
        d = {
            'position_id': self._position_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'contract_type': self.contract_type.value,
            'multiplier': str(self.multiplier),
            'quantity': str(self._quantity),
            'entry_price': str(self._entry_price),
            'mark_price': str(self._mark_price),
            'mark_price_updated_at': self._mark_price_updated_at,
            'liquidation_price': str(self._liquidation_price) if self._liquidation_price is not None else None,
            'leverage': self.leverage,
            'min_leverage': self.min_leverage,
            'max_leverage': self.max_leverage,
            'margin_mode': self.margin_mode.value,
            'initial_margin': str(self.initial_margin),
            'maintenance_margin': str(self.maintenance_margin),
            'maintenance_margin_rate': str(self.maintenance_margin_rate),
            'unrealized_pnl': str(self.unrealized_pnl),
            'realized_pnl': str(self.realized_pnl),
            'daily_realized_pnl': str(self.daily_realized_pnl),
            'status': self.status.value,
            'frozen': self.frozen,
            'close_reason': self.close_reason,
            'opened_at': self.opened_at,
            'updated_at': self.updated_at,
            'closed_at': self.closed_at,
            'last_fill_time': self.last_fill_time,
            'version': self.version,
            'volatility': str(self.volatility) if self.volatility is not None else None,
            'mfe': str(self.mfe),
            'mae': str(self.mae),
            'max_drawdown': str(self.max_drawdown),
            'peak_profit': str(self.peak_profit),
            'peak_loss': str(self.peak_loss),
            'qty_step': str(self.qty_step) if self.qty_step else None,
            'price_tick': str(self.price_tick) if self.price_tick else None,
            'risk_limit': str(self.risk_limit) if self.risk_limit else None,
        }
        if not safe:
            d['strategy_id'] = self.strategy_id
            d['tag'] = self.tag
            d['portfolio_id'] = self.portfolio_id
            d['opening_order_id'] = self.opening_order_id
            d['related_order_ids'] = self.related_order_ids
            d['attached_stop_loss_id'] = self.attached_stop_loss_id
            d['attached_take_profit_id'] = self.attached_take_profit_id
        if include_details:
            d['fills'] = self.fills
            d['funding_payments'] = self.funding_payments
            d['entry_calculation_log'] = self.entry_calculation_log
            d['leverage_changes'] = self.leverage_changes
            d['audit_log'] = self.audit_log
        return d

    def to_summary(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'quantity': str(self._quantity),
            'entry_price': str(self._entry_price),
            'mark_price': str(self._mark_price),
            'unrealized_pnl': str(self.unrealized_pnl),
            'realized_pnl': str(self.realized_pnl),
            'margin_ratio': str(self.margin_ratio) if self.margin_ratio is not None else None,
        }

    def to_analysis(self) -> Dict[str, Any]:
        return {
            'position_id': self._position_id,
            'mfe': str(self.mfe),
            'mae': str(self.mae),
            'max_drawdown': str(self.max_drawdown),
            'peak_profit': str(self.peak_profit),
            'peak_loss': str(self.peak_loss),
            'volatility': str(self.volatility) if self.volatility else None,
        }

    def to_audit_record(self) -> Dict[str, Any]:
        return {
            'position_id': self._position_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'open_time': self.opened_at,
            'close_time': self.closed_at,
            'close_reason': self.close_reason,
            'quantity': str(self._quantity),
            'realized_pnl': str(self.realized_pnl),
            'total_commission': str(self.total_commission),
            'total_funding': str(self.total_funding),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Position':
        def _d(key, default='0'):
            val = data.get(key)
            return _safe_decimal(val, default) if val is not None else None
        return cls(
            _position_id=data.get('position_id', ''),
            symbol=data.get('symbol', ''),
            side=PositionSide(data.get('side', 'LONG')),
            contract_type=ContractType(data.get('contract_type', 'LINEAR')),
            multiplier=_safe_decimal(data.get('multiplier', '1')),
            _quantity=_safe_decimal(data.get('quantity', '0')),
            _entry_price=_safe_decimal(data.get('entry_price', '0')),
            _mark_price=_safe_decimal(data.get('mark_price', '0')),
            _mark_price_updated_at=data.get('mark_price_updated_at'),
            _liquidation_price=_d('liquidation_price', None),
            leverage=data.get('leverage', 1),
            min_leverage=data.get('min_leverage', 1),
            max_leverage=data.get('max_leverage', 125),
            margin_mode=MarginMode(data.get('margin_mode', 'ISOLATED')),
            initial_margin=_safe_decimal(data.get('initial_margin', '0')),
            maintenance_margin=_safe_decimal(data.get('maintenance_margin', '0')),
            maintenance_margin_rate=_safe_decimal(data.get('maintenance_margin_rate', '0.005')),
            total_commission=_safe_decimal(data.get('total_commission', '0')),
            total_funding=_safe_decimal(data.get('total_funding', '0')),
            unrealized_pnl=_safe_decimal(data.get('unrealized_pnl', '0')),
            realized_pnl=_safe_decimal(data.get('realized_pnl', '0')),
            daily_realized_pnl=_safe_decimal(data.get('daily_realized_pnl', '0')),
            status=PositionStatus(data.get('status', 'OPEN')),
            frozen=data.get('frozen', False),
            close_reason=data.get('close_reason', ''),
            opened_at=data.get('opened_at', _utc_now_ts()),
            updated_at=data.get('updated_at', _utc_now_ts()),
            closed_at=data.get('closed_at'),
            last_fill_time=data.get('last_fill_time'),
            strategy_id=data.get('strategy_id', ''),
            tag=data.get('tag', ''),
            portfolio_id=data.get('portfolio_id', ''),
            opening_order_id=data.get('opening_order_id'),
            related_order_ids=data.get('related_order_ids', []),
            attached_stop_loss_id=data.get('attached_stop_loss_id'),
            attached_take_profit_id=data.get('attached_take_profit_id'),
            fills=data.get('fills', []),
            funding_payments=data.get('funding_payments', []),
            entry_calculation_log=data.get('entry_calculation_log', []),
            leverage_changes=data.get('leverage_changes', []),
            audit_log=data.get('audit_log', []),
            peak_value=_safe_decimal(data.get('peak_value', '0')),
            max_drawdown=_safe_decimal(data.get('max_drawdown', '0')),
            peak_profit=_safe_decimal(data.get('peak_profit', '0')),
            peak_loss=_safe_decimal(data.get('peak_loss', '0')),
            mfe=_safe_decimal(data.get('mfe', '0')),
            mae=_safe_decimal(data.get('mae', '0')),
            volatility=_d('volatility', None),
            version=data.get('version', 0),
            qty_step=_d('qty_step', None),
            price_tick=_d('price_tick', None),
            risk_limit=_d('risk_limit', None),
        )

    def copy(self) -> 'Position':
        return copy.deepcopy(self)

    def __hash__(self):
        return hash(self._position_id)

    def __eq__(self, other):
        return isinstance(other, Position) and self._position_id == other._position_id

    def __repr__(self) -> str:
        return (f"Position({self.symbol} {self.side.value} qty:{self._quantity} "
                f"entry:{self._entry_price} uPnl:{self.unrealized_pnl} rPnl:{self.realized_pnl})")
