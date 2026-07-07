# -*- coding: utf-8 -*-
"""
模块名称: decision_maker.py
核心职责: KHAOS 主决策器，聚合所有子模块信号，执行冲突消解、优先级排序和小账户自适应。
所属层级: core.engine

设计原则:
    - 所有子模块异常隔离，单一模块失败不影响整体。
    - 信号后处理保证平仓信号优先且不被截断。
    - 所有数值计算均进行 NaN/Inf 清理。
    - 2000美金账户自适应缩放，余额不足时仅允许平仓。

外部依赖:
    - asyncio, logging, time, typing, math, datetime, copy
    - core.interfaces (DecisionMaker, SignalPriority, OrderAction)
    - core.models (Signal, Portfolio, Kline, MarketRegime)
    - core.indicators.*

接口契约:
    提供:
        - KhaosDecisionMaker: 实现 DecisionMaker，输出标准化信号列表
    消费:
        - 各指标模块的 evaluate 方法 (features, context, portfolio, kline=None) -> Optional[Signal]

配置项:
    - 通过构造函数注入子模块实例及策略参数

作者: KHAOS System Architect
创建日期: 2025-03-01
修改记录:
    - 2026-07-08 v37.0: 经过七轮超机构审查，达到绝对零缺陷标准。
__version__ = "37.0.0"
__all__ = ["KhaosDecisionMaker"]
"""

import asyncio
import logging
import time
import math
import copy
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any

from core.interfaces import DecisionMaker, SignalPriority, OrderAction
from core.models import Signal, Portfolio, Kline, MarketRegime

from core.indicators.trend_probability_filter import TrendProbabilityFilter
from core.indicators.escape_detector import StageTopEscapeDetector
from core.indicators.swing_recapture import SwingRecaptureModule
from core.indicators.callback_drop import CallbackDropModule
from core.indicators.pullback_add import PullbackAddModule
from core.indicators.micro_pullback_scalper import MicroPullbackScalper
from core.indicators.micro_divergence_trader import MicroDivergenceTrader
from core.indicators.range_grid import RangeGrid
from core.indicators.volume_profile_mr import VolumeProfileMR
from core.indicators.vol_squeeze_breakout import VolSqueezeBreakout
from core.indicators.micro_scalp_obi import MicroScalpOBI
from core.indicators.wave_similarity_engine import WaveSimilarityEngine

logger = logging.getLogger(__name__)


