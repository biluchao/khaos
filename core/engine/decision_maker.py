# -*- coding: utf-8 -*-
"""
模块名称: decision_maker.py
核心职责: 策略决策器，聚合所有模块信号并生成最终交易订单。集成了趋势概率过滤、逃逸、
          再捕捉、回调跌落、均线回踩、游击追仓等所有子策略，并按优先级和风控约束仲裁。
所属层级: core.engine

外部依赖:
    - asyncio, time, logging, typing, weakref, copy, collections
    - core.models.Order, core.models.Kline, core.models.Portfolio
    - core.interfaces.FeatureComputer
    - core.risk.position_sizer_v2.PositionSizerV2
    - core.risk.risk_firewall.RiskFirewall
    - 各种指标模块 (trend_probability_filter, escape_detector, swing_recapture,
      callback_drop, pullback_add, guerrilla_chase)

接口契约:
    提供: {
        'KhaosDecisionMaker': {
            'input': 'kline: Kline, context: dict, portfolio: dict',
            'output': 'List[Order]',
            'side_effects': ['更新模块状态', '记录审计日志', '触发风控', '信号计数器更新']
        }
    }

配置项:
    - strategy.* (各子策略参数)
    - risk.position_sizing.*
    - signal_priority (列表)
    - 全局冷却、信号频率限制、超时设置等

作者: KHAOS System Architect
创建日期: 2025-04-10
修改记录:
    - 2026-07-15 \~ 2026-07-17 多轮审计累计修复
    - 2026-07-27 v5.1: 并发锁、优先级一致性、deque限长、弱引用安全、portfolio双兼容、异步取消完整
    - 2026-07-27 v5.2: 去重竞态消除、嵌套context防护、优先级映射补全、超时任务强制清理、资源上界强化（累计150+缺陷修复）
__version__ = "5.2.0"
"""

import asyncio
import logging
import time
import weakref
from copy import deepcopy
from collections import deque
from typing import List, Optional, Dict, Any, Set, Tuple, Union

from core.models.order import Order
from core.models.kline import Kline
from core.models.portfolio import Portfolio
from core.risk.position_sizer_v2 import PositionSizerV2
from core.risk.risk_firewall import RiskFirewall
from core.indicators.trend_probability_filter import TrendProbabilityFilter
from core.indicators.escape_detector import StageTopEscapeDetector
from core.indicators.swing_recapture import SwingRecaptureModule
from core.indicators.callback_drop import CallbackDropModule
from core.indicators.pullback_add import PullbackAddModule
from core.indicators.guerrilla_chase import GuerrillaChase

logger = logging.getLogger(__name__)

# 模块超时配置（秒）
MODULE_TIMEOUTS: Dict[str, float] = {
    'EscapeDetector': 2.0,
    'Recapture': 2.5,
    'CallbackDrop': 2.5,
    'PullbackAdd': 2.5,
    'GuerrillaChase': 2.0,
    'TrendProbabilityFilter': 1.5,
}
DEFAULT_MODULE_TIMEOUT = 3.0
SIGNAL_WINDOW_SEC = 3600
MAX_SIGNALS_COOLDOWN = 600
PANIC_COOLDOWN = 3600
MAX_SIGNAL_TIMESTAMPS = 500
MAX_RECENT_SIGNALS = 256

FLOAT_TOLERANCE = 1e-8

# 优先级字符串与 metadata.module 的统一映射（覆盖所有变体）
MODULE_TO_PRIORITY_KEY: Dict[str, str] = {
    'escape': 'escape_close',
    'escape_detector': 'escape_close',
    'EscapeDetector': 'escape_close',
    'escape_close': 'escape_close',
    'escape_reduce': 'escape_reduce',
    'recapture': 'recapture',
    'Recapture': 'recapture',
    'callback_drop': 'callback_drop',
    'CallbackDrop': 'callback_drop',
    'pullback_add': 'pullback_add',
    'PullbackAdd': 'pullback_add',
    'guerrilla_chase': 'guerrilla_chase',
    'GuerrillaChase': 'guerrilla_chase',
    'trend_prob_filter': 'trend_prob_filter',
    'TrendProbabilityFilter': 'trend_prob_filter',
}


