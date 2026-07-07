# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: interfaces.py
核心职责: 定义KHAOS系统所有抽象基类和协议，作为积木式架构的永恒契约。
所属层级: core

设计原则:
    - 所有 I/O 密集操作使用 async，计算密集使用同步。
    - 异步方法支持通过 asyncio.CancelledError 取消。
    - 上下文对象视为只读，禁止并发修改。
    - Float 字段不得为 NaN/Inf，调用方负责校验。

外部依赖:
    - abc, typing, dataclasses, enum, datetime, math, uuid, json
    - core.models (Kline, Order, Position, Signal, Tick, OrderBook, Portfolio)

配置项: 无

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2026-07-07 v30.0: 最终无懈可击版本，修复序列化、类型兼容、枚举缺失、完善文档。
__version__ = "30.0.0"
__all__ = [
    # Enums
    'MarketRegime', 'OrderAction', 'SignalPriority', 'OrderType', 'OrderSide',
    'ConnectionState', 'NotificationPriority', 'HealthStatus', 'ComponentLifecycle',
    'SuggestedAction', 'MetricType', 'DiagnosticSeverity', 'DiagnosticMode',
    'TemplateAction', 'ConfigValidationLevel', 'LogLevel',
    # Data Classes
    'SRLevel', 'OrderConfirmation', 'OrderRequest', 'DataHealth', 'DiagnosticFinding',
    'DiagnosticReport', 'AuditEntry', 'HealthCheckResult', 'RiskCheckRecord',
    'WaveSimilarityResult', 'PatternMetadata', 'EvolutionLogEntry', 'SendRequest',
    # TypedDicts
    'FeatureContext', 'RiskRuleMetadata', 'EvolutionData',
    # Exceptions
    'KHAOSException', 'DataSourceException', 'StateRecoveryException',
    'CheckpointVersionMismatchException', 'DecisionTimeoutException',
    'OrderExecutionException', 'RiskViolationException', 'ConfigurationException',
    'CloneException', 'StartupException',
    # Interfaces
    'MarketDataProvider', 'FeatureComputer', 'SupportResistanceComputer',
    'DecisionMaker', 'SignalConflictResolver', 'ExecutionAdapter',
    'RiskRule', 'EvolutionTask', 'WaveSimilarityEngine', 'StatefulComponent',
    'NotificationService', 'ConfigProvider', 'ServiceLifecycle',
    'Auditable', 'Diagnosable', 'MetricsProvider', 'Cloneable', 'Resettable',
    'Closable', 'EventBus', 'KHAOSSystem',
]

import math
import uuid
import json as json_module
from abc import ABC, abstractmethod
from typing import (
    List, Dict, Any, Optional, Tuple, AsyncIterator, 
    Callable, Union, TypedDict, TypeVar
)
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime
import asyncio

# 类型别名
ConfigChangeCallback = Callable[[str, Any, Any], None]

# =============================================================================
# 枚举定义
# =============================================================================

class MarketRegime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"

