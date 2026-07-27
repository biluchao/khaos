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
        if interval not in self.time
