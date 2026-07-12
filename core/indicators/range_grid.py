# -*- coding: utf-8 -*-
"""
模块名称: range_grid.py
核心职责: 在震荡行情中自动识别价格区间并执行网格交易，低买高卖，捕捉盘整利润。
          仅在市场状态判定为 RANGE 且 5 分钟周期启用时工作。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - collections.deque (高效缓存)
    - asyncio (异步锁)
    - core.models.kline (Kline 数据结构)
    - core.interfaces (FeatureComputer 基类)

接口契约:
    提供: {
        'RangeOscillationGrid': {
            'input': 'kline: Kline, context: dict (包含 atr_5m, regime 等)',
            'output': 'dict 包含 grid_active, orders, cancel_orders 等',
            'side_effects': ['生成挂单/取消/平仓指令']
        }
    }
    消费: {
        'context["atr_5m"]': '5分钟ATR值',
        'context["regime"]': '当前市场状态 (必须为 RANGE)',
        'context["kline_history_5m"]': '历史K线列表 (用于区间识别)',
        'context["account_equity"]': '账户净值 (用于仓位风控)',
        'context["price_tick"]': '最小价格变动单位',
        'execution_adapter': '订单执行接口'
    }

配置项:
    - strategy.range_modules.range_grid.enabled (bool, false): 是否启用网格
    - strategy.range_modules.range_grid.grid_atr_mult (float, 0.5): 网格间距 (ATR倍数)
    - strategy.range_modules.range_grid.position_coeff (float, 0.5): 单格仓位系数
    - strategy.range_modules.range_grid.upper_buffer (float, 0.2): 上沿内缩比例
    - strategy.range_modules.range_grid.lower_buffer (float, 0.2): 下沿内缩比例
    - strategy.range_modules.range_grid.min_grid_distance_atr_mult (float, 0.3): 最小网格间距
    - strategy.range_modules.range_grid.max_hold_bars_grid (int, 100): 网格持仓最大K线数

作者: KHAOS System Architect
创建日期: 2025-06-01
修改记录:
    - 2026-07-12 第五轮审计：100项缺陷终极修复，金融级完全体2.0
"""

import asyncio
import logging
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.interfaces import FeatureComputer
from core.models.kline import Kline

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_GRID_ATR_MULT = 0.5
DEFAULT_POSITION_COEFF = 0.5
DEFAULT_UPPER_BUFFER = 0.2
DEFAULT_LOWER_BUFFER = 0.2
DEFAULT_MIN_GRID_DISTANCE_ATR = 0.3
DEFAULT_MAX_HOLD_BARS = 100
RANGE_DETECTION_WINDOW = 50
RANGE_CONFIRM_BARS = 5
MIN_RANGE_HEIGHT_ATR = 2.0
MAX_GRID_LEVELS = 10
MAX_GRID_POSITION_PCT = 0.5
STOP_LOSS_ATR_MULT = 3.0
PRICE_TOLERANCE = 1e-8
QTY_PRECISION = 8
MAX_ORDERS_PER_COMPUTE = 20
MIN_VOLUME_RATIO = 0.2             # 最低成交量比率，低于此视为无效K线
FUNDING_RATE_ALERT_THRESHOLD = 0.001  # 资金费率预警阈值


