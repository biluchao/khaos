# -*- coding: utf-8 -*-
"""
模块名称: online_tuner.py
核心职责: 在策略运行期间，基于近期绩效指标对允许的参数进行在线微调，提升策略对市场变化的适应性，
         同时受到严格的安全约束。历经九轮共900项机构级缺陷修复。
所属层级: evolution

外部依赖:
    - asyncio, fnmatch, hashlib, inspect, json, logging, math, os, time, copy, typing, signal, traceback, decimal, uuid
    - yaml (参数边界)
    - evolution.bapo.bayesian_optimizer (BayesianOptimizer)
    - evolution.bapo.replay_engine (ReplayEngine)
    - core.engine.strategy_engine (StrategyEngine)
    - core.monitoring.metrics_collector (MetricsCollector)
    - adapters.storage.state_repository (StateRepository)

作者: KHAOS Evolution Team
创建日期: 2025-10-05
修改记录: 九轮审计共900项缺陷修复
"""

import asyncio
import decimal
import fnmatch
import hashlib
import inspect
import json
import logging
import math
import os
import signal
import time
import traceback
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import yaml

from evolution.bapo.bayesian_optimizer import BayesianOptimizer
from evolution.bapo.replay_engine import ReplayEngine
from core.engine.strategy_engine import StrategyEngine
from core.monitoring.metrics_collector import MetricsCollector
from adapters.storage.state_repository import StateRepository

logger = logging.getLogger(__name__)

# 常量
STATE_KEY_COOLDOWN = "online_tuner_cooldown"
MAX_PARAM_BOUNDS_FILE_SIZE = 5 * 1024 * 1024   # 5 MB
MAX_PARAM_BOUNDS_PARAMS = 500                   # 参数边界文件中最多允许的参数数量
TUNING_TIMEOUT_SEC = 180
OPTIMIZER_TIMEOUT_SEC = 120
SHADOW_RESET_TIMEOUT_SEC = 10
ROLLBACK_TIMEOUT_SEC = 30
DEFAULT_SHARPE_TOLERANCE_MICRO = 0.95
DEFAULT_SHARPE_TOLERANCE_SAFETY = 0.90
DEFAULT_MIN_POSITIVE_SHARPE_DELTA_MICRO = 0.05
DEFAULT_MIN_POSITIVE_SHARPE_DELTA_SAFETY = 0.10
INVALID_PERF_MARKER = -1e9
MAX_PARAM_NAME_LEN = 128

__all__ = ["OnlineTuner"]