class OrderAction(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    REDUCE = "REDUCE"
    ADD = "ADD"
    NO_ACTION = "NO_ACTION"

class SignalPriority(int, Enum):
    PANIC_CLOSE = 0
    HARD_STOP = 1
    ESCAPE_CLOSE = 2
    ESCAPE_REDUCE = 3
    RECAPTURE_ENTRY = 4
    CALLBACK_DROP = 5
    NORMAL_ENTRY = 6
    NORMAL_ADD = 7

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    OCO = "OCO"
    TWAP = "TWAP"
    ICEBERG = "ICEBERG"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class ConnectionState(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"

class NotificationPriority(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"

class ComponentLifecycle(str, Enum):
    INIT = "INIT"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    RESTARTING = "RESTARTING"
    FAILED = "FAILED"

class SuggestedAction(str, Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"
    REDUCE = "REDUCE"
    ADD = "ADD"
    EXIT = "EXIT"
    NO_ACTION = "NO_ACTION"

class MetricType(str, Enum):
    COUNTER = "COUNTER"
    GAUGE = "GAUGE"
    HISTOGRAM = "HISTOGRAM"
    SUMMARY = "SUMMARY"

class DiagnosticSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class DiagnosticMode(str, Enum):
    FULL = "full"
    LIGHT = "light"
    QUICK = "quick"

class TemplateAction(str, Enum):
    ADD = "ADD"
    REMOVE = "REMOVE"
    UPDATE = "UPDATE"
    GET = "GET"

class ConfigValidationLevel(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    ALL = "all"

# =============================================================================
# 数据类定义 (所有数据类包含 to_dict 和 __repr__)
# =============================================================================

@dataclass
class SRLevel:
    """支撑/阻力水平。confidence 范围 0.0-1.0。"""
    price: float
    strength: float
    method: str
    touches: int
    confidence: float = 1.0
    timestamp: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return f"SRLevel(price={self.price}, strength={self.strength}, method='{self.method}')"


@dataclass
class OrderConfirmation:
    """订单确认。commission 为负表示返佣。"""
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: str
    price: float
    avg_fill_price: Optional[float] = None
    filled_qty: float = 0.0
    total_qty: float = 0.0
    commission: float = 0.0
    commission_asset: str = ""
    created_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())
    request_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'order_id': self.order_id,
            'client_order_id': self.client_order_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'status': self.status,
            'price': self.price,
            'avg_fill_price': self.avg_fill_price,
            'filled_qty': self.filled_qty,
            'total_qty': self.total_qty,
            'commission': self.commission,
            'commission_asset': self.commission_asset,
            'created_at': self.created_at,
            'request_hash': self.request_hash,
        }

    def __repr__(self) -> str:
        return f"OrderConfirmation(id={self.order_id}, side={self.side.value}, filled={self.filled_qty})"


@dataclass
class OrderRequest:
    """提交订单所需的最小参数集。"""
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    client_order_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'quantity': self.quantity,
            'price': self.price,
            'stop_price': self.stop_price,
            'client_order_id': self.client_order_id,
        }

    def __repr__(self) -> str:
        return f"OrderRequest({self.side.value} {self.quantity} {self.symbol})"


@dataclass
class DataHealth:
    """数据源健康状态。不可用时使用 DataHealth.unavailable()。"""
    connection_state: ConnectionState
    latency_ms: float
    error_rate: float
    data_freshness_sec: float
    last_heartbeat: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    @classmethod
    def unavailable(cls) -> 'DataHealth':
        return cls(connection_state=ConnectionState.DISCONNECTED, latency_ms=math.inf, error_rate=1.0, data_freshness_sec=math.inf)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'connection_state': self.connection_state.value,
            'latency_ms': self.latency_ms if not math.isinf(self.latency_ms) else "inf",
            'error_rate': self.error_rate,
            'data_freshness_sec': self.data_freshness_sec if not math.isinf(self.data_freshness_sec) else "inf",
            'last_heartbeat': self.last_heartbeat,
        }

    def __repr__(self) -> str:
        return f"DataHealth(state={self.connection_state.value}, latency={self.latency_ms}ms)"


@dataclass
class DiagnosticFinding:
    """诊断发现。severity 为严重性。"""
    severity: DiagnosticSeverity
    message: str
    component: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'severity': self.severity.value,
            'message': self.message,
            'component': self.component,
            'details': self.details,
        }

    def __repr__(self) -> str:
        return f"DiagnosticFinding({self.severity.value}: {self.message})"


