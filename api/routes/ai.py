# -*- coding: utf-8 -*-
"""
模块名称: ai.py
核心职责: 提供 AI 对话接口，支持本地 ONNX 模型（离线）和云端 DeepSeek API，以及自然语言参数修改。
所属层级: api.routes

外部依赖:
    - fastapi (APIRouter, Depends, HTTPException, Request, BackgroundTasks)
    - pydantic (BaseModel, Field, validator)
    - services.ai_service (AIService)
    - core.monitoring.metrics_collector (Prometheus 指标)
    - api.dependencies (认证依赖)
    - asyncio, time, secrets, uuid, re, json, hashlib, math
    - redis.asyncio (可选，用于分布式限流)

接口契约:
    提供: {
        'POST /api/v1/ai/chat': '发送对话消息，返回 AI 回复',
        'POST /api/v1/ai/nlu/parse': '解析自然语言意图，用于参数修改等',
        'GET /api/v1/ai/status': '获取 AI 服务状态（本地/云端）'
    }
    消费: {
        'services.ai_service.AIService': 'AI 核心服务'
    }

配置项 (由 default.yaml 注入):
    ai_assist.max_calls_per_hour: 20
    ai_assist.max_tokens_per_request: 4096
    ai_assist.max_context_bytes: 10000
    ai_assist.forbidden_nlu_actions: ["set_param"]
    ai_assist.sensitive_context_keys: ["api_key","secret","password"]
    ai_assist.cloud_model_name: "deepseek-chat"
    ai_assist.local_model_name: "khaos-onnx-local"
    ai_assist.request_timeout_sec: 25
    ai_assist.max_cost_per_request_usd: 0.10
    ai_assist.max_concurrent_calls: 5
    ai_assist.local_model_checksum: "sha256:..."
    ai_assist.max_request_body_bytes: 50_000  # 请求体最大字节数
    ai_assist.enable_streaming: false          # 未来支持流式
    ai_assist.fallback_reply: "AI 服务暂不可用，请稍后重试。"  # 降级回复
"""

import asyncio
import hashlib
import logging
import math
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from api.dependencies import get_current_user, get_ai_service
from services.ai_service import AIService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

# =============================================================================
# 模块配置 (实际应从配置对象加载，此处用 CONST 占位)
# =============================================================================
CONFIG = {
    "max_calls_per_hour": 20,
    "request_timeout_sec": 25,
    "max_context_bytes": 10_000,
    "max_concurrent_calls": 5,
    "max_cost_per_request_usd": 0.10,
    "sensitive_keys": {"api_key", "secret", "password", "token", "private_key"},
    "forbidden_nlu_actions": {"set_param"},
    "cloud_model_name": "deepseek-chat",
    "local_model_name": "khaos-onnx-local",
    "local_model_checksum": "sha256:...",
    "max_request_body_bytes": 50_000,
    "fallback_reply": "AI 服务暂不可用，请稍后重试。",
    "enable_streaming": False,
    "rate_limit_redis_url": None,  # 生产环境配置
}

# =============================================================================
# 分布式限流 & 并发控制
# =============================================================================
if CONFIG["rate_limit_redis_url"]:
    import redis.asyncio as aioredis
    _redis = aioredis.from_url(CONFIG["rate_limit_redis_url"])
else:
    _redis = None

_ai_semaphore = asyncio.Semaphore(CONFIG["max_concurrent_calls"])
_rate_limit_local: Dict[str, List[float]] = {}
_rate_limit_lock = asyncio.Lock()

# Prometheus 指标 (示例占位)
# ai_requests_total = Counter('ai_requests_total', '...', ['endpoint', 'status'])
# ai_request_duration = Histogram('ai_request_duration_seconds', '...', ['endpoint'])

async def _cleanup_rate_limits_periodically():
    """定期清理本地限流记录，防止内存泄漏"""
    while True:
        await asyncio.sleep(600)
        async with _rate_limit_lock:
            now = time.time()
            for u in list(_rate_limit_local.keys()):
                _rate_limit_local[u] = [t for t in _rate_limit_local[u] if t > now - 3600]
                if not _rate_limit_local[u]:
                    del _rate_limit_local[u]

# 在应用启动时调用
# asyncio.create_task(_cleanup_rate_limits_periodically())

def _generate_request_id() -> str:
    return uuid.uuid4().hex[:16]

