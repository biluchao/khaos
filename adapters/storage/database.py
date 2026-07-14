# -*- coding: utf-8 -*-
"""
模块名称: database.py
核心职责: 提供统一、可切换的异步数据库连接管理，支持 SQLite 与 PostgreSQL，
          保证金融级数据完整性、并发安全与审计追溯。
所属层级: adapters.storage

外部依赖:
    - sqlalchemy[asyncio] >= 2.0
    - aiosqlite >= 0.17.0 (SQLite 异步驱动，可选)
    - asyncpg >= 0.28.0 (PostgreSQL 异步驱动，可选)

接口契约:
    提供: {
        'DatabaseManager': {
            'init(config)',
            'get_session() -> AsyncIterator[AsyncSession]',
            'get_write_session() -> AsyncIterator[AsyncSession]',
            'close()',
            'health_check(timeout, max_retries) -> bool',
            'create_tables()',
            'get_pool_stats() -> dict',
            'get_database_size() -> dict',
            'dangerously_drop_all_tables(confirm)',
            'is_initialized -> bool'
        },
        'get_database_manager(config, cache, force_reinit) -> DatabaseManager',
        'reset_instance()'
    }

配置项:
    storage.engine: 'sqlite' | 'postgresql'
    storage.database_url: 可选，直接指定完整连接字符串
    storage.sqlite_path: SQLite 文件路径
    storage.engine_alternative.postgresql.*: PG 连接参数
    storage.echo_sql: 是否打印 SQL
    storage.auto_create_tables: 自动建表（生产禁用）
    storage.allow_create_tables_in_production: 生产环境允许建表
    storage.deployment_environment: 'development' / 'production'
    storage.pool_size: 连接池大小（默认 SQLite 3，PG 5）
    storage.init_retry_attempts: 初始化重试次数（默认 1）

作者: KHAOS Infrastructure Team
创建日期: 2025-03-20
修改记录:
    - v1.0 初始版本
    - v2.0 PostgreSQL 支持、连接池、健康检查
    - v3.0 极限审计：死锁修复、权限强化、生产保护
    - v4.0 血洗审计：写锁串行化、数据完整性强化、factory、reset
    - v5.0 第五次穿透审计：URI编码修正、事务控制、安全加固、重试机制
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from sqlalchemy import MetaData, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from urllib.parse import quote_plus, urlencode

logger = logging.getLogger(__name__)

# 数据库命名约定（SQLAlchemy 最佳实践）
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class DatabaseManager:
    """
    异步数据库连接管理器（金融级）。
    """

    def __init__(self):
        self._engine: Optional[AsyncEngine] = None
        self._sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()      # SQLite 写入串行化
        self._initialized = False
        self._config: Dict[str, Any] = {}
        self._event_loop_id: Optional[int] = None

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @classmethod
    @asynccontextmanager
    async def create(cls, config: Dict[str, Any]) -> AsyncIterator["DatabaseManager"]:
        """工厂方法：创建并初始化数据库管理器，自动关闭。"""
        db = cls()
        await db.init(config)
        try:
            yield db
        finally:
            await db.close()

    def __repr__(self) -> str:
        return f"<DatabaseManager initialized={self._initialized} engine={self._engine}>"

    # --------------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------------

    async def init(self, config: Dict[str, Any]) -> None:
        """初始化数据库连接，支持重试。"""
        if not config:
            raise ValueError("config 不能为空")

        retries = config.get("init_retry_attempts", 1)
        last_exc = None

        for attempt in range(retries):
            try:
                async with self._lock:
                    if self._initialized:
                        logger.warning("数据库管理器已经初始化，忽略重复调用")
                        return

                    self._event_loop_id = id(asyncio.get_running_loop())
                    self._config = config

                    engine_type = config.get("engine", "sqlite").lower()

                    try:
                        if database_url := config.get("database_url"):
                            await self._init_from_url(database_url, config)
                        elif engine_type == "sqlite":
                            await self._init_sqlite(config)
                        elif engine_type in ("postgresql", "postgres", "pg"):
                            await self._init_postgresql(config)
                        else:
                            raise ValueError(f"不支持的数据库引擎: {engine_type}")

                        await self._log_version()

                        if config.get("auto_create_tables", False):
                            await self.create_tables()

                        self._initialized = True
                        logger.info("数据库管理器初始化完成")
                        return
                    except Exception:
                        await self._cleanup_on_failure()
                        raise
            except Exception as e:
                last_exc = e
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"数据库初始化失败，{wait}秒后重试... ({e})")
                    await asyncio.sleep(wait)
        raise RuntimeError(f"数据库初始化失败，已重试{retries}次: {last_exc}")

    @asynccontextmanager
    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """获取读会话（支持并发读）。"""
        if not self._sessionmaker:
            raise RuntimeError("数据库尚未初始化，请先调用 init()")
        session = self._sessionmaker()
        try:
            yield session
        except Exception:
            if session.in_transaction() is not None:
                try:
                    await session.rollback()
                except Exception as rollback_err:
                    logger.error("事务回滚失败", exc_info=rollback_err)
            raise
        finally:
            try:
                await session.close()
            except ResourceClosedError:
                pass

    @asynccontextmanager
    async def get_write_session(self) -> AsyncIterator[AsyncSession]:
        """获取写会话（SQLite 下串行化，自动管理事务）。"""
        async with self._write_lock:
            async with self.get_session() as session:
                async with session.begin():
                    yield session

    async def close(self) -> None:
        """关闭数据库连接池。"""
        await self._safe_dispose()

    async def health_check(self, timeout: float = 5.0, max_retries: int = 1) -> bool:
        """检测数据库是否可达，支持重试。"""
        if not self._engine:
            return False
        for attempt in range(max_retries):
            try:
                async with self._engine.connect() as conn:
                    await asyncio.wait_for(
                        conn.execute(text("SELECT 1")),
                        timeout=timeout
                    )
                return True
            except asyncio.TimeoutError:
                logger.warning(f"健康检查超时 (尝试 {attempt+1}/{max_retries})")
            except Exception as e:
                logger.error(f"数据库健康检查失败: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
        return False

    async def create_tables(self) -> None:
        """创建 ORM 表（生产环境请使用 Alembic）。"""
        if not self._engine:
            raise RuntimeError("数据库引擎未初始化")

        env = self._config.get("deployment_environment", "development")
        allow = self._config.get("allow_create_tables_in_production", False)
        if env == "production" and not allow:
            raise RuntimeError("生产环境不允许自动创建表，请使用 Alembic 迁移")

        async with self.get_write_session() as session:
            await session.run_sync(Base.metadata.create_all, checkfirst=True)
        logger.info("数据库表创建/检查完成")

    def get_pool_stats(self) -> Dict[str, Any]:
        """获取连接池状态。"""
        if not self._engine:
            return {"status": "not_initialized"}
        pool = self._engine.pool
        try:
            return {
                "pool_size": pool.size(),
                "checked_in": pool.checkedin(),
                "overflow": pool.overflow(),
                "total": pool.total(),
            }
        except AttributeError:
            return {"type": "NullPool"}

    async def get_database_size(self) -> Dict[str, Any]:
        """获取数据库大小（人类可读）。"""
        if not self._engine:
            return {"size": "未知", "bytes": 0}
        try:
            if "sqlite" in str(self._engine.url):
                db_path = self._config.get("sqlite_path", "data/khaos.db")
                try:
                    size_bytes = os.path.getsize(db_path)
                except FileNotFoundError:
                    return {"size": "文件不存在", "bytes": 0}
                return {"size": self._human_size(size_bytes), "bytes": size_bytes}
            else:
                async with self._engine.connect() as conn:
                    result = await conn.execute(
                        text("SELECT pg_database_size(current_database())")
                    )
                    size_bytes = result.scalar()
                    return {"size": self._human_size(size_bytes), "bytes": size_bytes}
        except Exception as e:
            return {"error": str(e), "bytes": 0}

    async def dangerously_drop_all_tables(self, confirm: bool = False) -> None:
        """清空所有表（需确认）。"""
        if not confirm:
            raise ValueError("必须设置 confirm=True 才能执行此危险操作")
        if not self._engine:
            raise RuntimeError("数据库引擎未初始化")

        async with self.get_write_session() as session:
            await session.run_sync(Base.metadata.drop_all)
        logger.warning("所有数据库表已删除！")

    # --------------------------------------------------------------------------
    # 内部初始化方法
    # --------------------------------------------------------------------------

    async def _init_from_url(self, url: str, config: Dict[str, Any]) -> None:
        """通过完整 URL 初始化。"""
        try:
            make_url(url)
        except Exception as e:
            raise ValueError(f"无效的数据库 URL: {url}") from e

        echo = config.get("echo_sql", False)
        self._engine = create_async_engine(
            url,
            echo=echo,
            pool_size=config.get("pool_size", 5),
            pool_recycle=config.get("pool_recycle", 3600),
            pool_pre_ping=config.get("pool_pre_ping", True),
            connect_args=config.get("connect_args", {}),
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._log_connection_info(url)

    async def _init_sqlite(self, config: Dict[str, Any]) -> None:
        """初始化 SQLite。"""
        try:
            import aiosqlite  # noqa
        except ImportError:
            raise ImportError("需要安装 aiosqlite: pip install aiosqlite>=0.17.0")

        sqlite_path = Path(config.get("sqlite_path", "data/khaos.db")).resolve()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        # 设置目录权限（仅 Unix）
        if os.name != "nt":
            try:
                os.chmod(sqlite_path.parent, 0o700)
            except OSError as e:
                logger.debug(f"无法设置目录权限: {e}")

        echo = config.get("echo_sql", False)
        pool_size = config.get("pool_size", 3)

        # 使用 URI 格式，正确处理 Windows 路径
        file_uri = sqlite_path.as_uri()  # file:///C:/... 或 file:///home/...
        database_url = file_uri.replace("file:///", "sqlite+aiosqlite:///", 1)

        self._engine = create_async_engine(
            database_url,
            echo=echo,
            pool_size=pool_size,
            pool_recycle=config.get("pool_recycle", 1800),
        )

        @event.listens_for(self._engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")   # 金融级数据完整性
            except Exception:
                logger.warning("WAL 模式不可用，回退到 DELETE")
                cursor.execute("PRAGMA journal_mode=DELETE")
                cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

        # 设置数据库文件权限（仅 Unix）
        if os.name != "nt" and sqlite_path.exists():
            try:
                os.chmod(sqlite_path, 0o600)
            except OSError as e:
                logger.debug(f"无法设置数据库文件权限: {e}")

        # 测试可写性
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("CREATE TABLE IF NOT EXISTS _khaos_test (id int)"))
                await conn.execute(text("DROP TABLE IF EXISTS _khaos_test"))
                await conn.commit()
        except Exception as e:
            raise RuntimeError(f"SQLite 数据库不可写: {sqlite_path}") from e

        self._log_connection_info(database_url, mask=True)
        logger.info(f"SQLite 数据库已配置: {sqlite_path}")

    async def _init_postgresql(self, config: Dict[str, Any]) -> None:
        """初始化 PostgreSQL。"""
        try:
            import asyncpg  # noqa
        except ImportError:
            raise ImportError("需要安装 asyncpg: pip install asyncpg>=0.28.0")

        pg_config = config.get("engine_alternative", {}).get("postgresql", {})
        host = pg_config.get("host", "localhost")
        port = pg_config.get("port", 5432)
        user = pg_config.get("user", "postgres")
        password = pg_config.get("password", "")
        database = pg_config.get("database", "khaos_data")
        sslmode = pg_config.get("sslmode", "require")
        echo = config.get("echo_sql", False)
        pool_size = config.get("pool_size", 5)
        statement_timeout = config.get("statement_timeout_ms", 30000)

        encoded_password = quote_plus(password) if password else ""
        auth = f"{user}:{encoded_password}@" if user else ""
        base_url = f"postgresql+asyncpg://{auth}{host}:{port}/{database}"

        params = {
            "application_name": "khaos-trading-engine",
            "connect_timeout": "10",
            "sslmode": sslmode,
            "options": f"-c statement_timeout={statement_timeout}"
        }
        database_url = f"{base_url}?{urlencode(params)}"

        self._engine = create_async_engine(
            database_url,
            echo=echo,
            pool_size=pool_size,
            max_overflow=5,
            pool_recycle=3600,
            pool_pre_ping=True,
            connect_args={"server_settings": {"jit": "off"}},
        )

        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._log_connection_info(database_url, mask=True)
        logger.info(f"PostgreSQL 数据库已配置: {host}:{port}/{database}")

    # --------------------------------------------------------------------------
    # 内部辅助方法
    # --------------------------------------------------------------------------

    async def _log_version(self) -> None:
        """记录数据库版本。"""
        if not self._engine:
            return
        try:
            async with self._engine.connect() as conn:
                if "sqlite" in str(self._engine.url):
                    result = await asyncio.wait_for(
                        conn.execute(text("SELECT sqlite_version()")),
                        timeout=5.0
                    )
                else:
                    result = await asyncio.wait_for(
                        conn.execute(text("SELECT version()")),
                        timeout=5.0
                    )
                version = result.scalar()
                logger.info(f"数据库版本: {version}")
        except asyncio.TimeoutError:
            logger.warning("数据库版本查询超时")
        except Exception as e:
            logger.warning(f"无法获取数据库版本: {e}")

    async def _safe_dispose(self) -> None:
        """安全关闭引擎。"""
        async with self._lock:
            if self._engine:
                try:
                    await self._engine.dispose()
                except Exception as e:
                    logger.error(f"关闭数据库引擎时异常: {e}")
                finally:
                    self._engine = None
                    self._sessionmaker = None
                    self._initialized = False
                    self._event_loop_id = None
                    logger.info("数据库连接池已关闭")

    async def _cleanup_on_failure(self) -> None:
        """初始化失败时清理资源，不获取锁。"""
        if self._engine:
            try:
                await self._engine.dispose()
            except Exception:
                pass
            finally:
                self._engine = None
                self._sessionmaker = None
                self._initialized = False
                self._event_loop_id = None
                logger.warning("数据库初始化失败，已清理资源")

    def _log_connection_info(self, url: str, mask: bool = False) -> None:
        """记录连接信息，可选择遮蔽密码。"""
        if mask:
            # 简单遮蔽密码
            display_url = url
            if "@" in url:
                parts = url.split("@")
                if ":" in parts[0]:
                    user_part = parts[0].split(":")[0]
                    display_url = f"{user_part}:***@{'@'.join(parts[1:])}"
            logger.debug(f"数据库连接: {display_url}")
        else:
            logger.debug(f"数据库连接: {url}")

    @staticmethod
    def _human_size(num_bytes: int) -> str:
        """将字节数转为人类可读的格式。"""
        if num_bytes == 0:
            return "0 B"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if num_bytes < 1024:
                return f"{num_bytes:.1f} {unit}"
            num_bytes /= 1024
        return f"{num_bytes:.1f} PB"


# --------------------------------------------------------------------------
# 全局单例管理
# --------------------------------------------------------------------------
_instance: Optional[DatabaseManager] = None
_instance_loop_id: Optional[int] = None


async def get_database_manager(
    config: Dict[str, Any],
    cache: bool = True,
    force_reinit: bool = False
) -> DatabaseManager:
    """
    获取全局唯一的 DatabaseManager 实例。

    Args:
        config: 数据库配置字典。
        cache: 是否使用缓存单例。
        force_reinit: 强制重新初始化（关闭旧实例并重建）。

    Returns:
        DatabaseManager 实例。
    """
    global _instance, _instance_loop_id

    if not cache:
        db = DatabaseManager()
        await db.init(config)
        return db

    current_loop_id = id(asyncio.get_running_loop())

    if _instance is not None and _instance_loop_id != current_loop_id:
        raise RuntimeError(
            "DatabaseManager 单例已在另一个事件循环中创建，"
            "请使用 cache=False 或确保在同一事件循环中。"
        )

    if _instance is not None and force_reinit:
        logger.warning("强制重新初始化数据库管理器，旧实例将被关闭")
        await _instance.close()
        _instance = None
        _instance_loop_id = None

    if _instance is None:
        _instance = DatabaseManager()
        await _instance.init(config)
        _instance_loop_id = current_loop_id
    elif not _instance.is_initialized:
        await _instance.init(config)

    return _instance


def reset_instance() -> None:
    """重置全局单例（仅用于测试环境）。"""
    global _instance, _instance_loop_id
    _instance = None
    _instance_loop_id = None


__all__ = [
    "Base",
    "DatabaseManager",
    "get_database_manager",
    "reset_instance",
    "NAMING_CONVENTION",
]
