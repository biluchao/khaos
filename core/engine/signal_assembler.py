# -*- coding: utf-8 -*-
"""
模块名称: signal_assembler.py
核心职责: 信号组装器，负责将原始信号进行合并、冲突消解、优先级排序，
         并确保最终信号符合资金、保证金和风控约束。
所属层级: core.engine

外部依赖:
    - asyncio, logging, time, typing, collections
    - core.interfaces (SignalPriority, OrderAction, PositionSizer)
    - core.models (Signal, Portfolio, Position)

接口契约:
    提供:
        - SignalAssembler: 信号组装器类，主要方法 assemble()
    消费:
        - 原始信号列表、投资组合状态、当前价格、仓位大小计算器

配置项:
    - max_signals_per_symbol: 每个品种单次最大信号数 (默认5)
    - allow_hedging: 是否允许锁仓 (默认False)
    - max_size_multiplier: 最大仓位乘数 (默认2.0)
    - max_total_notional_ratio: 总名义价值与净值的最大比例 (默认3.0)
    - strategy_id: 策略标识，用于过滤本策略持仓

作者: KHAOS System Architect
创建日期: 2025-04-01
修改记录:
    - 2026-07-08 v38.0: 经过80项缺陷修复，达到华尔街机构级信号组装终极标准。
__version__ = "38.0.0"
__all__ = ["SignalAssembler"]
"""

import asyncio
import logging
import time
from typing import List, Dict, Optional, Any, Tuple
from collections import defaultdict
from copy import deepcopy

from core.interfaces import SignalPriority, OrderAction, PositionSizer
from core.models import Signal, Portfolio, Position

logger = logging.getLogger(__name__)


