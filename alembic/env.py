# -*- coding: utf-8 -*-
"""
Alembic 数据库迁移环境配置 (华尔街机构级 v3.0)
======================================================
功能: 为 KHAOS 系统提供同步/异步、在线/离线、自动批处理、
      多数据库方言、连接验证、重试、审计日志等全面的迁移保障。
维护: KHAOS Engineering
要求: Python >= 3.10, SQLAlchemy >= 2.0, Alembic >= 1.13
"""

import asyncio
import logging
import os
import sys
import time
import re
from copy import deepcopy
from logging.config import fileConfig
from typing import Any, Dict, Optional
from urllib.parse import quote_plus, urlparse, urlunparse

from alembic import context
from sqlalchemy import create_engine, inspect, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 所有模型必须在此处导入，以确保 Base.metadata 完整
from adapters.storage.database import Base

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CONFIG_KEY_SQLALCHEMY_URL = "sqlalchemy.url"
ENV_DATABASE_URL = "DATABASE_URL"
DEFAULT_CHARSET = "utf8mb4"

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception as e:
        print(f"Warning: Could not load Alembic log config: {e}", file=sys.stderr)

logger = logging.getLogger("alembic.env")

# ---------------------------------------------------------------------------
# 目标元数据
# ---------------------------------------------------------------------------
target_metadata = Base.metadata
if target_metadata is None:
    logger.critical("Base.metadata 为 None，请检查模型导入。")
    sys.exit(1)
if len(target_metadata.tables) == 0:
    logger.warning("目标元数据中没有任何表，是否遗漏了模型导入？")

# ---------------------------------------------------------------------------
# 数据库 URL 处理 (强健壮性)
# ---------------------------------------------------------------------------
def _normalize_url(raw_url: str) -> str:
    """去除引号、空格，并添加默认字符集（若未指定）"""
    url = raw_url.strip().strip("'\"")
    # 简单处理：如果 url 不包含 charset，且为 mysql/pymysql 等，追加
    if "charset" not in url and any(d in url for d in ["mysql", "mariadb"]):
        if "?" in url:
            url += f"&charset={DEFAULT_CHARSET}"
        else:
            url += f"?charset={DEFAULT_CHARSET}"
    return url

def _get_database_url() -> str:
    """获取数据库 URL，优先级：环境变量 > alembic 配置，并进行标准化"""
    url = os.getenv(ENV_DATABASE_URL)
    if not url:
        url = context.config.get_main_option(CONFIG_KEY_SQLALCHEMY_URL)
    if not url:
        logger.critical(
            "未配置数据库 URL。请设置环境变量 %s 或在 alembic 配置中指定 %s。",
            ENV_DATABASE_URL, CONFIG_KEY_SQLALCHEMY_URL,
        )
        sys.exit(1)
    return _normalize_url(url)

DATABASE_URL = _get_database_url()

def _safe_log_url(url: str) -> str:
    """脱敏 URL：隐藏密码，使用 URL 解析"""
    try:
        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username}:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            parsed = parsed._replace(netloc=netloc)
            return urlunparse(parsed)
        return url
    except Exception:
        # 回退简单正则
        return re.sub(r"://[^:]+:[^@]+@", "://***:***@", url)

logger.info("数据库迁移目标: %s", _safe_log_url(DATABASE_URL))

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_async_url(url: str) -> bool:
    """判断是否为异步数据库 URL"""
    return "+async" in url or any(
        url.startswith(prefix) for prefix in [
            "postgresql+asyncpg", "mysql+aiomysql", "mariadb+aiomysql",
            "sqlite+aiosqlite",
        ]
    )

def get_section_safe(section_name: str) -> Dict[str, Any]:
    """安全获取配置段，返回深拷贝，避免修改原配置"""
    section = context.config.get_section(section_name)
    if section is None:
        return {}
    return {k: v for k, v in deepcopy(section).items() if v is not None}

