# -*- coding: utf-8 -*-
"""
模块名称: slippage_estimator.py
核心职责: 基于订单簿深度、近期波动率与历史滑点EMA，动态预估订单滑点并提供滑点保护，适配不同账户规模。
所属层级: core.execution

外部依赖:
    - asyncio, math, logging, time (标准库)
    - typing (类型注解)
    - core.models.order (Order, OrderDirection 枚举)
    - core.models.orderbook (OrderBook 模型)

接口契约:
    提供: {
        'SlippageEstimator': {
            'estimate(order, orderbook, recent_volatility, ema_slippage, context) -> float': '返回预估滑点百分比',
            'apply_slippage_guard(order, orderbook, context) -> Order': '在订单上设置最大滑点限制，必要时转为限价单',
            'update_ema(actual_slippage) -> None': '更新历史滑点EMA',
            'reset_ema(value=0.001) -> None': '重置EMA至初始值',
            'current_ema_slippage': 'float, 当前EMA滑点（只读属性）'
        }
    }
    消费: {
        'core.models.order.Order': '订单领域模型',
        'core.models.orderbook.OrderBook': '订单簿快照'
    }

配置项:
    - execution.slippage.model (str, 'dynamic'): 'fixed' 或 'dynamic'
    - execution.slippage.max_slippage_pct (float, 0.1): 最大允许滑点百分比
    - execution.slippage.max_dynamic_slippage_pct (float, 1.0): 动态模型输出硬上限
    - execution.slippage.fixed.slippage_pct (float, 0.05): 固定模型滑点
    - execution.slippage.dynamic.weights (dict): 三项权重 'orderbook_depth', 'volatility', 'recent_slippage_ema'，默认和为1
    - execution.slippage.dynamic.ema_halflife_trades (int, 50): EMA半衰期（成交笔数）
    - execution.slippage.dynamic.orderbook_depth_levels (int, 10): 使用的深度档数
    - execution.slippage.override_on_volatility (bool, True): 极端波动时是否放宽
    - execution.slippage.vol_slippage_factor (float, 0.1): 波动率转换为滑点的乘数
    - execution.slippage.max_volatility_slippage (float, 0.05): 波动率估算滑点上限
    - execution.slippage.small_account_threshold (float, 3000.0): 小账户净值阈值（美元）
    - execution.slippage.small_account_max_slippage (float, 0.03): 小账户最大滑点

作者: KHAOS Execution Team
创建日期: 2025-06-15
最后修改: 2026-01-16 华尔街级第二轮深度审计 (v3.0)
"""

import asyncio
import logging
import math
import time
from typing import Dict, Optional, Union

from core.models.order import Order, OrderDirection
from core.models.orderbook import OrderBook

logger = logging.getLogger(__name__)

# 默认常量
DEFAULT_EMA_ALPHA = 0.01386  # 对应 halflife=50 时的衰减因子
MIN_EMA_HALFLIFE = 1
DEFAULT_MAX_VOLATILITY_SLIPPAGE = 0.05
DEFAULT_VOL_SLIPPAGE_FACTOR = 0.1
DEFAULT_ORDERBOOK_MAX_AGE_MS = 500  # 订单簿过期阈值


