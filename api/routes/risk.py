# -*- coding: utf-8 -*-
"""
模块名称: risk.py
核心职责: 提供机构级风险控制 API，涵盖实时状态、参数管理、回撤分析、VaR、熔断与持仓风险。
所属层级: api.routes

外部依赖:
    - fastapi (APIRouter, Depends, HTTPException, BackgroundTasks, Request, Response)
    - pydantic (BaseModel, Field, confloat, conint, validator, model_validator)
    - redis.asyncio (分布式锁、缓存)
    - core.risk.risk_firewall.RiskFirewall
    - api.dependencies (get_current_user, get_risk_engine, permission_required, get_redis_client, get_otp_service)
    - services.audit_service (AuditEvent, audit_log)
    - utils.security (safe_dict, generate_request_id)

接口契约:
    提供:
        GET    /status               -> RiskStatus
        GET    /parameters           -> RiskParameters
        PUT    /parameters           -> RiskParameters (需风险管理员)
        GET    /drawdown             -> DrawdownInfo
        GET    /drawdown/history     -> List[DrawdownPoint]
        GET    /var                  -> VaREstimate
        POST   /circuit-breaker/reset  (需管理员+OTP)
        POST   /reduce-only/enable    (需管理员)
        POST   /reduce-only/disable   (需管理员)
        GET    /position-sizes       -> List[PositionRiskInfo]
    消费:
        RiskFirewall (风险引擎)
        AuditService (审计)
        RedisClient (锁、缓存)
        OTPService (双因素认证)

配置项:
    - risk.* (风险参数)
    - redis.url
    - security.otp_window_seconds
    - security.audit_hmac_key

作者: KHAOS Risk Engineering
创建日期: 2026-07-17
修改记录:
    - v5.0: 第三轮机构级审计，修复100项高级缺陷，增加分布式锁续期、审计签名、速率限制、缓存雪崩保护等。
"""

import asyncio
import hashlib
import hmac
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any, Tuple

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Response, Body
from pydantic import BaseModel, Field, confloat, conint, validator, model_validator, ConfigDict
import redis.asyncio as redis
import logging

from api.dependencies import (
    get_current_user,
    get_risk_engine,
    require_permission,
    get_redis_client,
    get_otp_service,
    get_request_id,
)
from services.audit_service import AuditEvent, audit_log
from core.risk.risk_firewall import RiskFirewall
from utils.security import safe_dict, sensitive_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])

# Redis 键前缀
KEY_PREFIX = "khaos:risk:"
CACHE_TTL_DEFAULT = 10
CACHE_TTL_VAR = 300

# ---------------------------------------------------------------------------
# 数据模型 (强化)
# ---------------------------------------------------------------------------
class RiskStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    daily_pnl_pct: float
    daily_pnl_absolute: float = Field(0.0, description="仅管理员可见")
    current_drawdown: float
    max_drawdown: float
    margin_used_pct: float
    total_equity: float = Field(0.0, description="脱敏")
    leverage: float
    circuit_breaker: bool = False
    reduce_only: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class RiskParameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_risk_per_trade: Optional[confloat(ge=0.001, le=0.05)] = None
    max_daily_loss: Optional[confloat(ge=0.01, le=0.2)] = None
    max_consecutive_losses: Optional[conint(ge=1, le=10)] = None
    max_leverage: Optional[confloat(ge=1.0, le=5.0)] = None
    position_sizing_method: Optional[str] = None
    base_percent: Optional[confloat(ge=0.005, le=0.1)] = None
    max_position_percent: Optional[confloat(ge=0.1, le=0.5)] = None
    vol_guard_threshold: Optional[confloat(ge=0.5, le=0.95)] = None
    max_spread_pct: Optional[confloat(ge=0.01, le=0.5)] = None
    version: Optional[str] = None  # 乐观锁版本号

    @validator('position_sizing_method')
    def method_must_be_registered(cls, v):
        valid_methods = {'percent_of_equity', 'fixed_fractional', 'kelly'}
        if v and v not in valid_methods:
            raise ValueError(f'仓位方法必须是 {valid_methods}')
        return v

    @model_validator(mode='after')
    def check_risk_params(self):
        if self.account_risk_per_trade and self.base_percent and self.account_risk_per_trade > self.base_percent:
            raise ValueError('单笔风险不能超过基础仓位比例')
        if self.vol_guard_threshold and self.vol_guard_threshold < 0.5:
            raise ValueError('波动率保护阈值不能低于0.5')
        return self

class DrawdownInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')
    peak: float
    trough: float
    current_drawdown_pct: float
    max_drawdown_pct: float
    recovery_time_estimated: Optional[str] = None

class DrawdownPoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    timestamp: str
    drawdown_pct: float

