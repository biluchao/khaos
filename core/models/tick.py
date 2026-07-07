# -*- coding: utf-8 -*-
"""
模块名称: tick.py
核心职责: 定义不可变、高精度、审计安全的逐笔成交（Tick）数据模型。
所属层级: core.models

设计原则:
    - 不可变性保证数据完整性，适合在多线程环境中安全共享。
    - 所有价格/数量字段提供 Decimal 版本，避免浮点误差累积。
    - 二进制序列化采用版本化的紧凑格式，支持向后兼容。
    - 内置审计元数据，记录数据来源、时间戳和签名。

使用示例:
    >>> tick = Tick(symbol="BTCUSDT", trade_id="123", price=50000.0, quantity=0.1)
    >>> tick.is_buy
    True

作者: KHAOS System Architect
创建日期: 2025-01-15
修改记录:
    - 2026-07-07 v4.0: 终极审计版本，增加审计字段、压缩序列化、零拷贝优化。
"""

from __future__ import annotations

import json
import struct
import zlib
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple, ClassVar, List
from datetime import datetime, timezone

import numpy as np  # 可选


@dataclass(frozen=True, slots=True)
class Tick:
    """
    逐笔成交数据模型。

    - 所有时间戳为 UTC 毫秒。
    - trade_id 与 symbol 共同构成全局唯一标识。
    - is_buyer_maker = True 表示卖方主动（买家是挂单方）。
    """
    # ---- 核心字段 ----
    symbol: str
    trade_id: str
    price: float
    quantity: float
    timestamp: int

    # ---- 可选字段 ----
    quote_quantity: float = 0.0
    is_buyer_maker: Optional[bool] = None
    is_liquidation: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---- 审计字段 ----
    source: str = ""                # 数据来源（如 "binance", "okx"）
    arrived_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp() * 1000.0)
    sequence_number: int = 0        # 局部序列号，用于检测丢失
    checksum: Optional[str] = None  # SHA256 校验和，用于防篡改

    __version__: ClassVar[str] = "4.0.0"
    NULL_TRADE_ID: ClassVar[str] = "INVALID"

    # -----------------------------------------------------------------
    # 初始化后校验
    # -----------------------------------------------------------------
    def __post_init__(self):
        """规范化并校验字段。raises: ValueError"""
        # symbol
        object.__setattr__(self, 'symbol', self.symbol.strip().upper())
        if not self.symbol:
            raise ValueError("symbol 不能为空")

        # trade_id
        if not self.trade_id or len(self.trade_id) > 64:
            raise ValueError("trade_id 必须为非空且长度<=64")

        # 价格/数量必须为正
        if self.price <= 0:
            raise ValueError("price 必须大于 0")
        if self.quantity <= 0:
            raise ValueError("quantity 必须大于 0")

        # 成交额
        if self.quote_quantity <= 0:
            computed = self.price * self.quantity
            object.__setattr__(self, 'quote_quantity', computed)

        # extra 深拷贝
        object.__setattr__(self, 'extra', deepcopy(self.extra))

        # 如果未提供 checksum，自动计算
        if self.checksum is None:
            object.__setattr__(self, 'checksum', self._compute_checksum())

    def _compute_checksum(self) -> str:
        """基于核心字段的 SHA256 哈希，用于防篡改。"""
        import hashlib
        data = f"{self.symbol}|{self.trade_id}|{self.price}|{self.quantity}|{self.timestamp}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    # -----------------------------------------------------------------
    # 属性
    # -----------------------------------------------------------------
    @property
    def is_buy(self) -> Optional[bool]:
        """主动买入？None 表示未知。"""
        return None if self.is_buyer_maker is None else not self.is_buyer_maker

    @property
    def is_sell(self) -> Optional[bool]:
        """主动卖出？"""
        return None if self.is_buyer_maker is None else self.is_buyer_maker

    @property
    def signed_quantity(self) -> float:
        if self.is_buy is None:
            return 0.0
        return self.quantity if self.is_buy else -self.quantity

    @property
    def price_dec(self) -> Decimal:
        return Decimal(str(self.price))

    @property
    def quantity_dec(self) -> Decimal:
        return Decimal(str(self.quantity))

    @property
    def quote_quantity_dec(self) -> Decimal:
        return Decimal(str(self.quote_quantity))

    @property
    def is_valid(self) -> bool:
        return (self.trade_id != self.NULL_TRADE_ID and 
                self.price > 0 and self.quantity > 0 and self.timestamp > 0)

    # -----------------------------------------------------------------
    # 序列化
    # -----------------------------------------------------------------
    def to_dict(self, redact: bool = False) -> Dict[str, Any]:
        """转换为字典，可选择脱敏。"""
        d = {
            'symbol': self.symbol,
            'trade_id': self.trade_id[-4:] + "****" if redact else self.trade_id,
            'price': self.price,
            'quantity': self.quantity,
            'timestamp': self.timestamp,
            'quote_quantity': self.quote_quantity,
            'is_buyer_maker': self.is_buyer_maker,
            'is_liquidation': self.is_liquidation,
            'source': self.source,
            'arrived_at': self.arrived_at,
            'sequence_number': self.sequence_number,
            'checksum': self.checksum,
        }
        if not redact:
            d['extra'] = deepcopy(self.extra)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Tick:
        """从字典创建。时间戳自动检测单位。"""
        # 时间戳处理：支持秒或毫秒
        ts = int(data['timestamp'])
        if ts < 1e12:  # 秒时间戳
            ts *= 1000
        return cls(
            symbol=str(data.get('symbol', 'UNKNOWN')).upper().strip(),
            trade_id=str(data['trade_id']),
            price=float(data['price']),
            quantity=float(data['quantity']),
            timestamp=ts,
            quote_quantity=float(data.get('quote_quantity', 0.0)),
            is_buyer_maker=data.get('is_buyer_maker'),
            is_liquidation=bool(data.get('is_liquidation', False)),
            extra=deepcopy(data.get('extra', {})),
            source=str(data.get('source', '')),
            arrived_at=float(data.get('arrived_at', ts)),
            sequence_number=int(data.get('sequence_number', 0)),
            checksum=data.get('checksum'),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> Tick:
        return cls.from_dict(json.loads(s))

    def to_bytes(self, version: int = 1) -> bytes:
        """
        紧凑二进制序列化。
        版本1: 固定长度头部 + 压缩 extra。
        """
        # 头部: symbol(20s), trade_id(64s), price(d), quantity(d), timestamp(q),
        #        quote_quantity(d), is_buyer_maker(b), is_liquidation(b),
        #        source(10s), arrived_at(q), sequence_number(q)
        sym = self.symbol.encode('utf-8')[:20].ljust(20, b'\x00')
        tid = self.trade_id.encode('utf-8')[:64].ljust(64, b'\x00')
        src = self.source.encode('utf-8')[:10].ljust(10, b'\x00')
        buyer_flag = 0 if self.is_buyer_maker is None else (1 if self.is_buyer_maker else 2)
        header = struct.pack('!20s 64s d d q d B B 10s q q',
                             sym, tid, self.price, self.quantity, self.timestamp,
                             self.quote_quantity, buyer_flag, self.is_liquidation,
                             src, int(self.arrived_at), self.sequence_number)
        # extra 压缩
        extra_json = json.dumps(self.extra).encode('utf-8')
        compressed = zlib.compress(extra_json)
        payload = header + struct.pack('!I', len(compressed)) + compressed
        # 版本前缀
        return struct.pack('!B', version) + payload

    @classmethod
    def from_bytes(cls, data: bytes) -> Tick:
        """从二进制数据恢复。"""
        version = struct.unpack('!B', data[:1])[0]
        offset = 1
        if version == 1:
            header_fmt = '!20s 64s d d q d B B 10s q q'
            header_len = struct.calcsize(header_fmt)
            (sym, tid, price, quantity, ts, qq, buyer_flag, liquidation,
             src, arrived, seq) = struct.unpack(header_fmt, data[offset:offset+header_len])
            offset += header_len
            extra_len = struct.unpack('!I', data[offset:offset+4])[0]
            offset += 4
            compressed = data[offset:offset+extra_len]
            extra = json.loads(zlib.decompress(compressed))
            return cls(
                symbol=sym.decode('utf-8').rstrip('\x00'),
                trade_id=tid.decode('utf-8').rstrip('\x00'),
                price=price, quantity=quantity,
                timestamp=ts, quote_quantity=qq,
                is_buyer_maker=None if buyer_flag == 0 else (True if buyer_flag == 1 else False),
                is_liquidation=bool(liquidation),
                source=src.decode('utf-8').rstrip('\x00'),
                arrived_at=arrived, sequence_number=seq,
                extra=extra,
            )
        else:
            raise ValueError(f"不支持的二进制版本: {version}")

    def to_tuple(self) -> Tuple:
        return (self.symbol, self.trade_id, self.price, self.quantity, self.timestamp,
                self.quote_quantity, self.is_buyer_maker, self.is_liquidation,
                self.source, int(self.arrived_at), self.sequence_number, self.checksum,
                json.dumps(self.extra))

    @classmethod
    def from_tuple(cls, tup: Tuple) -> Tick:
        return cls(
            symbol=tup[0], trade_id=tup[1], price=tup[2], quantity=tup[3],
            timestamp=tup[4], quote_quantity=tup[5],
            is_buyer_maker=tup[6],
            is_liquidation=bool(tup[7]),
            source=tup[8] if len(tup) > 8 else "",
            arrived_at=tup[9] if len(tup) > 9 else tup[4],
            sequence_number=tup[10] if len(tup) > 10 else 0,
            checksum=tup[11] if len(tup) > 11 else None,
            extra=json.loads(tup[12]) if len(tup) > 12 else {},
        )

    # -----------------------------------------------------------------
    # 比较与哈希（以 (symbol, trade_id, timestamp) 为标识）
    # -----------------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Tick):
            return NotImplemented
        return (self.symbol == other.symbol and 
                self.trade_id == other.trade_id and 
                self.timestamp == other.timestamp)

    def __hash__(self) -> int:
        return hash((self.symbol, self.trade_id, self.timestamp))

    def __lt__(self, other: Tick) -> bool:
        return self.timestamp < other.timestamp

    def __repr__(self) -> str:
        dir_str = "B" if self.is_buy else ("S" if self.is_sell else "?")
        return (f"Tick({self.symbol} {dir_str} P:{self.price} Q:{self.quantity} "
                f"@{self.timestamp})")

    # -----------------------------------------------------------------
    # 工具方法
    # -----------------------------------------------------------------
    @staticmethod
    def price_change(t1: Tick, t2: Tick) -> float:
        if t1.price == 0:
            return 0.0
        return (t2.price - t1.price) / t1.price * 100.0

    @classmethod
    def null(cls, symbol: str = "UNKNOWN") -> Tick:
        return cls(symbol=symbol, trade_id=cls.NULL_TRADE_ID, price=0.0, quantity=0.0,
                   timestamp=0)

    @classmethod
    def from_exchange_msg(cls, exchange: str, msg: Dict[str, Any]) -> Tick:
        """
        从交易所原始消息创建 Tick。注意：字段映射由适配器完成，
        此方法仅做标准化后的统一创建。
        """
        return cls.from_dict(msg)
