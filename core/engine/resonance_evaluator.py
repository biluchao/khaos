# -*- coding: utf-8 -*-
"""
模块名称: resonance_evaluator.py
核心职责: 多周期共振评估器，计算3/5分钟同向/背离强度并给出仓位乘数。
          支持品种隔离、小账户降杠杆、异步安全、状态持久化，完全并发安全。
所属层级: core.engine

设计原则:
    - 所有输入严格校验，非法返回安全值。
    - 内部状态按品种隔离，LRU淘汰防内存泄漏。
    - 所有状态读写均受锁保护，消除竞态。
    - 异步调用具备超时降级与executor生命周期管理。
    - 参数边界验证，金融级数值安全。

外部依赖:
    - math, time, logging, threading, dataclasses, typing, copy, collections, asyncio
    - concurrent.futures.ThreadPoolExecutor
    - core.interfaces (MarketRegime, SRLevel, FeatureContext)

接口契约:
    提供:
        ResonanceEvaluator.evaluate(...) -> ResonanceState
        ResonanceEvaluator.async_evaluate(...) -> ResonanceState (超时降级)
        get_position_multiplier, reset, get/set_internal_state, get_stats, shutdown
        is_healthy, get_config, update_params
    消费:
        - MarketRegime, SRLevel

配置项: 多个可调参数（见构造函数）

作者: KHAOS System Architect
创建日期: 2025-03-10
修改记录:
    - 2026-07-08 v34.0: 终极并发安全与异步保护，80项加固。
    - 2026-07-27 v34.1: 全面运行时缺陷修复与健壮性强化（版本提取、参数校验、
      EMA 状态清零、shutdown 同步拦截、context 容错、数值安全、接口一致性）。
    - 2026-07-28 v34.2: 机构级二次加固 — 配置/余额锁一致性、validate 无资源泄漏、
      损坏状态自愈、__del__ 安全网、快照原子性、生产可观测性。
    - 2026-07-28 v34.1.1: ResonanceState 非法 multiplier 回退后二次钳入 [min_reduce, max_boost]。
"""
from __future__ import annotations

__version__ = "34.2.0"

import asyncio
import logging
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.interfaces import MarketRegime, SRLevel, FeatureContext

logger = logging.getLogger(__name__)

__all__ = ["ResonanceState", "ResonanceEvaluator", "ResonanceException"]


class ResonanceException(Exception):
    """共振评估器异常基类。"""
    pass


@dataclass(frozen=True)
class ResonanceState:
    """共振评估结果（不可变）。"""
    strength: float
    state_3m: str
    state_5m: str
    multiplier: float
    weight: float
    max_boost: float
    min_reduce: float
    price: float
    symbol: str = ""
    timestamp: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        # 修正非法值 — 全部分支数值安全
        try:
            s = float(self.strength)
        except (TypeError, ValueError):
            s = 0.0
        if not math.isfinite(s) or abs(s) > 1.0:
            object.__setattr__(self, "strength", 0.0)
            logger.warning("ResonanceState: strength invalid, reset to 0.0")
        else:
            object.__setattr__(self, "strength", max(0.0, min(1.0, s)))

        valid = {e.value for e in MarketRegime}
        if self.state_3m not in valid:
            object.__setattr__(self, "state_3m", MarketRegime.RANGE.value)
        if self.state_5m not in valid:
            object.__setattr__(self, "state_5m", MarketRegime.RANGE.value)

        # 保证 min_reduce / max_boost 有限且 min <= max
        try:
            mn = float(self.min_reduce)
        except (TypeError, ValueError):
            mn = 0.3
        try:
            mx = float(self.max_boost)
        except (TypeError, ValueError):
            mx = 1.5
        if not math.isfinite(mn) or mn <= 0:
            mn = 0.3
        if not math.isfinite(mx) or mx <= 0:
            mx = 1.5
        if mn > mx:
            mn, mx = mx, mn
        object.__setattr__(self, "min_reduce", mn)
        object.__setattr__(self, "max_boost", mx)

        # 二次钳位乘数（含非法值回退到 1.0 后再夹入 [mn, mx]）
        try:
            m = float(self.multiplier)
        except (TypeError, ValueError):
            m = 1.0
        if not math.isfinite(m) or m <= 0:
            m = 1.0
        clamped = max(mn, min(m, mx))
        if clamped != self.multiplier:
            object.__setattr__(self, "multiplier", clamped)

        # price 安全
        try:
            p = float(self.price)
        except (TypeError, ValueError):
            p = 0.0
        if not math.isfinite(p) or p < 0:
            object.__setattr__(self, "price", 0.0)

        # symbol 截断
        sym = self.symbol if isinstance(self.symbol, str) else ""
        if len(sym) > 50:
            object.__setattr__(self, "symbol", sym[:50])

    def __repr__(self):
        return (
            f"ResonanceState(sym={self.symbol}, {self.state_3m}/{self.state_5m}, "
            f"strength={self.strength:.2f}, mult={self.multiplier:.2f})"
        )


