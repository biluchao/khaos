# -*- coding: utf-8 -*-
"""
模块名称: signal_schema.py (v4.0)
核心职责: 交易信号 Pydantic 模型，使用 Pydantic v2 特性，集成华尔街级安全与验证。
"""
import re
import math
import copy
import html
from typing import Optional, List, Dict, Any, Literal, Annotated
from datetime import datetime, timezone, timedelta
from pydantic import (
    BaseModel, Field, field_validator, model_validator, field_serializer, ConfigDict
)
from pydantic.functional_validators import AfterValidator
from pydantic.functional_serializers import PlainSerializer

# ==================== 常量与正则 ====================
SYMBOL_REGEX = re.compile(r'^[A-Z0-9]{2,10}$')
UUID_REGEX = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
ORDER_ID_REGEX = re.compile(r'^[a-zA-Z0-9_-]+$')
REQUEST_ID_REGEX = re.compile(r'^[a-zA-Z0-9_-]+$')
METADATA_KEY_REGEX = re.compile(r'^[a-zA-Z0-9_]+$')
CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')

# 允许值
VALID_DIRECTIONS = Literal['LONG', 'SHORT', 'CLOSE']
VALID_ACTIONS = Literal['OPEN', 'CLOSE', 'REDUCE', 'REJECT']
VALID_STATUSES = Literal['EXECUTED', 'REJECTED', 'MERGED', 'PENDING']

# 模块白名单（实际应从配置注入）
ALLOWED_MODULES = frozenset({
    'KMA', 'HMM', 'TrendProbabilityFilter', 'EscapeDetector',
    'Recapture', 'CallbackDrop', 'PullbackAdd', 'GuerrillaChase'
})

