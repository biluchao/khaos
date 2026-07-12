# -*- coding: utf-8 -*-
"""
模块名称: order_validator.py
核心职责: 在订单提交前校验其是否符合交易所交易规则与系统内部限制，拦截任何不合法订单。
所属层级: core.execution

外部依赖:
    - decimal (Decimal 高精度, getcontext)
    - asyncio (异步锁)
    - time (纳秒级单调时钟)
    - logging (结构化日志)
    - typing (类型注解)
    - core.models.order (Order 模型)

接口契约:
    提供: {
        'OrderValidator': {
            'validate(order: Order) -> None': '校验订单，非法时抛出带中英文错误代码的 ValueError',
            'update_rules(symbol: str, rules: dict) -> None': '手动更新规则缓存'
        }
    }

配置项:
    - execution.global.skip_if_below_min_notional (bool, true)
    - execution.global.price_rounding (str, 'floor_for_buy_ceil_for_sell')
    - execution.global.qty_rounding (str, 'floor')
    - execution.global.max_price_adjustment_ratio (float, 0.01)
    - execution.global.cache_ttl_sec (int, 300)
    - execution.global.cache_max_size (int, 1000)
    - execution.global.log_sensitive (bool, false): 是否在日志中记录价格/数量等敏感信息

作者: KHAOS Execution Team
创建日期: 2025-06-10
修改记录:
    - v1.0 初始版本
    - v2.0 数值安全、中文界面、异常处理全面加固
    - v3.0 极限审计：异步安全、敏感日志控制、错误代码、枚举优化、小账户适配
"""

import asyncio
import logging
import re
import time
from decimal import Decimal, getcontext, ROUND_HALF_UP
from enum import Enum
from typing import Callable, Dict, Optional, List, Tuple

from core.models.order import Order

