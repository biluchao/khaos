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
    - logging, time, typing
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
    - 2026-07-27 v37.1 \~ v37.3: 逐步消除副作用、统一比较、对称放行、持仓缓存、action兼容。
    - 2026-07-27 v37.4: 修复 None ratio 比较崩溃、非法 ratio 首次拦截、全局同优先级保留、持仓容错强化。
__version__ = "37.4.0"
__all__ = ["PriorityExecutor"]
"""

import logging
import time
from typing import List, Optional, Set, Dict, Any, FrozenSet, Tuple

from core.interfaces import SignalPriority, OrderAction
from core.models import Signal, Portfolio

logger = logging.getLogger(__name__)


class PriorityExecutor:
    """
    优先级执行器，基于信号优先级的硬裁决规则。

    裁决逻辑:
        1. 存在 PANIC_CLOSE 或 HARD_STOP 时，保留所有该级别的信号（多品种+动作去重），丢弃其他。
        2. 存在 ESCAPE_CLOSE 时，保留所有 ESCAPE_* 以及同品种/全局的 CLOSE/REDUCE 信号。
        3. 存在 ESCAPE_REDUCE 时，保留 ESCAPE_REDUCE 及同品种/全局的 CLOSE/REDUCE 信号。
        4. 无上述信号时，按优先级排序返回（稳定排序）。
        5. 所有逃生信号经过持仓验证，无持仓者丢弃；查询异常时保守保留。
    """

    DEFAULT_BLOCKING_PRIORITIES: FrozenSet[SignalPriority] = frozenset({
        SignalPriority.PANIC_CLOSE,
        SignalPriority.HARD_STOP,
    })
    DEFAULT_ESCAPE_PRIORITIES: FrozenSet[SignalPriority] = frozenset({
        SignalPriority.ESCAPE_CLOSE,
        SignalPriority.ESCAPE_REDUCE,
    })

    MAX_SIGNALS = 1000

    def __init__(
        self,
        blocking_priorities: Optional[Set[SignalPriority]] = None,
        escape_priorities: Optional[Set[SignalPriority]] = None,
    ):
        raw_blocking = blocking_priorities if blocking_priorities is not None else self.DEFAULT_BLOCKING_PRIORITIES
        raw_escape = escape_priorities if escape_priorities is not None else self.DEFAULT_ESCAPE_PRIORITIES

        for bp in raw_blocking:
            if not isinstance(bp, SignalPriority):
                raise TypeError(f"blocking_priorities must contain SignalPriority, got {type(bp)}")
            for ep in raw_escape:
                if not isinstance(ep, SignalPriority):
                    raise TypeError(f"escape_priorities must contain SignalPriority, got {type(ep)}")
                if bp.value >= ep.value:
                    raise ValueError(
                        f"Blocking priority {bp} (value={bp.value}) must be strictly less "
                        f"than escape priority {ep} (value={ep.value})"
                    )

        self.blocking_priorities: FrozenSet[SignalPriority] = frozenset(raw_blocking)
        self.escape_priorities: FrozenSet[SignalPriority] = frozenset(raw_escape)

        self._blocking_values: FrozenSet[int] = frozenset(p.value for p in self.blocking_priorities)
        self._escape_values: FrozenSet[int] = frozenset(p.value for p in self.escape_priorities)

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
        start_time = time.monotonic()
        self._stats["total_calls"] += 1
        self._stats["last_call_time"] = start_time

        if not isinstance(signals, list):
            logger.error("Invalid input: signals must be a list, got %s", type(signals).__name__)
            self._finish_stats(start_time)
            return []

        original_count = len(signals)
        if original_count > self.MAX_SIGNALS:
            logger.warning(
                "Signal count %d exceeds limit %d, truncating.",
                original_count, self.MAX_SIGNALS
            )
            signals = signals[: self.MAX_SIGNALS]

        # 规范化：(signal, resolved_priority)，绝不修改原对象
        normalized: List[Tuple[Signal, SignalPriority]] = []
        for s in signals:
            if s is None:
                continue
            action = getattr(s, "action", None)
            if action is None or self._is_no_action(action):
                continue

            priority = getattr(s, "priority", None)
            if priority is None:
                priority = SignalPriority.NORMAL_ENTRY
            elif not isinstance(priority, SignalPriority):
                try:
                    priority = SignalPriority(priority)
                except (ValueError, TypeError):
                    priority = SignalPriority.NORMAL_ENTRY

            normalized.append((s, priority))

        self._stats["total_signals_in"] += len(normalized)

        if not normalized:
            logger.debug("All signals invalid or NO_ACTION, returning empty.")
            self._finish_stats(start_time)
            return []

        # 1. 阻断信号
        blocking = [(s, p) for s, p in normalized if p.value in self._blocking_values]
        if blocking:
            deduped = self._deduplicate_by_symbol_action_priority(blocking, keep_highest=True)
            result = [s for s, _ in deduped]
            blocked_symbols = {self._safe_symbol(s) for s in result}
            suppressed = len(normalized) - len(result)
            logger.warning(
                "BLOCKING SIGNALS: %d signals for symbols %s, suppressing %d other signals.",
                len(result), blocked_symbols, suppressed
            )
            self._stats["total_signals_out"] += len(result)
            self._stats["suppressed_count"] += suppressed
            self._finish_stats(start_time)
            return self._stable_sort(deduped)

        # 2. 逃生信号处理
        has_escape_close = any(p.value == SignalPriority.ESCAPE_CLOSE.value for _, p in normalized)
        has_escape_reduce = any(p.value == SignalPriority.ESCAPE_REDUCE.value for _, p in normalized)

        if has_escape_close:
            escape_symbols = {
                self._safe_symbol(s)
                for s, p in normalized
                if p.value == SignalPriority.ESCAPE_CLOSE.value and self._safe_symbol(s)
            }
            allowed = []
            for s, p in normalized:
                if p.value in self._escape_values:
                    allowed.append((s, p))
                elif self._is_close_or_reduce(getattr(s, "action", None)):
                    sym = self._safe_symbol(s)
                    if not sym or sym in escape_symbols:
                        allowed.append((s, p))
            deduped = self._deduplicate_escape_signals(allowed)
            if portfolio is not None:
                deduped = self._filter_unnecessary_escapes(deduped, portfolio)
            result = [s for s, _ in deduped]
            logger.info(
                "ESCAPE_CLOSE active for %s, keeping %d signals.",
                escape_symbols or {"GLOBAL"}, len(result)
            )
            suppressed = len(normalized) - len(result)
            self._stats["total_signals_out"] += len(result)
            self._stats["suppressed_count"] += suppressed
            self._finish_stats(start_time)
            return self._stable_sort(deduped)

        if has_escape_reduce:
            escape_symbols = {
                self._safe_symbol(s)
                for s, p in normalized
                if p.value == SignalPriority.ESCAPE_REDUCE.value and self._safe_symbol(s)
            }
            allowed = []
            for s, p in normalized:
                if p.value in self._escape_values:
                    allowed.append((s, p))
                elif self._is_close_or_reduce(getattr(s, "action", None)):
                    sym = self._safe_symbol(s)
                    if not sym or sym in escape_symbols:
                        allowed.append((s, p))
            deduped = self._deduplicate_escape_signals(allowed)
            if portfolio is not None:
                deduped = self._filter_unnecessary_escapes(deduped, portfolio)
            result = [s for s, _ in deduped]
            logger.info(
                "ESCAPE_REDUCE active for %s, keeping %d signals.",
                escape_symbols or {"GLOBAL"}, len(result)
            )
            suppressed = len(normalized) - len(result)
            self._stats["total_signals_out"] += len(result)
            self._stats["suppressed_count"] += suppressed
            self._finish_stats(start_time)
            return self._stable_sort(deduped)

        # 3. 无阻断/逃生，全部返回
        result_pairs = normalized
        self._stats["total_signals_out"] += len(result_pairs)
        self._finish_stats(start_time)
        return self._stable_sort(result_pairs)

    # =========================================================================
    # 辅助
    # =========================================================================
    @staticmethod
    def _is_no_action(action: Any) -> bool:
        if action is None:
            return True
        if action == OrderAction.NO_ACTION:
            return True
        try:
            return str(action).upper() in ("NO_ACTION", "NONE", "0")
        except Exception:
            return False

    @staticmethod
    def _is_close_or_reduce(action: Any) -> bool:
        if action is None:
            return False
        if action in (OrderAction.CLOSE, OrderAction.REDUCE):
            return True
        try:
            name = str(getattr(action, "name", action)).upper()
            return name in ("CLOSE", "REDUCE")
        except Exception:
            return False

    @staticmethod
    def _is_close(action: Any) -> bool:
        if action is None:
            return False
        if action == OrderAction.CLOSE:
            return True
        try:
            return str(getattr(action, "name", action)).upper() == "CLOSE"
        except Exception:
            return False

    @staticmethod
    def _is_reduce(action: Any) -> bool:
        if action is None:
            return False
        if action == OrderAction.REDUCE:
            return True
        try:
            return str(getattr(action, "name", action)).upper() == "REDUCE"
        except Exception:
            return False

    @staticmethod
    def _safe_symbol(s: Signal) -> str:
        sym = getattr(s, "symbol", None)
        if sym is None:
            return ""
        return str(sym).strip()

    def _stable_sort(self, pairs: List[Tuple[Signal, SignalPriority]]) -> List[Signal]:
        def key_fn(item: Tuple[Signal, SignalPriority]) -> Tuple[int, str, str]:
            s, p = item
            return (p.value, self._safe_symbol(s), str(getattr(s, "action", "")))
        return [s for s, _ in sorted(pairs, key=key_fn)]

    def _deduplicate_by_symbol_action_priority(
        self,
        pairs: List[Tuple[Signal, SignalPriority]],
        keep_highest: bool = True,
    ) -> List[Tuple[Signal, SignalPriority]]:
        if not pairs:
            return []
        best: Dict[Tuple[str, str], Tuple[Signal, SignalPriority]] = {}
        for s, p in pairs:
            sym = self._safe_symbol(s) or "__no_symbol__"
            action_str = str(getattr(s, "action", ""))
            key = (sym, action_str)
            if key not in best:
                best[key] = (s, p)
            else:
                _, old_p = best[key]
                if keep_highest:
                    if p.value < old_p.value:
                        best[key] = (s, p)
                else:
                    if p.value > old_p.value:
                        best[key] = (s, p)
        return list(best.values())

    def _deduplicate_escape_signals(
        self,
        pairs: List[Tuple[Signal, SignalPriority]],
    ) -> List[Tuple[Signal, SignalPriority]]:
        close_map: Dict[str, Tuple[Signal, SignalPriority]] = {}
        reduce_map: Dict[str, Tuple[Signal, SignalPriority]] = {}
        global_close: List[Tuple[Signal, SignalPriority]] = []
        global_reduce: List[Tuple[Signal, SignalPriority]] = []
        others: List[Tuple[Signal, SignalPriority]] = []

        for s, p in pairs:
            sym = self._safe_symbol(s)
            action = getattr(s, "action", None)

            if self._is_close(action):
                if not sym:
                    global_close.append((s, p))
                elif sym not in close_map or p.value < close_map[sym][1].value:
                    close_map[sym] = (s, p)
            elif self._is_reduce(action):
                ratio = self._safe_ratio(s)
                if ratio is None:
                    # 非法 ratio，直接丢弃该信号
                    continue
                if not sym:
                    global_reduce.append((s, p))
                elif sym not in reduce_map:
                    reduce_map[sym] = (s, p)
                else:
                    existing_ratio = self._safe_ratio(reduce_map[sym][0])
                    # existing 已保证合法（首次插入时已过滤），但防御性处理
                    if existing_ratio is None:
                        reduce_map[sym] = (s, p)
                    elif ratio > existing_ratio or (
                        ratio == existing_ratio and p.value < reduce_map[sym][1].value
                    ):
                        reduce_map[sym] = (s, p)
            else:
                others.append((s, p))

        # 同品种 CLOSE 优先于 REDUCE
        for sym in list(reduce_map.keys()):
            if sym in close_map:
                del reduce_map[sym]

        # 全局信号：同优先级全部保留（多指令并行），不同优先级只留最高
        def _keep_global(items: List[Tuple[Signal, SignalPriority]]) -> List[Tuple[Signal, SignalPriority]]:
            if not items:
                return []
            min_val = min(x[1].value for x in items)
            return [x for x in items if x[1].value == min_val]

        return (
            list(close_map.values())
            + list(reduce_map.values())
            + _keep_global(global_close)
            + _keep_global(global_reduce)
            + others
        )

    @staticmethod
    def _safe_ratio(s: Signal) -> Optional[float]:
        """返回合法非负 ratio；非法或负值返回 None（调用方丢弃）。"""
        try:
            r = getattr(s, "reduce_ratio", 0.0)
            if r is None:
                return 0.0
            r = float(r)
            if r < 0.0:
                logger.debug("Negative reduce_ratio %s on signal, discarding.", r)
                return None
            return r
        except (TypeError, ValueError) as exc:
            logger.debug("Invalid reduce_ratio on signal: %s", exc)
            return None

    def _filter_unnecessary_escapes(
        self,
        pairs: List[Tuple[Signal, SignalPriority]],
        portfolio: Portfolio,
    ) -> List[Tuple[Signal, SignalPriority]]:
        held_symbols: Optional[Set[str]] = None
        try:
            held_symbols = self._build_held_symbols(portfolio)
        except Exception as exc:
            logger.warning(
                "Failed to build held symbols set (%s), falling back to per-signal check. detail=%s",
                type(exc).__name__, str(exc),
                exc_info=True,
            )

        filtered: List[Tuple[Signal, SignalPriority]] = []
        for s, p in pairs:
            sym = self._safe_symbol(s)
            if not sym:
                filtered.append((s, p))
                continue
            try:
                has_pos = (sym in held_symbols) if held_symbols is not None else self._has_position(portfolio, sym)
                if has_pos:
                    filtered.append((s, p))
                else:
                    logger.debug("Suppressing escape signal for %s: no position.", sym)
            except Exception as exc:
                logger.warning(
                    "Portfolio query failed for %s (%s), keeping escape signal. detail=%s",
                    sym, type(exc).__name__, str(exc),
                    exc_info=True,
                )
                filtered.append((s, p))
        return filtered

    def _build_held_symbols(self, portfolio: Portfolio) -> Set[str]:
        held: Set[str] = set()
        if portfolio is None:
            return held
        positions = getattr(portfolio, "positions", None)
        if positions is not None:
            if isinstance(positions, dict):
                for sym, pos in positions.items():
                    try:
                        if pos is None:
                            continue
                        if isinstance(pos, (int, float)):
                            if abs(pos) > 0:
                                held.add(str(sym))
                        else:
                            qty = abs(getattr(pos, "quantity", 0) or 0)
                            if qty > 0:
                                held.add(str(sym))
                    except Exception:
                        continue
            else:
                for p in positions:
                    try:
                        if p is None:
                            continue
                        sym = getattr(p, "symbol", "")
                        if not sym:
                            continue
                        qty = abs(getattr(p, "quantity", 0) or 0)
                        if qty > 0:
                            held.add(str(sym))
                    except Exception:
                        continue
        if hasattr(portfolio, "get_held_symbols"):
            try:
                extra = portfolio.get_held_symbols()
                if extra:
                    held.update(str(x) for x in extra)
            except Exception:
                pass
        return held

    def _has_position(self, portfolio: Portfolio, symbol: str) -> bool:
        if portfolio is None:
            return False
        try:
            positions = getattr(portfolio, "positions", None)
            if positions is not None:
                if isinstance(positions, dict):
                    pos = positions.get(symbol)
                    if pos is None:
                        return False
                    if isinstance(pos, (int, float)):
                        return abs(pos) > 0
                    qty = abs(getattr(pos, "quantity", 0) or 0)
                    return qty > 0
                for p in positions:
                    if p is None:
                        continue
                    if getattr(p, "symbol", "") == symbol:
                        qty = abs(getattr(p, "quantity", 0) or 0)
                        if qty > 0:
                            return True
            if hasattr(portfolio, "has_position"):
                return bool(portfolio.has_position(symbol))
            if hasattr(portfolio, "get_position"):
                pos = portfolio.get_position(symbol)
                if pos is None:
                    return False
                qty = abs(getattr(pos, "quantity", 0) or 0)
                return qty > 0
        except Exception:
            pass
        return False

    def _finish_stats(self, start_time: float) -> None:
        self._stats["last_call_duration_ms"] = (time.monotonic() - start_time) * 1000.0

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def reset_stats(self) -> None:
        self._stats = {
            "total_calls": 0,
            "total_signals_in": 0,
            "total_signals_out": 0,
            "suppressed_count": 0,
            "last_call_time": 0.0,
            "last_call_duration_ms": 0.0,
        }

    def get_config(self) -> Dict[str, Any]:
        return {
            "blocking_priorities": [p.name for p in self.blocking_priorities],
            "escape_priorities": [p.name for p in self.escape_priorities],
        }

    def reset(self) -> None:
        self.reset_stats()

    def __repr__(self) -> str:
        calls = self._stats.get("total_calls", 0)
        return (
            f"<PriorityExecutor blocking={len(self.blocking_priorities)} "
            f"escape={len(self.escape_priorities)} calls={calls}>"
        )


if __name__ == "__main__":
    try:
        from core.models import Signal
        s1 = Signal(
            symbol="BTCUSDT",
            action=OrderAction.OPEN,
            direction="LONG",
            priority=SignalPriority.NORMAL_ENTRY,
        )
        s2 = Signal(
            symbol="BTCUSDT",
            action=OrderAction.CLOSE,
            priority=SignalPriority.ESCAPE_CLOSE,
        )
        executor = PriorityExecutor()
        result = executor.resolve([s1, s2])
        print(
            "Result:",
            [(getattr(s.action, "value", s.action), getattr(s.priority, "name", s.priority)) for s in result],
        )
        print("Stats:", executor.get_stats())
    except Exception as e:
        print(f"Self-check skipped (missing runtime deps): {type(e).__name__}: {e}")
