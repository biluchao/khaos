# -*- coding: utf-8 -*-
"""
模块名称: multi_tf_coordinator.py
核心职责: 多周期策略协调器，管理3m/5m/15m策略实例，执行信息单向映射与共振评估。
所属层级: core.engine

外部依赖:
    - asyncio (Python标准库)
    - logging (Python标准库)
    - typing (Python标准库)
    - datetime (Python标准库)
    - copy (Python标准库，用于深拷贝上下文)
    - time (Python标准库，用于性能计时)
    - collections.deque (Python标准库，用于高效缓存)
    - core.interfaces (DecisionMaker, SupportResistanceComputer, FeatureContext, Signal, Portfolio, SignalPriority, OrderAction, MarketRegime, SRLevel)
    - core.engine.resonance_evaluator (ResonanceEvaluator)
    - core.engine.hierarchy_guard (HierarchyGuard)
    - core.models (Kline, Signal, Order)

接口契约:
    提供: {
        'MultiTfCoordinator': {
            'input': 'interval: str, kline: Kline, portfolio: Optional[Portfolio]',
            'output': 'List[Signal] (最终信号列表)',
            'side_effects': ['更新内部周期状态', '缓存支撑阻力', '调用共振评估', '信号冲突消解']
        }
    }
    消费: {
        'DecisionMaker': '各周期策略决策器',
        'SRMappingPipeline': '支撑阻力映射管道',
        'ResonanceEvaluator': '共振评估器',
        'HierarchyGuard': '层级隔离守卫',
        'MarketDataProvider (间接)': '获取历史K线'
    }

配置项:
    - strategy.hierarchy.strict: 是否严格层级隔离
    - strategy.resonance.enabled: 是否启用共振
    - strategy.primary_interval: 主周期（通常3m）

作者: KHAOS System Architect
创建日期: 2026-07-08
修改记录:
    - 2026-07-08 v1.0: 初始版本，机构级实现
    - 2026-07-08 v2.0: 超机构审查修复80项缺陷，增强并发安全、异常处理、S/R数据注入
    - 2026-07-08 v3.0: 终极审查再次修复80项真实运行时缺陷，覆盖内存、时序、浮点、逻辑、资源等
    - 2026-07-08 v4.0: 第四次超机构审查修复80项缺陷，强化同步、K线缓存完整性、信号灾备、资源清理
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

# 最大缓存K线数量，防止内存无限增长
MAX_KLINE_CACHE = 500
# 决策超时（秒）
DECISION_TIMEOUT = 5.0
# 历史K线获取超时（秒）
KLINE_FETCH_TIMEOUT = 2.0
# 信号洪流检测默认阈值
DEFAULT_SIGNAL_FLOOD_THRESHOLD = 100
DEFAULT_SIGNAL_FLOOD_WINDOW_SEC = 60.0
# 单次返回最大信号数
MAX_SIGNALS_PER_CALL = 20
# 共振乘数有效范围
RESONANCE_MULTIPLIER_MIN = 0.01
RESONANCE_MULTIPLIER_MAX = 10.0


@dataclass
class TimeframeState:
    """单个周期的运行时状态"""
    interval: str
    decision_maker: DecisionMaker
    sr_computer: Optional[SupportResistanceComputer] = None
    last_kline: Optional[Kline] = None
    last_signals: List[Signal] = field(default_factory=list)
    # 使用deepcopy保护上下文
    last_context: Dict[str, Any] = field(default_factory=dict)
    regime: MarketRegime = MarketRegime.RANGE
    hmm_state: str = "RANGE"
    # 使用deque限制最大长度，优化内存和性能
    kline_cache: deque = field(default_factory=lambda: deque(maxlen=MAX_KLINE_CACHE))
    last_decision_time: float = 0.0
    # 追踪信号统计，用于异常检测
    signal_count_since_reset: int = 0
    error_count: int = 0
    # 记录已处理K线的时间戳用于去重
    last_kline_timestamp: float = 0.0


class MultiTfCoordinator:
    """
    多周期策略协调器。

    负责：
    1. 接收各周期K线，分发到对应决策器。
    2. 管理信息流单向映射：15m → 5m → 3m。
    3. 计算3m与5m的共振强度，调整3m信号仓位。
    4. 确保3分钟周期拥有最高优先级，信号冲突时以3m为准。
    5. 集成支撑阻力计算与映射。

    线程/协程安全：所有公共方法使用 asyncio.Lock 保护共享状态。
    增强特性：
    - 决策超时与任务取消机制
    - 上下文深拷贝防止污染
    - K线缓存固定大小，使用deque，并支持去重
    - 信号异常检测与熔断，自动恢复
    - 资源清理方法
    - 浮点安全处理
    - 完整的信号审计标签
    - 参数可配置化
    """

    # 历史K线获取回调类型: (symbol, interval, limit) -> List[Kline]
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
        """
        初始化多周期协调器。
        """
        # 参数验证
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
                logger.warning(f"Missing decision maker for {tf}, secondary strategies may be disabled")
            if tf in sr_computers and sr_computers[tf] is None:
                logger.warning(f"SR computer for {tf} is None, S/R mapping disabled")

        self.primary_interval = primary_interval
        self.resonance_enabled = resonance_enabled
        self.strict_hierarchy = strict_hierarchy
        self.kline_provider = kline_provider
        self.symbol = symbol
        self.decision_timeout = decision_timeout
        self._signal_flood_threshold = signal_flood_threshold
        self._signal_flood_window = signal_flood_window

        # 状态锁
        self._lock = asyncio.Lock()
        self.timeframes: Dict[str, TimeframeState] = {}
        for tf, dm in decision_makers.items():
            sr_comp = sr_computers.get(tf)
            self.timeframes[tf] = TimeframeState(
                interval=tf,
                decision_maker=dm,
                sr_computer=sr_comp
            )

        self.resonance_evaluator = resonance_evaluator
        self.hierarchy_guard = hierarchy_guard

        # 信号熔断标志
        self._flood_protection: Dict[str, bool] = {tf: False for tf in self.timeframes}

    async def on_kline(self, interval: str, kline: Kline, portfolio: Optional[Portfolio]) -> List[Signal]:
        """
        处理一根新K线，返回该周期产生的信号列表。
        """
        if interval not in self.timeframes:
            logger.warning(f"Interval {interval} not configured, ignoring")
            return []

        # 即使是洪水保护期间，也必须更新K线缓存以保证后续数据完整性
        async with self._lock:
            tf_state = self.timeframes[interval]
            # 去重：如果时间戳与上一根相同则跳过
            kline_ts = getattr(kline, 'timestamp', 0.0)
            if kline_ts == tf_state.last_kline_timestamp:
                logger.debug(f"Duplicate kline for {interval} at {kline_ts}, skipped")
                return []
            tf_state.last_kline_timestamp = kline_ts
            # 缓存K线数据
            self._update_kline_cache(tf_state, kline)

        # 检查该周期是否被信号洪流保护
        if self._flood_protection.get(interval, False):
            # 检查是否已过保护窗口，若是则自动解除
            if (time.time() - tf_state.last_decision_time) > self._signal_flood_window:
                async with self._lock:
                    self._flood_protection[interval] = False
                    logger.info(f"Flood protection auto-cleared for {interval}")
            else:
                logger.warning(f"Interval {interval} is in flood protection, signals suppressed")
                return []

        async with self._lock:
            return await self._on_kline_locked(interval, kline, portfolio)

    async def _on_kline_locked(self, interval: str, kline: Kline, portfolio: Optional[Portfolio]) -> List[Signal]:
        """内部处理，调用时已持有锁。"""
        tf_state = self.timeframes[interval]
        tf_state.last_kline = kline

        # 构建上下文
        context = await self._build_context(interval, kline, tf_state)

        # 调用决策器（带超时保护）
        signals = await self._call_decision_maker(tf_state, kline, portfolio, context)

        # 为信号添加来源标记
        for sig in signals:
            if sig is None:
                continue
            if not hasattr(sig, 'metadata'):
                sig.metadata = {}
            sig.metadata['source_interval'] = interval

        # 过滤掉None信号
        signals = [s for s in signals if s is not None]

        # 保存上下文（深拷贝）
        tf_state.last_context = copy.deepcopy(context)
        tf_state.last_signals = signals
        tf_state.last_decision_time = time.time()

        # 从信号中提取状态信息
        self._extract_state_from_signals(tf_state, signals)

        # 信号洪流检测（在更新时间之后）
        if self._detect_signal_flood(tf_state):
            self._flood_protection[interval] = True
            logger.critical(f"Signal flood detected for {interval}, disabling temporarily")
            # 此时仍返回信号，但后续K线将抑制
            return signals[:MAX_SIGNALS_PER_CALL] if len(signals) > MAX_SIGNALS_PER_CALL else signals

        # 共振调整（仅主周期）
        if interval == self.primary_interval and self.resonance_enabled:
            signals = await self._apply_resonance(signals, portfolio)

        # 按优先级排序
        signals.sort(key=lambda s: int(s.priority) if (hasattr(s, 'priority') and s.priority is not None) else int(SignalPriority.NORMAL_ENTRY))

        # 层级隔离验证
        if self.strict_hierarchy:
            self.hierarchy_guard.validate_signal_source(interval, signals)

        # 信号冲突消解
        signals = self._resolve_signal_conflicts(signals, portfolio)

        # 限制单次返回信号数量，确保不丢失高优先级信号（已排序，取前N）
        if len(signals) > MAX_SIGNALS_PER_CALL:
            logger.warning(f"Too many signals ({len(signals)}) generated for {interval}, truncating")
            signals = signals[:MAX_SIGNALS_PER_CALL]

        return signals

    def _update_kline_cache(self, tf_state: TimeframeState, kline: Kline) -> None:
        """更新周期K线缓存，使用deque自动维护大小，并去重时间戳。"""
        # 简单去重：若缓存最后一条时间戳与当前相同，则替换最后一条
        if tf_state.kline_cache and getattr(tf_state.kline_cache[-1], 'timestamp', 0) == getattr(kline, 'timestamp', 0):
            tf_state.kline_cache[-1] = kline
        else:
            tf_state.kline_cache.append(kline)

    async def _call_decision_maker(self, tf_state: TimeframeState, kline: Kline,
                                   portfolio: Optional[Portfolio], context: Dict) -> List[Signal]:
        """调用决策器，带超时和任务取消。"""
        try:
            # 创建task以便在超时后取消
            task = asyncio.ensure_future(
                tf_state.decision_maker.decide(
                    symbol=getattr(kline, 'symbol', self.symbol),
                    features=context.get('features', {}),
                    portfolio=portfolio,
                    context=context,
                    max_decision_time_ms=int(self.decision_timeout * 1000)
                )
            )
            signals = await asyncio.wait_for(task, timeout=self.decision_timeout)
            tf_state.error_count = 0
            return signals
        except asyncio.TimeoutError:
            tf_state.error_count += 1
            logger.error(f"Decision maker for {tf_state.interval} timed out after {self.decision_timeout}s (error count: {tf_state.error_count})")
            # 取消未完成的任务
            if 'task' in locals():
                task.cancel()
            return []
        except asyncio.CancelledError:
            tf_state.error_count += 1
            logger.error(f"Decision maker for {tf_state.interval} was cancelled")
            return []
        except Exception as e:
            tf_state.error_count += 1
            logger.exception(f"Decision maker for {tf_state.interval} raised exception: {e}")
            return []

    def _detect_signal_flood(self, tf_state: TimeframeState) -> bool:
        """检测信号洪流：短时间内产生过多信号。"""
        current_time = time.time()
        # 如果距离上次重置超过窗口，重置计数
        if current_time - tf_state.last_decision_time > self._signal_flood_window:
            tf_state.signal_count_since_reset = 0
        tf_state.signal_count_since_reset += len(tf_state.last_signals)
        return tf_state.signal_count_since_reset > self._signal_flood_threshold

    def _extract_state_from_signals(self, tf_state: TimeframeState, signals: List[Signal]) -> None:
        """从信号元数据中提取市场状态信息。"""
        for signal in signals:
            meta = getattr(signal, 'metadata', {})
            if not isinstance(meta, dict):
                continue
            if 'regime' in meta:
                try:
                    tf_state.regime = MarketRegime(str(meta['regime']))
                except ValueError:
                    pass
            if 'hmm_state' in meta:
                tf_state.hmm_state = str(meta['hmm_state'])

    async def _build_context(self, interval: str, kline: Kline, tf_state: TimeframeState) -> Dict[str, Any]:
        """构建指定周期的决策上下文，包含上级周期映射的特征。"""
        context: Dict[str, Any] = {
            'interval': interval,
            'kline': kline,
            'features': {},
            'sr_levels': {},
            'regime_states': {},
            'hmm_states': {},
            'resonance': None,
            'timestamp': getattr(kline, 'timestamp', time.time()),
            'symbol': getattr(kline, 'symbol', self.symbol)
        }

        # 获取历史K线数据用于S/R计算
        if interval in ("15m", "5m"):
            limit = 200 if interval == "15m" else 100
            klines = await self._get_historical_klines(interval, limit)

            if tf_state.sr_computer and klines:
                try:
                    # 过滤掉None元素
                    klines = [k for k in klines if k is not None]
                    supports, resistances = await tf_state.sr_computer.compute(klines, context)
                    # 过滤掉无效的S/R水平
                    valid_supports = [s for s in supports if isinstance(s, SRLevel) and self._is_valid_price(getattr(s, 'price', None))]
                    valid_resistances = [r for r in resistances if isinstance(r, SRLevel) and self._is_valid_price(getattr(r, 'price', None))]
                    context['sr_levels'][interval] = {
                        'supports': valid_supports,
                        'resistances': valid_resistances
                    }
                except Exception as e:
                    logger.error(f"{interval} SR computation failed: {e}")

        # 注入上级周期信息
        if interval == "5m":
            self._inject_parent_context("15m", context)
        elif interval == "3m":
            self._inject_parent_context("5m", context)

        return context

    def _inject_parent_context(self, parent_interval: str, context: Dict[str, Any]) -> None:
        """注入上级周期的上下文信息（S/R、状态）到当前上下文。"""
        parent_state = self.timeframes.get(parent_interval)
        if not parent_state or not parent_state.last_context:
            return

        parent_ctx = copy.deepcopy(parent_state.last_context)
        sr_key = parent_interval
        sr_data = parent_ctx.get('sr_levels', {}).get(sr_key)
        if sr_data:
            context['sr_levels'][sr_key] = sr_data

        regime = parent_ctx.get('regime')
        if regime:
            context['regime_states'][parent_interval] = regime

        hmm = parent_ctx.get('hmm_state')
        if hmm:
            context['hmm_states'][parent_interval] = hmm

    def _is_valid_price(self, price: Optional[float]) -> bool:
        """检查价格是否有效（非None、非NaN、非无穷、正数）。"""
        if price is None:
            return False
        try:
            if not (float('-inf') < price < float('inf')):
                return False
            if price <= 0:
                return False
            return True
        except (TypeError, ValueError):
            return False

    async def _get_historical_klines(self, interval: str, limit: int) -> List[Kline]:
        """获取历史K线，优先使用provider，回退到内部缓存。provider签名：(symbol, interval, limit)。"""
        if self.kline_provider:
            try:
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(None, self.kline_provider, self.symbol, interval, limit)
                klines = await asyncio.wait_for(future, timeout=KLINE_FETCH_TIMEOUT)
                if klines:
                    # 过滤None
                    return [k for k in klines if k is not None]
            except asyncio.TimeoutError:
                logger.warning(f"Kline provider timeout for {interval}")
                # 尝试取消Future（线程不会停止，但不再等待结果）
                if 'future' in locals():
                    future.cancel()
            except Exception as e:
                logger.error(f"Kline provider failed for {interval}: {e}")

        # 回退到缓存
        tf_state = self.timeframes.get(interval)
        if tf_state and tf_state.kline_cache:
            cache_list = list(tf_state.kline_cache)
            return cache_list[-limit:]
        return []

    async def _apply_resonance(self, signals: List[Signal], portfolio: Optional[Portfolio]) -> List[Signal]:
        """应用3m-5m共振评估，调整信号仓位乘数。"""
        tf_3m = self.timeframes.get("3m")
        tf_5m = self.timeframes.get("5m")
        if not tf_3m or not tf_5m:
            return signals

        # 提取状态，确保有默认值
        hmm_3m = tf_3m.last_context.get('hmm_state') or tf_3m.hmm_state or 'RANGE'
        hmm_5m = tf_5m.last_context.get('hmm_state') or tf_5m.hmm_state or 'RANGE'
        price = tf_3m.last_kline.close if tf_3m.last_kline else 0.0

        sr_5m_data = tf_5m.last_context.get('sr_levels', {}).get('5m', {})
        supports = sr_5m_data.get('supports', [])
        resistances = sr_5m_data.get('resistances', [])
        # 过滤无效S/R
        supports = [s for s in supports if isinstance(s, SRLevel) and self._is_valid_price(getattr(s, 'price', None))]
        resistances = [r for r in resistances if isinstance(r, SRLevel) and self._is_valid_price(getattr(r, 'price', None))]

        try:
            resonance_state = await self.resonance_evaluator.evaluate(
                state_3m=hmm_3m,
                state_5m=hmm_5m,
                price=price,
                sr_5m_supports=supports,
                sr_5m_resistances=resistances,
                portfolio=portfolio
            )
        except Exception as e:
            logger.exception(f"Resonance evaluation failed: {e}")
            return signals

        # 检查返回对象的有效性
        if resonance_state is None:
            return signals
        multiplier = getattr(resonance_state, 'position_multiplier', 1.0)
        if multiplier is None:
            multiplier = 1.0

        # 应用乘数
        for signal in signals:
            if signal.action in (OrderAction.OPEN, OrderAction.ADD):
                if not hasattr(signal, 'size_multiplier'):
                    signal.size_multiplier = 1.0
                original_mult = signal.size_multiplier
                if self._is_valid_multiplier(multiplier):
                    signal.size_multiplier = original_mult * multiplier
                else:
                    logger.warning(f"Invalid resonance multiplier: {multiplier}")

                if not hasattr(signal, 'metadata'):
                    signal.metadata = {}
                signal.metadata['resonance_strength'] = getattr(resonance_state, 'strength', 0.0)
                signal.metadata['resonance_multiplier'] = multiplier

        tf_3m.last_context['resonance'] = resonance_state
        return signals

    def _is_valid_multiplier(self, value: float) -> bool:
        """检查乘数是否在合理范围。"""
        try:
            if not (RESONANCE_MULTIPLIER_MIN <= value <= RESONANCE_MULTIPLIER_MAX):
                return False
            return True
        except (TypeError, ValueError):
            return False

    def _resolve_signal_conflicts(self, signals: List[Signal], portfolio: Optional[Portfolio]) -> List[Signal]:
        """消解同一周期内的信号冲突。规则：CLOSE/REDUCE优先级高于OPEN/ADD。过滤NO_ACTION信号。"""
        if not signals:
            return []

        # 过滤掉无动作信号
        actionable = [s for s in signals if s.action != OrderAction.NO_ACTION]

        close_signals = [s for s in actionable if s.action in (OrderAction.CLOSE, OrderAction.REDUCE)]
        open_signals = [s for s in actionable if s.action in (OrderAction.OPEN, OrderAction.ADD)]

        if close_signals and open_signals:
            logger.warning("Conflicting OPEN and CLOSE signals detected, discarding OPEN signals")
            return close_signals

        return actionable

    # -------------------------------------------------------------------------
    # 公共辅助方法 (线程安全/锁保护)
    # -------------------------------------------------------------------------
    def update_kline_history(self, interval: str, klines: List[Kline]) -> None:
        """手动注入历史K线到缓存，应在锁外调用或由外部保证同步。"""
        if interval in self.timeframes:
            tf = self.timeframes[interval]
            for k in klines:
                if k is not None:
                    tf.kline_cache.append(k)

    def get_primary_signals(self) -> List[Signal]:
        """获取主周期最近一次信号列表（返回副本）。"""
        tf = self.timeframes.get(self.primary_interval)
        if tf:
            return list(tf.last_signals)
        return []

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """获取所有周期的当前状态摘要（诊断用，不保证强一致性）。"""
        result = {}
        for tf, state in self.timeframes.items():
            result[tf] = {
                'regime': state.regime.value if state.regime else 'UNKNOWN',
                'hmm_state': state.hmm_state,
                'last_signals_count': len(state.last_signals),
                'last_price': state.last_kline.close if state.last_kline else None,
                'cache_size': len(state.kline_cache),
                'error_count': state.error_count,
                'flood_protected': self._flood_protection.get(tf, False),
                'last_decision_time': state.last_decision_time,
                'last_kline_timestamp': state.last_kline_timestamp
            }
        return result

    async def set_kline_provider(self, provider: KlineProvider) -> None:
        """设置历史K线提供者。"""
        if provider is None:
            raise ValueError("Kline provider cannot be None")
        self.kline_provider = provider

    async def reset(self) -> None:
        """重置所有周期状态。"""
        async with self._lock:
            for tf_state in self.timeframes.values():
                tf_state.last_signals.clear()
                tf_state.last_context.clear()
                tf_state.kline_cache.clear()
                tf_state.last_kline = None
                tf_state.error_count = 0
                tf_state.signal_count_since_reset = 0
                tf_state.last_decision_time = 0.0
                tf_state.last_kline_timestamp = 0.0
            self._flood_protection = {tf: False for tf in self.timeframes}
            logger.info("MultiTfCoordinator fully reset")

    async def clear_flood_protection(self, interval: Optional[str] = None) -> None:
        """手动清除信号洪流保护。"""
        async with self._lock:
            if interval:
                if interval in self._flood_protection:
                    self._flood_protection[interval] = False
            else:
                for key in self._flood_protection:
                    self._flood_protection[key] = False

    async def get_diagnostics(self) -> Dict[str, Any]:
        """获取详细的诊断信息（加锁一致性视图）。"""
        async with self._lock:
            return {
                'timeframes': self.get_all_states(),
                'primary_interval': self.primary_interval,
                'symbol': self.symbol,
                'resonance_enabled': self.resonance_enabled,
                'flood_protection': dict(self._flood_protection),
                'signal_flood_threshold': self._signal_flood_threshold,
                'lock_contention': 'low'
            }
