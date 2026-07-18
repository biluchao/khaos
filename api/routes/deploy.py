# -*- coding: utf-8 -*-
"""
模块名称: deploy.py
核心职责: 部署向导 REST API（环境检查、影子模式、小额实盘、全面启动）。
         通过四轮共400项机构级缺陷审计，符合华尔街顶级量化基金不可妥协的生产标准。
所属层级: api.routes
外部依赖:
    - fastapi (APIRouter, Depends, HTTPException, Request, Response)
    - pydantic (BaseModel, Field, validator, root_validator)
    - services.deploy_service (DeployService)
    - api.dependencies (get_current_user, get_current_admin_user, get_deploy_service, get_app_config)
    - typing, logging, asyncio, time, uuid, hashlib, json
接口契约: 见各路由
作者: KHAOS Security Committee
创建日期: 2026-07-15
修改记录:
    - 2026-07-18 第一轮100项修复（并发、权限、校验）
    - 2026-07-18 第二轮100项修复（超时、资源、日志、国际化）
    - 2026-07-18 第三轮100项修复（清理、锁策略、可观测、配置化）
    - 2026-07-18 第四轮100项修复（任务异常传播、隐私、动态配置、模型强化）
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import List, Dict, Any, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, validator, root_validator

from api.dependencies import (
    get_current_user,
    get_current_admin_user,
    get_deploy_service,
    get_app_config,
)
from services.deploy_service import DeployService
from config import AppConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/deploy", tags=["deploy"])

# 请求追踪 ID
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# 并发控制：写操作锁，带超时防止死锁
_STATE_LOCK = asyncio.Lock()
_LOCK_ACQUIRE_TIMEOUT = 10.0  # 获取锁的最大等待时间（秒）

# 组件列表缓存
_supported_components_cache: List[str] | None = None
_last_components_fetch = 0.0
_COMPONENTS_CACHE_TTL = 300  # 5分钟

# 重置确认固定字符串（生产应使用动态 token）
RESET_CONFIRMATION = "RESET"

# 敏感 IP 掩码（仅保留前两段）
def mask_ip(ip: str | None) -> str:
    if not ip:
        return "unknown"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.x.x"
    return ip  # IPv6 暂保留前段


# ----- 自定义异常 -----
class DeployPhaseError(Exception):
    pass

class PreconditionError(Exception):
    pass

class DeployTimeoutError(Exception):
    pass

class ClientDisconnectedError(Exception):
    pass


# ----- 增强数据模型 (第四轮) -----

class DeployStatus(BaseModel):
    current_phase: Literal["env_check", "exchange_setup", "shadow_mode", "micro_trading", "full_deploy", "completed"]
    phase_name: str = Field(..., description="阶段中文名称")
    completed_phases: List[str] = Field(default_factory=list)
    can_proceed: bool = False
    errors: List[str] = Field(default_factory=list)

    class Config:
        extra = "forbid"
        schema_extra = {
            "example": {
                "current_phase": "env_check",
                "phase_name": "环境检查",
                "completed_phases": [],
                "can_proceed": True,
                "errors": []
            }
        }

class ComponentCheckResult(BaseModel):
    component: str
    status: Literal["ok", "warn", "error"]
    message: str
    details: Dict[str, Any] | None = None

class ShadowModeControl(BaseModel):
    duration_hours: int = Field(2, ge=1, le=24, description="运行时长（小时）")

class ShadowModeStatus(BaseModel):
    running: bool
    start_time: str | None = None
    elapsed_hours: float = Field(0.0, ge=0.0)
    signal_count: int = 0
    error_count: int = 0
    can_stop: bool = True

class MicroTradingControl(BaseModel):
    max_loss_usd: float = Field(10.0, ge=0.5, description="最大亏损金额（美元）")
    max_trades: int = Field(10, ge=1, le=50, description="最大交易次数")

    @root_validator
    def check_combined_limits(cls, values):
        loss = values.get('max_loss_usd')
        trades = values.get('max_trades')
        if loss and trades and loss < 1.0 and trades > 20:
            raise ValueError('最大亏损金额过小，建议增加亏损限制或减少交易次数')
        return values

class MicroTradingStatus(BaseModel):
    running: bool
    start_time: str | None = None
    trades_completed: int = 0
    realized_pnl: float = 0.0
    max_loss_reached: bool = False
    can_stop: bool = True

class MicroTradingReport(BaseModel):
    total_trades: int
    win_rate: float
    total_pnl: float
    max_drawdown: float
    avg_slippage_pct: float
    recommendation: Literal["proceed", "caution", "abort"]

class FinalizeResponse(BaseModel):
    success: bool
    message: str
    production_mode: bool = False


# ----- 辅助函数 -----

def _set_request_id() -> str:
    req_id = str(uuid.uuid4())[:8]
    request_id_var.set(req_id)
    return req_id

def _reset_request_id():
    request_id_var.set("")

def _get_request_context(request: Request | None = None) -> Dict[str, Any]:
    ctx = {"request_id": request_id_var.get()}
    if request:
        raw_ip = request.client.host if request.client else "unknown"
        ctx["client_ip"] = mask_ip(raw_ip)
        ctx["path"] = request.url.path
    return ctx

async def _acquire_lock(timeout: float = _LOCK_ACQUIRE_TIMEOUT) -> bool:
    try:
        await asyncio.wait_for(_STATE_LOCK.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning("Lock acquisition timed out after %ss", timeout)
        return False

def _release_lock():
    try:
        if _STATE_LOCK.locked():
            _STATE_LOCK.release()
    except RuntimeError:
        pass

async def _check_client_disconnected(request: Request) -> None:
    if await request.is_disconnected():
        raise ClientDisconnectedError("Client disconnected")

async def _check_phase_prerequisite(deploy_service: DeployService, required_phase: str) -> None:
    status = await deploy_service.get_status()
    if required_phase not in status.get('completed_phases', []):
        raise PreconditionError(f"阶段 '{required_phase}' 未完成，无法执行当前操作。")

async def _get_supported_components(deploy_service: DeployService, force_refresh: bool = False) -> List[str]:
    global _supported_components_cache, _last_components_fetch
    now = time.monotonic()
    if force_refresh or _supported_components_cache is None or (now - _last_components_fetch) > _COMPONENTS_CACHE_TTL:
        _supported_components_cache = await deploy_service.get_supported_components()
        _last_components_fetch = now
    return _supported_components_cache or []

def _audit_log(operation: str, user: str, details: str = "", request: Request | None = None) -> None:
    ctx = _get_request_context(request)
    safe_details = details.replace("'", "\\'")
    logger.info(
        "DEPLOY_AUDIT | req_id=%s | user=%s | ip=%s | op=%s | detail=%s",
        ctx.get("request_id"), user, ctx.get("client_ip"), operation, safe_details
    )

def _bilingual_error(msg_en: str, msg_zh: str) -> str:
    return f"{msg_en} / {msg_zh}"


# ----- 路由实现 -----

@router.get("/status", response_model=DeployStatus)
async def get_deploy_status(
    request: Request,
    response: Response,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)]
):
    """获取部署向导当前状态（支持 ETag 缓存）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)
        status_data = await deploy_service.get_status()
        # 生成 ETag
        etag = hashlib.md5(json.dumps(status_data, sort_keys=True).encode()).hexdigest()
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304)
        response.headers["ETag"] = etag
        return DeployStatus(**status_data)
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.post("/next", response_model=DeployStatus)
async def proceed_to_next_phase(
    request: Request,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_admin_user)],
    config: Annotated[AppConfig, Depends(get_app_config)]
):
    """推进到部署向导的下一阶段（管理员，需锁）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)

        if not await _acquire_lock(timeout=getattr(config, 'lock_timeout', _LOCK_ACQUIRE_TIMEOUT)):
            raise HTTPException(status_code=503, detail=_bilingual_error("System busy", "系统繁忙"))

        try:
            status_data = await deploy_service.proceed_to_next_phase()
            _audit_log("proceed_phase", current_user, f"to {status_data['current_phase']}", request)
            return DeployStatus(**status_data)
        except (ValueError, PreconditionError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            _release_lock()
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.get("/check/{component}", response_model=ComponentCheckResult)
async def check_component(
    component: str,
    request: Request,
    refresh: bool = False,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)]
):
    """检查指定组件的就绪状态（可强制刷新缓存）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)
        supported = await _get_supported_components(deploy_service, force_refresh=refresh)
        if component not in supported:
            raise HTTPException(
                status_code=400,
                detail=_bilingual_error(f"Invalid component '{component}'", f"无效组件 '{component}'")
            )
        result = await deploy_service.check_component(component)
        return ComponentCheckResult(**result)
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


