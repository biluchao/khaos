# -*- coding: utf-8 -*-
"""
模块名称: state_repository.py
核心职责: 持久化与恢复系统中有状态组件的内部状态，实现检查点机制，保障重启后策略连续性。
所属层级: adapters.storage

外部依赖:
    - sqlite3 (内建，或由 Database 抽象提供)
    - json, zlib, hashlib, hmac, os, time, struct, logging, typing, threading
    - cryptography (可选，用于加密)

接口契约:
    提供: {
        'StateRepository': {
            'save_state(component_id, state, kline_timestamp, metadata, return_id) -> Union[bool,int]': '保存状态',
            'load_state(component_id) -> Optional[dict]': '加载最近状态',
            'load_state_at(component_id, timestamp) -> Optional[dict]': '加载指定时间状态',
            'delete_state(component_id) -> None': '删除组件状态',
            'list_components(limit, offset) -> List[str]': '列出组件',
            'export_component(component_id) -> Generator[dict]': '流式导出',
            'import_component(component_id, versions) -> int': '导入版本',
            'exists(component_id) -> bool': '检查存在',
            'count_versions(component_id) -> int': '版本计数',
            'optimize() -> None': 'VACUUM',
            'validate_state(state) -> bool': '验证状态可序列化'
        }
    }
    消费: {
        'adapters.storage.database.Database': '提供 execute, fetch_one, fetch_all, fetch_all_stream, transaction, ping'
    }

配置项:
    - storage.checkpoint.max_versions (int, 10): 保留版本数
    - storage.checkpoint.compress (bool, true): 压缩存储
    - storage.checkpoint.integrity_check (bool, false): 哈希校验
    - storage.checkpoint.encrypt (bool, false): 加密存储
    - storage.checkpoint.max_state_size_bytes (int, 10MB): 单状态上限
    - storage.checkpoint.max_total_size_bytes (int, 500MB): 总存储上限
    - storage.checkpoint.ttl_days (int, 0): 自动过期天数

作者: KHAOS Data Team
创建日期: 2025-08-20
修改记录:
    - 2026-02-20 v4.0 第三轮机构审计：企业级加密、全局大小限制、TTL、标志位兼容、流式性能优化等
"""

import json
import logging
import time
import hashlib
import os
import struct
import sys
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from adapters.storage.database import Database

logger = logging.getLogger(__name__)

# 自定义异常
class StateError(Exception):
    """状态存储基础异常"""

class StateCorruptedError(StateError):
    """状态数据损坏"""

class StateTooLargeError(StateError):
    """状态超过允许大小"""

class StorageError(StateError):
    """底层存储错误"""

# 模块级别可选依赖检测
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


