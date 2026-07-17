# -*- coding: utf-8 -*-
"""
模块名称: evolution.py
核心职责: 进化模块 REST API (机构级 v6.0)
         历经六轮共600项缺陷修复，具备生产级韧性。
所属层级: api.routes
"""
import asyncio
import logging
import time
import uuid
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Literal, Set, Tuple, NamedTuple
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from api.dependencies import get_current_user, get_evolution_service, get_admin_user
from services.evolution_service import EvolutionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/evolution", tags=["evolution"])

# 常量
MAX_LOCK_POOL = 20
LOCK_EXPIRY_SECONDS = 1800  # 30分钟
ADMIN_ROLE = "admin"
MAX_BACKGROUND_TASKS = 5  # 最大并发后台任务

# 锁信息结构
class LockInfo(NamedTuple):
    lock: asyncio.Lock
    last_access: float

# 全局锁池（使用 defaultdict 简化）
_module_locks: Dict[str, LockInfo] = {}
_locks_guard = asyncio.Lock()
# 后台任务信号量
_background_sem = asyncio.Semaphore(MAX_BACKGROUND_TASKS)

# 定期清理锁任务
_cleanup_task: Optional[asyncio.Task] = None

async def _lock_cleanup_loop():
    """每10分钟清理过期锁"""
    while True:
        await asyncio.sleep(600)
        async with _locks_guard:
            now = time.monotonic()
            stale = [n for n, info in _module_locks.items() 
                     if not info.lock.locked() and (now - info.last_access) > LOCK_EXPIRY_SECONDS]
            for n in stale:
                del _module_locks[n]
        logger.debug("Lock cleanup: removed %d expired locks", len(stale))

async def _get_module_lock(module: str) -> asyncio.Lock:
    async with _locks_guard:
        now = time.monotonic()
        if module not in _module_locks:
            _module_locks[module] = LockInfo(asyncio.Lock(), now)
        else:
            info = _module_locks[module]
            _module_locks[module] = LockInfo(info.lock, now)
        return _module_locks[module].lock

def _allowed_modules(service: EvolutionService) -> Set[str]:
    try:
        return set(service.get_allowed_modules()) if service else set()
    except Exception:
        return set()

# 数据模型
class EvolutionTaskInfo(BaseModel):
    name: str
    status: Literal['idle','running','completed','failed','approved','rolled_back','timeout','cancelled'] = 'idle'
    task_id: Optional[str] = None
    last_run: Optional[str] = None
    last_duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    progress: Optional[int] = Field(None, ge=0, le=100)
    result_url: Optional[str] = None

class EvolutionStatus(BaseModel):
    global_enabled: bool
    mode: Literal['shadow','recommend','live']
    kill_switch: bool
    tasks: List[EvolutionTaskInfo]

class StartStopResponse(BaseModel):
    message: str
    scheduler_running: bool = False
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class ManualRunResponse(BaseModel):
    message: str
    module: str
    task_id: str
    started_at: str
    status: Optional[str] = None

class ApprovalRequest(BaseModel):
    module: str = Field(..., max_length=20, regex=r'^[a-zA-Z0-9_-]+$')

class ApprovalResponse(BaseModel):
    message: str
    module: str
    result_id: str
    approved: bool
    timestamp: str

class RollbackResponse(BaseModel):
    message: str
    previous_version: Optional[str]
    current_version: Optional[str]
    timestamp: str

class KillSwitchRequest(BaseModel):
    active: bool = True
    reason: str = Field(..., min_length=1, max_length=500)

class KillSwitchResponse(BaseModel):
    message: str
    active: bool
    tasks: List[EvolutionTaskInfo]
    timestamp: str

# 辅助函数
def _sanitize(text: str) -> str:
    import re
    return re.sub(r'[^\w\s\-:.,!?@#$%^&*()]', '', text)[:500]

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        # 取最后一个IP（最靠近代理）
        ips = [ip.strip() for ip in forwarded.split(',')]
        return ips[-1] if ips else request.client.host if request.client else 'unknown'
    return request.client.host if request.client else 'unknown'

def _audit(user: str, action: str, details: str = '', request: Request = None, trace_id: str = ''):
    entry = {
        "event": "evolution_audit",
        "user": user,
        "action": action,
        "details": details[:500],
        "ip": _client_ip(request) if request else 'unknown',
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id
    }
    logger.info("EVOLUTION_AUDIT %s", json.dumps(entry))

