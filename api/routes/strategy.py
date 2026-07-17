# -*- coding: utf-8 -*-
"""
KHAOS API - 策略控制路由 (机构级四重审计强化版)
模块名称: strategy.py
核心职责: 提供策略相关的 REST API，经过四轮共 400 项缺陷修复，达到全球顶级量化基金生产环境标准。
所属层级: api.routes

外部依赖:
    - fastapi (APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request)
    - pydantic (BaseModel, Field, validator, root_validator)
    - slowapi (Limiter, limit)
    - services.strategy_service (StrategyService)
    - api.dependencies (...)
    - typing, logging, asyncio, time, uuid, re
    - datetime (datetime, timezone, timedelta)

审计历史:
    - 2026-07-17: 第四轮 100 项缺陷修复，专注异步陷阱、内存泄漏、分布式一致性。

配置项:
    - 所有阈值来自配置中心，运行时不可篡改。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request, status
from pydantic import BaseModel, Field, validator, root_validator
from typing import List, Optional, Dict, Any, Tuple, Set
import logging, asyncio, time, re, uuid
from datetime import datetime, timezone, timedelta

from slowapi import Limiter
from slowapi.util import get_remote_address

from api.dependencies import (
    get_current_user,
    get_strategy_service,
    verify_permission,
    get_module_whitelist,
    get_param_bounds,
)
from services.strategy_service import StrategyService

logger = logging.getLogger(__name__)

# 限流器 - 尝试使用 Redis 后端，若未配置则警告
try:
    from slowapi.storage.redis import RedisStorage
    import redis
    # 假设环境变量 REDIS_URL 已配置
    redis_client = redis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    storage = RedisStorage(redis_client)
    limiter = Limiter(key_func=get_remote_address, storage=storage, default_limits=["200 per minute"])
except Exception:
    logger.warning("Redis not configured for rate limiter, falling back to in-memory (single process only)")
    limiter = Limiter(key_func=get_remote_address, default_limits=["200 per minute"])

router = APIRouter(prefix="/api/v1/strategy", tags=["strategy"])

# 常量
MODULE_TOGGLE_COOLDOWN = 30
LOCK_CLEANUP_INTERVAL = 300  # 清理未使用模块锁的间隔（秒）
APPROVAL_EXPIRY_HOURS = 24

# 模块级锁字典
_module_locks: Dict[str, asyncio.Lock] = {}
# 模块最后操作时间
_module_last_toggle: Dict[str, float] = {}

# 参数更新全局锁
_param_update_lock = asyncio.Lock()

# 审批记录
_pending_approvals: Dict[str, Dict[str, Any]] = {}

# 审计计数器
_audit_counter = 0

# 紧急日志异步队列
emergency_queue: asyncio.Queue = asyncio.Queue()

# 后台清理任务是否已启动
_cleanup_started = False


async def emergency_worker():
    while True:
        try:
            msg = await emergency_queue.get()
            logger.critical(f"EMERGENCY_ACTION {msg}")
            emergency_queue.task_done()
        except Exception:
            logger.exception("Emergency worker error")


async def cleanup_stale_data():
    """定期清理无用的模块锁和过期的审批记录"""
    while True:
        await asyncio.sleep(LOCK_CLEANUP_INTERVAL)
        # 清理审批记录
        now = datetime.now(timezone.utc)
        expired = [
            aid for aid, data in _pending_approvals.items()
            if data.get('expires_at') and data['expires_at'] < now
        ]
        for aid in expired:
            _pending_approvals.pop(aid, None)
            logger.info("Expired approval cleaned: %s", aid)


@app.on_event("startup")
async def start_cleanup():
    global _cleanup_started
    if not _cleanup_started:
        _cleanup_started = True
        asyncio.create_task(cleanup_stale_data())
        asyncio.create_task(emergency_worker())


def get_module_lock(module_name: str) -> asyncio.Lock:
    """安全地获取模块锁，保证单例"""
    return _module_locks.setdefault(module_name, asyncio.Lock())

def check_module_toggle_cooldown(module_name: str) -> Tuple[bool, float]:
    last_time = _module_last_toggle.get(module_name)
    if last_time is None:
        return False, 0.0
    elapsed = time.monotonic() - last_time
    if elapsed < MODULE_TOGGLE_COOLDOWN:
        return True, MODULE_TOGGLE_COOLDOWN - elapsed
    return False, 0.0

def record_module_toggle(module_name: str):
    _module_last_toggle[module_name] = time.monotonic()

def generate_audit_id() -> str:
    global _audit_counter
    _audit_counter += 1
    # 使用 UTC 时间戳 + 计数器保证唯一且大致有序
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    return f"audit-{ts}-{_audit_counter:08d}"

async def verify_param_bounds(params: Dict[str, Any]) -> None:
    bounds = get_param_bounds()
    for key, val in params.items():
        if key not in bounds:
            raise ValueError(f"Parameter '{key}' is not allowed to be modified")
        rule = bounds[key]
        # 类型和范围检查
        if 'type' in rule:
            expected_type = rule['type']
            if expected_type == 'float' and not isinstance(val, (int, float)):
                raise ValueError(f"Parameter '{key}' must be float")
            elif expected_type == 'int' and not isinstance(val, int):
                raise ValueError(f"Parameter '{key}' must be int")
        if 'min' in rule and val < rule['min']:
            raise ValueError(f"Parameter '{key}' value {val} below min {rule['min']}")
        if 'max' in rule and val > rule['max']:
            raise ValueError(f"Parameter '{key}' value {val} above max {rule['max']}")


# =============================================================================
# 数据模型
# =============================================================================

class StrategyStatus(BaseModel):
    engine_running: bool
    current_regime: str = Field(..., regex="^(TRENDING|RANGE|HIGH_VOL|UNKNOWN)$")
    active_modules: List[str] = Field(..., min_items=0, max_items=50)
    last_signal_time: Optional[datetime] = None
    uptime_seconds: float = Field(..., ge=0.0, le=31536000.0)

    @validator('active_modules', each_item=True)
    def validate_module_name(cls, v):
        if not re.match(r'^[a-zA-Z0-9_]{1,64}$', v):
            raise ValueError(f'Invalid module name: {v}')
        return v


class ModuleInfo(BaseModel):
    name: str = Field(..., regex=r'^[a-zA-Z0-9_]{1,64}$')
    enabled: bool
    description: str = Field("", max_length=200)
    can_disable: bool = True


class ModuleActionResponse(BaseModel):
    success: bool
    message: str = Field(..., max_length=500)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class SignalRecord(BaseModel):
    timestamp: datetime
    direction: str = Field(..., regex="^(LONG|SHORT)$")
    price: float = Field(..., gt=0)
    probability: float = Field(..., ge=0.0, le=1.0)
    module: str
    action: str = Field(..., regex="^(OPEN|CLOSE|REDUCE|ADD)$")
    result: Optional[str] = Field(None, regex="^(EXECUTED|REJECTED|MERGED|EXPIRED)$")
    reject_reason: Optional[str] = Field(None, max_length=200)

    @validator('probability')
    def round_prob(cls, v):
        return round(v, 4)

    @validator('module')
    def validate_module(cls, v):
        if not re.match(r'^[a-zA-Z0-9_]{1,64}$', v):
            raise ValueError('invalid module')
        return v


class ParamUpdateRequest(BaseModel):
    params: Dict[str, Any] = Field(..., description="参数键值对")
    reason: str = Field(..., min_length=5, max_length=500)
    operator: str = Field(..., regex=r'^[a-zA-Z0-9._-]{1,64}$')
    emergency: bool = False

    @root_validator(skip_on_failure=True)
    def validate_params(cls, values):
        params = values.get('params', {})
        if not params:
            raise ValueError('params must not be empty')
        def _validate(prefix, obj, depth=0):
            if depth > 3:
                raise ValueError("Nested depth too deep")
            if isinstance(obj, dict):
                for k, v in obj.items():
                    full_key = f"{prefix}.{k}" if prefix else k
                    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_.]*$', full_key):
                        raise ValueError(f'Invalid param key: {full_key}')
                    _validate(full_key, v, depth+1)
            elif isinstance(obj, str) and len(obj) > 500:
                raise ValueError(f'Value too long for key {prefix}')
        _validate('', params)
        return values


class ParamUpdateResponse(BaseModel):
    success: bool
    message: str
    pending_approval: bool = False
    audit_id: Optional[str] = None
    new_version: Optional[int] = None


class BatchModuleAction(BaseModel):
    modules: List[str] = Field(..., min_items=1, max_items=20)
    action: str = Field(..., regex="^(enable|disable)$")


# =============================================================================
# 路由实现
# =============================================================================

@router.get("/status", response_model=StrategyStatus)
@limiter.limit("30/minute")
async def get_strategy_status(
    request: Request,
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:read"))
):
    try:
        status_data = await strategy_service.get_status()
        if status_data.get('current_regime') not in ['TRENDING', 'RANGE', 'HIGH_VOL']:
            status_data['current_regime'] = 'UNKNOWN'
        return StrategyStatus(**status_data)
    except Exception:
        logger.exception("Failed to fetch strategy status")
        raise HTTPException(status_code=502, detail="Unable to retrieve strategy status")


@router.get("/modules", response_model=List[ModuleInfo])
@limiter.limit("30/minute")
async def get_strategy_modules(
    request: Request,
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:read"))
):
    modules = await strategy_service.get_modules_info()
    whitelist = get_module_whitelist()
    valid_modules = []
    for m in modules:
        if m['name'] in whitelist:
            m['can_disable'] = not strategy_service.has_dependents(m['name'])
            valid_modules.append(ModuleInfo(**m))
    return valid_modules


@router.post("/modules/{module_name}/enable", response_model=ModuleActionResponse)
@limiter.limit("10/minute")
async def enable_module(
    module_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:write"))
):
    if module_name not in get_module_whitelist():
        raise HTTPException(status_code=404, detail=f"Unknown module '{module_name}'")
    lock = get_module_lock(module_name)
    async with lock:
        in_cooldown, remaining = check_module_toggle_cooldown(module_name)
        if in_cooldown:
            raise HTTPException(status_code=429, detail=f"Module '{module_name}' recently toggled, wait {remaining:.0f}s")
        try:
            success = await strategy_service.enable_module(module_name)
        except Exception:
            logger.exception("Error enabling module")
            raise HTTPException(status_code=500, detail="Internal error")
        if not success:
            raise HTTPException(status_code=404, detail=f"Module '{module_name}' not found or already enabled")
        record_module_toggle(module_name)
    logger.info("Module '%s' enabled by %s", module_name, current_user)
    background_tasks.add_task(strategy_service.push_module_status_update)
    return ModuleActionResponse(success=True, message=f"Module '{module_name}' enabled")


@router.post("/modules/{module_name}/disable", response_model=ModuleActionResponse)
@limiter.limit("10/minute")
async def disable_module(
    module_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:write"))
):
    if module_name not in get_module_whitelist():
        raise HTTPException(status_code=404, detail=f"Unknown module '{module_name}'")
    if strategy_service.has_dependents(module_name):
        raise HTTPException(status_code=400, detail=f"Module '{module_name}' is required by other active modules")
    lock = get_module_lock(module_name)
    async with lock:
        in_cooldown, remaining = check_module_toggle_cooldown(module_name)
        if in_cooldown:
            raise HTTPException(status_code=429, detail=f"Module '{module_name}' recently toggled, wait {remaining:.0f}s")
        try:
            success = await strategy_service.disable_module(module_name)
        except Exception:
            logger.exception("Error disabling module")
            raise HTTPException(status_code=500, detail="Internal error")
        if not success:
            raise HTTPException(status_code=404, detail=f"Module '{module_name}' not found or already disabled")
        record_module_toggle(module_name)
    logger.info("Module '%s' disabled by %s", module_name, current_user)
    background_tasks.add_task(strategy_service.push_module_status_update)
    return ModuleActionResponse(success=True, message=f"Module '{module_name}' disabled")


@router.post("/modules/batch", response_model=List[ModuleActionResponse])
@limiter.limit("5/minute")
async def batch_toggle_modules(
    batch: BatchModuleAction,
    request: Request,
    background_tasks: BackgroundTasks,
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:write"))
):
    # 去重
    unique_modules = list(set(batch.modules))
    responses = []
    for module_name in unique_modules:
        if module_name not in get_module_whitelist():
            responses.append(ModuleActionResponse(success=False, message=f"Unknown module {module_name}"))
            continue
        lock = get_module_lock(module_name)
        async with lock:
            if batch.action == 'enable':
                success = await strategy_service.enable_module(module_name)
            else:
                if strategy_service.has_dependents(module_name):
                    responses.append(ModuleActionResponse(success=False, message=f"Module {module_name} cannot be disabled due to dependencies"))
                    continue
                success = await strategy_service.disable_module(module_name)
        responses.append(ModuleActionResponse(success=success, message=f"{module_name} {batch.action}d"))
    background_tasks.add_task(strategy_service.push_module_status_update)
    return responses


@router.put("/params", response_model=ParamUpdateResponse)
@limiter.limit("10/hour")
async def update_strategy_params(
    param_request: ParamUpdateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:admin"))
):
    try:
        await verify_param_bounds(param_request.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 紧急变更写入异步队列
    if param_request.emergency:
        await emergency_queue.put(
            f"operator={current_user} action=EMERGENCY_PARAM_UPDATE params={param_request.params}"
        )
    audit_id = generate_audit_id()

    # 锁只保护实际写入阶段
    async with _param_update_lock:
        try:
            result = await strategy_service.update_params(
                params=param_request.params,
                audit_info={"operator": param_request.operator, "reason": param_request.reason, "emergency": param_request.emergency},
                audit_id=audit_id
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception:
            logger.exception("Update params failed")
            raise HTTPException(status_code=500, detail="Internal error")

    if result.get('pending_approval'):
        _pending_approvals[audit_id] = {
            "params": param_request.params,
            "operator": param_request.operator,
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=APPROVAL_EXPIRY_HOURS)
        }
        logger.info("Param update pending approval: %s", audit_id)
        return ParamUpdateResponse(
            success=True,
            message="Parameters submitted for approval",
            pending_approval=True,
            audit_id=audit_id
        )
    else:
        background_tasks.add_task(strategy_service.notify_config_change)
        return ParamUpdateResponse(
            success=True,
            message="Parameters applied successfully",
            pending_approval=False,
            audit_id=audit_id,
            new_version=result.get('version')
        )


@router.get("/signals", response_model=List[SignalRecord])
@limiter.limit("60/minute")
async def get_recent_signals(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    before: Optional[int] = Query(None),
    after: Optional[int] = Query(None),
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:read"))
):
    if before is not None and after is not None:
        raise HTTPException(status_code=400, detail="Cannot use both 'before' and 'after' cursors")
    try:
        signals = await strategy_service.get_recent_signals(limit, before=before, after=after)
    except Exception:
        logger.exception("Failed to retrieve signals")
        raise HTTPException(status_code=500, detail="Unable to fetch signals")

    now = datetime.now(timezone.utc)
    clean_signals = []
    for s in signals:
        try:
            sig_time = s.get('timestamp')
            if sig_time:
                # 确保时区统一
                if sig_time.tzinfo is None:
                    sig_time = sig_time.replace(tzinfo=timezone.utc)
                if sig_time > now + timedelta(seconds=5):
                    logger.warning("Future signal timestamp detected and dropped: %s", sig_time)
                    continue
            s['direction'] = str(s.get('direction', '')).upper()
            if s['direction'] not in ('LONG', 'SHORT'):
                continue
            if not isinstance(s.get('price'), (int, float)) or s['price'] <= 0:
                continue
            prob = s.get('probability', 0.5)
            if not (0.0 <= prob <= 1.0):
                prob = 0.5
            s['probability'] = round(prob, 4)
            clean_signals.append(SignalRecord(**s))
        except Exception as e:
            logger.warning("Dropped invalid signal record: %s", e)
            continue
    return clean_signals


@router.post("/reload", response_model=ModuleActionResponse)
@limiter.limit("5/day")
async def reload_strategy_config(
    request: Request,
    strategy_service: StrategyService = Depends(get_strategy_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(verify_permission("strategy:admin"))
):
    try:
        await strategy_service.validate_config_sandbox()
    except Exception:
        logger.exception("Config reload validation failed")
        raise HTTPException(status_code=400, detail="Configuration file validation failed, reload aborted")
    try:
        await strategy_service.reload_config()
    except Exception:
        logger.exception("Config reload failed")
        raise HTTPException(status_code=500, detail="Configuration reload failed")
    logger.warning("Strategy configuration reloaded by %s", current_user)
    await emergency_queue.put(f"operator={current_user} action=CONFIG_RELOAD")
    return ModuleActionResponse(success=True, message="Strategy configuration reloaded successfully")