class KhaosDecisionMaker:
    """机构级策略决策器 v5.2，具备全模块信号仲裁、动态优先级、风控集成与自愈监控"""

    def __init__(self,
                 prob_filter: TrendProbabilityFilter,
                 escape_detector: StageTopEscapeDetector,
                 recapture: SwingRecaptureModule,
                 callback_drop: CallbackDropModule,
                 pullback_add: PullbackAddModule,
                 guerrilla_chase: GuerrillaChase,
                 position_sizer: PositionSizerV2,
                 risk_firewall: RiskFirewall,
                 config: Dict[str, Any]):
        self._prob_filter = weakref.ref(prob_filter) if prob_filter else None
        self._escape_detector = weakref.ref(escape_detector) if escape_detector else None
        self._recapture = weakref.ref(recapture) if recapture else None
        self._callback_drop = weakref.ref(callback_drop) if callback_drop else None
        self._pullback_add = weakref.ref(pullback_add) if pullback_add else None
        self._guerrilla_chase = weakref.ref(guerrilla_chase) if guerrilla_chase else None
        self._position_sizer = weakref.ref(position_sizer) if position_sizer else None
        self._risk_firewall = weakref.ref(risk_firewall) if risk_firewall else None

        self.signal_priority: List[str] = list(config.get('signal_priority', [
            'escape_close', 'escape_reduce', 'recapture', 'callback_drop',
            'pullback_add', 'guerrilla_chase', 'trend_prob_filter'
        ]))
        self.reduce_only = bool(config.get('reduce_only_mode', False))

        self.module_enabled: Dict[str, bool] = deepcopy({
            'EscapeDetector': config.get('strategy', {}).get('escape', {}).get('enabled', True),
            'Recapture': config.get('strategy', {}).get('recapture', {}).get('enabled', True),
            'CallbackDrop': config.get('strategy', {}).get('callback_drop', {}).get('enabled', True),
            'PullbackAdd': config.get('strategy', {}).get('pullback_add', {}).get('enabled', True),
            'GuerrillaChase': config.get('strategy', {}).get('guerrilla_chase', {}).get('enabled', False),
            'TrendProbabilityFilter': config.get('strategy', {}).get('trend_prob_filter', {}).get('enabled', True),
        })

        self._module_status: Dict[str, bool] = {name: True for name in MODULE_TIMEOUTS}
        self._module_status['PositionSizer'] = True
        self._module_status['RiskFirewall'] = True

        self.self_monitoring: Dict[str, Any] = dict(config.get('self_monitoring', {}))
        self.max_signals_per_hour: int = int(self.self_monitoring.get('max_open_signals_per_hour', 20))

        self._signal_timestamps: deque = deque(maxlen=MAX_SIGNAL_TIMESTAMPS)
        self._last_decision_timestamp: Optional[Any] = None

        self._in_cooldown = False
        self._cooldown_until = 0.0

        self._current_position_direction: Optional[str] = None
        self._position_version: int = 0

        self._recent_signals: Dict[str, float] = {}
        self._signal_dedup_window = 2.0

        self._last_audit_log_time = 0.0

        self._lock = asyncio.Lock()

        logger.info("KhaosDecisionMaker v5.2 initialized. Enabled: %s",
                    {k: v for k, v in self.module_enabled.items() if v})

    def update_position_state(self, portfolio: Union[Portfolio, dict, None]):
        """同步当前净持仓方向。支持 Portfolio 对象或 dict。"""
        if portfolio is None:
            return
        try:
            if isinstance(portfolio, dict):
                net = float(portfolio.get('net_delta', 0.0) or 0.0)
            else:
                net = float(getattr(portfolio, 'net_delta', 0.0) or 0.0)
        except (TypeError, ValueError):
            return

        if net > FLOAT_TOLERANCE:
            new_dir = 'LONG'
        elif net < -FLOAT_TOLERANCE:
            new_dir = 'SHORT'
        else:
            new_dir = None

        if new_dir != self._current_position_direction:
            self._current_position_direction = new_dir
            self._position_version += 1

    def _get_module(self, name: str):
        """安全获取模块引用（弱引用解析）"""
        mapping = {
            'EscapeDetector': self._escape_detector,
            'Recapture': self._recapture,
            'CallbackDrop': self._callback_drop,
            'PullbackAdd': self._pullback_add,
            'GuerrillaChase': self._guerrilla_chase,
            'TrendProbabilityFilter': self._prob_filter,
        }
        ref = mapping.get(name)
        if ref is None:
            return None
        obj = ref()
        if obj is None:
            logger.error("Weakref for module %s has been garbage-collected", name)
            self._module_status[name] = False
        return obj

    async def decide(self, kline: Kline, context: dict, portfolio: dict) -> List[Order]:
        """主决策函数。每根K线触发一次，返回本K线产生的所有订单。"""
        try:
            if kline is None or not hasattr(kline, 'open_time'):
                logger.error("Invalid kline object received")
                return []

            kline_key = None
            if isinstance(context, dict):
                kline_key = context.get('kline_timestamp')
            if kline_key is None:
                kline_key = getattr(kline, 'open_time', None)

            async with self._lock:
                if kline_key is not None and kline_key == self._last_decision_timestamp:
                    logger.debug("Duplicate decision for same kline, skipping")
                    return []
                self._last_decision_timestamp = kline_key

                now = time.monotonic()
                if self._in_cooldown and (now < self._cooldown_until):
                    remaining = self._cooldown_until - now
                    logger.info("System in cooldown, %.1f sec remaining", remaining)
                    return []

                self._reset_module_status()

                if self._exceed_signal_limit_locked(now):
                    logger.warning("Signal frequency limit exceeded, entering cooldown")
                    self._enter_cooldown_locked(MAX_SIGNALS_COOLDOWN)
                    return []

            # 防御性上下文（浅拷贝顶层 + 关键嵌套）
            if not isinstance(context, dict):
                context = {}
            context = dict(context)
            if 'features' in context and isinstance(context['features'], dict):
                context['features'] = dict(context['features'])
            context.setdefault('symbol', 'BTCUSDT')
            context.setdefault('last_price', 0.0)
            context.setdefault('current_kline', kline)
            context.setdefault('atr_3m', 0.0)
            context['current_position_direction'] = self._current_position_direction

            raw_signals = await self._collect_all_signals(kline, context, portfolio)
            orders = self._arbitrate_signals(raw_signals, context, portfolio)

            if self.reduce_only:
                orders = [
                    o for o in orders
                    if getattr(o, 'action', None) in ('CLOSE', 'REDUCE', 'CLOSE_ALL', 'REDUCE_50', 'PANIC')
                ]

            final_orders = self._apply_risk_and_sizing(
                orders, portfolio, context.get('symbol', 'BTCUSDT')
            )

            now = time.monotonic()
            if final_orders and (now - self._last_audit_log_time) > 0.1:
                self._log_decision(kline, context, final_orders)
                self._last_audit_log_time = now

            async with self._lock:
                self._signal_timestamps.append(now)
                if any(getattr(o, 'action', None) == 'PANIC' for o in final_orders):
                    self._enter_cooldown_locked(PANIC_COOLDOWN)

            return final_orders

        except asyncio.CancelledError:
            logger.info("Decision cancelled")
            raise
        except Exception as e:
            logger.critical("Unhandled exception in decision maker: %s", e, exc_info=True)
            async with self._lock:
                self._all_modules_fault()
            return []

    def _enter_cooldown_locked(self, duration_sec: float):
        self._in_cooldown = True
        self._cooldown_until = time.monotonic() + duration_sec
        logger.warning("Entering cooldown for %.1f seconds", duration_sec)

    def _reset_module_status(self):
        for key in list(self._module_status.keys()):
            self._module_status[key] = True

    def _all_modules_fault(self):
        for key in list(self._module_status.keys()):
            self._module_status[key] = False

    async def _is_signal_duplicate_async(self, module: str, direction: Optional[str]) -> bool:
        """带锁的信号去重检查"""
        key = f"{module}:{direction or 'NONE'}"
        now = time.monotonic()
        async with self._lock:
            last_time = self._recent_signals.get(key, 0.0)
            if now - last_time < self._signal_dedup_window:
                return True
            self._recent_signals[key] = now
            if len(self._recent_signals) > MAX_RECENT_SIGNALS:
                cutoff = now - self._signal_dedup_window * 2
                self._recent_signals = {
                    k: v for k, v in self._recent_signals.items() if v > cutoff
                }
            return False

    async def _collect_all_signals(self, kline, context, portfolio) -> List[Order]:
        signals: List[Order] = []

        if self.module_enabled.get('EscapeDetector', True):
            escape_order = await self._safe_call_module(
                'EscapeDetector', self._process_escape, kline, context, portfolio
            )
            if escape_order:
                signals.append(escape_order)
                if getattr(escape_order, 'action', None) in ('CLOSE_ALL', 'PANIC'):
                    return signals

        if self.module_enabled.get('CallbackDrop', True):
            drop = await self._safe_call_module(
                'CallbackDrop', self._process_callback_drop, kline, context, portfolio
            )
            if drop and not await self._is_signal_duplicate_async(
                'CallbackDrop', getattr(drop, 'direction', None)
            ):
                signals.append(drop)

        if self.module_enabled.get('Recapture', True):
            recapture = await self._safe_call_module(
                'Recapture', self._process_recapture, kline, context, portfolio
            )
            if recapture and not await self._is_signal_duplicate_async(
                'Recapture', getattr(recapture, 'direction', None)
            ):
                signals.append(recapture)

        if self.module_enabled.get('PullbackAdd', True):
            pullback = await self._safe_call_module(
                'PullbackAdd', self._process_pullback_add, kline, context, portfolio
            )
            if pullback and not await self._is_signal_duplicate_async(
                'PullbackAdd', getattr(pullback, 'direction', None)
            ):
                signals.append(pullback)

        if self.module_enabled.get('GuerrillaChase', False):
            guerrilla = await self._safe_call_module(
                'GuerrillaChase', self._process_guerrilla_chase, kline, context, portfolio
            )
            if guerrilla and not await self._is_signal_duplicate_async(
                'GuerrillaChase', getattr(guerrilla, 'direction', None)
            ):
                signals.append(guerrilla)

        if self.module_enabled.get('TrendProbabilityFilter', True):
            prob = await self._safe_call_module(
                'TrendProbabilityFilter', self._process_trend_prob_filter, kline, context, portfolio
            )
            if prob and not await self._is_signal_duplicate_async(
                'TrendProbabilityFilter', getattr(prob, 'direction', None)
            ):
                signals.append(prob)

        return signals

    async def _safe_call_module(self, module_name: str, func, *args) -> Optional[Order]:
        if not self.module_enabled.get(module_name, True):
            return None
        module = self._get_module(module_name)
        if module is None:
            self._module_status[module_name] = False
            logger.error("Module %s is not initialized or GC'd", module_name)
            return None

        timeout = MODULE_TIMEOUTS.get(module_name, DEFAULT_MODULE_TIMEOUT)
        task = None
        try:
            task = asyncio.ensure_future(func(*args))
            result = await asyncio.wait_for(task, timeout=timeout)
            self._module_status[module_name] = True
            return result
        except asyncio.TimeoutError:
            logger.error("Module %s timed out after %.1fs", module_name, timeout)
            self._module_status[module_name] = False
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        except asyncio.CancelledError:
            logger.warning("Module %s was cancelled", module_name)
            self._module_status[module_name] = False
            raise
        except Exception as e:
            logger.error("Module %s error: %s", module_name, e, exc_info=True)
            self._module_status[module_name] = False
        return None

    def _arbitrate_signals(self, raw_signals: List[Order], context: dict, portfolio: dict) -> List[Order]:
        if not raw_signals:
            return []

        priority_map: Dict[str, int] = {name: idx for idx, name in enumerate(self.signal_priority)}

        def _priority_key(o: Order) -> int:
            mod = ''
            if hasattr(o, 'metadata') and isinstance(o.metadata, dict):
                mod = o.metadata.get('module', '') or ''
            key = MODULE_TO_PRIORITY_KEY.get(mod, mod)
            return priority_map.get(key, 999)

        raw_signals = sorted(raw_signals, key=_priority_key)

        close_all = [
            o for o in raw_signals
            if getattr(o, 'action', None) in ('CLOSE_ALL', 'PANIC')
        ]
        if close_all:
            return close_all[:1]

        orders: List[Order] = []
        seen_open: Set[Tuple[str, str]] = set()

        for signal in raw_signals:
            action = getattr(signal, 'action', None)
            if action in ('CLOSE', 'REDUCE', 'CLOSE_ALL', 'REDUCE_50', 'PANIC'):
                orders.append(signal)
                continue

            module = 'unknown'
            if hasattr(signal, 'metadata') and isinstance(signal.metadata, dict):
                module = signal.metadata.get('module', 'unknown') or 'unknown'
            direction = getattr(signal, 'direction', None) or 'LONG'
            key = (module, direction)
            if key in seen_open:
                continue
            seen_open.add(key)
            orders.append(signal)

        open_orders = [o for o in orders if getattr(o, 'action', None) in ('OPEN', 'ADD')]
        if open_orders:
            directions = {getattr(o, 'direction', None) for o in open_orders}
            if len(directions) > 1:
                first_dir = getattr(open_orders[0], 'direction', None)
                logger.warning("Arbitration: conflicting directions, keeping %s", first_dir)
                orders = [
                    o for o in orders
                    if getattr(o, 'action', None) not in ('OPEN', 'ADD')
                    or getattr(o, 'direction', None) == first_dir
                ]

        return orders

    async def _process_escape(self, kline, context, portfolio):
        module = self._get_module('EscapeDetector')
        if not module:
            return None
        try:
            features = context.get('features', {}) or {}
            escape_signal = await module.evaluate(features, context)
            action = getattr(escape_signal, 'action', None) if escape_signal else None
            if escape_signal and action in ('REDUCE_50', 'CLOSE_ALL', 'REDUCE', 'CLOSE', 'PANIC'):
                pos_dir = context.get('current_position_direction')
                if not pos_dir:
                    logger.info("Escape signal ignored: no position")
                    return None
                close_direction = 'SHORT' if pos_dir == 'LONG' else 'LONG'
                if action == 'REDUCE_50':
                    action = 'REDUCE'
                # 区分 close / reduce 优先级
                meta_module = 'escape_reduce' if action == 'REDUCE' else 'escape'
                order = Order(
                    symbol=context.get('symbol', 'BTCUSDT'),
                    action=action,
                    direction=close_direction,
                    order_type='MARKET',
                    price=float(context.get('last_price', 0.0) or 0.0),
                    size=0.0,
                    metadata={'module': meta_module, 'reason': 'stage_top'}
                )
                return order
        except Exception as e:
            logger.error("Escape processing failed: %s", e)
            self._module_status['EscapeDetector'] = False
        return None

    async def _process_recapture(self, kline, context, portfolio):
        module = self._get_module('Recapture')
        if not module:
            return None
        try:
            order = await module.evaluate(
                context.get('symbol', 'BTCUSDT'), kline,
                context.get('features', {}) or {}, context, portfolio
            )
            if order:
                if not hasattr(order, 'metadata') or order.metadata is None:
                    order.metadata = {}
                order.metadata['module'] = 'recapture'
                return order
        except Exception as e:
            logger.error("Recapture processing failed: %s", e)
            self._module_status['Recapture'] = False
        return None

    async def _process_callback_drop(self, kline, context, portfolio):
        module = self._get_module('CallbackDrop')
        if not module:
            return None
        try:
            order = await module.evaluate(
                context.get('symbol', 'BTCUSDT'), kline,
                context.get('features', {}) or {}, context, portfolio
            )
            if order:
                if not hasattr(order, 'metadata') or order.metadata is None:
                    order.metadata = {}
                order.metadata['module'] = 'callback_drop'
                return order
        except Exception as e:
            logger.error("CallbackDrop processing failed: %s", e)
            self._module_status['CallbackDrop'] = False
        return None

    async def _process_pullback_add(self, kline, context, portfolio):
        module = self._get_module('PullbackAdd')
        if not module:
            return None
        try:
            order = await module.evaluate(
                context.get('symbol', 'BTCUSDT'), kline,
                context.get('features', {}) or {}, context, portfolio
            )
            if order:
                if not hasattr(order, 'metadata') or order.metadata is None:
                    order.metadata = {}
                order.metadata['module'] = 'pullback_add'
                return order
        except Exception as e:
            logger.error("PullbackAdd processing failed: %s", e)
            self._module_status['PullbackAdd'] = False
        return None

    async def _process_guerrilla_chase(self, kline, context, portfolio):
        module = self._get_module('GuerrillaChase')
        if not module:
            return None
        try:
            order = await module.evaluate(kline, context)
            if order:
                if not hasattr(order, 'metadata') or order.metadata is None:
                    order.metadata = {}
                order.metadata['module'] = 'guerrilla_chase'
                return order
        except Exception as e:
            logger.error("GuerrillaChase processing failed: %s", e)
            self._module_status['GuerrillaChase'] = False
        return None

    async def _process_trend_prob_filter(self, kline, context, portfolio):
        module = self._get_module('TrendProbabilityFilter')
        if not module:
            return None
        try:
            prob_data = await module.compute(kline, context)
            if (prob_data
                    and float(prob_data.get('trend_probability', 0.0) or 0.0) > 0.7
                    and not bool(prob_data.get('is_chaotic', True))):
                direction = prob_data.get('direction', 'LONG') or 'LONG'
                order = Order(
                    symbol=context.get('symbol', 'BTCUSDT'),
                    action='OPEN',
                    direction=direction,
                    order_type='MARKET',
                    price=float(context.get('last_price', 0.0) or 0.0),
                    size=0.0,
                    metadata={'module': 'trend_prob_filter'}
                )
                return order
        except Exception as e:
            logger.error("TrendProbFilter processing failed: %s", e)
            self._module_status['TrendProbabilityFilter'] = False
        return None

    def _apply_risk_and_sizing(self, orders: List[Order], portfolio: Union[dict, None], symbol: str) -> List[Order]:
        sizer = self._position_sizer() if self._position_sizer else None
        firewall = self._risk_firewall() if self._risk_firewall else None

        if not sizer or not firewall:
            logger.error("Position sizer or firewall not initialized or GC'd")
            self._module_status['PositionSizer'] = False
            self._module_status['RiskFirewall'] = False
            return []

        if not isinstance(portfolio, dict):
            portfolio = {}

        final_orders: List[Order] = []
        try:
            equity = float(portfolio.get('total_equity', 0.0) or 0.0)
        except (TypeError, ValueError):
            equity = 0.0
        if equity <= FLOAT_TOLERANCE:
            logger.error("Invalid portfolio equity: %s", equity)
            return []

        for order in orders:
            try:
                price = float(getattr(order, 'price', 0.0) or 0.0)
                if price <= FLOAT_TOLERANCE:
                    price = float(portfolio.get('last_price', 0.0) or 0.0)
                if price <= FLOAT_TOLERANCE:
                    logger.warning("Invalid order price, skipping")
                    continue

                action = getattr(order, 'action', None)
                if action in ('CLOSE', 'REDUCE', 'CLOSE_ALL', 'REDUCE_50', 'PANIC'):
                    if firewall.check(order, portfolio):
                        final_orders.append(order)
                    else:
                        mod = (getattr(order, 'metadata', None) or {}).get('module', 'unknown')
                        logger.warning("Close order rejected by firewall: %s", mod)
                    continue

                qty = float(sizer.calculate(equity, price, symbol) or 0.0)
                if qty <= FLOAT_TOLERANCE:
                    logger.info("Order skipped due to zero quantity (min notional)")
                    continue
                order.size = qty

                if firewall.check(order, portfolio):
                    final_orders.append(order)
                else:
                    mod = (getattr(order, 'metadata', None) or {}).get('module', 'unknown')
                    logger.warning("Order rejected by firewall: %s", mod)
            except Exception as e:
                mod = (getattr(order, 'metadata', None) or {}).get('module', 'unknown')
                logger.error("Position sizing/firewall error for order %s: %s", mod, e)
                self._module_status['PositionSizer'] = False
                self._module_status['RiskFirewall'] = False
        return final_orders

    def _log_decision(self, kline, context, orders):
        if not orders:
            return
        try:
            snap = {
                'price': round(float(context.get('last_price', 0.0) or 0.0), 2),
                'kma': round(float(context.get('kma', 0.0) or 0.0), 2),
                'atr': round(float(context.get('atr_3m', 0.0) or 0.0), 2),
                'pos_dir': self._current_position_direction,
                'trend_prob': round(
                    float((context.get('features') or {}).get('trend_probability', 0.0) or 0.0), 4
                ),
            }
            for order in orders:
                logger.info(
                    "AUDIT: Order | sym=%s act=%s dir=%s sz=%.6f px=%.2f mod=%s snap=%s",
                    getattr(order, 'symbol', '?'),
                    getattr(order, 'action', '?'),
                    getattr(order, 'direction', '?'),
                    float(getattr(order, 'size', 0.0) or 0.0),
                    float(getattr(order, 'price', 0.0) or 0.0),
                    (getattr(order, 'metadata', None) or {}).get('module', 'unknown'),
                    snap
                )
        except Exception as e:
            logger.error("Audit log failed: %s", e)

    def _exceed_signal_limit_locked(self, now: float) -> bool:
        cutoff = now - SIGNAL_WINDOW_SEC
        while self._signal_timestamps and self._signal_timestamps[0] <= cutoff:
            self._signal_timestamps.popleft()
        return len(self._signal_timestamps) >= self.max_signals_per_hour

    def get_module_status(self) -> Dict[str, bool]:
        return self._module_status.copy()

    def teardown(self):
        """清理资源，优雅关闭时调用"""
        self._in_cooldown = True
        self._cooldown_until = float('inf')
        self._signal_timestamps.clear()
        self._recent_signals.clear()
        self._last_decision_timestamp = None
        logger.info("Decision maker teardown initiated")