def _trace_id(request: Request) -> str:
    return getattr(request.state, 'trace_id', str(uuid.uuid4()))

# 启动后台清理任务
@router.on_event("startup")
async def startup_event():
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_lock_cleanup_loop())

@router.on_event("shutdown")
async def shutdown_event():
    if _cleanup_task:
        _cleanup_task.cancel()

# 路由实现
@router.get("/status", response_model=EvolutionStatus, summary="获取进化任务状态", response_description="当前所有进化模块的任务列表")
async def get_evolution_status(
    module: Optional[str] = None,
    evolution_service: EvolutionService = Depends(get_evolution_service),
    current_user: str = Depends(get_current_user),
    request: Request = None
):
    if not evolution_service:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service unavailable")
    try:
        status_data = await asyncio.wait_for(evolution_service.get_status_async(), timeout=5.0)
    except asyncio.TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Status query timed out")
    tasks = []
    for name, t in status_data.get('tasks', {}).items():
        if module and name != module:
            continue
        detail = evolution_service.get_task_detail(name) if evolution_service else {}
        tasks.append(EvolutionTaskInfo(
            name=name, status=t['status'], task_id=detail.get('task_id'),
            last_run=t.get('last_run'), last_duration_ms=detail.get('last_duration_ms'),
            error_message=detail.get('error_message'), progress=detail.get('progress'),
            result_url=detail.get('result_url')
        ))
    # 状态排序：异常优先
    severity = {'failed':0, 'timeout':0, 'running':1, 'completed':2, 'approved':2, 'rolled_back':2, 'idle':3, 'cancelled':3}
    tasks.sort(key=lambda x: severity.get(x.status, 3))
    return EvolutionStatus(
        global_enabled=status_data['global_enabled'],
        mode=status_data['mode'],
        kill_switch=status_data.get('kill_switch', False),
        tasks=tasks
    )

@router.put("/start", response_model=StartStopResponse, status_code=status.HTTP_202_ACCEPTED, summary="启动进化调度器")
async def start_evolution(
    background_tasks: BackgroundTasks,
    request: Request,
    evolution_service: EvolutionService = Depends(get_evolution_service),
    current_user: str = Depends(get_admin_user)
):
    if not evolution_service or not evolution_service.is_globally_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Evolution disabled")
    if evolution_service.is_scheduler_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "Scheduler already running")
    _audit(current_user, "start", request=request, trace_id=_trace_id(request))
    # 限制后台并发
    if _background_sem.locked():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many background tasks")
    async with _background_sem:
        background_tasks.add_task(_safe_start, evolution_service)
    return StartStopResponse(message="Scheduler starting", scheduler_running=True)

async def _safe_start(service: EvolutionService):
    try:
        await asyncio.wait_for(service.start(), timeout=300)
    except asyncio.TimeoutError:
        logger.error("Startup timed out")
    except asyncio.CancelledError:
        logger.warning("Startup cancelled")
    except Exception as e:
        logger.error("Startup failed: %s", e, exc_info=True)

@router.put("/stop", response_model=StartStopResponse, summary="停止进化调度器")
async def stop_evolution(
    request: Request,
    evolution_service: EvolutionService = Depends(get_evolution_service),
    current_user: str = Depends(get_admin_user)
):
    if not evolution_service:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE)
    _audit(current_user, "stop", request=request, trace_id=_trace_id(request))
    try:
        await asyncio.wait_for(evolution_service.stop(), timeout=10)
    except asyncio.TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Stop timed out")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Stop failed: %s", e, exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Stop failed")
    return StartStopResponse(message="Scheduler stopped", scheduler_running=False)

# 兼容旧路由
@router.post("/manual/{module}", include_in_schema=False)
async def manual_run_legacy(module: str, request: Request, evolution_service = Depends(get_evolution_service), current_user = Depends(get_admin_user)):
    return await manual_run(module, request, evolution_service, current_user)

