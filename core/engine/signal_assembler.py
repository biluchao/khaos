# -*- coding: utf-8 -*-
"""
模块名称: signal_assembler.py
核心职责: 信号组装器，负责将原始信号进行合并、冲突消解、优先级排序，
         并确保最终信号符合资金、保证金和风控约束。
所属层级: core.engine

外部依赖:
    - asyncio, logging, time, math, typing, collections
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
    - 2026-07-29 v38.1: 全面运行时健壮性加固（类型安全、并发、数值边界、拒绝列表一致性、实例参数生效）。
    - 2026-07-29 v38.2: 机构级深度审计修复——锁粒度优化、拒绝列表有界、metrics/rejected 读写锁保护、
                       异常路径指标完整性、价格快照、身份集合截断、万亿级浮点稳健、故障恢复全覆盖。
                       保留全部原有同步 API 并新增 async 安全版本，接口契约零破坏。
    - 2026-07-29 v38.3: 机构级最终加固——NaN/Inf 数值防线、CancelledError 指标完整性、持仓列表快照、
                       OrderAction 枚举安全访问、metrics 时间上界、只读信号写失败明确拒绝。
__version__ = "38.3.0"
__all__ = ["SignalAssembler"]
"""

import asyncio
import logging
import math
import time
from typing import List, Dict, Optional, Any, Tuple, Set
from collections import defaultdict
from copy import deepcopy

