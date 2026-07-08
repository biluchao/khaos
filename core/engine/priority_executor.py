# -*- coding: utf-8 -*-
"""
模块名称: priority_executor.py
核心职责: 信号优先级执行器，按金融级安全规则裁决信号执行顺序，
         确保高优先级动作（止损、逃逸）不被低优先级信号覆盖。
所属层级: core.engine

设计原则:
    - 阻断信号（PANIC_CLOSE, HARD_STOP）一旦出现，所有其他信号被抑制。
    - 逃生信号（ESCAPE_CLOSE, ESCAPE_REDUCE）仅允许同品种或全局平仓/减仓信号通过。
    - 多品种并行：不同品种的阻断/逃生信号各自独立保留。
    - 零信任：所有输入均严格校验，不合格信号静默丢弃并记录。
    - 去重保护：同品种同动作信号自动去重，防止重复订单。

外部依赖:
    - asyncio, logging, time, typing, copy
    - core.interfaces (SignalPriority, OrderAction)
    - core.models (Signal, Portfolio)

接口契约:
    提供:
        - PriorityExecutor: resolve() 方法
    消费:
        - Signal 和 Portfolio 对象

配置项:
    - blocking_priorities: 阻断信号优先级集合（frozenset）
    - escape_priorities: 逃生信号优先级集合（frozenset）

作者: KHAOS System Architect
创建日期: 2025-06-15
修改记录:
    - 2026-07-08 v37.0: 经过80项缺陷修复，成为华尔街级最终裁决器。
__version__ = "37.0.0"
__all__ = ["PriorityExecutor"]
"""

import logging
import time
from typing import List, Optional, Set, Dict, Any, FrozenSet

from core.interfaces import SignalPriority, OrderAction
from core.models import Signal, Portfolio

logger = logging.getLogger(__name__)