def get_bool_option(section: Dict[str, Any], key: str, default: bool = False) -> bool:
    """从配置段中获取布尔选项"""
    val = section.get(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")

# ---------------------------------------------------------------------------
# 上下文配置
# ---------------------------------------------------------------------------
def configure_context(connection: Connection, **kwargs: Any) -> None:
    """根据数据库方言和用户设置配置迁移上下文"""
    dialect = connection.engine.dialect.name
    opts: Dict[str, Any] = {
        "connection": connection,
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        "compare_indexes": True,
        "compare_unique": True,
        "compare_identity": True,
    }
    opts.update(kwargs)

    # 批量模式检测
    render_as_batch = os.getenv("ALEMBIC_BATCH_MODE", "").lower() == "force" or dialect == "sqlite"
    if render_as_batch:
        logger.info("启用批量迁移模式 (SQLite 或强制)")
        opts["render_as_batch"] = True

    context.configure(**opts)

# ---------------------------------------------------------------------------
# 连接验证与重试
# ---------------------------------------------------------------------------
def _verify_connection(connection: Connection) -> None:
    """验证数据库连接有效性"""
    try:
        connection.execute(text("SELECT 1"))
        logger.debug("数据库连接验证成功")
    except Exception:
        logger.exception("数据库连接验证失败")
        raise

def _retry_on_failure(func: callable, max_retries: int = 3, backoff_base: float = 1.0) -> None:
    """重试装饰器，指数退避"""
    for attempt in range(1, max_retries + 1):
        try:
            func()
            return
        except Exception as e:
            if attempt == max_retries:
                logger.error("重试 %d 次后仍然失败", max_retries)
                raise
            delay = backoff_base * (2 ** (attempt - 1))
            logger.warning("第 %d 次尝试失败，%0.1f 秒后重试: %s", attempt, delay, e)
            time.sleep(delay)

# ---------------------------------------------------------------------------
# 迁移模式实现
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """离线模式：生成 SQL 文件"""
    logger.info("启动离线迁移模式...")
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
        output_encoding="utf-8",
    )
    with context.begin_transaction():
        context.run_migrations()
    logger.info("离线迁移 SQL 生成完毕")

def do_run_migrations(connection: Connection) -> None:
    """在线迁移执行体（回调）"""
    assert connection is not None
    _verify_connection(connection)

    # SQLite 需要调整外键和事务
    if connection.engine.dialect.name == "sqlite":
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        logger.debug("SQLite 外键检查已临时关闭")
        try:
            configure_context(connection)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(text("PRAGMA foreign_keys=ON"))
    else:
        configure_context(connection)
        with context.begin_transaction():
            context.run_migrations()

def _log_migration_progress() -> None:
    """记录当前迁移版本和目标版本"""
    from alembic import migration
    cfg = context.config
    script = cfg.get_main_option("script_location", "alembic")
    # 获取当前数据库版本（在线模式下可用）
    # 此处简化，只记录 head
    logger.info("迁移目标版本: head")

def run_sync_online() -> None:
    """同步在线迁移"""
    logger.info("启动同步在线迁移...")
    section = get_section_safe(context.config.config_ini_section)
    engine_opts: Dict[str, Any] = {
        "poolclass": pool.NullPool,
        "echo": get_bool_option(section, "sqlalchemy.echo", False),
    }
    # 处理 connect_args
    connect_args = section.get("sqlalchemy.connect_args")
    if connect_args:
        if isinstance(connect_args, str):
            import json
            try:
                connect_args = json.loads(connect_args)
            except Exception:
                logger.warning("无法解析 connect_args，忽略: %s", connect_args)
                connect_args = {}
        engine_opts["connect_args"] = connect_args

    engine = create_engine(DATABASE_URL, **engine_opts)
    try:
        with engine.connect() as connection:
            _log_migration_progress()
            do_run_migrations(connection)
        logger.info("同步迁移成功")
    except Exception:
        logger.exception("同步迁移失败")
        raise
    finally:
        try:
            engine.dispose()
        except Exception:
            logger.warning("释放同步引擎时发生错误")

def run_async_online() -> None:
    """异步在线迁移"""
    logger.info("启动异步在线迁移...")
    section = get_section_safe(context.config.config_ini_section)
    # 构建带有前缀的配置字典
    configuration = {f"sqlalchemy.{k}": v for k, v in section.items() if v is not None}
    configuration["sqlalchemy.url"] = DATABASE_URL
    # 强制使用 NullPool
    configuration["sqlalchemy.poolclass"] = pool.NullPool
    echo = get_bool_option(section, "sqlalchemy.echo", False)
    if echo:
        configuration["sqlalchemy.echo"] = "True"

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_verify_connection)
            _log_migration_progress()
            await connection.run_sync(do_run_migrations)
        logger.info("异步迁移成功")
    except Exception:
        logger.exception("异步迁移失败")
        raise
    finally:
        try:
            await connectable.dispose()
        except Exception:
            logger.warning("释放异步引擎时发生错误")

def run_migrations_online() -> None:
    """在线迁移入口，根据 URL 自动选择同步/异步"""
    if is_async_url(DATABASE_URL):
        run_async_online()
    else:
        run_sync_online()

# ---------------------------------------------------------------------------
# 顶层入口：离线判断
# ---------------------------------------------------------------------------
start_time = time.time()
logger.info("迁移开始")

try:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
    elapsed = time.time() - start_time
    logger.info("迁移成功完成，耗时 %.2f 秒", elapsed)
except Exception:
    logger.exception("迁移过程中发生不可恢复的错误")
    sys.exit(1)
