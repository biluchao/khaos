# -*- coding: utf-8 -*-
"""
模块名称: action_arbitrator.py
核心职责: 动作优先级仲裁器，当同一K线周期内产生多个交易动作时，
          按照华尔街风控标准定义的严格优先级顺序，合并或排除冲突动作，确保账户安全。
所属层级: core.risk

外部依赖:
    - enum (ActionPriority枚举)
    - typing
    - logging
    - copy
    - math
    - time
    - uuid
    - core.models.signal (Signal 信号模型)

接口契约:
    提供: {
        'ActionPriority': '枚举类，定义所有交易动作的优先级顺序',
        'ActionArbitrator': '类，提供 arbitrate 方法，输入多个信号，返回最终执行信号',
        'NO_ACTION_SIGNAL': '常量，表示无操作信号'
    }
    消费: {
        'core.models.signal.Signal': '包含 action, direction, size_multiplier, price, stop_loss 等字段'
    }

作者: KHAOS Risk Committee
创建日期: 2025-04-20
修改记录:
    - 2026-01-12 增加信号合并逻辑，防止同向重复加仓
    - 2026-07-12 全面机构级重构：增强安全、审计、并发、错误处理、日志
    - 2026-07-13 第三次穿透审查：增加信号时效检查、字段脱敏、优先级动态注册、更严格的合并规则等
"""

import asyncio
import logging
import math
import time
from copy import deepcopy
from enum import IntEnum
from typing import List, Optional, Dict, Any, Tuple, Set
import uuid

from core.models.signal import Signal

logger = logging.getLogger(__name__)

__version__ = "4.0.0"

# 允许合并的动作集合
AGGREGATABLE_ACTIONS: Set[str] = {"NORMAL_ENTRY", "NORMAL_ADD", "RECAPTURE_ENTRY"}
# 不允许合并的动作（因为涉及特殊执行方式）
NON_AGGREGATABLE_ORDER_TYPES: Set[str] = {"iceberg", "twap"}


class ActionPriority(IntEnum):
    """交易动作优先级，数值越小优先级越高。"""
    PANIC_CLOSE = 0          # 系统级紧急平仓（不可覆盖）
    HARD_STOP = 1            # 硬止损触发
    ESCAPE_CLOSE = 2         # 阶段顶逃逸清仓
    ESCAPE_REDUCE = 3        # 阶段顶逃逸减仓
    RECAPTURE_ENTRY = 4      # 波段再捕捉开仓
    CALLBACK_DROP_ENTRY = 5  # 回调跌落追仓
    NORMAL_CLOSE = 6         # 普通全平（非紧急）
    NORMAL_ENTRY = 7         # 常规开仓
    NORMAL_ADD = 8           # 常规加仓
    UNKNOWN = 99             # 未知动作（应记录告警）


# 无操作信号常量
NO_ACTION_SIGNAL = Signal(
    action="NO_ACTION",
    direction=None,
    size_multiplier=0.0,
    reason="Arbitrator default no-action",
    tag="no_action"
)