@dataclass
class DiagnosticReport:
    """诊断报告。metrics 为数值型指标。"""
    component: str
    status: HealthStatus
    findings: List[DiagnosticFinding] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=lambda: datetime.utcnow().timestamp())
    redacted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'component': self.component,
            'status': self.status.value,
            'findings': [f.to_dict() for f in self.findings],
            'metrics': self.metrics,
            'errors': self.errors,
            'warnings': self.warnings,
            'timestamp': self.timestamp,
            'redacted': self.redacted,
        }

    def to_json(self) -> str:
        return json_module.dumps(self.to_dict(), indent=2)

    def __repr__(self) -> str:
        return f"DiagnosticReport({self.component}: {self.status.value})"


@dataclass
class AuditEntry:
    """审计条目。entry_id 自动生成，sequence_number 由实现填充。"""
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sequence_number: int = 0
    timestamp: float = field(default_factory=lambda: datetime.utcnow().timestamp())
    actor: str = ""
    action: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    signature: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return f"AuditEntry({self.sequence_number}: {self.actor} {self.action})"


@dataclass
class HealthCheckResult:
    """健康检查结果。details 应避免包含敏感信息。"""
    status: HealthStatus
    details: Dict[str, Any] = field(default_factory=dict)
    checked_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status.value,
            'details': self.details,
            'checked_at': self.checked_at,
        }

    def __repr__(self) -> str:
        return f"HealthCheckResult({self.status.value})"


@dataclass
class RiskCheckRecord:
    """风控检查历史记录。reason 避免包含账户具体数值。"""
    passed: bool
    reason: str
    timestamp: float = field(default_factory=lambda: datetime.utcnow().timestamp())
    rule_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return f"RiskCheckRecord({self.rule_name}: {'PASS' if self.passed else 'FAIL'})"


@dataclass
class WaveSimilarityResult:
    """波浪相似度结果。similarity_score 范围 0.0-1.0。confidence_interval 为 95% 置信区间。"""
    similarity_score: float
    confidence_interval: Tuple[float, float]
    top_matches: List[Dict[str, Any]] = field(default_factory=list)
    suggested_action: SuggestedAction = SuggestedAction.NO_ACTION

    def to_dict(self) -> Dict[str, Any]:
        return {
            'similarity_score': self.similarity_score,
            'confidence_interval': list(self.confidence_interval),
            'top_matches': self.top_matches,
            'suggested_action': self.suggested_action.value,
        }

    def __repr__(self) -> str:
        return f"WaveSimilarityResult(score={self.similarity_score:.2f}, action={self.suggested_action.value})"


@dataclass
class EvolutionLogEntry:
    """进化任务日志条目。"""
    timestamp: float
    level: LogLevel
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp,
            'level': self.level.value,
            'message': self.message,
        }

    def __repr__(self) -> str:
        return f"EvolutionLogEntry({self.level.value}: {self.message})"


@dataclass
class SendRequest:
    """批量发送通知的请求。"""
    message: str
    level: NotificationPriority = NotificationPriority.NORMAL
    channel: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'message': self.message,
            'level': self.level.value,
            'channel': self.channel,
        }

    def __repr__(self) -> str:
        return f"SendRequest({self.level.value})"


# =============================================================================
# TypedDict 定义
# =============================================================================

class PatternMetadata(TypedDict, total=False):
    timestamp: float
    market_regime: str
    profitability: float
    description: str


class FeatureContext(TypedDict, total=False):
    """特征计算上下文。字段可选；所有 float 不得为 NaN/Inf。sr_levels 的键为周期标识如 '5m','15m'。"""
    kma: float
    kma_slope: float
    atr_3m: float
    atr_5m: float
    atr_15m: float
    hmm_state: str
    hmm_probabilities: Optional[Dict[str, float]]
    sr_levels: Dict[str, List[SRLevel]]
    resonance: float
    last_price: float
    volume_ma20: Optional[float]
    volatility_percentile: float
    regime: Optional[MarketRegime]


class RiskRuleMetadata(TypedDict, total=False):
    name: str
    priority: int
    rule_type: str
    description: str
    enabled: bool


