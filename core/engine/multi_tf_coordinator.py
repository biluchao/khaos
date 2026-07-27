# -*- coding: utf-8 -*-
"""
模块名称: multi_tf_coordinator.py
核心职责: 多周期策略协调器，管理3m/5m/15m策略实例，执行信息单向映射与共振评估。
所属层级: core.engine

外部依赖:
    - asyncio, logging, typing, datetime, copy, time, collections.deque
    - core.interfaces (DecisionMaker, SupportResistanceComputer, Signal, Portfolio, ...)
    - core.engine.resonance_evaluator (ResonanceEvaluator)
    - core.engine.hierarchy_guard (HierarchyGuard)
    - core.models (Kline)

接口契约:
    提供: {
        'MultiTfCoordinator': {
            'input': 'interval: str, kline: Kline, portfolio: Optional[Portfolio]',
            'output': 'List[Signal]',
            'side_effects': ['更新内部周期状态', '缓存支撑阻力', '调用共振评估', '信号冲突消解']
        }
    }

作者: KHAOS System Architect
创建日期: 2026-07-08
修改记录:
    - 2026-07-08 v1.0\~v4.1 多轮审查
    - 2026-07-27 v4.2: 锁后状态版本校验、共振动态次级周期、error_count 硬限、
                      决策签名更稳健、洪流窗口完整重置（累计100+缺陷修复）
__version__ = "4.2.0"
"""

import asyncio
import logging
import copy
import time
from collections import deque
from typing import Dict, List, Optional, Tuple, Any, Callable, Union
from dataclasses import dataclass, field
from datetime import datetime

from core.interfaces import (
    DecisionMaker,
    SupportResistanceComputer,
    Signal,
    Portfolio,
    SignalPriority,
    OrderAction,
    MarketRegime,
    SRLevel,
    OrderConfirmation,
)
from core.engine.resonance_evaluator import ResonanceEvaluator
from core.engine.hierarchy_guard import HierarchyGuard
from core.models import Kline

logger = logging.getLogger(__name__)

MAX_KLINE_CACHE = 500
DECISION_TIMEOUT = 5.0
KLINE_FETCH_TIMEOUT = 2.0
DEFAULT_SIGNAL_FLOOD_THRESHOLD = 100
DEFAULT_SIGNAL_FLOOD_WINDOW_SEC = 60.0
MAX_SIGNALS_PER_CALL = 20
RESONANCE_MULTIPLIER_MIN = 0.01
RESONANCE_MULTIPLIER_MAX = 10.0
MAX_ERROR_COUNT_BEFORE_SKIP = 20


@dataclass
class TimeframeState:
    interval: str
    decision_maker: DecisionMaker
    sr_computer: Optional[SupportResistanceComputer] = None
    last_kline: Optional[Kline] = None
    last_signals: List[Signal] = field(default_factory=list)
    last_context: Dict[str, Any] = field(default_factory=dict)
    regime: MarketRegime = MarketRegime.RANGE
    hmm_state: str = "RANGE"
    kline_cache: deque = field(default_factory=lambda: deque(maxlen=MAX_KLINE_CACHE))
    last_decision_time: float = 0.0
    signal_count_since_reset: int = 0
    flood_window_start: float = 0.0
    error_count: int = 0
    last_kline_timestamp: float = 0.0
    state_version: int = 0


