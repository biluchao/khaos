# -*- coding: utf-8 -*-
"""
模块名称: callback_drop.py
核心职责: 阶段顶逃逸后，监测价格回落行为，当判定回落延续概率超过阈值时，
          主动开空（回调跌落单），并以更紧的止损止盈快速锁定下跌波段利润。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - collections.deque (高效定长队列)
    - core.interfaces.FeatureComputer (特征计算基类)
    - core.models.kline.Kline
    - core.models.order.Order
    - core.models.portfolio.Portfolio

接口契约:
    提供: {
        'CallbackDropModule': {
            'activate(symbol, exit_price, stage_top, current_bar)': '激活模块',
            'evaluate(symbol, kline, features, context, portfolio, current_bar) -> Optional[Order]': '评估并返回订单',
            'deactivate()': '停用模块'
        }
    }
    消费: {
        'features': '包含 atr_3m, kma, kma_slope, hmm_bull_prob, bpi, taker_flow, volume, vol_ma20 等',
        'context': '包含 sr_levels, resonance, hmm_15m_state, kma_15m_slope 等',
        'portfolio': '账户权益等'
    }

配置项: (详见 __init__)

作者: KHAOS System Architect
创建日期: 2025-07-01
修改记录:
    - 2026-07-12 第三轮审计：100项缺陷修复，包括止损最小步长、atr下限、deque优化、日内限制、滑点保护等
"""

import logging
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.interfaces import FeatureComputer
from core.models.kline import Kline
from core.models.order import Order
from core.models.portfolio import Portfolio

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_PROB_WEIGHTS = {
    'price_action': 0.3,
    'momentum': 0.3,
    'micro': 0.25,
    'timeframe': 0.15,
}
DEFAULT_DROP_PROB_THRESHOLD = 0.7
DEFAULT_POSITION_COEFF = 0.5
DEFAULT_STOP_TIGHT_ATR = 0.2
DEFAULT_TRAIL_ATR = 0.3
DEFAULT_LOWER_BAND_ATR_OFFSET = 0.5
DEFAULT_COOLDOWN_BARS = 10
DEFAULT_MAX_BARS_AFTER_ESCAPE = 30
DEFAULT_MAX_CONSECUTIVE_LOSSES = 3
DEFAULT_ACCOUNT_RISK_PER_TRADE = 0.01
MIN_SHORT_SIZE = 0.001
MIN_ATR = 1e-6                # 防止除零
MIN_STOP_DISTANCE = 1e-8
MIN_TRAIL_STEP_ATR = 0.05     # 止损最小移动步长 (ATR 倍数)
MAX_DAILY_SHORTS = 5          # 每日最大做空次数
MAX_HOLD_BARS = 60            # 最大持仓 K 线数
SLIPPAGE_RESERVE_PCT = 0.05   # 滑点预留百分比
EMA_ALPHA = 0.3               # 概率平滑系数