class StateRepository:
    """有状态组件的检查点存储库（机构级 v4.0）。"""

    DEFAULT_TABLE = "component_state"
    MAX_COMPONENT_ID_LEN = 255
    ALLOWED_TABLE_NAME_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
    KHAOS_VERSION = "4.0.0"
    COMPRESS_FLAG = 0x01          # 压缩标志位
    ENCRYPT_FLAG = 0x02          # 加密标志位（与压缩独立）
    DEFAULT_MAX_STATE_SIZE = 10 * 1024 * 1024
    DEFAULT_MAX_TOTAL_SIZE = 500 * 1024 * 1024

    def __init__(self,
                 db: Database,
                 max_versions: int = 10,
                 compress: bool = True,
                 compress_level: int = 6,
                 integrity_check: bool = False,
                 encrypt: bool = False,
                 encryption_password: Optional[str] = None,
                 encryption_salt: Optional[bytes] = None,
                 table_name: str = DEFAULT_TABLE,
                 max_state_size_bytes: int = DEFAULT_MAX_STATE_SIZE,
                 max_total_size_bytes: int = DEFAULT_MAX_TOTAL_SIZE,
                 ttl_days: int = 0):
        """
        Args:
            db: 数据库连接管理器
            max_versions: 每个组件保留版本数
            compress: 是否压缩存储
            compress_level: 压缩级别 1-9
            integrity_check: 是否计算 SHA256 哈希
            encrypt: 是否启用 AES-256-GCM 加密
            encryption_password: 加密密码（将经过 PBKDF2 派生密钥）
            encryption_salt: 派生盐值（16字节）
            table_name: 自定义表名
            max_state_size_bytes: 单个状态大小上限
            max_total_size_bytes: 所有状态总大小上限
            ttl_days: 状态自动过期天数（0 表示永不过期）
        """
        if max_versions < 1:
            raise ValueError("max_versions 至少为 1")
        if not self.ALLOWED_TABLE_NAME_RE.match(table_name):
            raise ValueError(f"非法表名: {table_name}")
        if len(table_name) > 64:
            raise ValueError("表名过长")

        self._db = db
        self._max_versions = max_versions
        self._compress = compress
        self._compress_level = compress_level
        self._integrity_check = integrity_check
        self._encrypt = encrypt
        self._table = table_name
        self._max_state_size = max_state_size_bytes
        self._max_total_size = max_total_size_bytes
        self._ttl_days = ttl_days
        self._lock = Lock()

        # 加密设置
        self._encryption_key = None
        if self._encrypt:
            if not _HAS_CRYPTO:
                raise StateError("加密功能需要安装 cryptography 库")
            if not encryption_password:
                raise StateError("加密密码不能为空")
            self._encryption_salt = encryption_salt or os.urandom(16)
            self._encryption_key = self._derive_key(encryption_password, self._encryption_salt)

        # 延迟验证数据库
        self._initialized = False

    # --------------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------------

    def save_state(self,
                   component_id: str,
                   state: Dict[str, Any],
                   kline_timestamp: Optional[float] = None,
                   metadata: Optional[Dict[str, Any]] = None,
                   return_id: bool = False) -> Union[bool, int]:
        """
        保存状态快照。若 return_id=True 返回新记录ID，否则返回 True。
        """
        self._lazy_init()
        if not component_id or len(component_id) > self.MAX_COMPONENT_ID_LEN:
            raise ValueError(f"component_id 长度 1-{self.MAX_COMPONENT_ID_LEN}")

        # 序列化
        state_json = json.dumps(state, cls=_ExtendedEncoder)
        if len(state_json) > self._max_state_size:
            raise StateTooLargeError(f"状态大小 {len(state_json)} 超限")

        state_blob = state_json.encode('utf-8')
        flags = 0x00
        if self._compress:
            state_blob = zlib.compress(state_blob, self._compress_level)
            flags |= self.COMPRESS_FLAG
        if self._encrypt and self._encryption_key:
            state_blob = self._encrypt_data(state_blob, self._encryption_key)
            flags |= self.ENCRYPT_FLAG
        # 添加标志字节
        state_blob = struct.pack('B', flags) + state_blob

        # 加密后大小检查
        if len(state_blob) > self._max_state_size:
            raise StateTooLargeError("加密/压缩后状态大小超限")

        state_hash = ""
        if self._integrity_check:
            state_hash = hashlib.sha256(state_blob).hexdigest()

        created_at = datetime.now(timezone.utc).isoformat()
        created_ts = time.time_ns() / 1e9
        meta = metadata.copy() if metadata else {}
        meta.setdefault('khaos_version', self.KHAOS_VERSION)
        meta_json = json.dumps(meta, cls=_ExtendedEncoder)

        # 重试保存
        last_exc = None
        for attempt in range(3):
            try:
                with self._lock:
                    with self._db.transaction():
                        cur = self._db.execute(
                            f"INSERT INTO {self._table} "
                            "(component_id, state_blob, state_hash, kline_timestamp, created_at, created_ts, metadata) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (component_id, state_blob, state_hash, kline_timestamp, created_at, created_ts, meta_json)
                        )
                        new_id = cur.lastrowid
                        self._cleanup_old_versions(component_id, self._max_versions)
                        self._enforce_total_size()
                        self._expire_old_states()
                        logger.info(f"状态已保存: {component_id} (id={new_id})")
                        return new_id if return_id else True
            except Exception as e:
                last_exc = e
                wait = (2 ** attempt) * 0.1
                time.sleep(wait)
        raise StorageError(f"保存失败，已重试: {last_exc}")

    def load_state(self, component_id: str) -> Optional[Dict[str, Any]]:
        """加载最近状态。"""
        self._lazy_init()
        row = self._db.fetch_one(
            f"SELECT state_blob, state_hash FROM {self._table} "
            "WHERE component_id = ? ORDER BY id DESC LIMIT 1",
            (component_id,)
        )
        if not row:
            return None
        state = self._deserialize(row[0], row[1])
        if state is None:
            raise StateCorruptedError(f"组件 {component_id} 的状态数据损坏")
        return state

    def load_state_at(self, component_id: str, timestamp: Optional[float]) -> Optional[Dict[str, Any]]:
        """加载不晚于指定时间戳的最新状态。若 timestamp 为 None 则等同 load_state。"""
        if timestamp is None:
            return self.load_state(component_id)
        self._lazy_init()
        row = self._db.fetch_one(
            f"SELECT state_blob, state_hash FROM {self._table} "
            "WHERE component_id = ? AND kline_timestamp <= ? "
            "ORDER BY kline_timestamp DESC, id DESC LIMIT 1",
            (component_id, timestamp)
        )
        if not row:
            return None
        state = self._deserialize(row[0], row[1])
        if state is None:
            raise StateCorruptedError(f"组件 {component_id} 的状态数据损坏")
        return state

    def delete_state(self, component_id: str) -> None:
        """删除组件所有版本。"""
        with self._lock:
            self._db.execute(f"DELETE FROM {self._table} WHERE component_id = ?", (component_id,))
            logger.info(f"已删除组件状态: {component_id}")

    def list_components(self, limit: int = 100, offset: int = 0) -> List[str]:
        """分页列出组件ID。"""
        rows = self._db.fetch_all(
            f"SELECT DISTINCT component_id FROM {self._table} ORDER BY component_id LIMIT ? OFFSET ?",
            (limit, offset)
        )
        return [row[0] for row in rows]

    def exists(self, component_id: str) -> bool:
        """检查组件是否存在状态记录。"""
        row = self._db.fetch_one(
            f"SELECT COUNT(*) FROM {self._table} WHERE component_id = ?",
            (component_id,)
        )
        return row[0] > 0 if row else False

    def count_versions(self, component_id: str) -> int:
        """返回组件版本数。"""
        row = self._db.fetch_one(
            f"SELECT COUNT(*) FROM {self._table} WHERE component_id = ?",
            (component_id,)
        )
        return row[0] if row else 0

    def cleanup_old_versions(self, component_id: str, keep: int) -> int:
        """手动清理旧版本，保留最近 keep 个。"""
        with self._lock:
            return self._cleanup_old_versions(component_id, keep)

    def export_component(self, component_id: str) -> Generator[Dict[str, Any], None, None]:
        """流式导出组件的所有版本。每次从数据库获取一批记录。"""
        BATCH_SIZE = 100
        offset = 0
        while True:
            rows = self._db.fetch_all(
                f"SELECT id, state_blob, state_hash, kline_timestamp, created_at, created_ts, metadata "
                f"FROM {self._table} WHERE component_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
                (component_id, BATCH_SIZE, offset)
            )
            if not rows:
                break
            for row in rows:
                state = self._deserialize(row[1], row[2])
                if state is None:
                    logger.warning(f"跳过损坏记录 {row[0]}")
                    continue
                yield {
                    "id": row[0],
                    "state": state,
                    "kline_timestamp": row[3],
                    "created_at": row[4],
                    "created_ts": row[5],
                    "metadata": json.loads(row[6]) if row[6] else {}
                }
            offset += BATCH_SIZE

    def import_component(self, component_id: str, versions: List[Dict[str, Any]]) -> int:
        """导入状态版本列表，分批提交，返回导入数量。"""
        count = 0
        batch = []
        for ver in versions:
            if 'state' not in ver:
                logger.warning("跳过缺少 state 的版本")
                continue
            batch.append(ver)
            if len(batch) >= 50:
                count += self._import_batch(component_id, batch)
                batch.clear()
        if batch:
            count += self._import_batch(component_id, batch)
        logger.info(f"导入组件 {component_id}，成功 {count} 版本")
        return count

    def optimize(self) -> None:
        """VACUUM 优化（耗时，建议在维护窗口调用）。"""
        logger.warning("开始 VACUUM，将阻塞所有操作")
        self._db.execute("VACUUM;")
        logger.info("VACUUM 完成")

    def validate_state(self, state: Dict[str, Any]) -> bool:
        """验证状态可成功序列化与反序列化（不写入数据库）。"""
        try:
            state_json = json.dumps(state, cls=_ExtendedEncoder)
            state_blob = state_json.encode('utf-8')
            if self._compress:
                state_blob = zlib.compress(state_blob)
            if self._encrypt and self._encryption_key:
                state_blob = self._encrypt_data(state_blob, self._encryption_key)
            # 尝试反序列化
            flags = (self.COMPRESS_FLAG if self._compress else 0) | (self.ENCRYPT_FLAG if self._encrypt else 0)
            state_blob = struct.pack('B', flags) + state_blob
            self._deserialize(state_blob, "")
            return True
        except Exception as e:
            logger.warning(f"状态验证失败: {e}")
            return False

    # --------------------------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------------------------

    def _lazy_init(self) -> None:
        """延迟初始化，确保数据库连接已建立。"""
        if not self._initialized:
            self._validate_db()
            self._ensure_table()
            self._initialized = True

    def _validate_db(self) -> None:
        """检查 Database 接口。"""
        required = ['execute', 'fetch_one', 'fetch_all', 'transaction']
        for method in required:
            if not callable(getattr(self._db, method, None)):
                raise StateError(f"Database 缺少方法: {method}")

    def _ensure_table(self) -> None:
        """创建表和索引。"""
        self._db.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component_id TEXT NOT NULL,
                state_blob BLOB NOT NULL,
                state_hash TEXT DEFAULT '',
                kline_timestamp REAL,
                created_at TEXT NOT NULL,
                created_ts REAL NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)
        # 索引
        try:
            self._db.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._table}_comp_id ON {self._table}(component_id, id DESC)")
        except Exception:
            pass
        try:
            self._db.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._table}_comp_kline ON {self._table}(component_id, kline_timestamp)")
        except Exception:
            pass
        # 性能优化
        self._db.execute("PRAGMA journal_mode=WAL;")
        self._db.execute("PRAGMA mmap_size=268435456;")

    def _cleanup_old_versions(self, component_id: str, keep: int) -> int:
        """保留最近 keep 个版本，返回删除数。"""
        if keep < 1:
            keep = 1
        cnt = self._db.fetch_one(
            f"SELECT COUNT(*) FROM {self._table} WHERE component_id = ?", (component_id,)
        )[0]
        if cnt <= keep:
            return 0
        cur = self._db.execute(
            f"DELETE FROM {self._table} WHERE component_id = ? AND id NOT IN (SELECT id FROM {self._table} WHERE component_id = ? ORDER BY id DESC LIMIT ?)",
            (component_id, component_id, keep)
        )
        return cur.rowcount

    def _enforce_total_size(self) -> None:
        """如果总大小超过限制，删除最旧的状态直到满足。"""
        if self._max_total_size <= 0:
            return
        row = self._db.fetch_one(
            f"SELECT SUM(LENGTH(state_blob)) FROM {self._table}"
        )
        total = row[0] or 0
        while total > self._max_total_size:
            # 删除最旧的记录
            cur = self._db.execute(
                f"DELETE FROM {self._table} WHERE id = (SELECT MIN(id) FROM {self._table})"
            )
            if cur.rowcount == 0:
                break
            total = self._db.fetch_one(f"SELECT SUM(LENGTH(state_blob)) FROM {self._table}")[0] or 0
            logger.warning("状态总大小超限，已删除最旧记录")

    def _expire_old_states(self) -> None:
        """删除超过 TTL 的状态。"""
        if self._ttl_days <= 0:
            return
        cutoff = time.time() - self._ttl_days * 86400
        cur = self._db.execute(
            f"DELETE FROM {self._table} WHERE created_ts < ?", (cutoff,)
        )
        if cur.rowcount:
            logger.info(f"清理过期状态 {cur.rowcount} 条")

    def _import_batch(self, component_id: str, batch: List[Dict[str, Any]]) -> int:
        """导入一批版本。"""
        count = 0
        try:
            with self._lock:
                with self._db.transaction():
                    for ver in batch:
                        try:
                            state_json = json.dumps(ver['state'], cls=_ExtendedEncoder)
                            state_blob = state_json.encode('utf-8')
                            flags = 0x00
                            if self._compress:
                                state_blob = zlib.compress(state_blob, self._compress_level)
                                flags |= self.COMPRESS_FLAG
                            if self._encrypt and self._encryption_key:
                                state_blob = self._encrypt_data(state_blob, self._encryption_key)
                                flags |= self.ENCRYPT_FLAG
                            state_blob = struct.pack('B', flags) + state_blob
                            state_hash = ""
                            if self._integrity_check:
                                state_hash = hashlib.sha256(state_blob).hexdigest()
                            kts = ver.get('kline_timestamp')
                            cat = ver.get('created_at', datetime.now(timezone.utc).isoformat())
                            cts = ver.get('created_ts', time.time())
                            meta = ver.get('metadata', {})
                            meta_json = json.dumps(meta, cls=_ExtendedEncoder)
                            self._db.execute(
                                f"INSERT INTO {self._table} "
                                "(component_id, state_blob, state_hash, kline_timestamp, created_at, created_ts, metadata) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (component_id, state_blob, state_hash, kts, cat, cts, meta_json)
                            )
                            count += 1
                        except Exception as e:
                            logger.error(f"导入版本失败: {e}")
                    self._cleanup_old_versions(component_id, self._max_versions)
        except Exception as e:
            logger.error(f"批次导入失败: {e}")
        return count

    def _deserialize(self, state_blob: bytes, state_hash: str) -> Optional[Dict[str, Any]]:
        """反序列化 blob，支持标志位、加密、压缩。"""
        if not state_blob:
            return None
        # 哈希校验
        if self._integrity_check and state_hash:
            computed = hashlib.sha256(state_blob).hexdigest()
            if computed != state_hash:
                logger.error("状态完整性校验失败")
                return None
        # 解析标志位
        flags = 0x00
        if len(state_blob) > 0:
            first_byte = state_blob[0]
            if first_byte <= 0x03:  # 兼容标志位范围
                flags = first_byte
                state_blob = state_blob[1:]
            else:
                # 旧数据无标志位，尝试自动检测
                pass
        # 解密
        if flags & self.ENCRYPT_FLAG:
            if not self._encryption_key:
                logger.error("加密数据但未提供密钥")
                return None
            try:
                state_blob = self._decrypt_data(state_blob, self._encryption_key)
            except Exception as e:
                logger.error(f"解密失败: {e}")
                return None
        # 解压
        if flags & self.COMPRESS_FLAG:
            try:
                state_blob = zlib.decompress(state_blob)
            except zlib.error:
                # 可能旧数据无标志位但已压缩，尝试解压
                try:
                    state_blob = zlib.decompress(state_blob)
                except zlib.error:
                    logger.error("解压失败")
                    return None
        else:
            # 尝试解压旧数据（无标志位且默认压缩时）
            if self._compress:
                try:
                    state_blob = zlib.decompress(state_blob)
                except zlib.error:
                    # 视为未压缩
                    pass
        try:
            state_json = state_blob.decode('utf-8')
            return json.loads(state_json)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.error(f"状态反序列化失败: {e}")
            return None

    def _encrypt_data(self, data: bytes, key: bytes) -> bytes:
        """AES-256-GCM 加密，返回 nonce + ciphertext + tag。"""
        nonce = os.urandom(12)
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce), backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(data) + encryptor.finalize()
        return nonce + ciphertext + encryptor.tag

    def _decrypt_data(self, data: bytes, key: bytes) -> bytes:
        """AES-256-GCM 解密。"""
        nonce = data[:12]
        tag = data[-16:]
        ciphertext = data[12:-16]
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        """PBKDF2 派生 256-bit 密钥。"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600000,
            backend=default_backend()
        )
        return kdf.derive(password.encode('utf-8'))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


# 辅助编码器
class _ExtendedEncoder(json.JSONEncoder):
    """支持 numpy, datetime, Decimal, Path, bytes, set 等类型。"""
    def default(self, obj):
        if _HAS_NUMPY:
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        try:
            from decimal import Decimal
            if isinstance(obj, Decimal):
                return str(obj)   # 保持精度
        except ImportError:
            pass
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, bytes):
            import base64
            return {"__bytes__": base64.b64encode(obj).decode('ascii')}
        if isinstance(obj, set):
            return list(obj)
        try:
            from pathlib import Path
            if isinstance(obj, Path):
                return str(obj)
        except ImportError:
            pass
        if isinstance(obj, float) and not obj.is_finite():
            return None  # JSON 不支持 inf/nan
        return super().default(obj)