class MultiTfCoordinator:
    """
    多周期策略协调器 (v4.2)。
    线程/协程安全：公共方法使用 asyncio.Lock 保护共享状态，长耗时决策在锁外执行。
    """

    KlineProvider = Callable[[str, str, int], List[Kline]]

    def __init__(self,
                 decision_makers: Dict[str, DecisionMaker],
                 sr_computers: Dict[str, SupportResistanceComputer],
                 resonance_evaluator: ResonanceEvaluator,
                 hierarchy_guard: HierarchyGuard,
                 primary_interval: str = "3m",
                 resonance_enabled: bool = True,
                 strict_hierarchy: bool = True,
                 kline_provider: Optional[KlineProvider] = None,
                 symbol: str = "BTCUSDT",
                 decision_timeout: float = DECISION_TIMEOUT,
                 signal_flood_threshold: int = DEFAULT_SIGNAL_FLOOD_THRESHOLD,
                 signal_flood_window: float = DEFAULT_SIGNAL_FLOOD_WINDOW_SEC):
        if not decision_makers:
            raise ValueError("decision_makers cannot be empty")
        if primary_interval not in decision_makers:
            raise ValueError(f"Primary interval {primary_interval} not found in decision_makers")
        if resonance_evaluator is None:
            raise ValueError("resonance_evaluator cannot be None")
        if hierarchy_guard is None:
            raise ValueError("hierarchy_guard cannot be None")

        for tf in ["5m", "15m"]:
            if tf not in decision_makers:
                logger.warning("Missing decision maker for %s, secondary strategies may be disabled", tf)

        self.primary_interval = primary_interval
        self.resonance_enabled = resonance_enabled
        self.strict_hierarchy = strict_hierarchy
        self.kline_provider = kline_provider
        self.symbol = symbol
        self.decision_timeout = max(0.5, float(decision_timeout))
        self._signal_flood_threshold = max(10, int(signal_flood_threshold))
        self._signal_flood_window = max(5.0, float(signal_flood_window))

        # 动态确定次级周期（用于共振）
        self._secondary_interval = "5m" if "5m" in decision_makers else (
            next((tf for tf in decision_makers if tf != primary_interval), None)
        )

        self._lock = asyncio.Lock()
        self.timeframes: Dict[str, TimeframeState] = {}
        for tf, dm in decision_makers.items():
            sr_comp = sr_computers.get(tf) if sr_computers else None
            self.timeframes[tf] = TimeframeState(
                interval=tf,
                decision_maker=dm,
                sr_computer=sr_comp
            )

        self.resonance_evaluator = resonance_evaluator
        self.hierarchy_guard = hierarchy_guard
        self._flood_protection: Dict[str, bool] = {tf: False for tf in self.timeframes}

    def _get_kline_ts(self, kline: Kline) -> float:
        ts = getattr(kline, "open_time", None)
        if ts is None:
            ts = getattr(kline, "timestamp", 0.0)
        try:
            return float(ts)
        except (TypeError, ValueError):
            return 0.0

    async def on_kline(self, interval: str, kline: Kline, portfolio: Optional[Portfolio]) -> List[Signal]:
        if interval not in self.timeframes:
            logger.warning("Interval %s not configured, ignoring", interval)
            return []
        if kline is None:
            return []

        kline_ts = self._get_kline_ts(kline)

        # 1. 更新缓存 + 洪水检查（短锁）
        async with self._lock:
            tf_state = self.timeframes[interval]
            if kline_ts == tf_state.last_kline_timestamp and kline_ts > 0:
                logger.debug("Duplicate kline for %s at %s, skipped", interval, kline_ts)
                return []
            tf_state.last_kline_timestamp = kline_ts
            self._update_kline_cache(tf_state, kline)

            if self._flood_protection.get(interval, False):
                if (time.time() - tf_state.flood_window_start) > self._signal_flood_window:
                    self._flood_protection[interval] = False
                    tf_state.signal_count_since_reset = 0
                    tf_state.flood_window_start = 0.0
                    logger.info("Flood protection auto-cleared for %s", interval)
                else:
                    logger.warning("Interval %s is in flood protection, signals suppressed", interval)
                    return []

            # 错误计数熔断
            if tf_state.error_count >= MAX_ERROR_COUNT_BEFORE_SKIP:
                if (time.time() - tf_state.last_decision_time) > 60.0:
                    tf_state.error_count = 0
                    logger.info("Error count reset for %s after cooldown", interval)
                else:
                    logger.warning("Interval %s error_count high, skipping decision", interval)
                    return []

            tf_state.last_kline = kline
            dm = tf_state.decision_maker
            sr_comp = tf_state.sr_computer
            version = tf_state.state_version
            # 轻量快照
            last_ctx_snapshot = {
                "sr_levels": copy.copy(tf_state.last_context.get("sr_levels", {})),
                "regime": tf_state.last_context.get("regime"),
                "hmm_state": tf_state.last_context.get("hmm_state"),
            } if tf_state.last_context else {}

        # 2. 构建上下文与决策（锁外）
        context = await self._build_context(interval, kline, last_ctx_snapshot, sr_comp)
        signals = await self._call_decision_maker(dm, interval, kline, portfolio, context)

        # 3. 后处理（短锁 + 版本校验）
        async with self._lock:
            tf_state = self.timeframes[interval]
            # 若期间被其他协程大幅修改（极少见），仍继续但记录
            if tf_state.state_version != version:
                logger.debug("State version changed during decision for %s", interval)

            for sig in signals:
                if sig is None:
                    continue
                if not hasattr(sig, "metadata") or sig.metadata is None:
                    sig.metadata = {}
                sig.metadata["source_interval"] = interval

            signals = [s for s in signals if s is not None]
            tf_state.last_context = context  # 已在锁外构建，直接赋值
            tf_state.last_signals = signals
            tf_state.last_decision_time = time.time()
            tf_state.state_version += 1
            self._extract_state_from_signals(tf_state, signals)

            if self._detect_signal_flood(tf_state):
                self._flood_protection[interval] = True
                tf_state.flood_window_start = time.time()
                logger.critical("Signal flood detected for %s, disabling temporarily", interval)
                return signals[:MAX_SIGNALS_PER_CALL]

            if interval == self.primary_interval and self.resonance_enabled:
                signals = await self._apply_resonance(signals, portfolio)

            signals.sort(
                key=lambda s: int(s.priority) if (hasattr(s, "priority") and s.priority is not None)
                else int(getattr(SignalPriority, "NORMAL_ENTRY", 50))
            )

            if self.strict_hierarchy:
                self._safe_validate_hierarchy(interval, signals)

            signals = self._resolve_signal_conflicts(signals, portfolio)

            if len(signals) > MAX_SIGNALS_PER_CALL:
                logger.warning("Too many signals (%d) for %s, truncating", len(signals), interval)
                signals = signals[:MAX_SIGNALS_PER_CALL]

            tf_state.last_signals = signals
            return signals

    def _safe_validate_hierarchy(self, interval: str, signals: List[Signal]) -> None:
        try:
            if hasattr(self.hierarchy_guard, "validate_signal_source"):
                self.hierarchy_guard.validate_signal_source(interval, signals)
            elif hasattr(self.hierarchy_guard, "validate_injection"):
                self.hierarchy_guard.validate_injection(interval, interval, raise_on_invalid=False)
        except Exception as e:
            logger.warning("Hierarchy validation failed for %s: %s", interval, e)

    def _update_kline_cache(self, tf_state: TimeframeState, kline: Kline) -> None:
        ts = self._get_kline_ts(kline)
        if tf_state.kline_cache and self._get_kline_ts(tf_state.kline_cache[-1]) == ts:
            tf_state.kline_cache[-1] = kline
        else:
            tf_state.kline_cache.append(kline)

    async def _call_decision_maker(self, dm: DecisionMaker, interval: str, kline: Kline,
                                   portfolio: Optional[Portfolio], context: Dict) -> List[Signal]:
        task = None
        try:
            decide_fn = getattr(dm, "decide", None)
            if decide_fn is None or not callable(decide_fn):
                return []

            # 统一包装为 coroutine
            async def _invoke():
                # 优先尝试 (kline, context, portfolio)
                try:
                    result = decide_fn(kline, context, portfolio or {})
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                except TypeError:
                    pass
                # 回退旧签名
                result = decide_fn(
                    symbol=getattr(kline, "symbol", self.symbol),
                    features=context.get("features", {}),
                    portfolio=portfolio,
                    context=context,
                    max_decision_time_ms=int(self.decision_timeout * 1000)
                )
                if asyncio.iscoroutine(result):
                    return await result
                return result

            task = asyncio.ensure_future(_invoke())
            signals = await asyncio.wait_for(task, timeout=self.decision_timeout)
            if signals is None:
                return []
            if not isinstance(signals, list):
                signals = [signals]
            # 成功则清零错误计数
            async with self._lock:
                if interval in self.timeframes:
                    self.timeframes[interval].error_count = 0
            return signals
        except asyncio.TimeoutError:
            logger.error("Decision maker for %s timed out after %.1fs", interval, self.decision_timeout)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            async with self._lock:
                if interval in self.timeframes:
                    self.timeframes[interval].error_count += 1
            return []
        except asyncio.CancelledError:
            logger.error("Decision maker for %s was cancelled", interval)
            raise
        except Exception as e:
            logger.exception("Decision maker for %s raised: %s", interval, e)
            async with self._lock:
                if interval in self.timeframes:
                    self.timeframes[interval].error_count += 1
            return []

    def _detect_signal_flood(self, tf_state: TimeframeState) -> bool:
        now = time.time()
        if tf_state.flood_window_start <= 0 or (now - tf_state.flood_window_start) > self._signal_flood_window:
            tf_state.flood_window_start = now
            tf_state.signal_count_since_reset = 0
        tf_state.signal_count_since_reset += len(tf_state.last_signals)
        return tf_state.signal_count_since_reset > self._signal_flood_threshold

    def _extract_state_from_signals(self, tf_state: TimeframeState, signals: List[Signal]) -> None:
        for signal in signals:
            meta = getattr(signal, "metadata", None) or {}
            if not isinstance(meta, dict):
                continue
            if "regime" in meta:
                try:
                    tf_state.regime = MarketRegime(str(meta["regime"]))
                except (ValueError, TypeError):
                    pass
            if "hmm_state" in meta:
                tf_state.hmm_state = str(meta["hmm_state"])

    async def _build_context(self, interval: str, kline: Kline,
                             parent_snapshot: Dict, sr_comp: Optional[SupportResistanceComputer]) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "interval": interval,
            "kline": kline,
            "features": {},
            "sr_levels": {},
            "regime_states": {},
            "hmm_states": {},
            "resonance": None,
            "timestamp": self._get_kline_ts(kline),
            "symbol": getattr(kline, "symbol", self.symbol),
            "last_price": getattr(kline, "close", 0.0),
            "open_time": getattr(kline, "open_time", 0),
        }

        if interval in ("15m", "5m") and sr_comp is not None:
            limit = 200 if interval == "15m" else 100
            klines = await self._get_historical_klines(interval, limit)
            if klines:
                try:
                    klines = [k for k in klines if k is not None]
                    result = await sr_comp.compute(klines, context)
                    if result is not None:
                        supports, resistances = result if isinstance(result, tuple) else (result, [])
                        valid_supports = [
                            s for s in (supports or [])
                            if isinstance(s, SRLevel) and self._is_valid_price(getattr(s, "price", None))
                        ]
                        valid_resistances = [
                            r for r in (resistances or [])
                            if isinstance(r, SRLevel) and self._is_valid_price(getattr(r, "price", None))
                        ]
                        context["sr_levels"][interval] = {
                            "supports": valid_supports,
                            "resistances": valid_resistances,
                        }
                except Exception as e:
                    logger.error("%s SR computation failed: %s", interval, e)

        if interval == "5m":
            self._inject_parent_context("15m", context, parent_snapshot)
        elif interval == self.primary_interval:
            self._inject_parent_context(self._secondary_interval or "5m", context, parent_snapshot)

        return context

    def _inject_parent_context(self, parent_interval: str, context: Dict[str, Any],
                               parent_snapshot: Dict) -> None:
        if not parent_interval:
            return
        # 优先使用最新状态
        parent_state = self.timeframes.get(parent_interval)
        if parent_state and parent_state.last_context:
            src = parent_state.last_context
        elif parent_snapshot:
            src = parent_snapshot
        else:
            return
        try:
            sr_data = src.get("sr_levels", {}).get(parent_interval)
            if sr_data:
                context["sr_levels"][parent_interval] = sr_data
            regime = src.get("regime")
            if regime:
                context["regime_states"][parent_interval] = regime
            hmm = src.get("hmm_state")
            if hmm:
                context["hmm_states"][parent_interval] = hmm
        except Exception:
            pass

    def _is_valid_price(self, price: Optional[float]) -> bool:
        if price is None:
            return False
        try:
            p = float(price)
            if not (float("-inf") < p < float("inf")) or p <= 0:
                return False
            return True
        except (TypeError, ValueError):
            return False

    async def _get_historical_klines(self, interval: str, limit: int) -> List[Kline]:
        if self.kline_provider:
            try:
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(None, self.kline_provider, self.symbol, interval, limit)
                klines = await asyncio.wait_for(future, timeout=KLINE_FETCH_TIMEOUT)
                if klines:
                    return [k for k in klines if k is not None]
            except asyncio.TimeoutError:
                logger.warning("Kline provider timeout for %s", interval)
            except Exception as e:
                logger.error("Kline provider failed for %s: %s", interval, e)

        tf_state = self.timeframes.get(interval)
        if tf_state and tf_state.kline_cache:
            return list(tf_state.kline_cache)[-limit:]
        return []

    async def _apply_resonance(self, signals: List[Signal], portfolio: Optional[Portfolio]) -> List[Signal]:
        tf_primary = self.timeframes.get(self.primary_interval)
        sec_interval = self._secondary_interval or "5m"
        tf_sec = self.timeframes.get(sec_interval)
        if not tf_primary or not tf_sec:
            return signals

        hmm_p = tf_primary.last_context.get("hmm_state") or tf_primary.hmm_state or "RANGE"
        hmm_s = tf_sec.last_context.get("hmm_state") or tf_sec.hmm_state or "RANGE"
        price = 0.0
        if tf_primary.last_kline is not None:
            try:
                price = float(tf_primary.last_kline.close or 0.0)
            except (TypeError, ValueError):
                price = 0.0

        sr_data = tf_sec.last_context.get("sr_levels", {}).get(sec_interval, {})
        supports = [
            s for s in sr_data.get("supports", [])
            if isinstance(s, SRLevel) and self._is_valid_price(getattr(s, "price", None))
        ]
        resistances = [
            r for r in sr_data.get("resistances", [])
            if isinstance(r, SRLevel) and self._is_valid_price(getattr(r, "price", None))
        ]

        try:
            resonance_state = await self.resonance_evaluator.evaluate(
                state_3m=hmm_p,
                state_5m=hmm_s,
                price=price,
                sr_5m_supports=supports,
                sr_5m_resistances=resistances,
                portfolio=portfolio
            )
        except Exception as e:
            logger.exception("Resonance evaluation failed: %s", e)
            return signals

        if resonance_state is None:
            return signals
        multiplier = getattr(resonance_state, "position_multiplier", 1.0)
        try:
            multiplier = float(multiplier) if multiplier is not None else 1.0
        except (TypeError, ValueError):
            multiplier = 1.0

        for signal in signals:
            action = getattr(signal, "action", None)
            if action in (OrderAction.OPEN, OrderAction.ADD, "OPEN", "ADD"):
                if not hasattr(signal, "size_multiplier"):
                    signal.size_multiplier = 1.0
                original = float(getattr(signal, "size_multiplier", 1.0) or 1.0)
                if self._is_valid_multiplier(multiplier):
                    signal.size_multiplier = original * multiplier
                else:
                    logger.warning("Invalid resonance multiplier: %s", multiplier)
                if not hasattr(signal, "metadata") or signal.metadata is None:
                    signal.metadata = {}
                signal.metadata["resonance_strength"] = getattr(resonance_state, "strength", 0.0)
                signal.metadata["resonance_multiplier"] = multiplier

        tf_primary.last_context["resonance"] = resonance_state
        return signals

    def _is_valid_multiplier(self, value: float) -> bool:
        try:
            v = float(value)
            return RESONANCE_MULTIPLIER_MIN <= v <= RESONANCE_MULTIPLIER_MAX
        except (TypeError, ValueError):
            return False

    def _resolve_signal_conflicts(self, signals: List[Signal], portfolio: Optional[Portfolio]) -> List[Signal]:
        if not signals:
            return []
        actionable = []
        for s in signals:
            action = getattr(s, "action", None)
            if action is None or action == OrderAction.NO_ACTION or str(action) == "NO_ACTION":
                continue
            actionable.append(s)

        close_signals = [
            s for s in actionable
            if getattr(s, "action", None) in (OrderAction.CLOSE, OrderAction.REDUCE, "CLOSE", "REDUCE", "CLOSE_ALL")
        ]
        open_signals = [
            s for s in actionable
            if getattr(s, "action", None) in (OrderAction.OPEN, OrderAction.ADD, "OPEN", "ADD")
        ]

        if close_signals and open_signals:
            logger.warning("Conflicting OPEN and CLOSE signals detected, discarding OPEN signals")
            return close_signals
        return actionable

    def update_kline_history(self, interval: str, klines: List[Kline]) -> None:
        if interval in self.timeframes:
            tf = self.timeframes[interval]
            for k in klines:
                if k is not None:
                    tf.kline_cache.append(k)

    def get_primary_signals(self) -> List[Signal]:
        tf = self.timeframes.get(self.primary_interval)
        if tf:
            return list(tf.last_signals)
        return []

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        result = {}
        for tf, state in self.timeframes.items():
            result[tf] = {
                "regime": state.regime.value if state.regime else "UNKNOWN",
                "hmm_state": state.hmm_state,
                "last_signals_count": len(state.last_signals),
                "last_price": state.last_kline.close if state.last_kline else None,
                "cache_size": len(state.kline_cache),
                "error_count": state.error_count,
                "flood_protected": self._flood_protection.get(tf, False),
                "last_decision_time": state.last_decision_time,
                "last_kline_timestamp": state.last_kline_timestamp,
            }
        return result

    async def set_kline_provider(self, provider: KlineProvider) -> None:
        if provider is None:
            raise ValueError("Kline provider cannot be None")
        self.kline_provider = provider

    async def reset(self) -> None:
        async with self._lock:
            for tf_state in self.timeframes.values():
                tf_state.last_signals.clear()
                tf_state.last_context.clear()
                tf_state.kline_cache.clear()
                tf_state.last_kline = None
                tf_state.error_count = 0
                tf_state.signal_count_since_reset = 0
                tf_state.flood_window_start = 0.0
                tf_state.last_decision_time = 0.0
                tf_state.last_kline_timestamp = 0.0
                tf_state.state_version = 0
            self._flood_protection = {tf: False for tf in self.timeframes}
            logger.info("MultiTfCoordinator fully reset")

    async def clear_flood_protection(self, interval: Optional[str] = None) -> None:
        async with self._lock:
            if interval:
                if interval in self._flood_protection:
                    self._flood_protection[interval] = False
                    if interval in self.timeframes:
                        self.timeframes[interval].signal_count_since_reset = 0
                        self.timeframes[interval].flood_window_start = 0.0
            else:
                for key in self._flood_protection:
                    self._flood_protection[key] = False
                for tf_state in self.timeframes.values():
                    tf_state.signal_count_since_reset = 0
                    tf_state.flood_window_start = 0.0

    async def get_diagnostics(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "timeframes": self.get_all_states(),
                "primary_interval": self.primary_interval,
                "secondary_interval": self._secondary_interval,
                "symbol": self.symbol,
                "resonance_enabled": self.resonance_enabled,
                "flood_protection": dict(self._flood_protection),
                "signal_flood_threshold": self._signal_flood_threshold,
            }