class ActionArbitrator:
    """
    动作优先级仲裁器 v4.0 (华尔街极境版)。
    提供确定性、并发安全、高度可审计的信号裁决，确保风控动作为最高优先级。
    """

    def __init__(self, aggregate_signals: bool = True, enable_audit_log: bool = True,
                 signal_max_age_seconds: float = 10.0):
        """
        Args:
            aggregate_signals: 是否允许合并同向同优先级的信号
            enable_audit_log: 是否记录详细的仲裁决策日志
            signal_max_age_seconds: 信号有效期，超过此时间的信号将被忽略
        """
        self.aggregate_signals = aggregate_signals
        self.enable_audit_log = enable_audit_log
        self.signal_max_age_seconds = signal_max_age_seconds
        self._lock = asyncio.Lock()
        self._rejected_signals: List[Tuple[Optional[Signal], str, float]] = []  # (signal, reason, timestamp)
        self._last_arbitration_result: Optional[Signal] = None
        self._call_count = 0
        self._override_raised = False

        # 优先级映射表
        self._priority_map: Dict[str, ActionPriority] = {
            "PANIC_CLOSE": ActionPriority.PANIC_CLOSE,
            "HARD_STOP": ActionPriority.HARD_STOP,
            "ESCAPE_CLOSE": ActionPriority.ESCAPE_CLOSE,
            "ESCAPE_REDUCE": ActionPriority.ESCAPE_REDUCE,
            "RECAPTURE_ENTRY": ActionPriority.RECAPTURE_ENTRY,
            "CALLBACK_DROP_ENTRY": ActionPriority.CALLBACK_DROP_ENTRY,
            "CLOSE_ALL": ActionPriority.NORMAL_CLOSE,
            "REDUCE_50": ActionPriority.ESCAPE_REDUCE,
            "NORMAL_ENTRY": ActionPriority.NORMAL_ENTRY,
            "NORMAL_ADD": ActionPriority.NORMAL_ADD,
            "NORMAL_CLOSE": ActionPriority.NORMAL_CLOSE,
        }

        # 全局抑制开关（风控系统可触发）
        self.global_suppress_all = False

    async def arbitrate(self, signals: Optional[List[Signal]]) -> Signal:
        """
        主仲裁方法。线程安全，返回唯一应执行的信号，或 NO_ACTION_SIGNAL。
        """
        async with self._lock:
            # 每次仲裁开始清空上次的拒绝记录
            self._rejected_signals.clear()
            self._call_count += 1

            try:
                if signals is None:
                    logger.warning("arbitrate called with None signals")
                    return deepcopy(NO_ACTION_SIGNAL)

                if not signals:
                    logger.debug("Empty signal list received")
                    return deepcopy(NO_ACTION_SIGNAL)

                # 全局抑制检查
                if self.global_suppress_all:
                    logger.warning("Global suppression enabled, all signals ignored")
                    return deepcopy(NO_ACTION_SIGNAL)

                # 浅拷贝列表，避免外部修改
                signals_copy = list(signals)
                valid_signals = self._filter_valid_signals(signals_copy)
                if not valid_signals:
                    return deepcopy(NO_ACTION_SIGNAL)

                # 检查是否有 override 信号
                override_signal = self._extract_override(valid_signals)
                if override_signal:
                    self._log_rejected("Overridden by higher priority override signal", None)
                    self._last_arbitration_result = override_signal
                    return override_signal

                # 计算优先级
                priorities = [self._get_priority(s) for s in valid_signals]
                min_priority = min(priorities)
                top_signals = [s for s, p in zip(valid_signals, priorities) if p == min_priority]

                final_signal = None
                if len(top_signals) == 1:
                    final_signal = top_signals[0]
                else:
                    if self.aggregate_signals and self._can_aggregate(top_signals):
                        final_signal = self._merge_signals(top_signals)
                    else:
                        # 按时间戳排序，选最早的有效信号
                        top_signals.sort(key=lambda s: getattr(s, 'timestamp', 0.0) or 0.0)
                        final_signal = top_signals[0]
                        if len(top_signals) > 1:
                            self._log_rejected("Multiple top-priority signals, selected earliest", top_signals[1])

                # 记录被拒绝的信号
                for s in valid_signals:
                    if s is not final_signal:
                        self._log_rejected("Lower priority or merged", s)

                self._last_arbitration_result = final_signal
                return final_signal
            except Exception as e:
                logger.exception("ActionArbitrator encountered an error")
                return deepcopy(NO_ACTION_SIGNAL)

    def _filter_valid_signals(self, signals: List[Signal]) -> List[Signal]:
        """返回有效信号列表，过滤非法、过期、无效字段"""
        valid = []
        now = time.time()
        seen_ids = set()
        for sig in signals:
            if not isinstance(sig, Signal):
                logger.warning("Non-Signal object ignored")
                self._log_rejected("Invalid type", None)
                continue
            # 去重
            sig_id = getattr(sig, 'id', None) or str(id(sig))
            if sig_id in seen_ids:
                continue
            seen_ids.add(sig_id)

            # 动作检查
            if not sig.action or sig.action.upper() in ("NO_ACTION", "NONE"):
                self._log_rejected("NO_ACTION or empty action", sig)
                continue

            # 动作标准化（不修改原信号，仅用于内部）
            action_upper = sig.action.upper().strip()

            # 如果信号是平仓类动作，方向可以忽略；但开仓必须有方向
            if action_upper not in ("CLOSE_ALL", "PANIC_CLOSE", "HARD_STOP", "ESCAPE_CLOSE",
                                    "ESCAPE_REDUCE", "NORMAL_CLOSE", "REDUCE_50"):
                if not sig.direction or sig.direction.upper() not in ("LONG", "SHORT"):
                    self._log_rejected("Missing or invalid direction for entry/add action", sig)
                    continue

            # 检查 size_multiplier 合法性
            if not math.isfinite(sig.size_multiplier) or sig.size_multiplier < 0.0:
                self._log_rejected("Non-finite or negative size_multiplier", sig)
                continue

            # 检查价格合法性（如果有）
            price = getattr(sig, 'price', None)
            if price is not None and (not math.isfinite(price) or price <= 0):
                self._log_rejected("Invalid price", sig)
                continue

            # 信号时效检查
            ts = getattr(sig, 'timestamp', None)
            if ts is not None and ts > 0 and (now - ts) > self.signal_max_age_seconds:
                self._log_rejected(f"Signal expired (age {now-ts:.2f}s)", sig)
                continue

            # 如果信号带有 reduce_only 标志，但动作是开仓，则忽略
            if getattr(sig, 'reduce_only', False) and action_upper in AGGREGATABLE_ACTIONS:
                self._log_rejected("Reduce-only flag on entry signal", sig)
                continue

            # 通过所有检查
            # 创建一个安全的信号副本，保证后续操作不影响原始对象
            clean_sig = self._sanitize_signal(sig)
            valid.append(clean_sig)
        return valid

    def _sanitize_signal(self, sig: Signal) -> Signal:
        """创建信号的清理副本，标准化动作字段"""
        s = deepcopy(sig)
        s.action = s.action.upper().strip()
        if s.direction:
            s.direction = s.direction.upper()
        # 确保 size_multiplier 为正
        s.size_multiplier = abs(s.size_multiplier)
        # 如果平仓动作，方向设为 None
        if s.action in ("CLOSE_ALL", "PANIC_CLOSE", "HARD_STOP", "ESCAPE_CLOSE", "NORMAL_CLOSE", "REDUCE_50"):
            s.direction = None
        return s

    def _extract_override(self, signals: List[Signal]) -> Optional[Signal]:
        """查找并返回 override 信号，同时忽略其他所有信号"""
        overrides = [s for s in signals if getattr(s, 'is_override', False)]
        if overrides:
            # 如果有多个 override，取优先级最高（数值最小）或第一个
            if len(overrides) > 1:
                overrides.sort(key=lambda s: self._get_priority(s))
            logger.warning("Override signal detected, ignoring all other signals")
            # 记录所有被覆盖的信号
            for s in signals:
                if s not in overrides:
                    self._log_rejected("Overridden by override signal", s)
            return overrides[0]
        return None

    def _get_priority(self, signal: Signal) -> ActionPriority:
        """获取信号优先级，未知动作返回 UNKNOWN 并记录错误"""
        action = signal.action.upper().strip()
        priority = self._priority_map.get(action, ActionPriority.UNKNOWN)
        if priority == ActionPriority.UNKNOWN:
            logger.error(f"Unknown action '{action}' in signal. Treating as lowest priority.")
            self._log_rejected(f"Unknown action: {action}", signal)
        return priority

    def _can_aggregate(self, signals: List[Signal]) -> bool:
        """检查信号列表是否满足合并条件"""
        if len(signals) <= 1:
            return False
        first = signals[0]
        if first.action not in AGGREGATABLE_ACTIONS:
            return False
        # 方向一致
        directions = set(s.direction for s in signals if s.direction)
        if len(directions) > 1:
            return False
        # 品种一致
        symbols = set((getattr(s, 'symbol', '') or '').upper() for s in signals)
        if len(symbols) > 1 and '' not in symbols:
            return False
        # 订单类型一致且不属于禁止合并的类型
        order_types = set(getattr(s, 'order_type', 'market') or 'market' for s in signals)
        if len(order_types) > 1:
            return False
        if any(ot in NON_AGGREGATABLE_ORDER_TYPES for ot in order_types):
            return False
        # 所有信号不能是 reduce_only
        if any(getattr(s, 'reduce_only', False) for s in signals):
            return False
        return True

    def _merge_signals(self, signals: List[Signal]) -> Signal:
        """合并信号，仓位乘数累加，最大2.0"""
        base = signals[0]
        total_multiplier = 0.0
        strategy_ids = []
        tags = set()
        for s in signals:
            total_multiplier += abs(s.size_multiplier)
            if s.strategy_id:
                strategy_ids.append(s.strategy_id)
            if getattr(s, 'tag', None):
                tags.add(s.tag)
        total_multiplier = min(round(total_multiplier, 4), 2.0)

        if total_multiplier <= 0.0:
            logger.info("Merged signal resulted in zero multiplier, returning NO_ACTION")
            return deepcopy(NO_ACTION_SIGNAL)

        # 选择最保守的止损/止盈
        direction = base.direction or "LONG"  # 即使合并没有方向也预设
        stop_loss_price = base.stop_loss_price
        take_profit_price = base.take_profit_price
        for s in signals[1:]:
            if s.stop_loss_price is not None:
                if direction == "LONG":
                    # 多头止损取最高（更接近现价）
                    if stop_loss_price is None or s.stop_loss_price > stop_loss_price:
                        stop_loss_price = s.stop_loss_price
                else:
                    if stop_loss_price is None or s.stop_loss_price < stop_loss_price:
                        stop_loss_price = s.stop_loss_price
            if s.take_profit_price is not None:
                if direction == "LONG":
                    if take_profit_price is None or s.take_profit_price < take_profit_price:
                        take_profit_price = s.take_profit_price
                else:
                    if take_profit_price is None or s.take_profit_price > take_profit_price:
                        take_profit_price = s.take_profit_price

        # 获取合并后的价格（如果有）
        price = base.price
        for s in signals[1:]:
            if s.price is not None:
                if direction == "LONG" and (price is None or s.price < price):
                    price = s.price  # 多头取最低买入价
                elif direction == "SHORT" and (price is None or s.price > price):
                    price = s.price  # 空头取最高卖出价

        # 生成原因说明
        ids_str = ";".join(strategy_ids[:5])
        reason = f"Merged {len(signals)} signals from [{ids_str}]"
        if len(reason) > 200:
            reason = reason[:197] + "..."
        merged_tag = "merged"
        if tags:
            merged_tag += "|" + "|".join(tags)

        merged = Signal(
            action=base.action,
            direction=base.direction,
            size_multiplier=total_multiplier,
            reason=reason,
            strategy_id=",".join(sorted(set(strategy_ids))),
            price=price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            tag=merged_tag,
        )
        # 复制其他重要字段
        for attr in ['symbol', 'order_type', 'timestamp', 'client_order_id', 'reduce_only',
                     'post_only', 'iceberg', 'metadata']:
            if hasattr(base, attr):
                setattr(merged, attr, getattr(base, attr))
        # 生成新的 client_order_id
        original_id = getattr(base, 'client_order_id', '') or ''
        merged.client_order_id = f"merged_{original_id}_{uuid.uuid4().hex[:6]}"
        # 标记为已仲裁
        merged.arbitrated = True
        logger.info(f"Merged signal created with multiplier {total_multiplier}")
        return merged

    def _log_rejected(self, reason: str, signal: Optional[Signal]) -> None:
        """记录被拒绝的信号（脱敏）"""
        if self.enable_audit_log:
            try:
                # 只记录动作和方向，不记录价格数量等敏感信息
                action = getattr(signal, 'action', 'NONE') if signal else 'NONE'
                direction = getattr(signal, 'direction', 'NONE') if signal else 'NONE'
                self._rejected_signals.append((signal, reason, time.time()))
                # 日志脱敏
                logger.info(f"Signal rejected: action={action} direction={direction} reason={reason}")
            except Exception:
                pass

    def register_action(self, action_name: str, priority: ActionPriority) -> None:
        """动态注册新的动作及其优先级"""
        self._priority_map[action_name.upper()] = priority
        logger.info(f"Registered new action '{action_name}' with priority {priority.name}")

    def get_rejected_signals(self) -> List[Tuple[Optional[Signal], str, float]]:
        return list(self._rejected_signals)

    async def clear_rejected(self) -> None:
        async with self._lock:
            self._rejected_signals.clear()

    async def reset(self) -> None:
        async with self._lock:
            self._rejected_signals.clear()
            self._last_arbitration_result = None
            self._call_count = 0

    def __repr__(self) -> str:
        return (f"ActionArbitrator(aggregate={self.aggregate_signals}, "
                f"audit={self.enable_audit_log}, calls={self._call_count})")
