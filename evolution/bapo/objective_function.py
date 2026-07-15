# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import annotations

"""
模块名称: objective_function.py
核心职责: 定义贝叶斯优化的目标函数，将策略参数映射为单一绩效分数。
          支持多目标、动态约束、小账户模式、风险厌恶、并行评估。
所属层级: evolution.bapo
版本: 8.0
作者: KHAOS Evolution Team
创建日期: 2025-09-12
最后审计: 2026-07-15 (第六轮穿透)
审计者: KHAOS Audit AI
"""

import logging
import time
import math
import hashlib
import warnings
from copy import deepcopy, copy
from dataclasses import dataclass, field
from enum import Enum
from math import isnan, isinf, isfinite
from typing import (Any, Callable, Dict, Final, List, Optional, Tuple, Union)
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
import os

from evolution.bapo.replay_engine import ShadowReplayEngine, PerformanceMetrics

__version__: Final[str] = "8.0"
__all__: Final[List[str]] = ['ObjectiveFunction', 'Objective', 'EvaluationDetail']
logger = logging.getLogger(__name__)
logger.propagate = True

# 数值常量
_INF_SCORE: Final[float] = float('-inf')
_UPPER_SCORE: Final[float] = 1e12

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class Objective(Enum):
    SHARPE = "sharpe"
    CALMAR = "calmar"
    SORTINO = "sortino"

    @classmethod
    def from_str(cls, name: Union[str, 'Objective']) -> 'Objective':
        if isinstance(name, cls):
            return name
        if not name or not isinstance(name, str):
            raise ValueError("优化目标不能为空")
        name = name.strip().lower()
        for obj in cls:
            if obj.value == name:
                return obj
        raise ValueError(f"不支持的优化目标: {name}，可选: {[e.value for e in cls]}")


class UtilityFunction(Enum):
    LINEAR = "linear"
    LOG = "log"
    SQRT = "sqrt"
    POWER = "power"

    @classmethod
    def from_str(cls, name: Union[str, 'UtilityFunction', None]) -> 'UtilityFunction':
        if isinstance(name, cls):
            return name
        if not name:
            return cls.LINEAR
        name = name.strip().lower()
        for uf in cls:
            if uf.value == name:
                return uf
        raise ValueError(f"不支持的效用函数: {name}")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class Constraint:
    __slots__ = ('max_dd_limit', 'min_sharpe', 'min_trades', 'cvar_limit', 'cvar_attr')
    max_dd_limit: float = 0.15
    min_sharpe: float = 0.0
    min_trades: int = 0
    cvar_limit: Optional[float] = None
    cvar_attr: str = 'cvar_95'   # 指标对象中CVaR属性名

    def validate(self) -> bool:
        if not (0.0 <= self.max_dd_limit <= 1.0):
            raise ValueError(f"max_dd_limit 必须在 [0,1]，当前: {self.max_dd_limit}")
        if self.min_sharpe < -10:
            warnings.warn(f"min_sharpe 过低: {self.min_sharpe}")
        if self.min_trades < 0:
            raise ValueError(f"min_trades 不能为负: {self.min_trades}")
        if self.cvar_limit is not None and not (0.0 <= self.cvar_limit <= 1.0):
            raise ValueError(f"cvar_limit 必须在 [0,1]，当前: {self.cvar_limit}")
        return True

    def __repr__(self) -> str:
        cvar = f" cvar({self.cvar_attr})={self.cvar_limit:.2%}" if self.cvar_limit else ""
        return (f"Constraint(max_dd={self.max_dd_limit:.2%}, "
                f"min_sharpe={self.min_sharpe:.2f}, min_trades={self.min_trades}{cvar})")


@dataclass
class EvaluationDetail:
    __slots__ = ('score', 'passed', 'fail_reason', 'metrics', 'elapsed_sec', 'param_hash')
    score: float = _INF_SCORE
    passed: bool = False
    fail_reason: str = ""
    metrics: Optional[PerformanceMetrics] = None
    elapsed_sec: float = 0.0
    param_hash: str = ""

    def __str__(self) -> str:
        return (f"Detail(passed={self.passed}, score={self.score:.4f}, "
                f"reason='{self.fail_reason[:50]}', time={self.elapsed_sec:.3f}s)")


