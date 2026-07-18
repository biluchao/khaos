# -*- coding: utf-8 -*-
"""
模块名称: auth.py (v8.0 终极不可突破)
核心职责: 提供 Redis 健康检查、内存回退清理、设备通知、密码泄露检查、权限降级感知、
         会话 ID、IP 匿名化、强制密码修改、自助注销等。
"""
import os
import uuid
import hashlib
import hmac
import secrets
import re
import time
import logging
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, SecretStr, validator
from jose import JWTError, ExpiredSignatureError, jwt
from passlib.context import CryptContext
from slowapi import Limiter
from slowapi.util import get_remote_address
import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool
import contextvars
import unicodedata

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# ---- 强配置与校验 ----
ACCESS_SECRET_KEY = os.getenv("KHAOS_ACCESS_SECRET")
REFRESH_SECRET_KEY = os.getenv("KHAOS_REFRESH_SECRET")
if not ACCESS_SECRET_KEY or not REFRESH_SECRET_KEY or ACCESS_SECRET_KEY == REFRESH_SECRET_KEY:
    raise RuntimeError("访问令牌和刷新令牌必须使用不同的安全密钥")
ALGORITHM = os.getenv("KHAOS_JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = min(60, max(1, int(os.getenv("KHAOS_ACCESS_TOKEN_MINUTES", "5"))))
REFRESH_TOKEN_EXPIRE_DAYS = min(30, max(1, int(os.getenv("KHAOS_REFRESH_TOKEN_DAYS", "7"))))
MAX_REFRESH_COUNT = int(os.getenv("KHAOS_MAX_REFRESH_COUNT", "10"))
MAX_ACTIVE_DEVICES = int(os.getenv("KHAOS_MAX_ACTIVE_DEVICES", "5"))
BCRYPT_ROUNDS = int(os.getenv("KHAOS_BCRYPT_ROUNDS", "12"))
API_KEY_ITERATIONS = max(100000, int(os.getenv("KHAOS_API_KEY_ITERATIONS", "600000")))
PASSWORD_MIN_LENGTH = int(os.getenv("KHAOS_PASSWORD_MIN_LENGTH", "12"))
PASSWORD_MAX_LENGTH = int(os.getenv("KHAOS_PASSWORD_MAX_LENGTH", "72"))
PASSWORD_PATTERN = re.compile(r'^(?=.*[A-Z])(?=.*[a-z])(?=.*\d)(?=.*[^A-Za-z0-9\s]).+$')
MAX_LOGIN_ATTEMPTS = int(os.getenv("KHAOS_MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_BASE_MINUTES = int(os.getenv("KHAOS_LOCKOUT_BASE_MINUTES", "15"))
MAX_LOCKOUT_MINUTES = int(os.getenv("KHAOS_MAX_LOCKOUT_MINUTES", "1440"))
PASSWORD_HISTORY_SIZE = int(os.getenv("KHAOS_PASSWORD_HISTORY_SIZE", "5"))
PASSWORD_VALIDITY_DAYS = int(os.getenv("KHAOS_PASSWORD_VALIDITY_DAYS", "90"))
HASH_TIMEOUT_SEC = int(os.getenv("KHAOS_HASH_TIMEOUT_SEC", "10"))
THREAD_POOL_SIZE = min(8, max(2, os.cpu_count() or 2))

# Redis 连接池
REDIS_URL = os.getenv("KHAOS_REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("KHAOS_REDIS_URL 环境变量未设置，生产环境必须配置 Redis")
pool = ConnectionPool.from_url(REDIS_URL, max_connections=10, socket_timeout=2,
                                socket_connect_timeout=2, health_check_interval=30)
redis_client = aioredis.Redis(connection_pool=pool)

pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__ident="2b", deprecated="auto")
_hash_executor = ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE, thread_name_prefix="auth-hash")
_hash_semaphore = asyncio.Semaphore(THREAD_POOL_SIZE * 2)

limiter = Limiter(key_func=get_remote_address, storage_uri=REDIS_URL)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# ---- 内存回退吊销 ----
_local_revoked: Dict[str, datetime] = {}
_revoke_local_lock = asyncio.Lock()

async def _cleanup_local_revoked():
    """定期清理过期的内存吊销条目"""
    while True:
        await asyncio.sleep(300)
        async with _revoke_local_lock:
            now = datetime.now(timezone.utc)
            expired = [jti for jti, exp in _local_revoked.items() if exp < now]
            for jti in expired:
                del _local_revoked[jti]

async def revoke_jti(jti: str, exp: datetime):
    ttl = max(1, int((exp - datetime.now(timezone.utc)).total_seconds()))
    try:
        await redis_client.setex(f"revoked_jti:{jti}", ttl, "1")
        return
    except (aioredis.ConnectionError, aioredis.TimeoutError):
        logger.critical("Redis不可用，令牌吊销回退到内存！")
    async with _revoke_local_lock:
        _local_revoked[jti] = exp

async def is_revoked(jti: str) -> bool:
    try:
        if await redis_client.exists(f"revoked_jti:{jti}"):
            return True
    except (aioredis.ConnectionError, aioredis.TimeoutError):
        pass
    async with _revoke_local_lock:
        return jti in _local_revoked

# ---- 密码工具 ----
async def verify_password(plain: str, hashed: str) -> bool:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            _run_hash_task(loop, pwd_context.verify, plain, hashed),
            timeout=HASH_TIMEOUT_SEC
        )
    except (asyncio.TimeoutError, FutureTimeoutError):
        logger.error("密码验证超时")
        raise HTTPException(status_code=503, detail="服务暂时不可用")

async def hash_password(password: str) -> str:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        _run_hash_task(loop, pwd_context.hash, password),
        timeout=HASH_TIMEOUT_SEC
    )

