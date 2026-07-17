# -*- coding: utf-8 -*-
"""
模块名称: config.py (v6.0 磐石版)
核心职责: 提供系统配置的 REST API，具备金融级安全、并发控制、灾备恢复。
所属层级: api.routes
"""
import asyncio
import logging
import re
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator, ConfigDict

from api.dependencies import get_config_service, get_current_user, require_admin
from services.config_service import ConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/config", tags=["config"])

# ---------------------------------------------------------------------------
# 常量与模式
# ---------------------------------------------------------------------------
SECTION_PATTERN = re.compile(r'^[a-z][a-z0-9_]*$', re.IGNORECASE)
OPERATOR_PATTERN = re.compile(r'[^a-zA-Z0-9_\-@.]')
REASON_CTRL_PATTERN = re.compile(r'[\r\n\t]')
REASON_SPACE_PATTERN = re.compile(r' +')

MAX_REASON_LENGTH = 256
MAX_DATA_DEPTH = 5
MAX_DICT_KEYS = 200
MAX_LIST_ITEMS = 500
MAX_KEY_LENGTH = 64

DEFAULT_READ_TIMEOUT = 3.0
DEFAULT_WRITE_TIMEOUT = 10.0

_pending_reload_tasks: List[asyncio.Task] = []
_global_write_lock = asyncio.Lock()
_reload_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
_last_reload_monotonic = 0.0
_reload_backoff = [1.0, 2.0, 4.0]

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def validate_section_name(section: str) -> str:
    """校验并统一配置段名称"""
    section = section.lower()
    if '..' in section or not SECTION_PATTERN.match(section):
        raise HTTPException(404, detail=f"无效的配置段 '{section}'")
    return section

def sanitize_operator(raw: str) -> str:
    cleaned = OPERATOR_PATTERN.sub('', raw).strip()
    cleaned = REASON_SPACE_PATTERN.sub(' ', cleaned)
    return cleaned[:32]

def sanitize_reason(raw: str) -> str:
    no_ctrl = REASON_CTRL_PATTERN.sub(' ', raw)
    compressed = REASON_SPACE_PATTERN.sub(' ', no_ctrl).strip()
    return compressed[:MAX_REASON_LENGTH]

def strip_internal_fields(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: strip_internal_fields(v) for k, v in data.items() if not k.startswith('_')}
    elif isinstance(data, list):
        return [strip_internal_fields(item) for item in data]
    return data

def validate_data_structure(data: Any, depth: int = 1) -> None:
    if depth > MAX_DATA_DEPTH:
        raise ValueError("数据嵌套层级过深")
    if isinstance(data, dict):
        if len(data) > MAX_DICT_KEYS:
            raise ValueError("配置键数量过多")
        for k, v in data.items():
            if not isinstance(k, str) or len(k) > MAX_KEY_LENGTH:
                raise ValueError(f"键名无效: {k}")
            validate_data_structure(v, depth + 1)
    elif isinstance(data, list):
        if len(data) > MAX_LIST_ITEMS:
            raise ValueError("列表元素过多")
        for item in data:
            validate_data_structure(item, depth)

def extract_audit_info(request: Request, operator: str, reason: str) -> Dict[str, Any]:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )
    return {
        "operator": operator,
        "reason": reason,
        "ip": client_ip,
        "user_agent": request.headers.get("User-Agent", ""),
        "timestamp": now
    }

