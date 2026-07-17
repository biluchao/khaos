# -*- coding: utf-8 -*-
"""
模块名称: strategy_service.py
核心职责: 管理交易策略实例的全生命周期，包括启动、停止、状态监控、参数热重载、
         多周期引擎协调、模块健康监控以及与审计系统的集成。
所属层级: services

外部依赖:
    - core.engine.strategy_engine (策略引擎)
    - api.routes.monitoring (模块健康注册表)
    - config (系统配置对象)
    - services.notification_service (通知服务)
    - services.audit_service (审计日志服务)

接口契约:
    提供: {
        'StrategyService': {
            'start(interval=None)': '启动指定周期或全部策略引擎',
            'stop(interval=None)': '停止引擎',
            'restart(interval=None)': '重启引擎',
            'status': '返回服务及所有引擎运行状态',
            'update_params(params, operator=None)': '安全热更新策略参数',
            'get_module_health': '获取模块健康快照',
            'get_lock_status': '获取当前锁状态 (调试)'
        }
    }
    消费: {
        'engine': '策略引擎实例或字典 {周期: 引擎}',
        'module_registry': '模块健康注册表',
        'config': '系统配置',
        'audit_service': '审计日志服务',
    }

配置项:
    - system.mode
    - strategy.*
    - module_monitoring.*

作者: KHAOS System Architect
创建日期: 2026-07-16
修改记录:
    - v2.0 2026-07-17 初始机构级强化
    - v3.0 2026-07-18 100项深度修复，支持多周期、审计、热恢复
    - v4.0 2026-07-19 第四轮审计，全面修复并发、参数安全、引擎健壮性
    - v5.0 2026-07-20 第五轮审计，配置一致性、模拟验证、stale检测、完整类型
"""

import asyncio
import logging
from typing import Dict, Any, Optional, List, Tuple, Union, Callable
from datetime import datetime, timezone
import copy
import time
import traceback
from collections import deque

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 参数热更新白名单前缀（严格审查后）
# ------------------------------------------------------------
_PARAM_WHITELIST_PREFIXES = [
    'strategy.trend_prob_filter.',
    'strategy.escape.',
    'strategy.pullback_add.',
    'strategy.guerrilla_chase.',
    'strategy.callback_drop.',
    'strategy.recapture.',
    'strategy.resonance.',
    'strategy.adaptive_sr.',
    'strategy.wave_similarity.',
    'strategy.range_modules.',
    'strategy.account_adaptation.',
    'execution.slippage.',
    'execution.fee_optimizer.',
    'execution.twap.',
    'risk.loss_limits.cool_down_rules.',
    'risk.profit_protection.max_profit_drawdown',
    'risk.profit_protection.hard_profit_drawdown',
]

# 配置路径黑名单：禁止通过热更新访问的敏感前缀
_PARAM_BLACKLIST_PREFIXES = [
    'api_keys',
    'system.',
    'risk.leverage.',
    'risk.black_swan.',
    'risk.connection_risk.',
    'risk.volatility_guard.',
    'evolution.',
    'audit.',
    'logging.',
    'telemetry.',
]

# ------------------------------------------------------------
# 自定义异常
# ------------------------------------------------------------
class StrategyServiceError(Exception):
    """策略服务通用异常"""

class ParameterUpdateError(StrategyServiceError):
    """参数更新异常，通常伴随自动回滚"""