async def _run_hash_task(loop, func, *args):
    async with _hash_semaphore:
        return await loop.run_in_executor(_hash_executor, func, *args)

async def check_and_upgrade_password(user: Dict, plain: str) -> bool:
    if not await verify_password(plain, user["hashed_password"]):
        return False
    if pwd_context.needs_update(user["hashed_password"]):
        new_hash = await hash_password(plain)
        user["hashed_password"] = new_hash
        # 持久化 (生产写入 DB)
    return True

# ---- 用户工具 ----
USERS_DB: Dict[str, Dict] = {
    "admin": {
        "user_id": "u-1001",
        "username": "admin",
        "full_name": "系统管理员",
        "email": "admin@khaos.com",
        "hashed_password": pwd_context.hash(os.getenv("ADMIN_INITIAL_PASSWORD", "Admin@123456!")),
        "disabled": False,
        "password_change_required": False,
        "password_last_changed": datetime.now(timezone.utc).isoformat(),
        "permissions": ["*"],
        "api_keys": [],
        "last_login": None,
        "last_ip": None,
        "active_refresh_tokens": OrderedDict(),
        "failed_login_attempts": 0,
        "locked_until": None,
        "password_history": [],
        "email_verified": True,
        "mfa_enabled": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
}
_users_lock = asyncio.Lock()

async def get_user(username: str) -> Optional[Dict]:
    async with _users_lock:
        return USERS_DB.get(username)

async def save_user(user: Dict):
    async with _users_lock:
        USERS_DB[user["username"]] = user
        user["updated_at"] = datetime.now(timezone.utc).isoformat()

# ---- 令牌生成 ----
def _create_token(data: dict, token_type: str, secret: str, expires: timedelta, extra: dict = None) -> str:
    now = datetime.now(timezone.utc)
    payload = data.copy()
    payload.update({
        "iss": os.getenv("KHAOS_JWT_ISSUER", "khaos"),
        "aud": [a.strip() for a in os.getenv("KHAOS_JWT_AUDIENCE", "khaos-api").split(",") if a.strip()],
        "iat": now,
        "exp": now + expires,
        "type": token_type,
        "jti": secrets.token_hex(32),
        "kid": os.getenv("KHAOS_JWT_KID", "default")
    })
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm=ALGORITHM)