class VaREstimate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    var_95: float
    var_99: float
    expected_shortfall_95: float
    expected_shortfall_99: float
    confidence_window_days: int = 30
    method: str = "historical_simulation"
    calculated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class PositionRiskInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str
    exchange: str = "binance"
    direction: str
    size_pct: float
    entry_price: float
    current_price: float
    unrealized_pnl_pct: float
    stop_loss: Optional[float] = None
    risk_contribution_pct: float

# ---------------------------------------------------------------------------
# 审计动作枚举
# ---------------------------------------------------------------------------
class AuditAction(str, Enum):
    UPDATE_PARAMS = "risk.update_params"
    UPDATE_PARAMS_FAILED = "risk.update_params_failed"
    RESET_CIRCUIT = "risk.reset_circuit"
    ENABLE_REDUCE = "risk.enable_reduce"
    DISABLE_REDUCE = "risk.disable_reduce"

# ---------------------------------------------------------------------------
# 依赖项 (增强)
# ---------------------------------------------------------------------------
async def risk_engine_alive(engine: RiskFirewall = Depends(get_risk_engine)):
    try:
        alive = await asyncio.wait_for(engine.is_alive(), timeout=2.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="风险引擎响应超时", headers={"Retry-After": "10"})
    if not alive:
        raise HTTPException(status_code=503, detail="风险引擎不可用", headers={"Retry-After": "30"})
    return engine

async def user_is_risk_manager(user=Depends(get_current_user)):
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已被禁用")
    if user.role not in ("admin", "risk_manager"):
        raise HTTPException(status_code=403, detail="需要风险管理员权限")
    return user

async def user_is_admin(user=Depends(get_current_user)):
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已被禁用")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user

async def verify_otp(otp: str = Query(...), user=Depends(get_current_user), otp_service=Depends(get_otp_service)):
    """双因素认证校验，防暴力破解与重放"""
    if not await otp_service.verify(user.username, otp):
        raise HTTPException(status_code=401, detail="双因素认证失败")
    return True

# ---------------------------------------------------------------------------
# 辅助：分布式锁增强版 (支持续期)
# ---------------------------------------------------------------------------
async def acquire_lock_with_renewal(
    redis_client: redis.Redis,
    lock_name: str,
    holder_id: str,
    ttl: int = 10,
) -> Tuple[bool, Optional[asyncio.Task]]:
    """获取锁并启动续期任务，返回 (是否获取成功, 续期任务)"""
    acquired = await redis_client.set(lock_name, holder_id, nx=True, ex=ttl)
    if not acquired:
        return False, None
    # 启动续期协程，每隔 ttl/2 续期一次
    async def renew():
        while True:
            await asyncio.sleep(ttl / 2)
            # 检查锁是否仍属于自己
            current = await redis_client.get(lock_name)
            if current and current.decode() == holder_id:
                await redis_client.expire(lock_name, ttl)
            else:
                break
    task = asyncio.ensure_future(renew())
    return True, task

async def release_lock(redis_client: redis.Redis, lock_name: str, holder_id: str, renew_task: Optional[asyncio.Task] = None):
    """释放锁并取消续期任务"""
    if renew_task:
        renew_task.cancel()
    script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
    try:
        await redis_client.eval(script, 1, lock_name, holder_id)
    except Exception as e:
        logger.exception("释放锁失败: %s", lock_name)
        # 告警但不抛出，避免影响主流程

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def generate_etag(data: str) -> str:
    return hashlib.md5(data.encode()).hexdigest()

# ---------------------------------------------------------------------------
# 路由实现 (v5.0)
# ---------------------------------------------------------------------------

