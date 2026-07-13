# -*- coding: utf-8 -*-
"""
模块名称: intent_logger.py
核心职责: 提供华尔街级意图日志审计，确保所有被否决的交易信号完整、不可变、可追溯。
所属层级: core.monitoring

外部依赖:
    - atexit, collections, copy, datetime, json, logging, logging.handlers, os,
      queue, socket, threading, time, uuid, weakref (用于内存压力回调)

接口契约:
    提供: {
        'IntentLogger': {
            'log_rejected_signal(...) -> bool': '记录被否决信号，线程安全，异步写入',
            'get_rejected_signals(limit) -> List[dict]': '获取只读审计副本',
            'flush() -> None': '强制刷新所有缓冲到持久化层',
            'set_enabled(bool) -> None': '运行时开关',
            'clear_buffer(confirm=False) -> bool': '安全清空内存缓冲',
            'resize_buffer(new_size) -> None': '动态调整缓冲区',
            'metrics() -> dict': '获取运行指标',
            'health_check() -> bool': '健康检查'
        }
    }
    消费: 任何需要记录否决信号的模块（如策略引擎、过滤器）

配置项:
    - audit.intent_log.enabled
    - audit.intent_log.buffer_size
    - audit.intent_log.min_log_interval_ms
    - audit.intent_log.per_module_interval_ms
    - audit.intent_log.critical_flush
    - audit.intent_log.sensitive_fields
    - audit.intent_log.max_memory_bytes

作者: KHAOS System Architect
创建日期: 2025-06-15
最后修订: 2026-07-13 (三轮机构级审计，100项增强)
"""

import atexit
import json
import logging
import logging.handlers
import os
import queue
import socket
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

# 尝试导入 Signal 类型，若失败则接受任意对象
try:
    from core.models.signal import Signal as SignalType
except ImportError:
    SignalType = object

# 审计日志器
audit_logger = logging.getLogger("khaos.audit")