def create_access_token(data: dict, permissions: List[str] = None) -> str:
    extra = {}
    if permissions:
        extra["permissions"] = permissions
    return _create_token(data, "access", ACCESS_SECRET_KEY, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES), extra)

def create_refresh_token(data: dict, refresh_count: int = 0) -> str:
    return _create_token(data, "refresh", REFRESH_SECRET_KEY, timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
                         extra={"refresh_count": refresh_count})

# ---- 认证依赖 ----
async def get_current_user(token: str = Depends(oauth2_scheme)) -> Dict:
    try:
        payload = jwt.decode(token, ACCESS_SECRET_KEY, algorithms=[ALGORITHM],
                             options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
                             audience=os.getenv("KHAOS_JWT_AUDIENCE", "khaos-api").split(","),
                             issuer=os.getenv("KHAOS_JWT_ISSUER", "khaos"), leeway=30)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="无效的凭据类型")
        username = payload.get("sub")
        jti = payload.get("jti")
        if not username or not jti:
            raise HTTPException(status_code=401, detail="无效的认证凭据")
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="令牌已过期", headers={"X-Token-Expired": "true"})
    except JWTError:
        raise HTTPException(status_code=401, detail="无效的认证凭据")

    if await is_revoked(jti):
        raise HTTPException(status_code=401, detail="令牌已被吊销")

    user = await get_user(username)
    if user is None or user.get("disabled"):
        raise HTTPException(status_code=400, detail="用户已禁用或不存在")
    return user

# ---- 模型 ----
class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    password_change_required: bool = False
    refresh_remaining: int = MAX_REFRESH_COUNT
    active_devices: int = 0
    max_devices: int = MAX_ACTIVE_DEVICES

class TokenRefresh(BaseModel):
    refresh_token: str = Field(..., min_length=10, max_length=2000, regex=r'^[A-Za-z0-9\-_]+?\.[A-Za-z0-9\-_]+?\.[A-Za-z0-9\-_]+$')

class UserInfo(BaseModel):
    username: str
    full_name: str
    email: str
    permissions: List[str]
    password_change_required: bool = False
    email_verified: bool = False
    mfa_enabled: bool = False

class ApiKeyCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=100, regex=r'^[A-Za-z0-9 _-]+$')
    scopes: List[str] = Field(default=["read"])

    @validator('scopes')
    def validate_scopes(cls, v):
        allowed = {"read", "trade", "admin"}
        for scope in v:
            if scope not in allowed:
                raise ValueError(f'无效的权限范围: {scope}')
        return v

class ApiKeyResponse(BaseModel):
    key_id: str
    key: str
    prefix: str
    created: str

class MessageResponse(BaseModel):
    message: str
    token_expired: bool = False

class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @validator('new_password')
    def validate_strength(cls, v, values):
        v = unicodedata.normalize('NFKC', v)
        if 'old_password' in values and v == values['old_password']:
            raise ValueError('新密码不能与旧密码相同')
        # 禁止用户名作为密码的一部分
        if 'username' in values and values['username'].lower() in v.lower():
            raise ValueError('密码不能包含用户名')
        if not PASSWORD_PATTERN.match(v):
            raise ValueError('密码必须包含大小写字母、数字和特殊字符（空格除外）')
        return v

class SessionInfo(BaseModel):
    session_id: str
    device: str
    ip: str
    created: str