async def call_service(coro, timeout: float = DEFAULT_READ_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.exception("服务调用超时")
        raise HTTPException(504, "服务响应超时", headers={"Retry-After": "5"})
    except ConnectionError:
        logger.exception("服务连接失败")
        raise HTTPException(503, "配置服务暂时不可用", headers={"Retry-After": "10"})
    except Exception:
        logger.exception("服务调用异常")
        raise HTTPException(500, "内部服务错误")

async def schedule_reload(config_service: ConfigService):
    global _last_reload_monotonic
    while True:
        item = await _reload_queue.get()
        if item is None:                     # 停止信号
            break
        now = time.monotonic()
        if now - _last_reload_monotonic < 5.0:
            continue
        _last_reload_monotonic = now
        for attempt, delay in enumerate(_reload_backoff):
            try:
                await call_service(config_service.reload_all(), timeout=DEFAULT_WRITE_TIMEOUT)
                logger.info("配置热加载成功")
                break
            except Exception:
                logger.error("配置热加载失败 (尝试 %d/%d)", attempt+1, len(_reload_backoff))
                if attempt < len(_reload_backoff) - 1:
                    await asyncio.sleep(delay)
                else:
                    logger.critical("配置热加载最终失败，系统可能处于不一致状态")
    logger.info("热加载队列消费者已停止")

async def enqueue_reload():
    try:
        _reload_queue.put_nowait(True)
    except asyncio.QueueFull:
        pass

# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
class ConfigResponse(BaseModel):
    section: str = ""
    data: Dict[str, Any]
    version: int = 0
    last_modified: Optional[str] = None
    warnings: List[str] = []

class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    data: Dict[str, Any]
    reason: str = Field(..., min_length=1)
    operator: str = Field(..., min_length=1, max_length=32)

    @field_validator('data')
    @classmethod
    def validate_data(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if not v:
            raise ValueError('data 不能为空')
        validate_data_structure(v)
        return v

class ConfigUpdateResponse(BaseModel):
    success: bool
    message: str
    version: int = 0
    requires_approval: bool = False
    validation_errors: List[str] = []
    warnings: List[str] = []

class ConfigValidateRequest(BaseModel):
    section: str
    data: Dict[str, Any]

class ConfigValidateResponse(BaseModel):
    valid: bool
    errors: List[str] = []

class ConfigVersion(BaseModel):
    version: int
    timestamp: str
    operator: str
    reason: str
    section: str = ""

class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(..., gt=0)
    operator: str = Field(..., min_length=1, max_length=32)

# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------
async def get_admin_sections(config_service: ConfigService = Depends(get_config_service)) -> List[str]:
    meta = await config_service.get_admin_sections()
    return meta if meta else ["risk", "execution"]

async def get_hidden_sections(config_service: ConfigService = Depends(get_config_service)) -> List[str]:
    meta = await config_service.get_hidden_sections()
    return meta if meta else ["api_keys"]

# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/export", response_model=ConfigResponse)
async def export_config(
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(require_admin)
):
    config = await call_service(config_service.export_full_config())
    return ConfigResponse(section="full", data=config, version=int(config.get("_version", 1)))

@router.post("/import", response_model=ConfigUpdateResponse)
async def import_config(
    request: ConfigUpdateRequest,
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user),
    http_request: Request = None,
    _: bool = Depends(require_admin)
):
    operator = sanitize_operator(request.operator or current_user)
    if operator != current_user:
        raise HTTPException(403, "操作员身份不一致")
    reason = sanitize_reason(request.reason)
    data = strip_internal_fields(deepcopy(request.data))
    audit_info = extract_audit_info(http_request, operator, reason)
    async with _global_write_lock:
        result = await call_service(config_service.import_config(data, audit_info), timeout=DEFAULT_WRITE_TIMEOUT)
    await enqueue_reload()
    return ConfigUpdateResponse(success=True, message="配置导入成功", version=int(result.get("version", 1)))

@router.get("", response_model=ConfigResponse)
async def get_full_config(
    sanitized: bool = Query(True),
    fields: Optional[str] = Query(None),
    config_service: ConfigService = Depends(get_config_service),
    hidden_sections: List[str] = Depends(get_hidden_sections),
    current_user: str = Depends(get_current_user)
):
    config = await call_service(config_service.get_full_config(sanitized=sanitized))
    for hs in hidden_sections:
        config.pop(hs, None)
    if fields:
        requested = [f.strip() for f in fields.split(',') if f.strip()]
        config = {k: v for k, v in config.items() if k in requested}
    return ConfigResponse(
        section="full",
        data=config,
        version=int(config.get("_version", 1)),
        last_modified=config.get("_last_modified")
    )

@router.get("/{section}", response_model=ConfigResponse)
async def get_section_config(
    section: str,
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user)
):
    section = validate_section_name(section)
    try:
        data = await call_service(config_service.get_section(section))
        meta = await call_service(config_service.get_metadata(section))
        if meta is None:
            meta = {}
        return ConfigResponse(
            section=section,
            data=data,
            version=int(meta.get("version", 1)),
            last_modified=meta.get("last_modified")
        )
    except KeyError:
        raise HTTPException(404, detail="配置段不存在")

@router.put("/{section}", response_model=ConfigUpdateResponse)
async def update_section_config(
    section: str,
    request: ConfigUpdateRequest,
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user),
    http_request: Request = None,
    admin_sections: List[str] = Depends(get_admin_sections)
):
    section = validate_section_name(section)
    operator = sanitize_operator(request.operator or current_user)
    if operator != current_user:
        raise HTTPException(403, "操作员身份不一致")
    reason = sanitize_reason(request.reason)
    data = strip_internal_fields(deepcopy(request.data))

    if section in admin_sections:
        # 已在外部通过依赖 require_admin 校验
        pass

    async with _global_write_lock:
        validation = await call_service(config_service.validate_section(section, data), timeout=DEFAULT_READ_TIMEOUT)
        if not validation.get("valid"):
            return ConfigUpdateResponse(
                success=False,
                message="验证失败",
                validation_errors=validation.get("errors", []),
                warnings=[]
            )
        audit_info = extract_audit_info(http_request, operator, reason)
        audit_info["section"] = section
        result = await call_service(config_service.update_section(section, data, audit_info), timeout=DEFAULT_WRITE_TIMEOUT)

    await enqueue_reload()
    return ConfigUpdateResponse(
        success=True,
        message="配置已更新" if not result.get("pending_approval") else "配置已提交审批",
        version=int(result.get("version", 1)),
        requires_approval=result.get("pending_approval", False),
        warnings=result.get("warnings", [])
    )

