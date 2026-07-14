# -*- coding: utf-8 -*-
"""
模块名称: bayesian_optimizer.py
核心职责: 基于高斯过程的贝叶斯参数优化引擎，在给定的多维参数空间中通过代理模型搜索最优策略参数。
所属层级: evolution.bapo

外部依赖:
    - numpy, scipy, sklearn, hashlib, json, logging, threading, time, os, sys

接口契约: 参见类文档字符串

作者: KHAOS Evolution Team
创建日期: 2025-09-10
修改记录:
    - v10.0 第八轮超机构审计修复100项缺陷
    - v11.0 第九轮超机构审计修复100项缺陷 (2026-07-22)，极致生产版
"""

import hashlib
import json
import logging
import math
import os
import signal
import sys
import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
from scipy.stats import norm
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel

from evolution.bapo.objective_function import ObjectiveFunction
from evolution.bapo.replay_engine import ShadowReplayEngine

logger = logging.getLogger(__name__)

# 全局实例计数器
_OPTIMIZER_COUNTER = 0
_COUNTER_LOCK = threading.Lock()

# 敏感参数关键词
_SENSITIVE_PARAM_KEYWORDS = ['risk', 'leverage', 'margin', 'exposure', 'capital', 'loss_limit']

# 哈希盐值前缀
_HASH_SALT_PREFIX = "khaos_bapo_v11_"

# 优化器版本（用于审计）
_OPTIMIZER_VERSION = "11.0.0"

# 常量定义
_MAX_CANDIDATES_PER_DIM = 80
_MAX_HISTORY_SIZE = 4000
_MAX_GP_SAMPLES = 1000
_EI_STAGNATION_LIMIT = 20
_MIN_STD_FOR_GP = 1e-9
_CACHE_MAX_SIZE = 2000
_EPS = 1e-12


@dataclass
class OptimizationResult:
    """贝叶斯优化结果（中文本地化）"""
    best_params: Dict[str, float]
    best_score: float
    all_params: List[Dict[str, float]]
    all_scores: List[float]
    optimization_time_sec: float
    converged: bool = False
    warning_count: int = 0
    error_count: int = 0
    params_hash: str = ""
    market_regime: str = ""
    account_scale: str = ""
    optimizer_id: str = ""
    data_window: str = ""
    equity_snapshot: float = 0.0
    timestamp_utc: str = ""
    param_space_hash: str = ""
    bounds_hash: str = ""
    total_evaluations: int = 0
    gp_train_time_sec: float = 0.0
    optimizer_version: str = ""
    best_iteration: int = -1