# ==================== 通用函数 ====================
def check_finite_and_positive(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError('数值必须为有限值')
    if v <= 0:
        raise ValueError('必须大于0')
    return v

def check_finite_float(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError('数值必须为有限值')
    return v

def check_probability(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError('概率必须是有限值')
    return max(0.0, min(1.0, v))

def validate_metadata_structure(metadata: Dict[str, Any], max_depth: int = 3, _current_depth: int = 0) -> Dict[str, Any]:
    if _current_depth > max_depth:
        raise ValueError(f'metadata 嵌套深度不能超过 {max_depth}')
    if len(metadata) > 20:
        raise ValueError('metadata 键数量不能超过20')
    for key, val in metadata.items():
        if not METADATA_KEY_REGEX.match(key):
            raise ValueError(f'metadata 键 "{key}" 包含非法字符')
        if key.startswith('$'):
            raise ValueError('metadata 键不能以 $ 开头')
        if isinstance(val, dict):
            validate_metadata_structure(val, max_depth, _current_depth + 1)
        elif isinstance(val, (int, float, bool, str)):
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError('metadata 值包含无穷或NaN')
        else:
            raise ValueError('metadata 值必须为基本类型')
    return copy.deepcopy(metadata)  # 深拷贝防止外部修改

def sanitize_text(v: str) -> str:
    if v:
        v = v.strip()
        if CONTROL_CHAR_RE.search(v):
            raise ValueError('字符串包含非法控制字符')
    return v

def utc_datetime(v: datetime) -> datetime:
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    else:
        v = v.astimezone(timezone.utc)
    return v

def normalize_half_width(s: str) -> str:
    """全角字母数字转半角"""
    if not s:
        return s
    result = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(ch)
    return ''.join(result)

# ==================== 序列化工具 ====================
def serialize_datetime(value: datetime) -> str:
    """统一将 datetime 序列化为 ISO8601 UTC 字符串"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()

# ==================== 数据模型 ====================

class SignalRequest(BaseModel):
    symbol: Annotated[str, Field(min_length=2, max_length=10, description="交易对")]
    direction: Literal['LONG', 'SHORT', 'CLOSE'] = Field(..., description="方向")
    price: Optional[Annotated[float, Field(gt=0, le=1e12)]] = Field(None, description="价格（限价单必填）")
    size: Optional[Annotated[float, Field(gt=0, le=1e6)]] = Field(None, description="数量")
    module: Annotated[str, Field(max_length=50, description="来源模块")]
    metadata: Dict[str, Any] = Field(default_factory=dict, description="附加信息")
    request_id: Optional[Annotated[str, Field(max_length=64)]] = Field(None, description="幂等请求ID")
    client_timestamp: Optional[datetime] = Field(None, description="客户端时间戳")

    @field_validator('symbol', mode='before')
    @classmethod
    def pre_symbol(cls, v):
        if isinstance(v, str):
            v = normalize_half_width(v.strip().upper())
        return v

    @field_validator('symbol')
    @classmethod
    def valid_symbol(cls, v):
        if not SYMBOL_REGEX.match(v):
            raise ValueError('交易对格式错误，必须为大写字母和数字组合')
        return v

    @field_validator('direction', mode='before')
    @classmethod
    def upper_direction(cls, v):
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @field_validator('module')
    @classmethod
    def allowed_module(cls, v):
        if v not in ALLOWED_MODULES:
            raise ValueError(f'模块 {v} 未注册或不允许')
        return v

    @field_validator('metadata')
    @classmethod
    def check_metadata(cls, v):
        return validate_metadata_structure(v)

    @field_validator('request_id')
    @classmethod
    def validate_request_id(cls, v):
        if v and not REQUEST_ID_REGEX.match(v):
            raise ValueError('request_id 只能包含字母、数字、下划线和连字符')
        return v

    @field_validator('price', 'size', mode='before')
    @classmethod
    def ensure_float(cls, v):
        if isinstance(v, str):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError('无效的数字格式')
        return v

    @field_validator('price')
    @classmethod
    def check_price(cls, v):
        if v is not None:
            if not math.isfinite(v):
                raise ValueError('价格必须为有限值')
            if v <= 0:
                raise ValueError('价格必须大于0')
        return v

    @field_validator('size')
    @classmethod
    def check_size(cls, v):
        if v is not None:
            if not math.isfinite(v):
                raise ValueError('数量必须为有限值')
            if v <= 0:
                raise ValueError('数量必须大于0')
        return v

    @field_validator('client_timestamp')
    @classmethod
    def utc_client_time(cls, v):
        if v:
            v = utc_datetime(v)
            now = datetime.now(timezone.utc)
            if abs(v - now) > timedelta(minutes=5):
                raise ValueError('客户端时间与服务器偏差超过5分钟')
        return v

    @model_validator(mode='after')
    def validate_price_size_logic(self) -> 'SignalRequest':
        direction = self.direction
        price = self.price
        size = self.size
        if direction == 'CLOSE':
            # 平仓可无需 price 和 size，按全平或给定 size 平仓
            return self
        # 开仓
        if price is None and size is None:
            raise ValueError('市价单需提供 size，限价单需提供 price 和 size')
        if price is not None and size is None:
            raise ValueError('限价单必须同时提供 size')
        return self

    model_config = ConfigDict(extra='forbid', validate_assignment=True, frozen=False, validate_default=True)


class SignalResponse(BaseModel):
    id: Annotated[str, Field(max_length=36, description="信号ID (UUID)")]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="信号时间 (UTC)")
    symbol: Annotated[str, Field(min_length=2, max_length=10, description="交易对")]
    direction: Literal['LONG', 'SHORT', 'CLOSE']
    price: Annotated[float, Field(gt=0, le=1e12, description="价格")]
    probability: Annotated[float, Field(ge=0, le=1, description="概率 0-1")]
    module: Annotated[str, Field(max_length=50, description="来源模块")]
    action: Literal['OPEN', 'CLOSE', 'REDUCE', 'REJECT']
    status: Literal['EXECUTED', 'REJECTED', 'MERGED', 'PENDING']
    reject_reason: Optional[Annotated[str, Field(max_length=500)]] = None
    order_id: Optional[Annotated[str, Field(max_length=64)]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator('id', mode='before')
    @classmethod
    def normalize_id(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator('id')
    @classmethod
    def valid_uuid(cls, v):
        if not UUID_REGEX.match(v):
            raise ValueError('信号ID必须为UUID格式')
        return v

    @field_validator('symbol', mode='before')
    @classmethod
    def pre_symbol(cls, v):
        if isinstance(v, str):
            v = normalize_half_width(v.strip().upper())
        return v

    @field_validator('symbol')
    @classmethod
    def valid_symbol(cls, v):
        if not SYMBOL_REGEX.match(v):
            raise ValueError('交易对格式错误')
        return v

    @field_validator('direction', 'action', 'status', mode='before')
    @classmethod
    def upper_case(cls, v):
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @field_validator('module')
    @classmethod
    def allowed_module(cls, v):
        if v not in ALLOWED_MODULES:
            raise ValueError(f'模块 {v} 未注册')
        return v

    @field_validator('metadata')
    @classmethod
    def check_metadata(cls, v):
        return validate_metadata_structure(v)

    @field_validator('timestamp')
    @classmethod
    def ensure_utc(cls, v):
        return utc_datetime(v)

    @field_validator('reject_reason', mode='before')
    @classmethod
    def sanitize_reason(cls, v):
        if isinstance(v, str):
            v = v.strip()
            v = html.escape(v, quote=False)
            v = v.replace('\n', ' ').replace('\r', ' ')
        return v

    @field_validator('order_id', mode='before')
    @classmethod
    def normalize_order_id(cls, v):
        if isinstance(v, str) and v.strip() == '':
            return None
        if v and not ORDER_ID_REGEX.match(v):
            raise ValueError('order_id 格式无效')
        return v

    @field_validator('price', 'probability', mode='before')
    @classmethod
    def to_float(cls, v):
        if isinstance(v, str):
            try:
                return float(v)
            except (TypeError, ValueError):
                raise ValueError('无效的数字格式')
        return v

    @field_validator('price')
    @classmethod
    def check_price_finite(cls, v):
        return check_finite_and_positive(v)

    @field_validator('probability')
    @classmethod
    def clamp_probability(cls, v):
        return check_probability(v)

    @model_validator(mode='after')
    def validate_logic(self) -> 'SignalResponse':
        # 动作和状态合理性检查
        if self.action == 'REJECT' and self.status != 'REJECTED':
            raise ValueError('拒绝信号的状态必须是REJECTED')
        if self.action != 'REJECT' and self.status == 'REJECTED':
            raise ValueError('非拒绝信号的状态不能是REJECTED')
        return self

    @field_serializer('timestamp')
    def serialize_timestamp(self, value: datetime, _info):
        return serialize_datetime(value)

    model_config = ConfigDict(extra='forbid', validate_assignment=True, frozen=True, validate_default=True)


class SignalListResponse(BaseModel):
    total: Annotated[int, Field(ge=0, description="总条数")]
    page: Annotated[int, Field(ge=1, description="当前页码")]
    page_size: Annotated[int, Field(ge=1, le=100, description="每页条数")]
    signals: List[SignalResponse] = Field(default_factory=list, description="信号列表（最多1000条）")

    @field_validator('signals')
    @classmethod
    def limit_signals(cls, v):
        if len(v) > 1000:
            raise ValueError('信号列表不能超过1000条')
        return v

    model_config = ConfigDict(extra='forbid', validate_assignment=True, frozen=True)


class SignalFilter(BaseModel):
    symbol: Optional[Annotated[str, Field(min_length=2, max_length=10)]] = None
    direction: Optional[Literal['LONG', 'SHORT', 'CLOSE']] = None
    module: Optional[Annotated[str, Field(max_length=50)]] = None
    action: Optional[Literal['OPEN', 'CLOSE', 'REDUCE', 'REJECT']] = None
    status: Optional[Literal['EXECUTED', 'REJECTED', 'MERGED', 'PENDING']] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    probability_min: Optional[Annotated[float, Field(ge=0, le=1)]] = None
    probability_max: Optional[Annotated[float, Field(ge=0, le=1)]] = None
    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20

    @field_validator('symbol', mode='before')
    @classmethod
    def pre_symbol(cls, v):
        if isinstance(v, str):
            return normalize_half_width(v.strip().upper())
        return v

    @field_validator('symbol')
    @classmethod
    def valid_symbol(cls, v):
        if v and not SYMBOL_REGEX.match(v):
            raise ValueError('交易对格式错误')
        return v

    @field_validator('direction', 'action', 'status', mode='before')
    @classmethod
    def upper_case(cls, v):
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @field_validator('module')
    @classmethod
    def check_module(cls, v):
        if v and v not in ALLOWED_MODULES:
            raise ValueError(f'模块 {v} 未注册')
        return v

    @field_validator('probability_min', 'probability_max', mode='before')
    @classmethod
    def to_float(cls, v):
        if isinstance(v, str):
            try:
                return float(v)
            except (TypeError, ValueError):
                raise ValueError('无效的数字格式')
        return v

    @model_validator(mode='after')
    def validate_range(self) -> 'SignalFilter':
        pmin = self.probability_min
        pmax = self.probability_max
        if pmin is not None and pmax is not None and pmin > pmax:
            raise ValueError('最小概率不能大于最大概率')
        start = self.start_time
        end = self.end_time
        if start and end:
            start = utc_datetime(start)
            end = utc_datetime(end)
            if start > end:
                raise ValueError('开始时间不能晚于结束时间')
            if (end - start).days > 90:
                raise ValueError('查询时间跨度不能超过90天')
            # 返回更新后的模型
            return self.model_copy(update={'start_time': start, 'end_time': end})
        return self

    model_config = ConfigDict(extra='forbid', validate_assignment=True, frozen=True)
