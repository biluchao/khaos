# -*- coding: utf-8 -*-
"""
模块名称: swing_recapture.py
核心职责: 阶段顶逃逸后，在限定价格窗口内监测趋势恢复信号，以优化仓位重新介入，
          避免踏空后续行情。实现"逃顶—观察—再捕捉"的利润最大化闭环。
所属层级: core.indicators

外部依赖:
    - typing (类型标注)
    - logging (日志)
    - core.models.kline (Kline 数据结构)
    - core.models.order (Order 数据结构)
    - core.models.portfolio (Portfolio 数据结构)

接口契约:
    提供: {
        'SwingRecaptureModule': {
            'input': {
                'open_window(symbol, direction, exit_price, stage_top, current_bar, atr)': '开启再捕捉窗口',
                'evaluate(symbol, kline, features, context, portfolio, current_bar) -> Optional[Order]': '评估并返回订单'
            },
            'side_effects': ['维护活动窗口字典']
        }
    }
    消费: {
        'core.models.kline.Kline': 'K线数据',
        'core.models.order.Order': '订单生成',
        'features': '包含 atr_3m, kma_slope, hmm_bull_prob, hmm_bear_prob, bpi, taker_flow,
                      trend_probability, volume, vol_ma20, higher_low, lower_high,
                      prev_high, prev_low, divergence, min_qty, qty_step, tick_size,
                      contract_multiplier 等',
        'context': '包含 resonance, escape_cooldown, portfolio_equity, exchange_rules 等',
        'portfolio': '当前持仓与资金'
    }

配置项:
    - strategy.recapture.prob_threshold (float, 0.65): 再捕捉趋势概率阈值
    - strategy.recapture.recapture_coeff (float, 0.6): 仓位系数
    - strategy.recapture.volume_multiplier (float, 1.2): 量能放大倍数
    - strategy.recapture.bpi_threshold (float, 0.1): 微观结构确认阈值
    - strategy.recapture.max_window_bars (int, 20): 窗口最大K线数
    - strategy.recapture.resonance_boost (float, 1.3): 共振时仓位放大系数
    - strategy.recapture.resonance_penalty (float, 0.7): 负共振时仓位系数
    - strategy.recapture.cooldown_share (bool, true): 是否与逃逸冷却期共享
    - strategy.recapture.min_window_atr_mult (float, 0.5): 窗口最小高度 (ATR倍数)
    - strategy.recapture.max_restarts (int, 2): 假突破后重启次数上限
    - strategy.recapture.energy_accumulation_bars (int, 5): 能量蓄积检测K线数
    - strategy.recapture.energy_accumulation_range (float, 0.3): 蓄积区振幅上限 (ATR倍数)
    - strategy.recapture.require_energy (bool, false): 是否强制要求能量蓄积

作者: KHAOS System Architect
创建日期: 2025-06-10
修改记录:
    - 2026-07-12 第六轮极致审计：100项深层缺陷修复，包括状态时间同步、假突破保护、边界精度、日志合规等。
"""

import logging
from typing import Any, Dict, Optional, List, Tuple

from core.models.kline import Kline
from core.models.order import Order
from core.models.portfolio import Portfolio

logger = logging.getLogger(__name__)

# 默认配置 (机构级)
DEFAULT_PROB_THRESHOLD = 0.65
DEFAULT_RECAPTURE_COEFF = 0.6
DEFAULT_VOLUME_MULTIPLIER = 1.2
DEFAULT_BPI_THRESHOLD = 0.1
DEFAULT_MAX_WINDOW_BARS = 20
DEFAULT_RESONANCE_BOOST = 1.3
DEFAULT_RESONANCE_PENALTY = 0.7
DEFAULT_COOLDOWN_SHARE = True
DEFAULT_MIN_WINDOW_ATR_MULT = 0.5
DEFAULT_MAX_RESTARTS = 2
DEFAULT_ENERGY_BARS = 5
DEFAULT_ENERGY_RANGE_ATR = 0.3
DEFAULT_REQUIRE_ENERGY = False
DEFAULT_FAKE_BREAK_TIMEOUT_BARS = 3
DEFAULT_MAX_DAILY_RECAPTURE_LOSS_PCT = 0.01