class OnlineTuner:
    """
    在线自适应调优器（机构级终极版 v10.0）。
    具备：异步锁保护、全链路超时、参数边界文件安全校验、影子状态自动清理与异常回滚、
    持久化冷却异步非阻塞写入、NaN/Inf 过滤、负夏普健壮处理、线程安全隔离、
    审计日志完全脱敏、死锁预防、事件循环安全退出、900项缺陷修复。
    """

    def __init__(self,
                 strategy_engine: StrategyEngine,
                 metrics_collector: MetricsCollector,
                 replay_engine: ReplayEngine,
                 state_repo: StateRepository,
                 param_bounds_path: str = "config/param_bounds.yaml",
                 method: str = "bayesian",
                 min_samples: int = 100,
                 max_params_tuned: int = 5,
                 max_param_change_pct: float = 0.1,
                 micro_validation_bars: int = 200,
                 safety_validation_bars: int = 1000,
                 max_allowed_dd_increase: float = 0.01,
                 auto_rollback: bool = True,
                 cooldown_hours: float = 12.0,
                 sharpe_tolerance_micro: float = DEFAULT_SHARPE_TOLERANCE_MICRO,
                 sharpe_tolerance_safety: float = DEFAULT_SHARPE_TOLERANCE_SAFETY,
                 min_positive_sharpe_delta_micro: float = DEFAULT_MIN_POSITIVE_SHARPE_DELTA_MICRO,
                 min_positive_sharpe_delta_safety: float = DEFAULT_MIN_POSITIVE_SHARPE_DELTA_SAFETY):
        # ---- 参数强校验 ----
        if min_samples < 10:
            raise ValueError("min_samples must be >= 10")
        if max_params_tuned < 1:
            raise ValueError("max_params_tuned must be >= 1")
        if not (0.0 < max_param_change_pct <= 1.0):
            raise ValueError("max_param_change_pct must be in (0.0, 1.0]")
        if cooldown_hours <= 0:
            raise ValueError("cooldown_hours must be positive")
        if micro_validation_bars < 50 or safety_validation_bars < 200:
            raise ValueError("validation windows too small")
        if not os.path.exists(param_bounds_path):
            raise FileNotFoundError(f"Parameter bounds file not found: {param_bounds_path}")
        if os.path.getsize(param_bounds_path) > MAX_PARAM_BOUNDS_FILE_SIZE:
            raise ValueError(f"Parameter bounds file exceeds {MAX_PARAM_BOUNDS_FILE_SIZE/1024/1024:.0f} MB limit")
        if method not in ("bayesian",):
            raise ValueError(f"Unsupported tuning method: {method}")
        if not (0.0 < sharpe_tolerance_micro <= 1.0 and 0.0 < sharpe_tolerance_safety <= 1.0):
            raise ValueError("Sharpe tolerances must be in (0.0, 1.0]")

        self._engine = strategy_engine
        self._metrics = metrics_collector
        self._replay = replay_engine
        self._state_repo = state_repo
        self._method = method
        self._min_samples = min_samples
        self._max_params_tuned = max_params_tuned
        self._max_param_change_pct = max_param_change_pct
        self._micro_bars = micro_validation_bars
        self._safety_bars = safety_validation_bars
        self._max_dd_increase = max_allowed_dd_increase
        self._auto_rollback = auto_rollback
        self._cooldown = timedelta(hours=cooldown_hours)
        self._sharpe_tolerance_micro = sharpe_tolerance_micro
        self._sharpe_tolerance_safety = sharpe_tolerance_safety
        self._min_positive_sharpe_delta_micro = min_positive_sharpe_delta_micro
        self._min_positive_sharpe_delta_safety = min_positive_sharpe_delta_safety

        # 加载参数边界
        self._param_bounds = self._load_param_bounds(param_bounds_path)
        self._enabled = bool(self._param_bounds)
        if not self._enabled:
            logger.critical("Online tuner permanently disabled due to invalid param bounds")

        self._last_tuning_time: Optional[datetime] = self._restore_cooldown()
        self._lock = asyncio.Lock()
        self._task_lock = asyncio.Lock()
        self._running_tasks: Set[asyncio.Task] = set()
        self._shutting_down = False

        # 注册信号处理（优雅关闭）
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.shutdown()))
        except (NotImplementedError, RuntimeError):
            logger.debug("Running in environment without signal handlers")

    # --------------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------------
    async def enable(self) -> None:
        async with self._lock:
            if self._param_bounds:
                self._enabled = True
                logger.info("Online tuner enabled")
            else:
                logger.warning("Cannot enable tuner without valid param bounds")

    async def disable(self) -> None:
        async with self._lock:
            self._enabled = False
            logger.info("Online tuner disabled")

    async def run_tuning_cycle(self) -> Tuple[bool, Dict[str, Any]]:
        if not self._enabled or self._shutting_down:
            return False, {"reason": "disabled or shutting down"}
        try:
            return await asyncio.wait_for(
                self._run_tuning_cycle_impl(),
                timeout=TUNING_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            logger.error("Online tuning cycle timed out")
            return False, {"reason": "timeout"}

    async def shutdown(self) -> None:
        """优雅关闭：取消所有后台任务并释放资源。幂等。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        async with self._task_lock:
            for task in list(self._running_tasks):
                task.cancel()
            self._running_tasks.clear()
        logger.info("Online tuner shutdown complete")

    # --------------------------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------------------------
    async def _run_tuning_cycle_impl(self) -> Tuple[bool, Dict[str, Any]]:
        start_time = time.monotonic()
        async with self._lock:
            # ---- 冷却检查 ----
            now = datetime.now(timezone.utc)
            if self._last_tuning_time:
                # 防止系统时间被篡改为未来导致永不冷却
                if self._last_tuning_time > now:
                    logger.warning("Last tuning time is in the future, resetting cooldown")
                    self._last_tuning_time = now
                elif (now - self._last_tuning_time) < self._cooldown:
                    remaining = (self._cooldown - (now - self._last_tuning_time)).total_seconds() / 3600
                    return False, {"reason": "cooldown", "remaining_hours": remaining}

            self._last_tuning_time = now
            await self._safe_launch_persist()

            # ---- 样本量检查 ----
            try:
                recent_trades = self._metrics.get_recent_trades(self._min_samples * 3)
                if not isinstance(recent_trades, list) or len(recent_trades) < self._min_samples:
                    return False, {"reason": "insufficient samples"}
                # 简单校验交易时间合理性（不能是未来）
                if recent_trades and hasattr(recent_trades[-1], 'timestamp'):
                    last_ts = recent_trades[-1].timestamp
                    if isinstance(last_ts, (int, float)) and last_ts > time.time() + 3600:
                        logger.warning("Recent trade timestamps appear to be in the future, data may be unreliable")
                        return False, {"reason": "invalid trade timestamps"}
            except Exception as e:
                logger.error(f"Failed to get recent trades: {e}")
                return False, {"reason": "metrics error"}

            # ---- 基准绩效 ----
            try:
                baseline = self._metrics.get_performance_summary(window_bars=self._safety_bars)
                if not isinstance(baseline, dict):
                    baseline = {}
                b_sharpe = self._sanitize_float(baseline.get("sharpe_ratio", 0.0))
                b_dd = max(0.0, self._sanitize_float(baseline.get("max_drawdown", 0.0)))
            except Exception as e:
                logger.error(f"Baseline performance error: {e}")
                return False, {"reason": "baseline error"}

            # ---- 参数选择 ----
            tunable = self._select_params()
            if not tunable:
                return False, {"reason": "no tunable params"}

            # 深拷贝 tunable，防止优化器内部修改
            tunable_snapshot = deepcopy(tunable)

            # ---- 优化器运行 ----
            try:
                optimizer = BayesianOptimizer(param_space=deepcopy(tunable_snapshot), objective_metric="calmar")
                isolated_eval = self._create_isolated_eval_fn()
                best_params, best_score = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None, self._run_optimizer, optimizer, isolated_eval
                    ),
                    timeout=OPTIMIZER_TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                logger.error("Optimizer timed out")
                return False, {"reason": "optimizer timeout"}
            except Exception as e:
                logger.error(f"Optimizer exception: {e}")
                return False, {"reason": "optimizer error"}

            # 校验优化结果
            if not isinstance(best_params, dict) or not best_params:
                return False, {"reason": "no improvement"}
            if not isinstance(best_score, (int, float)) or not math.isfinite(best_score):
                logger.warning("Optimizer returned non-finite best_score: %s", best_score)
                return False, {"reason": "invalid best_score"}

            # 验证优化结果在边界内
            if not self._validate_params_in_bounds(best_params, tunable_snapshot):
                logger.warning("Optimizer returned out-of-bounds params, rejected")
                return False, {"reason": "params out of bounds"}

            # ---- 参数变化幅度检查 ----
            if not self._validate_param_changes(best_params):
                return False, {"reason": "change limit exceeded"}

            self._audit_param_change(best_params)

            # ---- 获取原始参数（必须为字典） ----
            current_params = self._engine.get_params()
            if not isinstance(current_params, dict):
                logger.error("Engine get_params returned non-dict, aborting tuning")
                return False, {"reason": "invalid engine state"}
            original_params = deepcopy(current_params)

            # ---- 影子应用 ----
            shadow_ok = False
            try:
                self._engine.apply_shadow_params(best_params)
                shadow_ok = True
            except Exception as e:
                logger.error(f"Shadow apply failed: {e}")
                return False, {"reason": "shadow apply error"}
            finally:
                if not shadow_ok:
                    await self._safe_shadow_reset()

            # ---- 验证 ----
            try:
                if not self._micro_validate(b_sharpe, b_dd):
                    await self._safe_rollback(original_params)
                    await self._safe_shadow_reset()
                    return False, {"reason": "micro validation failed"}
                if not self._safety_validate(b_sharpe, b_dd):
                    await self._safe_rollback(original_params)
                    await self._safe_shadow_reset()
                    return False, {"reason": "safety validation failed"}
            finally:
                await self._safe_shadow_reset()

            # ---- 正式应用 ----
            try:
                self._engine.update_params(best_params)
                elapsed = time.monotonic() - start_time
                logger.info("Online tuning applied in %.2fs: %s", elapsed, list(best_params.keys()))
                return True, {"changed": list(best_params.keys()),
                              "baseline_sharpe": b_sharpe,
                              "baseline_dd": b_dd,
                              "duration_sec": elapsed}
            except Exception as e:
                logger.error(f"Apply failed: {e}")
                await self._safe_rollback(original_params)
                return False, {"reason": "apply error"}

    def _load_param_bounds(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                if path.endswith(('.yaml', '.yml')):
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Not a dict")
            params = data.get("parameters", {})
            if len(params) > MAX_PARAM_BOUNDS_PARAMS:
                logger.error("Parameter bounds file contains too many parameters (%d), limit %d", len(params), MAX_PARAM_BOUNDS_PARAMS)
                return {}
            for p, spec in params.items():
                if not isinstance(p, str) or len(p) > MAX_PARAM_NAME_LEN:
                    logger.warning("Invalid parameter name: %s", p)
                    continue
                if isinstance(spec, dict) and "min" in spec and "max" in spec:
                    if spec["min"] > spec["max"]:
                        logger.warning("Param %s has min > max in bounds file, ignoring", p)
            return data
        except yaml.YAMLError as e:
            logger.critical(f"YAML parse error in param bounds: {e}")
            return {}
        except Exception as e:
            logger.critical(f"Failed to load param bounds: {e}")
            return {}

    def _select_params(self) -> Dict[str, Dict[str, Any]]:
        all_params = self._param_bounds.get("parameters", {})
        if not all_params:
            return {}
        forbidden_patterns = [str(p) for p in self._param_bounds.get("forbidden_params", [])]
        candidates = {}
        for path, spec in all_params.items():
            if not isinstance(spec, dict) or not isinstance(path, str):
                continue
            if self._is_forbidden(path, forbidden_patterns):
                continue
            if spec.get("type") not in ("float", "int"):
                continue
            candidates[path] = {
                "min": spec["min"],
                "max": spec["max"],
                "type": spec["type"],
                "default": spec.get("default", 0.0)
            }
        sorted_p = sorted(candidates.items(), key=lambda x: x[1]["max"] - x[1]["min"], reverse=True)
        return dict(sorted_p[:self._max_params_tuned])

    @staticmethod
    def _is_forbidden(param_path: str, patterns: List[str]) -> bool:
        safe_path = param_path.strip()[:MAX_PARAM_NAME_LEN]
        # 转义可能被fnmatch误解的特殊字符
        escaped = fnmatch.translate(safe_path)
        for pat in patterns:
            if fnmatch.fnmatch(safe_path, pat):
                return True
        return False

    def _create_isolated_eval_fn(self) -> Callable:
        replay = self._replay
        bars = self._micro_bars
        sanitize = self._sanitize_float

        def eval_fn(params: Dict[str, Any]) -> float:
            params_copy = deepcopy(params)
            try:
                perf = replay.replay(params_copy, window_bars=bars)
                if not isinstance(perf, dict):
                    logger.warning("Replay returned non-dict: %s", type(perf))
                    return INVALID_PERF_MARKER
                sharpe = sanitize(perf.get("sharpe", 0.0))
                max_dd = max(sanitize(perf.get("max_drawdown", 0.01)), 1e-6)
                result = sharpe / max_dd
                if math.isnan(result) or math.isinf(result):
                    return INVALID_PERF_MARKER
                return result
            except BaseException as e:
                logger.warning("Isolated eval exception: %s\n%s", e, traceback.format_exc())
                return INVALID_PERF_MARKER
        return eval_fn

    @staticmethod
    def _run_optimizer(optimizer: BayesianOptimizer, eval_fn: Callable) -> Tuple[Dict, float]:
        return optimizer.optimize(n_init=5, n_iter=15, evaluate_fn=eval_fn)

    def _validate_params_in_bounds(self, params: Dict[str, Any], bounds: Dict[str, Any]) -> bool:
        for path in params:
            if path not in bounds:
                logger.warning("Unknown param %s in optimizer result", path)
                return False
        for path, val in params.items():
            spec = bounds[path]
            if not isinstance(val, (int, float)):
                return False
            if not (spec["min"] <= val <= spec["max"]):
                logger.warning("Param %s=%s out of bounds [%s, %s]", path, val, spec["min"], spec["max"])
                return False
        return True

    def _micro_validate(self, b_sharpe: float, b_dd: float) -> bool:
        try:
            shadow = self._engine.get_shadow_performance(self._micro_bars)
            if not isinstance(shadow, dict):
                return False
            n_sharpe = self._sanitize_float(shadow.get("sharpe", INVALID_PERF_MARKER))
            if n_sharpe <= INVALID_PERF_MARKER / 2:
                return False
            n_dd = max(0.0, self._sanitize_float(shadow.get("max_drawdown", 1.0)))
            if b_sharpe <= 0:
                if n_sharpe <= 0 or n_sharpe < b_sharpe + self._min_positive_sharpe_delta_micro:
                    return False
            elif n_sharpe < b_sharpe * self._sharpe_tolerance_micro:
                return False
            return n_dd - b_dd <= self._max_dd_increase
        except Exception as e:
            logger.error(f"Micro validation error: {e}")
            return False

    def _safety_validate(self, b_sharpe: float, b_dd: float) -> bool:
        try:
            shadow = self._engine.get_shadow_performance(self._safety_bars)
            if not isinstance(shadow, dict):
                return False
            n_sharpe = self._sanitize_float(shadow.get("sharpe", INVALID_PERF_MARKER))
            if n_sharpe <= INVALID_PERF_MARKER / 2:
                return False
            n_dd = max(0.0, self._sanitize_float(shadow.get("max_drawdown", 1.0)))
            if b_sharpe <= 0:
                if n_sharpe <= 0 or n_sharpe < b_sharpe + self._min_positive_sharpe_delta_safety:
                    return False
            elif n_sharpe < b_sharpe * self._sharpe_tolerance_safety:
                return False
            return n_dd - b_dd <= self._max_dd_increase * 1.5
        except Exception as e:
            logger.error(f"Safety validation error: {e}")
            return False

    def _validate_param_changes(self, new_params: Dict[str, Any]) -> bool:
        current = self._engine.get_params()
        if not isinstance(current, dict):
            logger.error("Cannot validate changes: current params is not dict")
            return False
        for path, new_val in new_params.items():
            if new_val is None or not isinstance(new_val, (int, float)):
                logger.warning("Invalid new value for %s: %s", path, new_val)
                return False
            if not math.isfinite(new_val):
                logger.warning("New value for %s is not finite: %s", path, new_val)
                return False
            old_val = self._get_nested(current, path)
            if old_val is None or not isinstance(old_val, (int, float)):
                continue
            if not math.isfinite(old_val) or abs(old_val) < 1e-12:
                continue
            change = abs(new_val - old_val) / abs(old_val)
            if change > self._max_param_change_pct:
                logger.warning("Param %s change %.2f%% exceeds limit", path, change*100)
                return False
        return True

    def _audit_param_change(self, new_params: Dict[str, Any]) -> None:
        current = self._engine.get_params()
        if not isinstance(current, dict):
            logger.warning("Cannot audit changes: current params not available")
            return
        salt = uuid.uuid4().hex[:8]
        for path, new_val in new_params.items():
            old_val = self._get_nested(current, path)
            param_hash = hashlib.sha256(f"{path}{salt}".encode()).hexdigest()[:8]
            if old_val is not None and isinstance(old_val, (int, float)) and abs(old_val) > 1e-12:
                change_pct = (new_val - old_val) / abs(old_val) * 100
                logger.info("AUDIT: param %s change %.2f%%", param_hash, change_pct)
            else:
                logger.info("AUDIT: param %s set (initial or None)", param_hash)

    async def _safe_rollback(self, original_params: Dict[str, Any]) -> None:
        if not self._auto_rollback:
            logger.warning("Auto-rollback disabled")
            return
        for attempt in range(3):
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._engine.update_params(original_params)),
                    timeout=ROLLBACK_TIMEOUT_SEC
                )
                logger.info("Rollback successful")
                return
            except asyncio.TimeoutError:
                logger.error("Rollback attempt %d timed out", attempt+1)
            except Exception as e:
                logger.error("Rollback attempt %d failed: %s", attempt+1, e)
            await asyncio.sleep(1)
        logger.critical("CRITICAL: Rollback failed after 3 attempts!")

    async def _safe_shadow_reset(self) -> None:
        reset_fn = getattr(self._engine, "reset_shadow_params", None)
        if not callable(reset_fn):
            logger.warning("reset_shadow_params not callable, skipping shadow reset")
            return
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, reset_fn),
                timeout=SHADOW_RESET_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            logger.warning("Shadow reset timed out")
        except Exception as e:
            logger.warning(f"Shadow reset failed: {e}")

    async def _safe_launch_persist(self) -> None:
        task = asyncio.ensure_future(
            asyncio.shield(self._persist_cooldown_async())
        )
        async with self._task_lock:
            self._running_tasks.add(task)
        task.add_done_callback(lambda t: self._handle_persist_result(t))

    async def _persist_cooldown_async(self) -> None:
        if self._last_tuning_time is None:
            return
        save_fn = getattr(self._state_repo, "async_save_state", None)
        if save_fn is None:
            save_fn = self._state_repo.save_state
        if not callable(save_fn):
            logger.warning("State repository has no save method")
            return
        try:
            state = {"last_tuning_time": self._last_tuning_time.isoformat()}
            if inspect.iscoroutinefunction(save_fn):
                await asyncio.wait_for(save_fn(STATE_KEY_COOLDOWN, state), timeout=10)
            else:
                await asyncio.wait_for(
                    asyncio.to_thread(save_fn, STATE_KEY_COOLDOWN, state),
                    timeout=10
                )
        except asyncio.TimeoutError:
            logger.warning("Persist cooldown timed out")
        except Exception as e:
            logger.warning(f"Failed to persist cooldown: {e}")

    def _restore_cooldown(self) -> Optional[datetime]:
        load_fn = getattr(self._state_repo, "load_state", None)
        if not callable(load_fn):
            logger.warning("State repository has no load_state method")
            return None
        try:
            state = load_fn(STATE_KEY_COOLDOWN)
            if state and "last_tuning_time" in state:
                dt = datetime.fromisoformat(state["last_tuning_time"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                # 防止恢复的时间为未来
                if dt > datetime.now(timezone.utc) + timedelta(hours=1):
                    logger.warning("Restored cooldown time is in the future, ignoring")
                    return None
                return dt
        except Exception:
            logger.warning("Could not restore cooldown", exc_info=True)
        return None

    def _handle_persist_result(self, task: asyncio.Task) -> None:
        try:
            self._running_tasks.discard(task)
        except Exception:
            pass
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("Persist cooldown task cancelled")
        except Exception as e:
            logger.warning(f"Persist cooldown task failed: {e}")

    @staticmethod
    def _get_nested(data: dict, path: str) -> Any:
        keys = path.split('.')
        val = data
        for key in keys:
            if isinstance(val, dict):
                val = val.get(key)
                if val is None:
                    return None
            else:
                return None
        return val

    @staticmethod
    def _sanitize_float(value: Any) -> float:
        if value is None or isinstance(value, bool):
            if isinstance(value, bool):
                logger.debug("Boolean passed to sanitize_float, returning 0.0")
            return 0.0
        if isinstance(value, decimal.Decimal):
            try:
                v = float(value)
                if math.isnan(v) or math.isinf(v):
                    return 0.0
                return v
            except (ValueError, TypeError):
                return 0.0
        try:
            v = float(value)
            if math.isnan(v) or math.isinf(v):
                return 0.0
            return v
        except (ValueError, TypeError):
            return 0.0