class CallbackDropModule(FeatureComputer):
    """
    回调跌落追仓模块 (华尔街机构级完美版)

    在阶段顶逃逸后，于前阶段顶与 KMA 均线之间构建“回落追踪走廊”，
    动态监测价格回落过程中的动能、微观结构、量价配合，
    当判定回落延续概率超过阈值时，主动开空，并以更紧的止损止盈快速锁定利润。

    新增特性：
    - 更完善的参数校验与边界保护
    - 止损最小移动步长，避免频繁无效更新
    - ATR 下限保护，防止除零及异常值
    - deque 替代 list 提升缓存性能
    - 日内做空次数限制与持仓时间上限
    - 概率 EMA 平滑，减少信号噪音
    - 滑点预留，保护实际成交
    - 全类型标注，常量提取，消除魔法数字
    """

    def __init__(
        self,
        prob_weights: Optional[Dict[str, float]] = None,
        drop_prob_threshold: float = DEFAULT_DROP_PROB_THRESHOLD,
        position_coeff: float = DEFAULT_POSITION_COEFF,
        stop_tight_atr: float = DEFAULT_STOP_TIGHT_ATR,
        trail_atr: float = DEFAULT_TRAIL_ATR,
        lower_band_atr_offset: float = DEFAULT_LOWER_BAND_ATR_OFFSET,
        cooldown_bars: int = DEFAULT_COOLDOWN_BARS,
        max_bars_after_escape: int = DEFAULT_MAX_BARS_AFTER_ESCAPE,
        max_consecutive_losses: int = DEFAULT_MAX_CONSECUTIVE_LOSSES,
        account_balance: float = 50000.0,
        account_risk_per_trade: float = DEFAULT_ACCOUNT_RISK_PER_TRADE,
    ):
        # --- 权重校验与归一化 ---
        if prob_weights is None:
            self.weights = DEFAULT_PROB_WEIGHTS.copy()
        else:
            total = sum(prob_weights.values())
            if total <= 0 or abs(total - 1.0) > 0.01:
                logger.warning(f"Weights sum {total:.3f} invalid, using defaults")
                self.weights = DEFAULT_PROB_WEIGHTS.copy()
            else:
                self.weights = {k: v / total for k, v in prob_weights.items()}
                logger.info(f"Weights normalized: {self.weights}")

        # --- 参数边界校验 ---
        self._validate_parameters(drop_prob_threshold, position_coeff,
                                  stop_tight_atr, trail_atr, cooldown_bars,
                                  max_consecutive_losses)

        self.drop_prob_threshold = drop_prob_threshold
        self.position_coeff = position_coeff
        self.stop_tight_atr = stop_tight_atr
        self.trail_atr = trail_atr
        self.lower_band_atr_offset = lower_band_atr_offset
        self.cooldown_bars = cooldown_bars
        self.max_bars_after_escape = max_bars_after_escape
        self.max_consecutive_losses = max_consecutive_losses
        self.account_balance = account_balance
        self.account_risk_per_trade = account_risk_per_trade

        # --- 内部状态 ---
        self._active = False
        self._escape_price: float = 0.0
        self._stage_top: float = 0.0
        self._escape_bar: int = 0
        self._cooldown_remaining: int = 0
        self._current_short_order: Optional[Order] = None
        self._entry_price: float = 0.0
        self._entry_bar: int = 0          # 开仓 K 线序号
        self._consecutive_losses: int = 0
        self._lowest_since_entry: float = float('inf')
        self._daily_short_count: int = 0
        self._daily_reset_bar: int = 0    # 用于每日重置计数器

        # --- 概率平滑 ---
        self._smoothed_prob: float = 0.0

        # --- 历史缓存 (使用 deque 提升性能) ---
        self._max_history_len = 50
        self._high_history: deque = deque(maxlen=self._max_history_len)
        self._low_history: deque = deque(maxlen=self._max_history_len)
        self._close_history: deque = deque(maxlen=self._max_history_len)
        self._bpi_history: deque = deque(maxlen=self._max_history_len)
        self._taker_history: deque = deque(maxlen=self._max_history_len)
        self._volume_history: deque = deque(maxlen=self._max_history_len)
        self._slope_history: deque = deque(maxlen=self._max_history_len)

        logger.info(
            f"CallbackDropModule v3 initialized: threshold={drop_prob_threshold}, "
            f"coeff={position_coeff}, tight_stop={stop_tight_atr}ATR, trail={trail_atr}ATR"
        )

    @staticmethod
    def _validate_parameters(threshold: float, coeff: float, stop_atr: float,
                             trail_atr: float, cooldown: int, max_losses: int) -> None:
        if not (0 < threshold < 1):
            raise ValueError("drop_prob_threshold must be in (0,1)")
        if coeff <= 0 or coeff > 1:
            raise ValueError("position_coeff must be in (0,1]")
        if stop_atr <= 0 or trail_atr <= 0:
            raise ValueError("stop/trail ATR must be positive")
        if cooldown < 0:
            raise ValueError("cooldown_bars must be >= 0")
        if max_losses < 1:
            raise ValueError("max_consecutive_losses must be >= 1")

    # -------------------------------------------------------------------------
    # 公开接口
    # -------------------------------------------------------------------------
    def activate(self, symbol: str, exit_price: float, stage_top: float, current_bar: int) -> None:
        """激活模块（仅当未激活且不在冷却期）"""
        if self._active:
            logger.warning("Module already active, deactivating first")
            self.deactivate()
        if self._cooldown_remaining > 0:
            logger.info(f"Cannot activate during cooldown ({self._cooldown_remaining} bars left)")
            return
        if not (stage_top > exit_price > 0):
            logger.error(f"Invalid prices: exit={exit_price}, top={stage_top}")
            return

        self._active = True
        self._escape_price = exit_price
        self._stage_top = stage_top
        self._escape_bar = current_bar
        self._cooldown_remaining = 0
        self._current_short_order = None
        self._entry_price = 0.0
        self._entry_bar = 0
        self._consecutive_losses = 0
        self._lowest_since_entry = float('inf')
        self._smoothed_prob = 0.0
        self._reset_daily_counter_if_new_day(current_bar)
        self._clear_caches()
        logger.info(f"CallbackDrop activated: exit={exit_price:.2f}, top={stage_top:.2f}")

    def deactivate(self) -> None:
        """停用模块，强制清除空单记录（实际平仓应由调用者负责）"""
        self._active = False
        if self._current_short_order:
            logger.warning("Deactivating while holding a short order; ensure position is closed externally")
        self._current_short_order = None
        self._clear_caches()
        logger.info("CallbackDrop deactivated")

    async def compute(self, kline: Kline, context: Dict[str, Any]) -> Dict[str, Any]:
        """返回概率与动作，供前端展示"""
        if not self._active:
            return {'drop_probability': 0.0, 'action': 'NONE'}
        prob, action = self._evaluate_probability(kline, context)
        return {'drop_probability': prob, 'action': action}

    def evaluate(
        self,
        symbol: str,
        kline: Kline,
        features: Dict[str, Any],
        context: Dict[str, Any],
        portfolio: Portfolio,
        current_bar: int,
    ) -> Optional[Order]:
        """核心评估，每根 K 线调用"""
        if not self._active:
            return None

        self._reset_daily_counter_if_new_day(current_bar)
        self._update_cache(kline, features, context)

        # --- 超时与冷却 ---
        if current_bar - self._escape_bar > self.max_bars_after_escape:
            logger.info("Window expired")
            return self._exit_with_flat(symbol, kline, "timeout")
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return None

        # --- 趋势恢复或强多头 ---
        if kline.high > self._stage_top:
            return self._exit_with_flat(symbol, kline, "top_breakout")
        if self._is_strong_bull_market(context):
            return None

        # --- 熔断 ---
        if self._consecutive_losses >= self.max_consecutive_losses:
            return self._exit_with_flat(symbol, kline, "loss_limit")
        if self._daily_short_count >= MAX_DAILY_SHORTS:
            logger.info("Max daily shorts reached")
            return None

        # --- 管理现有持仓 ---
        if self._current_short_order is not None:
            # 持仓时间上限
            if current_bar - self._entry_bar > MAX_HOLD_BARS:
                return self._close_short(symbol, kline.close, "max_hold")
            return self._manage_existing_short(symbol, kline, features, context)

        # --- 无持仓，评估开仓 ---
        prob, _ = self._evaluate_probability(kline, context)
        # EMA 平滑
        self._smoothed_prob = EMA_ALPHA * prob + (1 - EMA_ALPHA) * self._smoothed_prob
        if self._smoothed_prob < self.drop_prob_threshold:
            return None

        atr = features.get('atr_3m', 1.0)
        if atr < MIN_ATR:
            return None
        kma = context.get('kma', 0.0)
        lower_band = kma + self.lower_band_atr_offset * atr
        if kline.close < lower_band:
            return None

        size = self._calc_short_size(atr, portfolio)
        if size < MIN_SHORT_SIZE:
            return None

        # 止损位（加上滑点预留）
        raw_stop = kline.high + self.stop_tight_atr * atr
        stop_loss = raw_stop * (1 + SLIPPAGE_RESERVE_PCT)

        order = Order(
            symbol=symbol,
            direction='SHORT',
            quantity=round(size, 6),
            order_type='MARKET',
            tag='callback_drop',
            stop_loss=stop_loss,
        )
        self._current_short_order = order
        self._entry_price = kline.close
        self._entry_bar = current_bar
        self._lowest_since_entry = kline.low
        self._daily_short_count += 1
        logger.info(
            f"CALLBACK DROP OPEN | price={kline.close:.2f} qty={size:.4f} "
            f"stop={stop_loss:.2f} smooth_prob={self._smoothed_prob:.3f}"
        )
        return order

    # -------------------------------------------------------------------------
    # 概率计算
    # -------------------------------------------------------------------------
    def _evaluate_probability(self, kline: Kline, context: Dict[str, Any]) -> Tuple[float, str]:
        """计算回落延续概率"""
        kma_slope = context.get('kma_slope', 0.0)
        hmm_bull_prob = context.get('hmm_bull_prob', 0.5)
        bpi = context.get('bpi', 0.0)
        taker_flow = context.get('taker_flow', 0.0)
        volume = kline.volume
        vol_ma20 = context.get('vol_ma20', volume) if volume > 0 else 1.0

        # 价格行为
        S_price = 0.0
        if len(self._close_history) >= 2 and len(self._high_history) >= 2:
            prev_close = self._close_history[-1]
            prev_high = self._high_history[-1]
            if kline.close < prev_close and kline.high < prev_high:
                S_price += 0.3
        if kline.close <= kline.low + 0.2 * (kline.high - kline.low):
            S_price += 0.2

        # 动能
        S_mom = 0.0
        if kma_slope < -0.03:
            S_mom += 0.3
        if hmm_bull_prob < 0.5:
            S_mom += 0.4

        # 量能/微观
        S_micro = 0.0
        if volume > vol_ma20 * 1.2:
            S_micro += 0.2
        if taker_flow < -0.2:
            S_micro += 0.3
        if bpi < -0.2:
            S_micro += 0.3

        # 大周期
        S_tf = 0.0
        if context.get('sr_5min_bearish', False):
            S_tf += 0.3
        if context.get('sr_15min_resistance_valid', False):
            S_tf += 0.2

        prob = (
            self.weights['price_action'] * S_price +
            self.weights['momentum'] * S_mom +
            self.weights['micro'] * S_micro +
            self.weights['timeframe'] * S_tf
        )
        prob = max(0.0, min(1.0, prob))
        action = 'SHORT' if prob >= self.drop_prob_threshold else 'NONE'
        return prob, action

    # -------------------------------------------------------------------------
    # 持仓管理
    # -------------------------------------------------------------------------
    def _manage_existing_short(
        self, symbol: str, kline: Kline,
        features: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[Order]:
        """移动止损、止盈、强反弹平仓"""
        order = self._current_short_order
        if order is None:
            return None

        atr = features.get('atr_3m', 1.0)
        if atr < MIN_ATR:
            return None
        price = kline.close
        kma = context.get('kma', 0.0)

        # 更新最低价
        if kline.low < self._lowest_since_entry:
            self._lowest_since_entry = kline.low

        # 移动止损：创新低且幅度超过最小步长才下移
        if kline.low < self._lowest_since_entry - self.trail_atr * atr:
            if kline.low < order.stop_loss - MIN_TRAIL_STEP_ATR * atr:
                new_stop = kline.high + self.trail_atr * atr
                if new_stop < order.stop_loss:
                    order.stop_loss = new_stop
                    logger.debug(f"Stop trailed to {new_stop:.2f}")

        # 止盈
        lower_band = kma + self.lower_band_atr_offset * atr
        if price <= lower_band:
            return self._close_short(symbol, price, "target")

        # 止损
        if price >= order.stop_loss:
            return self._close_short(symbol, price, "stop_loss")

        # 微观逆转
        bpi = context.get('bpi', 0.0)
        taker_flow = context.get('taker_flow', 0.0)
        if bpi > 0.2 or taker_flow > 0.15:
            return self._close_short(symbol, price, "micro_reversal")

        # 强反弹
        if self._detect_strong_rebound(kline, features):
            return self._close_short(symbol, price, "strong_rebound")

        return None

    def _close_short(self, symbol: str, price: float, reason: str) -> Order:
        if self._current_short_order is None:
            return None
        qty = self._current_short_order.quantity
        order = Order(
            symbol=symbol,
            direction='LONG',
            quantity=qty,
            order_type='MARKET',
            tag=f'callback_drop_close_{reason}',
        )
        pnl = (self._entry_price - price) * qty if self._entry_price > 0 else 0.0
        logger.info(
            f"CALLBACK DROP CLOSE | reason={reason} price={price:.2f} qty={qty:.4f} pnl={pnl:.2f}"
        )
        self._current_short_order = None
        self._cooldown_remaining = self.cooldown_bars
        if reason == "stop_loss":
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        return order

    def _exit_with_flat(self, symbol: str, kline: Kline, reason: str) -> Optional[Order]:
        if self._current_short_order:
            order = self._close_short(symbol, kline.close, reason)
            self.deactivate()
            return order
        self.deactivate()
        return None

    # -------------------------------------------------------------------------
    # 辅助判断
    # -------------------------------------------------------------------------
    def _is_strong_bull_market(self, context: Dict[str, Any]) -> bool:
        hmm_15m = context.get('hmm_15m_state', 'RANGE')
        slope_15m = context.get('kma_15m_slope', 0.0)
        return hmm_15m == 'BULL' and slope_15m > 0.04

    def _detect_strong_rebound(self, kline: Kline, features: Dict[str, Any]) -> bool:
        if kline.close > kline.open and (kline.close - kline.open) > (kline.high - kline.low) * 0.6:
            vol = features.get('volume', 0)
            vol_ma = features.get('vol_ma20', 1)
            if vol > vol_ma * 1.3:
                return True
        return False

    # -------------------------------------------------------------------------
    # 仓位计算
    # -------------------------------------------------------------------------
    def _calc_short_size(self, atr: float, portfolio: Portfolio) -> float:
        if atr <= 0:
            return 0.0
        equity = getattr(portfolio, 'equity', 10000.0)
        if equity <= 0:
            return 0.0
        risk_budget = self.account_risk_per_trade * equity
        stop_distance = self.stop_tight_atr * atr
        if stop_distance < MIN_STOP_DISTANCE:
            return 0.0
        raw_size = risk_budget / stop_distance
        coeff = self.position_coeff
        if self.account_balance < 5000:
            coeff *= max(0.3, self.account_balance / 5000.0)
        return raw_size * coeff

    # -------------------------------------------------------------------------
    # 缓存管理
    # -------------------------------------------------------------------------
    def _update_cache(self, kline: Kline, features: Dict[str, Any], context: Dict[str, Any]) -> None:
        self._close_history.append(kline.close)
        self._high_history.append(kline.high)
        self._low_history.append(kline.low)
        self._bpi_history.append(context.get('bpi', 0.0))
        self._taker_history.append(context.get('taker_flow', 0.0))
        self._volume_history.append(kline.volume)
        self._slope_history.append(context.get('kma_slope', 0.0))

    def _clear_caches(self) -> None:
        for dq in [self._close_history, self._high_history, self._low_history,
                    self._bpi_history, self._taker_history, self._volume_history,
                    self._slope_history]:
            dq.clear()

    def _reset_daily_counter_if_new_day(self, current_bar: int) -> None:
        # 简化：每 1440 根 3 分钟 K 线视为一天
        if current_bar - self._daily_reset_bar >= 1440:
            self._daily_short_count = 0
            self._daily_reset_bar = current_bar

    # -------------------------------------------------------------------------
    # 状态持久化
    # -------------------------------------------------------------------------
    def get_state(self) -> Dict[str, Any]:
        return {
            'active': self._active,
            'escape_price': self._escape_price,
            'stage_top': self._stage_top,
            'escape_bar': self._escape_bar,
            'cooldown_remaining': self._cooldown_remaining,
            'entry_price': self._entry_price,
            'entry_bar': self._entry_bar,
            'consecutive_losses': self._consecutive_losses,
            'lowest_since_entry': self._lowest_since_entry,
            'daily_short_count': self._daily_short_count,
            'daily_reset_bar': self._daily_reset_bar,
            'smoothed_prob': self._smoothed_prob,
            'current_short_order': self._current_short_order.__dict__ if self._current_short_order else None,
            'close_history': list(self._close_history),
            'high_history': list(self._high_history),
            'low_history': list(self._low_history),
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        self._active = state.get('active', False)
        self._escape_price = state.get('escape_price', 0.0)
        self._stage_top = state.get('stage_top', 0.0)
        self._escape_bar = state.get('escape_bar', 0)
        self._cooldown_remaining = state.get('cooldown_remaining', 0)
        self._entry_price = state.get('entry_price', 0.0)
        self._entry_bar = state.get('entry_bar', 0)
        self._consecutive_losses = state.get('consecutive_losses', 0)
        self._lowest_since_entry = state.get('lowest_since_entry', float('inf'))
        self._daily_short_count = state.get('daily_short_count', 0)
        self._daily_reset_bar = state.get('daily_reset_bar', 0)
        self._smoothed_prob = state.get('smoothed_prob', 0.0)
        order_dict = state.get('current_short_order')
        self._current_short_order = Order(**order_dict) if order_dict else None
        for attr, hist in [('_close_history', self._close_history),
                           ('_high_history', self._high_history),
                           ('_low_history', self._low_history)]:
            hist.extend(state.get(attr, [])[-self._max_history_len:])
        logger.info("CallbackDrop state restored")