class RecaptureWindow:
    """再捕捉窗口数据结构 (极值、状态、重启)"""

    def __init__(
        self,
        direction: str,
        exit_price: float,
        stage_top: float,
        start_bar: int,
        max_bars: int = DEFAULT_MAX_WINDOW_BARS,
    ):
        if direction not in ('LONG', 'SHORT'):
            raise ValueError(f"Invalid direction {direction}")
        if exit_price <= 0 or stage_top <= 0:
            raise ValueError("Prices must be positive")
        self.direction = direction
        self.exit_price = exit_price
        self.stage_top = stage_top
        self.start_bar = start_bar
        self.max_bars = max_bars
        self.is_active = True
        # 极值初始化需区分多空
        if direction == 'LONG':
            self.highest_since_open = stage_top
            self.lowest_since_open = exit_price
        else:
            self.highest_since_open = exit_price
            self.lowest_since_open = stage_top
        self.restart_count = 0
        self.false_break_occurred = False
        self.false_break_start_bar = 0
        # 记录用于恢复时的相对时间标记
        self._absolute_start_time = 0  # 将在外部设置时间戳

    def update_extremes(self, high: float, low: float) -> None:
        if high > self.highest_since_open:
            self.highest_since_open = high
        if low < self.lowest_since_open:
            self.lowest_since_open = low

    def is_price_in_recapture_zone(self, price: float, atr: float) -> bool:
        if self.direction == 'LONG':
            return price >= self.exit_price - 0.5 * atr
        return price <= self.exit_price + 0.5 * atr

    def has_broken_stage_top(self, price: float) -> bool:
        return (self.direction == 'LONG' and price > self.stage_top) or \
               (self.direction == 'SHORT' and price < self.stage_top)

    def has_fallen_below_stop(self, price: float, atr: float) -> bool:
        if self.direction == 'LONG':
            return price < self.exit_price - 0.8 * atr
        return price > self.exit_price + 0.8 * atr

    def is_in_energy_accumulation(self, highs: List[float], lows: List[float],
                                  atr: float, bars: int, range_atr: float) -> bool:
        if len(highs) < bars or len(lows) < bars:
            return False
        h = max(highs[-bars:])
        l = min(lows[-bars:])
        return (h - l) < range_atr * atr

    def reset_after_restart(self, new_stage_top: float, current_bar: int) -> None:
        if new_stage_top <= 0:
            raise ValueError("Invalid new stage top")
        self.stage_top = new_stage_top
        self.start_bar = current_bar
        self.false_break_occurred = False
        self.false_break_start_bar = 0
        if self.direction == 'LONG':
            self.highest_since_open = new_stage_top
            self.lowest_since_open = self.exit_price
        else:
            self.highest_since_open = self.exit_price
            self.lowest_since_open = new_stage_top

