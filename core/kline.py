# -*- coding: utf-8 -*-
from __future__ import annotations

"""
模块名称: kline.py
核心职责: 定义不可变K线数据模型，提供金融级完整校验、序列化与交易所适配。
所属层级: core.models

外部依赖:
    - dataclasses, typing, math, datetime, re, logging, types
    - json

接口契约:
    提供: Kline 不可变数据类
    消费: 无

注意:
    - 本模型仅适用于正价格资产（如加密货币），不适用于可能负价格的商品。
    - 月周期 '1M' 的秒数为近似值 (30天)，涉及精确到期日的计算应使用专业库。

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2026-07-07 v6.0: 终极鲁棒版本，修复所有已知边界情况、性能及内存问题。
"""

import math
import re
import json
import logging
from dataclasses import dataclass, field, InitVar, fields, replace
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, ClassVar, Final, Tuple
from types import MappingProxyType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_INTERVAL_PATTERN = re.compile(r'^(\d+)(m|h|d|w|M)$')
_INTERVAL_NORMALIZE: Dict[str, str] = {
    '3min': '3m', '5min': '5m', '15min': '15m',
    '1h': '1h', '4h': '4h',
    '1day': '1d', '1week': '1w', '1month': '1M',
}
_INTERVAL_SECONDS: Final[MappingProxyType] = MappingProxyType({
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800,
    '1M': 2592000,   # 近似30天，需要精确日期请使用 calendar 模块
})

_STABLE_QUOTES = {'USDT', 'USDC', 'BUSD', 'DAI', 'USD', 'EUR', 'JPY',
                  'BTC', 'ETH', 'BNB', 'XRP', 'TRX'}


class ConsistencyIssue:
    """一致性检查结果"""
    def __init__(self, severity: str, message: str):
        self.severity = severity
        self.message = message

    def to_dict(self) -> Dict[str, str]:
        return {'severity': self.severity, 'msg': self.message}


