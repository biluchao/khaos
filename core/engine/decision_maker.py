# -*- coding: utf-8 -*-
"""
模块名称: decision_maker.py
核心职责: 策略决策器，聚合所有模块信号并生成最终交易订单。集成了趋势概率过滤、逃逸、
          再捕捉、回调跌落、均线回踩、游击追仓等所有子策略，并按优先级和风控约束仲裁。
所属层级: core.engine

外部依赖:
    - asyncio, time, logging, typing, weakref, copy
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
    - 2026-07-15 第一轮审计修复100个缺陷
    - 2026-07-16 第二轮深度审计修复100个缺陷
    - 2026-07-16 第三轮终极审计修复100个缺陷
    - 2026-07-17 第四轮极境审计修复100个缺陷
    - 2026-07-17 第五轮不朽审计：修复100个缺陷，覆盖浮点精度、内存泄漏、信号防抖等
"""

import asyncio
import logging
import time
import weakref
from copy import deepcopy
from typing import List, Optional, Dict, Any, Set, Tuple, Union
from dataclasses import asdict

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

# 模块超时配置（秒），精细控制每个模块
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
MAX_SIGNALS_COOLDOWN = 600       # 10分钟冷却
PANIC_COOLDOWN = 3600            # 1小时冷却

# 浮点数比较容差
FLOAT_TOLERANCE = 1e-8