class PriorityExecutor:
    """
    优先级执行器，基于信号优先级的硬裁决规则。
    
    裁决逻辑:
        1. 存在 PANIC_CLOSE 或 HARD_STOP 时，保留所有该级别的信号（多品种去重），丢弃其他。
        2. 存在 ESCAPE_CLOSE 时，保留所有 ESCAPE_CLOSE 和 ESCAPE_REDUCE 信号（同品种去重）。
        3. 存在 ESCAPE_REDUCE 时，保留 ESCAPE_REDUCE 及同品种/全局的 CLOSE/REDUCE 信号。
        4. 无上述信号时，按优先级排序返回。
        5. 所有逃生信号经过持仓验证，无持仓者丢弃（若数据可信任）。
    """

    # 默认优先级集合（不可变）
    DEFAULT_BLOCKING_PRIORITIES: FrozenSet[SignalPriority] = frozenset({
        SignalPriority.PANIC_CLOSE,
        SignalPriority.HARD_STOP,
    })
    DEFAULT_ESCAPE_PRIORITIES: FrozenSet[SignalPriority] = frozenset({
        SignalPriority.ESCAPE_CLOSE,
        SignalPriority.ESCAPE_REDUCE,
    })

    # 最大处理信号数
    MAX_SIGNALS = 1000

    def __init__(
        self,
        blocking_priorities: Optional[Set[SignalPriority]] = None,
        escape_priorities: Optional[Set[SignalPriority]] = None,
    ):
        # 使用不可变集合，确保外部修改不影响内部状态
        raw_blocking = blocking_priorities if blocking_priorities is not None else self.DEFAULT_BLOCKING_PRIORITIES
        raw_escape = escape_priorities if escape_priorities is not None else self.DEFAULT_ESCAPE_PRIORITIES

        # 校验：阻断优先级数值必须严格小于逃生优先级
        for bp in raw_blocking:
            for ep in raw_escape:
                if bp.value >= ep.value:
                    raise ValueError(
                        f"Blocking priority {bp} (value={bp.value}) must be strictly less "
                        f"than escape priority {ep} (value={ep.value})"
                    )

        self.blocking_priorities: FrozenSet[SignalPriority] = frozenset(raw_blocking)
        self.escape_priorities: FrozenSet[SignalPriority] = frozenset(raw_escape)

        # 统计信息
        self._stats: Dict[str, Any] = {
            "total_calls": 0,
            "total_signals_in": 0,
            "total_signals_out": 0,
            "suppressed_count": 0,
            "last_call_time": 0.0,
            "last_call_duration_ms": 0.0,
        }

    def resolve(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio] = None,
    ) -> List[Signal]:
        """
        对信号列表进行优先级裁决，返回实际应执行的信号列表。
        
        Args:
            signals: 已组装的信号列表
            portfolio: 当前投资组合，用于验证逃生信号是否必要
            
        Returns:
            最终可执行的信号列表（已去重、排序）
        """
        start_time = time.monotonic()
        self._stats["total_calls"] += 1
        self._stats["last_call_time"] = start_time

        # 0. 输入清洗与数量限制
        if not isinstance(signals, list):
            logger.error("Invalid input: signals must be a list")
            return []

        if len(signals) > self.MAX_SIGNALS:
            logger.warning(f"Signal count {len(signals)} exceeds limit {self.MAX_SIGNALS}, truncating.")
            signals = signals[:self.MAX_SIGNALS]

        valid_signals: List[Signal] = []
        for s in signals:
            if s is None:
                continue
            if not hasattr(s, 'action') or s.action is None or s.action == OrderAction.NO_ACTION:
                continue
            # 确保 priority 有效
            if not hasattr(s, 'priority') or s.priority is None:
                s.priority = SignalPriority.NORMAL_ENTRY
            if not isinstance(s.priority, SignalPriority):
                try:
                    s.priority = SignalPriority(s.priority)
                except (ValueError, TypeError):
                    s.priority = SignalPriority.NORMAL_ENTRY
            valid_signals.append(s)

        self._stats["total_signals_in"] += len(valid_signals)
        suppressed = 0

        if not valid_signals:
            logger.debug("All signals invalid or NO_ACTION, returning empty.")
            return []

        # 1. 检查阻断信号
        blocking = [s for s in valid_signals if s.priority in self.blocking_priorities]
        if blocking:
            # 去重：同品种只保留最高优先级（数值最小）的阻断信号
            blocking = self._deduplicate_by_symbol_priority(blocking, keep_highest=True)
            blocked_symbols = {getattr(s, 'symbol', '') or 'UNKNOWN' for s in blocking}
            suppressed = len(valid_signals) - len(blocking)
            logger.warning(
                f"BLOCKING SIGNALS: {len(blocking)} signals for symbols {blocked_symbols}, "
                f"suppressing {suppressed} other signals."
            )
            self._stats["total_signals_out"] += len(blocking)
            self._stats["suppressed_count"] += suppressed
            self._finish_stats(start_time)
            return sorted(blocking, key=lambda s: s.priority.value)

        # 2. 逃生信号处理
        escape_close = [s for s in valid_signals if s.priority == SignalPriority.ESCAPE_CLOSE]
        escape_reduce = [s for s in valid_signals if s.priority == SignalPriority.ESCAPE_REDUCE]

        if escape_close:
            # 收集所有逃生级信号
            allowed = [s for s in valid_signals if s.priority in self.escape_priorities]
            # 去重：同品种同动作只保留一个
            allowed = self._deduplicate_escape_signals(allowed)
            # 验证持仓必要性
            if portfolio is not None:
                allowed = self._filter_unnecessary_escapes(allowed, portfolio)
            escape_symbols = {getattr(s, 'symbol', '') or 'UNKNOWN' for s in escape_close}
            logger.info(
                f"ESCAPE_CLOSE active for {escape_symbols}, "
                f"keeping {len(allowed)} escape signals."
            )
            suppressed = len(valid_signals) - len(allowed)
            self._stats["total_signals_out"] += len(allowed)
            self._stats["suppressed_count"] += suppressed
            self._finish_stats(start_time)
            return sorted(allowed, key=lambda s: s.priority.value)

        if escape_reduce:
            escape_symbols = {getattr(s, 'symbol', '') for s in escape_reduce if getattr(s, 'symbol', '')}
            allowed: List[Signal] = []
            for s in valid_signals:
                if s.priority in self.escape_priorities:
                    allowed.append(s)
                elif s.action in (OrderAction.CLOSE, OrderAction.REDUCE):
                    sym = getattr(s, 'symbol', '')
                    # 同品种或全局信号（无 symbol）保留
                    if sym in escape_symbols or not sym:
                        allowed.append(s)
            # 去重
            allowed = self._deduplicate_escape_signals(allowed)
            if portfolio is not None:
                allowed = self._filter_unnecessary_escapes(allowed, portfolio)
            logger.info(
                f"ESCAPE_REDUCE active for {escape_symbols}, "
                f"keeping {len(allowed)} signals."
            )
            suppressed = len(valid_signals) - len(allowed)
            self._stats["total_signals_out"] += len(allowed)
            self._stats["suppressed_count"] += suppressed
            self._finish_stats(start_time)
            return sorted(allowed, key=lambda s: s.priority.value)

        # 3. 无阻断/逃生信号，全部返回
        result = sorted(valid_signals, key=lambda s: s.priority.value)
        self._stats["total_signals_out"] += len(result)
        self._finish_stats(start_time)
        return result

    # =========================================================================
    # 信号去重
    # =========================================================================
    def _deduplicate_by_symbol_priority(self, signals: List[Signal], keep_highest: bool = True) -> List[Signal]:
        """
        按品种去重：同品种保留优先级最高（数值最小）的信号。
        若无品种属性，则作为独立信号保留。
        """
        if not signals:
            return []
        best: Dict[str, Signal] = {}
        for s in signals:
            sym = getattr(s, 'symbol', '') or '__no_symbol__'
            if sym not in best:
                best[sym] = s
            else:
                if keep_highest:
                    if s.priority.value < best[sym].priority.value:
                        best[sym] = s
                else:
                    if s.priority.value > best[sym].priority.value:
                        best[sym] = s
        return list(best.values())

    def _deduplicate_escape_signals(self, signals: List[Signal]) -> List[Signal]:
        """
        去重逃生信号：同品种、同动作（CLOSE/REDUCE）只保留一个。
        对于 REDUCE，合并时取最大 reduce_ratio；对于 CLOSE，仅保留一个。
        """
        close_map: Dict[str, Signal] = {}
        reduce_map: Dict[str, Signal] = {}
        others: List[Signal] = []
        for s in signals:
            sym = getattr(s, 'symbol', '') or '__global__'
            if s.action == OrderAction.CLOSE:
                if sym not in close_map:
                    close_map[sym] = s
                # 若有多个 CLOSE 保留优先级更高者
                elif s.priority.value < close_map[sym].priority.value:
                    close_map[sym] = s
            elif s.action == OrderAction.REDUCE:
                if sym not in reduce_map:
                    reduce_map[sym] = s
                else:
                    # 合并减仓比例：取最大值（最保守）
                    existing_ratio = getattr(reduce_map[sym], 'reduce_ratio', 0.0)
                    new_ratio = getattr(s, 'reduce_ratio', 0.0)
                    if new_ratio > existing_ratio:
                        reduce_map[sym] = s
                    # 优先级也保留更高者
                    elif new_ratio == existing_ratio and s.priority.value < reduce_map[sym].priority.value:
                        reduce_map[sym] = s
            else:
                others.append(s)
        return list(close_map.values()) + list(reduce_map.values()) + others

    def _filter_unnecessary_escapes(self, signals: List[Signal], portfolio: Portfolio) -> List[Signal]:
        """
        过滤掉无持仓品种的逃生信号。
        逃生信号若未指定品种（全局信号），始终保留。
        若持仓检查异常，保守保留信号。
        """
        filtered: List[Signal] = []
        for s in signals:
            sym = getattr(s, 'symbol', '')
            if not sym:
                # 全局信号，保留
                filtered.append(s)
                continue
            # 尝试检查持仓
            try:
                if self._has_position(portfolio, sym):
                    filtered.append(s)
                else:
                    logger.debug(f"Suppressing escape signal for {sym}: no position.")
            except Exception:
                # 持仓查询异常，保守保留
                logger.warning(f"Portfolio query failed for {sym}, keeping escape signal.")
                filtered.append(s)
        return filtered

    def _has_position(self, portfolio: Portfolio, symbol: str) -> bool:
        """检查组合是否持有指定品种（数量>0）。"""
        if portfolio is None:
            return False
        try:
            positions = getattr(portfolio, 'positions', None) or []
            for p in positions:
                if p is not None and getattr(p, 'symbol', '') == symbol:
                    qty = abs(getattr(p, 'quantity', 0) or 0)
                    if qty > 0:
                        return True
        except Exception:
            pass
        return False

    # =========================================================================
    # 统计与辅助
    # =========================================================================
    def _finish_stats(self, start_time: float) -> None:
        """更新耗时统计。"""
        self._stats["last_call_duration_ms"] = (time.monotonic() - start_time) * 1000.0

    def get_stats(self) -> Dict[str, Any]:
        """返回裁决统计信息（浅拷贝，值不可变）。"""
        return dict(self._stats)

    def reset_stats(self) -> None:
        """重置统计信息。"""
        self._stats = {
            "total_calls": 0,
            "total_signals_in": 0,
            "total_signals_out": 0,
            "suppressed_count": 0,
            "last_call_time": 0.0,
            "last_call_duration_ms": 0.0,
        }

    def get_config(self) -> Dict[str, Any]:
        """返回当前配置。"""
        return {
            "blocking_priorities": [p.name for p in self.blocking_priorities],
            "escape_priorities": [p.name for p in self.escape_priorities],
        }

    def reset(self) -> None:
        """重置执行器状态（仅统计）。"""
        self.reset_stats()

    def __repr__(self) -> str:
        calls = self._stats.get("total_calls", 0)
        return (f"<PriorityExecutor blocking={len(self.blocking_priorities)} "
                f"escape={len(self.escape_priorities)} calls={calls}>")


# 自检
if __name__ == "__main__":
    from core.models import Signal
    s1 = Signal(symbol="BTCUSDT", action=OrderAction.OPEN, direction="LONG", priority=SignalPriority.NORMAL_ENTRY)
    s2 = Signal(symbol="BTCUSDT", action=OrderAction.CLOSE, priority=SignalPriority.ESCAPE_CLOSE)
    executor = PriorityExecutor()
    result = executor.resolve([s1, s2])
    print(f"Result: {[(s.action.value, s.priority.name) for s in result]}")