def _recursive_sanitize(obj: Any, sensitive: Set[str]) -> Any:
    """递归脱敏，处理字典/列表/字符串"""
    if isinstance(obj, dict):
        return {
            k: "***REDACTED***" if k.lower() in sensitive else _recursive_sanitize(v, sensitive)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_recursive_sanitize(i, sensitive) for i in obj]
    elif isinstance(obj, bytes):
        return "<binary>"
    else:
        return obj

async def _check_rate_limit(user: str, max_calls: int) -> Tuple[bool, int]:
    """统一限流检查：优先使用 Redis 原子计数器，回退本地锁"""
    now = time.time()
    window = 3600
    if _redis:
        # Redis 原子实现：每个用户一个 key，每次请求增加计数并设置过期
        key = f"ai_rate:{user}:{now // window}"
        current = await _redis.incr(key)
        await _redis.expire(key, window)
        if current > max_calls:
            return False, 0
        return True, max_calls - current
    else:
        async with _rate_limit_lock:
            if user not in _rate_limit_local:
                _rate_limit_local[user] = []
            _rate_limit_local[user] = [t for t in _rate_limit_local[user] if t > now - window]
            calls = _rate_limit_local[user]
            if len(calls) >= max_calls:
                return False, 0
            calls.append(now)
            return True, max_calls - len(calls)

# =============================================================================
# 数据模型（增强校验）
# =============================================================================

FORBIDDEN_PATTERNS = re.compile(
    r"(eval|exec|__import__|system|subprocess|rm\s+-rf|delete\s+from|DROP\s+TABLE)",
    re.IGNORECASE
)

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    prefer_offline: bool = Field(False)
    context: Optional[Dict[str, Any]] = Field(None)

    @validator('message')
    def message_not_forbidden(cls, v):
        if FORBIDDEN_PATTERNS.search(v):
            raise ValueError("消息包含不安全内容")
        return v

    @validator('context')
    def context_size(cls, v):
        if v is not None:
            if len(str(v).encode('utf-8')) > CONFIG["max_context_bytes"]:
                raise ValueError(f"上下文超出 {CONFIG['max_context_bytes']} 字节限制")
        return v

class ChatResponse(BaseModel):
    reply: str
    model_used: str
    tokens_used: Optional[int] = None
    cost_usd: Optional[float] = None
    request_id: str

class NLUIntent(BaseModel):
    action: str
    params: Optional[Dict[str, Any]] = None
    confidence: float

    @validator('action')
    def not_forbidden(cls, v):
        if v in CONFIG["forbidden_nlu_actions"]:
            raise ValueError(f"动作 '{v}' 被系统禁用")
        return v

class NLURequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)

    @validator('text')
    def text_not_forbidden(cls, v):
        if FORBIDDEN_PATTERNS.search(v):
            raise ValueError("文本包含不安全内容")
        return v

class NLUResponse(BaseModel):
    intents: List[NLUIntent]
    raw_text: str
    request_id: str

class AIStatus(BaseModel):
    local_available: bool
    cloud_available: bool
    local_model_name: Optional[str] = CONFIG["local_model_name"]
    cloud_model_name: Optional[str] = CONFIG["cloud_model_name"]
    rate_limit_remaining: int
    total_cost_today_usd: float
    circuit_breaker_open: bool = False
    concurrent_requests: int = 0

# =============================================================================
# 路由实现
# =============================================================================