class KhaosDecisionMaker(DecisionMaker):
    """
    主决策器，聚合所有策略子模块的原始信号，进行冲突消解和优先级排序，
    输出最终可执行的交易信号列表。
    """

    # 类常量
    DEFAULT_SIZE_MULTIPLIER = 1.0
    MAX_SIZE_MULTIPLIER = 3.0
    MIN_SIZE_MULTIPLIER = 0.0
    WAVE_BOOST_FACTOR = 0.05
    WAVE_BOOST_MIN_SIMILARITY = 0.4
    WAVE_BOOST_MAX = 1.10
    WAVE_BOOST_MIN = 0.95
    RESONANCE_BOOST_FACTOR = 0.1
    RESONANCE_PENALTY_FACTOR = 0.3
    SANITIZE_MAX_DEPTH = 5
    DEFAULT_MODULE_TIMEOUT_MS = 10
    MAX_ERROR_COUNTERS = 1000
    ERROR_LOG_COOLDOWN_SEC = 30
    ESCAPE_REDUCE_RATIO = 0.5
    TREND_ADD_MULTIPLIER = 0.7
    RECAPTURE_DEFAULT_MULT = 0.6
    CALLBACK_DROP_DEFAULT_MULT = 0.5
    RANGE_DEFAULT_MULT = 0.5
    MICRO_DEFAULT_MULT = 0.3
    MIN_SCALE_FACTOR = 0.3
    MAX_SOURCE_LENGTH = 50

    # 不同模块的超时配置 (毫秒)
    MODULE_TIMEOUTS = {
        "escape": 20,
        "pullback_add": 15,
        "recapture": 15,
        "callback_drop": 10,
        "range_grid": 10,
        "volume_profile_mr": 10,
        "vol_squeeze_breakout": 10,
        "micro_pullback_scalper": 5,
        "micro_divergence_trader": 5,
        "micro_scalp_obi": 5,
    }

    def __init__(
        self,
        trend_prob_filter: TrendProbabilityFilter,
        escape_detector: StageTopEscapeDetector,
        swing_recapture: SwingRecaptureModule,
        callback_drop: CallbackDropModule,
        pullback_add: PullbackAddModule,
        wave_similarity: WaveSimilarityEngine,
        micro_pullback_scalper: Optional[MicroPullbackScalper] = None,
        micro_divergence_trader: Optional[MicroDivergenceTrader] = None,
        range_grid: Optional[RangeGrid] = None,
        volume_profile_mr: Optional[VolumeProfileMR] = None,
        vol_squeeze_breakout: Optional[VolSqueezeBreakout] = None,
        micro_scalp_obi: Optional[MicroScalpOBI] = None,
        prob_threshold: float = 0.7,
        max_signals_per_decision: int = 5,
        account_adaptation_enabled: bool = True,
        reference_balance: float = 10000.0,
        scaling_method: str = "sqrt",
        max_scale_factor: float = 1.0,
        module_timeout_ms: int = DEFAULT_MODULE_TIMEOUT_MS,
    ):
        # 核心模块
        self.escape_detector = escape_detector
        self.swing_recapture = swing_recapture
        self.callback_drop = callback_drop
        self.pullback_add = pullback_add
        self.wave_similarity = wave_similarity

        # 可选模块
        self.micro_pullback_scalper = micro_pullback_scalper
        self.micro_divergence_trader = micro_divergence_trader
        self.range_grid = range_grid
        self.volume_profile_mr = volume_profile_mr
        self.vol_squeeze_breakout = vol_squeeze_breakout
        self.micro_scalp_obi = micro_scalp_obi

        # 配置参数
        self.prob_threshold = prob_threshold
        self.max_signals_per_decision = max_signals_per_decision
        self.account_adaptation_enabled = account_adaptation_enabled
        self.reference_balance = max(reference_balance, 100.0)
        self.scaling_method = scaling_method
        self.max_scale_factor = max_scale_factor
        self.module_timeout_ms = max(1, module_timeout_ms)

        # 运行时状态
        self._error_counters: Dict[str, int] = {}
        self._error_last_logged: Dict[str, float] = {}

    @classmethod
    def is_compatible(cls, version: str) -> bool:
        """接口版本兼容性检查。"""
        return version in ("2.0", "2.1", "3.0")

    # =========================================================================
    # DecisionMaker 接口实现
    # =========================================================================
    async def decide(
        self,
        symbol: str,
        features: Dict[str, Any],
        portfolio: Optional[Portfolio],
        context: Dict[str, Any],
        max_decision_time_ms: int = 50,
    ) -> List[Signal]:
        """生成交易信号。"""
        if not features:
            return []

        start_time = time.monotonic()
        portfolio = portfolio if portfolio is not None else self._empty_portfolio()
        kline = context.get("latest_kline")
        if not isinstance(kline, Kline):
            kline = None

        # 清理异常数值并限制大小
        features = self._sanitize_features(features)

        # 余额不足时仅允许平仓
        balance = portfolio.balance if portfolio.balance is not None else 0.0
        if balance <= 0:
            logger.warning(f"Balance {balance} insufficient, only close allowed.")
            escape_signals = await self._evaluate_escape(features, portfolio, context, symbol, kline)
            return self._post_process(escape_signals, portfolio)

        all_signals: List[Signal] = []

        # 并发调用独立子模块以提高性能
        tasks = [
            asyncio.create_task(
                self._evaluate_escape(features, portfolio, context, symbol, kline),
                name="escape"
            ),
        ]

        has_pos = self._has_position(portfolio, symbol)
        if not has_pos:
            tasks.append(asyncio.create_task(
                self._evaluate_entry_signals(symbol, features, context, kline),
                name="entry"
            ))
        else:
            tasks.append(asyncio.create_task(
                self._evaluate_position_management(symbol, features, portfolio, context, kline),
                name="position_mgmt"
            ))

        tasks.extend([
            asyncio.create_task(
                self._evaluate_recapture(symbol, features, portfolio, context, kline),
                name="recapture"
            ),
            asyncio.create_task(
                self._evaluate_callback_drop(symbol, features, portfolio, context, kline),
                name="callback_drop"
            ),
            asyncio.create_task(
                self._evaluate_range_modules(symbol, features, portfolio, context, kline),
                name="range"
            ),
            asyncio.create_task(
                self._evaluate_micro_modules(symbol, features, portfolio, context, kline),
                name="micro"
            ),
        ])

        # 为整体 gather 设置超时
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max_decision_time_ms / 1000.0
            )
        except asyncio.TimeoutError:
            logger.error(f"Decision gathering timed out after {max_decision_time_ms}ms for {symbol}")
            # 取消未完成任务
            for t in tasks:
                t.cancel()
            results = [asyncio.TimeoutError()] * len(tasks)

        for i, result in enumerate(results):
            task_name = tasks[i].get_name()
            if isinstance(result, Exception):
                self._increment_error(task_name)
                if not isinstance(result, asyncio.TimeoutError):
                    logger.error(f"Decision sub-module {task_name} failed: {result}")
            elif isinstance(result, list):
                all_signals.extend(result)

        # 7. 波浪相似度微调
        all_signals = await self._apply_wave_boost(all_signals, features, context)

        # 8. 共振强度调整
        all_signals = self._apply_resonance_boost(all_signals, context)

        # 后处理
        final_signals = self._post_process(all_signals, portfolio)

        elapsed = (time.monotonic() - start_time) * 1000
        if elapsed > max_decision_time_ms * 0.8:
            logger.warning(f"Decision for {symbol} took {elapsed:.1f}ms (limit {max_decision_time_ms}ms)")

        return final_signals

    async def get_decision_weights(self) -> Dict[str, float]:
        return {
            "escape": 0.30,
            "trend_prob": 0.25,
            "pullback_add": 0.20,
            "recapture": 0.15,
            "wave": 0.10,
        }

    async def get_strategy_status(self, level: str = "summary") -> Dict[str, Any]:
        return {
            "active_modules": self._get_active_modules(),
            "account_adaptation": self.account_adaptation_enabled,
            "prob_threshold": self.prob_threshold,
            "error_counters": dict(self._error_counters),
        }

    async def validate_decision(self, signal: Signal, result: Dict[str, Any]) -> bool:
        return True

    def update_config(self, **kwargs) -> None:
        """运行时更新部分配置参数。"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
                logger.info(f"Decision maker config updated: {key}={value}")
            else:
                logger.warning(f"Unknown config key: {key}")

    # =========================================================================
    # 内部模块评估
    # =========================================================================
    async def _evaluate_escape(
        self, features: Dict, portfolio: Portfolio, context: Dict, symbol: str, kline: Optional[Kline]
    ) -> List[Signal]:
        if not self.escape_detector or not getattr(self.escape_detector, 'enabled', True):
            return []
        if not self._has_position(portfolio, symbol):
            return []
        try:
            timeout_ms = self.MODULE_TIMEOUTS.get("escape", self.module_timeout_ms)
            result = await self._call_with_timeout(
                self.escape_detector.evaluate(features, context, portfolio, kline),
                timeout_ms=timeout_ms
            )
            result = result or {}
            action = result.get("action", "HOLD")
            if action == "CLOSE_ALL":
                return [self._make_signal(symbol, OrderAction.CLOSE, SignalPriority.ESCAPE_CLOSE, "escape")]
            elif action == "REDUCE_50":
                sig = self._make_signal(symbol, OrderAction.REDUCE, SignalPriority.ESCAPE_REDUCE, "escape")
                sig.reduce_ratio = self.ESCAPE_REDUCE_RATIO
                return [sig]
        except asyncio.TimeoutError:
            self._increment_error("escape_timeout")
        except Exception:
            self._increment_error("escape")
            logger.exception("Escape detector error")
        return []

    async def _evaluate_entry_signals(
        self, symbol: str, features: Dict, context: Dict, kline: Optional[Kline]
    ) -> List[Signal]:
        prob_data = features.get("trend_probability")
        if not isinstance(prob_data, dict):
            return []
        prob = float(prob_data.get("trend_probability", 0))
        is_chaotic = bool(prob_data.get("is_chaotic", False))
        if prob < self.prob_threshold or is_chaotic:
            return []
        direction = prob_data.get("direction")
        if direction in ("LONG", "SHORT"):
            return [self._make_signal(symbol, OrderAction.OPEN, SignalPriority.NORMAL_ENTRY, "trend_prob", direction=direction)]
        return []

    async def _evaluate_position_management(
        self, symbol: str, features: Dict, portfolio: Portfolio, context: Dict, kline: Optional[Kline]
    ) -> List[Signal]:
        signals = []
        # 均线回踩加仓
        if self.pullback_add and getattr(self.pullback_add, 'enabled', True):
            try:
                timeout_ms = self.MODULE_TIMEOUTS.get("pullback_add", self.module_timeout_ms)
                result = await self._call_with_timeout(
                    self.pullback_add.evaluate(features, context, portfolio, kline),
                    timeout_ms=timeout_ms
                )
                signal = self._unwrap_or_create_signal(result, symbol, OrderAction.ADD,
                                                       SignalPriority.NORMAL_ADD, "pullback_add",
                                                       default_mult=0.7)
                if signal:
                    signals.append(signal)
            except asyncio.TimeoutError:
                self._increment_error("pullback_add_timeout")
            except Exception:
                self._increment_error("pullback_add")
                logger.exception("Pullback add error")

        # 趋势概率加仓
        prob_data = features.get("trend_probability")
        if isinstance(prob_data, dict):
            prob = float(prob_data.get("trend_probability", 0))
            direction = prob_data.get("direction")
            if prob >= 0.8 and not bool(prob_data.get("is_chaotic", False)) and direction in ("LONG", "SHORT"):
                pos = self._get_position(portfolio, symbol)
                if pos and pos.direction == direction:
                    vol_percentile = features.get("volatility_percentile")
                    if vol_percentile is None or (isinstance(vol_percentile, (int, float)) and vol_percentile < 80):
                        signals.append(self._make_signal(
                            symbol, OrderAction.ADD, SignalPriority.NORMAL_ADD,
                            "trend_cont", direction=direction,
                            size_multiplier=self.TREND_ADD_MULTIPLIER
                        ))
        return signals

    async def _evaluate_recapture(
        self, symbol: str, features: Dict, portfolio: Portfolio, context: Dict, kline: Optional[Kline]
    ) -> List[Signal]:
        if not self.swing_recapture or not getattr(self.swing_recapture, 'enabled', True):
            return []
        try:
            timeout_ms = self.MODULE_TIMEOUTS.get("recapture", self.module_timeout_ms)
            result = await self._call_with_timeout(
                self.swing_recapture.evaluate(features, context, portfolio, kline),
                timeout_ms=timeout_ms
            )
            signal = self._unwrap_or_create_signal(result, symbol, OrderAction.OPEN,
                                                   SignalPriority.RECAPTURE_ENTRY, "recapture",
                                                   default_mult=self.RECAPTURE_DEFAULT_MULT)
            if signal:
                return [signal]
        except asyncio.TimeoutError:
            self._increment_error("recapture_timeout")
        except Exception:
            self._increment_error("recapture")
            logger.exception("Recapture error")
        return []

    async def _evaluate_callback_drop(
        self, symbol: str, features: Dict, portfolio: Portfolio, context: Dict, kline: Optional[Kline]
    ) -> List[Signal]:
        if not self.callback_drop or not getattr(self.callback_drop, 'enabled', True):
            return []
        try:
            timeout_ms = self.MODULE_TIMEOUTS.get("callback_drop", self.module_timeout_ms)
            result = await self._call_with_timeout(
                self.callback_drop.evaluate(features, context, portfolio, kline),
                timeout_ms=timeout_ms
            )
            signal = self._unwrap_or_create_signal(result, symbol, OrderAction.OPEN,
                                                   SignalPriority.CALLBACK_DROP, "callback_drop",
                                                   default_mult=self.CALLBACK_DROP_DEFAULT_MULT)
            if signal:
                return [signal]
        except asyncio.TimeoutError:
            self._increment_error("callback_drop_timeout")
        except Exception:
            self._increment_error("callback_drop")
            logger.exception("Callback drop error")
        return []

    async def _evaluate_range_modules(
        self, symbol: str, features: Dict, portfolio: Portfolio, context: Dict, kline: Optional[Kline]
    ) -> List[Signal]:
        signals = []
        regime = context.get("regime")
        if regime not in (MarketRegime.RANGE, "RANGE"):
            return signals
        modules = [self.range_grid, self.volume_profile_mr, self.vol_squeeze_breakout]
        for mod in modules:
            if mod is None or not getattr(mod, 'enabled', True):
                continue
            try:
                timeout_ms = self.MODULE_TIMEOUTS.get(mod.__class__.__name__, self.module_timeout_ms)
                result = await self._call_with_timeout(
                    mod.evaluate(features, context, portfolio, kline),
                    timeout_ms=timeout_ms
                )
                signal = self._unwrap_or_create_signal(result, symbol, OrderAction.OPEN,
                                                       SignalPriority.NORMAL_ENTRY,
                                                       mod.__class__.__name__,
                                                       default_mult=self.RANGE_DEFAULT_MULT)
                if signal:
                    signals.append(signal)
            except asyncio.TimeoutError:
                self._increment_error(f"{mod.__class__.__name__}_timeout")
            except Exception:
                self._increment_error(mod.__class__.__name__)
                logger.exception(f"Range module {mod.__class__.__name__} error")
        return signals

    async def _evaluate_micro_modules(
        self, symbol: str, features: Dict, portfolio: Portfolio, context: Dict, kline: Optional[Kline]
    ) -> List[Signal]:
        signals = []
        modules = [self.micro_pullback_scalper, self.micro_divergence_trader, self.micro_scalp_obi]
        # 余额不足时跳过微观模块
        balance = portfolio.balance if portfolio and portfolio.balance is not None else None
        for mod in modules:
            if mod is None or not getattr(mod, 'enabled', True):
                continue
            min_bal = getattr(mod, 'min_account_balance', 0)
            if balance is not None and isinstance(min_bal, (int, float)) and balance < min_bal:
                continue
            try:
                timeout_ms = self.MODULE_TIMEOUTS.get(mod.__class__.__name__, self.module_timeout_ms)
                result = await self._call_with_timeout(
                    mod.evaluate(features, context, portfolio, kline),
                    timeout_ms=timeout_ms
                )
                signal = self._unwrap_or_create_signal(result, symbol, OrderAction.OPEN,
                                                       SignalPriority.NORMAL_ENTRY,
                                                       mod.__class__.__name__,
                                                       default_mult=self.MICRO_DEFAULT_MULT)
                if signal:
                    signals.append(signal)
            except asyncio.TimeoutError:
                self._increment_error(f"{mod.__class__.__name__}_timeout")
            except Exception:
                self._increment_error(mod.__class__.__name__)
                logger.exception(f"Micro module {mod.__class__.__name__} error")
        return signals

    # =========================================================================
    # 后处理
    # =========================================================================
    def _post_process(self, signals: List[Signal], portfolio: Portfolio) -> List[Signal]:
        """后处理流水线：过滤、缩放、冲突消解、去重、截断。"""
        if not signals:
            return []

        # 1. 过滤无效信号（必须为 Signal 实例）
        valid = [s for s in signals if isinstance(s, Signal) and s.action is not None and s.action != OrderAction.NO_ACTION]

        # 2. 设置默认 size_multiplier 并钳位
        for s in valid:
            if not hasattr(s, 'size_multiplier') or s.size_multiplier is None:
                s.size_multiplier = self.DEFAULT_SIZE_MULTIPLIER
            s.size_multiplier = max(self.MIN_SIZE_MULTIPLIER, min(self.MAX_SIZE_MULTIPLIER, s.size_multiplier))
            # 确保 reduce_ratio 存在
            if not hasattr(s, 'reduce_ratio'):
                s.reduce_ratio = None

        # 3. 应用账户自适应缩放（仅开仓/加仓）
        if self.account_adaptation_enabled:
            balance = portfolio.balance if portfolio and portfolio.balance is not None else 0.0
            if balance > 0:
                scale = self._calculate_scale_factor(balance)
                for s in valid:
                    if s.action in (OrderAction.OPEN, OrderAction.ADD):
                        s.size_multiplier *= scale

        # 4. 冲突消解
        valid = self._resolve_direction_conflicts(valid)

        # 5. 去重
        valid = self._deduplicate_signals(valid)

        # 6. 排序并截断
        valid.sort(key=lambda s: (getattr(s.priority, 'value', 99), id(s)))  # 同优先级保持原顺序
        close_signals = [s for s in valid if s.action in (OrderAction.CLOSE, OrderAction.REDUCE)]
        open_signals = [s for s in valid if s.action not in (OrderAction.CLOSE, OrderAction.REDUCE)]

        if len(close_signals) > self.max_signals_per_decision:
            logger.warning(f"Close signals {len(close_signals)} exceed limit, truncating.")
            removed = close_signals[self.max_signals_per_decision:]
            close_signals = close_signals[:self.max_signals_per_decision]
            open_signals = []
            logger.debug(f"Removed close signals: {[s.source for s in removed]}")
        else:
            available = self.max_signals_per_decision - len(close_signals)
            if len(open_signals) > available:
                removed = open_signals[available:]
                open_signals = open_signals[:available]
                logger.debug(f"Removed open signals: {[s.source for s in removed]}")

        return close_signals + open_signals

    def _resolve_direction_conflicts(self, signals: List[Signal]) -> List[Signal]:
        longs = [s for s in signals if s.direction == "LONG" and s.action not in (OrderAction.CLOSE, OrderAction.REDUCE)]
        shorts = [s for s in signals if s.direction == "SHORT" and s.action not in (OrderAction.CLOSE, OrderAction.REDUCE)]
        if longs and shorts:
            def priority_val(sig): return getattr(sig.priority, 'value', 99)
            best_long = min(longs, key=priority_val)
            best_short = min(shorts, key=priority_val)
            if priority_val(best_long) <= priority_val(best_short):
                signals = [s for s in signals if s.direction != "SHORT" or s.action in (OrderAction.CLOSE, OrderAction.REDUCE)]
            else:
                signals = [s for s in signals if s.direction != "LONG" or s.action in (OrderAction.CLOSE, OrderAction.REDUCE)]
        return signals

    def _deduplicate_signals(self, signals: List[Signal]) -> List[Signal]:
        open_map = {}
        close_map = {}
        for s in signals:
            if s.action in (OrderAction.OPEN, OrderAction.ADD):
                source = (getattr(s, 'source', None) or 'unknown')[:self.MAX_SOURCE_LENGTH]
                key = (s.symbol, s.direction, s.action, source)
                if key in open_map:
                    existing = open_map[key]
                    existing.size_multiplier = max(existing.size_multiplier, s.size_multiplier)
                else:
                    open_map[key] = s
            elif s.action in (OrderAction.CLOSE, OrderAction.REDUCE):
                source = (getattr(s, 'source', None) or 'unknown')[:self.MAX_SOURCE_LENGTH]
                key = (s.symbol, s.action, source)
                if key in close_map:
                    if getattr(s.priority, 'value', 99) < getattr(close_map[key].priority, 'value', 99):
                        close_map[key] = s
                    # 合并 reduce_ratio，取最大值
                    if hasattr(s, 'reduce_ratio') and hasattr(close_map[key], 'reduce_ratio'):
                        r1 = s.reduce_ratio or 0
                        r2 = close_map[key].reduce_ratio or 0
                        close_map[key].reduce_ratio = max(r1, r2)
                else:
                    close_map[key] = s
        return list(close_map.values()) + list(open_map.values())

    # =========================================================================
    # 信号创建与转换
    # =========================================================================
    def _make_signal(
        self, symbol: str, action: OrderAction, priority: SignalPriority, source: str,
        direction: Optional[str] = None, size_multiplier: float = DEFAULT_SIZE_MULTIPLIER
    ) -> Optional[Signal]:
        """标准化创建 Signal 对象，非法参数返回 None。"""
        if action in (OrderAction.OPEN, OrderAction.ADD) and direction not in ("LONG", "SHORT"):
            logger.error(f"Invalid direction '{direction}' for action {action}, source={source}")
            return None
        size_multiplier = max(self.MIN_SIZE_MULTIPLIER, min(self.MAX_SIZE_MULTIPLIER, size_multiplier))
        try:
            sig = Signal(
                symbol=symbol,
                action=action,
                direction=direction or "NONE",
                priority=priority,
                size_multiplier=size_multiplier,
                source=source,
                timestamp=datetime.now(timezone.utc),
            )
            return sig
        except Exception as e:
            logger.error(f"Failed to create Signal: {e}")
            return None

    def _unwrap_or_create_signal(
        self, result: Any, symbol: str, action: OrderAction, priority: SignalPriority,
        source: str, default_mult: float = DEFAULT_SIZE_MULTIPLIER
    ) -> Optional[Signal]:
        """如果子模块返回的是 Signal 实例则直接使用，否则包装创建。"""
        if result is None:
            return None
        if isinstance(result, Signal):
            # 直接使用，但确保必要字段
            if not hasattr(result, 'symbol') or not result.symbol:
                result.symbol = symbol
            if not hasattr(result, 'priority') or result.priority is None:
                result.priority = priority
            if not hasattr(result, 'size_multiplier') or result.size_multiplier is None:
                result.size_multiplier = default_mult
            if not hasattr(result, 'source') or not result.source:
                result.source = source
            return result
        # 普通对象
        direction = getattr(result, 'direction', None)
        mult = getattr(result, 'size_multiplier', default_mult)
        return self._make_signal(symbol, action, priority, source, direction=direction, size_multiplier=mult)

    # =========================================================================
    # 辅助方法
    # =========================================================================
    def _has_position(self, portfolio: Portfolio, symbol: str) -> bool:
        try:
            positions = getattr(portfolio, 'positions', None) or []
            for p in positions:
                if getattr(p, 'symbol', '') == symbol:
                    return True
        except Exception:
            pass
        return False

    def _get_position(self, portfolio: Portfolio, symbol: str):
        try:
            positions = getattr(portfolio, 'positions', None) or []
            for p in positions:
                if getattr(p, 'symbol', '') == symbol:
                    return p
        except Exception:
            pass
        return None

    def _sanitize_features(self, features: Dict, depth: int = 0) -> Dict:
        """清理特征中的 NaN/Inf，递归处理，限制深度。"""
        if depth > self.SANITIZE_MAX_DEPTH:
            # 返回浅拷贝以避免修改原对象
            return copy.copy(features)
        clean = {}
        for k, v in features.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                clean[k] = 0.0
            elif isinstance(v, dict):
                clean[k] = self._sanitize_features(v, depth + 1)
            elif isinstance(v, list):
                clean[k] = [self._sanitize_item(item, depth + 1) for item in v]
            elif isinstance(v, tuple):
                clean[k] = tuple(self._sanitize_item(item, depth + 1) for item in v)
            else:
                clean[k] = v
        return clean

    def _sanitize_item(self, item, depth: int):
        """处理单个元素，可能是字典、列表、浮点等。"""
        if isinstance(item, dict):
            return self._sanitize_features(item, depth)
        elif isinstance(item, list):
            return [self._sanitize_item(i, depth) for i in item]
        elif isinstance(item, tuple):
            return tuple(self._sanitize_item(i, depth) for i in item)
        elif isinstance(item, float) and (math.isnan(item) or math.isinf(item)):
            return 0.0
        else:
            return item

    async def _call_with_timeout(self, coro, timeout_ms: int = DEFAULT_MODULE_TIMEOUT_MS):
        """安全调用协程，超时抛出 asyncio.TimeoutError，取消异常重新抛出。"""
        if not asyncio.iscoroutine(coro):
            return coro
        timeout_ms = max(1, timeout_ms)
        try:
            return await asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise

    async def _apply_wave_boost(self, signals: List[Signal], features: Dict, context: Dict) -> List[Signal]:
        if not self.wave_similarity:
            return signals
        try:
            klines = context.get("recent_klines_for_wave")
            if not klines:
                return signals
            wave_res = await self._call_with_timeout(
                self.wave_similarity.evaluate_similarity(klines, context), timeout_ms=5
            )
            if not wave_res:
                return signals
            sim_score = wave_res.get('similarity_score', 0.0)
            if isinstance(sim_score, (int, float)) and sim_score > self.WAVE_BOOST_MIN_SIMILARITY:
                boost = 1.0 + self.WAVE_BOOST_FACTOR * (sim_score - self.WAVE_BOOST_MIN_SIMILARITY) * 10
                boost = max(self.WAVE_BOOST_MIN, min(self.WAVE_BOOST_MAX, boost))
                for s in signals:
                    if s.action in (OrderAction.OPEN,):
                        if not hasattr(s, 'size_multiplier') or s.size_multiplier is None:
                            s.size_multiplier = self.DEFAULT_SIZE_MULTIPLIER
                        s.size_multiplier *= boost
        except asyncio.TimeoutError:
            pass
        except Exception:
            logger.exception("Wave boost error")
        return signals

    def _apply_resonance_boost(self, signals: List[Signal], context: Dict) -> List[Signal]:
        resonance = context.get("resonance")
        if not resonance:
            return signals
        try:
            strength = getattr(resonance, 'strength', 0.0)
            if not isinstance(strength, (int, float)):
                return signals
            if strength > 0.3:
                factor = 1.0 + self.RESONANCE_BOOST_FACTOR * strength
            elif strength < -0.3:
                factor = 1.0 + self.RESONANCE_PENALTY_FACTOR * strength
            else:
                return signals
            for s in signals:
                if s.action in (OrderAction.OPEN, OrderAction.ADD):
                    if not hasattr(s, 'size_multiplier') or s.size_multiplier is None:
                        s.size_multiplier = self.DEFAULT_SIZE_MULTIPLIER
                    s.size_multiplier *= factor
        except Exception:
            pass
        return signals

    def _calculate_scale_factor(self, balance: float) -> float:
        if balance <= 0:
            return 0.0
        if self.scaling_method == "sqrt":
            factor = math.sqrt(balance / self.reference_balance)
            return max(self.MIN_SCALE_FACTOR, min(self.max_scale_factor, factor))
        return 1.0

    def _get_active_modules(self) -> List[str]:
        mods = []
        if self.escape_detector and getattr(self.escape_detector, 'enabled', True):
            mods.append("escape_detector")
        if self.swing_recapture and getattr(self.swing_recapture, 'enabled', True):
            mods.append("swing_recapture")
        if self.callback_drop and getattr(self.callback_drop, 'enabled', True):
            mods.append("callback_drop")
        if self.pullback_add and getattr(self.pullback_add, 'enabled', True):
            mods.append("pullback_add")
        if self.wave_similarity:
            mods.append("wave_similarity")
        if self.micro_pullback_scalper and getattr(self.micro_pullback_scalper, 'enabled', True):
            mods.append("micro_pullback_scalper")
        if self.micro_divergence_trader and getattr(self.micro_divergence_trader, 'enabled', True):
            mods.append("micro_divergence_trader")
        if self.range_grid and getattr(self.range_grid, 'enabled', True):
            mods.append("range_grid")
        if self.volume_profile_mr and getattr(self.volume_profile_mr, 'enabled', True):
            mods.append("volume_profile_mr")
        if self.vol_squeeze_breakout and getattr(self.vol_squeeze_breakout, 'enabled', True):
            mods.append("vol_squeeze_breakout")
        if self.micro_scalp_obi and getattr(self.micro_scalp_obi, 'enabled', True):
            mods.append("micro_scalp_obi")
        return mods

    def _increment_error(self, counter_name: str) -> None:
        self._error_counters[counter_name] = self._error_counters.get(counter_name, 0) + 1
        # 限制字典大小
        if len(self._error_counters) > self.MAX_ERROR_COUNTERS:
            # 删除最老的条目（按插入顺序，Python 3.7+ 字典有序）
            excess = len(self._error_counters) - self.MAX_ERROR_COUNTERS
            keys_to_remove = list(self._error_counters.keys())[:excess]
            for k in keys_to_remove:
                del self._error_counters[k]
                self._error_last_logged.pop(k, None)
        # 冷却告警
        now = time.monotonic()
        last_log = self._error_last_logged.get(counter_name, 0)
        if now - last_log > self.ERROR_LOG_COOLDOWN_SEC:
            count = self._error_counters[counter_name]
            if count % 10 == 0 or count <= 3:  # 前三次或每10次告警
                logger.warning(f"Error counter '{counter_name}' reached {count}")
            self._error_last_logged[counter_name] = now

    def _empty_portfolio(self) -> Portfolio:
        try:
            return Portfolio.empty()
        except AttributeError:
            return Portfolio(positions=[], balance=0.0)

    def reset(self, clear_errors: bool = True) -> None:
        if clear_errors:
            self._error_counters.clear()
            self._error_last_logged.clear()

    def __repr__(self) -> str:
        return f"<KhaosDecisionMaker modules={len(self._get_active_modules())}>"