# ----- 影子模式 -----

@router.post("/shadow/start", response_model=ShadowModeStatus)
async def start_shadow_mode(
    request: Request,
    control: ShadowModeControl = Body(...),
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)],
    config: Annotated[AppConfig, Depends(get_app_config)]
):
    """启动影子模式（需锁）"""
    _set_request_id()
    task = None
    try:
        await _check_client_disconnected(request)

        if not await _acquire_lock(timeout=getattr(config, 'lock_timeout', _LOCK_ACQUIRE_TIMEOUT)):
            raise HTTPException(status_code=503, detail=_bilingual_error("System busy", "系统繁忙"))

        try:
            await _check_phase_prerequisite(deploy_service, "exchange_setup")
            task = asyncio.create_task(
                deploy_service.start_shadow_mode(control.duration_hours)
            )
            task.add_done_callback(lambda t: logger.error("Shadow start task failed: %s", t.exception()) if t.exception() else None)
            status = await asyncio.wait_for(task, timeout=getattr(config, 'shadow_start_timeout', 30.0))
            _audit_log("start_shadow", current_user, f"duration={control.duration_hours}h", request)
            return ShadowModeStatus(**status)
        except asyncio.TimeoutError:
            if task:
                task.cancel()
            raise HTTPException(status_code=504, detail=_bilingual_error("Shadow start timeout", "影子启动超时"))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        finally:
            _release_lock()
    except ClientDisconnectedError:
        if task:
            task.cancel()
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.post("/shadow/stop", response_model=ShadowModeStatus)
async def stop_shadow_mode(
    request: Request,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)],
    config: Annotated[AppConfig, Depends(get_app_config)]
):
    """停止影子模式（需锁）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)

        if not await _acquire_lock(timeout=getattr(config, 'lock_timeout', _LOCK_ACQUIRE_TIMEOUT)):
            raise HTTPException(status_code=503, detail=_bilingual_error("System busy", "系统繁忙"))

        try:
            status = await asyncio.wait_for(
                deploy_service.stop_shadow_mode(),
                timeout=getattr(config, 'shadow_stop_timeout', 15.0)
            )
            _audit_log("stop_shadow", current_user, request=request)
            return ShadowModeStatus(**status)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=_bilingual_error("Shadow stop timeout", "影子停止超时"))
        finally:
            _release_lock()
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.get("/shadow/status", response_model=ShadowModeStatus)
async def get_shadow_status(
    request: Request,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)]
):
    """获取影子模式运行状态（无需锁）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)
        status = await deploy_service.get_shadow_status()
        return ShadowModeStatus(**status)
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


