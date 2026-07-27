# -*- coding: utf-8 -*-
"""
模块名称: decision_maker.py
核心职责: 量化策略决策中枢，协调多信号源、共振、优先级、风控，生成最终交易订单。
          本版本具备异步死锁保护、批次追踪、深度配置一致性等极致机构级特性。
所属层级: core.engine

依赖:
    - asyncio, time, logging, copy, uuid
    - typing (Dict, List, Optional, Any, Callable, Tuple)
    - core.models.kline, core.models.signal, core.models.order
    - core.engine.context_pipeline, signal_assembler, resonance_evaluator,
      priority_executor, market_regime_monitor
    - core.risk.global_risk_bus
    - core.interfaces.FeatureComputer
    - config 模块

作者: KHAOS System Architect
版本: 4.0.0 (机构级最终版)
"""

import asyncio
import logging
import time
import copy
import uuid
from typing import Dict, List, Optional, Any, Callable, Set, Tuple, Type

from core.models.kline import Kline
from core.models.signal import Signal, SignalAction
from core.models.order import Order
from core.engine.context_pipeline import ContextPipeline
from core.engine.signal_assembler import SignalAssembler
from core.engine.resonance_evaluator import ResonanceEvaluator, ResonanceState
from core.engine.priority_executor import PriorityExecutor
from core.engine.market_regime_monitor import MarketRegimeMonitor
from core.risk.global_risk_bus import GlobalRiskBus
from core.interfaces import FeatureComputer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认常量
# ---------------------------------------------------------------------------
DEFAULT_MAX_SIGNALS = 20
DEFAULT_MAX_MODULES = 15
RISK_BUS_TIMEOUT_SEC = 2.0
MODULE_COMPUTE_TIMEOUT_SEC = 1.0
RESONANCE_FLOOR = 0.3
RESONANCE_CEIL = 2.0
MAX_MODULE_ERRORS = 3
CONTEXT_BUILD_MAX_RETRIES = 1
# 决策总超时（秒）—— 防止整体决策流程永久挂起
DECISION_TIMEOUT_SEC = 10.0