@dataclass(frozen=True, slots=True)
class Kline:
    """
    不可变K线数据模型。
    时间戳: UTC 毫秒整数；价格: 浮点数。
    实例化时自动执行类型强制和完整校验。
    仅适用于正价格资产。
    """
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    is_closed: bool = False
    source: str = ""
    open_interest: Optional[float] = None

    _validate: InitVar[bool] = True

    # 类级配置
    IGNORE_FUTURE_CHECK: ClassVar[bool] = False
    MAX_FUTURE_MS: ClassVar[int] = 300_000
    DOJI_THRESHOLD: ClassVar[float] = 0.1
    FLAT_RANGE_THRESHOLD: ClassVar[float] = 1e-8
    VALID_INTERVALS: ClassVar[Tuple[str, ...]] = tuple(_INTERVAL_SECONDS.keys())

    # 日志去重缓存 (线程安全建议加锁，若多线程环境)
    _warned_cache: ClassVar[set] = set()
    _WARNED_CACHE_MAX_SIZE: ClassVar[int] = 10000

    def __post_init__(self, _validate: bool) -> None:
        # 始终执行类型强制
        self._coerce_types()
        if _validate:
            self._perform_validation()

    # -------------------------------------------------------------------------
    # 类型强制 (绕过 frozen)
    # -------------------------------------------------------------------------
    def _coerce_types(self) -> None:
        """强制将数值字段转换为正确的类型。"""
        # int 字段
        for int_f in ('open_time_ms', 'close_time_ms', 'trades'):
            val = getattr(self, int_f)
            if val is not None and not isinstance(val, int):
                object.__setattr__(self, int_f, int(val))
        # float 字段不强制，因为 int 可接受
        # None 保护
        if self.open_interest is not None and not isinstance(self.open_interest, (int, float)):
            object.__setattr__(self, 'open_interest', float(self.open_interest))

    # -------------------------------------------------------------------------
    # 内部安全创建（跳过校验）
    # -------------------------------------------------------------------------
    @classmethod
    def _unsafe_create(cls, **kwargs: Any) -> 'Kline':
        """
        绕过校验创建实例。调用者必须确保数据合法。
        仅供内部高性能批量加载使用。
        """
        req_fields = {f.name for f in fields(cls)}
        missing = req_fields - set(kwargs.keys())
        if missing:
            raise ValueError(f"_unsafe_create 缺少字段: {missing}")
        instance = object.__new__(cls)
        for k, v in kwargs.items():
            object.__setattr__(instance, k, v)
        # 类型强制
        instance._coerce_types()
        return instance

    # -------------------------------------------------------------------------
    # 校验
    # -------------------------------------------------------------------------
    def _perform_validation(self) -> None:
        errors: List[str] = []
        warnings: List[str] = []
        max_errors = 20

        # --- symbol ---
        if not isinstance(self.symbol, str) or not (1 <= len(self.symbol) <= 20):
            errors.append("symbol 长度 1-20")
        elif re.search(r'[^A-Z0-9.\-_/]', self.symbol):
            errors.append("symbol 包含非法字符")

        # --- interval ---
        if not isinstance(self.interval, str) or not self.interval:
            errors.append("interval 缺失")
        else:
            norm = self.normalize_interval(self.interval)
            if norm not in self.VALID_INTERVALS:
                errors.append(f"interval '{self.interval}' 不支持")

        # --- source ---
        if not isinstance(self.source, str) or len(self.source) > 16:
            errors.append("source 长度 ≤ 16")
        elif self.source and re.search(r'[^A-Za-z0-9_]', self.source):
            errors.append("source 含非法字符")

        # --- 时间戳 ---
        if self.open_time_ms <= 0:
            errors.append("open_time_ms 无效")
        if self.close_time_ms <= 0:
            errors.append("close_time_ms 无效")
        if self.close_time_ms < self.open_time_ms:
            errors.append("close_time_ms < open_time_ms")
        if not self.IGNORE_FUTURE_CHECK:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            if self.open_time_ms > now_ms + self.MAX_FUTURE_MS:
                errors.append(f"open_time_ms 太未来")

        # --- 价格 ---
        for name in ['open', 'high', 'low', 'close']:
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
                errors.append(f"{name} 不是有效数字")
            elif v < 0:
                errors.append(f"{name} 为负数")
        if self.high <= 0:
            errors.append("high 必须 > 0 (仅支持正价格资产)")
        if self.low > self.high:
            errors.append("low > high")
        if not (self.low <= self.open <= self.high):
            errors.append("open 不在 [low, high]")
        if not (self.low <= self.close <= self.high):
            errors.append("close 不在 [low, high]")

        # --- 成交量 ---
        for name in ['volume', 'quote_volume']:
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v) or v < 0:
                errors.append(f"{name} 无效")

        # --- trades ---
        if not isinstance(self.trades, int) or self.trades < 0:
            errors.append("trades 无效")

        # --- open_interest ---
        if self.open_interest is not None:
            if not isinstance(self.open_interest, (int, float)) or math.isnan(self.open_interest) or math.isinf(self.open_interest) or self.open_interest < 0:
                errors.append("open_interest 无效")

        # --- 非致命警告 (去重) ---
        def _warn_once(sym: str, key: str, msg: str) -> None:
            if len(Kline._warned_cache) >= Kline._WARNED_CACHE_MAX_SIZE:
                Kline._warned_cache.clear()
            if (sym, key) not in Kline._warned_cache:
                Kline._warned_cache.add((sym, key))
                warnings.append(msg)

        if self.volume > 0 and self.trades == 0:
            _warn_once(self.symbol, 'no_trades', "volume>0 但 trades==0")
        if self.volume > 0 and self.quote_volume == 0:
            _warn_once(self.symbol, 'no_quote_vol', "volume>0 但 quote_volume==0")
        if self.volume == 0 and self.quote_volume > 0:
            _warn_once(self.symbol, 'no_vol', "volume==0 但 quote_volume>0")

        if errors:
            raise ValueError("Kline 校验失败 | " + " | ".join(errors[:max_errors]))
        for w in warnings:
            logger.warning(f"{self.symbol} {self.interval}: {w}")

    # -------------------------------------------------------------------------
    # 标准化工具
    # -------------------------------------------------------------------------
    @staticmethod
    def normalize_symbol(s: Optional[str]) -> str:
        if s is None:
            return ''
        return s.strip().upper()

    @staticmethod
    def normalize_interval(iv: Optional[str]) -> str:
        if iv is None:
            return ''
        iv = iv.lower().strip()
        return _INTERVAL_NORMALIZE.get(iv, iv)

    # -------------------------------------------------------------------------
    # 类方法设置阈值
    # -------------------------------------------------------------------------
    @classmethod
    def set_doji_threshold(cls, threshold: float) -> None:
        cls.DOJI_THRESHOLD = threshold

    @classmethod
    def set_flat_threshold(cls, threshold: float) -> None:
        cls.FLAT_RANGE_THRESHOLD = threshold

    # -------------------------------------------------------------------------
    # 工厂方法
    # -------------------------------------------------------------------------
    @classmethod
    def create(cls, **kwargs: Any) -> 'Kline':
        """创建带校验的 Kline 实例。"""
        if 'symbol' in kwargs:
            kwargs['symbol'] = cls.normalize_symbol(kwargs['symbol'])
        if 'interval' in kwargs:
            kwargs['interval'] = cls.normalize_interval(kwargs['interval'])
        # 类型预转换
        for int_f in ('open_time_ms', 'close_time_ms', 'trades'):
            if int_f in kwargs and kwargs[int_f] is not None:
                kwargs[int_f] = int(kwargs[int_f])
        for float_f in ('open', 'high', 'low', 'close', 'volume', 'quote_volume'):
            if float_f in kwargs and kwargs[float_f] is not None:
                kwargs[float_f] = float(kwargs[float_f])
        if 'open_interest' in kwargs and kwargs['open_interest'] is not None:
            kwargs['open_interest'] = float(kwargs['open_interest'])
        kwargs.setdefault('is_closed', False)
        kwargs.setdefault('source', '')
        return cls(**kwargs)

    def evolve(self, **changes: Any) -> 'Kline':
        """创建修改了指定字段的新实例。"""
        try:
            return replace(self, **changes)
        except TypeError as e:
            raise ValueError(f"无效的字段: {e}") from e

    # -------------------------------------------------------------------------
    # 序列化
    # -------------------------------------------------------------------------
    def to_dict(self, include_iso_time: bool = False) -> Dict[str, Any]:
        d = {
            'symbol': self.symbol,
            'interval': self.interval,
            'open_time_ms': self.open_time_ms,
            'close_time_ms': self.close_time_ms,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'quote_volume': self.quote_volume,
            'trades': self.trades,
            'is_closed': self.is_closed,
            'source': self.source,
            'open_interest': self.open_interest,
        }
        if include_iso_time:
            def _safe_iso(ts_ms: int) -> str:
                try:
                    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                except (OSError, OverflowError):
                    return f"invalid_timestamp({ts_ms})"
            d['open_time'] = _safe_iso(self.open_time_ms)
            d['close_time'] = _safe_iso(self.close_time_ms)
        return d

    def to_json(self, include_iso_time: bool = False, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(include_iso_time), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], reference_time_ms: Optional[int] = None) -> 'Kline':
        symbol = cls.normalize_symbol(data.get('symbol', ''))
        interval = cls.normalize_interval(data.get('interval', ''))
        if not symbol or not interval:
            raise ValueError("symbol 和 interval 必须提供")

        def _parse_time(key: str, alt_key: str) -> int:
            val = data.get(key) or data.get(alt_key)
            if val is None:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str):
                # 尝试多种 ISO 格式
                for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S%z'):
                    try:
                        dt = datetime.strptime(val, fmt)
                        return int(dt.timestamp() * 1000)
                    except ValueError:
                        continue
                # 尝试替换 'Z' 后使用 fromisoformat
                try:
                    dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
                    return int(dt.timestamp() * 1000)
                except Exception:
                    raise ValueError(f"无法解析时间字段 {key}: {val}")
            raise ValueError(f"无效的时间戳 {key}: {val}")

        open_time_ms = _parse_time('open_time_ms', 'open_time')
        close_time_ms = _parse_time('close_time_ms', 'close_time')

        if close_time_ms <= 0 and interval in _INTERVAL_SECONDS:
            close_time_ms = open_time_ms + _INTERVAL_SECONDS[interval] * 1000

        def _safe_float(key: str, default: float = 0.0) -> float:
            val = data.get(key, default)
            if val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                raise ValueError(f"字段 {key} 不是有效数字")

        open_interest = None
        if 'open_interest' in data and data['open_interest'] is not None:
            try:
                open_interest = float(data['open_interest'])
            except (ValueError, TypeError):
                raise ValueError("open_interest 无效")

        is_closed = bool(data.get('is_closed'))
        if not is_closed and close_time_ms > 0:
            now_ms = reference_time_ms if reference_time_ms is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            is_closed = close_time_ms <= now_ms

        return cls.create(
            symbol=symbol,
            interval=interval,
            open_time_ms=open_time_ms,
            close_time_ms=close_time_ms,
            open=_safe_float('open'),
            high=_safe_float('high'),
            low=_safe_float('low'),
            close=_safe_float('close'),
            volume=_safe_float('volume'),
            quote_volume=_safe_float('quote_volume'),
            trades=int(data.get('trades', 0)),
            is_closed=is_closed,
            source=data.get('source', ''),
            open_interest=open_interest,
        )

    # -------------------------------------------------------------------------
    # 交易所适配器
    # -------------------------------------------------------------------------
    @classmethod
    def from_binance_kline(cls, data: list, symbol: str, interval: str,
                           source_override: str = 'binance') -> 'Kline':
        if len(data) < 9:
            raise ValueError("Binance K线数组长度不足")
        try:
            return cls.create(
                symbol=symbol,
                interval=interval,
                open_time_ms=int(data[0]),
                open=float(data[1]),
                high=float(data[2]),
                low=float(data[3]),
                close=float(data[4]),
                volume=float(data[5]),
                close_time_ms=int(data[6]),
                quote_volume=float(data[7]),
                trades=int(data[8]),
                source=source_override,
            )
        except (ValueError, TypeError, OverflowError) as e:
            raise ValueError(f"解析Binance K线失败: {e}") from e

    @classmethod
    def from_okx_kline(cls, data: list, symbol: str, interval: str,
                       source_override: str = 'okx') -> 'Kline':
        if len(data) < 7:
            raise ValueError("OKX K线数组长度不足")
        try:
            open_time_ms = int(data[0])
            seconds = _INTERVAL_SECONDS.get(interval, 60)
            return cls.create(
                symbol=symbol,
                interval=interval,
                open_time_ms=open_time_ms,
                open=float(data[1]),
                high=float(data[2]),
                low=float(data[3]),
                close=float(data[4]),
                volume=float(data[5]),
                quote_volume=float(data[6]),
                close_time_ms=open_time_ms + seconds * 1000,
                trades=0,
                source=source_override,
            )
        except (ValueError, TypeError, OverflowError) as e:
            raise ValueError(f"解析OKX K线失败: {e}") from e

    # -------------------------------------------------------------------------
    # 属性
    # -------------------------------------------------------------------------
    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        rng = self.total_range
        if rng <= 0.0:
            return True
        return (self.body / rng) < self.DOJI_THRESHOLD

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_direction(self) -> float:
        return self.close - self.open

    @property
    def upper_wick(self) -> float:
        return max(0.0, self.high - max(self.open, self.close))

    @property
    def lower_wick(self) -> float:
        return max(0.0, min(self.open, self.close) - self.low)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def median_price(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def vwap(self) -> float:
        if self.volume > 0 and self.quote_volume > 0:
            return self.quote_volume / self.volume
        return self.typical_price

    @property
    def base_asset(self) -> str:
        symbol = self.symbol
        if not symbol:
            return ''
        best_quote = ''
        for q in _STABLE_QUOTES:
            if symbol.endswith(q) and len(q) > len(best_quote):
                best_quote = q
        if best_quote:
            return symbol[:-len(best_quote)]
        return symbol[:-3] if len(symbol) > 3 else symbol

    @property
    def quote_asset(self) -> str:
        symbol = self.symbol
        if not symbol:
            return ''
        best_quote = ''
        for q in _STABLE_QUOTES:
            if symbol.endswith(q) and len(q) > len(best_quote):
                best_quote = q
        if best_quote:
            return best_quote
        return symbol[-3:] if len(symbol) >= 3 else ''

    @property
    def interval_seconds(self) -> int:
        sec = _INTERVAL_SECONDS.get(self.interval)
        if sec is None:
            logger.warning(f"未知周期 {self.interval}，返回默认60秒")
            return 60
        return sec

    @property
    def duration_ms(self) -> int:
        return self.close_time_ms - self.open_time_ms

    @property
    def is_valid(self) -> bool:
        return self.open_time_ms > 0 and self.close_time_ms > 0 and self.volume >= 0

    @property
    def is_flat(self) -> bool:
        return (self.high - self.low) < self.FLAT_RANGE_THRESHOLD

    def is_gap_up(self, prev_close: float) -> bool:
        if math.isnan(prev_close):
            return False
        return self.low > prev_close

    def is_gap_down(self, prev_close: float) -> bool:
        if math.isnan(prev_close):
            return False
        return self.high < prev_close

    # -------------------------------------------------------------------------
    # 一致性检查
    # -------------------------------------------------------------------------
    def check_consistency(self) -> List[ConsistencyIssue]:
        issues = []
        if self.volume > 0 and self.quote_volume > 0:
            implied = self.quote_volume / self.volume
            if implied < self.low or implied > self.high:
                issues.append(ConsistencyIssue('warning', f'VWAP {implied:.2f} outside [{self.low},{self.high}]'))
        if self.volume > 0 and self.trades == 0:
            issues.append(ConsistencyIssue('warning', 'volume>0 but trades=0'))
        return issues

    # -------------------------------------------------------------------------
    # 排序
    # -------------------------------------------------------------------------
    def __lt__(self, other: 'Kline') -> bool:
        if self.open_time_ms != other.open_time_ms:
            return self.open_time_ms < other.open_time_ms
        if self.close_time_ms != other.close_time_ms:
            return self.close_time_ms < other.close_time_ms
        if self.interval != other.interval:
            return self.interval < other.interval
        return self.symbol < other.symbol

    # -------------------------------------------------------------------------
    # 显示
    # -------------------------------------------------------------------------
    def __repr__(self) -> str:
        direction = "↑" if self.is_bullish else "↓"
        parts = [
            f"Kline({self.symbol} {self.interval} {direction}",
            f"O:{self.open:.8g} H:{self.high:.8g} L:{self.low:.8g} C:{self.close:.8g}",
        ]
        if self.open_interest is not None:
            parts.append(f"OI:{self.open_interest:.8g}")
        parts.append(")")
        return " ".join(parts)

    def __str__(self) -> str:
        return f"{self.symbol} {self.interval} @ {self.open_time_ms}"

    def repr_safe(self) -> str:
        return f"Kline({self.symbol} {self.interval} [closed={self.is_closed}])"

__all__ = ['Kline']