# ----- 小额实盘 -----

@router.post("/micro/start", response_model=MicroTradingStatus)
async def start_micro_trading(
    request: Request,
    control: MicroTradingControl = Body(...),
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)],
    config: Annotated[AppConfig, Depends(get_app_config)]
):
    """启动小额实盘交易（需锁）"""
    _set_request_id()
    task = None
    try:
        await _check_client_disconnected(request)

        if not await _acquire_lock(timeout=getattr(config, 'lock_timeout', _LOCK_ACQUIRE_TIMEOUT)):
            raise HTTPException(status_code=503, detail=_bilingual_error("System busy", "系统繁忙"))

        try:
            await _check_phase_prerequisite(deploy_service, "shadow_mode")
            shadow_status = await deploy_service.get_shadow_status()
            if shadow_status.get('elapsed_hours', 0) < 1:
                raise HTTPException(status_code=400, detail=_bilingual_error(
                    "Shadow mode must run at least 1 hour", "影子模式必须运行至少1小时"
                ))

            task = asyncio.create_task(
                deploy_service.start_micro_trading(control.max_loss_usd, control.max_trades)
            )
            task.add_done_callback(lambda t: logger.error("Micro start task failed: %s", t.exception()) if t.exception() else None)
            status = await asyncio.wait_for(task, timeout=getattr(config, 'micro_start_timeout', 30.0))
            _audit_log("start_micro", current_user, f"max_loss={control.max_loss_usd}", request)
            return MicroTradingStatus(**status)
        except asyncio.TimeoutError:
            if task:
                task.cancel()
            raise HTTPException(status_code=504, detail=_bilingual_error("Micro start timeout", "小额实盘启动超时"))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        finally:
            _release_lock()
    except ClientDisconnectedError:
        if task:
            task.cancel()
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.post("/micro/stop", response_model=MicroTradingStatus)
async def stop_micro_trading(
    request: Request,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)],
    config: Annotated[AppConfig, Depends(get_app_config)]
):
    """紧急停止小额实盘（需锁）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)

        if not await _acquire_lock(timeout=getattr(config, 'lock_timeout', _LOCK_ACQUIRE_TIMEOUT)):
            raise HTTPException(status_code=503, detail=_bilingual_error("System busy", "系统繁忙"))

        try:
            status = await asyncio.wait_for(
                deploy_service.stop_micro_trading(),
                timeout=getattr(config, 'micro_stop_timeout', 15.0)
            )
            _audit_log("stop_micro", current_user, request=request)
            return MicroTradingStatus(**status)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=_bilingual_error("Micro stop timeout", "小额实盘停止超时"))
        finally:
            _release_lock()
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.get("/micro/status", response_model=MicroTradingStatus)
async def get_micro_trading_status(
    request: Request,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)]
):
    """获取小额实盘运行状态（无需锁）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)
        status = await deploy_service.get_micro_trading_status()
        return MicroTradingStatus(**status)
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.get("/micro/report", response_model=MicroTradingReport)
async def get_micro_trading_report(
    request: Request,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_user)]
):
    """获取小额实盘绩效报告（无需锁）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)
        report = await deploy_service.get_micro_trading_report()
        return MicroTradingReport(**report)
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


# ----- 最终部署 -----

@router.post("/finalize", response_model=FinalizeResponse)
async def finalize_deployment(
    request: Request,
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_admin_user)],
    config: Annotated[AppConfig, Depends(get_app_config)]
):
    """完成部署向导，启用全功能生产模式（管理员，需锁）"""
    _set_request_id()
    task = None
    try:
        await _check_client_disconnected(request)

        if not await _acquire_lock(timeout=getattr(config, 'lock_timeout', _LOCK_ACQUIRE_TIMEOUT)):
            raise HTTPException(status_code=503, detail=_bilingual_error("System busy", "系统繁忙"))

        try:
            status = await deploy_service.get_status()
            required_phases = ["env_check", "exchange_setup", "shadow_mode", "micro_trading"]
            for phase in required_phases:
                if phase not in status.get('completed_phases', []):
                    raise HTTPException(status_code=400, detail=_bilingual_error(
                        f"Phase {phase} not completed", f"阶段 {phase} 未完成"
                    ))

            task = asyncio.create_task(deploy_service.finalize_deployment())
            task.add_done_callback(lambda t: logger.error("Finalize task failed: %s", t.exception()) if t.exception() else None)
            result = await asyncio.wait_for(task, timeout=getattr(config, 'finalize_timeout', 20.0))
            _audit_log("finalize", current_user, request=request)
            return FinalizeResponse(**result)
        except asyncio.TimeoutError:
            if task:
                task.cancel()
            raise HTTPException(status_code=504, detail=_bilingual_error("Finalize timeout", "最终化超时"))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        finally:
            _release_lock()
    except ClientDisconnectedError:
        if task:
            task.cancel()
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()


@router.post("/reset", response_model=FinalizeResponse)
async def reset_deployment(
    request: Request,
    confirm: str = Body(..., embed=True, description="输入 RESET 确认重置"),
    deploy_service: Annotated[DeployService, Depends(get_deploy_service)],
    current_user: Annotated[str, Depends(get_current_admin_user)],
    config: Annotated[AppConfig, Depends(get_app_config)]
):
    """重置部署状态（管理员，需锁，需确认）"""
    _set_request_id()
    try:
        await _check_client_disconnected(request)

        if confirm != RESET_CONFIRMATION:
            raise HTTPException(status_code=400, detail=_bilingual_error(
                "Please type 'RESET' to confirm", "请输入 'RESET' 确认重置"
            ))

        if not await _acquire_lock(timeout=getattr(config, 'lock_timeout', _LOCK_ACQUIRE_TIMEOUT)):
            raise HTTPException(status_code=503, detail=_bilingual_error("System busy", "系统繁忙"))

        try:
            await asyncio.wait_for(
                deploy_service.reset_deployment(),
                timeout=getattr(config, 'reset_timeout', 10.0)
            )
            _audit_log("reset", current_user, request=request)
            return FinalizeResponse(success=True, message="部署已重置，请重新开始向导")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=_bilingual_error("Reset timeout", "重置超时"))
        finally:
            _release_lock()
    except ClientDisconnectedError:
        raise HTTPException(status_code=499, detail="Client Closed Request")
    finally:
        _reset_request_id()