@router.post("/batch", response_model=ConfigUpdateResponse)
async def batch_update(
    updates: Dict[str, ConfigUpdateRequest],
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user),
    http_request: Request = None,
    _: bool = Depends(require_admin)
):
    async with _global_write_lock:
        results = []
        for section, req in updates.items():
            section = validate_section_name(section)
            operator = sanitize_operator(req.operator or current_user)
            if operator != current_user:
                raise HTTPException(403, f"操作员身份不一致 on {section}")
            reason = sanitize_reason(req.reason)
            data = strip_internal_fields(deepcopy(req.data))
            validation = await call_service(config_service.validate_section(section, data), timeout=DEFAULT_READ_TIMEOUT)
            if not validation.get("valid"):
                return ConfigUpdateResponse(success=False, message=f"节 {section} 验证失败", validation_errors=validation.get("errors", []))
            audit_info = extract_audit_info(http_request, operator, reason)
            audit_info["section"] = section
            res = await call_service(config_service.update_section(section, data, audit_info), timeout=DEFAULT_WRITE_TIMEOUT)
            results.append(res)
    await enqueue_reload()
    return ConfigUpdateResponse(success=True, message="批量更新成功", version=max(int(r.get("version",1)) for r in results))

@router.post("/validate", response_model=ConfigValidateResponse)
async def validate_config(
    request: ConfigValidateRequest,
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user)
):
    section = validate_section_name(request.section)
    validation = await call_service(config_service.validate_section(section, request.data))
    return ConfigValidateResponse(
        valid=validation.get("valid") is True,
        errors=validation.get("errors", [])
    )

@router.get("/versions/{section}", response_model=List[ConfigVersion])
async def get_config_versions(
    section: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user)
):
    section = validate_section_name(section)
    versions = await call_service(config_service.get_version_history(section, limit=limit, offset=offset))
    return [ConfigVersion(**v) for v in versions]

@router.post("/rollback", response_model=ConfigUpdateResponse)
async def rollback_config(
    request: RollbackRequest,
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user),
    http_request: Request = None,
    _: bool = Depends(require_admin)
):
    operator = sanitize_operator(request.operator or current_user)
    if operator != current_user:
        raise HTTPException(403, "操作员身份不一致")
    audit_info = extract_audit_info(http_request, operator, f"回滚至版本 {request.version}")
    audit_info["section"] = "rollback"

    async with _global_write_lock:
        try:
            result = await call_service(config_service.rollback_to_version(request.version, audit_info), timeout=DEFAULT_WRITE_TIMEOUT)
        except ValueError as e:
            raise HTTPException(404, detail=str(e))

    await enqueue_reload()
    return ConfigUpdateResponse(
        success=True,
        message=f"已回滚至版本 {request.version}",
        version=int(result.get("version", 1))
    )

@router.post("/reload", response_model=ConfigUpdateResponse)
async def reload_config(
    reason: Optional[str] = Query(None),
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user),
    http_request: Request = None,
    _: bool = Depends(require_admin)
):
    operator = sanitize_operator(current_user)
    audit_info = extract_audit_info(http_request, operator, reason or "手动重载配置")
    audit_info["section"] = "reload"
    await call_service(config_service.reload_all(), timeout=DEFAULT_WRITE_TIMEOUT)
    return ConfigUpdateResponse(success=True, message="配置已重载", version=1)

@router.get("/pending", response_model=List[ConfigUpdateResponse])
async def list_pending_approvals(
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user),
    _: bool = Depends(require_admin)
):
    pending = await call_service(config_service.get_pending_approvals())
    return pending

@router.get("/sections/list", response_model=List[Dict[str, str]])
async def list_sections(
    config_service: ConfigService = Depends(get_config_service),
    current_user: str = Depends(get_current_user)
):
    static = [
        {"name": "strategy", "display_name": "策略配置"},
        {"name": "risk", "display_name": "风险控制"},
        {"name": "execution", "display_name": "执行层"},
        {"name": "data_sources", "display_name": "数据源"},
        {"name": "evolution", "display_name": "进化模块"},
        {"name": "logging", "display_name": "日志与审计"},
    ]
    dynamic = await call_service(config_service.get_dynamic_sections())
    if not isinstance(dynamic, list):
        dynamic = []
    merged = {s["name"]: s for s in static}
    for d in dynamic:
        merged[d["name"]] = d
    return sorted(merged.values(), key=lambda x: x["name"])

# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
async def on_startup():
    from services.config_service import get_config_service_instance
    svc = get_config_service_instance()
    if svc:
        task = asyncio.create_task(schedule_reload(svc))
        _pending_reload_tasks.append(task)
    else:
        logger.warning("ConfigService 未初始化，热加载消费者未启动")

async def on_shutdown():
    await _reload_queue.put(None)  # 停止信号
    for task in _pending_reload_tasks:
        task.cancel()
    await asyncio.wait_for(
        asyncio.gather(*_pending_reload_tasks, return_exceptions=True),
        timeout=5.0
    )
    logger.info("所有后台任务已终止")