class DecisionMaker:
    """策略决策中枢（机构级 v4.0 最终版）"""

    def __init__(
        self,
        config: Any,
        context_pipeline: ContextPipeline,
        signal_assembler: SignalAssembler,
        resonance_evaluator: ResonanceEvaluator,
        priority_executor: PriorityExecutor,
        market_regime_monitor: MarketRegimeMonitor,
        risk_bus: GlobalRiskBus,
        signal_modules: Dict[str, FeatureComputer],
        metrics_collector: Any = None,
    ):
        self.config = config
        self.context_pipeline = context_pipeline
        self.signal_assembler = signal_assembler
        self.resonance_evaluator = resonance_evaluator
        self.priority_executor = priority_executor
        self.market_regime_monitor = market_regime_monitor
        self.risk_bus = risk_bus
        self.signal_modules = dict(signal_modules)
        self.metrics = metrics_collector

        self._refresh_config()
        self._reduce_only_mode = False
        self._cooldown_until = 0.0
        self._module_errors: Dict[str, int] = {}
        self._disabled_modules: Set[str] = set()

        # 动态配置回调与热重载
        self._config_change_callback: Optional[Callable] = None

        # 自定义脱敏规则
        self._sanitize_rules: List[Callable[[str], str]] = [
            lambda s: s.replace(str(getattr(self.config, 'api_keys', '')), '***'),
        ]

    def _refresh_config(self):
        """刷新内部配置快照"""
        dm_cfg = getattr(self.config, 'decision_maker', None) or {}
        self._max_signals_per_iter = getattr(dm_cfg, 'max_signals_per_iter', DEFAULT_MAX_SIGNALS)
        self._max_modules = getattr(dm_cfg, 'max_modules', DEFAULT_MAX_MODULES)
        self._audit_enabled = getattr(self.config, 'audit', {}).get('enabled', False)
        self._sanitize_logs = getattr(dm_cfg, 'sanitize_logs', True)
        self._module_compute_timeout = getattr(dm_cfg, 'module_compute_timeout', MODULE_COMPUTE_TIMEOUT_SEC)
        self._decision_timeout = getattr(dm_cfg, 'decision_timeout', DECISION_TIMEOUT_SEC)

    # -------------------------------------------------------------------------
    # 主决策入口（受总超时保护）
    # -------------------------------------------------------------------------
    async def decide(self, kline: Kline, base_context: dict) -> List[Order]:
        batch_id = uuid.uuid4().hex[:8]
        start_time = time.monotonic()
        orders: List[Order] = []

        try:
            # 使用 asyncio.wait_for 实现整个决策流程的超时熔断
            orders = await asyncio.wait_for(
                self._decide_impl(kline, base_context, batch_id),
                timeout=self._decision_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"Decision batch {batch_id} timed out after {self._decision_timeout}s")
            if self.metrics:
                self.metrics.record_decision_timeout()
        except Exception as e:
            logger.critical(f"Decision batch {batch_id} failed: {e}", exc_info=True)
        finally:
            elapsed = time.monotonic() - start_time
            logger.debug(f"Decision batch {batch_id} completed in {elapsed:.4f}s, orders: {len(orders)}")
            if self.metrics:
                self.metrics.record_decision_latency(elapsed)
        return orders

    async def _decide_impl(self, kline, base_context, batch_id) -> List[Order]:
        orders = []
        if not self._can_trade():
            logger.debug(f"Batch {batch_id}: trading restricted")

        # 构建上下文
        context = await self._build_context_with_retry(kline, base_context, batch_id)
        if context is None:
            logger.error(f"Batch {batch_id}: context build failed, aborting")
            return orders

        # 收集信号（带超时和模块隔离）
        raw_signals = await self._collect_signals(kline, context, batch_id)

        # 信号装配
        assembled = self.signal_assembler.assemble(raw_signals, context, batch_id)

        # 共振
        resonance = await self._evaluate_resonance(context, batch_id)
        assembled = self._apply_resonance(assembled, resonance)

        # 优先级
        final_signals = self.priority_executor.resolve_all(assembled, resonance, batch_id)

        # 限制过滤
        final_signals = self._apply_trading_restrictions(final_signals, batch_id)

        # 风控审批
        orders = await self._approve_and_generate_orders(final_signals, context, batch_id)
        return orders

    # -------------------------------------------------------------------------
    # 上下文构建（带批次追踪）
    # -------------------------------------------------------------------------
    async def _build_context_with_retry(self, kline, base_context, batch_id, max_retries=CONTEXT_BUILD_MAX_RETRIES):
        for attempt in range(max_retries + 1):
            try:
                context = await self.context_pipeline.enrich_context(
                    tf=self.config.strategy.primary_interval,
                    kline=kline,
                    base_context=base_context,
                )
                if context is not None:
                    context['decision_batch_id'] = batch_id
                    return self._ensure_context_defaults(context)
                logger.warning(f"Batch {batch_id}: context pipeline returned None (attempt {attempt+1})")
            except Exception as e:
                logger.warning(f"Batch {batch_id}: context build attempt {attempt+1} failed: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(0.1)
        # 回退上下文，填充必要默认值
        fallback = dict(base_context)
        fallback['decision_batch_id'] = batch_id
        fallback['context_degraded'] = True
        logger.warning(f"Batch {batch_id}: using degraded context")
        return self._ensure_context_defaults(fallback)

    def _ensure_context_defaults(self, ctx: dict) -> dict:
        """确保上下文包含必要的字段，防止后续 KeyError"""
        defaults = {
            'kma': None,
            'kma_slope': 0.0,
            'atr_3m': 0.0,
            'hmm_state_3m': 'UNKNOWN',
            'bpi': 0.0,
            'takerflow': 0.0,
            'resonance': ResonanceState(strength=0.0),
        }
        for k, v in defaults.items():
            if k not in ctx:
                ctx[k] = v
        return ctx

    # -------------------------------------------------------------------------
    # 信号收集（带批次追踪、超时、异步保护）
    # -------------------------------------------------------------------------
    async def _collect_signals(self, kline, context, batch_id) -> List[Signal]:
        raw_signals = []
        modules = copy.copy(self.signal_modules)
        tasks = {}

        for name, module in modules.items():
            if not self._is_module_enabled(name):
                continue
            tasks[name] = asyncio.create_task(self._compute_module(name, module, kline, context, batch_id))

        if not tasks:
            return raw_signals

        done, pending = await asyncio.wait(tasks.values(), timeout=self._module_compute_timeout)
        # 取消超时未完成的任务
        for task in pending:
            task.cancel()
            # 等待取消完成（忽略取消异常）
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        for name, task in tasks.items():
            try:
                if task in done:
                    result = task.result()
                    if isinstance(result, list):
                        raw_signals.extend(result)
                else:
                    logger.warning(f"Batch {batch_id}: module {name} did not complete within timeout")
                    self._handle_module_error(name)
            except Exception as e:
                logger.error(f"Batch {batch_id}: unexpected error from module {name}: {e}")
                self._handle_module_error(name)

        # 信号截断（优先保留平仓/减仓信号）
        return self._truncate_signals(raw_signals, batch_id)

    async def _compute_module(self, name, module, kline, context, batch_id):
        try:
            return await asyncio.wait_for(
                module.compute(kline, context),
                timeout=self._module_compute_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Batch {batch_id}: module {name} timed out")
            return []
        except Exception as e:
            logger.error(f"Batch {batch_id}: module {name} error: {e}")
            return []

    def _truncate_signals(self, raw_signals: List[Signal], batch_id) -> List[Signal]:
        if len(raw_signals) <= self._max_signals_per_iter:
            return raw_signals
        open_signals = [s for s in raw_signals if s.action in (SignalAction.OPEN, SignalAction.ADD)]
        close_signals = [s for s in raw_signals if s.action not in (SignalAction.OPEN, SignalAction.ADD)]
        # 保留所有平仓信号，剩余名额给开仓（按优先级排序）
        free_slots = self._max_signals_per_iter - len(close_signals)
        if free_slots > 0:
            # 开仓信号按优先级排序（例如共振、概率降序）
            open_signals.sort(key=lambda s: s.probability * s.size_multiplier, reverse=True)
            open_signals = open_signals[:free_slots]
        else:
            open_signals = []
        truncated = close_signals + open_signals
        logger.warning(f"Batch {batch_id}: signals truncated from {len(raw_signals)} to {len(truncated)}")
        return truncated

    # -------------------------------------------------------------------------
    # 共振
    # -------------------------------------------------------------------------
    async def _evaluate_resonance(self, context, batch_id) -> ResonanceState:
        try:
            state = await self.resonance_evaluator.evaluate(context)
            return state if state else ResonanceState(strength=0.0)
        except Exception:
            logger.warning(f"Batch {batch_id}: resonance evaluation failed, using neutral")
            return ResonanceState(strength=0.0)

    def _apply_resonance(self, signals: List[Signal], resonance: ResonanceState) -> List[Signal]:
        for sig in signals:
            if sig.action in (SignalAction.OPEN, SignalAction.ADD):
                multiplier = 1.0 + resonance.strength * 0.5
                multiplier = max(RESONANCE_FLOOR, min(RESONANCE_CEIL, multiplier))
                sig.size_multiplier *= multiplier
        return signals

    # -------------------------------------------------------------------------
    # 限制过滤
    # -------------------------------------------------------------------------
    def _apply_trading_restrictions(self, signals: List[Signal], batch_id) -> List[Signal]:
        filtered = []
        for sig in signals:
            if self._reduce_only_mode:
                if sig.action in (SignalAction.CLOSE_ALL, SignalAction.REDUCE_50, SignalAction.STOP_LOSS):
                    filtered.append(sig)
                else:
                    logger.debug(f"Batch {batch_id}: signal suppressed (reduce-only): {sig.action}")
                continue
            if not self._can_trade() and sig.action in (SignalAction.OPEN, SignalAction.ADD):
                logger.debug(f"Batch {batch_id}: signal suppressed (cooldown): {sig.action}")
                continue
            filtered.append(sig)
        return filtered

    # -------------------------------------------------------------------------
    # 风控审批
    # -------------------------------------------------------------------------
    async def _approve_and_generate_orders(self, signals: List[Signal], context, batch_id) -> List[Order]:
        orders = []
        for sig in signals:
            if sig.action == SignalAction.NO_ACTION:
                continue
            order = sig.to_order(self.config.execution)
            if order is None:
                logger.warning(f"Batch {batch_id}: failed to create order from signal {sig}")
                continue
            order.tracking_id = f"{batch_id}-{sig.module}-{sig.action}"
            try:
                approved = await asyncio.wait_for(
                    self.risk_bus.approve(order, context),
                    timeout=RISK_BUS_TIMEOUT_SEC,
                )
                if approved is True:
                    orders.append(order)
                    self._log_decision(sig, order, batch_id)
                else:
                    logger.info(f"Batch {batch_id}: order rejected by risk bus")
            except asyncio.TimeoutError:
                logger.error(f"Batch {batch_id}: risk bus approval timeout for {order.tracking_id}")
            except Exception as e:
                logger.error(f"Batch {batch_id}: risk bus error: {e}", exc_info=True)
        return orders

    # -------------------------------------------------------------------------
    # 日志与脱敏
    # -------------------------------------------------------------------------
    def _log_decision(self, signal: Signal, order: Order, batch_id):
        if self._audit_enabled:
            logger.info(f"AUDIT|{batch_id}|{signal.action}|{signal.direction}|qty={order.qty:.4f}")
        else:
            logger.debug(f"Decision|{batch_id}|{signal.summary()} -> {self._sanitize_order(order)}")

    def _sanitize_order(self, order: Order) -> str:
        if self._sanitize_logs:
            base = f"Order({order.symbol} {order.side} qty={order.qty:.4f})"
            for rule in self._sanitize_rules:
                base = rule(base)
            return base
        return str(order)

    def register_sanitize_rule(self, rule: Callable[[str], str]):
        """注册自定义脱敏函数"""
        self._sanitize_rules.append(rule)

    # -------------------------------------------------------------------------
    # 状态控制
    # -------------------------------------------------------------------------
    def _can_trade(self) -> bool:
        if self._reduce_only_mode:
            return False
        if self._cooldown_until > 0 and time.monotonic() < self._cooldown_until:
            return False
        return True

    def set_reduce_only(self, active: bool):
        self._reduce_only_mode = active
        logger.info(f"Reduce-only mode: {active}")

    def set_cooldown(self, seconds: float):
        self._cooldown_until = time.monotonic() + seconds
        logger.info(f"Cooldown set for {seconds}s")

    def _is_module_enabled(self, name: str) -> bool:
        if name in self._disabled_modules:
            return False
        try:
            mod_cfg = getattr(self.config.strategy, name, None)
            return mod_cfg.get('enabled', True) if mod_cfg else True
        except Exception:
            return True

    # -------------------------------------------------------------------------
    # 热插拔与配置重载
    # -------------------------------------------------------------------------
    def add_module(self, name: str, module: FeatureComputer):
        self.signal_modules[name] = module
        logger.info(f"Module {name} added")

    def remove_module(self, name: str):
        self.signal_modules.pop(name, None)
        self._disabled_modules.discard(name)
        logger.info(f"Module {name} removed")

    def register_config_change_callback(self, callback: Callable):
        self._config_change_callback = callback

    async def on_config_changed(self):
        self._refresh_config()
        self._module_errors.clear()
        self._disabled_modules.clear()
        logger.info("DecisionMaker config reloaded")
        if self._config_change_callback:
            await self._config_change_callback()

    def health_status(self) -> dict:
        return {
            "reduce_only": self._reduce_only_mode,
            "cooldown_remaining": max(0, self._cooldown_until - time.monotonic()),
            "active_modules": [m for m in self.signal_modules if self._is_module_enabled(m)],
            "disabled_modules": list(self._disabled_modules),
            "module_errors": dict(self._module_errors),
          }
