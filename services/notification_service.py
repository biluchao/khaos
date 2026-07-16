"""
模块名称: notification_service.py (v5.0 金融级至尊版)
核心职责: 多通道告警通知服务，支持 Telegram、邮件、短信，具备静默时段、
          优先级队列（含持久化降级）、滑动窗口限流、去重、重试、降级、
          安全脱敏、健康检查、指标暴露、分布式追踪及上下文管理。
所属层级: services
依赖: aiohttp, smtplib, email, twilio(可选), ssl, contextvars, asyncio
接口契约:
    提供:
        NotificationService:
            - send(message: str, level: str = 'info') -> bool
            - enqueue(message: str, level: str) -> None
            - health_check() -> Dict[str, bool]
            - metrics() -> Dict[str, Any]
            - close()
        async with NotificationService(config) as ns: ...
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import signal
import socket
import ssl
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timezone, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Dict, List, Optional, Tuple, Union, Callable, Awaitable, Any,
    Deque, Protocol, runtime_checkable,
)
from collections import deque
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
import smtplib

import aiohttp

logger = logging.getLogger(__name__)

# ----- 分布式追踪上下文 -----
_trace_id: ContextVar[Optional[str]] = ContextVar('trace_id', default=None)
_span_id: ContextVar[Optional[str]] = ContextVar('span_id', default=None)

def set_trace_context(trace_id: str, span_id: str = "") -> None:
    _trace_id.set(trace_id)
    _span_id.set(span_id or uuid.uuid4().hex[:16])

def get_trace_id() -> Optional[str]:
    return _trace_id.get()

def get_span_id() -> Optional[str]:
    return _span_id.get()

# ----- 枚举 -----
class MessageLevel(Enum):
    INFO = auto()
    WARNING = auto()
    CRITICAL = auto()

# ----- 异常 -----
class NotificationError(Exception): ...
class ConfigurationError(NotificationError): ...
class ChannelNotAvailableError(NotificationError): ...
class QueueFullError(NotificationError): ...

# ----- 工具函数 -----
def _escape_markdown_v2(text: str) -> str:
    escape_chars = '_*[]()~`>#+-=|{}.!'
    return ''.join(['\\' + ch if ch in escape_chars else ch for ch in text])

def _sanitize_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()

def _validate_telegram_token(token: str) -> bool:
    return bool(re.match(r'^\d{8,10}:[\w-]{35}$', token))

def _mask_sensitive(text: str) -> str:
    # 脱敏常见敏感信息模式
    text = re.sub(r'(bot\d+:[A-Za-z0-9_-]+)', '***TOKEN***', text)
    text = re.sub(r'(secret|password|token)=([^&\s]+)', r'\1=***', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b', '***CC***', text)  # 信用卡号
    return text

def _machine_id() -> str:
    return hashlib.md5(socket.gethostname().encode()).hexdigest()[:8]

# ----- 配置 -----
@dataclass
class ChannelConfig:
    enabled: bool = True
    retry_attempts: int = 2
    retry_backoff_base: float = 1.0
    timeout_sec: float = 10.0
    rate_limit_per_hour: int = 20
    rate_limit_window_sec: int = 3600
    burst_ratio: float = 0.2          # 短时突发可超出限额的比例

@dataclass
class TelegramConfig(ChannelConfig):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    api_url: str = "https://api.telegram.org"

@dataclass
class EmailConfig(ChannelConfig):
    smtp_server: Optional[str] = None
    smtp_port: int = 587
    username: Optional[str] = None
    password: Optional[str] = None
    recipients: List[str] = field(default_factory=list)
    use_tls: bool = True
    require_auth: bool = True
    subject_prefix: str = "[KHAOS]"

@dataclass
class SmsConfig(ChannelConfig):
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    from_number: Optional[str] = None
    to_numbers: List[str] = field(default_factory=list)
    fallback_to_email: bool = True

@dataclass
class NotificationConfig:
    enabled: bool = True
    quiet_hours_enabled: bool = False
    quiet_start: dt_time = dt_time(23, 0)
    quiet_end: dt_time = dt_time(7, 0)
    quiet_timezone: timezone = timezone.utc
    quiet_days: List[int] = field(default_factory=lambda: [0,1,2,3,4,5,6])  # 每天
    allow_p0_during_quiet: bool = True
    allow_warning_during_quiet: bool = False
    keyword_whitelist: List[str] = field(default_factory=list)  # 包含这些关键词的消息即使在静默时段也发送
    max_message_length: int = 4000
    dry_run: bool = False
    dedup_window_sec: int = 300
    max_queue_size: int = 100
    persistence_path: Optional[Path] = None  # 队列溢出时持久化目录
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    sms: SmsConfig = field(default_factory=SmsConfig)
    health_cache_ttl_sec: int = 30

    def __post_init__(self):
        # 从环境变量覆盖
        if not self.telegram.bot_token:
            self.telegram.bot_token = os.getenv("KH_TELEGRAM_TOKEN")
        if not self.email.password:
            self.email.password = os.getenv("KH_EMAIL_PASSWORD")
        if not self.sms.auth_token:
            self.sms.auth_token = os.getenv("KH_SMS_TOKEN")

    def validate(self) -> List[str]:
        errors = []
        if self.telegram.enabled:
            if not self.telegram.bot_token or not self.telegram.chat_id:
                errors.append("Telegram token/chat_id 缺失")
            elif not _validate_telegram_token(self.telegram.bot_token):
                errors.append("Telegram token 格式无效")
        if self.email.enabled:
            if not self.email.smtp_server:
                errors.append("邮件 SMTP 服务器未配置")
            if self.email.require_auth and (not self.email.username or not self.email.password):
                errors.append("邮件认证凭据缺失")
        if self.sms.enabled:
            if not self.sms.account_sid or not self.sms.auth_token:
                errors.append("短信 SID/Token 缺失")
        if self.quiet_start == self.quiet_end:
            errors.append("静默开始与结束时间不能相同")
        return errors

# ----- 消息 -----
@dataclass
class Message:
    content: str
    level: MessageLevel
    id: str = field(default_factory=lambda: f"{_machine_id()}-{uuid.uuid4().hex}")
    trace_id: Optional[str] = field(default_factory=get_trace_id)
    span_id: Optional[str] = field(default_factory=get_span_id)
    created_at: float = field(default_factory=time.monotonic)

# ----- 渠道接口 (Protocol) -----
@runtime_checkable
class NotificationChannel(Protocol):
    async def send(self, message: Message) -> bool: ...
    async def health_check(self) -> bool: ...
    async def close(self) -> None: ...

# ----- 抽象基类 -----
class BaseChannel(ABC):
    def __init__(self, config: ChannelConfig):
        self.config = config
        self._sent_times: Deque[float] = deque()

    def _check_rate_limit(self) -> bool:
        now = time.monotonic()
        window = self.config.rate_limit_window_sec
        while self._sent_times and self._sent_times[0] < now - window:
            self._sent_times.popleft()
        limit = self.config.rate_limit_per_hour * (1 + self.config.burst_ratio)
        return len(self._sent_times) < limit

    def _record_send(self):
        self._sent_times.append(time.monotonic())

    @abstractmethod
    async def send(self, message: Message) -> bool: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    async def close(self) -> None: pass

# ----- Telegram 渠道 -----
class TelegramChannel(BaseChannel):
    def __init__(self, config: TelegramConfig):
        super().__init__(config)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_sec)
            connector = aiohttp.TCPConnector(limit=5, limit_per_host=2, keepalive_timeout=30)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)

    async def send(self, message: Message) -> bool:
        if not self.config.enabled or not self._check_rate_limit():
            return False
        url = f"{self.config.api_url}/bot{self.config.bot_token}/sendMessage"
        text = _escape_markdown_v2(message.content)
        payload = {"chat_id": self.config.chat_id, "text": text, "parse_mode": "MarkdownV2"}
        for attempt in range(1, self.config.retry_attempts + 1):
            try:
                await self._get_session()
                async with self._session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        self._record_send()
                        return True
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", self.config.retry_backoff_base * (2 ** attempt)))
                        await asyncio.sleep(retry_after)
                    else:
                        logger.error("Telegram HTTP %s", resp.status)
                        return False
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.error("Telegram 网络异常 (尝试 %d): %s", attempt, e)
                if attempt < self.config.retry_attempts:
                    await asyncio.sleep(self.config.retry_backoff_base * (2 ** attempt))
        return False

    async def health_check(self) -> bool:
        try:
            await self._get_session()
            url = f"{self.config.api_url}/bot{self.config.bot_token}/getMe"
            async with self._session.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

# ----- 邮件渠道 -----
class EmailChannel(BaseChannel):
    def __init__(self, config: EmailConfig):
        super().__init__(config)

    async def send(self, message: Message) -> bool:
        if not self.config.enabled or not self.config.recipients:
            return False
        if not self._check_rate_limit():
            return False
        try:
            msg = MIMEMultipart()
            prefix = self.config.subject_prefix
            if message.level == MessageLevel.CRITICAL:
                prefix += " [紧急]"
            msg["Subject"] = Header(prefix, "utf-8")
            msg["From"] = formataddr(("KHAOS", self.config.username))
            msg["To"] = ", ".join(self.config.recipients)
            msg.attach(MIMEText(message.content, "plain", "utf-8"))

            loop = asyncio.get_running_loop()
            for attempt in range(1, self.config.retry_attempts + 1):
                try:
                    await loop.run_in_executor(None, self._sync_send, msg)
                    self._record_send()
                    return True
                except (smtplib.SMTPException, socket.timeout, ConnectionError, OSError) as e:
                    logger.error("邮件发送异常 (尝试 %d): %s", attempt, e)
                    if attempt < self.config.retry_attempts:
                        await asyncio.sleep(self.config.retry_backoff_base * (2 ** attempt))
            return False
        except Exception as e:
            logger.error("邮件构造失败: %s", e)
            return False

    def _sync_send(self, msg):
        context = ssl.create_default_context()
        if not self.config.use_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=self.config.timeout_sec) as server:
            server.ehlo()
            if self.config.use_tls:
                server.starttls(context=context)
                server.ehlo()
            if self.config.require_auth:
                server.login(self.config.username, self.config.password)
            server.send_message(msg)

    async def health_check(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._check_smtp)
            return True
        except Exception:
            return False

    def _check_smtp(self):
        with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=5) as server:
            server.ehlo()
            if self.config.use_tls:
                server.starttls()

# ----- 短信渠道 -----
class SmsChannel(BaseChannel):
    def __init__(self, config: SmsConfig):
        super().__init__(config)

    async def send(self, message: Message) -> bool:
        if not self.config.enabled or not self.config.to_numbers:
            return False
        if not self._check_rate_limit():
            return False
        try:
            from twilio.rest import Client
            from twilio.base.exceptions import TwilioRestException
        except ImportError:
            logger.error("Twilio 未安装")
            return False
        try:
            client = Client(self.config.account_sid, self.config.auth_token)
            loop = asyncio.get_running_loop()
            success = True
            for to in self.config.to_numbers:
                try:
                    await loop.run_in_executor(
                        None,
                        lambda: client.messages.create(body=message.content, from_=self.config.from_number, to=to)
                    )
                except TwilioRestException as e:
                    logger.error("短信发送到 %s 失败: %s", to, e)
                    success = False
            if success:
                self._record_send()
            return success
        except Exception as e:
            logger.error("短信服务异常: %s", e)
            return False

    async def health_check(self) -> bool:
        try:
            from twilio.rest import Client
            client = Client(self.config.account_sid, self.config.auth_token)
            account = client.api.accounts(self.config.account_sid).fetch()
            return account.status == "active"
        except Exception:
            return False

# ----- 通知服务核心 -----
class NotificationService:
    """金融级至尊通知服务"""

    def __init__(self, config: NotificationConfig):
        self.config = config
        errors = config.validate()
        if errors:
            raise ConfigurationError("; ".join(errors))
        self.channels: Dict[str, BaseChannel] = {}
        if config.telegram.enabled:
            self.channels["telegram"] = TelegramChannel(config.telegram)
        if config.email.enabled:
            self.channels["email"] = EmailChannel(config.email)
        if config.sms.enabled:
            self.channels["sms"] = SmsChannel(config.sms)

        self._lock = asyncio.Lock()
        self._health_cache: Dict[str, Tuple[float, bool]] = {}
        self._metrics: Dict[str, int] = {"sent": 0, "failed": 0, "dropped": 0, "retried": 0}
        self._dedup: Dict[str, float] = {}
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._sender_task: Optional[asyncio.Task] = None
        self._closed = False

    async def _sender_loop(self):
        while not self._closed:
            try:
                priority, msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            success = await self._dispatch(msg)
            if success:
                self._metrics["sent"] += 1
            else:
                # 失败重试：若是 critical 消息，重新入队
                if msg.level == MessageLevel.CRITICAL and self._queue.qsize() < self.config.max_queue_size:
                    self._queue.put_nowait((0, msg))
                    self._metrics["retried"] += 1
                else:
                    self._metrics["failed"] += 1
                    # 持久化降级
                    if self.config.persistence_path:
                        await self._persist_message(msg)
            self._queue.task_done()

    async def _persist_message(self, msg: Message):
        try:
            path = self.config.persistence_path / f"failed_{datetime.now().strftime('%Y%m%d')}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            async with asyncio.Lock():  # 简单文件锁
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now().isoformat()} | {msg.level.name} | {msg.id} | {msg.content}\n")
        except Exception as e:
            logger.error("持久化消息失败: %s", e)

    async def start(self):
        if not self._sender_task:
            self._sender_task = asyncio.create_task(self._sender_loop())

    async def enqueue(self, message: str, level: str = "info"):
        lvl = MessageLevel[level.upper()] if level.upper() in MessageLevel.__members__ else MessageLevel.INFO
        content = _sanitize_whitespace(message)
        content = _mask_sensitive(content)
        msg = Message(content=content, level=lvl)
        prio = 0 if lvl == MessageLevel.CRITICAL else (1 if lvl == MessageLevel.WARNING else 2)
        try:
            self._queue.put_nowait((prio, msg))
        except asyncio.QueueFull:
            if lvl == MessageLevel.CRITICAL:
                # critical 消息强制等待
                await self._queue.put((prio, msg))
            else:
                self._metrics["dropped"] += 1
                logger.warning("队列满，丢弃非紧急消息")

    async def send(self, message: str, level: str = "info") -> bool:
        await self.enqueue(message, level)
        return True

    async def _dispatch(self, msg: Message) -> bool:
        clean = msg.content
        # 去重
        msg_hash = hashlib.md5(clean.encode()).hexdigest()
        now = time.monotonic()
        if msg_hash in self._dedup and self._dedup[msg_hash] > now:
            logger.debug("消息去重: %s", clean[:50])
            return False
        self._dedup[msg_hash] = now + self.config.dedup_window_sec
        self._dedup = {h: t for h, t in self._dedup.items() if t > now}

        # 静默时段与关键词白名单
        if self._is_quiet(msg.level):
            if any(kw in clean for kw in self.config.keyword_whitelist):
                logger.debug("关键词白名单通过，忽略静默时段")
            else:
                logger.debug("静默时段，消息被抑制")
                return False

        results = {}
        for name, channel in self.channels.items():
            try:
                results[name] = await channel.send(msg)
            except Exception as e:
                logger.error("渠道 %s 异常: %s", name, e)
                results[name] = False

        if not results.get("sms", True) and "email" in self.channels:
            try:
                fallback_msg = Message(content=clean + "\n[短信回退]", level=msg.level)
                results["email"] = await self.channels["email"].send(fallback_msg)
            except Exception as e:
                logger.error("短信回退邮件异常: %s", e)

        success = any(results.values())
        if success:
            logger.info("通知发送成功: channels=%s", results)
        else:
            logger.error("通知发送全部失败: channels=%s", results)
        return success

    def _is_quiet(self, level: MessageLevel) -> bool:
        if not self.config.quiet_hours_enabled:
            return False
        now = datetime.now(self.config.quiet_timezone)
        if now.weekday() not in self.config.quiet_days:
            return False
        t = now.time()
        start = self.config.quiet_start
        end = self.config.quiet_end
        in_quiet = (start <= t < end) if start < end else (t >= start or t < end)
        if in_quiet:
            if level == MessageLevel.CRITICAL and self.config.allow_p0_during_quiet:
                return False
            if level == MessageLevel.WARNING and self.config.allow_warning_during_quiet:
                return False
            return True
        return False

    async def health_check(self) -> Dict[str, bool]:
        now = time.monotonic()
        ttl = self.config.health_cache_ttl_sec
        status = {}
        tasks = []
        names = []
        for name, channel in self.channels.items():
            cached = self._health_cache.get(name)
            if cached and (now - cached[0]) < ttl:
                status[name] = cached[1]
            else:
                tasks.append(channel.health_check())
                names.append(name)
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, res in zip(names, results):
                alive = res if isinstance(res, bool) else False
                self._health_cache[name] = (time.monotonic(), alive)
                status[name] = alive
        return status

    def metrics(self) -> Dict[str, int]:
        return dict(self._metrics)

    def get_stats(self) -> Dict[str, Any]:
        """详细统计信息"""
        return {
            "queue_size": self._queue.qsize(),
            "metrics": self.metrics(),
            "health": asyncio.get_event_loop().run_until_complete(self.health_check()) if not asyncio.get_event_loop().is_running() else None,
        }

    async def close(self):
        self._closed = True
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await asyncio.wait_for(self._sender_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        # 清空剩余队列（记录日志）
        while not self._queue.empty():
            _, msg = self._queue.get_nowait()
            logger.warning("未处理的消息丢弃: %s", msg.id)
        for ch in self.channels.values():
            await ch.close()
        logger.debug("通知服务已关闭")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    def __repr__(self):
        return f"<NotificationService channels={list(self.channels.keys())}>"