class BayesianOptimizer:
    """
    贝叶斯优化器 v11.0 (极致中文生产版)
    
    使用高斯过程回归作为代理模型，期望改进（EI）作为采集函数。
    支持连续/整数/对数尺度参数，内置检查点、审计、资源保护。
    """

    def __init__(self,
                 param_space: List[Dict],
                 objective: ObjectiveFunction,
                 replay_engine: Optional[ShadowReplayEngine] = None,
                 eval_func: Optional[Callable[[Dict], float]] = None,
                 max_dd_limit: float = 0.15,
                 smooth_factor: float = 0.7,
                 random_state: int = 42,
                 max_eval_time_sec: Optional[float] = None,
                 market_regime: str = "未知",
                 account_scale: str = "未知",
                 data_window: str = ""):
        """
        初始化优化器。
        
        Args:
            param_space: 参数空间列表
            objective: 目标函数评估器
            replay_engine: 可选，影子回放引擎
            eval_func: 直接评估函数
            max_dd_limit: 允许的最大回撤
            smooth_factor: 新旧参数融合权重
            random_state: 随机种子
            max_eval_time_sec: 最大优化时间
            market_regime: 市场状态标签
            account_scale: 账户规模标签
            data_window: 数据窗口描述
        """
        # 参数空间验证
        if not param_space:
            raise ValueError("参数空间不能为空")
        if len(param_space) != len(set(p['name'] for p in param_space)):
            raise ValueError("参数名必须唯一")
        for p in param_space:
            if not all(k in p for k in ('name', 'min', 'max')):
                raise ValueError(f"参数 '{p.get('name', '?')}' 定义不完整")
            if p['min'] > p['max']:
                raise ValueError(f"参数 '{p['name']}' 最小值 > 最大值")
            if p.get('log_scale') and p['min'] <= 0:
                raise ValueError(f"参数 '{p['name']}' 对数尺度下 min 必须 > 0")
            if p.get('type') not in ('float', 'int', None):
                raise ValueError(f"参数 '{p['name']}' 类型不支持")

        self.param_space = param_space
        self.objective = objective
        self.replay_engine = replay_engine
        self._eval_func = eval_func
        self.max_dd_limit = max_dd_limit
        self.smooth_factor = smooth_factor
        self.max_eval_time_sec = max_eval_time_sec
        self.market_regime = market_regime
        self.account_scale = account_scale or "未指定"
        self.data_window = data_window
        self.rng = np.random.RandomState(random_state)

        global _OPTIMIZER_COUNTER
        with _COUNTER_LOCK:
            _OPTIMIZER_COUNTER += 1
            self._optimizer_id = f"bapo-{os.getpid()}-{threading.get_ident()}-{_OPTIMIZER_COUNTER}"

        # 内部状态
        self.X: List[np.ndarray] = []
        self.y: List[float] = []
        self.best_params: Optional[Dict] = None
        self.best_score: float = -np.inf
        self.best_iteration: int = -1
        self.gp: Optional[GaussianProcessRegressor] = None
        self._start_time: Optional[float] = None
        self._gp_train_time: float = 0.0
        self._total_iterations: int = 0

        # 审计与监控
        self._warning_count = 0
        self._error_count = 0
        self._eval_cache: OrderedDict = OrderedDict()
        self._max_cache_size = _CACHE_MAX_SIZE
        self._hash_salt = _HASH_SALT_PREFIX + str(random_state)
        self._eval_source = "replay" if self.replay_engine else "function"

        # 构建参数映射
        self._build_bounds()
        self.param_names = [p['name'] for p in param_space]

        if not self.replay_engine and not self._eval_func:
            raise ValueError("必须提供 replay_engine 或 eval_func")

        # 取消令牌与中断机制
        self._cancel_token: Optional[threading.Event] = None
        self._interrupted = threading.Event()
        self._original_sigint = None
        if threading.current_thread() is threading.main_thread():
            try:
                self._original_sigint = signal.getsignal(signal.SIGINT)
                signal.signal(signal.SIGINT, self._signal_handler)
            except ValueError:
                pass

        # 审计哈希
        self._param_space_hash = self._compute_param_space_hash()
        self._bounds_hash = self._compute_bounds_hash()

        # 停滞检测
        self._ei_stagnation_counter = 0
        # 用于恢复中断检查点的锁
        self._state_lock = threading.Lock()

    def _compute_param_space_hash(self) -> str:
        try:
            raw = json.dumps(self.param_space, sort_keys=True) + self._hash_salt
            return hashlib.sha256(raw.encode()).hexdigest()[:12]
        except Exception:
            return "未知"

    def _compute_bounds_hash(self) -> str:
        try:
            raw = json.dumps(self.bounds) + self._hash_salt
            return hashlib.sha256(raw.encode()).hexdigest()[:8]
        except Exception:
            return "未知"

    def _signal_handler(self, sig, frame):
        self._interrupted.set()
        if self._original_sigint and callable(self._original_sigint):
            self._original_sigint(sig, frame)

    def __del__(self):
        try:
            if self._original_sigint and threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGINT, self._original_sigint)
        except Exception:
            pass
        self._release_resources()

    def _release_resources(self):
        self.gp = None
        self.X.clear()
        self.y.clear()
        self._eval_cache.clear()

    def _build_bounds(self):
        self.bounds = []
        self.is_log = []
        self.is_int = []
        for p in self.param_space:
            low, high = float(p['min']), float(p['max'])
            self.bounds.append((low, high))
            self.is_log.append(p.get('log_scale', False))
            self.is_int.append(p.get('type') == 'int')

        self._norm_low = np.array([b[0] for b in self.bounds], dtype=np.float64)
        self._norm_high = np.array([b[1] for b in self.bounds], dtype=np.float64)
        self._norm_range = self._norm_high - self._norm_low
        narrow = self._norm_range < _EPS
        self._norm_range[narrow] = _EPS
        self._norm_high[narrow] = self._norm_low[narrow] + _EPS

    # --------------------------------------------------------------------------
    # 核心优化循环
    # --------------------------------------------------------------------------

    def optimize(self, n_iter: int = 80, n_init: int = 20,
                 cancel_token: Optional[threading.Event] = None) -> OptimizationResult:
        """执行贝叶斯优化。"""
        if n_init < 0 or n_iter < 0:
            raise ValueError("迭代次数必须非负")
        if n_init + n_iter > 10000:
            raise ValueError(f"优化迭代总数 ({n_init + n_iter}) 过大")

        self._cancel_token = cancel_token
        if cancel_token and cancel_token.is_set():
            logger.warning(f"[{self._optimizer_id}] 启动时取消令牌已置位")
            return self._empty_result()

        self._interrupted.clear()  # 重置中断标志
        self._start_time = time.time()
        all_params, all_scores = [], []

        equity_snapshot = getattr(self.replay_engine, 'initial_equity', 0.0) if self.replay_engine else 0.0
        logger.info(f"[{self._optimizer_id}] 优化启动 | 空间哈希={self._param_space_hash} | "
                    f"维度={len(self.param_names)} | 初始={n_init} 迭代={n_iter} | "
                    f"市场={self.market_regime} 账户={self.account_scale}")

        # 初始采样
        for idx, p in enumerate(self._sample_random(n_init)):
            if self._should_stop(): break
            score = self._evaluate(p)
            self._update(p, score, idx)
            all_params.append(p); all_scores.append(score)
            self._total_iterations += 1

        # 贝叶斯迭代
        for i in range(n_iter):
            if self._should_stop():
                logger.warning(f"[{self._optimizer_id}] 优化中断，已完成 {len(self.X)} 次评估")
                break

            t0 = time.time()
            self._train_gp()
            self._gp_train_time += time.time() - t0

            # 停滞检测
            if self._ei_stagnation_counter > _EI_STAGNATION_LIMIT:
                logger.warning(f"[{self._optimizer_id}] EI 停滞过久，增加随机探索")
                next_p = self._sample_random(1)[0]
                self._ei_stagnation_counter = 0
            else:
                next_p = self._acquire_next()
                if next_p == self._sample_random(1)[0]:
                    self._ei_stagnation_counter += 1
                else:
                    self._ei_stagnation_counter = 0

            score = self._evaluate(next_p)
            self._update(next_p, score, n_init + i)
            all_params.append(next_p); all_scores.append(score)
            self._total_iterations += 1

            if (i + 1) % max(1, n_iter // 10) == 0:
                elapsed = time.time() - self._start_time
                logger.info(f"[{self._optimizer_id}] 进度 {i+1}/{n_iter} | "
                            f"最优={self.best_score:.4f} | 耗时={elapsed:.0f}s")

        elapsed = time.time() - self._start_time
        logger.info(f"[{self._optimizer_id}] 优化完成 | 总耗时={elapsed:.1f}s | "
                    f"GP训练={self._gp_train_time:.1f}s | 最优={self.best_score:.4f}")

        params_hash = ""
        if self.best_params:
            try:
                raw = (json.dumps(self.best_params, sort_keys=True) + self._hash_salt +
                       self.market_regime + self.data_window + str(int(elapsed)) +
                       self._param_space_hash + self._bounds_hash + _OPTIMIZER_VERSION)
                params_hash = hashlib.sha256(raw.encode()).hexdigest()[:12]
            except Exception:
                pass

        self._log_param_importance()

        return OptimizationResult(
            best_params=self.best_params if self.best_params else {},
            best_score=self.best_score,
            all_params=all_params, all_scores=all_scores,
            optimization_time_sec=elapsed,
            converged=len(self.X) > 0 and not self._interrupted.is_set(),
            warning_count=self._warning_count,
            error_count=self._error_count,
            params_hash=params_hash,
            market_regime=self.market_regime,
            account_scale=self.account_scale,
            optimizer_id=self._optimizer_id,
            data_window=self.data_window,
            equity_snapshot=equity_snapshot,
            timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            param_space_hash=self._param_space_hash,
            bounds_hash=self._bounds_hash,
            total_evaluations=len(self.X),
            gp_train_time_sec=self._gp_train_time,
            optimizer_version=_OPTIMIZER_VERSION,
            best_iteration=self.best_iteration
        )

    def _empty_result(self) -> OptimizationResult:
        return OptimizationResult(
            best_params=self.best_params or {}, best_score=self.best_score,
            all_params=[], all_scores=[], optimization_time_sec=0.0,
            optimizer_id=self._optimizer_id,
            param_space_hash=self._param_space_hash,
            bounds_hash=self._bounds_hash,
            optimizer_version=_OPTIMIZER_VERSION
        )

    def _should_stop(self) -> bool:
        if self._interrupted.is_set():
            return True
        if self._cancel_token and self._cancel_token.is_set():
            return True
        if self.max_eval_time_sec and self._start_time:
            if (time.time() - self._start_time) > self.max_eval_time_sec:
                logger.warning(f"[{self._optimizer_id}] 超过最大优化时间 {self.max_eval_time_sec}s")
                return True
        if hasattr(sys, 'is_finalizing') and sys.is_finalizing():
            return True
        return False

    # --------------------------------------------------------------------------
    # 参数采样与转换
    # --------------------------------------------------------------------------

    def _sample_random(self, n: int) -> List[Dict[str, float]]:
        if n <= 0:
            return []
        points = []
        log_lows, log_highs = [], []
        for i, p in enumerate(self.param_space):
            if self.is_log[i]:
                safe_min = max(p['min'], 1e-30)
                safe_max = max(p['max'], 1e-30)
                log_lows.append(np.log(safe_min))
                log_highs.append(np.log(safe_max))
            else:
                log_lows.append(p['min'])
                log_highs.append(p['max'])

        for _ in range(n):
            params = {}
            for i, p in enumerate(self.param_space):
                if self.is_log[i]:
                    val = np.exp(self.rng.uniform(log_lows[i], log_highs[i]))
                else:
                    val = self.rng.uniform(p['min'], p['max'])
                if self.is_int[i]:
                    val = int(round(val))
                    val = max(p['min'], min(val, p['max']))
                params[p['name']] = val
            points.append(params)
        return points

    def _to_vector(self, params_dict: Dict[str, float]) -> np.ndarray:
        try:
            return np.array([float(params_dict[name]) for name in self.param_names])
        except KeyError as e:
            raise ValueError(f"参数字典缺失键: {e}")

    def _from_vector(self, vector: np.ndarray) -> Dict[str, float]:
        clipped = self._clip_to_bounds(vector)
        params = {}
        for i, name in enumerate(self.param_names):
            val = float(clipped[i])
            if self.is_int[i]:
                val = int(round(val))
                low, high = self.bounds[i]
                val = max(low, min(val, high))
            params[name] = val
        return params

    def _clip_to_bounds(self, vector: np.ndarray) -> np.ndarray:
        safe = np.nan_to_num(vector, nan=0.0, posinf=self._norm_high, neginf=self._norm_low)
        return np.clip(safe, self._norm_low, self._norm_high)

    # --------------------------------------------------------------------------
    # 评估函数
    # --------------------------------------------------------------------------

    def _evaluate(self, params: Dict[str, float]) -> float:
        try:
            safe_params = {k: v if not math.isnan(v) else 0.0 for k, v in params.items()}
            cache_key = json.dumps(safe_params, sort_keys=True, default=str)
        except (TypeError, ValueError):
            cache_key = str(sorted(params.items()))

        # 限制缓存键长度以防内存膨胀
        if len(cache_key) > 500:
            cache_key = cache_key[:500]

        if cache_key in self._eval_cache:
            return self._eval_cache[cache_key]

        try:
            if self.replay_engine:
                score = self.objective.evaluate(params, self.replay_engine)
            elif self._eval_func:
                score = self._eval_func(params)
            else:
                raise RuntimeError("无可用评估函数")

            if score is None or (isinstance(score, float) and (math.isnan(score) or (math.isinf(score) and score < 0))):
                logger.warning(f"[{self._optimizer_id}] 无效分数 {score}")
                self._warning_count += 1
                score = -np.inf
            elif isinstance(score, float) and score < -1e6:
                logger.warning(f"[{self._optimizer_id}] 异常低分 {score}")
                self._warning_count += 1
                score = -np.inf

            if len(self._eval_cache) >= self._max_cache_size:
                self._eval_cache.popitem(last=False)
            self._eval_cache[cache_key] = score
            return score

        except Exception as e:
            logger.error(f"[{self._optimizer_id}] 评估异常: {e}")
            self._error_count += 1
            return -np.inf

    # --------------------------------------------------------------------------
    # 高斯过程代理模型
    # --------------------------------------------------------------------------

    def _train_gp(self):
        if len(self.X) < 2:
            self.gp = None; return
        X = np.array(self.X); y = np.array(self.y)
        valid = np.isfinite(y) & (y > -1e6)
        if np.sum(valid) < 2:
            self.gp = None; return
        X, y = X[valid], y[valid]
        if np.std(y) < _MIN_STD_FOR_GP:
            self.gp = None; return

        # 降采样以提高效率
        if X.shape[0] > _MAX_GP_SAMPLES:
            indices = self.rng.choice(X.shape[0], size=_MAX_GP_SAMPLES, replace=False)
            X, y = X[indices], y[indices]

        X_norm = (X - self._norm_low) / self._norm_range
        dim = X.shape[1]
        kernel = ConstantKernel(1.0, (1e-3, 1e3)) * RBF(np.ones(dim), (1e-2, 1e2)) + WhiteKernel(1e-3, (1e-6, 1e-1))
        gp = GaussianProcessRegressor(
            kernel=kernel,
            n_restarts_optimizer=3 if dim > 10 else 5,
            normalize_y=True,
            random_state=self.rng.randint(0, 2**31)
        )
        try:
            gp.fit(X_norm, y)
            self.gp = gp
        except Exception as e:
            logger.error(f"[{self._optimizer_id}] GP训练失败: {e}")
            self.gp = None; self._error_count += 1

    # --------------------------------------------------------------------------
    # 采集函数
    # --------------------------------------------------------------------------

    def _acquire_next(self) -> Dict[str, float]:
        if self.gp is None or len(self.X) < 2:
            return self._sample_random(1)[0]

        dim = len(self.param_names)
        n_random = min(400, _MAX_CANDIDATES_PER_DIM * dim)
        n_perturb = min(400, _MAX_CANDIDATES_PER_DIM * dim)

        candidates = []
        rand_points = self._sample_random(n_random)
        candidates.extend([self._to_vector(p) for p in rand_points])

        if self.best_params:
            best_vec = self._to_vector(self.best_params)
            for _ in range(n_perturb):
                noise = self.rng.normal(0, 0.05, size=dim)
                perturbed = best_vec * (1 + noise)
                perturbed = self._clip_to_bounds(perturbed)
                candidates.append(perturbed)

        if not candidates:
            return self._sample_random(1)[0]

        candidates = np.array(candidates)
        X_cand_norm = (candidates - self._norm_low) / self._norm_range

        try:
            mu, sigma = self.gp.predict(X_cand_norm, return_std=True)
        except Exception:
            self._error_count += 1; return self._sample_random(1)[0]

        sigma = np.maximum(sigma, _EPS)
        mu = np.nan_to_num(mu, nan=-np.inf)
        f_best = self.best_score if np.isfinite(self.best_score) else np.median(mu)

        with np.errstate(invalid='ignore'):
            z = (mu - f_best) / sigma
            ei = (mu - f_best) * norm.cdf(z) + sigma * norm.pdf(z)
        ei = np.nan_to_num(ei, nan=0.0, neginf=0.0)

        best_idx = np.argmax(ei)
        if ei[best_idx] <= 0:
            return self._sample_random(1)[0]

        return self._from_vector(candidates[best_idx])

    # --------------------------------------------------------------------------
    # 内部状态更新
    # --------------------------------------------------------------------------

    def _update(self, params: Dict[str, float], score: float, iteration: int) -> None:
        if np.isinf(score) and score < 0:
            self._warning_count += 1; return

        vec = self._to_vector(params)
        self.X.append(vec); self.y.append(score)

        if len(self.X) > _MAX_HISTORY_SIZE:
            indices = list(range(len(self.X)))
            best_idx = np.argmax(self.y)
            keep = set(self.rng.choice(indices, size=_MAX_HISTORY_SIZE - 1, replace=False).tolist())
            keep.add(best_idx)
            self.X = [self.X[i] for i in sorted(keep)]
            self.y = [self.y[i] for i in sorted(keep)]

        if score > self.best_score:
            old_best = self.best_score
            self.best_score = score
            self.best_params = params.copy()
            self.best_iteration = iteration
            logger.info(f"[{self._optimizer_id}] 新最优 (迭代 #{iteration}): "
                        f"{self._sanitize_params(params)}, 得分: {score:.4f} (提升 {score - old_best:.6f})")

    def _sanitize_params(self, params: Dict[str, float]) -> Dict[str, float]:
        return {
            k: (round(v, 4) if any(kw in k.lower() for kw in _SENSITIVE_PARAM_KEYWORDS) else v)
            for k, v in params.items()
        }

    def _log_param_importance(self):
        if self.gp is None: return
        try:
            kernel = self.gp.kernel_
            scales = None
            if hasattr(kernel, 'k1') and hasattr(kernel.k1, 'k2'):
                rbf = kernel.k1.k2
                if hasattr(rbf, 'length_scale'): scales = rbf.length_scale
            if scales is not None:
                importance = 1.0 / (np.array(scales).flatten() + _EPS)
                ranked = sorted(zip(self.param_names, importance), key=lambda x: -x[1])
                logger.info(f"[{self._optimizer_id}] 参数重要度: "
                            f"{', '.join(f'{n}={v:.3f}' for n, v in ranked[:10])}")
        except Exception as e:
            logger.debug(f"参数重要度分析失败: {e}")

    # --------------------------------------------------------------------------
    # 检查点接口（持久化与恢复）
    # --------------------------------------------------------------------------

    def save_checkpoint(self) -> Dict[str, Any]:
        """导出优化器当前状态，用于持久化存储。"""
        with self._state_lock:
            return {
                'optimizer_id': self._optimizer_id,
                'X': [vec.tolist() for vec in self.X],
                'y': self.y.copy(),
                'best_params': self.best_params,
                'best_score': self.best_score,
                'best_iteration': self.best_iteration,
                'gp_train_time': self._gp_train_time,
                'total_iterations': self._total_iterations,
                'warning_count': self._warning_count,
                'error_count': self._error_count,
                'param_space_hash': self._param_space_hash,
                'bounds_hash': self._bounds_hash,
                'version': _OPTIMIZER_VERSION,
                'timestamp': time.time()
            }

    def load_checkpoint(self, state: Dict[str, Any]) -> None:
        """从先前导出的状态恢复优化器。"""
        with self._state_lock:
            if state.get('version') != _OPTIMIZER_VERSION:
                logger.warning(f"[{self._optimizer_id}] 检查点版本不匹配，可能无法完全恢复")
            self.X = [np.array(vec) for vec in state.get('X', [])]
            self.y = state.get('y', [])
            self.best_params = state.get('best_params')
            self.best_score = state.get('best_score', -np.inf)
            self.best_iteration = state.get('best_iteration', -1)
            self._gp_train_time = state.get('gp_train_time', 0.0)
            self._total_iterations = state.get('total_iterations', 0)
            self._warning_count = state.get('warning_count', 0)
            self._error_count = state.get('error_count', 0)
            logger.info(f"[{self._optimizer_id}] 检查点已恢复 ({len(self.X)} 个历史点)")

    # --------------------------------------------------------------------------
    # 辅助方法
    # --------------------------------------------------------------------------

    def set_initial_points(self, points: List[Dict[str, float]]) -> None:
        if not points: return
        for p in points:
            try:
                vec = self._to_vector(p)
            except Exception:
                continue
            self.X.append(vec)
            self.y.append(self.best_score - 0.1 if self.best_score != -np.inf else 0.0)
        logger.info(f"[{self._optimizer_id}] 注入 {len(points)} 个初始点")

    def reset(self) -> None:
        with self._state_lock:
            self.X.clear(); self.y.clear()
            self.best_params = None; self.best_score = -np.inf
            self.best_iteration = -1
            self.gp = None; self._eval_cache.clear()
            self._warning_count = 0; self._error_count = 0
            self._interrupted.clear()
            self._cancel_token = None
            self._gp_train_time = 0.0
            self._total_iterations = 0
            self._ei_stagnation_counter = 0
        logger.info(f"[{self._optimizer_id}] 优化器已重置")

    def __repr__(self) -> str:
        return (f"BayesianOptimizer(id={self._optimizer_id}, "
                f"dim={len(self.param_names)}, evals={len(self.X)}, "
                f"best_score={self.best_score:.4f})")
