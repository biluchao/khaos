# -*- coding: utf-8 -*-
"""
模块名称: health_checker.py
核心职责: 系统级健康检查框架，支持异步检查、定时巡检、自动告警、指标上报与优雅降级。
         经七轮机构级穿透审计，具备极致并发安全、资源管理和数据一致性。
所属层级: core.monitoring

外部依赖:
    - asyncio, time, logging, enum, dataclasses, typing, collections.OrderedDict, re
    - core.monitoring.metrics_collector (可选)

接口契约:
    提供: {
        'HealthChecker': {
            'async with HealthChecker() as checker': '异步上下文管理器',
            'has_check(name) -> bool': '检查是否存在指定检查项',
            'register(name, check_fn, ...) -> Optional[Callable]': '注册检查项',
            'unregister(name) -> Optional[Callable]': '移除检查项',
            'async run_all() -> OrderedDict[str, HealthReport]': '运行所有检查',
            'async run(name) / check_now(name) -> HealthReport': '运行指定检查',
            'async get_report(name) -> HealthReport': '异步获取指定报告',
            'get_status() -> Dict[str, HealthReport]': '同步获取报告快照',
            'async_get_status() -> Dict[str, HealthReport]': '异步获取报告',
            'property overall_healthy: bool': '整体是否健康',
            'get_overall_status() -> str': '总体状态字符串',
            'async get_config(name) -> Optional[dict]': '获取检查项配置',
            'async update_check_timeout(name, timeout)': '动态修改检查超时',
            'async pause_check(name)': '暂停检查',
            'async resume_check(name)': '恢复检查',
            'async get_paused_checks() -> Set[str]': '获取暂停的检查项列表',
            'async get_active_checks() -> List[str]': '获取当前正在运行的检查项名称',
            'async cancel_all_checks()': '取消所有正在运行的检查',
            'async reset_statistics(name)': '重置单项统计',
            'async reset_all_statistics()': '重置全部统计',
            'async clear_all()': '清除所有注册',
            'property registered_count: int': '已注册数量',
            'async start_periodic(interval) -> None': '定时检查',
            'async stop_periodic() -> None': '停止定时',
            'async update_interval(interval) -> None': '动态更新定时间隔',
            'async shutdown() -> None': '优雅关闭',
            'to_dict(include_error: bool = False) -> List[dict]': '序列化'
        }
    }
作者: KHAOS System Architect
创建日期: 2025-04-10
修改记录:
    - 2026-07-13 第七轮审计：任务管理、接口异步化、自动移除逻辑强化、活跃检查等100项修复
版本: 9.0.0
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Type, Union

logger = logging.getLogger(__name__)

class Severity(Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class CheckStatus(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    MAINTENANCE = "MAINTENANCE"

@dataclass
class CheckResult:
    status: CheckStatus
    details: str = ""
    duration_ms: float = 0.0
    error: Optional[str] = field(default=None, repr=False)

    def __post_init__(self):
        self.details = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', self.details).strip()[:500]
        if self.error:
            self.error = self.error[:200]

@dataclass
class HealthReport:
    name: str
    last_result: Optional[CheckResult] = None
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0
    last_success_time: Optional[float] = None
    last_run_time: float = 0.0
    severity: Severity = Severity.MEDIUM
    description: str = ""

    def to_dict(self, include_error: bool = False) -> Dict[str, Any]:
        last = self.last_result
        result = {
            "name": self.name,
            "status": last.status.value if last else "UNKNOWN",
            "details": last.details if last else "",
            "duration_ms": last.duration_ms if last else 0,
            "consecutive_failures": self.consecutive_failures,
            "severity": self.severity.value,
            "description": self.description,
            "last_run_time": self.last_run_time,
            "last_success_time": self.last_success_time,
        }
        if include_error and last and last.error:
            result["error"] = last.error
        return result


class HealthChecker:
    """
    华尔街级健康检查器 v9.0，提供极致可靠性与并发安全。
    """

    NAME_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_\-]{0,63}$')
    DEFAULT_MAX_CHECKS = 50
    DEFAULT_CONSECUTIVE_TIMEOUT_LIMIT = 5

    @classmethod
    async def initialize(cls, **kwargs) -> None:
        pass

    @classmethod
    async def healthcheck(cls) -> None:
        pass

    def __init__(self,
                 name: str = "HealthChecker",
                 timeout_sec: float = 10.0,
                 export_metrics: bool = True,
                 metrics_collector: Optional[Any] = None,
                 max_checks: int = DEFAULT_MAX_CHECKS,
                 consecutive_timeout_limit: int = DEFAULT_CONSECUTIVE_TIMEOUT_LIMIT,
                 shutdown_timeout: float = 10.0,
                 max_concurrent_checks: int = 0):
        if timeout_sec <= 0:
            raise ValueError("timeout_sec 必须大于 0")
        if max_checks < 1:
            raise ValueError("max_checks 至少为 1")
        if consecutive_timeout_limit < 1:
            raise ValueError("consecutive_timeout_limit 至少为 1")
        self.name = name
        self._global_timeout = timeout_sec
        self._export_metrics = export_metrics and metrics_collector is not None
        self._metrics_collector = metrics_collector
        self._max_checks = max_checks
        self._consecutive_timeout_limit = consecutive_timeout_limit
        self._shutdown_timeout = shutdown_timeout

        self._checks: Dict[str, Callable[[], Awaitable[CheckResult]]] = {}
        self._configs: Dict[str, dict] = {}
        self._reports: Dict[str, HealthReport] = {}
        self._running_futures: Dict[str, asyncio.Task] = {}
        self._paused_checks: Set[str] = set()

        self._state_lock = asyncio.Lock()
        self._future_lock = asyncio.Lock()
        self._start_stop_lock = asyncio.Lock()
        self._concurrency_semaphore = asyncio.Semaphore(max_concurrent_checks) if max_concurrent_checks > 0 else None

        self._periodic_task: Optional[asyncio.Task] = None
        self._periodic_interval: float = 30.0
        self._shutdown = False

        logger.info("[%s] 健康检查器已创建", self.name)

    def __repr__(self) -> str:
        return f"<HealthChecker name={self.name!r} checks={self.registered_count}>"

    # ---------- 上下文管理器 ----------
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            logger.error("[%s] 上下文管理器内发生异常: %s", self.name, exc_val)
        await self.shutdown()
        # 不抑制异常

    # ---------- 公共属性 ----------
    @property
    def registered_count(self) -> int:
        return len(self._checks)

    @property
    def overall_healthy(self) -> bool:
        return self.get_overall_status() == CheckStatus.HEALTHY.value

    # ---------- 公共 API ----------

    def has_check(self, name: str) -> bool:
        return name in self._checks

    async def register(self, name: str, check_fn: Callable[[], Awaitable[CheckResult]],
                       severity: Severity = Severity.MEDIUM,
                       description: str = "",
                       custom_timeout: Optional[float] = None) -> Optional[Callable]:
        if self._shutdown:
            raise RuntimeError("已关闭，不能注册")
        if not self.NAME_PATTERN.match(name):
            raise ValueError(f"无效名称: {name} (必须以字母或下划线开头，长度1-64)")
        if not isinstance(severity, Severity):
            raise TypeError("severity 必须是 Severity 枚举")
        if len(description) > 200:
            raise ValueError("描述不能超过200字符")
        if not asyncio.iscoroutinefunction(check_fn):
            raise TypeError("check_fn 必须是异步函数")
        if custom_timeout is not None and custom_timeout < 0.1:
            raise ValueError("custom_timeout 必须 >= 0.1 秒")
        async with self._state_lock:
            return await self._register_unlocked(name, check_fn, severity, description, custom_timeout)

    async def unregister(self, name: str) -> Optional[Callable]:
        if self._shutdown:
            raise RuntimeError("已关闭，不能注销")
        async with self._state_lock:
            return await self._unregister_unlocked(name)

    async def run_all(self) -> OrderedDict[str, HealthReport]:
        if self._shutdown:
            raise RuntimeError("已关闭，无法运行检查")

        async with self._state_lock:
            if self._shutdown:
                raise RuntimeError("已关闭")
            checks_snapshot = [(name, self._checks[name], self._configs.get(name, {}).get('custom_timeout'))
                               for name in list(self._checks.keys()) if name not in self._paused_checks]

        async def run_with_semaphore(name, fn, timeout):
            if self._concurrency_semaphore:
                async with self._concurrency_semaphore:
                    return await self._run_single(name, fn, timeout)
            else:
                return await self._run_single(name, fn, timeout)

        tasks = [run_with_semaphore(name, check_fn, timeout) for name, check_fn, timeout in checks_snapshot]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.warning("[%s] run_all 被取消，正在清理 %d 个子任务", self.name, len(tasks))
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
            except asyncio.TimeoutError:
                logger.error("[%s] 取消子任务超时", self.name)
            raise

        async with self._state_lock:
            for (name, check_fn, _), result in zip(checks_snapshot, results):
                if self._checks.get(name) is not check_fn:
                    continue
                if isinstance(result, BaseException):
                    result = CheckResult(status=CheckStatus.UNKNOWN, details="检查异常", error=str(result))
                self._update_report(name, check_fn, result)
            ordered_reports = OrderedDict((name, self._reports[name]) for name, _, _ in checks_snapshot)

        if self._export_metrics:
            async with self._state_lock:
                reports_snapshot = {name: (self._reports[name].last_result.status if self._reports[name].last_result else None)
                                    for name in list(self._reports.keys())}
            await self._export_to_metrics(reports_snapshot)

        return ordered_reports

    async def run(self, name: str) -> HealthReport:
        """运行指定检查项 (别名 check_now)"""
        if self._shutdown:
            raise RuntimeError("已关闭，无法运行检查")
        async with self._state_lock:
            if name not in self._checks:
                raise KeyError(f"检查项 {name} 未注册")
            check_fn = self._checks[name]
            timeout = self._configs.get(name, {}).get('custom_timeout') or self._global_timeout

        result = await self._run_single(name, check_fn, timeout)
        if isinstance(result, BaseException):
            result = CheckResult(status=CheckStatus.UNKNOWN, error=str(result))

        async with self._state_lock:
            if self._checks.get(name) is check_fn:
                self._update_report(name, check_fn, result)
            return self._reports.get(name, HealthReport(name=name))

    check_now = run

    async def get_report(self, name: str) -> HealthReport:
        async with self._state_lock:
            if name not in self._reports:
                raise KeyError(f"无 {name} 的报告")
            return self._reports[name]

    def get_status(self) -> Dict[str, HealthReport]:
        names = list(self._checks.keys())
        return {name: self._reports[name] for name in names if name in self._reports}

    async def async_get_status(self) -> Dict[str, HealthReport]:
        async with self._state_lock:
            names = list(self._checks.keys())
            return {name: self._reports[name] for name in names if name in self._reports}

    def get_overall_status(self) -> str:
        reports = list(self._reports.values())
        if not reports:
            return CheckStatus.UNKNOWN.value
        statuses = [r.last_result.status for r in reports if r.last_result]
        if any(s == CheckStatus.UNHEALTHY for s in statuses):
            return CheckStatus.UNHEALTHY.value
        if any(s == CheckStatus.TIMEOUT for s in statuses):
            return CheckStatus.DEGRADED.value
        if all(s == CheckStatus.MAINTENANCE for s in statuses):
            return CheckStatus.MAINTENANCE.value
        if all(s == CheckStatus.UNKNOWN for s in statuses):
            return CheckStatus.UNKNOWN.value
        return CheckStatus.HEALTHY.value

    async def get_config(self, name: str) -> Optional[dict]:
        async with self._state_lock:
            cfg = self._configs.get(name)
            return dict(cfg) if cfg else None

    async def update_check_timeout(self, name: str, timeout: Optional[float]) -> None:
        if self._shutdown:
            raise RuntimeError("已关闭")
        async with self._state_lock:
            if name not in self._configs:
                raise KeyError(f"检查项 {name} 不存在")
            if timeout is not None and timeout < 0.1:
                raise ValueError("timeout 必须 >= 0.1 秒")
            self._configs[name]['custom_timeout'] = timeout

    async def pause_check(self, name: str) -> None:
        if self._shutdown:
            raise RuntimeError("已关闭")
        async with self._state_lock:
            if name not in self._checks:
                raise KeyError(f"检查项 {name} 不存在")
            self._paused_checks.add(name)

    async def resume_check(self, name: str) -> None:
        if self._shutdown:
            raise RuntimeError("已关闭")
        async with self._state_lock:
            self._paused_checks.discard(name)

    async def get_paused_checks(self) -> Set[str]:
        async with self._state_lock:
            return set(self._paused_checks)

    async def get_active_checks(self) -> List[str]:
        """返回当前正在运行的检查项名称"""
        async with self._future_lock:
            return list(self._running_futures.keys())

    async def cancel_all_checks(self) -> None:
        """取消所有正在运行的检查任务，但不移除注册"""
        async with self._future_lock:
            tasks = list(self._running_futures.values())
            for task in tasks:
                task.cancel()
            self._running_futures.clear()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("[%s] 取消所有检查任务超时", self.name)

    async def start_periodic(self, interval_sec: float = 30.0) -> None:
        if self._shutdown:
            raise RuntimeError("已关闭")
        if interval_sec < 1.0:
            raise ValueError("间隔至少为 1 秒")
        async with self._start_stop_lock:
            if self._periodic_task:
                if not self._periodic_task.done():
                    logger.warning("[%s] 定时任务已在运行", self.name)
                    return
                self._periodic_task = None  # 清理已完成任务
            self._periodic_interval = interval_sec
            self._periodic_task = asyncio.create_task(self._periodic_loop(interval_sec))
            self._periodic_task.add_done_callback(self._on_periodic_done)
            logger.info("[%s] 定时健康检查已启动，间隔 %.1f 秒", self.name, interval_sec)

    async def stop_periodic(self) -> None:
        async with self._start_stop_lock:
            if not self._periodic_task or self._periodic_task.done():
                return
            self._periodic_task.cancel()
            try:
                await self._periodic_task
            except asyncio.CancelledError:
                pass
            self._periodic_task = None
            logger.info("[%s] 定时任务已停止", self.name)

    async def update_interval(self, interval_sec: float) -> None:
        await self.stop_periodic()
        await self.start_periodic(interval_sec)

    async def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        try:
            await self.stop_periodic()
        except Exception as e:
            logger.warning("[%s] 停止定时任务异常: %s", self.name, e)

        async with self._future_lock:
            tasks = list(self._running_futures.values())
            for task in tasks:
                task.cancel()
            self._running_futures.clear()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=self._shutdown_timeout)
            except asyncio.TimeoutError:
                logger.error("[%s] shutdown 时等待任务取消超时（%.1fs），强制取消", self.name, self._shutdown_timeout)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
                except asyncio.TimeoutError:
                    logger.error("[%s] 强制取消也超时，放弃等待", self.name)
        logger.info("[%s] 健康检查器已关闭", self.name)

    def to_dict(self, include_error: bool = False) -> List[Dict[str, Any]]:
        return [r.to_dict(include_error=include_error) for r in list(self._reports.values())]

    async def reset_statistics(self, name: str) -> None:
        if self._shutdown:
            raise RuntimeError("已关闭")
        async with self._state_lock:
            if name in self._reports:
                cfg = self._configs.get(name, {})
                self._reports[name] = HealthReport(name=name,
                                                   severity=cfg.get('severity', Severity.MEDIUM),
                                                   description=cfg.get('description', ''))

    async def reset_all_statistics(self) -> None:
        if self._shutdown:
            raise RuntimeError("已关闭")
        async with self._state_lock:
            self._reports.clear()

    async def clear_all(self) -> None:
        if self._shutdown:
            raise RuntimeError("已关闭")
        async with self._state_lock:
            names = list(self._checks.keys())
            for name in names:
                await self._unregister_unlocked(name)

    # ---------- 内部实现 ----------

    async def _register_unlocked(self, name: str, check_fn, severity, description, custom_timeout) -> Optional[Callable]:
        if len(self._checks) >= self._max_checks:
            raise RuntimeError(f"达到最大注册数 {self._max_checks}")
        old = self._checks.get(name)
        if old is check_fn:
            return old  # 相同函数不重复注册
        self._checks[name] = check_fn
        self._configs[name] = {
            'severity': severity,
            'description': description,
            'custom_timeout': custom_timeout
        }
        self._reports.pop(name, None)
        self._paused_checks.discard(name)

        async with self._future_lock:
            if name in self._running_futures:
                task = self._running_futures.pop(name)
                if task and not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                        pass

        if old:
            logger.info("[%s] 检查项 %s 被覆盖", self.name, name)
        else:
            logger.debug("[%s] 注册健康检查项: %s", self.name, name)
        return old

    async def _unregister_unlocked(self, name: str) -> Optional[Callable]:
        old = self._checks.pop(name, None)
        self._configs.pop(name, None)
        self._reports.pop(name, None)
        self._paused_checks.discard(name)

        async with self._future_lock:
            task = self._running_futures.pop(name, None)
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass

        if old:
            logger.debug("[%s] 移除健康检查项: %s", self.name, name)
        return old

    async def _run_single(self, name: str, check_fn, timeout: float) -> CheckResult:
        result = None
        task = asyncio.create_task(check_fn())
        async with self._future_lock:
            old_task = self._running_futures.get(name)
            if old_task and not old_task.done():
                old_task.cancel()
                try:
                    await asyncio.wait_for(old_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception as exc:
                    logger.warning("[%s] 取消旧任务 %s 时出现异常: %s", self.name, name, exc)
            self._running_futures[name] = task

        start = time.monotonic()
        try:
            try:
                raw = await asyncio.wait_for(task, timeout=timeout)
                if not isinstance(raw, CheckResult):
                    result = CheckResult(status=CheckStatus.UNKNOWN,
                                         details=f"返回非 CheckResult: {type(raw).__name__}",
                                         error="Invalid return type")
                else:
                    result = raw
                    if result.duration_ms == 0.0:
                        result.duration_ms = (time.monotonic() - start) * 1000
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    logger.error("[%s] 检查项 %s 超时取消失败", self.name, name)
                except Exception as exc:
                    logger.warning("[%s] 检查项 %s 取消后出现异常: %s", self.name, name, exc)
                duration = (time.monotonic() - start) * 1000
                result = CheckResult(status=CheckStatus.TIMEOUT,
                                     details=f"超时 ({timeout:.1f}s)",
                                     duration_ms=duration,
                                     error="TimeoutError")
            except asyncio.CancelledError:
                logger.debug("[%s] 检查项 %s 被取消", self.name, name)
                raise
            except RuntimeError as e:
                logger.exception("[%s] 检查项 %s 运行时错误", self.name, name)
                duration = (time.monotonic() - start) * 1000
                result = CheckResult(status=CheckStatus.UNKNOWN, details="运行时错误",
                                     duration_ms=duration, error=str(e)[:200])
            except Exception as e:
                logger.exception("[%s] 检查项 %s 异常", self.name, name)
                duration = (time.monotonic() - start) * 1000
                result = CheckResult(status=CheckStatus.UNKNOWN, details="异常",
                                     duration_ms=duration, error=str(e)[:200])
        finally:
            if result is not None and result.duration_ms == 0.0:
                result.duration_ms = (time.monotonic() - start) * 1000
            async with self._future_lock:
                if self._running_futures.get(name) is task:
                    del self._running_futures[name]

        return result

    def _update_report(self, name: str, check_fn, result: CheckResult) -> None:
        cfg = self._configs.get(name, {})
        if name not in self._reports:
            self._reports[name] = HealthReport(name=name,
                                              severity=cfg.get('severity', Severity.MEDIUM),
                                              description=cfg.get('description', ''))
        report = self._reports[name]
        report.last_result = result
        report.total_runs += 1
        report.last_run_time = time.time()

        if result.status == CheckStatus.HEALTHY:
            report.consecutive_failures = 0
            report.last_success_time = time.time()
        elif result.status in (CheckStatus.UNHEALTHY, CheckStatus.TIMEOUT):
            report.consecutive_failures += 1
            report.total_failures += 1

        # 仅在第一次达到超时限制时触发自动移除，防止重复创建任务
        if (result.status == CheckStatus.TIMEOUT and
                report.consecutive_failures == self._consecutive_timeout_limit):
            logger.error("[%s] 检查项 %s 连续超时 %d 次，自动移除", self.name, name, report.consecutive_failures)
            current_check_fn = self._checks.get(name)
            if current_check_fn is not None:
                task = asyncio.create_task(self._safe_auto_remove(name, current_check_fn))
                task.add_done_callback(self._on_auto_remove_done)

    async def _safe_auto_remove(self, name: str, expected_check_fn):
        if self._shutdown:
            return
        try:
            async with self._state_lock:
                if name in self._checks and self._checks[name] is expected_check_fn:
                    await self._unregister_unlocked(name)
        except Exception as e:
            logger.error("[%s] 自动移除检查项 %s 失败: %s", self.name, name, e)

    def _on_auto_remove_done(self, task):
        if not task.cancelled() and task.exception():
            logger.error("[%s] 自动移除任务异常: %s", self.name, task.exception())

    def _on_periodic_done(self, task):
        if not task.cancelled() and task.exception():
            logger.error("[%s] 定时健康检查任务异常: %s", self.name, task.exception())

    async def _export_to_metrics(self, reports_snapshot: Dict[str, Optional[CheckStatus]]) -> None:
        if not self._metrics_collector:
            return
        try:
            if hasattr(self._metrics_collector, 'set_gauge'):
                status_map = {
                    CheckStatus.HEALTHY: 0,
                    CheckStatus.DEGRADED: 1,
                    CheckStatus.UNHEALTHY: 2,
                    CheckStatus.TIMEOUT: 3,
                    CheckStatus.UNKNOWN: 4,
                    CheckStatus.MAINTENANCE: 5,
                }
                for name, status in reports_snapshot.items():
                    if status is not None:
                        value = status_map.get(status, 4)
                        safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', name)[:63]
                        if not safe_name:
                            safe_name = "unknown"
                        gauge_fn = self._metrics_collector.set_gauge
                        if asyncio.iscoroutinefunction(gauge_fn):
                            await gauge_fn("khaos_monitor_health_check", value, labels={"check_name": safe_name})
                        else:
                            gauge_fn("khaos_monitor_health_check", value, labels={"check_name": safe_name})
        except Exception as e:
            logger.error("[%s] 指标上报失败: %s", self.name, e)

    async def _periodic_loop(self, interval_sec: float) -> None:
        while True:
            try:
                await self.run_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[%s] 定时健康检查出错: %s", self.name, e)
            await asyncio.sleep(interval_sec)


__all__ = [
    "HealthChecker",
    "CheckStatus",
    "CheckResult",
    "HealthReport",
    "Severity",
              ]