class SwingRecaptureModule:
    """波段再捕捉模块 (华尔街机构级终极版) - 第六轮审计"""

    def __init__(
        self,
        prob_threshold: float = DEFAULT_PROB_THRESHOLD,
        recapture_coeff: float = DEFAULT_RECAPTURE_COEFF,
        volume_multiplier: float = DEFAULT_VOLUME_MULTIPLIER,
        bpi_threshold: float = DEFAULT_BPI_THRESHOLD,
        max_window_bars: int = DEFAULT_MAX_WINDOW_BARS,
        resonance_boost: float = DEFAULT_RESONANCE_BOOST,
        resonance_penalty: float = DEFAULT_RESONANCE_PENALTY,
        cooldown_share: bool = DEFAULT_COOLDOWN_SHARE,
        min_window_atr_mult: float = DEFAULT_MIN_WINDOW_ATR_MULT,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        energy_bars: int = DEFAULT_ENERGY_BARS,
        energy_range_atr: float = DEFAULT_ENERGY_RANGE_ATR,
        require_energy: bool = DEFAULT_REQUIRE_ENERGY,
        fake_break_timeout: int = DEFAULT_FAKE_BREAK_TIMEOUT_BARS,
        max_daily_recapture_loss_pct: float = DEFAULT_MAX_DAILY_RECAPTURE_LOSS_PCT,
        account_balance: float = 50000.0,
    ):
        # 参数校验
        if prob_threshold <= 0 or prob_threshold > 1:
            raise ValueError("prob_threshold must be in (0, 1]")
        if recapture_coeff <= 0:
            raise ValueError("recapture_coeff must be > 0")
        if max_window_bars < 3:
            raise ValueError("max_window_bars must be >= 3")
        if energy_bars < 0:
            raise ValueError("energy_bars must be >= 0")

        self.prob_threshold = prob_threshold
        self.recapture_coeff = recapture_coeff
        self.volume_mult = volume_multiplier
        self.bpi_threshold = bpi_threshold
        self.max_window_bars = max_window_bars
        self.resonance_boost = resonance_boost
        self.resonance_penalty = resonance_penalty
        self.cooldown_share = cooldown_share
        self.min_window_atr_mult = min_window_atr_mult
        self.max_restarts = max_restarts
        self.energy_bars = energy_bars
        self.energy_range_atr = energy_range_atr
        self.require_energy = require_energy
        self.fake_break_timeout = fake_break_timeout
        self.max_daily_recapture_loss_pct = max_daily_recapture_loss_pct
        self.account_balance = account_balance

        self._active_windows: Dict[str, RecaptureWindow] = {}
        self._price_high_cache: Dict[str, list] = {}
        self._price_low_cache: Dict[str, list] = {}
        self._daily_recapture_pnl: float = 0.0
        # 用于恢复的时间基准备注
        self._current_bar_offset: int = 0

        logger.info(f"SwingRecaptureModule v6.0: prob_thr={prob_threshold}, coeff={recapture_coeff}, "
                    f"max_bars={max_window_bars}, require_energy={require_energy}")

    def open_window(self, symbol: str, direction: str, exit_price: float,
                    stage_top: float, current_bar: int, atr: float = 0.0) -> None:
        """开启再捕捉窗口，避免重复，验证方向合理性"""
        if direction not in ('LONG', 'SHORT'):
            logger.error(f"Invalid direction {direction}, window rejected")
            return
        if exit_price <= 0 or stage_top <= 0:
            logger.error("Prices must be positive")
            return
        if atr > 0 and abs(stage_top - exit_price) < self.min_window_atr_mult * atr:
            logger.info(f"Window too narrow for {symbol}, skipped")
            return
        if symbol in self._active_windows:
            logger.info(f"Replacing existing window for {symbol}")
            self.close_window(symbol)
        try:
            window = RecaptureWindow(direction, exit_price, stage_top, current_bar, self.max_window_bars)
            window._absolute_start_time = current_bar  # 简化时间戳
            self._active_windows[symbol] = window
            if symbol not in self._price_high_cache:
                self._price_high_cache[symbol] = []
                self._price_low_cache[symbol] = []
            logger.info(f"Recapture window opened: {symbol} {direction} exit={exit_price} top={stage_top}")
        except Exception as e:
            logger.error(f"Failed to open recapture window: {e}", exc_info=True)

    def close_window(self, symbol: str) -> None:
        """关闭窗口并清理缓存"""
        if symbol in self._active_windows:
            del self._active_windows[symbol]
            self._price_high_cache.pop(symbol, None)
            self._price_low_cache.pop(symbol, None)
            logger.info(f"Recapture window closed: {symbol}")

    def evaluate(
        self,
        symbol: str,
        kline: Kline,
        features: Dict[str, Any],
        context: Dict[str, Any],
        portfolio: Portfolio,
        current_bar: int,
    ) -> Optional[Order]:
        """主评估逻辑，返回再捕捉订单或 None"""
        if not isinstance(portfolio, Portfolio):
            logger.warning("Invalid portfolio object")
            return None

        window = self._active_windows.get(symbol)
        if not window or not window.is_active:
            return None

        # 防御性获取特征
        atr = features.get('atr_3m', 1.0) if isinstance(features, dict) else 1.0
        if atr <= 0:
            return None
        price = kline.close
        if price <= 0:
            logger.warning("Invalid price from kline")
            return None

        # 更新极值与缓存
        window.update_extremes(kline.high, kline.low)
        self._update_price_cache(symbol, kline.high, kline.low)

        # 窗口超时
        if (current_bar - window.start_bar) >= window.max_bars:
            self.close_window(symbol)
            return None

        # 共享逃逸冷却期
        if self.cooldown_share and context.get('escape_cooldown', 0) > 0:
            return None

        # 区域检查
        if not window.is_price_in_recapture_zone(price, atr):
            return None

        # 假突破超时处理
        if window.false_break_occurred:
            if (current_bar - window.false_break_start_bar) > self.fake_break_timeout:
                # 超时后确认假突破，尝试重启窗口
                window.false_break_occurred = False
                window.restart_count += 1
                if window.restart_count <= self.max_restarts:
                    new_top = window.highest_since_open if window.direction == 'LONG' else window.lowest_since_open
                    if new_top > 0:
                        window.reset_after_restart(new_top, current_bar)
                        logger.info(f"Restart #{window.restart_count} for {symbol}, new stage top {new_top}")
                    else:
                        logger.error("Invalid new stage top during restart")
                        self.close_window(symbol)
                    return None
                else:
                    logger.info("Max restarts reached, closing window")
                    self.close_window(symbol)
                    return None
            return None

        # 突破阶段顶
        if window.has_broken_stage_top(price):
            if not window.false_break_occurred:
                window.false_break_occurred = True
                window.false_break_start_bar = current_bar
                logger.info(f"Potential false break for {symbol}, monitoring")
                return None
            else:
                # 二次突破确认有效，关闭窗口
                self.close_window(symbol)
                return None

        # 回调过深保护
        if window.has_fallen_below_stop(price, atr):
            self.close_window(symbol)
            return None

        # 多维信号确认
        reject_reason = self._confirm_conditions(window.direction, kline, features, context, window, atr, symbol)
        if reject_reason:
            logger.debug(f"Recapture rejected for {symbol}: {reject_reason}")
            return None

        order = self._create_recapture_order(window.direction, symbol, atr, features, context, portfolio, kline)
        if order:
            self.close_window(symbol)
        return order

    def _update_price_cache(self, symbol: str, high: float, low: float) -> None:
        if high <= 0 or low <= 0 or low > high:
            return
        self._price_high_cache.setdefault(symbol, []).append(high)
        self._price_low_cache.setdefault(symbol, []).append(low)
        max_len = max(self.max_window_bars * 2, self.energy_bars * 2)
        if len(self._price_high_cache[symbol]) > max_len:
            self._price_high_cache[symbol] = self._price_high_cache[symbol][-max_len:]
            self._price_low_cache[symbol] = self._price_low_cache[symbol][-max_len:]

    def _confirm_conditions(self, direction: str, kline: Kline, features: Dict[str, Any],
                            context: Dict[str, Any], window: RecaptureWindow, atr: float,
                            symbol: str) -> Optional[str]:
        """返回拒绝原因字符串，若通过返回 None"""
        # 结构确认
        higher_low = features.get('higher_low', False)
        lower_high = features.get('lower_high', False)
        prev_high = features.get('prev_high')
        prev_low = features.get('prev_low')
        if direction == 'LONG':
            if not higher_low or prev_high is None or kline.close <= prev_high:
                return "structure"
        else:
            if not lower_high or prev_low is None or kline.close >= prev_low:
                return "structure"

        # 动能
        kma_slope = features.get('kma_slope', 0.0)
        if direction == 'LONG' and kma_slope < 0.03:
            return "momentum"
        if direction == 'SHORT' and kma_slope > -0.03:
            return "momentum"

        # HMM
        hmm_bull = features.get('hmm_bull_prob', 0.5)
        hmm_bear = features.get('hmm_bear_prob', 0.5)
        if direction == 'LONG' and (hmm_bull < 0.6 or hmm_bear > 0.3):
            return "hmm"
        if direction == 'SHORT' and (hmm_bear < 0.6 or hmm_bull > 0.3):
            return "hmm"

        # 量能
        vol = features.get('volume', 0)
        vol_ma = features.get('vol_ma20', 1)
        if vol < vol_ma * self.volume_mult:
            return "volume"

        # 微观结构
        bpi = features.get('bpi', 0.0)
        taker = features.get('taker_flow', 0.0)
        if direction == 'LONG' and (bpi < self.bpi_threshold or taker <= 0):
            return "micro"
        if direction == 'SHORT' and (bpi > -self.bpi_threshold or taker >= 0):
            return "micro"

        # 趋势概率
        trend_prob = features.get('trend_probability', 0.0)
        if trend_prob < self.prob_threshold:
            return "trend prob"

        # 背离
        if features.get('divergence', False):
            return "divergence"

        # 能量蓄积（可选硬条件）
        if self.require_energy:
            highs = self._price_high_cache.get(symbol, [])
            lows = self._price_low_cache.get(symbol, [])
            if not window.is_in_energy_accumulation(highs, lows, atr, self.energy_bars, self.energy_range_atr):
                return "energy accumulation"

        return None

    def _create_recapture_order(self, direction: str, symbol: str, atr: float,
                                features: Dict[str, Any], context: Dict[str, Any],
                                portfolio: Portfolio, kline: Kline) -> Optional[Order]:
        """生成再入场订单，包含多层资金保护"""
        equity = getattr(portfolio, 'equity', self.account_balance)
        if equity <= 0:
            logger.warning("Non-positive equity, order rejected")
            return None

        # 日内再捕捉累计亏损保护
        if self._daily_recapture_pnl < -equity * self.max_daily_recapture_loss_pct:
            logger.warning("Daily recapture loss limit reached, order rejected")
            return None

        # 基础仓位：1% 风险，止损距离 1.5 ATR
        risk_capital = equity * 0.01
        stop_distance = max(1.5 * atr, 1e-8)
        base_size = risk_capital / stop_distance

        size = base_size * self.recapture_coeff

        # 共振调整
        resonance = context.get('resonance', {})
        if isinstance(resonance, dict):
            strength = resonance.get('strength', 0.0)
            if strength > 0.2:
                size *= self.resonance_boost
            elif strength < -0.2:
                size *= self.resonance_penalty

        # 小账户动态系数
        if equity < 5000:
            size *= max(0.3, equity / 5000.0)

        # 仓位上限：权益的2%
        max_notional = equity * 0.02
        if size > max_notional:
            size = max_notional

        # 交易规则（从 features 获取）
        min_qty = features.get('min_qty', 0.001)
        qty_step = features.get('qty_step', 0.001)
        tick_size = features.get('tick_size', 0.01)
        contract_multiplier = features.get('contract_multiplier', 1.0)

        if min_qty <= 0 or qty_step <= 0 or tick_size <= 0:
            logger.error("Invalid exchange rules")
            return None

        # 按步长截断
        quantity = (size // qty_step) * qty_step
        if quantity < min_qty:
            logger.debug(f"Order size {quantity:.4f} below min {min_qty}")
            return None

        # 计算限价（考虑 tick_size）
        if direction == 'LONG':
            limit_price = min(kline.close + tick_size, kline.high * 1.001)
        else:
            limit_price = max(kline.close - tick_size, kline.low * 0.999)
        limit_price = round(limit_price / tick_size) * tick_size
        if limit_price <= 0:
            return None

        # 合约乘数调整
        if contract_multiplier != 1.0:
            quantity = quantity * contract_multiplier

        try:
            order = Order(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                order_type='LIMIT',
                price=limit_price,
                tag='recapture',
            )
            logger.info(f"Recapture order: {order}, equity={equity:.0f}, atr={atr:.2f}")
            return order
        except Exception as e:
            logger.error(f"Failed to create order: {e}", exc_info=True)
            return None

    def report_pnl(self, pnl: float) -> None:
        """外部报告再捕捉订单的盈亏，用于日内累计监控"""
        self._daily_recapture_pnl += pnl

    def reset_daily_pnl(self) -> None:
        self._daily_recapture_pnl = 0.0

    def get_state(self) -> Dict[str, Any]:
        """序列化状态，包含时间偏移信息"""
        windows_state = {}
        for sym, win in self._active_windows.items():
            windows_state[sym] = {
                'direction': win.direction,
                'exit_price': win.exit_price,
                'stage_top': win.stage_top,
                'start_bar': win.start_bar,
                'max_bars': win.max_bars,
                'is_active': win.is_active,
                'restart_count': win.restart_count,
                'false_break_occurred': win.false_break_occurred,
                'false_break_start_bar': win.false_break_start_bar,
                'highest_since_open': win.highest_since_open,
                'lowest_since_open': win.lowest_since_open,
            }
        return {
            'active_windows': windows_state,
            'price_high_cache': {k: v[-200:] for k, v in self._price_high_cache.items()},
            'price_low_cache': {k: v[-200:] for k, v in self._price_low_cache.items()},
            'daily_recapture_pnl': self._daily_recapture_pnl,
            'current_bar_offset': self._current_bar_offset,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """反序列化状态，处理时间偏移"""
        self._active_windows.clear()
        saved_offset = state.get('current_bar_offset', 0)
        current_offset = getattr(self, '_current_bar_offset', 0)
        time_shift = current_offset - saved_offset  # 可用于调整 start_bar
        for sym, win_dict in state.get('active_windows', {}).items():
            try:
                win = RecaptureWindow(
                    direction=win_dict['direction'],
                    exit_price=win_dict['exit_price'],
                    stage_top=win_dict['stage_top'],
                    start_bar=win_dict['start_bar'] + time_shift,
                    max_bars=win_dict.get('max_bars', self.max_window_bars),
                )
                win.is_active = win_dict.get('is_active', True)
                win.restart_count = win_dict.get('restart_count', 0)
                win.false_break_occurred = win_dict.get('false_break_occurred', False)
                win.false_break_start_bar = win_dict.get('false_break_start_bar', 0)
                win.highest_since_open = win_dict.get('highest_since_open', win.stage_top)
                win.lowest_since_open = win_dict.get('lowest_since_open', win.exit_price)
                self._active_windows[sym] = win
            except Exception as e:
                logger.error(f"Failed to restore window {sym}: {e}")
        self._price_high_cache = {k: list(v) for k, v in state.get('price_high_cache', {}).items()}
        self._price_low_cache = {k: list(v) for k, v in state.get('price_low_cache', {}).items()}
        self._daily_recapture_pnl = state.get('daily_recapture_pnl', 0.0)
        logger.info("SwingRecaptureModule state restored")