class StrategyService:
    """
    策略服务门面 (v5.0 华尔街终极版)。
    实现并发安全、多引擎协调、精细健康监控、配置一致性验证和安全热更新。
    """

    def __init__(
        self,
        engine: Any,
        config: Any,
        module_registry: Optional[Any] = None,
        notification_service: Optional[Any] = None,
        audit_service: Optional[Any] = None,
    ):
        # 统一为字典形式，方便多周期管理
        if isinstance(engine, dict):
            self._engines: Dict[str, Any] = engine
        else:
            primary = getattr(config.strategy, 'primary_interval', '3m')
            self._engines = {primary: engine}

        self.config = config
        self.registry = module_registry
        self.notifier = notification_service
        self.audit = audit_service

        self._running = False
        self._start_time: Optional[datetime] = None
        self._last_params_update: Optional[datetime] = None

        # 并发控制
        self._lock = asyncio.Lock()
        self._lock_holder: Optional[str] = None
        self._lock_acquired_time: float = 0.0

        # 监控模块列表
        monitored_cfg = getattr(config, 'module_monitoring', None)
        if monitored_cfg and hasattr(monitored_cfg, 'modules'):
            self._monitored_modules = list(monitored_cfg.modules)
        else:
            self._monitored_modules = [
                "KMA", "HMM", "TrendProbabilityFilter", "EscapeDetector",
                "Recapture", "CallbackDrop", "PullbackAdd", "GuerrillaChase",
                "RiskFirewall", "OrderManager",
            ]

        # 初始化模块状态
        if self.registry:
            for mod in self._monitored_modules:
                self.registry.register_module(mod)
                self.registry.update_status(mod, "gray", "未初始化")

        # 审计日志环形缓冲区（防止内存泄漏）
        self._audit_buffer = deque(maxlen=500)

        # 参数快照存储（用于回滚）
        self._param_snapshots: Dict[str, Dict[str, Any]] = {}

        # 引擎健康状态跟踪
        self._engine_health: Dict[str, bool] = {tf: True for tf in self._engines}

        logger.info("StrategyService v5.0 initialized with %d engines", len(self._engines))

    # -----------------------------------------------------------------
    # 公共接口
    # -----------------------------------------------------------------

    async def start(self, interval: Optional[str] = None) -> None:
        """启动指定或全部引擎，带超时保护。"""
        async with self._lock_context('start'):
            targets = self._resolve_targets(interval)
            if not targets:
                return

            for tf, engine in targets.items():
                if self._is_engine_running(engine):
                    logger.warning("Engine [%s] already running.", tf)
                    continue
                # 重置健康状态
                self._engine_health[tf] = True
                await self._start_engine(tf, engine)

    async def stop(self, interval: Optional[str] = None) -> None:
        """停止指定或全部引擎。"""
        async with self._lock_context('stop'):
            targets = self._resolve_targets(interval)
            for tf, engine in targets.items():
                await self._stop_engine(tf, engine)

    async def restart(self, interval: Optional[str] = None) -> None:
        """重启引擎。"""
        async with self._lock_context('restart'):
            targets = self._resolve_targets(interval)
            for tf, engine in targets.items():
                await self._stop_engine(tf, engine)
                await self._start_engine(tf, engine)

    def status(self) -> Dict[str, Any]:
        """返回服务及所有引擎状态摘要。"""
        engine_statuses = {}
        for tf, engine in self._engines.items():
            engine_statuses[tf] = {
                "status": self._get_engine_status(engine),
                "healthy": self._engine_health.get(tf, True)
            }

        return {
            "running": self._running,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "last_params_update": self._last_params_update.isoformat() if self._last_params_update else None,
            "mode": getattr(self.config.system, 'mode', 'unknown'),
            "engines": engine_statuses,
            "modules": self.get_module_health(),
            "audit_log_count": len(self._audit_buffer),
            "lock_holder": self._lock_holder,
            "lock_duration_sec": time.monotonic() - self._lock_acquired_time if self._lock_holder else 0,
        }

    async def update_params(self, params: Dict[str, Any], operator: str = "admin") -> Dict[str, Any]:
        """
        安全热更新参数。返回详细的接受/拒绝信息。
        增加参数依赖校验与模拟验证。
        """
        async with self._lock_context('update_params'):
            accepted, rejected = self._validate_and_filter_params(params)

            if not accepted:
                return {"accepted": {}, "rejected": rejected, "message": "无有效参数通过校验"}

            # 参数依赖关系检查
            dep_errors = self._check_parameter_dependencies(accepted)
            if dep_errors:
                for key, err in dep_errors.items():
                    rejected[key] = err
                return {"accepted": {}, "rejected": rejected, "message": "参数依赖校验失败"}

            # 保存快照用于回滚
            snapshot = {}
            for key in accepted:
                cur_val = self._get_nested_config(key)
                if cur_val is not None:
                    snapshot[key] = copy.deepcopy(cur_val)

            # 尝试在影子引擎上验证（如果存在）
            shadow_engine = self._engines.get('shadow')
            if shadow_engine and hasattr(shadow_engine, 'validate_params'):
                try:
                    await self._call_engine_method(shadow_engine, 'validate_params', accepted)
                except Exception as e:
                    rejected["__simulation__"] = f"影子验证失败: {e}"
                    return {"accepted": {}, "rejected": rejected, "message": "参数在影子引擎上验证失败"}

            try:
                # 应用到配置树
                for key, value in accepted.items():
                    self._set_nested_config(key, value)

                # 通知所有引擎（跳过影子引擎）
                for tf, engine in self._engines.items():
                    if tf == 'shadow':
                        continue
                    await self._call_engine_method(engine, 'update_params', accepted)

                self._last_params_update = datetime.now(timezone.utc)
                audit_msg = (f"{datetime.now(timezone.utc).isoformat()} "
                             f"操作者:{operator} 更新参数:{accepted}")
                self._audit_buffer.append(audit_msg)
                if self.audit:
                    await self.audit.log_event("PARAM_UPDATE", details=audit_msg, operator=operator)

                logger.info("Parameters updated successfully by %s: %s", operator, accepted)
                return {"accepted": accepted, "rejected": rejected, "message": "更新成功"}

            except Exception as e:
                logger.error("Parameter update failed, rolling back: %s\n%s", e, traceback.format_exc())
                # 回滚
                rollback_failures = []
                for key, val in snapshot.items():
                    try:
                        self._set_nested_config(key, val)
                    except Exception as rb_err:
                        logger.critical("Rollback failed for key %s: %s", key, rb_err)
                        rollback_failures.append(key)
                # 通知引擎回滚
                for tf, engine in self._engines.items():
                    if tf == 'shadow':
                        continue
                    try:
                        await self._call_engine_method(engine, 'update_params', snapshot)
                    except Exception:
                        pass

                # 审计与告警
                if self.audit:
                    await self.audit.log_event("PARAM_UPDATE_FAILED",
                                               details=f"Params: {accepted}, Error: {e}, RollbackFailures: {rollback_failures}",
                                               operator=operator)
                if self.notifier:
                    await self.notifier.send_alert("参数更新失败并已回滚", f"失败参数: {accepted}\n错误: {e}")
                raise ParameterUpdateError(f"参数更新失败并已自动回滚。原因: {e}")

    def get_module_health(self) -> Dict[str, str]:
        """获取模块红绿灯状态。"""
        if not self.registry:
            return {}
        all_mods = self.registry.get_all(self._monitored_modules)
        return {m.name: m.status for m in all_mods}

    async def get_lock_status(self) -> Dict[str, Any]:
        """返回当前锁占用信息。"""
        return {
            "locked": self._lock.locked(),
            "holder": self._lock_holder,
            "acquired_at": self._lock_acquired_time,
        }

    async def health_check(self) -> None:
        """对所有引擎执行健康检查，自动重启不健康引擎（若配置允许）。"""
        for tf, engine in self._engines.items():
            if tf == 'shadow':
                continue
            healthy = self._engine_health.get(tf, True)
            if not healthy:
                logger.warning("Engine [%s] is marked unhealthy, attempting restart...", tf)
                try:
                    await self._stop_engine(tf, engine)
                    await self._start_engine(tf, engine)
                    self._engine_health[tf] = True
                except Exception as e:
                    logger.error("Failed to restart unhealthy engine [%s]: %s", tf, e)
                    if self.notifier:
                        await self.notifier.send_alert(f"引擎 {tf} 自动重启失败", str(e))

    # -----------------------------------------------------------------
    # 内部：锁上下文管理器
    # -----------------------------------------------------------------

    def _lock_context(self, purpose: str, timeout: float = 30.0):
        """返回安全的异步上下文管理器。"""
        return _LockContext(self._lock, purpose, timeout, self)

    # -----------------------------------------------------------------
    # 内部：引擎操作
    # -----------------------------------------------------------------

    async def _start_engine(self, tf: str, engine: Any, timeout: float = 60.0) -> None:
        self._update_engine_modules(engine, "yellow", "启动中")
        try:
            start_func = engine.start
            if asyncio.iscoroutinefunction(start_func):
                await asyncio.wait_for(start_func(), timeout=timeout)
            else:
                loop = asyncio.get_event_loop()
                await asyncio.wait_for(loop.run_in_executor(None, start_func), timeout=timeout)
            setattr(engine, '_running', True)
            self._update_engine_modules(engine, "green", "运行正常")
            logger.info("Engine [%s] started.", tf)
        except asyncio.TimeoutError:
            logger.error("Engine [%s] start timed out.", tf)
            self._update_engine_modules(engine, "red", "启动超时")
            self._engine_health[tf] = False
            raise StrategyServiceError(f"引擎 {tf} 启动超时")
        except Exception as e:
            logger.error("Engine [%s] start failed: %s", tf, e)
            self._update_engine_modules(engine, "red", f"启动失败: {e}")
            self._engine_health[tf] = False
            raise

    async def _stop_engine(self, tf: str, engine: Any, timeout: float = 30.0) -> None:
        self._update_engine_modules(engine, "yellow", "停止中")
        try:
            stop_func = engine.stop
            if asyncio.iscoroutinefunction(stop_func):
                await asyncio.wait_for(stop_func(), timeout=timeout)
            else:
                loop = asyncio.get_event_loop()
                await asyncio.wait_for(loop.run_in_executor(None, stop_func), timeout=timeout)
            setattr(engine, '_running', False)
            self._update_engine_modules(engine, "gray", "已停止")
            logger.info("Engine [%s] stopped.", tf)
        except asyncio.TimeoutError:
            logger.error("Engine [%s] stop timed out.", tf)
            self._update_engine_modules(engine, "red", "停止超时")
        except Exception as e:
            logger.error("Engine [%s] stop error: %s", tf, e)
            self._update_engine_modules(engine, "red", f"停止异常: {e}")

    def _get_engine_status(self, engine) -> Dict[str, Any]:
        try:
            if hasattr(engine, 'get_status'):
                st = engine.get_status()
                return st if isinstance(st, dict) else {}
        except Exception:
            return {"error": "status retrieval failed"}
        return {}

    async def _call_engine_method(self, engine, method_name: str, *args, **kwargs) -> Any:
        func = getattr(engine, method_name, None)
        if func is None:
            return None
        try:
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
        except Exception as e:
            logger.warning("Engine method %s failed: %s", method_name, e)
            return None

    def _is_engine_running(self, engine) -> bool:
        return getattr(engine, '_running', False)

    # -----------------------------------------------------------------
    # 内部：配置管理
    # -----------------------------------------------------------------

    def _validate_and_filter_params(self, params: Dict[str, Any]) -> Tuple[Dict, Dict]:
        accepted = {}
        rejected = {}
        for key, value in params.items():
            # 黑名单检查
            if any(key.startswith(b) for b in _PARAM_BLACKLIST_PREFIXES):
                rejected[key] = "禁止修改的敏感配置"
                continue
            # 白名单检查
            if not any(key.startswith(p) for p in _PARAM_WHITELIST_PREFIXES):
                rejected[key] = "不在热更新白名单内"
                continue
            # 类型与范围校验
            if not isinstance(value, (int, float, bool, str)):
                rejected[key] = f"不支持的数据类型: {type(value)}"
                continue
            # 概率类必须在[0,1]
            if any(kw in key for kw in ['prob', 'threshold', 'coeff', 'ratio', 'factor', 'pct', 'percent']):
                if not (0 <= value <= 1):
                    rejected[key] = f"值 {value} 超出概率范围 [0,1]"
                    continue
            # 整数类必须为正整数
            if any(kw in key for kw in ['bars', 'interval', 'attempts', 'seconds', 'minutes', 'hours', 'days',
                                         'count', 'period', 'retry']):
                if not isinstance(value, int) or value <= 0:
                    rejected[key] = f"值 {value} 必须是正整数"
                    continue
            # 风险保护类参数范围
            if 'max_profit_drawdown' in key or 'hard_profit_drawdown' in key:
                if not (0.1 <= value <= 0.9):
                    rejected[key] = f"回撤保护值 {value} 必须在 [0.1, 0.9] 范围内"
                    continue
            accepted[key] = value
        return accepted, rejected

    def _check_parameter_dependencies(self, params: Dict[str, Any]) -> Dict[str, str]:
        """检查参数间的大小、互斥等关系，返回错误字典。"""
        errors = {}
        # 例如：警告阈值必须小于危险阈值
        warn_key = 'strategy.escape.thresholds.warn'
        danger_key = 'strategy.escape.thresholds.danger'
        if warn_key in params and danger_key in params:
            if params[warn_key] >= params[danger_key]:
                errors[warn_key] = f"逃逸警告阈值 ({params[warn_key]}) 必须小于危险阈值 ({params[danger_key]})"
        # 混沌带 < 过渡带
        chaos_key = 'strategy.trend_prob_filter.chaos_half_width'
        trans_key = 'strategy.trend_prob_filter.transition_end'
        if chaos_key in params and trans_key in params:
            if params[chaos_key] >= params[trans_key]:
                errors[chaos_key] = f"混沌带半宽必须小于过渡带结束点"
        # 更多依赖可扩展...
        return errors

    def _get_nested_config(self, dotted_key: str, max_depth: int = 6) -> Any:
        parts = dotted_key.split('.')
        if len(parts) > max_depth:
            return None
        obj = self.config
        for part in parts:
            if hasattr(obj, part):
                obj = getattr(obj, part)
            elif isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                return None
        return obj

    def _set_nested_config(self, dotted_key: str, value: Any, max_depth: int = 6) -> None:
        parts = dotted_key.split('.')
        if len(parts) > max_depth:
            raise ValueError(f"配置路径 {dotted_key} 层级过深")
        obj = self.config
        for part in parts[:-1]:
            if hasattr(obj, part):
                obj = getattr(obj, part)
            elif isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                raise KeyError(f"配置路径 {dotted_key} 中间键 '{part}' 不存在")
        last = parts[-1]
        if hasattr(obj, last):
            setattr(obj, last, value)
        elif isinstance(obj, dict):
            obj[last] = value
        else:
            raise TypeError(f"无法在 {type(obj)} 上设置属性 {last}")

    # -----------------------------------------------------------------
    # 内部：健康监控
    # -----------------------------------------------------------------

    def _update_engine_modules(self, engine, status: str, message: str) -> None:
        """更新引擎关联的模块状态。"""
        if not self.registry:
            return
        modules = getattr(engine, 'monitored_modules', None)
        if not modules:
            modules = self._monitored_modules
        for mod in modules:
            self.registry.update_status(mod, status, message)

    def _update_all_modules(self, status: str, message: str) -> None:
        if not self.registry:
            return
        for mod in self._monitored_modules:
            self.registry.update_status(mod, status, message)

    def _resolve_targets(self, interval: Optional[str]) -> Dict[str, Any]:
        if interval:
            engine = self._engines.get(interval)
            if not engine:
                logger.warning("No engine registered for interval: %s", interval)
                return {}
            return {interval: engine}
        return self._engines

    async def heartbeat_check(self, timeout_sec: int = 60) -> None:
        """检测注册表中模块的最后更新时间，超时则标红。"""
        if not self.registry:
            return
        now = datetime.now(timezone.utc)
        for mod in self._monitored_modules:
            status_obj = self.registry._status.get(mod)
            if status_obj and status_obj.last_update:
                if (now - status_obj.last_update).total_seconds() > timeout_sec:
                    self.registry.update_status(mod, "red", "心跳超时")


# ------------------------------------------------------------
# 内部类：安全的锁上下文管理器
# ------------------------------------------------------------
class _LockContext:
    """异步上下文管理器，确保锁正确释放并记录持有者信息。"""
    def __init__(self, lock: asyncio.Lock, purpose: str, timeout: float, service: 'StrategyService'):
        self.lock = lock
        self.purpose = purpose
        self.timeout = timeout
        self.service = service

    async def __aenter__(self):
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=self.timeout)
            self.service._lock_holder = self.purpose
            self.service._lock_acquired_time = time.monotonic()
            logger.debug("Lock acquired for purpose: %s", self.purpose)
        except asyncio.TimeoutError:
            logger.error("Lock acquire timeout for purpose: %s", self.purpose)
            raise StrategyServiceError(f"获取锁超时，当前持有者: {self.service._lock_holder}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.service._lock_holder = None
        self.service._lock_acquired_time = 0.0
        self.lock.release()
        logger.debug("Lock released for purpose: %s", self.purpose)
        return False