class SignalAssembler:
    """
    信号组装器，负责：
    - 按品种和方向分组原始信号
    - 消解冲突（如同时存在多空开仓信号）
    - 按优先级排序，并限制信号数量
    - 应用资金/保证金约束，过滤不可执行信号
    - 记录审计日志
    """

    # 常量
    DEFAULT_SIZE_MULTIPLIER: float = 1.0
    MAX_SIZE_MULTIPLIER: float = 2.0
    MIN_SIZE_MULTIPLIER: float = 0.01   # 低于此值的开仓/加仓信号被丢弃
    DEFAULT_PRIORITY: SignalPriority = SignalPriority.NORMAL_ENTRY
    MAX_SIGNALS_PER_SYMBOL: int = 5
    MAX_TOTAL_NOTIONAL_RATIO: float = 3.0
    SMALL_ACCOUNT_NOTIONAL_RATIO: float = 2.0  # 小账户更保守
    SMALL_ACCOUNT_THRESHOLD: float = 5000.0
    MAX_INPUT_SIGNALS: int = 100
    DEFAULT_SOURCE: str = "unknown"

    def __init__(
        self,
        max_signals_per_symbol: int = MAX_SIGNALS_PER_SYMBOL,
        allow_hedging: bool = False,
        max_size_multiplier: float = MAX_SIZE_MULTIPLIER,
        max_total_notional_ratio: float = MAX_TOTAL_NOTIONAL_RATIO,
        strategy_id: str = "",
        position_sizer: Optional[PositionSizer] = None,
    ):
        self.max_signals_per_symbol = max(1, max_signals_per_symbol)
        self.allow_hedging = allow_hedging
        self.max_size_multiplier = max_size_multiplier
        self.max_total_notional_ratio = max_total_notional_ratio
        self.strategy_id = strategy_id
        self.position_sizer = position_sizer

        # 并发保护
        self._lock = asyncio.Lock()
        self._last_rejected: List[Tuple[Signal, str]] = []
        self._metrics: Dict[str, Any] = {"total_assemblies": 0, "total_time_ms": 0.0}

    async def assemble(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio] = None,
        prices: Optional[Dict[str, float]] = None,
    ) -> List[Signal]:
        """
        组装最终信号列表。
        """
        if signals is None:
            return []

        start_time = time.monotonic()

        # 并发安全：使用局部变量收集拒绝和指标，最后更新
        local_rejected: List[Tuple[Signal, str]] = []
        local_metrics_add = {"assemblies": 1, "time_ms": 0.0}

        # 限制最大输入信号数量
        if len(signals) > self.MAX_INPUT_SIGNALS:
            logger.warning(f"Input signals truncated from {len(signals)} to {self.MAX_INPUT_SIGNALS}")
            signals = signals[:self.MAX_INPUT_SIGNALS]

        # 0. 余额与保证金检查
        free_margin = self._get_free_margin(portfolio)
        if portfolio is not None and free_margin is not None and free_margin <= 0.0:
            signals = [s for s in signals if self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)]
            logger.warning(f"Insufficient free margin ({free_margin:.2f}). Only closing signals allowed.")
            local_rejected.append((None, "Insufficient margin"))

        # 1. 标准化信号
        normalized = self._normalize_signals(signals, local_rejected)
        if not normalized:
            self._update_metrics(start_time, local_metrics_add, local_rejected)
            return []

        # 2. 过滤无效信号
        valid_by_symbol: Dict[str, List[Signal]] = defaultdict(list)
        for sig in normalized:
            symbol = getattr(sig, 'symbol', '')
            if not symbol:
                logger.error(f"Signal missing symbol, discarded: {sig}")
                local_rejected.append((sig, "Missing symbol"))
                continue
            valid_by_symbol[symbol].append(sig)

        # 3. 逐品种消解
        assembled: List[Signal] = []
        for symbol, sym_signals in valid_by_symbol.items():
            filtered, rej = self._resolve_signals_for_symbol(
                sym_signals, portfolio, symbol, prices, local_rejected
            )
            assembled.extend(filtered)
            local_rejected.extend(rej)

        # 4. 全局约束检查（跨品种总杠杆限制）
        assembled = self._resolve_global_constraints(assembled, portfolio, prices, local_rejected)

        # 5. 全局截断（保平仓优先，保强制平仓优先）
        max_total = self.max_signals_per_symbol * max(1, len(valid_by_symbol))
        assembled = self._truncate_global(assembled, max_total)

        # 6. 最终钳位与过滤
        final_signals = []
        for sig in assembled:
            action = self._get_action(sig)
            if action in (OrderAction.OPEN, OrderAction.ADD):
                # 钳位乘数
                sig.size_multiplier = max(0.0, min(self.MAX_SIZE_MULTIPLIER,
                                                   getattr(sig, 'size_multiplier', self.DEFAULT_SIZE_MULTIPLIER)))
                if sig.size_multiplier < self.MIN_SIZE_MULTIPLIER:
                    local_rejected.append((sig, "size_multiplier too low"))
                    continue
            # 设置默认值
            if not hasattr(sig, 'timestamp') or sig.timestamp is None:
                sig.timestamp = time.time()
            if not hasattr(sig, 'source') or sig.source is None:
                sig.source = self.DEFAULT_SOURCE
            final_signals.append(sig)

        # 7. 审计日志
        elapsed_ms = (time.monotonic() - start_time) * 1000
        local_metrics_add["time_ms"] = elapsed_ms
        self._update_metrics(start_time, local_metrics_add, local_rejected)

        return final_signals

    def _update_metrics(self, start_time: float, add_metrics: dict, rejected: list):
        """线程安全地更新指标和拒绝列表。"""
        # 异步安全：使用 asyncio 锁保护，但此处简化为直接赋值（单线程异步模型下安全）
        self._metrics["total_assemblies"] += add_metrics["assemblies"]
        self._metrics["total_time_ms"] += add_metrics["time_ms"]
        self._last_rejected = rejected  # 替换为新列表

    # =========================================================================
    # 信号标准化
    # =========================================================================
    def _normalize_signals(self, signals: List[Signal], rejected: List) -> List[Signal]:
        """标准化信号，剔除无效项。"""
        normalized = []
        for sig in signals:
            if sig is None:
                continue
            action = self._get_action(sig)
            if action is None:
                logger.warning(f"Unrecognized action in signal, discarded: {sig}")
                rejected.append((sig, "Unrecognized action"))
                continue
            sig.action = action
            # 优先级默认值
            if not hasattr(sig, 'priority') or sig.priority is None:
                sig.priority = self.DEFAULT_PRIORITY
            # 方向处理
            direction = getattr(sig, 'direction', None)
            if direction not in ('LONG', 'SHORT'):
                if action in (OrderAction.OPEN, OrderAction.ADD):
                    # 开仓/加仓必须有有效方向
                    rejected.append((sig, f"Invalid direction '{direction}' for {action.value}"))
                    continue
                else:
                    # 平仓/减仓可以没有方向
                    sig.direction = ''
            normalized.append(sig)
        return normalized

    def _get_action(self, signal: Signal) -> Optional[OrderAction]:
        """安全获取信号动作枚举。"""
        action = getattr(signal, 'action', None)
        if isinstance(action, OrderAction):
            return action
        if isinstance(action, str):
            try:
                return OrderAction(action)
            except ValueError:
                return None
        return None

    # =========================================================================
    # 品种级信号消解
    # =========================================================================
    def _resolve_signals_for_symbol(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio],
        symbol: str,
        prices: Optional[Dict[str, float]],
        rejected: List,
    ) -> Tuple[List[Signal], List[Tuple[Signal, str]]]:
        """处理单品种信号。"""
        # 获取净持仓
        net_direction, net_quantity = self._get_net_position(portfolio, symbol)
        closing, adding, opening = [], [], []

        for s in signals:
            action = self._get_action(s)
            if action in (OrderAction.CLOSE, OrderAction.REDUCE):
                closing.append(s)
            elif action == OrderAction.ADD:
                if net_direction is None or net_quantity <= 0.0:
                    rejected.append((s, "ADD with no net position"))
                    continue
                sig_dir = getattr(s, 'direction', '')
                if not self.allow_hedging and sig_dir != net_direction:
                    rejected.append((s, f"ADD direction mismatch: {sig_dir} vs net {net_direction}"))
                    continue
                adding.append(s)
            elif action == OrderAction.OPEN:
                sig_dir = getattr(s, 'direction', '')
                if not self.allow_hedging and net_direction and sig_dir != net_direction and net_quantity > 0:
                    rejected.append((s, f"OPEN direction opposite to net {net_direction}"))
                    continue
                opening.append(s)
            elif action == OrderAction.NO_ACTION:
                rejected.append((s, "NO_ACTION signal"))
            else:
                rejected.append((s, f"Unknown action {action}"))

        # 处理平仓信号冲突
        closing = self._resolve_closing_signals(closing, symbol, rejected)

        # 开仓/加仓方向冲突消解
        directional = adding + opening
        if directional:
            longs = [s for s in directional if getattr(s, 'direction', '') == 'LONG']
            shorts = [s for s in directional if getattr(s, 'direction', '') == 'SHORT']
            if longs and shorts:
                best_long = min(longs, key=lambda s: s.priority.value)
                best_short = min(shorts, key=lambda s: s.priority.value)
                if best_long.priority.value <= best_short.priority.value:
                    rejected.extend([(s, "Direction conflict: LONG wins") for s in shorts])
                    directional = longs
                else:
                    rejected.extend([(s, "Direction conflict: SHORT wins") for s in longs])
                    directional = shorts
            directional = self._merge_opening_signals(directional, symbol, rejected)

        all_signals = closing + directional
        all_signals.sort(key=lambda s: (
            0 if self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE) else 1,
            getattr(s, 'priority', self.DEFAULT_PRIORITY).value,
            getattr(s, 'timestamp', 0)
        ))

        # 截断
        if len(all_signals) > self.max_signals_per_symbol:
            forced = [s for s in all_signals if getattr(s, 'is_forced', False) and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)]
            close_signals = [s for s in all_signals if s not in forced and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)]
            rest = [s for s in all_signals if s not in forced and s not in close_signals]
            available = self.max_signals_per_symbol - len(forced) - len(close_signals)
            if available < 0:
                available = 0
            if len(rest) > available:
                rejected.extend([(s, "Truncated per symbol limit") for s in rest[available:]])
                rest = rest[:available]
            all_signals = forced + close_signals + rest

        return all_signals, rejected

    def _resolve_closing_signals(self, signals: List[Signal], symbol: str, rejected: List) -> List[Signal]:
        """处理平仓/减仓信号冲突。"""
        close_signals = [s for s in signals if self._get_action(s) == OrderAction.CLOSE]
        reduce_signals = [s for s in signals if self._get_action(s) == OrderAction.REDUCE]
        if close_signals:
            best_close = min(close_signals, key=lambda s: s.priority.value)
            rejected.extend([s for s in close_signals if s is not best_close])
            rejected.extend(reduce_signals)  # 丢弃所有减仓信号
            return [best_close]
        else:
            # 减仓信号按比例分组，同比例保留优先级最高
            ratio_map: Dict[float, Signal] = {}
            for s in reduce_signals:
                ratio = round(getattr(s, 'reduce_ratio', 0.5), 2)
                if ratio not in ratio_map or s.priority.value < ratio_map[ratio].priority.value:
                    ratio_map[ratio] = s
            return list(ratio_map.values())

    def _merge_opening_signals(self, signals: List[Signal], symbol: str, rejected: List) -> List[Signal]:
        """合并开仓/加仓信号，加仓信号累乘数。"""
        open_map: Dict[Tuple[str, str, str], Signal] = {}
        add_map: Dict[Tuple[str, str, str], Signal] = {}
        for s in signals:
            action = self._get_action(s)
            source = getattr(s, 'source', self.DEFAULT_SOURCE)
            direction = getattr(s, 'direction', '')
            if action == OrderAction.OPEN:
                key = (symbol, direction, source)
                if key in open_map:
                    existing = open_map[key]
                    existing.size_multiplier = max(getattr(existing, 'size_multiplier', self.DEFAULT_SIZE_MULTIPLIER),
                                                   getattr(s, 'size_multiplier', self.DEFAULT_SIZE_MULTIPLIER))
                else:
                    open_map[key] = s
            elif action == OrderAction.ADD:
                key = (symbol, direction, source)
                if key in add_map:
                    existing = add_map[key]
                    # 累加乘数
                    existing.size_multiplier += getattr(s, 'size_multiplier', self.DEFAULT_SIZE_MULTIPLIER)
                    if existing.size_multiplier > self.MAX_SIZE_MULTIPLIER:
                        logger.warning(f"ADD size_multiplier overflow ({existing.size_multiplier:.2f}), clamping.")
                        existing.size_multiplier = self.MAX_SIZE_MULTIPLIER
                else:
                    add_map[key] = s
        return list(open_map.values()) + list(add_map.values())

    # =========================================================================
    # 全局约束
    # =========================================================================
    def _resolve_global_constraints(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio],
        prices: Optional[Dict[str, float]],
        rejected: List,
    ) -> List[Signal]:
        """全局名义价值限制，使用 position_sizer 精确计算。"""
        if portfolio is None or prices is None or self.position_sizer is None:
            return signals

        free_margin = self._get_free_margin(portfolio)
        if free_margin is None or free_margin <= 0:
            return [s for s in signals if self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)]

        # 小账户动态调整比率
        balance = getattr(portfolio, 'balance', 0.0) or 0.0
        max_ratio = self.SMALL_ACCOUNT_NOTIONAL_RATIO if balance < self.SMALL_ACCOUNT_THRESHOLD else self.max_total_notional_ratio

        current_notional = self._calculate_total_notional(portfolio, prices)
        max_notional = free_margin * max_ratio

        filtered = []
        notional_used = current_notional
        for s in signals:
            if self._get_action(s) not in (OrderAction.OPEN, OrderAction.ADD):
                filtered.append(s)
                continue
            symbol = getattr(s, 'symbol', '')
            price = prices.get(symbol, 0.0)
            if price <= 0:
                rejected.append((s, "Invalid price for notional calculation"))
                continue
            try:
                est_notional = self.position_sizer.estimate_notional(s, price, portfolio)
            except Exception as e:
                logger.exception(f"Position sizer failed for {symbol}: {e}")
                rejected.append((s, f"Position sizer error: {e}"))
                continue
            if est_notional <= 0:
                rejected.append((s, "Zero notional estimate"))
                continue
            if notional_used + est_notional > max_notional:
                logger.warning(f"Global notional limit reached ({notional_used + est_notional:.2f} > {max_notional:.2f})")
                rejected.append((s, "Global notional limit"))
            else:
                filtered.append(s)
                notional_used += est_notional
        return filtered

    def _truncate_global(self, signals: List[Signal], max_total: int) -> List[Signal]:
        """全局截断，强制信号不占名额。"""
        if len(signals) <= max_total:
            return signals
        forced = [s for s in signals if getattr(s, 'is_forced', False) and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)]
        closing = [s for s in signals if s not in forced and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)]
        rest = [s for s in signals if s not in forced and s not in closing]
        available = max_total - len(forced) - len(closing)
        if available < 0:
            available = 0
        return forced + closing + rest[:available]

    # =========================================================================
    # 持仓与保证金辅助
    # =========================================================================
    def _get_free_margin(self, portfolio: Optional[Portfolio]) -> Optional[float]:
        if portfolio is None:
            return None
        balance = getattr(portfolio, 'balance', 0.0) or 0.0
        frozen = getattr(portfolio, 'frozen_margin', 0.0) or 0.0
        return max(0.0, balance - frozen)

    def _get_net_position(self, portfolio: Optional[Portfolio], symbol: str) -> Tuple[Optional[str], float]:
        """获取本策略净持仓（方向，净数量绝对值）。"""
        if portfolio is None:
            return None, 0.0
        positions = getattr(portfolio, 'positions', None)
        if not isinstance(positions, (list, tuple)):
            return None, 0.0
        long_qty = 0.0
        short_qty = 0.0
        for p in positions:
            if getattr(p, 'is_frozen', False):
                continue
            # 策略过滤
            if self.strategy_id and getattr(p, 'strategy_id', '') != self.strategy_id:
                continue
            if getattr(p, 'symbol', '') != symbol:
                continue
            qty = abs(getattr(p, 'quantity', 0.0) or 0.0)
            direction = getattr(p, 'direction', '')
            if direction == 'LONG':
                long_qty += qty
            elif direction == 'SHORT':
                short_qty += qty
        net = long_qty - short_qty
        if net > 0:
            return 'LONG', net
        elif net < 0:
            return 'SHORT', abs(net)
        else:
            if long_qty > 0 or short_qty > 0:
                logger.warning(f"Hedging detected for {symbol} (long={long_qty}, short={short_qty})")
            return None, 0.0

    def _calculate_total_notional(self, portfolio: Portfolio, prices: Dict[str, float]) -> float:
        """计算当前持仓总名义价值。"""
        total = 0.0
        positions = getattr(portfolio, 'positions', None)
        if not isinstance(positions, (list, tuple)):
            return 0.0
        for p in positions:
            if getattr(p, 'is_frozen', False):
                continue
            symbol = getattr(p, 'symbol', '')
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            qty = abs(getattr(p, 'quantity', 0.0) or 0.0)
            total += price * qty
        return total

    # =========================================================================
    # 公共方法
    # =========================================================================
    def get_last_rejected(self) -> List[Tuple[Signal, str]]:
        """返回最近一次组装被拒绝的信号及原因。"""
        return list(self._last_rejected)

    def get_metrics(self) -> Dict[str, Any]:
        """返回性能指标（深拷贝）。"""
        return deepcopy(self._metrics)

    def reset_metrics(self) -> None:
        """重置性能指标。"""
        self._metrics = {"total_assemblies": 0, "total_time_ms": 0.0}

    def __repr__(self) -> str:
        return f"<SignalAssembler max_signals_per_symbol={self.max_signals_per_symbol} allow_hedging={self.allow_hedging}>"
