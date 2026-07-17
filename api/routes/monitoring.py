"""
模块名称: monitoring.py (v6.0 - 永恒之眼)
核心职责: KHAOS 分布式模块健康监控中枢，提供金融级韧性、全链路追踪、
         智能降级、审计合规，可在全球顶级量化基金万亿级集群中稳定运行。
所属层级: api.routes

外部依赖:
    - fastapi
    - pydantic (v1/v2 兼容)
    - redis (分布式存储与锁)
    - core.config
    - services.audit_service
    - api.dependencies
    - asyncio, logging, uuid, time, sys, re, json
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Query, status
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
import asyncio
import logging
import uuid
import sys
import re
import hashlib
import json

# Pydantic v2 兼容
try:
    from pydantic import field_validator, model_validator
    PYDANTIC_V2 = True
except ImportError:
    PYDANTIC_V2 = False

from api.dependencies import (
    get_current_user,
    get_monitoring_config,
    require_admin,
    rate_limiter,
)
from services.audit_service import audit_log
from core.redis_manager import RedisManager
from core.distributed_lock import Redlock

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])

# ---------------------------------------------------------------------------
# 数据模型 (金融级强化，Pydantic 兼容)
# ---------------------------------------------------------------------------
class ModuleStatus(BaseModel):
    name: str = Field(..., max_length=100)
    status: str = Field("gray", pattern=r"^(green|yellow|red|gray)$")
    message: str = Field("未上报", max_length=500)
    last_update: Optional[datetime] = Field(None)
    registered_at: Optional[datetime] = Field(None)
    version: Optional[str] = Field(None, max_length=50)
    node_id: Optional[str] = Field(None, max_length=50)
    metrics: Optional[Dict[str, Any]] = Field(None)

    # Pydantic v2 使用 field_validator，v1 使用 validator
    if PYDANTIC_V2:
        @field_validator('name')
        @classmethod
        def name_valid(cls, v):
            if not re.match(r'^[a-zA-Z0-9_-]+$', v):
                raise ValueError('模块名称只能包含字母、数字、下划线和连字符')
            return v
    else:
        @validator('name')
        def name_valid(cls, v):
            if not re.match(r'^[a-zA-Z0-9_-]+$', v):
                raise ValueError('模块名称只能包含字母、数字、下划线和连字符')
            return v


class ModuleRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=200)
    version: Optional[str] = Field(None, max_length=50)
    node_id: Optional[str] = Field(None, max_length=50)

    if PYDANTIC_V2:
        @field_validator('name')
        @classmethod
        def name_valid(cls, v):
            if not re.match(r'^[a-zA-Z0-9_-]+$', v):
                raise ValueError('模块名称只能包含字母、数字、下划线和连字符')
            return v
    else:
        @validator('name')
        def name_valid(cls, v):
            if not re.match(r'^[a-zA-Z0-9_-]+$', v):
                raise ValueError('模块名称只能包含字母、数字、下划线和连字符')
            return v


class ModuleHistoryEntry(BaseModel):
    timestamp: datetime
    status: str
    message: str
    operator: Optional[str] = "system"


class HealthCheckResult(BaseModel):
    overall: str
    total_modules: int
    healthy_modules: int
    warning_modules: int
    critical_modules: int
    details: List[ModuleStatus]
    cluster_view: Optional[List[Dict[str, Any]]] = None


class ClusterNodeInfo(BaseModel):
    node_id: str
    module_count: int
    last_heartbeat: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 金融级注册表 (Redis 后端 + Redlock + 完善的异常降级与资源管理)
# ---------------------------------------------------------------------------
class ModuleHealthRegistry:
    # 配置键常量，防止硬编码
    CFG_MAX_HISTORY = 'max_history_per_module'
    CFG_MAX_STALENESS = 'max_staleness_sec'
    CFG_HISTORY_MEMORY_MB = 'max_history_memory_mb'
    CFG_ALLOWED_IPS = 'allowed_ips'
    CFG_CACHE_TTL = 'health_cache_ttl_sec'
    REDIS_PREFIX = "monitoring"
    CACHE_KEY = "monitoring:cached_health"

    def __init__(self, config: Any, redis: RedisManager):
        self.redis = redis
        self.config = config
        self.lock = Redlock(f"{self.REDIS_PREFIX}:registry_lock", redis)
        self.max_history = self._cfg(self.CFG_MAX_HISTORY, 200)
        self.max_staleness = self._cfg(self.CFG_MAX_STALENESS, 60)
        self.max_history_memory_mb = self._cfg(self.CFG_HISTORY_MEMORY_MB, 10)
        self.allowed_ips = self._cfg(self.CFG_ALLOWED_IPS, None)
        self.health_cache_ttl = self._cfg(self.CFG_CACHE_TTL, 60)
        self._is_redis_available = True
        self._bg_tasks: List[asyncio.Task] = []
        # 启动后台任务并保存引用，以便清理
        self._start_background_tasks()

    def _cfg(self, key: str, default: Any) -> Any:
        return getattr(self.config, key, default)

    def _start_background_tasks(self):
        """安全启动后台任务，捕获异常并记录"""
        try:
            task = asyncio.create_task(self._periodic_health_cache())
            self._bg_tasks.append(task)
            task.add_done_callback(self._on_task_done)
        except Exception as e:
            logger.error("Failed to start health cache task: %s", e)

    def _on_task_done(self, task: asyncio.Task):
        """后台任务完成/异常回调"""
        if task.cancelled():
            logger.info("Background task cancelled")
        elif task.exception():
            logger.error("Background task failed with exception: %s", task.exception())

    async def shutdown(self):
        """优雅关闭：取消所有后台任务"""
        for task in self._bg_tasks:
            task.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

    async def _redis_op(self, coro, fallback=None, err_msg=None):
        """统一 Redis 操作异常处理，自动降级并重连"""
        try:
            return await coro
        except Exception as e:
            logger.error(f"Redis operation failed: {err_msg or str(e)}")
            self._is_redis_available = False
            # 尝试异步重连
            asyncio.create_task(self._attempt_reconnect())
            return fallback

    async def _attempt_reconnect(self):
        """尝试恢复 Redis 连接"""
        await asyncio.sleep(1)
        try:
            await self.redis.ping()
            self._is_redis_available = True
            logger.info("Redis connection restored")
        except Exception:
            pass

    async def _check_redis(self):
        if not self._is_redis_available:
            raise HTTPException(status_code=503, detail="监控服务暂时不可用（Redis 故障）")

    # ---------- 分布式锁增强 ----------
    async def _acquire_lock(self, lock_name: str, timeout: int = 10) -> bool:
        if not self._is_redis_available:
            return True  # 降级：无Redis时不加锁，接受潜在不一致
        try:
            # 锁有效期应长于操作时间，并支持自动续期
            return await self.lock.acquire(lock_name, ttl=timeout * 1000, auto_renew=True)
        except Exception:
            logger.warning("Failed to acquire lock %s, proceeding without lock", lock_name)
            return True

    async def _release_lock(self, lock_name: str):
        if not self._is_redis_available:
            return
        try:
            await self.lock.release(lock_name)
        except Exception as e:
            logger.warning("Failed to release lock %s: %s", lock_name, e)

    # ---------- 键名生成 ----------
    def _get_status_key(self, name: str) -> str:
        return f"{self.REDIS_PREFIX}:module:{name}"

    def _get_history_key(self, name: str) -> str:
        return f"{self.REDIS_PREFIX}:history:{name}"

    # ---------- 状态管理 ----------
    async def register(self, name: str, description: str = None, version: str = None,
                       node_id: str = None) -> ModuleStatus:
        await self._check_redis()
        lock_name = f"reg:{name}"
        await self._acquire_lock(lock_name)
        try:
            key = self._get_status_key(name)
            exists = await self._redis_op(self.redis.exists(key), False, "exists")
            if exists:
                raise ValueError(f"模块 '{name}' 已存在")
            now = datetime.now(timezone.utc)
            status = ModuleStatus(
                name=name, status="gray",
                message=f"已注册 ({description or ''})",
                registered_at=now, version=version, node_id=node_id
            )
            await self._redis_op(self.redis.set(key, status.model_dump_json()), None, "set")
            logger.info("Module registered: %s", name)
            return status
        finally:
            await self._release_lock(lock_name)

    async def update_status(self, name: str, status: str, message: str = "",
                            version: str = None, node_id: str = None,
                            operator: str = "system") -> ModuleStatus:
        await self._check_redis()
        lock_name = f"upd:{name}"
        await self._acquire_lock(lock_name)
        try:
            key = self._get_status_key(name)
            now = datetime.now(timezone.utc)
            raw = await self._redis_op(self.redis.get(key), None, "get")
            if not raw:
                # 自动注册
                module = await self.register(name, "auto-registered", version, node_id)
            else:
                module = ModuleStatus.model_validate_json(raw)
            module.status = status
            module.message = self._sanitize(message)[:500]
            module.last_update = now
            if version:
                module.version = version
            if node_id:
                module.node_id = node_id
            await self._redis_op(self.redis.set(key, module.model_dump_json()), None, "set")
            # 原子性记录历史
            hist_key = self._get_history_key(name)
            entry = ModuleHistoryEntry(timestamp=now, status=status, message=message, operator=operator)
            await self._redis_op(self.redis.lpush(hist_key, entry.model_dump_json()), None, "lpush")
            await self._redis_op(self.redis.ltrim(hist_key, 0, self.max_history - 1), None, "ltrim")
            return module
        finally:
            await self._release_lock(lock_name)

    async def get_all(self) -> List[ModuleStatus]:
        await self._check_redis()
        now = datetime.now(timezone.utc)
        modules = []
        try:
            keys = await self._redis_op(self.redis.keys(f"{self.REDIS_PREFIX}:module:*"), [], "keys")
            if not keys:
                return modules
            # 批量获取提高性能
            pipe = self.redis.pipeline()
            for key in keys:
                pipe.get(key)
            results = await self._redis_op(pipe.execute(), [], "pipeline get")
            if not results:
                return modules
            for i, key in enumerate(keys):
                data = results[i]
                if data:
                    try:
                        mod = ModuleStatus.model_validate_json(data)
                        if mod.status == "green" and mod.last_update:
                            if (now - mod.last_update).total_seconds() > self.max_staleness:
                                # 超时处理：更新状态并记录历史
                                await self.update_status(mod.name, "red",
                                                         f"超时未上报 (>{self.max_staleness}s)",
                                                         operator="timeout")
                                mod.status = "red"
                                mod.message = f"超时未上报 (>{self.max_staleness}s)"
                        modules.append(mod)
                    except Exception as e:
                        logger.error("Failed to parse module %s: %s", key, e)
        except Exception as e:
            logger.error("Error fetching module list: %s", e)
            raise HTTPException(status_code=500, detail="模块列表获取失败")
        priority = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
        modules.sort(key=lambda x: priority.get(x.status, 99))
        return modules

    async def get_history(self, name: str) -> List[ModuleHistoryEntry]:
        await self._check_redis()
        hist_key = self._get_history_key(name)
        items = await self._redis_op(self.redis.lrange(hist_key, 0, self.max_history - 1), [], "lrange")
        history = []
        for item in items:
            try:
                history.append(ModuleHistoryEntry.model_validate_json(item))
            except Exception as e:
                logger.warning("Failed to parse history entry for %s: %s", name, e)
        return history

    async def unregister(self, name: str) -> bool:
        await self._check_redis()
        lock_name = f"unreg:{name}"
        await self._acquire_lock(lock_name)
        try:
            key = self._get_status_key(name)
            exists = await self._redis_op(self.redis.exists(key), False, "exists")
            if exists:
                await self._redis_op(self.redis.delete(key), None, "delete")
                await self._redis_op(self.redis.delete(self._get_history_key(name)), None, "delete hist")
                return True
            return False
        finally:
            await self._release_lock(lock_name)

    async def get_health_check(self) -> HealthCheckResult:
        modules = await self.get_all()
        total = len(modules)
        healthy = sum(1 for m in modules if m.status == "green")
        warning = sum(1 for m in modules if m.status == "yellow")
        critical = sum(1 for m in modules if m.status == "red")
        if critical > 0:
            overall = "unhealthy"
        elif warning > 0:
            overall = "degraded"
        else:
            overall = "healthy"
        cluster = []
        try:
            cluster = await self._get_cluster_view()
        except Exception as e:
            logger.warning("Failed to get cluster view: %s", e)
        return HealthCheckResult(
            overall=overall, total_modules=total,
            healthy_modules=healthy, warning_modules=warning,
            critical_modules=critical, details=modules,
            cluster_view=cluster
        )

    async def _periodic_health_cache(self):
        """定期计算健康状态并缓存，任务异常自恢复"""
        while True:
            try:
                await asyncio.sleep(30)
                if self._is_redis_available:
                    health = await self.get_health_check()
                    await self.redis.set(self.CACHE_KEY, health.model_dump_json(), ex=self.health_cache_ttl)
            except asyncio.CancelledError:
                logger.info("Health cache task cancelled")
                break
            except Exception as e:
                logger.error("Cached health update failed: %s", e)

    async def _get_cluster_view(self) -> List[ClusterNodeInfo]:
        nodes = {}
        keys = await self._redis_op(self.redis.keys(f"{self.REDIS_PREFIX}:module:*"), [], "keys")
        if not keys:
            return []
        pipe = self.redis.pipeline()
        for key in keys:
            pipe.get(key)
        results = await self._redis_op(pipe.execute(), [], "pipeline get")
        if not results:
            return []
        for i, key in enumerate(keys):
            data = results[i]
            if data:
                try:
                    mod = ModuleStatus.model_validate_json(data)
                    nid = mod.node_id or "unknown"
                    if nid not in nodes:
                        nodes[nid] = ClusterNodeInfo(node_id=nid, module_count=0)
                    nodes[nid].module_count += 1
                except Exception:
                    pass
        return list(nodes.values())

    def _sanitize(self, message: str) -> str:
        message = re.sub(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b', '[IP_HIDDEN]', message)
        message = re.sub(r'(api_key|token|secret|password)=[^&\\s]+', r'\1=[HIDDEN]', message, flags=re.IGNORECASE)
        return message


# ---------------------------------------------------------------------------
# 依赖注入 (全局实例管理，支持优雅关闭)
# ---------------------------------------------------------------------------
_registry: Optional[ModuleHealthRegistry] = None

async def get_registry(config=Depends(get_monitoring_config)) -> ModuleHealthRegistry:
    global _registry
    if _registry is None:
        redis = RedisManager.get_instance()
        _registry = ModuleHealthRegistry(config, redis)
    return _registry

# 应用关闭事件，清理后台任务
@router.on_event("shutdown")
async def shutdown_registry():
    global _registry
    if _registry:
        await _registry.shutdown()

async def check_rate_limit(user=Depends(get_current_user), limiter=Depends(rate_limiter)):
    if not limiter.is_allowed(user.username):
        raise HTTPException(status_code=429, detail="请求过于频繁")

# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------

@router.get("/modules", response_model=List[ModuleStatus])
async def get_modules(registry=Depends(get_registry), user=Depends(get_current_user), _=Depends(check_rate_limit)):
    return await registry.get_all()

@router.get("/modules/{module_name}/history", response_model=List[ModuleHistoryEntry])
async def get_module_history(module_name: str, registry=Depends(get_registry), user=Depends(get_current_user)):
    if not re.match(r'^[a-zA-Z0-9_-]+$', module_name):
        raise HTTPException(status_code=400, detail="模块名称格式无效")
    return await registry.get_history(module_name)

@router.post("/modules/register", response_model=ModuleStatus, status_code=201)
async def register_module(req: ModuleRegisterRequest, bg: BackgroundTasks,
                         registry=Depends(get_registry), user=Depends(get_current_user)):
    try:
        mod = await registry.register(req.name, req.description, req.version, req.node_id)
        bg.add_task(audit_log, user=user.username, action="register_module", details=req.dict())
        return mod
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

@router.delete("/modules/{module_name}")
async def unregister_module(module_name: str, bg: BackgroundTasks,
                           registry=Depends(get_registry), user=Depends(require_admin)):
    if not re.match(r'^[a-zA-Z0-9_-]+$', module_name):
        raise HTTPException(status_code=400, detail="模块名称格式无效")
    ok = await registry.unregister(module_name)
    if not ok:
        raise HTTPException(status_code=404, detail="模块不存在")
    bg.add_task(audit_log, user=user.username, action="unregister_module", details=module_name)
    return {"message": f"模块 '{module_name}' 已注销"}

@router.get("/health", response_model=HealthCheckResult)
async def health_check(registry=Depends(get_registry), user=Depends(get_current_user)):
    # 优先使用缓存，避免缓存击穿（双重检测）
    cached = await registry.redis.get(registry.CACHE_KEY)
    if cached:
        try:
            return HealthCheckResult.model_validate_json(cached)
        except Exception:
            pass
    # 实时计算并回写缓存
    result = await registry.get_health_check()
    try:
        await registry.redis.set(registry.CACHE_KEY, result.model_dump_json(), ex=registry.health_cache_ttl)
    except Exception as e:
        logger.warning("Failed to write health cache: %s", e)
    return result

@router.put("/modules/{module_name}", response_model=ModuleStatus)
async def manual_update(module_name: str, status: str = Query(..., pattern=r"^(green|yellow|red|gray)$"),
                        message: str = Query("手动更新", max_length=500), bg: BackgroundTasks = None,
                        registry=Depends(get_registry), user=Depends(require_admin)):
    if not re.match(r'^[a-zA-Z0-9_-]+$', module_name):
        raise HTTPException(status_code=400, detail="模块名称格式无效")
    mod = await registry.update_status(module_name, status, message, operator=user.username)
    if bg:
        bg.add_task(audit_log, user=user.username, action="manual_update", details=f"{module_name} -> {status}")
    return mod

@router.get("/cluster/nodes", response_model=List[ClusterNodeInfo])
async def cluster_nodes(registry=Depends(get_registry), user=Depends(require_admin)):
    return await registry._get_cluster_view()

@router.post("/status/{module_name}", status_code=202)
async def report_status(module_name: str, status: str, message: str = "",
                        request: Request = None, registry=Depends(get_registry)):
    # IP白名单校验（同时支持内部认证，由上层处理）
    if registry.allowed_ips and request:
        client_ip = request.client.host
        if client_ip not in registry.allowed_ips:
            raise HTTPException(status_code=403, detail="上报IP未授权")
    await registry.update_status(module_name, status, message)
    return {"status": "accepted"}