class EvolutionData(TypedDict, total=False):
    """进化任务数据。klines 为原始K线列表，features 为预处理特征，labels 为监督标签。"""
    klines: List[Any]
    features: List[Dict[str, Any]]
    labels: List[float]
    metadata: Dict[str, Any]


# =============================================================================
# 自定义异常类
# =============================================================================

class KHAOSException(Exception):
    pass

class DataSourceException(KHAOSException):
    pass

class StateRecoveryException(KHAOSException):
    pass

class CheckpointVersionMismatchException(StateRecoveryException):
    pass

class DecisionTimeoutException(KHAOSException):
    pass

class OrderExecutionException(KHAOSException):
    pass

class RiskViolationException(KHAOSException):
    pass

class ConfigurationException(KHAOSException):
    pass

class CloneException(KHAOSException):
    pass

class StartupException(KHAOSException):
    pass


# =============================================================================
# 核心接口定义
# =============================================================================

class MarketDataProvider(ABC):
    """行情数据提供者。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def subscribe_klines(self, symbol: str, interval: str, timeout_sec: float = 30.0, on_reconnect: Optional[Callable[[], None]] = None) -> None:
        """订阅K线。raises: DataSourceException"""
        ...

    @abstractmethod
    async def unsubscribe_klines(self, symbol: str, interval: str) -> None: ...

    @abstractmethod
    async def get_recent_klines(self, symbol: str, interval: str, limit: int, timeout_sec: float = 10.0) -> List['Kline']: ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 10, timeout_sec: float = 5.0) -> 'OrderBook': ...

    @abstractmethod
    async def stream_ticks(self, symbol: str, timeout_sec: float = 60.0, max_queue_size: int = 1000) -> AsyncIterator['Tick']: ...

    @abstractmethod
    async def stop_stream(self, symbol: str) -> None: ...

    @abstractmethod
    async def get_health_status(self) -> DataHealth: ...

    @abstractmethod
    async def get_exchange_info(self, symbol: Optional[str] = None) -> Dict[str, Any]: ...

    @abstractmethod
    async def verify_data_integrity(self, symbol: str, interval: str) -> bool: ...

    @abstractmethod
    async def get_historical_trades(self, symbol: str, start_time: datetime, end_time: datetime, limit: int = 1000) -> List['Tick']: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        """子类必须重写以声明支持的版本。"""
        ...


class FeatureComputer(ABC):
    """特征计算器。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def compute(self, kline: 'Kline', context: FeatureContext) -> Optional[Dict[str, Any]]:
        """返回特征字典或 None（仅表示数据不足）。异常表示错误。"""
        ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def get_feature_metadata(self) -> Dict[str, Any]: ...

    @abstractmethod
    def get_required_context_keys(self) -> List[str]: ...

    @abstractmethod
    def supports_state(self) -> bool:
        """是否支持状态持久化。"""
        ...

    def save_checkpoint(self) -> Dict[str, Any]:
        """若支持状态，需实现此方法。"""
        raise NotImplementedError

    def load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        """原子性恢复。raises: StateRecoveryException"""
        raise NotImplementedError

    def get_checkpoint_version(self) -> int:
        return 1

    def reset_to_default(self) -> None:
        pass

    def try_repair(self) -> bool:
        return False

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class SupportResistanceComputer(ABC):
    """支撑/阻力计算器。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def compute(self, klines: List['Kline'], context: Optional[FeatureContext] = None) -> Tuple[List[SRLevel], List[SRLevel]]: ...

    @abstractmethod
    def get_method_name(self) -> str: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class DecisionMaker(ABC):
    """策略决策器。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def decide(self, symbol: str, features: Dict[str, Any], portfolio: Optional['Portfolio'], context: FeatureContext, max_decision_time_ms: int = 50) -> List['Signal']:
        """返回已排序的信号列表。空列表表示无动作。超时抛出 DecisionTimeoutException。"""
        ...

    @abstractmethod
    async def get_decision_weights(self) -> Dict[str, float]: ...

    @abstractmethod
    async def get_strategy_status(self, level: str = "summary") -> Dict[str, Any]: ...

    @abstractmethod
    async def validate_decision(self, signal: 'Signal', result: Dict[str, Any]) -> bool: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class SignalConflictResolver(ABC):
    """信号冲突消解器。__version__ = "1.3" """
    __version__ = "1.3"
    SUPPORTED_VERSIONS = ["1.0", "1.1", "1.2", "1.3"]

    @abstractmethod
    async def resolve(self, signals: List['Signal'], portfolio: Optional['Portfolio']) -> List['Signal']: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class ExecutionAdapter(ABC):
    """订单执行适配器。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def submit_order(self, order: OrderRequest, client_order_id: Optional[str] = None, on_fill: Optional[Callable[[OrderConfirmation], None]] = None, on_progress: Optional[Callable[[float], None]] = None, timeout_sec: float = 10.0) -> OrderConfirmation:
        """提交订单。on_progress 回调 0.0-1.0。"""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: Optional[str] = None, timeout_sec: float = 30.0) -> int:
        """网络问题应返回 0 并记录，致命错误抛出异常。"""
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str, symbol: str) -> OrderConfirmation: ...

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderConfirmation]: ...

    @abstractmethod
    async def sync_positions(self, symbol: Optional[str] = None) -> List['Position']: ...

    @abstractmethod
    async def get_trading_fees(self) -> Dict[str, float]: ...

    @abstractmethod
    async def handle_exchange_error(self, error: Exception) -> None: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class RiskRule(ABC):
    """风控规则。check 为纯函数，无副作用。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    def check(self, order: Optional['OrderRequest'], portfolio: Optional['Portfolio'], context: FeatureContext, account_history: Optional[List[Dict[str, Any]]] = None, history: Optional[List[RiskCheckRecord]] = None, market_snapshot: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """返回 (通过, 拒绝原因)。history 不超过100条。"""
        ...

    @abstractmethod
    def get_rule_name(self) -> str: ...

    @abstractmethod
    def get_metadata(self) -> RiskRuleMetadata: ...

    @abstractmethod
    def is_enabled(self) -> bool: ...

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None: ...

    def get_dependencies(self) -> List[str]:
        return []

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class EvolutionTask(ABC):
    """进化任务。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def run(self, config: Dict[str, Any], data: Union[List['Kline'], List[Dict[str, Any]]], resume_from_checkpoint: Optional[str] = None, on_progress: Optional[Callable[[float], None]] = None, stop_event: Optional[asyncio.Event] = None) -> Dict[str, Any]:
        """返回包含 'params' 和 'log' (List[EvolutionLogEntry]) 的字典。"""
        ...

    @abstractmethod
    async def validate(self, result: Dict[str, Any], validation_data: List[Dict[str, Any]]) -> Dict[str, Any]: ...

    @abstractmethod
    def get_progress(self) -> float:
        """任务开始前返回 0.0。取消后保持最后进度。"""
        ...

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def resume(self) -> None: ...

    @abstractmethod
    async def cancel(self) -> bool: ...

    @abstractmethod
    def get_task_info(self) -> Dict[str, Any]:
        """返回包含 'status' 的字典。取消后 status 为 CANCELLED。"""
        ...

    @abstractmethod
    def get_last_error(self) -> Optional[str]: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class WaveSimilarityEngine(ABC):
    """波浪相似度引擎。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def add_positive_pattern(self, klines: List['Kline'], metadata: PatternMetadata) -> bool:
        """相似度>0.95视为重复，不添加返回 False。"""
        ...

    @abstractmethod
    async def evaluate_similarity(self, recent_klines: List['Kline'], context: FeatureContext, max_compute_time_ms: int = 5) -> WaveSimilarityResult: ...

    @abstractmethod
    def get_cache_size(self) -> int: ...

    @abstractmethod
    def clear_cache(self) -> int: ...

    @abstractmethod
    async def export_cache(self) -> bytes: ...

    @abstractmethod
    async def import_cache(self, data: bytes) -> int: ...

    @abstractmethod
    async def set_weights(self, weights: Dict[str, float]) -> None: ...

    @abstractmethod
    def get_weight_keys(self) -> List[str]: ...

    @abstractmethod
    async def set_threshold(self, threshold: float) -> None: ...

    @abstractmethod
    def get_threshold(self) -> float: ...

    @abstractmethod
    def get_max_cache_size(self) -> int: ...

    @abstractmethod
    async def set_max_cache_size(self, size: int) -> None: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class StatefulComponent(ABC):
    """有状态组件。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    def save_checkpoint(self, compress: bool = False, encrypt: bool = False) -> bool:
        """成功返回 True。检查点应包含 checksum。"""
        ...

    @abstractmethod
    def load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        """原子性恢复。raises: StateRecoveryException"""
        ...

    @abstractmethod
    def get_checkpoint_version(self) -> int: ...

    @abstractmethod
    def reset_to_default(self) -> None:
        """重置后检查点版本应置为 1。"""
        ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class NotificationService(ABC):
    """通知服务。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def send(self, message: str, level: NotificationPriority = NotificationPriority.NORMAL, channel: Optional[str] = None, retry_on_failure: bool = True, max_retries: int = 3) -> bool: ...

    @abstractmethod
    async def send_batch(self, messages: List[SendRequest]) -> List[bool]: ...

    @abstractmethod
    async def send_template(self, template_name: str, params: Dict[str, Any]) -> bool: ...

    @abstractmethod
    async def is_channel_available(self, channel: str, force_check: bool = False) -> bool: ...

    @abstractmethod
    async def manage_template(self, action: TemplateAction, name: str, content: Optional[str] = None) -> bool: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class ConfigProvider(ABC):
    """配置提供者。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置，不存在且无默认值时抛出 ConfigurationException。"""
        ...

    @abstractmethod
    def get_all(self, mask_sensitive: bool = False, prefix: Optional[str] = None) -> Dict[str, Any]: ...

    @abstractmethod
    async def reload(self) -> None:
        """失败抛出 ConfigurationException。"""
        ...

    @abstractmethod
    def validate(self, level: ConfigValidationLevel = ConfigValidationLevel.ERROR) -> Tuple[bool, List[str], List[str]]: ...

    @abstractmethod
    def subscribe(self, callback: ConfigChangeCallback, key_filter: Optional[str] = None) -> str: ...

    @abstractmethod
    def unsubscribe(self, token: str) -> None: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class ServiceLifecycle(ABC):
    """服务生命周期。状态转换：
       INIT -> STARTING -> RUNNING / PAUSED / RESTARTING -> STOPPING -> STOPPED.
       RUNNING 可暂停到 PAUSED，恢复回 RUNNING。FAILED 可通过 recover() 恢复到 INIT。
    """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def start(self, start_timeout_sec: float = 30.0) -> None:
        """启动服务。抛出 StartupException。若状态为 FAILED 应先调用 recover()。"""
        ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def resume(self) -> None: ...

    @abstractmethod
    def get_lifecycle_state(self) -> ComponentLifecycle: ...

    @abstractmethod
    async def health_check(self) -> HealthCheckResult: ...

    @abstractmethod
    async def recover(self) -> bool: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class Auditable(ABC):
    """可审计组件。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    def get_audit_trail(self, since: Optional[datetime] = None, until: Optional[datetime] = None, limit: Optional[int] = None, offset: int = 0) -> List[AuditEntry]:
        """先 offset 再 limit。"""
        ...

    @abstractmethod
    def get_recent_entries(self, n: int = 100) -> List[AuditEntry]: ...

    @abstractmethod
    def mask_sensitive_data(self, entries: List[AuditEntry]) -> List[AuditEntry]:
        """返回脱敏的新列表，不修改原条目。"""
        ...

    @abstractmethod
    async def export_audit_trail(self, format: str = "csv") -> bytes: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class Diagnosable(ABC):
    """可诊断组件。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    async def run_diagnostics(self, mode: DiagnosticMode = DiagnosticMode.FULL, timeout_sec: float = 30.0, redact_sensitive: bool = True) -> DiagnosticReport: ...

    @abstractmethod
    def last_diagnosis_time(self) -> Optional[datetime]: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class MetricsProvider(ABC):
    """指标提供者。__version__ = "2.3" """
    __version__ = "2.3"
    SUPPORTED_VERSIONS = ["2.0", "2.1", "2.2", "2.3"]

    @abstractmethod
    def get_metrics(self, namespace: str = "default") -> Dict[str, float]: ...

    @abstractmethod
    def get_metrics_with_labels(self, namespace: str = "default") -> Dict[str, Dict[str, float]]: ...

    @abstractmethod
    def get_metric_names(self) -> List[str]: ...

    @abstractmethod
    def get_metric_descriptions(self) -> Dict[str, str]: ...

    @abstractmethod
    def reset_metrics(self) -> None: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class Cloneable(ABC):
    """可克隆组件。clone 必须返回完全独立的深拷贝，处理循环引用。__version__ = "1.2" """
    __version__ = "1.2"
    SUPPORTED_VERSIONS = ["1.0", "1.1", "1.2"]

    @abstractmethod
    def clone(self) -> 'Cloneable':
        """深拷贝，可能抛出 CloneException。"""
        ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class Resettable(ABC):
    """可热重置组件。__version__ = "1.2" """
    __version__ = "1.2"
    SUPPORTED_VERSIONS = ["1.0", "1.1", "1.2"]

    @abstractmethod
    def reset(self, on_reset: Optional[Callable[[], None]] = None) -> None:
        """重置组件。on_reset 回调为同步调用。"""
        ...

    @abstractmethod
    def is_reset(self) -> bool:
        """重置完成后返回 True。"""
        ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class Closable(ABC):
    """可关闭组件。close 应幂等。__version__ = "1.1" """
    __version__ = "1.1"
    SUPPORTED_VERSIONS = ["1.0", "1.1"]

    @abstractmethod
    async def close(self) -> None: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class EventBus(ABC):
    """事件总线。payload 应为 JSON 可序列化对象。__version__ = "1.1" """
    __version__ = "1.1"
    SUPPORTED_VERSIONS = ["1.0", "1.1"]

    @abstractmethod
    async def publish(self, event_type: str, payload: Any) -> None: ...

    @abstractmethod
    async def subscribe(self, event_type: str, callback: Callable[[Any], None]) -> str: ...

    @abstractmethod
    async def unsubscribe(self, token: str) -> None: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...


class KHAOSSystem(ABC):
    """系统顶层接口。__version__ = "1.2" """
    __version__ = "1.2"
    SUPPORTED_VERSIONS = ["1.0", "1.1", "1.2"]

    @abstractmethod
    def get_market_data_provider(self) -> MarketDataProvider: ...

    @abstractmethod
    def get_decision_maker(self) -> DecisionMaker: ...

    @abstractmethod
    def get_execution_adapter(self) -> ExecutionAdapter: ...

    @abstractmethod
    def get_risk_engine(self) -> List[RiskRule]: ...

    @abstractmethod
    def get_notification_service(self) -> NotificationService: ...

    @abstractmethod
    def get_config_provider(self) -> ConfigProvider: ...

    @abstractmethod
    def get_event_bus(self) -> EventBus: ...

    @abstractmethod
    async def start_all(self) -> None: ...

    @abstractmethod
    async def stop_all(self) -> None: ...

    @classmethod
    @abstractmethod
    def is_compatible(cls, version: str) -> bool:
        ...