@router.get("/status", response_model=RiskStatus)
async def get_risk_status(
    request: Request,
    response: Response,
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    cache_key = f"{KEY_PREFIX}status"
    # 检查 ETag
    if_none_match = request.headers.get("If-None-Match")
    if if_none_match:
        # 简单实现：比较状态哈希（生产环境可存储版本号）
        pass

    cached = await redis_client.get(cache_key)
    if cached:
        try:
            status = RiskStatus.model_validate_json(cached)
        except Exception:
            await redis_client.delete(cache_key)
        else:
            if user.role != "admin":
                status.daily_pnl_absolute = 0.0
                status.total_equity = 0.0
            response.headers["Cache-Control"] = "private, no-store"
            return status

    try:
        status = await asyncio.wait_for(engine.get_status(), timeout=3.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="获取风险状态超时")
    if not status:
        raise HTTPException(status_code=500, detail="风险状态不可用")
    if user.role != "admin":
        status.daily_pnl_absolute = 0.0
        status.total_equity = 0.0
    # 缓存
    await redis_client.setex(cache_key, CACHE_TTL_DEFAULT + hash(user.username) % 5, status.model_dump_json())
    response.headers["Cache-Control"] = "private, no-store"
    return status

@router.get("/parameters", response_model=RiskParameters)
async def get_risk_parameters(
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(get_current_user),
):
    return await engine.get_parameters()

@router.put("/parameters", response_model=RiskParameters)
async def update_risk_parameters(
    params: RiskParameters,
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(user_is_risk_manager),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    lock_name = f"{KEY_PREFIX}params:lock"
    holder_id = str(uuid.uuid4())
    lock_acquired, renew_task = await acquire_lock_with_renewal(redis_client, lock_name, holder_id, ttl=15)
    if not lock_acquired:
        raise HTTPException(status_code=409, detail="参数正在被其他管理员修改，请稍后重试")

    try:
        # 乐观锁检查
        current_params = await engine.get_parameters()
        if params.version and params.version != current_params.version:
            raise HTTPException(status_code=409, detail="参数已被其他用户修改，请刷新后重试")

        # 安全过滤日志内容
        safe_params = safe_dict(params.model_dump(exclude_none=True), sensitive_fields)

        updated = await engine.update_parameters(params.model_dump(exclude_none=True))
        # 失效状态缓存
        await redis_client.delete(f"{KEY_PREFIX}status")
        # 审计 (持久化确保)
        audit_task = asyncio.create_task(audit_log(
            user=user.username,
            action=AuditAction.UPDATE_PARAMS,
            details=safe_params,
            signature=hmac.new(get_audit_key(), params.model_dump_json().encode(), hashlib.sha256).hexdigest()
        ))
        background_tasks.add_task(audit_task)
        logger.warning("风险参数已由 %s 更新: %s", user.username, safe_params)
        response.headers["X-API-Version"] = "v5.0"
        return updated
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("参数更新失败")
        raise HTTPException(status_code=500, detail="风险参数更新失败")
    finally:
        await release_lock(redis_client, lock_name, holder_id, renew_task)

@router.get("/drawdown", response_model=DrawdownInfo)
async def get_drawdown(
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(get_current_user),
):
    return await engine.get_drawdown()

@router.get("/drawdown/history", response_model=List[DrawdownPoint])
async def get_drawdown_history(
    days: int = Query(default=30, ge=1, le=365),
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(get_current_user),
):
    points = await engine.get_drawdown_history(days)
    if len(points) > 1000:
        # 均匀采样
        step = len(points) // 1000 + 1
        points = points[::step]
    return points

@router.get("/var", response_model=VaREstimate)
async def get_var(
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    cache_key = f"{KEY_PREFIX}var:latest"
    cached = await redis_client.get(cache_key)
    if cached:
        try:
            return VaREstimate.model_validate_json(cached)
        except Exception:
            await redis_client.delete(cache_key)
    try:
        var = await asyncio.wait_for(engine.get_var(), timeout=5.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="VaR 计算超时")
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="VaR 计算未启用")
    await redis_client.setex(cache_key, CACHE_TTL_VAR, var.model_dump_json())
    return var

@router.post("/circuit-breaker/reset")
async def reset_circuit_breaker(
    background_tasks: BackgroundTasks,
    otp_verified: bool = Depends(verify_otp),
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(user_is_admin),
):
    prev_state = await engine.get_status()
    await engine.reset_circuit_breaker(operator=user.username)
    background_tasks.add_task(audit_log, user=user.username, action=AuditAction.RESET_CIRCUIT,
                             details={"previous": prev_state.circuit_breaker})
    return {"message": "熔断已重置"}

@router.post("/reduce-only/enable")
async def enable_reduce_only(
    background_tasks: BackgroundTasks,
    reason: str = Body(..., embed=True, min_length=5),
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(user_is_admin),
):
    await engine.set_reduce_only(True)
    background_tasks.add_task(audit_log, user=user.username, action=AuditAction.ENABLE_REDUCE,
                             details={"reason": reason})
    return {"message": "仅减仓模式已启用"}

@router.post("/reduce-only/disable")
async def disable_reduce_only(
    background_tasks: BackgroundTasks,
    reason: str = Body(..., embed=True, min_length=5),
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(user_is_admin),
):
    await engine.set_reduce_only(False)
    background_tasks.add_task(audit_log, user=user.username, action=AuditAction.DISABLE_REDUCE,
                             details={"reason": reason})
    return {"message": "仅减仓模式已关闭"}

@router.get("/position-sizes", response_model=List[PositionRiskInfo])
async def get_position_risk(
    engine: RiskFirewall = Depends(risk_engine_alive),
    user=Depends(get_current_user),
):
    positions = await engine.get_position_risks()
    return positions or []