class SlippageEstimator:
    """
    滑点预估器，支持固定滑点与基于订单簿的动态滑点模型。
    自动适配2000美金至万亿美金账户，小账户会收紧最大允许滑点以保护本金。
    """

    def __init__(self,
                 model: str = 'dynamic',
                 max_slippage_pct: float = 0.1,
                 fixed_slippage_pct: float = 0.05,
                 dynamic_weights: Optional[Dict[str, float]] = None,
                 max_dynamic_slippage_pct: float = 1.0,
                 orderbook_depth_levels: int = 10,
                 ema_halflife_trades: int = 50,
                 override_on_volatility: bool = True,
                 small_account_threshold: float = 3000.0,
                 small_account_max_slippage: float = 0.03,
                 vol_slippage_factor: float = DEFAULT_VOL_SLIPPAGE_FACTOR,
                 max_volatility_slippage: float = DEFAULT_MAX_VOLATILITY_SLIPPAGE,
                 verbose: bool = False):
        """
        初始化滑点预估器，所有参数将进行范围校验。
        """
        # 参数校验
        if max_slippage_pct <= 0 or max_slippage_pct > 1:
            raise ValueError(f"max_slippage_pct 必须在 (0, 1] 之间，当前: {max_slippage_pct}")
        if fixed_slippage_pct <= 0 or fixed_slippage_pct > 1:
            raise ValueError(f"fixed_slippage_pct 必须在 (0, 1] 之间，当前: {fixed_slippage_pct}")
        if max_dynamic_slippage_pct <= 0 or max_dynamic_slippage_pct > 1:
            raise ValueError(f"max_dynamic_slippage_pct 必须在 (0, 1] 之间，当前: {max_dynamic_slippage_pct}")

        self.model = model if model in ('fixed', 'dynamic') else 'dynamic'
        self.max_slippage_pct = max_slippage_pct
        self.fixed_slippage_pct = fixed_slippage_pct
        self.max_dynamic_slippage_pct = max_dynamic_slippage_pct
        self.orderbook_depth_levels = max(1, orderbook_depth_levels)
        self.override_on_volatility = override_on_volatility
        self.vol_slippage_factor = vol_slippage_factor
        self.max_volatility_slippage = max_volatility_slippage
        self.verbose = verbose

        # 动态权重归一化 (确保和为1)
        default_weights = {'orderbook_depth': 0.5, 'volatility': 0.3, 'recent_slippage_ema': 0.2}
        self.weights = dynamic_weights if dynamic_weights else default_weights
        w_sum = sum(self.weights.values())
        if w_sum > 0:
            self.weights = {k: v / w_sum for k, v in self.weights.items()}
        else:
            self.weights = default_weights

        # EMA 参数
        self.ema_halflife = max(1, ema_halflife_trades)
        if self.ema_halflife <= 0:
            self._ema_alpha = 1.0  # 完全使用新值
        else:
            self._ema_alpha = 1 - math.exp(math.log(0.5) / self.ema_halflife)
        self._ema_slippage: float = 0.001  # 初始保守滑点0.1%

        # 小账户适配：确保小账户最大滑点不超过全局限制
        self.small_account_threshold = small_account_threshold
        self.small_account_max_slippage = min(max_slippage_pct, small_account_max_slippage)

        # 并发锁
        self._ema_lock = asyncio.Lock()

        logger.info(
            f"滑点预估器初始化: model={self.model}, max_slippage={self.max_slippage_pct:.2%}, "
            f"small_account_max={self.small_account_max_slippage:.2%}, ema_halflife={self.ema_halflife}"
        )

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def estimate(self,
                 order: Order,
                 orderbook: Optional[OrderBook] = None,
                 recent_volatility: Optional[float] = None,
                 ema_slippage: Optional[float] = None,
                 context: Optional[dict] = None) -> float:
        """
        返回预估滑点百分比（0.01 = 1%）。若参数无效则返回 0。

        Args:
            order: 订单对象
            orderbook: 当前订单簿快照
            recent_volatility: 近期波动率（如 ATR/价格，建议使用相对波动率）
            ema_slippage: 外部提供的 EMA 值，若为 None 则使用内部值
            context: 可选的上下文字典，可能包含 'equity', 'orderbook_timestamp', 'tick_size' 等

        Returns:
            预估滑点百分比
        """
        if not order or order.quantity <= 0:
            return 0.0

        # 方向有效性检查
        if order.direction not in (OrderDirection.LONG, OrderDirection.SHORT):
            logger.warning(f"未知的订单方向: {order.direction}")
            return 0.0

        if self.model == 'fixed':
            # 固定模型也受小账户和全局限制
            effective_max = self._effective_max_slippage(context)
            return min(self.fixed_slippage_pct, effective_max)

        # 动态模型
        try:
            depth_est = self._estimate_from_orderbook(order, orderbook)
        except Exception as e:
            logger.error(f"订单簿滑点估算失败，使用保守值: {e}")
            depth_est = self.max_slippage_pct  # 保守

        vol_est = self._estimate_from_volatility(recent_volatility)
        ema_val = ema_slippage if ema_slippage is not None else self._ema_slippage

        weighted = (self.weights.get('orderbook_depth', 0.5) * depth_est +
                    self.weights.get('volatility', 0.3) * vol_est +
                    self.weights.get('recent_slippage_ema', 0.2) * ema_val)

        # 钳位到动态模型硬上限和全局上限
        effective_max = self._effective_max_slippage(context)
        return min(weighted, self.max_dynamic_slippage_pct, effective_max)

    def apply_slippage_guard(self, order: Order,
                             orderbook: Optional[OrderBook] = None,
                             context: Optional[dict] = None) -> Order:
        """
        为订单应用滑点保护。对于市价单，如果预估滑点超过允许值，则转为限价单并设定合理限价。
        小账户会自动降低最大滑点容忍度。

        Args:
            order: 待保护的订单
            orderbook: 当前订单簿快照
            context: 包含 'equity', 'recent_volatility', 'ema_slippage', 'tick_size', 'orderbook_timestamp' 的可选字典

        Returns:
            修改后的订单（会直接修改传入的order对象）
        """
        # 仅对市价单进行转换保护
        if order.order_type != 'MARKET':
            return order

        # 确定有效最大滑点（考虑小账户）
        effective_max = self._effective_max_slippage(context)

        recent_vol = context.get('recent_volatility') if context else None
        ema_slip = context.get('ema_slippage') if context else None
        est_slippage = self.estimate(order, orderbook, recent_vol, ema_slip, context)

        if est_slippage <= effective_max:
            if self.verbose:
                logger.debug(f"订单 {order.client_order_id}: 预估滑点 {est_slippage:.4%} 在阈值内，保持市价单")
            return order

        # 滑点超限，转为限价单
        logger.info(f"订单 {order.client_order_id}: 市价单预估滑点 {est_slippage:.4%} 超过上限 {effective_max:.4%}，转为限价单")

        # 获取 tick_size，默认 0.01
        tick_size = context.get('tick_size', 0.01) if context else 0.01

        # 计算限价
        if orderbook and not self._is_orderbook_stale(orderbook, context):
            if order.direction == OrderDirection.LONG:
                ref_price = getattr(orderbook, 'best_ask', None)
                if ref_price is not None and ref_price > 0:
                    order.price = ref_price * (1 + effective_max)
                else:
                    order.price = self._compute_limit_price(order, effective_max)
            else:  # SHORT
                ref_price = getattr(orderbook, 'best_bid', None)
                if ref_price is not None and ref_price > 0:
                    order.price = ref_price * (1 - effective_max)
                else:
                    order.price = self._compute_limit_price(order, effective_max)
        else:
            if orderbook and self._is_orderbook_stale(orderbook, context):
                logger.warning(f"订单 {order.client_order_id}: 订单簿过期，使用保守限价")
            order.price = self._compute_limit_price(order, effective_max)

        # 价格圆整至 tick_size
        if order.price is not None and order.price > 0:
            order.price = self._round_to_tick_size(order.price, tick_size)
        else:
            logger.error(f"订单 {order.client_order_id}: 转换限价单失败，无法确定价格，保持市价单")
            return order

        order.order_type = 'LIMIT'
        # 确保时间有效
        if not hasattr(order, 'time_in_force') or not order.time_in_force:
            order.time_in_force = 'GTC'

        logger.info(f"订单 {order.client_order_id}: 已转为限价单, price={order.price:.2f}, TIF=GTC")
        return order

    async def update_ema(self, actual_slippage: float) -> None:
        """根据实际成交滑点更新内部 EMA。线程安全。"""
        clamped = max(0.0, min(1.0, actual_slippage))
        async with self._ema_lock:
            self._ema_slippage = self._ema_alpha * clamped + (1 - self._ema_alpha) * self._ema_slippage

    async def reset_ema(self, value: float = 0.001) -> None:
        """重置 EMA 至给定值（默认 0.1%）。"""
        clamped = max(0.0, min(1.0, value))
        async with self._ema_lock:
            self._ema_slippage = clamped
        logger.info(f"滑点 EMA 已重置为 {clamped:.4%}")

    @property
    async def current_ema_slippage(self) -> float:
        """返回当前 EMA 滑点值（只读）。"""
        async with self._ema_lock:
            return self._ema_slippage

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _estimate_from_orderbook(self, order: Order, orderbook: Optional[OrderBook]) -> float:
        """基于订单簿深度计算平均成交价滑点。"""
        if not orderbook or order.quantity <= 0:
            return 0.0

        if order.direction == OrderDirection.LONG:
            levels = orderbook.asks
        elif order.direction == OrderDirection.SHORT:
            levels = orderbook.bids
        else:
            return 0.0

        # 过滤非法价格
        levels = [l for l in levels[:self.orderbook_depth_levels] if getattr(l, 'price', 0) > 0]
        if not levels:
            return 0.0

        remaining = float(order.quantity)
        total_cost = 0.0
        total_qty = 0.0

        # 中价
        best_bid = getattr(orderbook, 'best_bid', None)
        best_ask = getattr(orderbook, 'best_ask', None)
        if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0 and best_bid <= best_ask:
            mid_price = (best_bid + best_ask) / 2.0
        else:
            mid_price = levels[0].price if levels else 0.0

        if mid_price <= 0:
            return 0.0

        for level in levels:
            fill_qty = min(remaining, getattr(level, 'volume', 0.0))
            total_cost += fill_qty * level.price
            total_qty += fill_qty
            remaining -= fill_qty
            if remaining <= 0:
                break

        if total_qty == 0:
            return 0.0

        avg_price = total_cost / total_qty
        slippage = (avg_price - mid_price) / mid_price if order.direction == OrderDirection.LONG else (mid_price - avg_price) / mid_price
        return max(0.0, slippage)

    def _estimate_from_volatility(self, recent_volatility: Optional[float]) -> float:
        """基于近期波动率估算滑点。volatility 建议为相对值（如 0.02 表示 2%）。"""
        if recent_volatility is None or recent_volatility <= 0:
            return 0.0
        return min(recent_volatility * self.vol_slippage_factor, self.max_volatility_slippage)

    def _effective_max_slippage(self, context: Optional[dict]) -> float:
        """根据账户规模计算实际允许的最大滑点。"""
        effective = self.max_slippage_pct
        if context:
            equity = context.get('equity')
            if equity is not None and 0 < equity < self.small_account_threshold:
                effective = min(effective, self.small_account_max_slippage)
        return max(0.0, min(effective, 1.0))

    def _compute_limit_price(self, order: Order, effective_max: float) -> float:
        """无订单簿时根据原始价格计算保守限价。"""
        effective_max = max(0.0, min(1.0, effective_max))
        base_price = order.price if order.price and order.price > 0 else 0.0
        if base_price <= 0:
            logger.error(f"订单 {order.client_order_id}: 无法确定基础价格")
            return 0.0
        if order.direction == OrderDirection.LONG:
            return base_price * (1 + effective_max)
        else:
            return base_price * (1 - effective_max)

    def _is_orderbook_stale(self, orderbook: OrderBook, context: Optional[dict]) -> bool:
        """检查订单簿是否过期。"""
        if not context:
            return False
        ob_timestamp = context.get('orderbook_timestamp')
        if ob_timestamp is None:
            return False
        now = time.time() * 1000  # 毫秒
        return (now - ob_timestamp) > DEFAULT_ORDERBOOK_MAX_AGE_MS

    @staticmethod
    def _round_to_tick_size(price: float, tick_size: float) -> float:
        """将价格圆整到指定 tick_size。"""
        if tick_size <= 0:
            return price
        return round(price / tick_size) * tick_size

    def __repr__(self) -> str:
        return (f"SlippageEstimator(model={self.model}, max_slippage={self.max_slippage_pct:.2%}, "
                f"ema_slippage={self._ema_slippage:.4%})")