@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    req: Request,
    background_tasks: BackgroundTasks,
    ai_service: AIService = Depends(get_ai_service),
    current_user: str = Depends(get_current_user)
):
    request_id = _generate_request_id()
    # 请求体大小限制（FastAPI 中间件可实现，这里在路由内二次检查）
    body = await req.body()
    if len(body) > CONFIG["max_request_body_bytes"]:
        raise HTTPException(status_code=413, detail="请求体过大")

    # 限流
    max_calls = CONFIG["max_calls_per_hour"]
    allowed, remaining = await _check_rate_limit(current_user, max_calls)
    if not allowed:
        logger.warning("AI_CHAT_RATELIMIT [%s] user=%s", request_id, current_user)
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")

    # 脱敏上下文
    safe_ctx = _recursive_sanitize(request.context, CONFIG["sensitive_keys"])
    logger.info("AI_CHAT_START [%s] user=%s offline=%s msg_len=%d",
                request_id, current_user, request.prefer_offline, len(request.message))

    # 并发控制（公平信号量）
    acquired = _ai_semaphore.locked()
    async with _ai_semaphore:
        start = time.monotonic()
        try:
            reply, meta = await asyncio.wait_for(
                ai_service.chat(
                    message=request.message,
                    prefer_offline=request.prefer_offline,
                    context=safe_ctx
                ),
                timeout=CONFIG["request_timeout_sec"]
            )
        except asyncio.TimeoutError:
            logger.error("AI_CHAT_TIMEOUT [%s]", request_id)
            raise HTTPException(status_code=504, detail="AI 服务响应超时")
        except ValueError as e:
            logger.warning("AI_CHAT_INVALID [%s] %s", request_id, str(e))
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            logger.error("AI_CHAT_UNAVAILABLE [%s] %s", request_id, str(e))
            # 返回降级回复，而不是直接抛出 503
            return ChatResponse(
                reply=CONFIG["fallback_reply"],
                model_used="fallback",
                tokens_used=0,
                cost_usd=0.0,
                request_id=request_id
            )
        except Exception:
            logger.exception("AI_CHAT_FAILED [%s]", request_id)
            raise HTTPException(status_code=500, detail="AI 对话内部错误")
        finally:
            elapsed = time.monotonic() - start
            # ai_request_duration.labels(endpoint="chat").observe(elapsed)

    cost = meta.get("cost", 0.0)
    if cost > CONFIG["max_cost_per_request_usd"]:
        logger.error("AI_CHAT_COST_ALERT [%s] cost=$%.4f", request_id, cost)
        raise HTTPException(status_code=507, detail="单次对话费用异常，已被系统阻断")

    logger.info("AI_CHAT_DONE [%s] model=%s tokens=%s cost=$%.4f elapsed=%.3fs",
                request_id, meta.get("model"), meta.get("tokens"), cost, elapsed)

    return ChatResponse(
        reply=reply,
        model_used=meta.get("model", CONFIG["cloud_model_name"]),
        tokens_used=meta.get("tokens"),
        cost_usd=cost,
        request_id=request_id
    )


@router.post("/nlu/parse", response_model=NLUResponse)
async def parse_natural_language(
    request: NLURequest,
    req: Request,
    ai_service: AIService = Depends(get_ai_service),
    current_user: str = Depends(get_current_user)
):
    request_id = _generate_request_id()
    # 请求体大小检查
    body = await req.body()
    if len(body) > CONFIG["max_request_body_bytes"]:
        raise HTTPException(status_code=413, detail="请求体过大")

    allowed, _ = await _check_rate_limit(current_user, CONFIG["max_calls_per_hour"])
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁")

    logger.info("AI_NLU_START [%s] user=%s text_preview=%s", request_id, current_user, request.text[:80])

    async with _ai_semaphore:
        try:
            intents = await asyncio.wait_for(
                ai_service.parse_intent(request.text),
                timeout=CONFIG["request_timeout_sec"]
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="NLU 服务超时")
        except Exception:
            logger.exception("AI_NLU_FAILED [%s]", request_id)
            raise HTTPException(status_code=500, detail="意图解析失败")

    safe_intents = []
    for intent in intents:
        if intent.get("action") in CONFIG["forbidden_nlu_actions"]:
            logger.warning("AI_NLU_BLOCKED [%s] action=%s", request_id, intent["action"])
            continue
        safe_intents.append(NLUIntent(**intent))

    return NLUResponse(
        intents=safe_intents,
        raw_text=request.text,
        request_id=request_id
    )


@router.get("/status", response_model=AIStatus)
async def get_ai_status(
    ai_service: AIService = Depends(get_ai_service),
    current_user: str = Depends(get_current_user)
):
    try:
        status = await asyncio.wait_for(ai_service.get_status(), timeout=5)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="状态检查超时")
    except Exception:
        raise HTTPException(status_code=500, detail="无法获取 AI 服务状态")

    async with _rate_limit_lock if not _redis else asyncio.nullcontext():
        if _redis:
            key = f"ai_rate:{current_user}:{time.time() // 3600}"
            used = await _redis.get(key) or 0
            remaining = max(0, CONFIG["max_calls_per_hour"] - int(used))
        else:
            now = time.time()
            calls = _rate_limit_local.get(current_user, [])
            calls = [t for t in calls if t > now - 3600]
            used = len(calls)
            remaining = max(0, CONFIG["max_calls_per_hour"] - used)

    return AIStatus(
        local_available=status.get("local_available", False),
        cloud_available=status.get("cloud_available", False),
        local_model_name=status.get("local_model_name", CONFIG["local_model_name"]),
        cloud_model_name=status.get("cloud_model_name", CONFIG["cloud_model_name"]),
        rate_limit_remaining=remaining,
        total_cost_today_usd=status.get("total_cost_today_usd", 0.0),
        circuit_breaker_open=status.get("circuit_breaker_open", False),
        concurrent_requests=_ai_semaphore._value
    )