# 设置 Decimal 全局精度（28位足够金融计算）
getcontext().prec = 28

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 订单方向枚举（优化版，附带中文描述与方法）
# ---------------------------------------------------------------------------
class OrderDirection(Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def cn_name(self) -> str:
        return "买入" if self == OrderDirection.BUY else "卖出"

    @classmethod
    def from_string(cls, s: str) -> 'OrderDirection':
        if not s:
            raise ValueError("方向缺失")
        upper = s.upper()
        if upper in ("BUY", "LONG"):
            return cls.BUY
        if upper in ("SELL", "SHORT"):
            return cls.SELL
        raise ValueError(f"不支持的方向: {s}，请使用 BUY/SELL 或 LONG/SHORT")


# ---------------------------------------------------------------------------
# 错误代码常量
# ---------------------------------------------------------------------------
ERR_SYMBOL_MISSING = "ERR_SYMBOL_MISSING"
ERR_TYPE_MISSING = "ERR_TYPE_MISSING"
ERR_TYPE_UNSUPPORTED = "ERR_TYPE_UNSUPPORTED"
ERR_DIRECTION_MISSING = "ERR_DIRECTION_MISSING"
ERR_DIRECTION_INVALID = "ERR_DIRECTION_INVALID"
ERR_QTY_ZERO = "ERR_QTY_ZERO"
ERR_QTY_NAN = "ERR_QTY_NAN"
ERR_QTY_BELOW_MIN = "ERR_QTY_BELOW_MIN"
ERR_QTY_ABOVE_MAX = "ERR_QTY_ABOVE_MAX"
ERR_QTY_STEP = "ERR_QTY_STEP"
ERR_PRICE_ZERO = "ERR_PRICE_ZERO"
ERR_PRICE_NAN = "ERR_PRICE_NAN"
ERR_PRICE_TICK = "ERR_PRICE_TICK"
ERR_PRICE_ADJUST = "ERR_PRICE_ADJUST"
ERR_STOP_PRICE_MISSING = "ERR_STOP_PRICE_MISSING"
ERR_STOP_PRICE_TICK = "ERR_STOP_PRICE_TICK"
ERR_NOTIONAL_BELOW_MIN = "ERR_NOTIONAL_BELOW_MIN"
ERR_NO_LAST_PRICE = "ERR_NO_LAST_PRICE"
ERR_RULES_INCONSISTENT = "ERR_RULES_INCONSISTENT"
ERR_CLIENT_ID_LENGTH = "ERR_CLIENT_ID_LENGTH"


class OrderValidator:
    def __init__(self,
                 rules_provider: Optional[Callable[[str], Dict]] = None,
                 skip_if_below_min_notional: bool = True,
                 price_rounding: str = 'floor_for_buy_ceil_for_sell',
                 qty_rounding: str = 'floor',
                 max_price_adjustment_ratio: float = 0.01,
                 cache_ttl_sec: int = 300,
                 cache_max_size: int = 1000,
                 lang: str = 'zh',
                 log_sensitive: bool = False):
        if max_price_adjustment_ratio <= 0 or max_price_adjustment_ratio > 0.5:
            raise ValueError("max_price_adjustment_ratio 应在 (0, 0.5] 范围内")
        if cache_ttl_sec <= 0:
            raise ValueError("cache_ttl_sec 必须为正整数")
        if cache_max_size <= 0:
            raise ValueError("cache_max_size 必须为正整数")
        if price_rounding not in ('floor', 'ceil', 'floor_for_buy_ceil_for_sell'):
            raise ValueError(f"不支持的 price_rounding: {price_rounding}")
        if qty_rounding not in ('floor', 'ceil', 'round'):
            raise ValueError(f"不支持的 qty_rounding: {qty_rounding}")

        self._rules_provider = rules_provider
        self._skip_below_min = skip_if_below_min_notional
        self._price_rounding = price_rounding
        self._qty_rounding = qty_rounding
        self._max_price_adj_ratio = Decimal(str(max_price_adjustment_ratio))
        self._cache_ttl_sec = cache_ttl_sec
        self._cache_max_size = cache_max_size
        self.lang = lang
        self._log_sensitive = log_sensitive

        self._rules_cache: Dict[str, Dict] = {}
        self._cache_timestamps: Dict[str, int] = {}  # 纳秒单调时钟
        self._lock = asyncio.Lock()

    async def update_rules(self, symbol: str, rules: dict) -> None:
        """异步安全地手动更新规则缓存"""
        async with self._lock:
            self._rules_cache[symbol] = rules
            self._cache_timestamps[symbol] = time.monotonic_ns()
            logger.info(f"规则缓存已手动更新: {symbol}")

    async def validate(self, order: Order) -> None:
        """
        完整校验订单，任何一项不通过抛出带错误代码的 ValueError。
        """
        # 0. 对象存在性
        if order is None:
            raise ValueError(self._msg(ERR_SYMBOL_MISSING, "订单对象为空", "Order object is None"))

        # 1. 基本字段
        if not order.symbol or not order.symbol.strip():
            raise ValueError(self._msg(ERR_SYMBOL_MISSING, "订单缺少交易对", "Order missing symbol"))
        if not order.order_type:
            raise ValueError(self._msg(ERR_TYPE_MISSING, "订单缺少类型", "Order missing type"))

        # 2. 方向标准化
        try:
            direction = OrderDirection.from_string(order.direction)
        except ValueError as e:
            raise ValueError(self._msg(ERR_DIRECTION_INVALID, str(e), str(e))) from e
        order.direction = direction.value  # 统一为 'BUY' 或 'SELL'

        # 3. 订单类型校验
        ot = order.order_type.lower()
        allowed_types = {'limit', 'market', 'stop_limit', 'stop_market'}
        if ot not in allowed_types:
            raise ValueError(self._msg(ERR_TYPE_UNSUPPORTED,
                                       f"不支持的订单类型: {order.order_type}，允许: {allowed_types}",
                                       f"Unsupported order type: {order.order_type}. Allowed: {allowed_types}"))

        # 4. 止损单必须提供 stop_price
        if ot in ('stop_limit', 'stop_market'):
            if order.stop_price is None or order.stop_price <= 0:
                raise ValueError(self._msg(ERR_STOP_PRICE_MISSING,
                                           "止损单必须提供有效的 stop_price",
                                           "Stop orders must have valid stop_price"))
        else:
            if order.stop_price is not None and order.stop_price > 0:
                logger.warning(self._msg(ERR_STOP_PRICE_MISSING,
                                         f"订单类型 {ot} 不需要 stop_price，该字段将被忽略",
                                         f"stop_price provided for {ot} order, will be ignored"))

        # 市价单包含价格仅警告
        if ot == 'market' and order.price is not None and order.price > 0:
            logger.warning("市价单提供了价格字段，将被忽略")

        # 5. 获取交易对规则
        rules = await self._get_rules(order.symbol)

        # 6. 数量校验
        qty = self._safe_decimal(order.quantity, "订单数量")
        if qty <= 0:
            raise ValueError(self._msg(ERR_QTY_ZERO, "订单数量必须大于零", "Order quantity must be positive"))
        self._check_finite(qty, "订单数量")

        min_qty = self._safe_decimal(rules.get('min_qty', '0'), "最小交易量")
        max_qty = self._safe_decimal(rules.get('max_qty', '10000000'), "最大交易量")
        if max_qty <= 0:
            raise ValueError(self._msg(ERR_RULES_INCONSISTENT, "规则异常：最大交易量必须为正数", "max_qty must be positive"))
        if min_qty > max_qty:
            raise ValueError(self._msg(ERR_RULES_INCONSISTENT, "规则异常：最小交易量不能大于最大交易量", "min_qty > max_qty"))

        if qty < min_qty:
            raise ValueError(self._msg(ERR_QTY_BELOW_MIN, f"订单数量 {qty} 低于最小交易量 {min_qty}",
                                       f"Order quantity {qty} below min {min_qty}"))
        if qty > max_qty:
            raise ValueError(self._msg(ERR_QTY_ABOVE_MAX, f"订单数量 {qty} 超过最大交易量 {max_qty}",
                                       f"Order quantity {qty} exceeds max {max_qty}"))

        # 7. 数量步长校验
        step_size = self._safe_decimal(rules.get('step_size', '1e-8'), "步长")
        if step_size > 0:
            original_qty = qty
            qty = self._adjust_quantity(qty, step_size)
            if qty != original_qty:
                if self._log_sensitive:
                    logger.debug(self._msg("", f"数量自动调整: {original_qty} -> {qty}",
                                           f"Qty adjusted: {original_qty} -> {qty}"))
            if qty < min_qty:
                raise ValueError(self._msg(ERR_QTY_STEP, f"数量调整后 {qty} 低于最小交易量 {min_qty}",
                                           f"Adjusted qty {qty} below min {min_qty}"))
            if qty > max_qty:
                raise ValueError(self._msg(ERR_QTY_STEP, f"数量调整后 {qty} 超过最大交易量 {max_qty}",
                                           f"Adjusted qty {qty} exceeds max {max_qty}"))
            order.quantity = float(qty)

        # 8. 价格校验
        price = None
        if ot in ('limit', 'stop_limit') and order.price is not None:
            price = self._safe_decimal(order.price, "订单价格")
            if price <= 0:
                raise ValueError(self._msg(ERR_PRICE_ZERO, "限价单价格必须大于零", "Limit order price must be positive"))
            self._check_finite(price, "订单价格")

            tick_size = self._safe_decimal(rules.get('tick_size', '0.01'), "最小价格变动")
            if tick_size > 0:
                adjusted_price = self._adjust_price(price, tick_size, direction)
                if adjusted_price != price:
                    deviation = abs(adjusted_price - price) / price
                    if deviation > self._max_price_adj_ratio:
                        raise ValueError(self._msg(ERR_PRICE_ADJUST,
                                                   f"限价单价格 {price} 不符合最小变动单位 {tick_size}，"
                                                   f"自动调整后为 {adjusted_price}，偏差 {deviation:.4%} 超出允许范围",
                                                   f"Price {price} doesn't match tick size {tick_size}, "
                                                   f"adjusted to {adjusted_price}, deviation {deviation:.4%} exceeds limit"))
                    if self._log_sensitive:
                        logger.info(self._msg("", f"价格自动调整: {price} -> {adjusted_price} (偏差 {deviation:.4%})",
                                              f"Price adjusted: {price} -> {adjusted_price} (deviation {deviation:.4%})"))
                    order.price = float(adjusted_price)
                    price = adjusted_price
                if adjusted_price <= 0:
                    raise ValueError(self._msg(ERR_PRICE_TICK, "调整后价格必须大于零", "Adjusted price must be positive"))
        elif ot in ('limit', 'stop_limit') and order.price is None:
            raise ValueError(self._msg(ERR_PRICE_ZERO, "限价单必须提供价格", "Limit order requires a price"))

        # 9. 止损价格校验（步长）
        if ot in ('stop_limit', 'stop_market') and order.stop_price is not None:
            stop_price = self._safe_decimal(order.stop_price, "止损价格")
            if stop_price <= 0:
                raise ValueError(self._msg(ERR_STOP_PRICE_MISSING, "止损价格必须大于零", "Stop price must be positive"))
            tick_size = self._safe_decimal(rules.get('tick_size', '0.01'), "最小价格变动")
            adjusted_stop = self._adjust_price(stop_price, tick_size, direction)
            if adjusted_stop != stop_price:
                order.stop_price = float(adjusted_stop)

        # 10. 名义价值
        if price is not None:
            notional = qty * price
        else:
            last_price_val = rules.get('last_price', '0')
            last_price = self._safe_decimal(last_price_val, "估算价格")
            if last_price <= 0:
                raise ValueError(self._msg(ERR_NO_LAST_PRICE, "无法获取最新价格，无法校验名义价值",
                                           "Last price unavailable"))
            notional = qty * last_price

        min_notional = self._safe_decimal(rules.get('min_notional', '0'), "最小名义价值")
        if min_notional > 0 and notional < min_notional:
            msg = self._msg(ERR_NOTIONAL_BELOW_MIN,
                            f"订单名义价值 {notional:.2f} 低于最小要求 {min_notional:.2f}",
                            f"Notional {notional:.2f} below minimum {min_notional:.2f}")
            if self._skip_below_min:
                raise ValueError(msg)
            else:
                logger.warning(msg + self._msg("", "，但已配置为跳过拒绝", ", skipping rejection per config"))

        # 11. 客户端订单 ID
        if order.client_order_id and (len(order.client_order_id) > 64 or not re.match(r'^[\w\-]+$', order.client_order_id)):
            raise ValueError(self._msg(ERR_CLIENT_ID_LENGTH,
                                       "client_order_id 长度不超过64且仅允许字母数字下划线连字符",
                                       "client_order_id max 64 chars, alphanumeric, underscore, hyphen only"))

        logger.debug(self._msg("", "订单校验通过", "Order validation passed"))

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _normalize_direction(self, direction: str) -> OrderDirection:
        return OrderDirection.from_string(direction)

    def _adjust_quantity(self, qty: Decimal, step: Decimal) -> Decimal:
        if self._qty_rounding == 'floor':
            return (qty // step) * step
        elif self._qty_rounding == 'ceil':
            return -(-qty // step) * step
        elif self._qty_rounding == 'round':
            return (qty / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        raise ValueError(f"不支持的 qty_rounding: {self._qty_rounding}")

    def _adjust_price(self, price: Decimal, tick: Decimal, direction: OrderDirection) -> Decimal:
        if self._price_rounding == 'floor_for_buy_ceil_for_sell':
            if direction == OrderDirection.BUY:
                return (price // tick) * tick
            else:
                return -(-price // tick) * tick
        elif self._price_rounding == 'floor':
            return (price // tick) * tick
        elif self._price_rounding == 'ceil':
            return -(-price // tick) * tick
        raise ValueError(f"不支持的 price_rounding: {self._price_rounding}")

    async def _get_rules(self, symbol: str) -> Dict:
        async with self._lock:
            now = time.monotonic_ns()
            if symbol in self._rules_cache:
                ts = self._cache_timestamps.get(symbol, 0)
                if now - ts < self._cache_ttl_sec * 1_000_000_000:
                    return self._rules_cache[symbol]

        # 释放锁获取新规则
        rules = None
        if self._rules_provider:
            try:
                rules = self._rules_provider(symbol)
            except Exception as e:
                logger.error(f"获取交易对规则失败 ({symbol}): {e}，使用保守默认值", exc_info=True)

        if not rules:
            logger.warning(f"使用保守默认规则: {symbol}")
            rules = {
                'min_qty': '0.0001', 'max_qty': '10000',
                'step_size': '0.000001', 'tick_size': '0.01',
                'min_notional': '10.0', 'last_price': '0.0',
            }

        # 补齐可能缺失的键
        defaults = {'min_qty': '0', 'max_qty': '10000000', 'step_size': '0',
                    'tick_size': '0.01', 'min_notional': '0', 'last_price': '0'}
        for k, v in defaults.items():
            if k not in rules:
                rules[k] = v

        async with self._lock:
            if len(self._rules_cache) >= self._cache_max_size:
                try:
                    oldest = min(self._cache_timestamps, key=self._cache_timestamps.get)
                    del self._rules_cache[oldest]
                    del self._cache_timestamps[oldest]
                    logger.info(f"缓存淘汰: {oldest}")
                except ValueError:
                    pass
            self._rules_cache[symbol] = rules
            self._cache_timestamps[symbol] = time.monotonic_ns()

        return rules

    @staticmethod
    def _safe_decimal(value, name: str = "数值") -> Decimal:
        if value is None:
            raise ValueError(f"{name} 为 None")
        try:
            if isinstance(value, Decimal):
                return value
            return Decimal(str(value))
        except Exception as e:
            raise ValueError(f"无效的{name}: {value}") from e

    @staticmethod
    def _check_finite(value: Decimal, name: str) -> None:
        if not value.is_finite():
            raise ValueError(f"{name} 不是有限数值: {value}")

    def _msg(self, code: str, zh: str, en: str) -> str:
        """
        格式化错误消息，附加错误代码。
        根据语言选择中文或英文，格式: [CODE] 消息
        """
        text = zh if self.lang == 'zh' else en
        return f"[{code}] {text}"
