# -*- coding: utf-8 -*-
"""
模块名称: base_execution.py (v4.0 - 华尔街终极完善版)
核心职责: 定义交易所执行适配器的完整抽象接口，覆盖现货、永续、期货的所有交易、风控、资金、
         费率、合规、监控及事件回调。提供金融级枚举与数据类，杜绝字符串误用。
适用账户: 2000美金 ~ 万亿美金，特别强化小账户保护。
所属层级: adapters.execution

外部依赖:
    - abc (ABC, abstractmethod)
    - typing (List, Optional, Dict, Any, Callable, Union, Tuple)
    - decimal (Decimal)
    - dataclasses (dataclass)
    - enum (Enum, auto)
    - core.models.order (Order, ExecutionReport, Fill)
    - core.models.position (Position)

接口契约:
    提供: ExecutionAdapter 抽象基类，要求子类实现所有抽象方法。
    消费: 具体交易所适配器（如 binance_adapter.py, okx_adapter.py）

配置项:
    - execution.exchange.primary: 主交易所
    - execution.exchange.api_key: API 密钥
    - execution.exchange.secret: 密钥

作者: KHAOS Execution Team
修改记录:
    - 2026-01-13 第三轮审计，补全金融级接口
    - 2026-01-14 第四轮审计，增加枚举、衍生品、小账户保护、监控等100项增强

CHANGELOG:
    v4.0: 新增 OrderType, OrderSide, PositionSide, OrderStatus, OrderEventType,
          MarginMode, ContractType, KlineInterval, UserDataEvent, ExecutionErrorCode 等枚举；
          新增 ExchangeInfo 数据类；新增大量抽象方法涵盖衍生品、风控、合规、数据流等；
          所有异常增加 message_cn 属性，支持中文错误提示；
          增加小账户保护相关方法；完善文档示例。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from core.models.order import Order, ExecutionReport, Fill
from core.models.position import Position

# =============================================================================
# 枚举定义 (消除字符串误用)
# =============================================================================

class OrderType(Enum):
    """订单类型"""
    LIMIT = auto()
    MARKET = auto()
    STOP_LOSS = auto()
    STOP_LOSS_LIMIT = auto()
    TAKE_PROFIT = auto()
    TAKE_PROFIT_LIMIT = auto()
    LIMIT_MAKER = auto()
    TRAILING_STOP_MARKET = auto()

class OrderSide(Enum):
    """买卖方向"""
    BUY = auto()
    SELL = auto()

class PositionSide(Enum):
    """持仓方向"""
    LONG = auto()
    SHORT = auto()
    BOTH = auto()

class OrderStatus(Enum):
    """订单状态"""
    NEW = auto()
    PARTIALLY_FILLED = auto()
    FILLED = auto()
    CANCELED = auto()
    PENDING_CANCEL = auto()
    REJECTED = auto()
    EXPIRED = auto()

class OrderEventType(Enum):
    """订单事件类型"""
    FILLED = auto()
    PARTIALLY_FILLED = auto()
    CANCELED = auto()
    REJECTED = auto()
    EXPIRED = auto()

class MarginMode(Enum):
    """保证金模式"""
    ISOLATED = auto()
    CROSSED = auto()

class ContractType(Enum):
    """合约类型"""
    SPOT = auto()
    PERPETUAL = auto()
    FUTURES = auto()

class KlineInterval(Enum):
    """K 线周期"""
    SECOND_1 = "1s"
    MINUTE_1 = "1m"
    MINUTE_3 = "3m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    HOUR_1 = "1h"
    HOUR_4 = "4h"
    DAY_1 = "1d"

class UserDataEvent(Enum):
    """用户数据流事件类型"""
    ORDER_UPDATE = auto()
    ACCOUNT_UPDATE = auto()
    POSITION_UPDATE = auto()

class ExecutionErrorCode(Enum):
    """执行层标准错误码"""
    UNKNOWN = -1
    ORDER_REJECTED = -1001
    INSUFFICIENT_FUNDS = -2010
    INVALID_ORDER = -1013
    CONNECTION_ERROR = -3001
    RATE_LIMIT = -3002
    AUTHENTICATION_ERROR = -3003

# =============================================================================
# 数据结构
# =============================================================================

@dataclass
class ExchangeInfo:
    """交易所交易规则与限制"""
    symbol: str
    base_asset: str
    quote_asset: str
    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_notional: Decimal
    price_precision: int
    qty_precision: int
    contract_type: ContractType = ContractType.SPOT
    contract_multiplier: Decimal = Decimal('1')
    is_margin_trading_allowed: bool = True
    leverage_range: Dict[str, int] = field(default_factory=dict)

@dataclass
class OrderEvent:
    """订单状态变化事件"""
    event_type: OrderEventType
    order: Order
    timestamp: int           # Unix 毫秒
    message: Optional[str] = None

@dataclass
class AssetInfo:
    """资产详细信息"""
    asset: str
    network: str
    min_deposit: Decimal
    min_withdraw: Decimal
    withdraw_fee: Decimal
    deposit_enabled: bool
    withdraw_enabled: bool

@dataclass
class FundingRateInfo:
    """资金费率信息"""
    symbol: str
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal
    next_funding_time: int

@dataclass
class Kline:
    """K线数据"""
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int

# =============================================================================
# 异常体系 (增强中文支持)
# =============================================================================

class ExecutionError(Exception):
    """所有执行层异常的基类"""
    def __init__(self, message: str, code: int = -1, message_cn: str = ""):
        super().__init__(message)
        self.code = code
        self.message_cn = message_cn or message

class OrderRejectedError(ExecutionError):
    """订单被交易所拒绝"""
    def __init__(self, message: str, code: int = ExecutionErrorCode.ORDER_REJECTED.value, message_cn: str = ""):
        super().__init__(message, code, message_cn or "订单被拒绝")

class InsufficientFundsError(ExecutionError):
    """资金不足"""
    def __init__(self, message: str, code: int = ExecutionErrorCode.INSUFFICIENT_FUNDS.value, message_cn: str = ""):
        super().__init__(message, code, message_cn or "资金不足")

class InvalidOrderError(ExecutionError):
    """订单参数非法"""
    def __init__(self, message: str, code: int = ExecutionErrorCode.INVALID_ORDER.value, message_cn: str = ""):
        super().__init__(message, code, message_cn or "订单参数非法")

class ConnectionError(ExecutionError):
    """网络或连接异常"""
    def __init__(self, message: str, code: int = ExecutionErrorCode.CONNECTION_ERROR.value, message_cn: str = ""):
        super().__init__(message, code, message_cn or "连接异常")

class RateLimitError(ExecutionError):
    """触发频率限制"""
    def __init__(self, message: str, code: int = ExecutionErrorCode.RATE_LIMIT.value, message_cn: str = ""):
        super().__init__(message, code, message_cn or "请求过于频繁")

class AuthenticationError(ExecutionError):
    """API 密钥无效或权限不足"""
    def __init__(self, message: str, code: int = ExecutionErrorCode.AUTHENTICATION_ERROR.value, message_cn: str = ""):
        super().__init__(message, code, message_cn or "认证失败")

# =============================================================================
# 抽象适配器基类
# =============================================================================

class ExecutionAdapter(ABC):
    """
    华尔街机构级交易所执行适配器抽象接口 v4.0。

    覆盖现货、永续、期货的全交易生命周期，所有金融数值使用 Decimal，
    关键概念使用枚举，强化小账户保护与合规监控。

    使用示例：
        adapter = BinanceExecutionAdapter(api_key='...', secret='...', testnet=False)
        await adapter.initialize()
        info = await adapter.fetch_exchange_info('BTCUSDT')
        order = Order(...)
        report = await adapter.submit_order(order)
        await adapter.shutdown()
    """

    def __init__(self, api_key: str, secret: str, testnet: bool = False):
        self.api_key = api_key
        self.secret = secret
        self.testnet = testnet
        self._order_listeners: List[Callable[[OrderEvent], None]] = []
        self._error_callbacks: List[Callable[[ExecutionError], None]] = []

    # --------------------------------------------------------------------------
    # 交易所标识属性
    # --------------------------------------------------------------------------
    @property
    @abstractmethod
    def EXCHANGE_NAME(self) -> str:
        """交易所唯一标识名称，如 'binance', 'okx'"""
        ...

    @property
    @abstractmethod
    def EXCHANGE_VERSION(self) -> str:
        """当前使用的 API 版本号，语义版本格式"""
        ...

    @property
    @abstractmethod
    def max_batch_size(self) -> int:
        """单次批量请求允许的最大订单数"""
        ...

    @property
    @abstractmethod
    def connect_timeout(self) -> float:
        """连接超时（秒）"""
        ...

    @property
    @abstractmethod
    def read_timeout(self) -> float:
        """读取超时（秒）"""
        ...

    @property
    @abstractmethod
    def connection_pool_size(self) -> int:
        """HTTP 连接池大小"""
        ...

    @property
    @abstractmethod
    def supports_compression(self) -> bool:
        """是否支持请求/响应压缩"""
        ...

    @property
    @abstractmethod
    def signature_method(self) -> str:
        """签名方式，如 'HMAC-SHA256', 'RSA-SHA256'"""
        ...

    # --------------------------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------------------------
    @abstractmethod
    async def initialize(self) -> None:
        """建立连接，验证凭证，加载规则与费率。必须首先调用。"""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """优雅关闭连接，取消所有挂单，释放资源。"""
        ...

    # --------------------------------------------------------------------------
    # 凭证与安全
    # --------------------------------------------------------------------------
    @abstractmethod
    async def validate_credentials(self) -> bool:
        """验证 API 密钥有效性。"""
        ...

    @abstractmethod
    async def rotate_api_key(self, new_key: str, new_secret: str) -> None:
        """在线更新 API 密钥。"""
        ...

    @abstractmethod
    async def revoke_api_key(self) -> None:
        """撤销当前 API 密钥（紧急情况）。"""
        ...

    # --------------------------------------------------------------------------
    # 网络与重试
    # --------------------------------------------------------------------------
    @abstractmethod
    async def set_retry_policy(self, max_retries: int, backoff_factor: float) -> None:
        """设置重试策略"""
        ...

    @abstractmethod
    async def set_reconnect_policy(self, max_attempts: int, interval_sec: float) -> None:
        """设置 WebSocket 重连策略"""
        ...

    @abstractmethod
    async def set_proxy(self, proxy_url: str) -> None:
        """设置 HTTP/HTTPS 代理"""
        ...

    # --------------------------------------------------------------------------
    # 订单管理 (基础)
    # --------------------------------------------------------------------------
    @abstractmethod
    async def submit_order(self, order: Order) -> ExecutionReport:
        """提交单个订单，保证幂等。"""
        ...

    @abstractmethod
    async def submit_batch_orders(self, orders: List[Order],
                                  progress_callback: Optional[Callable[[int, int], None]] = None) -> List[ExecutionReport]:
        """批量提交订单。"""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """撤销指定订单。"""
        ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """撤销所有活跃订单。"""
        ...

    @abstractmethod
    async def amend_order(self, order_id: str, new_price: Decimal, new_qty: Decimal) -> ExecutionReport:
        """修改订单。"""
        ...

    @abstractmethod
    async def test_order(self, order: Order) -> ExecutionReport:
        """模拟订单（测试参数，不实际成交）。"""
        ...

    @abstractmethod
    async def cancel_and_replace(self, cancel_order_id: str, new_order: Order) -> ExecutionReport:
        """撤消旧订单并立即提交新订单（原子性）。"""
        ...

    # --------------------------------------------------------------------------
    # 订单管理 (高级)
    # --------------------------------------------------------------------------
    @abstractmethod
    async def submit_iceberg_order(self, order: Order, visible_qty: Decimal) -> ExecutionReport:
        """提交冰山订单。"""
        ...

    @abstractmethod
    async def submit_algo_order(self, order: Order, algo_type: str, params: Dict[str, Any]) -> ExecutionReport:
        """提交算法订单（TWAP/VWAP 等）。"""
        ...

    @abstractmethod
    async def submit_conditional_order(self, order: Order, condition: str, stop_price: Decimal) -> ExecutionReport:
        """提交条件订单（OCO/OTO 等）。"""
        ...

    # --------------------------------------------------------------------------
    # 订单查询
    # --------------------------------------------------------------------------
    @abstractmethod
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """查询活跃订单。"""
        ...

    @abstractmethod
    async def fetch_order_by_id(self, order_id: str) -> Optional[Order]:
        """按 ID 查询订单。"""
        ...

    @abstractmethod
    async def fetch_orders_by_ids(self, order_ids: List[str]) -> List[Order]:
        """批量查询订单。"""
        ...

    @abstractmethod
    async def fetch_order_history(self, symbol: str,
                                  start_time: Optional[int] = None,
                                  end_time: Optional[int] = None,
                                  limit: int = 100) -> List[Order]:
        """查询历史订单。"""
        ...

    @abstractmethod
    async def fetch_order_amend_history(self, order_id: str) -> List[Dict[str, Any]]:
        """查询订单修改历史。"""
        ...

    # --------------------------------------------------------------------------
    # 持仓与余额
    # --------------------------------------------------------------------------
    @abstractmethod
    async def fetch_positions(self) -> List[Position]:
        """获取当前所有持仓"""
        ...

    @abstractmethod
    async def fetch_position_history(self, symbol: str, days: int = 7) -> List[Dict[str, Any]]:
        """获取历史持仓快照"""
        ...

    @abstractmethod
    async def fetch_balance(self) -> Decimal:
        """获取可用余额（报价资产）"""
        ...

    @abstractmethod
    async def fetch_account_info(self) -> Dict[str, Decimal]:
        """获取完整账户信息"""
        ...

    # --------------------------------------------------------------------------
    # 交易规则与费用
    # --------------------------------------------------------------------------
    @abstractmethod
    async def fetch_exchange_info(self, symbol: str) -> ExchangeInfo:
        """查询交易对规则"""
        ...

    @abstractmethod
    async def fetch_fee_schedule(self) -> Dict[str, Decimal]:
        """查询手续费率"""
        ...

    @abstractmethod
    async def estimate_fee(self, order: Order) -> Decimal:
        """预估单笔订单手续费"""
        ...

    @abstractmethod
    async def get_min_order_notional(self, symbol: str) -> Decimal:
        """最小名义价值"""
        ...

    @abstractmethod
    async def get_qty_precision(self, symbol: str) -> int:
        """数量精度"""
        ...

    @abstractmethod
    async def get_price_precision(self, symbol: str) -> int:
        """价格精度"""
        ...

    @abstractmethod
    async def get_contract_multiplier(self, symbol: str) -> Decimal:
        """合约乘数（衍生品）"""
        ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Decimal:
        """当前资金费率"""
        ...

    @abstractmethod
    async def fetch_funding_rate_history(self, symbol: str, limit: int = 100) -> List[FundingRateInfo]:
        """资金费率历史"""
        ...

    @abstractmethod
    async def get_funding_interval(self, symbol: str) -> int:
        """资金费率结算间隔（小时）"""
        ...

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Decimal:
        """永续合约标记价格"""
        ...

    # --------------------------------------------------------------------------
    # 风控与账户设置
    # --------------------------------------------------------------------------
    @abstractmethod
    async def close_position(self, symbol: str, side: PositionSide) -> ExecutionReport:
        """市价全平指定方向持仓"""
        ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """设置杠杆倍数"""
        ...

    @abstractmethod
    async def get_max_leverage(self, symbol: str) -> int:
        """查询最大可用杠杆"""
        ...

    @abstractmethod
    async def set_margin_mode(self, symbol: str, mode: MarginMode) -> None:
        """设置保证金模式"""
        ...

    @abstractmethod
    async def estimate_liquidation_price(self, symbol: str) -> Decimal:
        """预估强平价格"""
        ...

    @abstractmethod
    async def set_stop_order_type(self, order_type: OrderType) -> None:
        """设置止损单执行类型（市价/限价）"""
        ...

    @abstractmethod
    async def set_self_trade_prevention(self, mode: str) -> None:
        """设置自成交防护模式"""
        ...

    # --------------------------------------------------------------------------
    # 行情与数据
    # --------------------------------------------------------------------------
    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """获取订单簿深度"""
        ...

    @abstractmethod
    async def get_last_price(self, symbol: str) -> Decimal:
        """获取最新成交价"""
        ...

    @abstractmethod
    async def fetch_klines(self, symbol: str, interval: KlineInterval,
                           start_time: Optional[int] = None,
                           limit: int = 500) -> List[Kline]:
        """获取 K 线数据"""
        ...

    @abstractmethod
    async def get_symbol_status(self, symbol: str) -> str:
        """获取交易对状态 (TRADING, HALT, etc.)"""
        ...

    @abstractmethod
    async def get_system_status(self) -> str:
        """获取交易所系统状态"""
        ...

    @abstractmethod
    async def fetch_announcements(self) -> List[Dict[str, str]]:
        """获取交易所公告"""
        ...

    # --------------------------------------------------------------------------
    # 用户数据流 (WebSocket)
    # --------------------------------------------------------------------------
    @abstractmethod
    async def subscribe_user_data_stream(self) -> str:
        """订阅用户数据流，返回 listenKey"""
        ...

    @abstractmethod
    async def unsubscribe_user_data_stream(self, listen_key: str) -> None:
        """取消用户数据流订阅"""
        ...

    @abstractmethod
    async def subscribe_order_book(self, symbol: str, depth: int = 10) -> None:
        """订阅订单簿实时推送"""
        ...

    @abstractmethod
    async def subscribe_market_data(self, symbol: str, channels: List[str]) -> None:
        """订阅行情数据"""
        ...

    @abstractmethod
    async def unsubscribe_all(self) -> None:
        """取消所有 WebSocket 订阅"""
        ...

    # --------------------------------------------------------------------------
    # 监控与健康
    # --------------------------------------------------------------------------
    @abstractmethod
    async def health_check(self) -> bool:
        """快速健康检查"""
        ...

    @abstractmethod
    async def get_connection_status(self) -> Dict[str, Any]:
        """详细连接状态"""
        ...

    @abstractmethod
    async def get_server_time(self) -> int:
        """交易所服务器时间戳（毫秒）"""
        ...

    @abstractmethod
    async def handle_rate_limit(self, retry_after_ms: int = 0) -> None:
        """处理频率限制"""
        ...

    @abstractmethod
    async def send_heartbeat(self) -> None:
        """发送心跳以保持连接"""
        ...

    @abstractmethod
    async def check_ws_health(self) -> bool:
        """检查 WebSocket 连接健康"""
        ...

    @abstractmethod
    async def get_api_usage(self) -> Dict[str, int]:
        """获取 API 权重使用情况"""
        ...

    # --------------------------------------------------------------------------
    # 小账户保护与合规
    # --------------------------------------------------------------------------
    @abstractmethod
    async def get_min_account_balance(self) -> Decimal:
        """获取交易所要求的最低账户余额"""
        ...

    @abstractmethod
    async def validate_price(self, symbol: str, price: Decimal) -> bool:
        """检查价格是否符合 tickSize"""
        ...

    @abstractmethod
    async def validate_quantity(self, symbol: str, quantity: Decimal) -> bool:
        """检查数量是否符合 stepSize"""
        ...

    @abstractmethod
    async def get_max_fillable_qty(self, symbol: str, side: OrderSide) -> Decimal:
        """基于当前盘口计算可立即成交的最大数量"""
        ...

    @abstractmethod
    async def estimate_slippage(self, symbol: str, side: OrderSide, quantity: Decimal) -> Decimal:
        """预估滑点"""
        ...

    # --------------------------------------------------------------------------
    # 衍生品特性
    # --------------------------------------------------------------------------
    @abstractmethod
    async def fetch_open_interest(self, symbol: str) -> Decimal:
        """获取未平仓合约数量"""
        ...

    @abstractmethod
    async def fetch_long_short_ratio(self, symbol: str) -> Decimal:
        """获取多空账户数比率"""
        ...

    @abstractmethod
    async def get_adl_status(self, symbol: str) -> Dict[str, Any]:
        """查询自动减仓排队状态"""
        ...

    @abstractmethod
    async def get_expiry_date(self, symbol: str) -> Optional[int]:
        """获取交割合约的到期时间戳"""
        ...

    @abstractmethod
    async def fetch_settlement_history(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        """交割记录"""
        ...

    @abstractmethod
    async def fetch_insurance_fund(self, symbol: str) -> Decimal:
        """获取保险基金余额"""
        ...

    # --------------------------------------------------------------------------
    # 资金管理
    # --------------------------------------------------------------------------
    @abstractmethod
    async def fetch_deposits(self, start_time: Optional[int] = None) -> List[Dict[str, Any]]:
        """充值记录"""
        ...

    @abstractmethod
    async def fetch_withdrawals(self, start_time: Optional[int] = None) -> List[Dict[str, Any]]:
        """提币记录"""
        ...

    @abstractmethod
    async def transfer_funds(self, asset: str, amount: Decimal, from_account: str, to_account: str) -> bool:
        """内部资金划转"""
        ...

    @abstractmethod
    async def get_asset_info(self, asset: str) -> AssetInfo:
        """获取资产网络和费用信息"""
        ...

    @abstractmethod
    async def fetch_withdrawal_whitelist(self) -> List[str]:
        """获取提币白名单地址"""
        ...

    # --------------------------------------------------------------------------
    # 事件回调
    # --------------------------------------------------------------------------
    def add_order_event_listener(self, callback: Callable[[OrderEvent], None]) -> None:
        """注册订单事件监听器"""
        self._order_listeners.append(callback)

    def remove_order_event_listener(self, callback: Callable[[OrderEvent], None]) -> None:
        """移除订单事件监听器"""
        if callback in self._order_listeners:
            self._order_listeners.remove(callback)

    def set_error_callback(self, callback: Callable[[ExecutionError], None]) -> None:
        """注册错误回调"""
        self._error_callbacks.append(callback)

    def _notify_order_event(self, event: OrderEvent) -> None:
        """分发订单事件"""
        for listener in self._order_listeners:
            try:
                listener(event)
            except Exception:
                pass

    def _notify_error(self, error: ExecutionError) -> None:
        """分发错误事件"""
        for callback in self._error_callbacks:
            try:
                callback(error)
            except Exception:
                pass