class ResonanceEvaluator:
    """
    多周期共振评估器（完全并发安全，机构级）。
    """

    # 默认配置常量
    DEFAULT_WEIGHT = 0.5
    DEFAULT_MAX_BOOST = 1.5
    DEFAULT_MIN_REDUCE = 0.3
    DEFAULT_SMOOTH_HALFLIFE = 3
    DEFAULT_MAX_CHANGE_RATIO = 0.2
    DEFAULT_SMALL_BALANCE_THRESHOLD = 2000.0
    DEFAULT_SMALL_BALANCE_MAX_BOOST = 1.2
    DEFAULT_BASE_STRENGTH = 0.6
    DEFAULT_MAX_TRACKED_SYMBOLS = 50
    DEFAULT_INITIAL_ENTRY_MAX_BOOST = 1.5
    DEFAULT_LOW_VOL_THRESHOLD = 0.4
    MIN_ALPHA = 0.01
    MIN_PREV_MULTIPLIER = 0.1
    STATE_VERSION = 2
    # 无共振状态集合：进入这些状态时强制清零 EMA，避免残差误导
    _ZERO_STRENGTH_STATES = frozenset({
        MarketRegime.RANGE.value,
        MarketRegime.HIGH_VOL.value,
    })

    def __init__(
        self,
        weight: float = DEFAULT_WEIGHT,
        max_boost: float = DEFAULT_MAX_BOOST,
        min_reduce: float = DEFAULT_MIN_REDUCE,
        smooth_halflife: int = DEFAULT_SMOOTH_HALFLIFE,
        max_position_change_ratio: float = DEFAULT_MAX_CHANGE_RATIO,
        skip_ratio_on_gap: bool = True,
        exempt_for_initial_entry: bool = True,
        small_balance_threshold: float = DEFAULT_SMALL_BALANCE_THRESHOLD,
        small_balance_max_boost: float = DEFAULT_SMALL_BALANCE_MAX_BOOST,
        base_strength: float = DEFAULT_BASE_STRENGTH,
        max_tracked_symbols: int = DEFAULT_MAX_TRACKED_SYMBOLS,
        initial_entry_max_boost: float = DEFAULT_INITIAL_ENTRY_MAX_BOOST,
        low_vol_threshold: float = DEFAULT_LOW_VOL_THRESHOLD,
        allow_resonance_in_high_vol: bool = False,
    ):
        # 参数验证（与 validate_config 保持一致）
        self._validate_and_assign(
            weight=weight,
            max_boost=max_boost,
            min_reduce=min_reduce,
            smooth_halflife=smooth_halflife,
            max_position_change_ratio=max_position_change_ratio,
            skip_ratio_on_gap=skip_ratio_on_gap,
            exempt_for_initial_entry=exempt_for_initial_entry,
            small_balance_threshold=small_balance_threshold,
            small_balance_max_boost=small_balance_max_boost,
            base_strength=base_strength,
            max_tracked_symbols=max_tracked_symbols,
            initial_entry_max_boost=initial_entry_max_boost,
            low_vol_threshold=low_vol_threshold,
            allow_resonance_in_high_vol=allow_resonance_in_high_vol,
        )

        # 线程安全锁 (可重入)
        self._lock = threading.RLock()

        # 品种状态 LRU (所有访问均在锁内)
        self._per_symbol_state: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._DEFAULT_SYMBOL = "__default__"

        # 统计 (锁保护)
        self._eval_count: int = 0
        self._total_time: float = 0.0
        self._error_count: int = 0

        # 动态权重状态 (锁保护；按实例级，非 per-symbol)