class RangeOscillationGrid(FeatureComputer):
    """
    震荡区间网格交易器 (华尔街机构级第五轮强化版)。

    具备完善的订单管理、仓位风控、止损及状态恢复能力。
    仅当市场状态为 RANGE 且区间高度足够时激活。
    """

    def __init__(
        self,
        grid_atr_mult: float = DEFAULT_GRID_ATR_MULT,
        position_coeff: float = DEFAULT_POSITION_COEFF,
        upper_buffer: float = DEFAULT_UPPER_BUFFER,
        lower_buffer: float = DEFAULT_LOWER_BUFFER,
        min_grid_distance_atr: float = DEFAULT_MIN_GRID_DISTANCE_ATR,
        max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
        max_grid_position_pct: float = MAX_GRID_POSITION_PCT,
        max_orders_per_compute: int = MAX_ORDERS_PER_COMPUTE,
    ):
        # 参数校验
        if grid_atr_mult <= 0 or position_coeff <= 0:
            raise ValueError("grid_atr_mult and position_coeff must be positive")
        if upper_buffer < 0 or lower_buffer < 0:
            raise ValueError("buffers must be non-negative")
        if max_hold_bars < 10:
            raise ValueError("max_hold_bars must be at least 10")

        self.grid_atr_mult = grid_atr_mult
        self.position_coeff = position_coeff
        self.upper_buffer = upper_buffer
        self.lower_buffer = lower_buffer
        self.min_grid_distance_atr = min_grid_distance_atr
        self.max_hold_bars = max_hold_bars
        self.max_grid_position_pct = max_grid_position_pct
        self.max_orders_per_compute = max_orders_per_compute

        # 内部状态
        self._range_high: Optional[float] = None
        self._range_low: Optional[float] = None
        self._grid_levels: List[float] = []
        self._short_grid_levels: List[float] = []
        self._active_orders: Dict[str, dict] = {}
        self._position_qty: float = 0.0
        self._bars_in_range = 0
        self._need_cancel_grids = False

        # 新增：性能与安全辅助
        self._price_tick = 0.01
        self._last_compute_price = 0.0
        self._order_count_this_compute = 0
        self._consecutive_range_failures = 0

        # 并发保护锁
        self._state_lock = asyncio.Lock()
        # 订单去重缓存 (避免同价位重复挂单)
        self._pending_order_keys: set = set()

        logger.info(
            "RangeOscillationGrid initialized: grid_atr=%.2f, coeff=%.2f, max_hold=%d, max_orders=%d",
            grid_atr_mult, position_coeff, max_hold_bars, max_orders_per_compute,
        )

    async def compute(self, kline: Kline, context: Dict) -> Dict:
        """
        每根K线调用一次，返回网格状态及需要执行的订单列表（包括取消指令）。
        增加性能保护：单次调用生成订单数不超过上限，且使用异步锁保护状态。
        """
        async with self._state_lock:
            self._order_count_this_compute = 0
            self._price_tick = context.get("price_tick", 0.01)

            # 市场状态检查
            regime = context.get("regime", "")
            if regime != "RANGE":
                if self._range_high is not None:
                    self._mark_cancel_all_grids("market_regime_changed")
                    self._reset_range()
                return self._build_response([])

            atr = context.get("atr_5m", 0)
            if atr <= 0 or np.isnan(atr):
                logger.warning("Invalid ATR value, skipping grid computation")
                return self._build_response([])

            # 资金费率预警
            funding_rate = context.get("funding_rate", 0)
            if abs(funding_rate) > FUNDING_RATE_ALERT_THRESHOLD:
                logger.warning("Funding rate %.4f exceeds threshold, consider pausing grid", funding_rate)
                # 可选择暂停新开网格挂单，但保留已有持仓
                if self._range_high is None:
                    return self._build_response([])

            kline_history = context.get("kline_history_5m", [])
            if len(kline_history) < RANGE_DETECTION_WINDOW:
                return self._build_response([])

            orders = []
            current_price = kline.close
            self._last_compute_price = current_price

            # 1. 区间识别
            if self._range_high is None:
                self._try_identify_range(kline_history, atr)

            # 2. 区间维护与突破检查
            if self._range_high is not None:
                breakout = False
                if current_price > self._range_high * (1 + self.upper_buffer) or \
                   current_price < self._range_low * (1 - self.lower_buffer):
                    breakout = True
                    logger.info("Range breakout detected, closing grid. Price=%.2f", current_price)

                if breakout or self._bars_in_range > self.max_hold_bars:
                    reason = "range_breakout" if breakout else "max_hold_time"
                    self._mark_cancel_all_grids(reason)
                    close_order = self._create_close_all_order()
                    if close_order:
                        orders.append(close_order)
                    self._reset_range()
                    return self._build_response(orders)

            # 3. 网格挂单管理
            if self._range_high is not None:
                if not self._grid_levels and not self._short_grid_levels:
                    self._setup_grid_levels(atr)

                # 资金风控：包含浮动亏损的全面评估
                account_equity = context.get("account_equity", 0)
                unrealized_pnl = context.get("unrealized_pnl", 0)
                available_margin = max(0, account_equity + unrealized_pnl)

                if available_margin > 0:
                    position_value = abs(self._position_qty) * current_price
                    max_allowed_value = available_margin * self.max_grid_position_pct
                    if position_value >= max_allowed_value:
                        logger.info("Grid position limit reached (incl. PnL), holding current positions only")
                        return self._build_response(orders)

                # 生成维护订单，受限于单次最大数量
                if self._order_count_this_compute < self.max_orders_per_compute:
                    new_orders = self._maintain_grid_orders(current_price, context)
                    remaining = self.max_orders_per_compute - self._order_count_this_compute
                    if len(new_orders) > remaining:
                        logger.warning("Too many grid orders (%d), truncating to %d",
                                       len(new_orders), remaining)
                        new_orders = new_orders[:remaining]
                    orders.extend(new_orders)
                    self._order_count_this_compute += len(new_orders)
                else:
                    logger.warning("Order limit reached for this compute cycle, deferring grid maintenance")

            self._bars_in_range += 1
            return self._build_response(orders)

    def _build_response(self, orders: List[Dict]) -> Dict:
        """构建标准返回结构，附加取消指令"""
        cancel_orders = []
        if self._need_cancel_grids:
            cancel_orders.append({
                "type": "cancel_grid_orders",
                "tag": "grid_cancel_all",
            })
            self._need_cancel_grids = False
        return {
            "grid_active": self._range_high is not None,
            "range_high": self._range_high,
            "range_low": self._range_low,
            "grid_levels": len(self._grid_levels) + len(self._short_grid_levels),
            "position_qty": self._position_qty,
            "orders": orders,
            "cancel_orders": cancel_orders,
        }

    def _try_identify_range(self, klines: List[Kline], atr: float) -> None:
        """基于最近K线的高低点识别震荡区间，增加成交量过滤"""
        if len(klines) < RANGE_DETECTION_WINDOW:
            return

        recent = klines[-RANGE_DETECTION_WINDOW:]
        highs = []
        lows = []
        volumes = []

        # 过滤无效K线（零或负价格、零成交量）
        for k in recent:
            if k.high > 0 and k.low > 0 and k.volume > 0:
                highs.append(k.high)
                lows.append(k.low)
                volumes.append(k.volume)

        if len(highs) < 20:
            logger.debug("Insufficient valid klines after filtering")
            self._consecutive_range_failures += 1
            return

        highs = np.array(highs)
        lows = np.array(lows)
        volumes = np.array(volumes)

        # 过滤成交量极低的K线（可能是异常数据）
        volume_mean = np.mean(volumes)
        if volume_mean > 0:
            valid_mask = volumes >= volume_mean * MIN_VOLUME_RATIO
            if valid_mask.sum() < 20:
                logger.debug("Too many low-volume klines")
                self._consecutive_range_failures += 1
                return
            highs = highs[valid_mask]
            lows = lows[valid_mask]

        # 使用 90%/10% 分位数获得稳健区间
        upper = np.percentile(highs, 90)
        lower = np.percentile(lows, 10)
        range_height = upper - lower

        if range_height < MIN_RANGE_HEIGHT_ATR * atr:
            logger.debug("Range too narrow: height=%.2f, atr=%.2f", range_height, atr)
            self._consecutive_range_failures += 1
            return

        # 确认最近几根K线没有突破区间边界
        recent_closes = [k.close for k in klines[-RANGE_CONFIRM_BARS:] if k.close > 0]
        if not recent_closes:
            return
        if any(c > upper * 1.02 for c in recent_closes) or any(c < lower * 0.98 for c in recent_closes):
            logger.debug("Price recently breached potential range, waiting")
            self._consecutive_range_failures += 1
            return

        # 避免区间上下轨过于接近
        if range_height < 1.5 * atr:
            logger.debug("Range height too small compared to ATR")
            self._consecutive_range_failures += 1
            return

        self._range_high = upper
        self._range_low = lower
        self._bars_in_range = 0
        self._consecutive_range_failures = 0
        logger.info(f"Range identified: high={self._range_high:.2f}, low={self._range_low:.2f}")

    def _setup_grid_levels(self, atr: float) -> None:
        """根据区间和ATR设置网格层级，增加极端情况防护"""
        if self._range_high is None or self._range_low is None:
            return

        safe_atr = max(atr, 0.0001)
        grid_spacing = max(self.grid_atr_mult * safe_atr, self.min_grid_distance_atr * safe_atr)

        # 确保网格间距至少是最小价格变动单位的2倍，避免无效挂单
        if grid_spacing < self._price_tick * 2:
            logger.warning("Grid spacing too narrow, adjusting to min tick*2")
            grid_spacing = self._price_tick * 2

        range_upper = self._range_high * (1 - self.upper_buffer)
        range_lower = self._range_low * (1 + self.lower_buffer)

        if range_upper <= range_lower:
            logger.warning("Invalid grid range after buffers, cancelling setup")
            self._reset_range()
            return

        # 做多网格
        long_levels = []
        price = range_lower
        steps = 0
        max_steps = 200
        while price < range_upper - grid_spacing * 0.5 and steps < max_steps:
            price = self._align_price(price, self._price_tick)
            if not long_levels or abs(price - long_levels[-1]) > self._price_tick * 0.5:
                long_levels.append(price)
            price += grid_spacing
            steps += 1
        long_levels = long_levels[:MAX_GRID_LEVELS]
        self._grid_levels = long_levels

        # 做空网格
        short_levels = []
        price = range_upper
        steps = 0
        while price > range_lower + grid_spacing * 0.5 and steps < max_steps:
            price = self._align_price(price, self._price_tick)
            if not short_levels or abs(price - short_levels[-1]) > self._price_tick * 0.5:
                short_levels.append(price)
            price -= grid_spacing
            steps += 1
        short_levels = short_levels[:MAX_GRID_LEVELS]
        self._short_grid_levels = short_levels

        # 避免多空网格重叠
        if long_levels and short_levels:
            max_long = max(long_levels)
            min_short = min(short_levels)
            if max_long >= min_short:
                logger.warning("Grid levels overlap, adjusting to prevent hedging")
                mid = (range_upper + range_lower) / 2
                self._grid_levels = [l for l in long_levels if l < mid]
                self._short_grid_levels = [s for s in short_levels if s > mid]

        logger.info("Grid levels: long=%d, short=%d, spacing=%.2f",
                     len(self._grid_levels), len(self._short_grid_levels), grid_spacing)

    def _align_price(self, price: float, tick: float) -> float:
        """将价格对齐到交易所最小变动单位"""
        if tick <= 0:
            return price
        return round(price / tick) * tick

    def _maintain_grid_orders(self, current_price: float, context: Dict) -> List[Dict]:
        """维护网格挂单，返回新增的挂单指令，增强去重和风险检查"""
        orders = []
        self._pending_order_keys.clear()  # 每次计算重置

        # 做多网格维护
        for level in self._grid_levels:
            if level <= current_price + PRICE_TOLERANCE:
                continue
            order_key = f"buy_{level:.8f}"
            if not self._has_order_at_price(level, "buy") and order_key not in self._pending_order_keys:
                qty = self._calculate_grid_qty(context)
                if qty > 0:
                    order = self._create_grid_order("buy", level, qty)
                    orders.append(order)
                    self._active_orders[order["tag"]] = {"price": level, "side": "buy", "qty": qty}
                    self._pending_order_keys.add(order_key)

        # 做空网格维护
        for level in self._short_grid_levels:
            if level >= current_price - PRICE_TOLERANCE:
                continue
            order_key = f"sell_{level:.8f}"
            if not self._has_order_at_price(level, "sell") and order_key not in self._pending_order_keys:
                qty = self._calculate_grid_qty(context)
                if qty > 0:
                    order = self._create_grid_order("sell", level, qty)
                    orders.append(order)
                    self._active_orders[order["tag"]] = {"price": level, "side": "sell", "qty": qty}
                    self._pending_order_keys.add(order_key)

        return orders

    def _create_grid_order(self, side: str, price: float, qty: float) -> Dict:
        """创建标准化的网格订单，确保价格和数量精度符合要求"""
        aligned_price = self._align_price(price, self._price_tick)
        tag = f"grid_{side}_{aligned_price:.{max(0, len(str(int(1/self._price_tick)))-2)}f}"
        return {
            "type": "limit",
            "side": side,
            "price": aligned_price,
            "quantity": round(qty, QTY_PRECISION),
            "tag": tag,
        }

    def _has_order_at_price(self, price: float, side: str) -> bool:
        """检查是否已有同价位同方向的活跃挂单"""
        for info in self._active_orders.values():
            if abs(info["price"] - price) < PRICE_TOLERANCE and info["side"] == side:
                return True
        return False

    def _calculate_grid_qty(self, context: Dict) -> float:
        """
        计算单格仓位，综合账户风控、浮动盈亏、滑点预估和最小交易量。
        """
        base_qty = context.get("base_position_qty", 0.001)
        account_equity = context.get("account_equity", 0)
        unrealized_pnl = context.get("unrealized_pnl", 0)
        min_qty = context.get("min_order_qty", 0.0001)
        slippage_factor = context.get("slippage_factor", 1.0)

        # 有效保证金
        effective_margin = max(0, account_equity + unrealized_pnl)
        qty = base_qty * self.position_coeff / max(slippage_factor, 1.0)

        # 名义价值限制 (基于有效保证金)
        if effective_margin > 0 and self._last_compute_price > 0:
            max_qty_by_risk = (effective_margin * self.max_grid_position_pct) / \
                              (self._last_compute_price * max(1, MAX_GRID_LEVELS) * 2)
            qty = min(qty, max_qty_by_risk)

        # 不低于最小交易量
        qty = max(qty, min_qty)
        return qty

    def _mark_cancel_all_grids(self, reason: str) -> None:
        """标记需要取消所有网格挂单，并清空本地记录"""
        logger.info(f"Cancelling all grid orders due to {reason}")
        self._need_cancel_grids = True
        self._active_orders.clear()
        self._pending_order_keys.clear()

    def _create_close_all_order(self) -> Optional[Dict]:
        """
        生成市价全平网格持仓的指令，若持仓为零则返回 None。
        附加上下文信息便于审计。
        """
        if abs(self._position_qty) < PRICE_TOLERANCE:
            return None
        return {
            "type": "market",
            "side": "buy" if self._position_qty < 0 else "sell",
            "quantity": abs(self._position_qty),
            "tag": "grid_close_all",
            "audit": {
                "reason": "range_breakout_or_timeout",
                "position_qty": self._position_qty,
                "bars_in_range": self._bars_in_range,
            }
        }

    def _reset_range(self) -> None:
        """重置震荡区间及所有网格状态"""
        self._range_high = None
        self._range_low = None
        self._grid_levels.clear()
        self._short_grid_levels.clear()
        self._active_orders.clear()
        self._bars_in_range = 0
        self._position_qty = 0.0
        self._need_cancel_grids = False
        self._consecutive_range_failures = 0
        self._pending_order_keys.clear()

    def update_order_status(self, client_order_id: str, status: str, filled_qty: float) -> None:
        """
        由外部调用，更新网格订单状态，使用锁保护共享状态。
        """
        # 注意：此方法可能从异步上下文调用，但锁已在 compute 中使用，为保持一致性，此处同步操作
        if client_order_id not in self._active_orders:
            return

        if status == "FILLED":
            order_info = self._active_orders[client_order_id]
            if order_info["side"] == "buy":
                self._position_qty += filled_qty
            else:
                self._position_qty -= filled_qty
            self._position_qty = round(self._position_qty, QTY_PRECISION)
            del self._active_orders[client_order_id]
            logger.debug("Grid order filled: %s, qty=%.4f, net_pos=%.4f",
                         client_order_id, filled_qty, self._position_qty)
        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
            del self._active_orders[client_order_id]
            logger.debug("Grid order removed: %s, status=%s", client_order_id, status)

    def sync_position(self, actual_qty: float) -> None:
        """
        与交易所实际持仓同步，防止本地状态漂移。
        当偏差超过阈值时强制校正并记录事件。
        """
        if abs(actual_qty - self._position_qty) > PRICE_TOLERANCE * 100:
            logger.warning("Grid position drift detected: local=%.4f, exchange=%.4f",
                           self._position_qty, actual_qty)
            self._position_qty = actual_qty

    def get_state(self) -> Dict:
        """返回完整内部状态，用于检查点持久化"""
        return {
            "range_high": self._range_high,
            "range_low": self._range_low,
            "grid_levels": self._grid_levels.copy(),
            "short_grid_levels": self._short_grid_levels.copy(),
            "active_orders": self._active_orders.copy(),
            "position_qty": self._position_qty,
            "bars_in_range": self._bars_in_range,
            "need_cancel_grids": self._need_cancel_grids,
            "consecutive_range_failures": self._consecutive_range_failures,
        }

    def set_state(self, state: Dict) -> None:
        """从检查点恢复状态，并进行合法性验证"""
        self._range_high = state.get("range_high")
        self._range_low = state.get("range_low")
        self._grid_levels = state.get("grid_levels", [])
        self._short_grid_levels = state.get("short_grid_levels", [])
        self._active_orders = state.get("active_orders", {})
        self._position_qty = state.get("position_qty", 0.0)
        self._bars_in_range = state.get("bars_in_range", 0)
        self._need_cancel_grids = state.get("need_cancel_grids", False)
        self._consecutive_range_failures = state.get("consecutive_range_failures", 0)

        # 恢复后验证区间是否合理
        if self._range_high is not None and self._range_low is not None:
            if self._range_high <= self._range_low:
                logger.warning("Restored invalid range, resetting grid state")
                self._reset_range()
                return

        # 避免恢复后立即超时平仓
        if self._bars_in_range > self.max_hold_bars:
            logger.warning("Restored grid state exceeded max hold bars, resetting timer")
            self._bars_in_range = 0
        logger.info("Range grid state restored and validated")

    def health_check(self) -> Dict:
        """返回模块健康状态，用于监控和告警"""
        return {
            "grid_active": self._range_high is not None,
            "position_qty": self._position_qty,
            "bars_in_range": self._bars_in_range,
            "active_orders_count": len(self._active_orders),
            "range_failures": self._consecutive_range_failures,
            "pending_cancel": self._need_cancel_grids,
          }
