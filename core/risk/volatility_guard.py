# -*- coding: utf-8 -*-
"""
模块名称: volatility_guard.py
核心职责: 实时监测市场波动率，在波动率异常升高时自动降低策略杠杆或收紧止损，
          并在波动率回落且稳定后逐步恢复。提供完整的审计日志、状态检查点支持、
          强制干预接口和可启用的保护开关。
所属层级: core.risk

版本: 4.0 (华尔街机构级完美版)
作者: KHAOS Risk Committee
许可证: 内部使用 - 机密
创建日期: 2025-05-10
修改记录:
    - 2026-01-12 机构级重构
    - 2026-07-12 二次审计
    - 2026-07-13 三次审计
    - 2026-07-14 四次审计：完善所有边缘路径，增加启用开关、强制干预理由、数据类冻结

外部依赖:
    - math
    - time (monotonic)
    - threading
    - logging
    - enum
    - dataclasses

接口契约:
    提供: {
        'VolatilityGuard': 核心保护器
        'VolatilityDecision': 决策数据类
        'VolatilityAction': 动作枚举
    }

用法示例:
    guard = VolatilityGuard.from_config(config['volatility_guard'])
    decision = guard.evaluate(vol=0.35, current_lev=2.0, percentile=0.85, base_lev=3.0)
    if decision.action == VolatilityAction.REDUCE_LEVERAGE:
        apply_leverage(decision.target_leverage)
"""

__all__ = ['VolatilityGuard', 'VolatilityDecision', 'VolatilityAction']

import math
import time
import threading
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 模块级常量
_EPSILON = 1e-9
_DEFAULT_MAX_LEVERAGE = 3.0
_DEFAULT_MIN_REDUCTION_INTERVAL_SEC = 600.0


class VolatilityAction(Enum):
    """波动率保护动作枚举"""
    NONE = "none"
    REDUCE_LEVERAGE = "reduce_leverage"
    RESTORE_LEVERAGE = "restore_leverage"
    TIGHTEN_STOPS = "tighten_stops"


@dataclass(frozen=True)
class VolatilityDecision:
    """不可变的波动率保护决策结果"""
    action: VolatilityAction
    target_leverage: float
    reason: str
    current_volatility: float
    percentile: float
    original_percentile: float = 0.0
    clipped: bool = False

    def __post_init__(self):
        """数据校验，确保目标杠杆非负"""
        if self.target_leverage < 0:
            raise ValueError(f"target_leverage 不能为负: {self.target_leverage}")

    def __repr__(self) -> str:
        return (f"VolatilityDecision(action={self.action.value}, "
                f"target_leverage={self.target_leverage:.2f}, "
                f"reason='{self.reason}')")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'action': self.action.value,
            'target_leverage': self.target_leverage,
            'reason': self.reason,
            'current_volatility': self.current_volatility,
            'percentile': self.percentile,
            'original_percentile': self.original_percentile,
            'clipped': self.clipped,
        }