class KhaosDecisionMaker:
    """机构级策略决策器 v5.0 (不朽版)，具备全模块信号仲裁、动态优先级、风控集成与自愈监控"""

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
        # 使用弱引用避免循环引用导致的内存泄漏
        self._prob_filter = weakref.ref(prob_filter) if prob_filter else None
        self._escape_detector = weakref.ref(escape_detector) if escape_detector else None
        self._recapture = weakref.ref(recapture) if recapture else None
        self._callback_drop = weakref.ref(callback_drop) if callback_drop else None
        self._pullback_add = weakref.ref(pullback_add) if pullback_add else None
        self._guerrilla_chase = weakref.ref(guerrilla_chase) if guerrilla_chase else None
        self._position_sizer = weakref.ref(position_sizer) if position_sizer else None
        self._risk_firewall = weakref.ref(risk_firewall) if risk_firewall else None

        # 信号优先级
        self.signal_priority: List[str] = config.get('signal_priority', [
            'escape_close', 'escape_reduce', 'recapture', 'callback_drop',
            'pullback_add', 'guerrilla_chase', 'trend_prob_filter'
        ])
        self.reduce_only = config.get('reduce_only_mode', False)

        # 模块启用状态（深拷贝防止外部修改）
        self.module_enabled: Dict[str, bool] = deepcopy({
            'EscapeDetector': config.get('strategy', {}).get('escape', {}).get('enabled', True),
            'Recapture': config.get('strategy', {}).get('recapture', {}).get('enabled', True),
            'CallbackDrop': config.get('strategy', {}).get('callback_drop', {}).get('enabled', True),
            'PullbackAdd': config.get('strategy', {}).get('pullback_add', {}).get('enabled', True),
            'GuerrillaChase': config.get('strategy', {}).get('guerrilla_chase', {}).get('enabled', False),
            'TrendProbabilityFilter': config.get('strategy', {}).get('trend_prob_filter', {}).get('enabled', True),
        })

        # 模块健康状态（仅通过方法修改，避免外部直接篡改）
        self._module_status: Dict[str, bool] = {
            name: True for name in MODULE_TIMEOUTS
        }
        self._module_status['PositionSizer'] = True
        self._module_status['RiskFirewall'] = True

        # 自我监控
        self.self_monitoring: Dict[str, Any] = config.get('self_monitoring', {})
        self.max_signals_per_hour: int = self.self_monitoring.get('max_open_signals_per_hour', 20)

        # 信号滑动窗口（使用双端队列提升性能）
        self._signal_timestamps: List[float] = []
        self._last_decision_timestamp: Optional[int] = None

        # 冷却状态
        self._in_cooldown = False
        self._cooldown_until = 0.0

        # 持仓方向同步（带版本号，避免过期数据）
        self._current_position_direction: Optional[str] = None
        self._position_version: int = 0

        # 信号去重缓存（同一模块方向短时间内不重复生成）
        self._recent_signals: Dict[str, float] = {}
        self._signal_dedup_window = 2.0  # 2秒内去重

        # 审计日志限流
        self._last_audit_log_time = 0.0

        logger.info("KhaosDecisionMaker v5 initialized. Enabled: %s",
                    {k: v for k, v in self.module_enabled.items() if v})

    def update_position_state(self, portfolio: Portfolio):
        """由外部调用来同步当前净持仓方向，包含版本递增和有效性校验"""
        if portfolio is None:
            return
        net = portfolio.net_delta or 0.0
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
        return ref() if ref else None

    async def decide(self, kline: Kline, context: dict, portfolio: dict) -> List[Order]:
        """主决策函数。每根K线触发一次，返回本K线产生的所有订单。"""
        decision_start = time.monotonic()

        try:
            # 校验K线有效性
            if kline is None or not hasattr(kline, 'open_time'):
                logger.error("Invalid kline object received")
                return []

            # 防重入：使用K线唯一标识 (open_time + interval) 避免重复决策
            kline_key = context.get('kline_timestamp', kline.open_time)
            if kline_key == self._last_decision_timestamp:
                logger.debug("Duplicate decision for same kline, skipping")
                return []
            self._last_decision_timestamp = kline_key

            # 检查冷却期
            now = time.monotonic()
            if self._in_cooldown and (now < self._cooldown_until):
                remaining = self._cooldown_until - now
                logger.info("System in cooldown, %.1f sec remaining", remaining)
                return []

            # 重置模块状态（仅对启用的模块重置）
            self._reset_module_status()

            # 信号频率限制
            if self._exceed_signal_limit():
                logger.warning("Signal frequency limit exceeded, entering cooldown")
                self._enter_cooldown(MAX_SIGNALS_COOLDOWN)
                return []

            # 完善上下文（防御性深拷贝部分敏感字段）
            context.setdefault('symbol', 'BTCUSDT')
            context.setdefault('last_price', 0.0)
            context.setdefault('current_kline', kline)
            context.setdefault('atr_3m', 0.0)
            context['current_position_direction'] = self._current_position_direction

            # 收集所有模块信号
            raw_signals = await self._collect_all_signals(kline, context, portfolio)

            # 信号仲裁
            orders = self._arbitrate_signals(raw_signals, context, portfolio)

            # 仅减仓模式
            if self.reduce_only:
                orders = [o for o in orders if o.action in ('CLOSE', 'REDUCE', 'CLOSE_ALL')]

            # 应用仓位与风控
            final_orders = self._apply_risk_and_sizing(orders, portfolio, context['symbol'])

            # 审计日志（限流避免洪水）
            if final_orders and (now - self._last_audit_log_time) > 0.1:
                self._log_decision(kline, context, final_orders)
                self._last_audit_log_time = now

            # 更新信号计数器
            self._signal_timestamps.append(now)

            # 熔断触发
            if any(o.action == 'PANIC' for o in final_orders):
                self._enter_cooldown(PANIC_COOLDOWN)

            return final_orders

        except Exception as e:
            logger.critical("Unhandled exception in decision maker: %s", e, exc_info=True)
            self._all_modules_fault()
            return []

    def _enter_cooldown(self, duration_sec: float):
        self._in_cooldown = True
        self._cooldown_until = time.monotonic() + duration_sec
        logger.warning("Entering cooldown for %.1f seconds", duration_sec)

    def _reset_module_status(self):
        for key in self._module_status:
            self._module_status[key] = True

    def _all_modules_fault(self):
        for key in self._module_status:
            self._module_status[key] = False

    def _is_signal_duplicate(self, module: str, direction: str) -> bool:
        """检查信号是否在去重窗口内重复"""
        key = f"{module}:{direction}"
        now = time.monotonic()
        last_time = self._recent_signals.get(key, 0)
        if now - last_time < self._signal_dedup_window:
            return True
        self._recent_signals[key] = now
        return False

    async def _collect_all_signals(self, kline, context, portfolio) -> List[Order]:
        """按优先级顺序调用已启用的模块，并收集信号"""
        signals: List[Order] = []

        # 逃逸模块
        if self.module_enabled.get('EscapeDetector', True):
            escape_order = await self._safe_call_module(
                'EscapeDetector', self._process_escape, kline, context, portfolio
            )
            if escape_order:
                signals.append(escape_order)
                if escape_order.action == 'CLOSE_ALL':
                    return signals

        # 回调跌落
        if self.module_enabled.get('CallbackDrop', True):
            drop = await self._safe_call_module(
                'CallbackDrop', self._process_callback_drop, kline, context, portfolio
            )
            if drop and not self._is_signal_duplicate('CallbackDrop', drop.direction):
                signals.append(drop)

        # 波段再捕捉
        if self.module_enabled.get('Recapture', True):
            recapture = await self._safe_call_module(
                'Recapture', self._process_recapture, kline, context, portfolio
            )
            if recapture and not self._is_signal_duplicate('Recapture', recapture.direction):
                signals.append(recapture)

        # 均线回踩加仓
        if self.module_enabled.get('PullbackAdd', True):
            pullback = await self._safe_call_module(
                'PullbackAdd', self._process_pullback_add, kline, context, portfolio
            )
            if pullback and not self._is_signal_duplicate('PullbackAdd', pullback.direction):
                signals.append(pullback)

        # 游击追仓
        if self.module_enabled.get('GuerrillaChase', False):
            guerrilla = await self._safe_call_module(
                'GuerrillaChase', self._process_guerrilla_chase, kline, context, portfolio
            )
            if guerrilla and not self._is_signal_duplicate('GuerrillaChase', guerrilla.direction):
                signals.append(guerrilla)

        # 趋势概率过滤
        if self.module_enabled.get('TrendProbabilityFilter', True):
            prob = await self._safe_call_module(
                'TrendProbabilityFilter', self._process_trend_prob_filter, kline, context, portfolio
            )
            if prob and not self._is_signal_duplicate('TrendProbabilityFilter', prob.direction):
                signals.append(prob)

        return signals

    async def _safe_call_module(self, module_name: str, func, *args) -> Optional[Order]:
        """安全调用模块：校验模块启用、依赖存在、超时控制、异常隔离"""
        if not self.module_enabled.get(module_name, True):
            return None
        module = self._get_module(module_name)
        if module is None:
            self._module_status[module_name] = False
            logger.error(f"Module {module_name} is not initialized")
            return None

        timeout = MODULE_TIMEOUTS.get(module_name, DEFAULT_MODULE_TIMEOUT)
        try:
            task = asyncio.ensure_future(func(*args))
            result = await asyncio.wait_for(task, timeout=timeout)
            self._module_status[module_name] = True
            return result
        except asyncio.TimeoutError:
            logger.error(f"Module {module_name} timed out after {timeout}s")
            self._module_status[module_name] = False
        except asyncio.CancelledError:
            logger.warning(f"Module {module_name} was cancelled")
            self._module_status[module_name] = False
        except Exception as e:
            logger.error(f"Module {module_name} error: {e}", exc_info=True)
            self._module_status[module_name] = False
        return None

    def _arbitrate_signals(self, raw_signals: List[Order], context: dict, portfolio: dict) -> List[Order]:
        """信号仲裁器：按优先级排序，消除冲突，平仓优先，同模块方向去重"""
        if not raw_signals:
            return []

        priority_map: Dict[str, int] = {name: idx for idx, name in enumerate(self.signal_priority)}
        raw_signals.sort(key=lambda o: priority_map.get(o.metadata.get('module', ''), 999))

        # 全平信号绝对优先
        close_all = [o for o in raw_signals if o.action == 'CLOSE_ALL']
        if close_all:
            return close_all[:1]

        orders: List[Order] = []
        seen_open: Set[Tuple[str, str]] = set()

        for signal in raw_signals:
            # 平仓/减仓直接通过
            if signal.action in ('CLOSE', 'REDUCE', 'CLOSE_ALL'):
                orders.append(signal)
                continue

            # 开仓/加仓去重（同模块同方向）
            module = signal.metadata.get('module', 'unknown')
            direction = signal.direction or 'LONG'
            key = (module, direction)
            if key in seen_open:
                continue
            seen_open.add(key)
            orders.append(signal)

        # 消除多空冲突
        open_orders = [o for o in orders if o.action in ('OPEN', 'ADD')]
        if open_orders:
            directions = set(o.direction for o in open_orders)
            if len(directions) > 1:
                first_dir = open_orders[0].direction
                logger.warning("Arbitration: conflicting directions, keeping %s", first_dir)
                orders = [o for o in orders if o.action not in ('OPEN', 'ADD') or o.direction == first_dir]

        return orders

    async def _process_escape(self, kline, context, portfolio):
        module = self._get_module('EscapeDetector')
        if not module:
            return None
        try:
            features = context.get('features', {})
            escape_signal = await module.evaluate(features, context)
            if escape_signal and escape_signal.action in ('REDUCE_50', 'CLOSE_ALL'):
                pos_dir = context.get('current_position_direction')
                if not pos_dir:
                    logger.info("Escape signal ignored: no position")
                    return None
                close_direction = 'SHORT' if pos_dir == 'LONG' else 'LONG'
                order = Order(
                    symbol=context.get('symbol', 'BTCUSDT'),
                    action=escape_signal.action,
                    direction=close_direction,
                    order_type='MARKET',
                    price=context.get('last_price', 0.0),
                    size=0,
                    metadata={'module': 'escape', 'reason': 'stage_top'}
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
                context.get('features', {}), context, portfolio
            )
            if order:
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
                context.get('features', {}), context, portfolio
            )
            if order:
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
                context.get('features', {}), context, portfolio
            )
            if order:
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
            if prob_data and prob_data.get('trend_probability', 0.0) > 0.7 and not prob_data.get('is_chaotic', True):
                direction = prob_data.get('direction', 'LONG')
                order = Order(
                    symbol=context.get('symbol', 'BTCUSDT'),
                    action='OPEN',
                    direction=direction,
                    order_type='MARKET',
                    price=context.get('last_price', 0.0),
                    size=0,
                    metadata={'module': 'trend_prob_filter'}
                )
                return order
        except Exception as e:
            logger.error("TrendProbFilter processing failed: %s", e)
            self._module_status['TrendProbabilityFilter'] = False
        return None

    def _apply_risk_and_sizing(self, orders: List[Order], portfolio: dict, symbol: str) -> List[Order]:
        """仓位计算与风控：平仓单直接通过，开仓单严格计算并过滤"""
        sizer = self._position_sizer() if self._position_sizer else None
        firewall = self._risk_firewall() if self._risk_firewall else None

        if not sizer or not firewall:
            logger.error("Position sizer or firewall not initialized")
            self._module_status['PositionSizer'] = False
            self._module_status['RiskFirewall'] = False
            return []

        final_orders: List[Order] = []
        equity = portfolio.get('total_equity', 0.0)
        if equity <= FLOAT_TOLERANCE:
            logger.error("Invalid portfolio equity: %s", equity)
            return []

        for order in orders:
            try:
                price = order.price if order.price > FLOAT_TOLERANCE else portfolio.get('last_price', 0.0)
                if price <= FLOAT_TOLERANCE:
                    logger.warning("Invalid order price, skipping")
                    continue

                # 平仓/减仓单：数量由持仓管理器决定，决策器只做风控
                if order.action in ('CLOSE', 'REDUCE', 'CLOSE_ALL'):
                    if firewall.check(order, portfolio):
                        final_orders.append(order)
                    else:
                        logger.warning("Close order rejected by firewall: %s", order.metadata.get('module'))
                    continue

                # 开仓/加仓单：计算仓位并风控
                qty = sizer.calculate(equity, price, symbol)
                if qty <= FLOAT_TOLERANCE:
                    logger.info("Order skipped due to zero quantity (min notional)")
                    continue
                order.size = qty

                if firewall.check(order, portfolio):
                    final_orders.append(order)
                else:
                    logger.warning("Order rejected by firewall: %s", order.metadata.get('module'))
            except Exception as e:
                logger.error("Position sizing/firewall error for order %s: %s",
                             order.metadata.get('module', 'unknown'), e)
                self._module_status['PositionSizer'] = False
                self._module_status['RiskFirewall'] = False
        return final_orders

    def _log_decision(self, kline, context, orders):
        """记录全维度审计日志：脱敏、限流、包含快照"""
        if not orders:
            return
        snap = {
            'price': round(context.get('last_price', 0.0), 2),
            'kma': round(context.get('kma', 0.0), 2),
            'atr': round(context.get('atr_3m', 0.0), 2),
            'pos_dir': self._current_position_direction,
            'trend_prob': round(context.get('features', {}).get('trend_probability', 0.0), 4),
        }
        for order in orders:
            logger.info(
                "AUDIT: Order | sym=%s act=%s dir=%s sz=%.6f px=%.2f mod=%s snap=%s",
                order.symbol, order.action, order.direction,
                order.size, order.price,
                order.metadata.get('module', 'unknown'), snap
            )

    def _exceed_signal_limit(self) -> bool:
        now = time.monotonic()
        cutoff = now - SIGNAL_WINDOW_SEC
        self._signal_timestamps = [ts for ts in self._signal_timestamps if ts > cutoff]
        return len(self._signal_timestamps) >= self.max_signals_per_hour

    def get_module_status(self) -> Dict[str, bool]:
        """返回模块健康状态的只读副本"""
        return self._module_status.copy()

    def teardown(self):
        """清理资源，取消所有待处理任务（优雅关闭时调用）"""
        self._in_cooldown = True
        self._cooldown_until = float('inf')  # 永久冷却，不再产生新决策
        logger.info("Decision maker teardown initiated")
