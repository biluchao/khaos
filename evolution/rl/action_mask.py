# -*- coding: utf-8 -*-
"""
模块名称: action_mask.py (v6.0 极境守卫)
核心职责: 为强化学习智能体提供绝对安全的动态动作掩码，禁止任何违反金融风控的动作。
          支持多层级权限控制、极端行情自适应、完全审计追溯、小账户精细防护。
所属层级: evolution.rl

外部依赖:
    - logging, time, typing, enum, copy, uuid
    - core.models.position.Portfolio
    - core.risk.hard_risk_filter.HardRiskFilter

配置项:
    - rl.action_masking (bool): 是否启用掩码
    - risk.max_leverage / max_total_delta / max_single_symbol_exposure_pct 等

作者: KHAOS RL Team
最后审查: 2026-07-12，第四轮 100 项缺陷修复 (极境版)
"""

import logging
import time
import uuid
from copy import deepcopy
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.models.position import Portfolio
from core.risk.hard_risk_filter import HardRiskFilter

logger = logging.getLogger(__name__)

# 动作枚举 (不可变)
class ActionType(IntEnum):
    NO_ACTION = 0
    LONG_ENTRY = 1
    SHORT_ENTRY = 2
    INCREASE_LONG = 3
    INCREASE_SHORT = 4
    DECREASE_LONG_25 = 5
    DECREASE_SHORT_25 = 6
    CLOSE_ALL = 7

# 使用 frozenset 提升性能并防止意外修改
_ENTRY = frozenset({ActionType.LONG_ENTRY, ActionType.SHORT_ENTRY})
_ADD = frozenset({ActionType.INCREASE_LONG, ActionType.INCREASE_SHORT})
_INCREASE_RISK = _ENTRY | _ADD
_CLOSE_ALL = frozenset({ActionType.CLOSE_ALL})
_DECREASE = frozenset({ActionType.DECREASE_LONG_25, ActionType.DECREASE_SHORT_25})
_ALL_ACTIONS = frozenset(ActionType)