class VolatilityGuard:
    """
    波动率自适应保护器。
    当市场波动率超过历史高分位数时，自动降低杠杆（或收紧止损）；
    波动率回落后，经过冷却期逐步恢复杠杆。
    支持进一步降杠杆、强制干预、全局启用/禁用。
    所有公共方法线程安全。
    """

    def __init__(self,
                 vol_guard_threshold: float = 0.8,
                 vol_guard_reduce_factor: float = 0.8,
                 vol_guard_min_leverage: float = 1.0,
                 vol_guard_restore_threshold: float = 0.6,
                 restore_cooldown_hours: float = 24.0,
                 action_type: str = 'reduce_leverage',
                 max_leverage: float = _DEFAULT_MAX_LEVERAGE,
                 min_reduction_interval_sec: float = _DEFAULT_MIN_REDUCTION_INTERVAL_SEC,
                 enabled: bool = True):
        """
        初始化保护器，详细参数说明见文档。
        Raises: ValueError 当参数非法时
        """
        # 参数校验
        if not (0.0 < vol_guard_threshold <= 1.0):
            raise ValueError(f"vol_guard_threshold 必须在 (0,1] 之间，收到 {vol_guard_threshold}")
        if not (0.0 <= vol_guard_restore_threshold < vol_guard_threshold - 0.05):
            raise ValueError("vol_guard_restore_threshold 必须小于 vol_guard_threshold 至少 0.05")
        if not (0.0 < vol_guard_reduce_factor < 1.0):
            raise ValueError(f"vol_guard_reduce_factor 必须在 (0,1) 之间")
        if vol_guard_min_leverage <= 0:
            raise ValueError(f"vol_guard_min_leverage 必须 > 0")
        if restore_cooldown_hours < 0:
            raise ValueError("restore_cooldown_hours 不能为负数")
        if max_leverage < vol_guard_min_leverage:
            raise ValueError(f"max_leverage ({max_leverage}) 不能小于 vol_guard_min_leverage ({vol_guard_min_leverage})")
        if action_type not in ('reduce_leverage', 'tighten_stops'):
            raise ValueError(f"不支持的动作类型: {action_type}")
        if vol_guard_reduce_factor > 0.95:
            logger.warning(f"降杠杆因子 ({vol_guard_reduce_factor}) 接近1，保护效果微弱")
        if min_reduction_interval_sec < 0:
            raise ValueError("min_reduction_interval_sec 不能为负数")

        self._vol_guard_threshold = vol_guard_threshold
        self._vol_guard_reduce_factor = vol_guard_reduce_factor
        self._vol_guard_min_leverage = vol_guard_min_leverage
        self._vol_guard_restore_threshold = vol_guard_restore_threshold
        self._restore_cooldown_sec = restore_cooldown_hours * 3600.0
        self._action_type = action_type
        self._max_leverage = max_leverage
        self._min_reduction_interval_sec = min_reduction_interval_sec
        self._enabled = enabled

        # 内部状态
        self._lock = threading.Lock()
        self._is_reduced = False
        self._original_leverage: Optional[float] = None
        self._last_reduction_time = 0.0
        self._last_restore_time = 0.0
        self._last_cooling_log_time = -1e9

        logger.info(f"VolatilityGuard 初始化: enabled={enabled}, threshold={vol_guard_threshold:.2f}, "
                    f"reduce_factor={vol_guard_reduce_factor}, min_lev={vol_guard_min_leverage}, "
                    f"restore_th={vol_guard_restore_threshold}, cooldown_h={restore_cooldown_hours}, "
                    f"action={action_type}, max_lev={max_leverage}, min_intv={min_reduction_interval_sec}s")

    # ========== 只读属性 ==========
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_reduced(self) -> bool:
        with self._lock:
            return self._is_reduced

    @property
    def last_reduction_time(self) -> float:
        """返回上次降杠杆的 monotonic 时间戳"""
        with self._lock:
            return self._last_reduction_time

    @property
    def last_restore_time(self) -> float:
        with self._lock:
            return self._last_restore_time

    @property
    def threshold(self) -> float:
        return self._vol_guard_threshold

    @property
    def restore_threshold(self) -> float:
        return self._vol_guard_restore_threshold

    @property
    def restore_cooldown_hours(self) -> float:
        return self._restore_cooldown_sec / 3600.0

    @property
    def min_reduction_interval_sec(self) -> float:
        return self._min_reduction_interval_sec

    @property
    def cooldown_remaining_seconds(self) -> float:
        """若当前处于保护状态，返回恢复冷却剩余秒数；否则返回0"""
        with self._lock:
            if not self._is_reduced:
                return 0.0
            elapsed = time.monotonic() - self._last_restore_time
            remaining = self._restore_cooldown_sec - elapsed
            return max(0.0, remaining)

    # ========== 核心评估 ==========
    def evaluate(self,
                 current_volatility: float,
                 current_leverage: float,
                 percentile: float,
                 base_leverage: float) -> VolatilityDecision:
        """
        评估当前波动率，返回杠杆调整建议。
        输入参数将被严格校验，非法值导致安全 NONE 决策。
        """
        # 若模块被禁用，直接返回正常
        if not self._enabled:
            return VolatilityDecision(VolatilityAction.NONE, current_leverage,
                                      '保护已禁用', current_volatility, percentile)

        # 输入验证
        decision = self._validate_inputs(current_volatility, current_leverage, percentile, base_leverage)
        if decision is not None:
            return decision

        base_leverage = max(1.0, min(base_leverage, self._max_leverage))
        original_percentile = percentile
        clipped = False
        if math.isnan(percentile) or math.isinf(percentile):
            percentile = 0.5
            clipped = True
        elif not (0.0 <= percentile <= 1.0):
            percentile = max(0.0, min(1.0, percentile))
            clipped = True

        now = time.monotonic()

        with self._lock:
            return self._evaluate_locked(current_volatility, current_leverage, percentile,
                                         base_leverage, original_percentile, clipped, now)

    def _evaluate_locked(self, vol, cur_lev, pct, base_lev, orig_pct, clipped, now):
        """锁内评估逻辑，降低 evaluate 方法复杂度"""
        # 处于保护状态
        if self._is_reduced:
            # 是否进一步降杠杆
            if pct >= self._vol_guard_threshold:
                if now - self._last_reduction_time >= self._min_reduction_interval_sec:
                    new_lev = max(round(cur_lev * self._vol_guard_reduce_factor, 4),
                                  self._vol_guard_min_leverage)
                    if new_lev < cur_lev - _EPSILON:
                        self._last_reduction_time = now
                        self._last_restore_time = now
                        self._last_cooling_log_time = -1e9
                        logger.warning(f"波动率仍高 ({orig_pct:.1%})，进一步降杠杆: {cur_lev:.2f} -> {new_lev:.2f}")
                        return VolatilityDecision(self._action_enum(), new_lev,
                                                  f'波动率分位数({orig_pct:.1%})仍超阈值，继续降杠杆',
                                                  vol, pct, orig_pct, clipped)
                    else:
                        logger.info("杠杆已为最低，无法再降")
                        return VolatilityDecision(VolatilityAction.NONE, cur_lev,
                                                  '杠杆已达最低', vol, pct, orig_pct, clipped)
                else:
                    logger.debug("降杠杆间隔未满，跳过")
                    return VolatilityDecision(VolatilityAction.NONE, cur_lev,
                                              '降杠杆间隔未满', vol, pct, orig_pct, clipped)

            # 是否恢复
            if pct <= self._vol_guard_restore_threshold:
                if now - self._last_restore_time >= self._restore_cooldown_sec:
                    target = min(base_lev, self._max_leverage)
                    if target > cur_lev + _EPSILON:
                        self._is_reduced = False
                        self._last_restore_time = now
                        self._original_leverage = None
                        self._last_cooling_log_time = -1e9
                        logger.info(f"波动率回落 ({orig_pct:.1%})，恢复杠杆至 {target:.2f}")
                        return VolatilityDecision(VolatilityAction.RESTORE_LEVERAGE, target,
                                                  f'波动率分位数降至恢复阈值以下，恢复杠杆',
                                                  vol, pct, orig_pct, clipped)
                    else:
                        return VolatilityDecision(VolatilityAction.NONE, cur_lev,
                                                  '目标杠杆不高于当前', vol, pct, orig_pct, clipped)
                else:
                    if now - self._last_cooling_log_time >= 600:
                        remaining = (self._restore_cooldown_sec - (now - self._last_restore_time)) / 3600.0
                        logger.debug(f"恢复冷却中，还需 {remaining:.1f} 小时")
                        self._last_cooling_log_time = now
                    return VolatilityDecision(VolatilityAction.NONE, cur_lev,
                                              '恢复冷却中', vol, pct, orig_pct, clipped)
            else:
                return VolatilityDecision(VolatilityAction.NONE, cur_lev,
                                          '波动率未显著变化，维持保护', vol, pct, orig_pct, clipped)

        # 未保护，检查触发
        if pct >= self._vol_guard_threshold:
            new_lev = max(round(cur_lev * self._vol_guard_reduce_factor, 4),
                          self._vol_guard_min_leverage)
            if new_lev < cur_lev - _EPSILON:
                self._is_reduced = True
                self._original_leverage = cur_lev
                self._last_reduction_time = now
                self._last_restore_time = now
                self._last_cooling_log_time = -1e9
                logger.warning(f"波动率触发保护 ({orig_pct:.1%})，降杠杆: {cur_lev:.2f} -> {new_lev:.2f}")
                return VolatilityDecision(self._action_enum(), new_lev,
                                          f'波动率分位数({orig_pct:.1%})超过阈值{self._vol_guard_threshold}',
                                          vol, pct, orig_pct, clipped)
            else:
                return VolatilityDecision(VolatilityAction.NONE, cur_lev,
                                          '杠杆已达最低，无法降杠杆', vol, pct, orig_pct, clipped)

        # 正常
        return VolatilityDecision(VolatilityAction.NONE, cur_lev,
                                  '波动率正常', vol, pct, orig_pct, clipped)

    # ========== 强制干预 ==========
    def force_reduce(self, current_leverage: float, reason: str = "人工强制降杠杆") -> VolatilityDecision:
        """手动强制降杠杆，无视冷却和间隔。若保护已禁用则警告并忽略。"""
        if not self._enabled:
            logger.warning("保护已禁用，强制降杠杆无效")
            return VolatilityDecision(VolatilityAction.NONE, current_leverage, "保护已禁用", 0.0, 0.0)
        if current_leverage <= 0:
            logger.error("无效杠杆")
            return VolatilityDecision(VolatilityAction.NONE, current_leverage, "无效杠杆", 0.0, 0.0)
        with self._lock:
            new_lev = max(round(current_leverage * self._vol_guard_reduce_factor, 4),
                          self._vol_guard_min_leverage)
            self._is_reduced = True
            now = time.monotonic()
            self._last_reduction_time = now
            self._last_restore_time = now
            self._last_cooling_log_time = -1e9
            logger.warning(f"强制降杠杆: {current_leverage:.2f} -> {new_lev:.2f}, reason: {reason}")
            return VolatilityDecision(self._action_enum(), new_lev, reason, 0.0, 0.0)

    def force_restore(self, base_leverage: float, reason: str = "人工强制恢复杠杆") -> VolatilityDecision:
        """手动强制恢复杠杆，无视冷却。若保护已禁用则警告。"""
        if not self._enabled:
            logger.warning("保护已禁用，强制恢复杠杆无效")
            return VolatilityDecision(VolatilityAction.NONE, base_leverage, "保护已禁用", 0.0, 0.0)
        with self._lock:
            target = min(base_leverage, self._max_leverage)
            self._is_reduced = False
            self._original_leverage = None
            self._last_restore_time = time.monotonic()
            self._last_cooling_log_time = -1e9
            logger.info(f"强制恢复杠杆至 {target:.2f}, reason: {reason}")
            return VolatilityDecision(VolatilityAction.RESTORE_LEVERAGE, target, reason, 0.0, 0.0)

    # ========== 状态管理 ==========
    def reset(self) -> None:
        with self._lock:
            self._is_reduced = False
            self._original_leverage = None
            self._last_reduction_time = 0.0
            self._last_restore_time = 0.0
            self._last_cooling_log_time = -1e9
            logger.info("VolatilityGuard 状态已重置")

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'is_reduced': self._is_reduced,
                'original_leverage': self._original_leverage,
                'last_reduction_time': self._last_reduction_time,
                'last_restore_time': self._last_restore_time,
                'last_cooling_log_time': self._last_cooling_log_time,
            }

    def set_state(self, state: Dict[str, Any]) -> None:
        if not isinstance(state, dict):
            logger.error("状态数据必须为字典")
            return
        with self._lock:
            self._is_reduced = state.get('is_reduced', False)
            self._original_leverage = state.get('original_leverage', None)
            self._last_reduction_time = state.get('last_reduction_time', 0.0)
            self._last_restore_time = state.get('last_restore_time', 0.0)
            self._last_cooling_log_time = state.get('last_cooling_log_time', -1e9)
            logger.info("VolatilityGuard 状态已从检查点恢复")

    def get_status_summary(self) -> Dict[str, Any]:
        with self._lock:
            remaining = 0.0
            if self._is_reduced:
                elapsed = time.monotonic() - self._last_restore_time
                remaining = max(0.0, self._restore_cooldown_sec - elapsed)
            return {
                'enabled': self._enabled,
                'protected': self._is_reduced,
                'action_type': self._action_type,
                'original_leverage': self._original_leverage,
                'cooldown_remaining_seconds': remaining,
                'params': self.to_config(),
            }

    # ========== 配置构建 ==========
    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'VolatilityGuard':
        return cls(
            vol_guard_threshold=config.get('vol_guard_threshold', 0.8),
            vol_guard_reduce_factor=config.get('vol_guard_reduce_factor', 0.8),
            vol_guard_min_leverage=config.get('vol_guard_min_leverage', 1.0),
            vol_guard_restore_threshold=config.get('vol_guard_restore_threshold', 0.6),
            restore_cooldown_hours=config.get('restore_cooldown_hours', 24.0),
            action_type=config.get('action_type', 'reduce_leverage'),
            max_leverage=config.get('max_leverage', _DEFAULT_MAX_LEVERAGE),
            min_reduction_interval_sec=config.get('min_reduction_interval_sec', _DEFAULT_MIN_REDUCTION_INTERVAL_SEC),
            enabled=config.get('enabled', True),
        )

    def to_config(self) -> Dict[str, Any]:
        return {
            'vol_guard_threshold': self._vol_guard_threshold,
            'vol_guard_reduce_factor': self._vol_guard_reduce_factor,
            'vol_guard_min_leverage': self._vol_guard_min_leverage,
            'vol_guard_restore_threshold': self._vol_guard_restore_threshold,
            'restore_cooldown_hours': self.restore_cooldown_hours,
            'action_type': self._action_type,
            'max_leverage': self._max_leverage,
            'min_reduction_interval_sec': self._min_reduction_interval_sec,
            'enabled': self._enabled,
        }

    # ========== 内部工具 ==========
    def _validate_inputs(self, vol, lev, pct, base_lev) -> Optional[VolatilityDecision]:
        """返回 None 表示通过，否则返回错误决策"""
        if vol is None or lev is None or pct is None or base_lev is None:
            logger.error("输入参数包含 None")
            return VolatilityDecision(VolatilityAction.NONE, lev if lev else 0, "输入参数包含 None", 0.0, 0.0)
        if not isinstance(vol, (int, float)) or not isinstance(lev, (int, float)):
            logger.error("输入参数类型错误")
            return VolatilityDecision(VolatilityAction.NONE, lev, "输入参数类型错误", 0.0, 0.0)
        if math.isnan(vol) or math.isinf(vol):
            logger.error(f"无效波动率: {vol}")
            return VolatilityDecision(VolatilityAction.NONE, lev, "波动率为 NaN 或 Inf", 0.0, 0.0)
        if vol < 0:
            logger.warning(f"波动率为负数: {vol}")
            return VolatilityDecision(VolatilityAction.NONE, lev, "波动率为负数", 0.0, 0.0)
        if lev <= 0:
            logger.error(f"无效杠杆: {lev}")
            return VolatilityDecision(VolatilityAction.NONE, lev, "杠杆 <= 0", 0.0, 0.0)
        return None

    def _action_enum(self) -> VolatilityAction:
        return (VolatilityAction.REDUCE_LEVERAGE if self._action_type == 'reduce_leverage'
                else VolatilityAction.TIGHTEN_STOPS)

    def __repr__(self) -> str:
        return (f"VolatilityGuard(enabled={self._enabled}, threshold={self._vol_guard_threshold}, "
                f"reduce_factor={self._vol_guard_reduce_factor}, is_reduced={self.is_reduced})")


# ========== 简单自测 ==========
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    guard = VolatilityGuard(restore_cooldown_hours=0.01)  # 短冷却方便测试
    # 模拟波动率升高
    decision = guard.evaluate(0.4, 2.0, 0.85, 3.0)
    print(decision)
    # 模拟回落
    decision2 = guard.evaluate(0.2, 1.6, 0.5, 3.0)
    print(decision2)
    # 等待冷却后恢复（由于冷却0.01小时=36秒，此处不会立即恢复）
    print("冷却剩余:", guard.cooldown_remaining_seconds)
    guard.force_restore(3.0)
    print("强制恢复后:", guard.is_reduced)