@dataclass
class ObjectiveStatistics:
    __slots__ = ('total_evaluations', 'successful_evaluations', 'failed_evaluations',
                 'best_score', 'total_elapsed_sec', 'last_evaluation_time',
                 'fail_reasons')
    total_evaluations: int = 0
    successful_evaluations: int = 0
    failed_evaluations: int = 0
    best_score: float = _INF_SCORE
    total_elapsed_sec: float = 0.0
    last_evaluation_time: float = 0.0
    fail_reasons: Dict[str, int] = field(default_factory=dict)

    def record_fail(self, reason: str) -> None:
        if reason and len(self.fail_reasons) < 1000:  # 防止无限增长
            self.fail_reasons[reason] = self.fail_reasons.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------
class ObjectiveFunction:
    """
    目标函数封装，支持多目标、动态约束、小账户模式、风险厌恶、并行评估。
    线程安全的并发评估需确保 replay_engine 实例支持并发或提供 __copy__ 方法。
    """

    __slots__ = ('objective', 'constraint', '_original_constraint', 'bounds', 'statistics',
                 '_observers', 'small_account_mode', 'account_balance',
                 'risk_aversion_coeff', 'utility_function', 'enable_parallel',
                 'max_workers', '_logger')

    def __init__(self,
                 objective: Union[str, Objective] = Objective.CALMAR,
                 max_dd_limit: float = 0.15,
                 min_sharpe: float = 0.0,
                 min_trades: int = 0,
                 cvar_limit: Optional[float] = None,
                 cvar_attr: str = 'cvar_95',
                 bounds: Optional[Dict[str, Tuple[float, float]]] = None,
                 small_account_mode: bool = False,
                 account_balance: float = 0.0,
                 risk_aversion_coeff: float = 0.0,
                 utility_function: Union[str, UtilityFunction] = UtilityFunction.LINEAR,
                 enable_parallel: bool = False,
                 max_workers: int = 4):
        # 目标解析
        self.objective: Objective = Objective.from_str(objective)

        # 约束
        self.constraint = Constraint(max_dd_limit, min_sharpe, min_trades, cvar_limit, cvar_attr)
        self.constraint.validate()
        self._original_constraint = deepcopy(self.constraint)  # 保存原始约束

        # 边界
        self.bounds: Optional[Dict[str, Tuple[float, float]]] = None
        if bounds is not None:
            sanitized = {}
            for k, v in bounds.items():
                k_clean = k.strip().lower()
                if not k_clean:
                    continue
                if not (isinstance(v, (list, tuple)) and len(v) == 2 and v[0] < v[1]):
                    raise ValueError(f"参数边界非法: {k} -> {v}")
                sanitized[k_clean] = tuple(v)
            self.bounds = sanitized

        self.statistics = ObjectiveStatistics()
        self._observers: List[Callable[[EvaluationDetail], None]] = []

        # 小账户 & 风险
        self.small_account_mode = small_account_mode
        self.account_balance = account_balance
        self.risk_aversion_coeff = risk_aversion_coeff
        if isinstance(utility_function, str):
            self.utility_function = UtilityFunction.from_str(utility_function)
        else:
            self.utility_function = utility_function

        # 并行
        self.enable_parallel = enable_parallel
        self.max_workers = max(1, max_workers)

        self._logger = logging.getLogger(f"{__name__}.{id(self)}")
        self._logger.info("目标函数初始化: objective=%s, %s, small_account=%s, balance=%.0f, parallel=%s",
                         self.objective.value, self.constraint, self.small_account_mode,
                         self.account_balance, self.enable_parallel)

    def __del__(self):
        self._observers.clear()

    # --------------------------------------------------------------------------
    # 配置管理
    # --------------------------------------------------------------------------
    def set_objective(self, objective: Union[str, Objective]) -> 'ObjectiveFunction':
        old = self.objective
        self.objective = Objective.from_str(objective)
        self._logger.info("优化目标从 %s 切换为 %s", old.value, self.objective.value)
        return self

    def get_objective(self) -> Objective:
        return self.objective

    def set_constraints(self, max_dd_limit: Optional[float] = None,
                        min_sharpe: Optional[float] = None,
                        min_trades: Optional[int] = None,
                        cvar_limit: Optional[float] = None,
                        cvar_attr: Optional[str] = None) -> Constraint:
        """更新约束，返回旧约束的副本。"""
        old = deepcopy(self.constraint)
        if max_dd_limit is not None:
            self.constraint.max_dd_limit = max_dd_limit
        if min_sharpe is not None:
            self.constraint.min_sharpe = min_sharpe
        if min_trades is not None:
            self.constraint.min_trades = min_trades
        if cvar_limit is not None:
            self.constraint.cvar_limit = cvar_limit
        if cvar_attr is not None:
            self.constraint.cvar_attr = cvar_attr
        self.constraint.validate()
        self._original_constraint = deepcopy(self.constraint)
        self._logger.info("约束更新: %s", self.constraint)
        return old

    def get_constraints(self) -> Constraint:
        return deepcopy(self.constraint)

    @property
    def constraints_summary(self) -> str:
        return str(self.constraint)

    def update_bounds(self, new_bounds: Dict[str, Tuple[float, float]], override: bool = True) -> None:
        if self.bounds is None:
            self.bounds = {}
        for k, v in new_bounds.items():
            k_clean = k.strip().lower()
            if not k_clean:
                continue
            if not (isinstance(v, (list, tuple)) and len(v) == 2 and v[0] < v[1]):
                raise ValueError(f"参数边界非法: {k} -> {v}")
            if override or k_clean not in self.bounds:
                self.bounds[k_clean] = tuple(v)

    def add_bound(self, param: str, low: float, high: float) -> None:
        self.update_bounds({param: (low, high)})

    def remove_bound(self, param_name: str) -> Optional[Tuple[float, float]]:
        if self.bounds:
            return self.bounds.pop(param_name.strip().lower(), None)
        return None

    def get_bounds(self) -> Optional[Dict[str, Tuple[float, float]]]:
        return deepcopy(self.bounds) if self.bounds else None

    # 小账户模式
    def set_account_balance(self, balance: float) -> None:
        old = self.account_balance
        self.account_balance = balance
        self._logger.info("账户余额 %.2f -> %.2f", old, balance)
        # 如果小账户模式已启用，重新评估约束
        if self.small_account_mode:
            self._adjust_constraints_for_balance()

    def enable_small_account_mode(self, enabled: bool = True) -> None:
        self.small_account_mode = enabled
        self._logger.info("小账户模式: %s", "开启" if enabled else "关闭")
        if enabled and self.account_balance > 0:
            self._adjust_constraints_for_balance()
        elif not enabled:
            # 恢复原始约束
            self.constraint = deepcopy(self._original_constraint)

    def _adjust_constraints_for_balance(self) -> None:
        """根据账户余额动态调整约束（在有效约束中处理）。"""
        if self.account_balance < 2000:
            self.constraint.min_trades = max(0, self._original_constraint.min_trades - 2)
            self.constraint.max_dd_limit = min(0.5, self._original_constraint.max_dd_limit + 0.05)
            self._logger.info("小账户约束自动调整: %s", self.constraint)
        else:
            # 恢复到原始约束
            self.constraint = deepcopy(self._original_constraint)

    def set_risk_aversion(self, coeff: float) -> None:
        if coeff < 0:
            raise ValueError("风险厌恶系数不能为负")
        self.risk_aversion_coeff = coeff

    def set_utility_function(self, uf: Union[str, UtilityFunction]) -> None:
        if isinstance(uf, str):
            uf = UtilityFunction.from_str(uf)
        self.utility_function = uf

    def set_parallel(self, enable: bool, max_workers: Optional[int] = None) -> None:
        self.enable_parallel = enable
        if max_workers is not None:
            self.max_workers = max(1, max_workers)

    # 观察者
    def add_observer(self, callback: Callable[[EvaluationDetail], None]) -> None:
        if len(self._observers) < 10:
            self._observers.append(callback)
        else:
            self._logger.warning("观察者数量已达上限")

    def remove_observer(self, callback: Callable[[EvaluationDetail], None]) -> None:
        try:
            self._observers.remove(callback)
        except ValueError:
            pass

    def clear_observers(self) -> None:
        self._observers.clear()

    # --------------------------------------------------------------------------
    # 评估接口
    # --------------------------------------------------------------------------
    def __call__(self, params: Dict[str, float],
                 replay_engine: Optional[ShadowReplayEngine] = None) -> float:
        return self.evaluate(params, replay_engine)

    def evaluate(self, params: Dict[str, float],
                 replay_engine: Optional[ShadowReplayEngine] = None) -> float:
        try:
            detail = self.evaluate_detailed(params, replay_engine)
            self._notify_observers(detail)
            return detail.score
        except Exception as e:
            self._logger.exception("evaluate 发生未预期异常")
            # 仍然通知观察者一个失败的详情
            fail_detail = EvaluationDetail(fail_reason=f"未预期异常: {e}")
            self.statistics.failed_evaluations += 1
            self._notify_observers(fail_detail)
            return _INF_SCORE

    def evaluate_detailed(self, params: Dict[str, float],
                          replay_engine: Optional[ShadowReplayEngine] = None) -> EvaluationDetail:
        detail = EvaluationDetail()
        start_time = time.perf_counter()
        self.statistics.total_evaluations += 1

        # 1. 引擎检查
        if not self._check_engine(replay_engine, detail):
            return self._finish_evaluation(detail, start_time)

        # 2. 参数清理与校验
        params_clean = self._sanitize_params(params)
        if not self.is_param_valid(params_clean, detail):
            return self._finish_evaluation(detail, start_time)

        # 3. 回放
        metrics = self._run_replay(replay_engine, params_clean, detail)
        if metrics is None:
            return self._finish_evaluation(detail, start_time)

        # 4. 构建有效约束（考虑小账户）
        effective_constraint = self._build_effective_constraint()

        # 5. 指标验证
        if not self._validate_metrics(metrics, effective_constraint, detail):
            return self._finish_evaluation(detail, start_time)

        # 6. 硬约束检查
        if not self._check_constraints(metrics, effective_constraint, detail):
            return self._finish_evaluation(detail, start_time)

        # 7. 计算分数
        score = self._compute_score(metrics)
        score = self._apply_risk_utility(score, metrics)
        score = self._clamp_score(score)

        detail.score = score
        detail.passed = True
        detail.metrics = metrics
        detail.param_hash = hashlib.sha256(
            str(sorted(params_clean.items())).encode()
        ).hexdigest()[:16]

        self.statistics.successful_evaluations += 1
        if score > self.statistics.best_score:
            self.statistics.best_score = score

        return self._finish_evaluation(detail, start_time)

    def evaluate_batch(self, param_list: List[Dict[str, float]],
                       replay_engine: Optional[ShadowReplayEngine] = None,
                       progress_callback: Optional[Callable[[int, int], None]] = None) -> List[float]:
        if not param_list:
            return []
        total = len(param_list)
        if self.enable_parallel and total > 1 and self._can_parallelize(replay_engine):
            return self._parallel_evaluate(param_list, replay_engine, progress_callback)
        results = []
        for i, params in enumerate(param_list):
            results.append(self.evaluate(params, replay_engine))
            if progress_callback:
                progress_callback(i + 1, total)
        return results

    # --------------------------------------------------------------------------
    # 参数验证
    # --------------------------------------------------------------------------
    def validate_params(self, params: Dict[str, float]) -> Tuple[bool, str]:
        if not params:
            return False, "参数字典为空"
        for key, val in params.items():
            key_clean = key.strip().lower()
            if not key_clean:
                return False, "参数键为空"
            if val is None:
                return False, f"参数 '{key_clean}' 值为 None"
            if not isinstance(val, (int, float)) or type(val) is bool:
                return False, f"参数 '{key_clean}' 类型非法"
            if isnan(val) or isinf(val):
                return False, f"参数 '{key_clean}' 值为 {'NaN' if isnan(val) else 'Inf'}"
            if self.bounds:
                low_high = self.bounds.get(key_clean, self.bounds.get(key, None))
                if low_high is not None:
                    low, high = low_high
                    if val < low or val > high:
                        return False, f"参数 '{key_clean}' 值 {val} 超出范围 [{low}, {high}]"
        return True, ""

    def is_param_valid(self, params: Dict[str, float],
                       detail: Optional[EvaluationDetail] = None) -> bool:
        valid, reason = self.validate_params(params)
        if not valid and detail:
            detail.fail_reason = reason
        return valid

    # --------------------------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------------------------
    def _build_effective_constraint(self) -> Constraint:
        """考虑小账户模式下的动态约束"""
        c = deepcopy(self.constraint)
        if self.small_account_mode and self.account_balance < 2000:
            c.min_trades = max(0, c.min_trades - 2)
            c.max_dd_limit = min(0.5, c.max_dd_limit + 0.05)
        return c

    def _check_engine(self, engine, detail: EvaluationDetail) -> bool:
        if engine is None:
            detail.fail_reason = "回放引擎为 None"
            return False
        if not callable(getattr(engine, 'run', None)):
            detail.fail_reason = "引擎无 run 方法"
            return False
        return True

    def _can_parallelize(self, engine) -> bool:
        """检查引擎是否支持并行（线程安全或可拷贝）"""
        if engine is None:
            return False
        return getattr(engine, 'is_thread_safe', False) or hasattr(engine, '__copy__')

    def _sanitize_params(self, params: Dict[str, float]) -> Dict[str, float]:
        clean = {}
        warned_keys = set()
        for k, v in params.items():
            k_clean = k.strip().lower()
            if not k_clean:
                continue
            if k_clean in clean:
                self._logger.warning("重复参数键 %s，使用最后一个值", k_clean)
            if isinstance(v, (int, float)) and type(v) is not bool:
                if not (isnan(v) or isinf(v)):
                    clean[k_clean] = v
                else:
                    if k_clean not in warned_keys:
                        self._logger.warning("丢弃非法数值 %s=%s", k_clean, v)
                        warned_keys.add(k_clean)
            else:
                if k_clean not in warned_keys:
                    self._logger.warning("丢弃非数值参数 %s=%s", k_clean, v)
                    warned_keys.add(k_clean)
        return clean

    def _run_replay(self, engine, params: Dict[str, float],
                    detail: EvaluationDetail) -> Optional[PerformanceMetrics]:
        try:
            return engine.run(params)
        except (ValueError, RuntimeError, IOError) as e:
            detail.fail_reason = f"回放异常: {e}"
        except Exception as e:
            detail.fail_reason = f"未预期异常: {e}"
            self._logger.exception("回放异常详情")
        return None

    def _validate_metrics(self, metrics: PerformanceMetrics,
                          constraint: Constraint,
                          detail: EvaluationDetail) -> bool:
        required = ['sharpe_ratio', 'max_drawdown', 'annualized_return', 'total_trades']
        try:
            for attr in required:
                if not hasattr(metrics, attr):
                    detail.fail_reason = f"缺少属性 {attr}"
                    return False
                val = getattr(metrics, attr)
                if val is None:
                    detail.fail_reason = f"{attr} 为 None"
                    return False
                if isinstance(val, (int, float)):
                    if isnan(val) or isinf(val):
                        detail.fail_reason = f"{attr} 非法"
                        return False
            if not (0.0 <= metrics.max_drawdown <= 1.0):
                detail.fail_reason = f"max_drawdown 异常: {metrics.max_drawdown}"
                return False
            total_trades = int(metrics.total_trades)  # 确保整数
            if total_trades < 0:
                detail.fail_reason = "total_trades 为负"
                return False
            # 依据有效约束判断交易次数
            if total_trades == 0 and constraint.min_trades > 0:
                detail.fail_reason = "无交易记录"
                return False
            if abs(metrics.annualized_return) > 500:  # 放宽限制
                detail.fail_reason = f"annualized_return 异常: {metrics.annualized_return}"
                return False
            return True
        except Exception as e:
            detail.fail_reason = f"检查指标异常: {e}"
            return False

    def _check_constraints(self, metrics: PerformanceMetrics,
                           constraint: Constraint,
                           detail: EvaluationDetail) -> bool:
        if metrics.max_drawdown >= constraint.max_dd_limit:
            detail.fail_reason = f"回撤超标: {metrics.max_drawdown:.2%} >= {constraint.max_dd_limit:.2%}"
            return False
        if metrics.sharpe_ratio < constraint.min_sharpe:
            detail.fail_reason = f"夏普过低: {metrics.sharpe_ratio:.2f} < {constraint.min_sharpe}"
            return False
        total_trades = int(metrics.total_trades)
        if constraint.min_trades > 0 and total_trades < constraint.min_trades:
            detail.fail_reason = f"交易次数不足: {total_trades} < {constraint.min_trades}"
            return False
        if constraint.cvar_limit is not None:
            cvar = getattr(metrics, constraint.cvar_attr, None)
            if cvar is not None and isinstance(cvar, (int, float)) and not isnan(cvar):
                if cvar > constraint.cvar_limit:
                    detail.fail_reason = f"CVaR超标: {cvar:.2%} > {constraint.cvar_limit:.2%}"
                    return False
        return True

    def _compute_score(self, metrics: PerformanceMetrics) -> float:
        obj = self.objective
        if obj == Objective.SHARPE:
            return max(-100.0, min(metrics.sharpe_ratio, 100.0))
        elif obj == Objective.CALMAR:
            ret = metrics.annualized_return
            dd = metrics.max_drawdown
            if dd > 1e-12:
                return ret / dd
            elif ret > 0:
                return min(ret * 100, _UPPER_SCORE)
            else:
                return _INF_SCORE
        elif obj == Objective.SORTINO:
            sortino = getattr(metrics, 'sortino_ratio', None)
            if sortino is not None and isfinite(sortino):
                return sortino
            self._logger.debug("sortino_ratio 不可用，回退到 sharpe_ratio")
            return max(-100.0, min(metrics.sharpe_ratio, 100.0))
        return metrics.sharpe_ratio

    def _apply_risk_utility(self, score: float, metrics: PerformanceMetrics) -> float:
        if self.risk_aversion_coeff > 0:
            score -= self.risk_aversion_coeff * metrics.max_drawdown
        uf = self.utility_function
        if uf == UtilityFunction.LOG:
            if score > 0:
                score = math.log(1 + score)
            else:
                score = _INF_SCORE
        elif uf == UtilityFunction.SQRT:
            if score >= 0:
                score = math.sqrt(score)
            else:
                score = _INF_SCORE
        elif uf == UtilityFunction.POWER:
            if score >= 0:
                score = score ** 2
        return score

    @staticmethod
    def _clamp_score(score: float) -> float:
        if not isfinite(score) or isnan(score):
            return _INF_SCORE
        if score > _UPPER_SCORE:
            return _UPPER_SCORE
        return score

    def _finish_evaluation(self, detail: EvaluationDetail, start_time: float) -> EvaluationDetail:
        detail.elapsed_sec = time.perf_counter() - start_time
        if not detail.passed:
            self.statistics.failed_evaluations += 1
            if detail.fail_reason:
                self.statistics.record_fail(detail.fail_reason)
        self.statistics.total_elapsed_sec += detail.elapsed_sec
        self.statistics.last_evaluation_time = detail.elapsed_sec
        return detail

    # --------------------------------------------------------------------------
    # 并行支持
    # --------------------------------------------------------------------------
    def _parallel_evaluate(self, param_list: List[Dict[str, float]],
                           engine: ShadowReplayEngine,
                           progress_callback: Optional[Callable[[int, int], None]] = None) -> List[float]:
        total = len(param_list)
        results = [_INF_SCORE] * total
        workers = min(self.max_workers, total, (os.cpu_count() or 4))
        # 分块提交，避免内存压力
        chunk_size = max(1, total // workers)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {}
            for i, params in enumerate(param_list):
                engine_copy = copy(engine) if hasattr(engine, '__copy__') else engine
                future = executor.submit(self.evaluate, params, engine_copy)
                future_to_idx[future] = i
            completed = 0
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except CancelledError:
                    self._logger.warning("并行评估任务被取消")
                except Exception as e:
                    self._logger.error("并行评估失败: %s", e)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)
        return results

    def _notify_observers(self, detail: EvaluationDetail) -> None:
        for obs in self._observers[:]:
            try:
                obs(detail)
            except Exception:
                self._logger.exception("观察者回调异常")

    # --------------------------------------------------------------------------
    # 统计
    # --------------------------------------------------------------------------
    def reset_statistics(self, keep_best: bool = False) -> None:
        old_best = self.statistics.best_score if keep_best else _INF_SCORE
        old_fails = self.statistics.fail_reasons if keep_best else {}
        self.statistics = ObjectiveStatistics()
        self.statistics.best_score = old_best
        self.statistics.fail_reasons = old_fails

    def get_statistics(self) -> ObjectiveStatistics:
        return deepcopy(self.statistics)

    def get_best_score(self) -> float:
        return self.statistics.best_score

    # --------------------------------------------------------------------------
    # 序列化
    # --------------------------------------------------------------------------
    def to_dict(self, include_statistics: bool = False) -> Dict[str, Any]:
        bounds_dict = {}
        if self.bounds:
            for k, v in self.bounds.items():
                bounds_dict[k] = list(v)
        data = {
            'objective': self.objective.value,
            'max_dd_limit': self.constraint.max_dd_limit,
            'min_sharpe': self.constraint.min_sharpe,
            'min_trades': self.constraint.min_trades,
            'cvar_limit': self.constraint.cvar_limit,
            'cvar_attr': self.constraint.cvar_attr,
            'bounds': bounds_dict,
            'small_account_mode': self.small_account_mode,
            'account_balance': self.account_balance,
            'risk_aversion_coeff': self.risk_aversion_coeff,
            'utility_function': self.utility_function.value,
            'enable_parallel': self.enable_parallel,
            'max_workers': self.max_workers,
        }
        if include_statistics:
            data['statistics'] = {
                'total_evaluations': self.statistics.total_evaluations,
                'best_score': self.statistics.best_score,
            }
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ObjectiveFunction':
        bounds = None
        bounds_raw = data.get('bounds')
        if bounds_raw:
            bounds = {k: tuple(v) for k, v in bounds_raw.items()}
        try:
            uf = UtilityFunction.from_str(data.get('utility_function', 'linear'))
        except ValueError:
            uf = UtilityFunction.LINEAR
        return cls(
            objective=data.get('objective', 'calmar'),
            max_dd_limit=data.get('max_dd_limit', 0.15),
            min_sharpe=data.get('min_sharpe', 0.0),
            min_trades=data.get('min_trades', 0),
            cvar_limit=data.get('cvar_limit'),
            cvar_attr=data.get('cvar_attr', 'cvar_95'),
            bounds=bounds,
            small_account_mode=data.get('small_account_mode', False),
            account_balance=data.get('account_balance', 0.0),
            risk_aversion_coeff=data.get('risk_aversion_coeff', 0.0),
            utility_function=uf,
            enable_parallel=data.get('enable_parallel', False),
            max_workers=data.get('max_workers', 4),
        )

    def __repr__(self) -> str:
        return (f"ObjectiveFunction(obj={self.objective.value}, {self.constraint}, "
                f"small={self.small_account_mode}, parallel={self.enable_parallel})")

    def __str__(self) -> str:
        return self.__repr__()

    def __eq__(self, other) -> bool:
        if not isinstance(other, ObjectiveFunction):
            return False
        return (self.objective == other.objective and
                self.constraint == other.constraint and
                self.bounds == other.bounds and
                self.small_account_mode == other.small_account_mode and
                self.account_balance == other.account_balance and
                self.risk_aversion_coeff == other.risk_aversion_coeff and
                self.utility_function == other.utility_function)