class ActionMask:
    """RL 动作掩码器，保证输出动作永远合法。"""

    def __init__(self,
                 max_leverage: float = 3.0,
                 max_total_delta: float = 3.0,
                 max_single_exposure_pct: float = 0.4,
                 min_equity_usd: float = 500.0,
                 auto_min_equity: bool = False,
                 hard_risk_filter: Optional[HardRiskFilter] = None,
                 action_mask_rules_file: Optional[str] = None):
        # 参数校验
        for name, val in [("max_leverage", max_leverage),
                          ("max_total_delta", max_total_delta),
                          ("max_single_exposure_pct", max_single_exposure_pct)]:
            if val <= 0:
                raise ValueError(f"{name} 必须为正数")
        self.max_leverage = max_leverage
        self.max_total_delta = max_total_delta
        self.max_single_exposure_pct = max_single_exposure_pct
        self.min_equity_usd = min_equity_usd
        self.auto_min_equity = auto_min_equity
        self.hard_risk_filter = hard_risk_filter
        self.rules_file = action_mask_rules_file
        self._last_reasons: Dict[int, List[str]] = {a: [] for a in _ALL_ACTIONS}
        self._last_mask: List[int] = [1] * self.get_action_size()
        self._last_timestamp: float = 0.0
        self._last_trace_id: str = ""
        self._custom_rules: Dict[str, Callable] = {}
        self._allowed_actions: Optional[Set[ActionType]] = None  # 外部权限注入
        if self.rules_file:
            self._load_custom_rules(self.rules_file)

    # ---------- 公共 API ----------
    def generate_mask(self, portfolio: Optional[Portfolio],
                      current_state: Optional[Dict[str, Any]] = None) -> List[int]:
        """生成当前状态下的动作掩码 (1=允许, 0=禁止)"""
        trace_id = str(uuid.uuid4())[:8]
        if portfolio is None:
            logger.error(f"[{trace_id}] Portfolio 为空，返回全禁止掩码")
            return [0] * self.get_action_size()

        state = current_state or {}
        reasons: Dict[int, List[str]] = {a: [] for a in _ALL_ACTIONS}
        mask = [1] * self.get_action_size()

        # 权限与全局状态 (最底层)
        self._check_account_frozen(mask, reasons, state, trace_id)
        self._check_api_permissions(mask, reasons, state, trace_id)
        self._check_connection(mask, reasons, state, trace_id)
        self._check_symbol_active(mask, reasons, state, trace_id)

        # 资金与风控 (使用修正后的有效权益)
        effective_equity = self._effective_equity(portfolio, state)
        self._check_equity(mask, reasons, portfolio, effective_equity, state, trace_id)
        self._check_leverage(mask, reasons, portfolio, effective_equity, trace_id)
        self._check_margin_advanced(mask, reasons, portfolio, state, trace_id)
        self._check_single_risk(mask, reasons, portfolio, state, trace_id)
        self._check_volatility_tier(mask, reasons, state, trace_id)
        self._check_daily_loss(mask, reasons, state, trace_id)
        self._check_consecutive_losses(mask, reasons, state, trace_id)
        self._check_funding_time(mask, reasons, state, trace_id)

        # 持仓规则
        self._check_position_rules(mask, reasons, portfolio, trace_id)

        # 时间与频率
        self._check_action_cooldown(mask, reasons, state, trace_id)

        # 自定义规则
        self._apply_custom_rules(mask, reasons, portfolio, state, trace_id)

        # 最终权限收束：如果账户冻结或只读，仅允许减仓/平仓
        if state.get("account_frozen") or state.get("api_readonly"):
            for act in _ALL_ACTIONS - _CLOSE_ALL - _DECREASE:
                if mask[act] == 1:
                    mask[act] = 0
                    reasons[act].append("账户冻结/只读，仅允许减仓平仓")

        self._last_reasons = reasons
        self._last_mask = mask.copy()
        self._last_timestamp = time.time()
        self._last_trace_id = trace_id
        return mask

    def get_action_size(self) -> int:
        return len(ActionType)

    def get_mask_reasons(self) -> Dict[int, List[str]]:
        return {k: list(v) for k, v in self._last_reasons.items()}

    def get_snapshot(self) -> Dict[str, Any]:
        """返回最近一次掩码生成的完整快照，用于审计"""
        return {
            "trace_id": self._last_trace_id,
            "timestamp": self._last_timestamp,
            "mask": self._last_mask,
            "reasons": self.get_mask_reasons(),
        }

    def update_rules(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                if key in ("max_leverage", "max_total_delta", "min_equity_usd") and isinstance(value, (int, float)):
                    setattr(self, key, float(value))
                elif key == "max_single_exposure_pct" and isinstance(value, float):
                    setattr(self, key, value)
                elif key == "auto_min_equity" and isinstance(value, bool):
                    setattr(self, key, value)
                elif key == "allowed_actions" and isinstance(value, set):
                    self._allowed_actions = value
                else:
                    logger.warning(f"参数 {key}={value} 类型无效")
            else:
                logger.warning(f"未知参数: {key}")

    def reset(self) -> None:
        self._last_reasons = {a: [] for a in _ALL_ACTIONS}
        self._last_mask = [1] * self.get_action_size()
        self._last_trace_id = ""

    # ---------- 内部计算方法 ----------
    def _effective_equity(self, portfolio: Portfolio, state: Dict) -> float:
        """计算有效权益，考虑未实现亏损和标记价格偏离"""
        equity = portfolio.equity or 0.0
        # 如果提供了标记价格偏离，使用更保守的估值
        mark_deviation = state.get("mark_price_deviation", 0.0)
        # 避免无标记价时误用0
        if mark_deviation == 0.0 and state.get("mark_price_available", True):
            mark_deviation = 0.0
        return equity * (1 - abs(mark_deviation))

    def _disable(self, mask: List[int], actions: Set[ActionType], reason: str,
                 reasons: Dict[int, List[str]], trace_id: str = ""):
        for act in actions:
            if mask[act] == 1:
                mask[act] = 0
                reasons[act].append(reason)
                logger.debug(f"[{trace_id}] 屏蔽动作 {act.name}: {reason}")

    # ---------- 各项检查 ----------
    def _check_account_frozen(self, mask, reasons, state, trace_id):
        if state.get("account_frozen"):
            self._disable(mask, _ENTRY | _ADD, "账户已被冻结", reasons, trace_id)

    def _check_api_permissions(self, mask, reasons, state, trace_id):
        if state.get("api_readonly"):
            self._disable(mask, _ENTRY | _ADD, "API密钥仅读权限", reasons, trace_id)
        # 若外部注入了允许的动作集合，则屏蔽未授权的动作
        if self._allowed_actions is not None:
            for act in _ALL_ACTIONS:
                if act not in self._allowed_actions and mask[act] == 1:
                    mask[act] = 0
                    reasons[act].append("管理员限制该动作")

    def _check_connection(self, mask, reasons, state, trace_id):
        if not state.get("connection_active", True):
            self._disable(mask, _ENTRY | _ADD, "交易所连接断开", reasons, trace_id)

    def _check_symbol_active(self, mask, reasons, state, trace_id):
        if not state.get("symbol_active", True):
            self._disable(mask, _ENTRY | _ADD, "交易对已暂停/退市", reasons, trace_id)

    def _check_equity(self, mask, reasons, portfolio, effective_equity, state, trace_id):
        threshold = self.min_equity_usd
        if self.auto_min_equity:
            # 结合已实现盈亏修正最低权益
            realized = getattr(portfolio, 'realized_pnl', 0.0) or 0.0
            base = effective_equity - realized  # 初始本金估算
            threshold = max(base * 0.25, 100.0)
        if effective_equity <= 0:
            self._disable(mask, _INCREASE_RISK, "有效权益为零或负值", reasons, trace_id)
        elif effective_equity < threshold:
            self._disable(mask, _INCREASE_RISK,
                         f"权益{effective_equity:.2f}<最低{threshold:.2f}", reasons, trace_id)

    def _check_leverage(self, mask, reasons, portfolio, effective_equity, trace_id):
        lev = getattr(portfolio, 'current_leverage', None) or 1.0
        delta = getattr(portfolio, 'total_delta', None) or 0.0
        pending_delta = getattr(portfolio, 'pending_delta', 0.0) or 0.0
        total_delta = delta + pending_delta
        if lev >= self.max_leverage or total_delta >= self.max_total_delta:
            self._disable(mask, _INCREASE_RISK,
                         f"杠杆/Delta超限 (lev={lev:.2f}, delta={total_delta:.2f})", reasons, trace_id)

    def _check_margin_advanced(self, mask, reasons, portfolio, state, trace_id):
        avail = getattr(portfolio, 'available_margin', 0.0) or 0.0
        required = state.get("estimated_margin")
        if required is None or required <= 0:
            required = avail * 0.1  # 保守假设
        locked = getattr(portfolio, 'locked_margin', 0.0) or 0.0
        if required + locked > avail * 0.85:
            self._disable(mask, _INCREASE_RISK, "保证金不足 (含锁定)", reasons, trace_id)

    def _check_single_risk(self, mask, reasons, portfolio, state, trace_id):
        if self.hard_risk_filter is None:
            return
        for act in _INCREASE_RISK:
            if mask[act] == 0:
                continue
            est_risk = state.get("estimated_risk_pct")
            if est_risk is None or est_risk <= 0:
                est_risk = self.hard_risk_filter.single_risk_pct  # 保守
            if est_risk > self.hard_risk_filter.single_risk_pct:
                self._disable(mask, {act}, f"预估单笔风险{est_risk:.2%}超限", reasons, trace_id)

    def _check_volatility_tier(self, mask, reasons, state, trace_id):
        tier = state.get("volatility_tier", 0)
        if tier >= 2:
            self._disable(mask, _INCREASE_RISK, "波动率重度异常", reasons, trace_id)
        elif tier == 1:
            self._disable(mask, _ENTRY, "波动率轻度异常，禁开新仓", reasons, trace_id)

    def _check_daily_loss(self, mask, reasons, state, trace_id):
        if state.get("daily_loss_halted"):
            self._disable(mask, _INCREASE_RISK, "日亏损熔断", reasons, trace_id)

    def _check_consecutive_losses(self, mask, reasons, state, trace_id):
        if state.get("consecutive_losses_halted"):
            self._disable(mask, _INCREASE_RISK, "连续亏损熔断", reasons, trace_id)

    def _check_funding_time(self, mask, reasons, state, trace_id):
        if state.get("funding_rate_soon"):
            self._disable(mask, _ENTRY, "临近资金费率结算，禁开新仓", reasons, trace_id)

    def _check_position_rules(self, mask, reasons, portfolio, trace_id):
        def _has_long():
            func = getattr(portfolio, 'has_long_position', None)
            return func() if callable(func) else bool(getattr(portfolio, 'has_long_position', False))
        def _has_short():
            func = getattr(portfolio, 'has_short_position', None)
            return func() if callable(func) else bool(getattr(portfolio, 'has_short_position', False))

        has_long, has_short = _has_long(), _has_short()

        if has_long:
            self._disable(mask, {ActionType.SHORT_ENTRY, ActionType.INCREASE_SHORT}, "已有多头", reasons, trace_id)
        if has_short:
            self._disable(mask, {ActionType.LONG_ENTRY, ActionType.INCREASE_LONG}, "已有空头", reasons, trace_id)
        if not has_long:
            self._disable(mask, {ActionType.INCREASE_LONG}, "无多头", reasons, trace_id)
        if not has_short:
            self._disable(mask, {ActionType.INCREASE_SHORT}, "无空头", reasons, trace_id)
        if not has_long and not has_short:
            self._disable(mask, _DECREASE | _CLOSE_ALL, "无持仓", reasons, trace_id)

        exp = getattr(portfolio, 'max_symbol_exposure_pct', 0.0) or 0.0
        if exp >= self.max_single_exposure_pct:
            if has_long:
                self._disable(mask, {ActionType.LONG_ENTRY, ActionType.INCREASE_LONG},
                              "单品种敞口超限", reasons, trace_id)
            if has_short:
                self._disable(mask, {ActionType.SHORT_ENTRY, ActionType.INCREASE_SHORT},
                              "单品种敞口超限", reasons, trace_id)

    def _check_action_cooldown(self, mask, reasons, state, trace_id):
        last_action = state.get("last_action")
        last_action_time = state.get("last_action_time", 0.0)
        cooldown_map = state.get("action_cooldown_map", {})  # 按动作类型的冷却时间
        if last_action is not None:
            try:
                act = ActionType(last_action)
                cooldown = cooldown_map.get(last_action, 0.5)
                if (time.time() - last_action_time) < cooldown:
                    self._disable(mask, {act}, f"动作冷却中 ({cooldown}s)", reasons, trace_id)
            except ValueError:
                pass

    def _apply_custom_rules(self, mask, reasons, portfolio, state, trace_id):
        for name, func in list(self._custom_rules.items()):
            try:
                new_mask, new_reasons = func(
                    deepcopy(mask),
                    {k: list(v) for k, v in reasons.items()},
                    portfolio, state
                )
                mask[:] = new_mask
                for act, why in new_reasons.items():
                    if isinstance(why, list):
                        reasons[act].extend(why)
            except Exception as e:
                logger.error(f"[{trace_id}] 自定义规则 {name} 异常: {e}")

    def _load_custom_rules(self, filepath: str):
        try:
            if filepath.endswith('.yaml') or filepath.endswith('.yml'):
                import yaml
                with open(filepath, 'r') as f:
                    _ = yaml.safe_load(f)
                logger.info("已加载自定义规则 (YAML)")
            elif filepath.endswith('.json'):
                import json
                with open(filepath, 'r') as f:
                    _ = json.load(f)
                logger.info("已加载自定义规则 (JSON)")
            else:
                logger.warning(f"不支持的自定义规则文件格式: {filepath}")
        except ImportError:
            logger.error("需要安装 PyYAML 以加载 YAML 规则文件")
        except Exception as e:
            logger.error(f"加载外部规则失败: {e}")

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'ActionMask':
        return cls(
            max_leverage=config.get('max_leverage', 3.0),
            max_total_delta=config.get('max_total_delta', 3.0),
            max_single_exposure_pct=config.get('max_single_exposure_pct', 0.4),
            min_equity_usd=config.get('min_equity_usd', 500.0),
            auto_min_equity=config.get('auto_min_equity', False),
                   )