from core.interfaces import SignalPriority, OrderAction, PositionSizer
from core.models import Signal, Portfolio, Position

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """将任意值转为有限浮点数；NaN / Inf / 非法类型返回 default。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _is_finite_positive(value: float) -> bool:
    return value > 0.0 and math.isfinite(value)


class SignalAssembler:
    """
    信号组装器，负责：
    - 按品种和方向分组原始信号
    - 消解冲突（如同时存在多空开仓信号）
    - 按优先级排序，并限制信号数量
    - 应用资金/保证金约束，过滤不可执行信号
    - 记录审计日志
    """

    DEFAULT_SIZE_MULTIPLIER: float = 1.0
    MAX_SIZE_MULTIPLIER: float = 2.0
    MIN_SIZE_MULTIPLIER: float = 0.01
    DEFAULT_PRIORITY: SignalPriority = SignalPriority.NORMAL_ENTRY
    MAX_SIGNALS_PER_SYMBOL: int = 5
    MAX_TOTAL_NOTIONAL_RATIO: float = 3.0
    SMALL_ACCOUNT_NOTIONAL_RATIO: float = 2.0
    SMALL_ACCOUNT_THRESHOLD: float = 5000.0
    MAX_INPUT_SIGNALS: int = 100
    DEFAULT_SOURCE: str = "unknown"
    MAX_REJECTED_RECORDS: int = 512
    NOTIONAL_EPS: float = 1e-6
    QTY_EPS: float = 1e-12
    # 单次 assemble 耗时上界（防止异常时钟导致 metrics 溢出）
    MAX_ELAPSED_MS: float = 3_600_000.0  # 1 hour

    def __init__(
        self,
        max_signals_per_symbol: int = MAX_SIGNALS_PER_SYMBOL,
        allow_hedging: bool = False,
        max_size_multiplier: float = MAX_SIZE_MULTIPLIER,
        max_total_notional_ratio: float = MAX_TOTAL_NOTIONAL_RATIO,
        strategy_id: str = "",
        position_sizer: Optional[PositionSizer] = None,
    ):
        self.max_signals_per_symbol = max(1, int(max_signals_per_symbol) if max_signals_per_symbol else 1)
        self.allow_hedging = bool(allow_hedging)
        msm = _safe_float(max_size_multiplier, self.MAX_SIZE_MULTIPLIER)
        self.max_size_multiplier = max(self.MIN_SIZE_MULTIPLIER, min(msm, 100.0))
        mtnr = _safe_float(max_total_notional_ratio, self.MAX_TOTAL_NOTIONAL_RATIO)
        self.max_total_notional_ratio = max(0.1, mtnr)
        self.strategy_id = str(strategy_id) if strategy_id is not None else ""
        self.position_sizer = position_sizer
        self._lock = asyncio.Lock()
        self._last_rejected: List[Tuple[Optional[Signal], str]] = []
        self._metrics: Dict[str, Any] = {"total_assemblies": 0, "total_time_ms": 0.0}

    async def assemble(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio] = None,
        prices: Optional[Dict[str, float]] = None,
    ) -> List[Signal]:
        """
        组装最终信号列表。
        锁策略：仅在入口快照配置与出口更新 metrics/rejected 时持锁，
        重计算（含 position_sizer）在锁外执行，避免高并发下锁放大延迟。
        """
        if signals is None:
            return []

        start_time = time.monotonic()
        local_rejected: List[Tuple[Optional[Signal], str]] = []
        local_metrics_add = {"assemblies": 1, "time_ms": 0.0}

        async with self._lock:
            cfg = {
                "max_signals_per_symbol": self.max_signals_per_symbol,
                "allow_hedging": self.allow_hedging,
                "max_size_multiplier": self.max_size_multiplier,
                "max_total_notional_ratio": self.max_total_notional_ratio,
                "strategy_id": self.strategy_id,
                "position_sizer": self.position_sizer,
            }

        try:
            result = await self._assemble_impl(
                signals, portfolio, prices, start_time, local_rejected, local_metrics_add, cfg
            )
            return result
        except asyncio.CancelledError:
            # 取消仍更新指标，保证可观测性，再传播取消
            local_metrics_add["time_ms"] = min(
                (time.monotonic() - start_time) * 1000.0, self.MAX_ELAPSED_MS
            )
            local_rejected.append((None, "assemble cancelled"))
            async with self._lock:
                self._update_metrics_unlocked(local_metrics_add, local_rejected)
            raise
        except Exception as e:
            logger.exception(f"SignalAssembler.assemble unexpected failure: {e}")
            local_rejected.append((None, f"Internal assemble error: {type(e).__name__}: {e}"))
            local_metrics_add["time_ms"] = min(
                (time.monotonic() - start_time) * 1000.0, self.MAX_ELAPSED_MS
            )
            async with self._lock:
                self._update_metrics_unlocked(local_metrics_add, local_rejected)
            raise

    async def _assemble_impl(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio],
        prices: Optional[Dict[str, float]],
        start_time: float,
        local_rejected: List[Tuple[Optional[Signal], str]],
        local_metrics_add: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> List[Signal]:
        if not isinstance(signals, (list, tuple)):
            logger.warning("Input signals is not a sequence, returning empty list")
            local_metrics_add["time_ms"] = min(
                (time.monotonic() - start_time) * 1000.0, self.MAX_ELAPSED_MS
            )
            async with self._lock:
                self._update_metrics_unlocked(local_metrics_add, local_rejected)
            return []
        if len(signals) > self.MAX_INPUT_SIGNALS:
            logger.warning(
                f"Input signals truncated from {len(signals)} to {self.MAX_INPUT_SIGNALS}"
            )
            signals = list(signals[: self.MAX_INPUT_SIGNALS])
        else:
            signals = list(signals)

        prices_snap: Optional[Dict[str, float]] = None
        if prices is not None:
            try:
                prices_snap = {
                    str(k): _safe_float(v, 0.0) for k, v in dict(prices).items()
                }
            except Exception:
                prices_snap = {}
                local_rejected.append((None, "prices dict unreadable; treated as empty"))

        free_margin = self._get_free_margin(portfolio)
        if portfolio is not None and free_margin is not None and free_margin <= 0.0:
            original_len = len(signals)
            signals = [
                s for s in signals
                if self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)
            ]
            if len(signals) < original_len:
                local_rejected.append(
                    (None, f"Insufficient free margin ({free_margin:.2f}); only CLOSE/REDUCE retained")
                )
            logger.warning(
                f"Insufficient free margin ({free_margin:.2f}). Only closing signals allowed."
            )

        normalized = self._normalize_signals(signals, local_rejected)
        if not normalized:
            local_metrics_add["time_ms"] = min(
                (time.monotonic() - start_time) * 1000.0, self.MAX_ELAPSED_MS
            )
            async with self._lock:
                self._update_metrics_unlocked(local_metrics_add, local_rejected)
            return []

        valid_by_symbol: Dict[str, List[Signal]] = defaultdict(list)
        for sig in normalized:
            symbol = getattr(sig, "symbol", None)
            if not symbol or not isinstance(symbol, str) or not symbol.strip():
                logger.error(f"Signal missing or invalid symbol, discarded: {sig!r}")
                local_rejected.append((sig, "Missing or invalid symbol"))
                continue
            valid_by_symbol[symbol.strip()].append(sig)

        if not valid_by_symbol:
            local_metrics_add["time_ms"] = min(
                (time.monotonic() - start_time) * 1000.0, self.MAX_ELAPSED_MS
            )
            async with self._lock:
                self._update_metrics_unlocked(local_metrics_add, local_rejected)
            return []

        assembled: List[Signal] = []
        for symbol, sym_signals in valid_by_symbol.items():
            filtered, _ = self._resolve_signals_for_symbol(
                sym_signals, portfolio, symbol, prices_snap, local_rejected, cfg
            )
            assembled.extend(filtered)

        assembled = self._resolve_global_constraints(
            assembled, portfolio, prices_snap, local_rejected, cfg
        )

        max_total = cfg["max_signals_per_symbol"] * max(1, len(valid_by_symbol))
        assembled = self._truncate_global(assembled, max_total, local_rejected)

        final_signals: List[Signal] = []
        max_mult = cfg["max_size_multiplier"]
        for sig in assembled:
            action = self._get_action(sig)
            if action is None:
                local_rejected.append((sig, "Unrecognized action after assembly"))
                continue
            if action in (OrderAction.OPEN, OrderAction.ADD):
                current_mult = _safe_float(
                    getattr(sig, "size_multiplier", self.DEFAULT_SIZE_MULTIPLIER),
                    self.DEFAULT_SIZE_MULTIPLIER,
                )
                clamped = max(0.0, min(max_mult, current_mult))
                try:
                    sig.size_multiplier = clamped
                except Exception:
                    local_rejected.append((sig, "size_multiplier write failed (read-only signal)"))
                    continue
                if clamped < self.MIN_SIZE_MULTIPLIER:
                    local_rejected.append((sig, f"size_multiplier too low ({clamped:.6f})"))
                    continue
            if not hasattr(sig, "timestamp") or getattr(sig, "timestamp", None) is None:
                try:
                    sig.timestamp = time.time()
                except Exception:
                    pass
            if not hasattr(sig, "source") or getattr(sig, "source", None) is None:
                try:
                    sig.source = self.DEFAULT_SOURCE
                except Exception:
                    pass
            final_signals.append(sig)

        local_metrics_add["time_ms"] = min(
            (time.monotonic() - start_time) * 1000.0, self.MAX_ELAPSED_MS
        )
        async with self._lock:
            self._update_metrics_unlocked(local_metrics_add, local_rejected)

        return final_signals

    def _update_metrics_unlocked(
        self,
        add_metrics: dict,
        rejected: List[Tuple[Optional[Signal], str]],
    ) -> None:
        """调用方必须已持有 self._lock。"""
        try:
            self._metrics["total_assemblies"] = (
                int(self._metrics.get("total_assemblies", 0)) + int(add_metrics.get("assemblies", 0))
            )
            self._metrics["total_time_ms"] = (
                float(self._metrics.get("total_time_ms", 0.0))
                + min(float(add_metrics.get("time_ms", 0.0)), self.MAX_ELAPSED_MS)
            )
        except (TypeError, ValueError, KeyError) as e:
            logger.exception(f"Metrics update failed: {e}")
        if len(rejected) > self.MAX_REJECTED_RECORDS:
            rejected = rejected[-self.MAX_REJECTED_RECORDS :]
        self._last_rejected = list(rejected)

    def _normalize_signals(
        self, signals: List[Signal], rejected: List[Tuple[Optional[Signal], str]]
    ) -> List[Signal]:
        """标准化信号，剔除无效项。"""
        normalized: List[Signal] = []
        for sig in signals:
            if sig is None:
                rejected.append((None, "Null signal"))
                continue
            action = self._get_action(sig)
            if action is None:
                logger.warning(f"Unrecognized action in signal, discarded: {sig!r}")
                rejected.append((sig, "Unrecognized action"))
                continue
            try:
                sig.action = action
            except Exception:
                pass

            pri = getattr(sig, "priority", None)
            if pri is None or not hasattr(pri, "value"):
                try:
                    sig.priority = self.DEFAULT_PRIORITY
                except Exception:
                    pass
            else:
                try:
                    _ = pri.value
                except Exception:
                    try:
                        sig.priority = self.DEFAULT_PRIORITY
                    except Exception:
                        pass

            direction = getattr(sig, "direction", None)
            if direction not in ("LONG", "SHORT"):
                if action in (OrderAction.OPEN, OrderAction.ADD):
                    rejected.append(
                        (sig, f"Invalid direction '{direction}' for {getattr(action, 'value', action)}")
                    )
                    continue
                else:
                    try:
                        sig.direction = ""
                    except Exception:
                        pass
            normalized.append(sig)
        return normalized

    def _get_action(self, signal: Any) -> Optional[OrderAction]:
        """安全获取信号动作枚举。"""
        if signal is None:
            return None
        action = getattr(signal, "action", None)
        if isinstance(action, OrderAction):
            return action
        if isinstance(action, str):
            try:
                return OrderAction(action)
            except (ValueError, TypeError):
                return None
        return None

    def _is_no_action(self, action: Optional[OrderAction]) -> bool:
        """安全判断是否为 NO_ACTION（枚举可能不存在该成员）。"""
        if action is None:
            return False
        no_act = getattr(OrderAction, "NO_ACTION", None)
        if no_act is not None and action is no_act:
            return True
        # 字符串兜底
        return getattr(action, "value", None) == "NO_ACTION" or str(action) == "NO_ACTION"

    def _priority_value(self, signal: Any) -> int:
        """安全获取优先级数值（越小越优先）。缺失时返回默认值。"""
        pri = getattr(signal, "priority", None)
        if pri is None:
            return getattr(self.DEFAULT_PRIORITY, "value", 0)
        try:
            return int(pri.value)
        except (AttributeError, TypeError, ValueError):
            return getattr(self.DEFAULT_PRIORITY, "value", 0)

    def _resolve_signals_for_symbol(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio],
        symbol: str,
        prices: Optional[Dict[str, float]],
        rejected: List[Tuple[Optional[Signal], str]],
        cfg: Dict[str, Any],
    ) -> Tuple[List[Signal], List[Tuple[Optional[Signal], str]]]:
        """处理单品种信号。返回 (保留信号, 拒绝列表引用)。"""
        net_direction, net_quantity = self._get_net_position(portfolio, symbol, cfg)
        closing: List[Signal] = []
        adding: List[Signal] = []
        opening: List[Signal] = []
        allow_hedging = cfg["allow_hedging"]
        max_per_sym = cfg["max_signals_per_symbol"]

        for s in signals:
            action = self._get_action(s)
            if action is None:
                rejected.append((s, "Unknown action"))
                continue
            if action in (OrderAction.CLOSE, OrderAction.REDUCE):
                closing.append(s)
            elif action == OrderAction.ADD:
                if net_direction is None or net_quantity <= 0.0:
                    rejected.append((s, "ADD with no net position"))
                    continue
                sig_dir = getattr(s, "direction", "") or ""
                if not allow_hedging and sig_dir != net_direction:
                    rejected.append(
                        (s, f"ADD direction mismatch: {sig_dir} vs net {net_direction}")
                    )
                    continue
                adding.append(s)
            elif action == OrderAction.OPEN:
                sig_dir = getattr(s, "direction", "") or ""
                if (
                    not allow_hedging
                    and net_direction
                    and sig_dir != net_direction
                    and net_quantity > 0
                ):
                    rejected.append(
                        (s, f"OPEN direction opposite to net {net_direction}")
                    )
                    continue
                opening.append(s)
            elif self._is_no_action(action):
                rejected.append((s, "NO_ACTION signal"))
            else:
                rejected.append((s, f"Unknown action {action}"))

        closing = self._resolve_closing_signals(closing, symbol, rejected)

        directional = adding + opening
        if directional:
            longs = [s for s in directional if getattr(s, "direction", "") == "LONG"]
            shorts = [s for s in directional if getattr(s, "direction", "") == "SHORT"]
            if longs and shorts:
                best_long = min(longs, key=self._priority_value)
                best_short = min(shorts, key=self._priority_value)
                if self._priority_value(best_long) <= self._priority_value(best_short):
                    for s in shorts:
                        rejected.append((s, "Direction conflict: LONG wins"))
                    directional = longs
                else:
                    for s in longs:
                        rejected.append((s, "Direction conflict: SHORT wins"))
                    directional = shorts
            directional = self._merge_opening_signals(directional, symbol, rejected, cfg)

        all_signals = closing + directional
        all_signals.sort(
            key=lambda s: (
                0 if self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE) else 1,
                self._priority_value(s),
                _safe_float(getattr(s, "timestamp", 0), 0.0),
            )
        )

        if len(all_signals) > max_per_sym:
            forced = [
                s
                for s in all_signals
                if getattr(s, "is_forced", False)
                and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)
            ]
            forced_ids: Set[int] = {id(s) for s in forced}
            close_signals = [
                s
                for s in all_signals
                if id(s) not in forced_ids
                and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)
            ]
            close_ids: Set[int] = {id(s) for s in close_signals}
            rest = [
                s
                for s in all_signals
                if id(s) not in forced_ids and id(s) not in close_ids
            ]
            available = max_per_sym - len(forced) - len(close_signals)
            if available < 0:
                available = 0
                excess_close = len(forced) + len(close_signals) - max_per_sym
                if excess_close > 0 and close_signals:
                    for s in close_signals[len(close_signals) - excess_close :]:
                        rejected.append((s, "Truncated per symbol limit (close)"))
                    close_signals = close_signals[: max(0, len(close_signals) - excess_close)]
            if len(rest) > available:
                for s in rest[available:]:
                    rejected.append((s, "Truncated per symbol limit"))
                rest = rest[:available]
            all_signals = forced + close_signals + rest

        return all_signals, rejected

    def _resolve_closing_signals(
        self,
        signals: List[Signal],
        symbol: str,
        rejected: List[Tuple[Optional[Signal], str]],
    ) -> List[Signal]:
        """处理平仓/减仓信号冲突。始终以 (Signal, str) 形式写入 rejected。"""
        close_signals = [s for s in signals if self._get_action(s) == OrderAction.CLOSE]
        reduce_signals = [s for s in signals if self._get_action(s) == OrderAction.REDUCE]

        if close_signals:
            best_close = min(close_signals, key=self._priority_value)
            for s in close_signals:
                if s is not best_close:
                    rejected.append((s, "Superseded by higher-priority CLOSE"))
            for s in reduce_signals:
                rejected.append((s, "Discarded because CLOSE present"))
            return [best_close]

        ratio_map: Dict[float, Signal] = {}
        for s in reduce_signals:
            raw = getattr(s, "reduce_ratio", 0.5)
            ratio = round(_safe_float(raw, 0.5), 2)
            ratio = max(0.01, min(1.0, ratio))
            if ratio not in ratio_map or self._priority_value(s) < self._priority_value(ratio_map[ratio]):
                if ratio in ratio_map:
                    rejected.append(
                        (ratio_map[ratio], f"Superseded by higher-priority REDUCE ratio={ratio}")
                    )
                ratio_map[ratio] = s
        return list(ratio_map.values())

    def _merge_opening_signals(
        self,
        signals: List[Signal],
        symbol: str,
        rejected: List[Tuple[Optional[Signal], str]],
        cfg: Dict[str, Any],
    ) -> List[Signal]:
        """合并开仓/加仓信号。OPEN 取最大乘数，ADD 累加乘数（再钳位）。"""
        open_map: Dict[Tuple[str, str, str], Signal] = {}
        add_map: Dict[Tuple[str, str, str], Signal] = {}
        max_mult = cfg["max_size_multiplier"]

        for s in signals:
            action = self._get_action(s)
            source = getattr(s, "source", None) or self.DEFAULT_SOURCE
            direction = getattr(s, "direction", "") or ""
            mult = _safe_float(
                getattr(s, "size_multiplier", self.DEFAULT_SIZE_MULTIPLIER),
                self.DEFAULT_SIZE_MULTIPLIER,
            )
            mult = max(0.0, mult)

            if action == OrderAction.OPEN:
                key = (symbol, direction, source)
                if key in open_map:
                    existing = open_map[key]
                    existing_mult = _safe_float(
                        getattr(existing, "size_multiplier", self.DEFAULT_SIZE_MULTIPLIER), 0.0
                    )
                    new_mult = max(existing_mult, mult)
                    try:
                        existing.size_multiplier = new_mult
                    except Exception:
                        pass
                    rejected.append((s, "Merged into existing OPEN (max multiplier)"))
                else:
                    open_map[key] = s
            elif action == OrderAction.ADD:
                key = (symbol, direction, source)
                if key in add_map:
                    existing = add_map[key]
                    existing_mult = _safe_float(
                        getattr(existing, "size_multiplier", self.DEFAULT_SIZE_MULTIPLIER), 0.0
                    )
                    new_mult = existing_mult + mult
                    if new_mult > max_mult:
                        logger.warning(
                            f"ADD size_multiplier overflow ({new_mult:.2f}), clamping to {max_mult}"
                        )
                        new_mult = max_mult
                    try:
                        existing.size_multiplier = new_mult
                    except Exception:
                        pass
                    rejected.append((s, "Merged into existing ADD (summed multiplier)"))
                else:
                    add_map[key] = s

        return list(open_map.values()) + list(add_map.values())

    def _resolve_global_constraints(
        self,
        signals: List[Signal],
        portfolio: Optional[Portfolio],
        prices: Optional[Dict[str, float]],
        rejected: List[Tuple[Optional[Signal], str]],
        cfg: Dict[str, Any],
    ) -> List[Signal]:
        """全局名义价值限制，使用 position_sizer 精确计算。"""
        position_sizer = cfg["position_sizer"]
        if portfolio is None or prices is None or position_sizer is None:
            return signals

        free_margin = self._get_free_margin(portfolio)
        if free_margin is None or free_margin <= 0:
            filtered = [
                s for s in signals
                if self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)
            ]
            filtered_ids = {id(s) for s in filtered}
            for s in signals:
                if id(s) not in filtered_ids:
                    rejected.append((s, "Insufficient free margin (global)"))
            return filtered

        balance = _safe_float(getattr(portfolio, "balance", 0.0), 0.0)
        max_ratio = (
            self.SMALL_ACCOUNT_NOTIONAL_RATIO
            if balance < self.SMALL_ACCOUNT_THRESHOLD
            else cfg["max_total_notional_ratio"]
        )

        current_notional = self._calculate_total_notional(portfolio, prices)
        max_notional = free_margin * max_ratio
        if max_notional < 0 or not math.isfinite(max_notional):
            max_notional = 0.0

        open_like = [
            s for s in signals
            if self._get_action(s) in (OrderAction.OPEN, OrderAction.ADD)
        ]
        others = [
            s for s in signals
            if self._get_action(s) not in (OrderAction.OPEN, OrderAction.ADD)
        ]
        open_like.sort(key=self._priority_value)

        filtered: List[Signal] = list(others)
        notional_used = current_notional
        for s in open_like:
            symbol = getattr(s, "symbol", "") or ""
            price = _safe_float(prices.get(symbol, 0.0), 0.0)
            if not _is_finite_positive(price):
                rejected.append((s, "Invalid price for notional calculation"))
                continue
            try:
                if not hasattr(position_sizer, "estimate_notional"):
                    rejected.append((s, "Position sizer missing estimate_notional"))
                    continue
                est_notional = position_sizer.estimate_notional(s, price, portfolio)
                est_notional = _safe_float(est_notional, 0.0)
            except Exception as e:
                logger.exception(f"Position sizer failed for {symbol}: {e}")
                rejected.append((s, f"Position sizer error: {type(e).__name__}"))
                continue
            if est_notional <= 0:
                rejected.append((s, "Zero or negative notional estimate"))
                continue
            if notional_used + est_notional > max_notional + self.NOTIONAL_EPS:
                logger.warning(
                    f"Global notional limit reached "
                    f"({notional_used + est_notional:.2f} > {max_notional:.2f})"
                )
                rejected.append((s, "Global notional limit"))
            else:
                filtered.append(s)
                notional_used += est_notional
        return filtered

    def _truncate_global(
        self,
        signals: List[Signal],
        max_total: int,
        rejected: Optional[List[Tuple[Optional[Signal], str]]] = None,
    ) -> List[Signal]:
        """全局截断，强制信号不占名额。使用 id 集合避免身份误判。"""
        if len(signals) <= max_total:
            return signals
        forced = [
            s
            for s in signals
            if getattr(s, "is_forced", False)
            and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)
        ]
        forced_ids: Set[int] = {id(s) for s in forced}
        closing = [
            s
            for s in signals
            if id(s) not in forced_ids
            and self._get_action(s) in (OrderAction.CLOSE, OrderAction.REDUCE)
        ]
        close_ids: Set[int] = {id(s) for s in closing}
        rest = [s for s in signals if id(s) not in forced_ids and id(s) not in close_ids]
        available = max_total - len(forced) - len(closing)
        if available < 0:
            available = 0
            excess = len(forced) + len(closing) - max_total
            if excess > 0 and closing:
                dropped = closing[len(closing) - excess :]
                closing = closing[: max(0, len(closing) - excess)]
                if rejected is not None:
                    for s in dropped:
                        rejected.append((s, "Truncated global limit (close)"))
        if len(rest) > available:
            if rejected is not None:
                for s in rest[available:]:
                    rejected.append((s, "Truncated global limit"))
            rest = rest[:available]
        return forced + closing + rest

    def _get_free_margin(self, portfolio: Optional[Portfolio]) -> Optional[float]:
        if portfolio is None:
            return None
        balance = _safe_float(getattr(portfolio, "balance", 0.0), 0.0)
        frozen = _safe_float(getattr(portfolio, "frozen_margin", 0.0), 0.0)
        return max(0.0, balance - frozen)

    def _get_net_position(
        self, portfolio: Optional[Portfolio], symbol: str, cfg: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[str], float]:
        """获取本策略净持仓（方向，净数量绝对值）。持仓列表快照防止并发修改。"""
        if portfolio is None:
            return None, 0.0
        positions = getattr(portfolio, "positions", None)
        if not isinstance(positions, (list, tuple)):
            return None, 0.0
        # 快照，避免遍历中外部修改
        try:
            positions = list(positions)
        except Exception:
            return None, 0.0
        strategy_id = (cfg or {}).get("strategy_id", self.strategy_id)
        long_qty = 0.0
        short_qty = 0.0
        for p in positions:
            if p is None:
                continue
            if getattr(p, "is_frozen", False):
                continue
            if strategy_id and getattr(p, "strategy_id", "") != strategy_id:
                continue
            if getattr(p, "symbol", "") != symbol:
                continue
            qty = abs(_safe_float(getattr(p, "quantity", 0.0), 0.0))
            direction = getattr(p, "direction", "") or ""
            if direction == "LONG":
                long_qty += qty
            elif direction == "SHORT":
                short_qty += qty
        net = long_qty - short_qty
        if net > self.QTY_EPS:
            return "LONG", net
        elif net < -self.QTY_EPS:
            return "SHORT", abs(net)
        else:
            if long_qty > self.QTY_EPS or short_qty > self.QTY_EPS:
                logger.warning(
                    f"Hedging detected for {symbol} (long={long_qty}, short={short_qty})"
                )
            return None, 0.0

    def _calculate_total_notional(
        self, portfolio: Portfolio, prices: Dict[str, float]
    ) -> float:
        """计算当前持仓总名义价值。持仓列表快照。"""
        total = 0.0
        positions = getattr(portfolio, "positions", None)
        if not isinstance(positions, (list, tuple)):
            return 0.0
        try:
            positions = list(positions)
        except Exception:
            return 0.0
        for p in positions:
            if p is None or getattr(p, "is_frozen", False):
                continue
            symbol = getattr(p, "symbol", "") or ""
            price = _safe_float(prices.get(symbol, 0.0), 0.0)
            if not _is_finite_positive(price):
                continue
            qty = abs(_safe_float(getattr(p, "quantity", 0.0), 0.0))
            total += price * qty
        return total if math.isfinite(total) else 0.0

    def get_last_rejected(self) -> List[Tuple[Optional[Signal], str]]:
        """返回最近一次组装被拒绝的信号及原因（同步，兼容原契约）。"""
        return list(self._last_rejected)

    async def get_last_rejected_async(self) -> List[Tuple[Optional[Signal], str]]:
        """并发安全版本。"""
        async with self._lock:
            return list(self._last_rejected)

    def get_metrics(self) -> Dict[str, Any]:
        """返回性能指标（深拷贝，同步，兼容原契约）。"""
        try:
            return deepcopy(self._metrics)
        except Exception:
            return dict(self._metrics)

    async def get_metrics_async(self) -> Dict[str, Any]:
        """并发安全版本。"""
        async with self._lock:
            try:
                return deepcopy(self._metrics)
            except Exception:
                return dict(self._metrics)

    def reset_metrics(self) -> None:
        """重置性能指标（同步，兼容原契约）。"""
        self._metrics = {"total_assemblies": 0, "total_time_ms": 0.0}

    async def reset_metrics_async(self) -> None:
        """并发安全版本。"""
        async with self._lock:
            self._metrics = {"total_assemblies": 0, "total_time_ms": 0.0}

    def __repr__(self) -> str:
        return (
            f"<SignalAssembler max_signals_per_symbol={self.max_signals_per_symbol} "
            f"allow_hedging={self.allow_hedging} max_size_multiplier={self.max_size_multiplier}>"
             )
