# -*- coding: utf-8 -*-
"""
模块名称: kline_repository.py
核心职责: 提供K线数据的持久化存储、批量写入、查询与清理功能，作为多周期K线缓冲的持久层
所属层级: adapters.storage

外部依赖:
    - sqlite3 (数据库引擎)
    - datetime (时间处理)
    - os, threading, logging, time, math (系统与并发)
    - typing (类型注解)
    - core.models.kline.Kline (K线数据结构)

接口契约:
    提供: {
        'KlineRepository': {
            'init_schema() -> None': '创建表与索引，迁移至最新结构',
            'bulk_insert(klines: List[Kline], interval: str) -> InsertResult': '批量插入，返回详情',
            'get_recent_klines(symbol: str, interval: str, limit: int) -> List[Kline]': '获取最近N根K线',
            'get_klines_range(symbol: str, interval: str, start_ms: int, end_ms: int, max_limit: int) -> List[Kline]': '按时间范围查询',
            'delete_old_klines(symbol: str, interval: str, before_ms: int, min_keep: int) -> int': '删除过期数据',
            'vacuum() -> None': '整理数据库空间',
            'get_stats() -> dict': '获取库统计信息',
            'ping() -> bool': '健康检查',
            'optimize() -> None': '定期优化'
        }
    }
    消费: {
        'core.models.kline.Kline': 'K线数据模型',
        'sqlite3': '数据库连接'
    }

配置项:
    - storage.sqlite_path (str, 'data/khaos_klines.db'): 数据库文件路径
    - storage.retention.kline_retention_days (int, 90): K线保留天数

作者: KHAOS Data Team
创建日期: 2025-05-15
修改记录:
    - 2026-07-14 第三轮100项缺陷修复，重铸机构级数据基石
"""

import os
import sqlite3
import threading
import logging
import time
import math
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, NamedTuple

from core.models.kline import Kline

logger = logging.getLogger(__name__)

# 周期别名映射
INTERVAL_ALIASES = {
    "1min": "1m", "3min": "3m", "5min": "5m", "15min": "15m",
    "30min": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w",
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w",
}
VALID_INTERVALS = frozenset(INTERVAL_ALIASES.values())


class InsertResult(NamedTuple):
    inserted: int
    skipped: int
    errors: int