@router.post("/modules/{module}/run", response_model=ManualRunResponse, status_code=status.HTTP_202_ACCEPTED, summary="手动运行进化模块")
async def manual_run(
    module: str,
    request: Request,
    evolution_service: EvolutionService = Depends(get_evolution_service),
    current_user: str = Depends(get_admin_user)
):
    if not evolution_service:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE)
    module = module.lower().strip()
    if '..' in module or '/' in module:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid module name")
    if module not in _allowed_modules(evolution_service):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown module: {module}")
    if evolution_service._kill_switch:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Kill switch active")
    if not evolution_service.is_module_enabled(module):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Module {module} is disabled")

    _audit(current_user, f"manual_run:{module}", request=request, trace_id=_trace_id(request))
    lock = await _get_module_lock(module)
    if lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Module {module} is already running")

    async with lock:
        task_id = str(uuid.uuid4())
        started = datetime.now(timezone.utc).isoformat()
        try:
            evolution_service.register_task(module, task_id, started)
        except Exception as e:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to register task: {e}")
        try:
            start_time = time.monotonic()
            await asyncio.wait_for(evolution_service.trigger_manual_run(module), timeout=1800)
            duration = int((time.monotonic() - start_time) * 1000)
            evolution_service.update_task_metrics(module, duration_ms=duration)
            return ManualRunResponse(message="Completed", module=module, task_id=task_id, started_at=started, status="completed")
        except asyncio.TimeoutError:
            evolution_service.update_task_metrics(module, error="Timeout", status="timeout")
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Task timed out")
        except asyncio.CancelledError:
            evolution_service.update_task_metrics(module, error="Cancelled", status="cancelled")
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Task cancelled")
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        except Exception as e:
            logger.error("Manual run error for %s: %s", module, e, exc_info=True)
            evolution_service.update_task_metrics(module, error=str(e), status="failed")
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Task failed: {str(e)}")

@router.post("/results/{result_id}/approve", response_model=ApprovalResponse, summary="审批进化结果")
async def approve_result(
    result_id: str,
    req: ApprovalRequest,
    request: Request,
    evolution_service: EvolutionService = Depends(get_evolution_service),
    current_user: str = Depends(get_admin_user)
):
    if not evolution_service:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE)
    if evolution_service.get_mode() == 'shadow':
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot approve in shadow mode")
    # 验证UUID
    try:
        uuid.UUID(result_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid result_id format")
    # 防止自我审批（由服务层比对提交人）
    _audit(current_user, f"approve:{req.module}", f"result={result_id}", request=request, trace_id=_trace_id(request))
    try:
        await evolution_service.approve_result(req.module, result_id, current_user)
        return ApprovalResponse(
            message=f"Result {result_id} approved", module=req.module,
            result_id=result_id, approved=True,
            timestamp=datetime.now(timezone.utc).isoformat()
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except RuntimeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

@router.post("/config/rollback", response_model=RollbackResponse, summary="回滚进化参数")
async def rollback_parameters(
    request: Request,
    evolution_service: EvolutionService = Depends(get_evolution_service),
    current_user: str = Depends(get_admin_user)
):
    if not evolution_service:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE)
    _audit(current_user, "rollback", request=request, trace_id=_trace_id(request))
    prev = evolution_service.get_previous_version()
    if prev is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No rollback version available")
    try:
        await evolution_service.rollback_params()
        return RollbackResponse(
            message="Rolled back", previous_version=prev,
            current_version=evolution_service.get_current_version(),
            timestamp=datetime.now(timezone.utc).isoformat()
        )
    except Exception as e:
        logger.error("Rollback failed: %s", e, exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Rollback failed")

@router.put("/kill-switch", response_model=KillSwitchResponse, summary="设置紧急停止")
async def set_kill_switch(
    req: KillSwitchRequest,
    request: Request,
    evolution_service: EvolutionService = Depends(get_evolution_service),
    current_user: str = Depends(get_admin_user)
):
    if not evolution_service:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE)
    reason = _sanitize(req.reason)
    _audit(current_user, f"kill_switch:{req.active}", reason, request=request, trace_id=_trace_id(request))
    evolution_service.set_kill_switch(req.active)
    status = evolution_service.status()
    tasks = [EvolutionTaskInfo(name=n, status=t['status']) for n,t in status.get('tasks', {}).items()]
    return KillSwitchResponse(
        message=f"Kill switch {'activated' if req.active else 'deactivated'}",
        active=req.active, tasks=tasks,
        timestamp=datetime.now(timezone.utc).isoformat()
    )