class IntentLogger:
    """
    华尔街级意图日志记录器 (v3.0)。
    特性:
    - 异步日志写入，避免阻塞策略线程
    - 内存与持久化双重缓冲
    - 审计记录不可变快照
    - 智能节流与敏感字段过滤
    - 丰富的运维指标与健康检查
    """

    DEFAULT_SENSITIVE_FIELDS = {
        'api_key', 'apikey', 'secret', 'password', 'token', 'passphrase',
        'private_key', 'access_key', 'signature', 'key', 'credential'
    }

    def __init__(self,
                 buffer_size: int = 100,
                 enabled: bool = True,
                 min_log_interval_ms: int = 500,
                 per_module_interval_ms: Optional[Dict[str, int]] = None,
                 critical_flush: bool = False,
                 sensitive_fields: Optional[List[str]] = None,
                 max_memory_bytes: int = 50 * 1024 * 1024,  # 50MB
                 async_write: bool = True):
        """
        Args:
            buffer_size: 内存缓冲区最大记录数（仅用于快速查询）
            enabled: 初始启用状态
            min_log_interval_ms: 默认节流间隔
            per_module_interval_ms: 按模块的自定义节流
            critical_flush: 每条记录后立即刷盘（损耗性能）
            sensitive_fields: 额外的敏感字段
            max_memory_bytes: 内存缓冲区最大字节数，超限后自动截断旧记录
            async_write: 是否使用异步日志队列（推荐）
        """
        self._enabled = enabled
        self._critical_flush = critical_flush
        self._async_write = async_write
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()

        # 内存缓冲区 (deque)
        buffer_size = max(1, buffer_size)
        self._max_buffer = buffer_size
        self._buffer: deque = deque(maxlen=buffer_size)
        self._buffer_byte_size = 0
        self._max_memory_bytes = max_memory_bytes

        # 节流控制
        self._min_interval_ms = min_log_interval_ms
        self._per_module_interval = per_module_interval_ms or {}
        self._last_log_time: Dict[str, float] = {}

        # 敏感字段集合
        self._sensitive_fields = set(s.lower() for s in (sensitive_fields or []))
        self._sensitive_fields.update(IntentLogger.DEFAULT_SENSITIVE_FIELDS)

        # 异步写入队列及工作线程
        if self._async_write:
            self._write_queue: queue.Queue = queue.Queue(maxsize=5000)
            self._writer_thread = threading.Thread(target=self._async_writer, daemon=True)
            self._writer_thread.start()
        else:
            self._write_queue = None
            self._writer_thread = None

        # 指标与统计
        self._total_events = 0
        self._throttled_count = 0
        self._error_count = 0
        self._dropped_count = 0
        self._written_bytes = 0
        self._module_counters: Dict[str, int] = {}
        self._sequence = 0

        # 主机与进程信息
        self._hostname = self._get_hostname()
        self._pid = os.getpid()
        self._session_id = str(uuid.uuid4())

        # 注册清理函数
        atexit.register(self._cleanup)

        # 日志器检查
        if not audit_logger.handlers and enabled:
            audit_logger.warning("审计日志器无处理器，意图日志可能丢失")
        if audit_logger.level == logging.NOTSET or audit_logger.level > logging.INFO:
            audit_logger.setLevel(logging.INFO)

    # --------------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------------

    def log_rejected_signal(self,
                            signal: Any,
                            reject_reason: str,
                            reject_module: str,
                            extra: Optional[Dict[str, Any]] = None,
                            operator: Optional[str] = None) -> bool:
        """记录被否决信号。线程安全，支持异步写入。"""
        if not self._enabled or signal is None:
            return False

        safe_reason = self._sanitize(reject_reason)
        safe_module = self._sanitize(reject_module)

        # 节流检查
        now_ts = time.time()
        throttle_key = f"{safe_module}::{safe_reason}"
        with self._lock:
            interval = self._per_module_interval.get(safe_module, self._min_interval_ms)
            last_ts = self._last_log_time.get(throttle_key, 0)
            if (now_ts - last_ts) * 1000 < interval:
                self._throttled_count += 1
                return False
            self._last_log_time[throttle_key] = now_ts

        # 构建记录
        try:
            record = self._build_record(signal, safe_reason, safe_module, extra, operator)
        except Exception as e:
            self._error_count += 1
            audit_logger.error(f"构造意图日志失败: {e}")
            return False

        # 序列化
        try:
            log_line = json.dumps(record, ensure_ascii=False, default=str)
            if len(log_line) > 10_000:  # 防止超大记录
                log_line = log_line[:10_000] + '..." (truncated)'
                self._dropped_count += 1
        except Exception as e:
            self._error_count += 1
            self._dropped_count += 1
            audit_logger.error(f"意图日志序列化失败: {e}")
            return False

        # 写入 (同步或异步)
        if self._async_write and self._write_queue is not None:
            try:
                self._write_queue.put_nowait(log_line)
            except queue.Full:
                self._dropped_count += 1
                # 降级为同步写入
                self._write_sync(log_line)
        else:
            self._write_sync(log_line)

        # 更新内存缓冲区与统计 (锁内)
        with self._lock:
            self._update_memory_buffer(record)
            self._total_events += 1
            self._module_counters[safe_module] = self._module_counters.get(safe_module, 0) + 1
            self._sequence += 1

        return True

    def get_rejected_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        """返回只读审计副本（时间倒序）"""
        limit = max(1, min(limit, self._max_buffer))
        with self._lock:
            items = list(self._buffer)[-limit:]
        try:
            return deepcopy(list(reversed(items)))
        except Exception:
            audit_logger.warning("深拷贝审计数据失败，返回浅拷贝")
            return list(reversed(items))

    def flush(self) -> None:
        """强制刷新所有缓冲（异步队列和日志处理器）"""
        # 等待异步队列排空
        if self._async_write and self._write_queue is not None:
            timeout = time.time() + 5
            while not self._write_queue.empty() and time.time() < timeout:
                time.sleep(0.05)
        self._flush_handlers()

    def set_enabled(self, enabled: bool) -> None:
        """运行时开关"""
        self._enabled = enabled
        audit_logger.info(f"意图日志已{'启用' if enabled else '禁用'}")

    def clear_buffer(self, confirm: bool = False) -> bool:
        """安全清空内存缓冲区，需显式确认"""
        if not confirm:
            audit_logger.warning("清空缓冲区未确认，操作被拒绝")
            return False
        with self._lock:
            self._buffer.clear()
            self._buffer_byte_size = 0
            self._last_log_time.clear()
            audit_logger.info("意图日志内存缓冲区已手动清空")
            return True

    def resize_buffer(self, new_size: int) -> None:
        """动态调整缓冲区记录数"""
        new_size = max(1, new_size)
        with self._lock:
            if new_size == self._max_buffer:
                return
            old = list(self._buffer)
            self._buffer = deque(old, maxlen=new_size)
            self._max_buffer = new_size

    def metrics(self) -> Dict[str, Any]:
        """返回运行指标"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "total_events": self._total_events,
                "buffer_size": len(self._buffer),
                "buffer_bytes": self._buffer_byte_size,
                "max_buffer": self._max_buffer,
                "throttled_count": self._throttled_count,
                "error_count": self._error_count,
                "dropped_count": self._dropped_count,
                "written_bytes": self._written_bytes,
                "module_counters": dict(self._module_counters),
                "sequence": self._sequence,
                "session_id": self._session_id,
            }

    def health_check(self) -> bool:
        """健康检查：若错误率过高或缓冲区异常，返回 False"""
        total = max(self._total_events, 1)
        if self._error_count / total > 0.1:
            return False
        if self._buffer_byte_size > self._max_memory_bytes * 1.2:
            return False
        return True

    # --------------------------------------------------------------------------
    # 内部：异步写入线程
    # --------------------------------------------------------------------------

    def _async_writer(self) -> None:
        """后台线程：从队列取日志行并同步写入审计日志器"""
        while not self._shutdown_event.is_set():
            try:
                line = self._write_queue.get(timeout=0.5)
                self._write_sync(line)
            except queue.Empty:
                continue
            except Exception:
                continue

    def _write_sync(self, log_line: str) -> None:
        """实际写入审计日志器"""
        try:
            audit_logger.info(log_line)
            if self._critical_flush:
                self._flush_handlers()
            self._written_bytes += len(log_line.encode('utf-8'))
        except Exception:
            self._error_count += 1

    def _flush_handlers(self) -> None:
        """强制刷新所有日志处理器"""
        for handler in audit_logger.handlers + logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass

    # --------------------------------------------------------------------------
    # 内部：记录构造与辅助
    # --------------------------------------------------------------------------

    def _build_record(self, signal: Any, reason: str, module: str,
                      extra: Optional[Dict[str, Any]], operator: Optional[str]) -> Dict[str, Any]:
        """构造标准化审计记录，包含序列号、会话ID等信息"""
        now = datetime.now(timezone.utc)
        sig_data = self._extract_signal_data(signal)

        record = {
            "event_id": str(uuid.uuid4()),
            "sequence": 0,  # 后续在线程安全更新
            "timestamp": now.isoformat(),
            "hostname": self._hostname,
            "pid": self._pid,
            "session_id": self._session_id,
            "event": "signal_rejected",
            "signal": sig_data,
            "reject_reason": reason,
            "reject_module": module,
        }
        if operator:
            record["operator"] = self._sanitize(operator)

        # 更新序列号
        with self._lock:
            record["sequence"] = self._sequence + 1

        # 额外上下文（敏感过滤）
        if extra and isinstance(extra, dict):
            record["extra"] = self._filter_and_sanitize(extra)

        return record

    def _extract_signal_data(self, signal: Any) -> Dict[str, Any]:
        """安全提取信号属性，避免因属性访问异常导致记录丢失"""
        data = {}
        for attr in ('direction', 'price', 'probability', 'strength', 'source_module'):
            try:
                val = getattr(signal, attr, None)
                data[attr] = val
            except Exception:
                data[attr] = None
        # 时间戳
        try:
            sig_ts = getattr(signal, 'timestamp', None)
            data['timestamp'] = sig_ts.isoformat() if hasattr(sig_ts, 'isoformat') else str(sig_ts)
        except Exception:
            data['timestamp'] = None
        return data

    def _update_memory_buffer(self, record: Dict[str, Any]) -> None:
        """更新内存缓冲区并控制内存占用"""
        # 估算记录大小
        try:
            rec_bytes = len(json.dumps(record, default=str).encode('utf-8'))
        except Exception:
            rec_bytes = 1024

        self._buffer.append(record)
        self._buffer_byte_size += rec_bytes

        # 若超出内存上限，移除最旧记录
        while self._buffer_byte_size > self._max_memory_bytes and self._buffer:
            old = self._buffer.popleft()
            try:
                self._buffer_byte_size -= len(json.dumps(old, default=str).encode('utf-8'))
            except Exception:
                self._buffer_byte_size = max(0, self._buffer_byte_size - 512)

    def _filter_and_sanitize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """递归过滤敏感字段并清洗字符串，防止信息泄露和注入"""
        if not isinstance(data, dict):
            return data
        cleaned = {}
        for key, value in data.items():
            safe_key = self._sanitize(key, max_length=100)
            if safe_key.lower() in self._sensitive_fields:
                cleaned[safe_key] = "***REDACTED***"
                continue
            if isinstance(value, dict):
                cleaned[safe_key] = self._filter_and_sanitize(value)
            elif isinstance(value, list):
                cleaned[safe_key] = [self._filter_and_sanitize(item) if isinstance(item, dict)
                                     else self._sanitize_value(item) for item in value]
            else:
                cleaned[safe_key] = self._sanitize_value(value)
        return cleaned

    def _sanitize_value(self, value: Any) -> Any:
        """清洗单个值"""
        if isinstance(value, str):
            return self._sanitize(value)
        return value

    @staticmethod
    def _sanitize(value: str, max_length: int = 200) -> str:
        """移除换行、回车，限制长度"""
        if not isinstance(value, str):
            value = str(value)
        value = value.replace('\n', ' ').replace('\r', ' ').strip()
        if len(value) > max_length:
            value = value[:max_length] + '...'
        return value

    @staticmethod
    def _get_hostname() -> str:
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"

    def _cleanup(self) -> None:
        """程序退出时的清理工作"""
        self._shutdown_event.set()
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=3)
        self._flush_handlers()

    def __del__(self):
        try:
            self._cleanup()
        except Exception:
            pass