class KlineRepository:
    """
    K线数据仓库，使用 SQLite 存储，支持高并发读取和批量写入。
    所有时间戳均为 UTC 毫秒。
    """

    def __init__(self, db_path: Optional[str] = None, retention_days: int = 90,
                 max_query_limit: int = 10000, synchronous: str = "NORMAL"):
        if db_path is None:
            data_dir = os.environ.get("KHAOS_DATA_DIR", "data")
            db_path = os.path.join(data_dir, "khaos_klines.db")
        self._db_path = os.path.expanduser(db_path)
        self._retention_days = retention_days
        self._max_query_limit = max_query_limit
        self._synchronous_mode = synchronous
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

        db_dir = os.path.dirname(self._db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    # ------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------
    def __enter__(self):
        self._ensure_connection_public()
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------
    # 连接管理 (重构：避免死锁)
    # ------------------------------------------------------------
    def _ensure_connection_public(self) -> sqlite3.Connection:
        """公共方法：获取可用连接（内部加锁）"""
        with self._lock:
            return self._get_connection_locked()

    def _get_connection_locked(self) -> sqlite3.Connection:
        """必须在持有 self._lock 时调用"""
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1 LIMIT 1")
                return self._conn
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                logger.warning("连接失效，重新建立")
                self._close_connection_locked()
        self._conn = self._create_connection_locked()
        return self._conn

    def _create_connection_locked(self) -> sqlite3.Connection:
        for attempt in range(3):
            try:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                conn.execute(f"PRAGMA synchronous={self._synchronous_mode};")
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA temp_store=MEMORY;")
                conn.execute("PRAGMA cache_size=-8000;")
                conn.execute("PRAGMA busy_timeout=5000;")
                conn.execute("PRAGMA wal_autocheckpoint=1000;")
                conn.execute("PRAGMA mmap_size=134217728;")  # 128MB
                conn.isolation_level = None
                conn.row_factory = sqlite3.Row
                logger.info("数据库连接已建立: %s", os.path.basename(self._db_path))
                return conn
            except sqlite3.OperationalError as e:
                logger.error("创建连接失败 (尝试 %d/3): %s", attempt+1, e)
                time.sleep(1)
        raise RuntimeError("无法连接到数据库")

    def _close_connection_locked(self):
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    # ------------------------------------------------------------
    # Schema 管理
    # ------------------------------------------------------------
    def init_schema(self) -> None:
        """初始化表结构，确保唯一索引存在"""
        with self._lock:
            conn = self._get_connection_locked()
            # 基础表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS klines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    close_time_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    quote_volume REAL DEFAULT 0.0,
                    trades INTEGER DEFAULT 0,
                    synthetic INTEGER DEFAULT 0
                )
            """)
            # 索引
            for sql in [
                "CREATE INDEX IF NOT EXISTS idx_klines_sym_int_time ON klines(symbol, interval, open_time_ms)",
                "CREATE INDEX IF NOT EXISTS idx_klines_close_time ON klines(close_time_ms)",
            ]:
                conn.execute(sql)
            # 唯一索引，防止重复
            try:
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_klines_unique ON klines(symbol, interval, open_time_ms)")
            except sqlite3.IntegrityError:
                logger.error("无法创建唯一索引，表中可能存在重复数据，请清理后再启动。")
                raise
            conn.commit()

    # ------------------------------------------------------------
    # 数据校验与标准化
    # ------------------------------------------------------------
    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        s = symbol.strip().upper()
        if not s or len(s) > 30:
            raise ValueError(f"无效 symbol: {symbol}")
        return s

    @staticmethod
    def _normalize_interval(interval: str) -> str:
        i = interval.strip().lower()
        i = INTERVAL_ALIASES.get(i, i)
        if i not in VALID_INTERVALS:
            raise ValueError(f"无效 interval: {interval}")
        return i

    @staticmethod
    def _safe_float(val, default=0.0) -> float:
        try:
            v = float(val)
            if math.isnan(v) or math.isinf(v):
                return default
            return v
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(val, default=0) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _validate_kline(self, k: Kline) -> bool:
        """严格校验单根K线数据，返回是否有效"""
        if not hasattr(k, 'symbol') or not k.symbol:
            return False
        if getattr(k, 'open_time', None) is None or getattr(k, 'close_time', None) is None:
            return False
        open_t = self._safe_int(k.open_time)
        close_t = self._safe_int(k.close_time)
        if open_t < 0 or close_t < 0:
            return False
        try:
            high = self._safe_float(k.high, -1)
            low = self._safe_float(k.low, -1)
            if high < low:
                logger.warning("K线 high(%s) < low(%s)，数据异常，跳过", high, low)
                return False
            for val in (k.open, k.close, k.volume):
                if self._safe_float(val, -1) < 0:
                    return False
        except Exception:
            return False
        return True

    # ------------------------------------------------------------
    # 批量插入
    # ------------------------------------------------------------
    def bulk_insert(self, klines: List[Kline], interval: str) -> InsertResult:
        """批量插入K线，返回 (插入数, 跳过数, 错误数)"""
        if not klines:
            return InsertResult(0, 0, 0)

        try:
            interval_std = self._normalize_interval(interval)
        except ValueError:
            return InsertResult(0, len(klines), len(klines))

        # 过滤并分组
        symbol_groups: Dict[str, List[Kline]] = {}
        skipped = errors = 0
        for k in klines:
            if not k or not self._validate_kline(k):
                errors += 1
                continue
            sym = self._normalize_symbol(k.symbol)
            symbol_groups.setdefault(sym, []).append(k)

        total_inserted = 0
        with self._lock:
            conn = self._get_connection_locked()
            for symbol, group in symbol_groups.items():
                # 排序以保证顺序
                group.sort(key=lambda x: x.open_time)
                for i in range(0, len(group), 500):
                    batch = group[i:i+500]
                    rows = []
                    for k in batch:
                        try:
                            rows.append((
                                symbol,
                                interval_std,
                                self._safe_int(k.open_time),
                                self._safe_int(k.close_time),
                                self._safe_float(k.open),
                                self._safe_float(k.high),
                                self._safe_float(k.low),
                                self._safe_float(k.close),
                                self._safe_float(k.volume),
                                self._safe_float(getattr(k, 'quote_volume', 0)),
                                self._safe_int(getattr(k, 'trades', 0)),
                                1 if getattr(k, 'synthetic', False) else 0
                            ))
                        except Exception:
                            errors += 1
                    if not rows:
                        continue

                    for retry in range(3):
                        try:
                            conn.execute("BEGIN IMMEDIATE")
                            cursor = conn.cursor()
                            cursor.executemany(
                                "INSERT OR IGNORE INTO klines "
                                "(symbol, interval, open_time_ms, close_time_ms, open, high, low, close, volume, quote_volume, trades, synthetic) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                rows
                            )
                            inserted_now = cursor.rowcount
                            conn.commit()
                            total_inserted += inserted_now
                            skipped += len(rows) - inserted_now
                            break
                        except sqlite3.OperationalError as e:
                            conn.rollback()
                            if "locked" in str(e) and retry < 2:
                                time.sleep(0.1 * (retry+1))
                            else:
                                logger.error("批量插入失败: %s", e)
                                errors += len(rows)
                                break
        return InsertResult(total_inserted, skipped, errors)

    # ------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------
    def get_recent_klines(self, symbol: str, interval: str, limit: int = 500) -> List[Kline]:
        symbol = self._normalize_symbol(symbol)
        interval = self._normalize_interval(interval)
        limit = max(1, min(limit, self._max_query_limit))

        with self._lock:
            conn = self._get_connection_locked()
            rows = conn.execute(
                "SELECT * FROM klines WHERE symbol=? AND interval=? "
                "ORDER BY open_time_ms DESC LIMIT ?",
                (symbol, interval, limit)
            ).fetchall()
        # 反转至升序，传入 interval
        return [self._row_to_kline(r, interval) for r in reversed(rows)]

    def get_klines_range(self, symbol: str, interval: str,
                         start_ms: int, end_ms: int,
                         max_limit: Optional[int] = None) -> List[Kline]:
        symbol = self._normalize_symbol(symbol)
        interval = self._normalize_interval(interval)
        if start_ms > end_ms:
            start_ms, end_ms = end_ms, start_ms
        limit = max_limit if max_limit else self._max_query_limit
        limit = max(1, min(limit, 50000))

        with self._lock:
            conn = self._get_connection_locked()
            rows = conn.execute(
                "SELECT * FROM klines WHERE symbol=? AND interval=? "
                "AND open_time_ms>=? AND open_time_ms<=? "
                "ORDER BY open_time_ms ASC LIMIT ?",
                (symbol, interval, start_ms, end_ms, limit)
            ).fetchall()
        if len(rows) == limit:
            logger.warning("范围查询达到上限 %d，数据可能截断", limit)
        return [self._row_to_kline(r, interval) for r in rows]

    # ------------------------------------------------------------
    # 数据清理
    # ------------------------------------------------------------
    def delete_old_klines(self, symbol: str, interval: str,
                          before_ms: Optional[int] = None,
                          min_keep_bars: Optional[int] = None) -> int:
        symbol = self._normalize_symbol(symbol)
        interval = self._normalize_interval(interval)
        if before_ms is None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
            before_ms = int(cutoff.timestamp() * 1000)
        if before_ms > int(datetime.now(timezone.utc).timestamp() * 1000):
            logger.warning("before_ms 是未来时间，取消删除")
            return 0
        if min_keep_bars is None:
            # 根据周期设定最小保留数
            if interval in ("1m", "3m", "5m"):
                min_keep_bars = 2000
            elif interval in ("15m", "30m"):
                min_keep_bars = 1000
            else:
                min_keep_bars = 500

        with self._lock:
            conn = self._get_connection_locked()
            total = conn.execute(
                "SELECT COUNT(*) FROM klines WHERE symbol=? AND interval=?",
                (symbol, interval)
            ).fetchone()[0]
            if total <= min_keep_bars:
                return 0
            max_deletable = total - min_keep_bars
            deleted = 0
            batch = 1000
            while deleted < max_deletable:
                cur_batch = min(batch, max_deletable - deleted)
                # 使用子查询避免不支持 DELETE ... LIMIT
                cur = conn.execute(
                    "DELETE FROM klines WHERE rowid IN ("
                    "SELECT rowid FROM klines WHERE symbol=? AND interval=? AND close_time_ms < ? "
                    "ORDER BY close_time_ms ASC LIMIT ?)",
                    (symbol, interval, before_ms, cur_batch)
                )
                if cur.rowcount == 0:
                    break
                deleted += cur.rowcount
                conn.commit()
            logger.info("清理 %s %s 过期数据: %d 行", symbol, interval, deleted)
            return deleted

    def vacuum(self) -> None:
        with self._lock:
            conn = self._get_connection_locked()
            try:
                file_size = os.path.getsize(self._db_path)
                if file_size < 10 * 1024 * 1024:
                    return
            except OSError:
                pass
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            conn.execute("VACUUM;")
            logger.info("数据库 vacuum 完成")

    def optimize(self) -> None:
        """定期优化：检查点 + 整理空间 + 更新统计"""
        with self._lock:
            conn = self._get_connection_locked()
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            conn.execute("PRAGMA optimize;")
            logger.info("数据库优化完成")

    # ------------------------------------------------------------
    # 辅助功能
    # ------------------------------------------------------------
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._get_connection_locked()
            cur = conn.execute("SELECT COUNT(*) FROM klines")
            total = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(DISTINCT symbol) FROM klines")
            symbols = cur.fetchone()[0]
        try:
            file_size = os.path.getsize(self._db_path)
        except OSError:
            file_size = 0
        return {
            "total_klines": total,
            "unique_symbols": symbols,
            "db_size_bytes": file_size,
            "retention_days": self._retention_days,
        }

    def ping(self) -> bool:
        try:
            with self._lock:
                conn = self._get_connection_locked()
                conn.execute("SELECT 1 LIMIT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            self._close_connection_locked()

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------
    @staticmethod
    def _row_to_kline(row: sqlite3.Row, interval: str = "") -> Kline:
        return Kline(
            symbol=row["symbol"],
            open_time=row["open_time_ms"],
            close_time=row["close_time_ms"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            quote_volume=float(row["quote_volume"]) if row["quote_volume"] is not None else 0.0,
            trades=int(row["trades"]) if row["trades"] is not None else 0,
            synthetic=bool(row["synthetic"]),
          )