# ---- 路由 ----
@router.post("/login", response_model=Token)
@limiter.limit("5/minute")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    user = await get_user(form_data.username)
    if not user:
        audit_log("LOGIN_FAILED", form_data.username, "用户不存在", False, request)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    locked_until = user.get("locked_until")
    if locked_until and datetime.fromisoformat(locked_until) > datetime.now(timezone.utc):
        audit_log("LOGIN_LOCKED", form_data.username, "账户已锁定", False, request)
        raise HTTPException(status_code=423, detail="账户已锁定，请稍后再试")

    if not await verify_password(form_data.password, user["hashed_password"]):
        async with _users_lock:
            if not locked_until or datetime.fromisoformat(locked_until) <= datetime.now(timezone.utc):
                user["failed_login_attempts"] += 1
                attempts = user["failed_login_attempts"]
                if attempts >= MAX_LOGIN_ATTEMPTS:
                    lockout_minutes = min(MAX_LOCKOUT_MINUTES, LOCKOUT_BASE_MINUTES * (2 ** (attempts - MAX_LOGIN_ATTEMPTS)))
                    user["locked_until"] = (datetime.now(timezone.utc) + timedelta(minutes=lockout_minutes)).isoformat()
        audit_log("LOGIN_FAILED", form_data.username, "密码错误", False, request)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    password_last_changed = user.get("password_last_changed")
    if password_last_changed:
        days_since = (datetime.now(timezone.utc) - datetime.fromisoformat(password_last_changed)).days
        if days_since > PASSWORD_VALIDITY_DAYS:
            user["password_change_required"] = True

    async with _users_lock:
        user["failed_login_attempts"] = 0
        user["locked_until"] = None
        removed_devices = []
        while len(user["active_refresh_tokens"]) >= MAX_ACTIVE_DEVICES:
            oldest_jti, oldest_info = user["active_refresh_tokens"].popitem(last=False)
            await revoke_jti(oldest_jti, datetime.now(timezone.utc) + timedelta(days=1))
            removed_devices.append(oldest_info.get("device", "unknown")[:50])
        access = create_access_token({"sub": user["username"]}, permissions=user.get("permissions"))
        refresh = create_refresh_token({"sub": user["username"]}, refresh_count=0)
        refresh_payload = jwt.decode(refresh, REFRESH_SECRET_KEY, algorithms=[ALGORITHM])
        refresh_jti = refresh_payload["jti"]
        session_id = secrets.token_hex(8)
        user["active_refresh_tokens"][refresh_jti] = {
            "session_id": session_id,
            "device": request.headers.get("User-Agent", "unknown")[:200],
            "ip": request.headers.get("X-Forwarded-For", request.client.host),
            "created": datetime.now(timezone.utc).isoformat()
        }
        user["last_login"] = datetime.now(timezone.utc).isoformat()
        user["last_ip"] = request.headers.get("X-Forwarded-For", request.client.host)
        user["updated_at"] = datetime.now(timezone.utc).isoformat()
        pwd_change_required = user.get("password_change_required", False)
        active_count = len(user["active_refresh_tokens"])
    audit_log("LOGIN_SUCCESS", user["username"], f"devices={active_count}", True, request)
    return Token(
        access_token=access, refresh_token=refresh,
        password_change_required=pwd_change_required,
        refresh_remaining=MAX_REFRESH_COUNT,
        active_devices=active_count,
        max_devices=MAX_ACTIVE_DEVICES
    )

@router.post("/refresh", response_model=Token)
@limiter.limit("10/minute")
async def refresh_token(refresh: TokenRefresh, request: Request):
    cred_exc = HTTPException(status_code=401, detail="无效的刷新令牌")
    try:
        payload = jwt.decode(refresh.refresh_token, REFRESH_SECRET_KEY, algorithms=[ALGORITHM],
                             options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
                             audience=os.getenv("KHAOS_JWT_AUDIENCE", "khaos-api").split(","),
                             issuer=os.getenv("KHAOS_JWT_ISSUER", "khaos"), leeway=30)
        if payload.get("type") != "refresh":
            raise cred_exc
        jti = payload.get("jti")
        username = payload.get("sub")
        refresh_count = payload.get("refresh_count", 0)
        if not jti or not username:
            raise cred_exc
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="刷新令牌已过期")
    except JWTError:
        raise cred_exc

    if await is_revoked(jti):
        raise HTTPException(status_code=401, detail="刷新令牌已被吊销")

    user =
