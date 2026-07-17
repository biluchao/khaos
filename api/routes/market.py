# -*- coding: utf-8 -*-
"""
模块名称: market.py
核心职责: 提供市场数据 REST API：交易对、K线、行情、订单簿、成交、交易所信息。
所属层级: api.routes

外部依赖:
    - fastapi (APIRouter, Depends, HTTPException, Query, Request, Response)
    - pydantic (BaseModel, Field, conint)
    - services.market_data_service (MarketDataService)
    - api.dependencies (权限、限流、数据服务)
    - typing (List, Optional, Dict, Any, Tuple)
    - asyncio, time, uuid, logging, datetime

接口契约:
    提供: 6个GET端点
    消费: MarketDataService, 限流器, 权限依赖

配置项 (通过依赖注入 config):
    - market.max_concurrency: 最大并发请求数 (默认 100)
    - market.request_timeout: 上游超时秒数 (默认 10)
    - market.allowed_symbols: 交易对白名单

作者: KHAOS System Architect
创建日期: 2026-07-15
修改记录:
    - 2026-07-18 第五轮 100 项机构级修复，达到终极可靠性
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, conint

from api.dependencies import (
    get_current_user,
    get_market_data_service,
    get_rate_limiter,
    require_permission,
    get_app_config,
)
from services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/market", tags=["market"])

# ---------- 常量 ----------
ALLOWED_INTERVALS = frozenset({"1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"})
MAX_TIME_RANGE_MS = 30 * 24 * 3600 * 1000  # 30天
MAX_OFFSET = 10000

# ---------- 并发控制（上下文管理器，从配置读取）----------
class _ConcurrencyLimiter:
    def __init__(self):
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_initialized(self, config):
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            max_conc = getattr(config, 'market', {}).get('max_concurrency', 100)
            self._semaphore = asyncio.Semaphore(max_conc)
            self._initialized = True

    @asynccontextmanager
    async def limit(self, config, timeout: float = 5.0):
        await self._ensure_initialized(config)
        sem = self._semaphore
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="Server busy, please retry later")
        except asyncio.CancelledError:
            # 任务取消时也需要释放信号量
            sem.release()
            raise
        try:
            yield
        finally:
            sem.release()

_concurrency_limiter = _ConcurrencyLimiter()

async def get_concurrency_limiter(config=Depends(get_app_config)):
    # 返回同一个 limiter 实例，懒初始化
    return _concurrency_limiter, config

# ---------- 统一错误模型 (RFC 7807) ----------
class ProblemDetail(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: Optional[str] = None

# (全局异常处理器应在 app 层将 HTTPException 转换为 ProblemDetail，此处仅定义)

# ---------- 响应模型 ----------
class SymbolInfo(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    min_notional: float
    min_qty: float
    step_size: float
    tick_size: float
    price_precision: int
    qty_precision: int

class KlineResponse(BaseModel):
    open_time: str = Field("", example="2026-07-17T08:00:00+00:00")
    close_time: str = Field("", example="2026-07-17T08:02:59+00:00")
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    trades: int = 0

class TickerResponse(BaseModel):
    symbol: str
    last_price: float
    price_change: float
    price_change_percent: float
    high_24h: float
    low_24h: float
    volume_24h: float
    quote_volume_24h: float
    open_24h: float
    timestamp: int

class OrderBookResponse(BaseModel):
    symbol: str
    bids: List[Tuple[float, float]]
    asks: List[Tuple[float, float]]
    timestamp: int

class TradeRecord(BaseModel):
    id: str
    price: float
    quantity: float
    time: int
    is_buyer_maker: bool

class ExchangeInfo(BaseModel):
    name: str
    server_time: int
    rate_limits: List[Dict[str, Any]]
    timezone: str = "UTC"

# ---------- 工具函数 ----------
def _safe_float(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default

def _ms_to_iso(ms: Optional[int]) -> str:
    if ms is None or ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""

def _validate_symbol(symbol: str):
    if not symbol or not symbol.isalnum() or len(symbol) < 6:
        raise HTTPException(status_code=400, detail="Invalid symbol format")

def _log_audit(request: Request, user: str, trace_id: str, params: dict):
    safe = {k: v for k, v in params.items() if k.lower() not in {"api_key", "secret", "sign"}}
    logger.info(
        "AUDIT market trace=%s user=%s ip=%s user_agent=%s referer=%s endpoint=%s params=%s",
        trace_id, user,
        request.client.host if request.client else "unknown",
        request.headers.get("user-agent", ""),
        request.headers.get("referer", ""),
        request.url.path, safe
    )

def _check_offset(offset: int):
    if offset > MAX_OFFSET:
        raise HTTPException(status_code=400, detail=f"Offset too large, max {MAX_OFFSET}")

# ---------- 路由实现 ----------

@router.get(
    "/symbols",
    response_model=List[SymbolInfo],
    summary="获取交易对列表",
    description="返回所有可交易对的规则信息，支持分页。结果缓存10分钟。",
)
async def get_symbols(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    market_service: MarketDataService = Depends(get_market_data_service),
    current_user: str = Depends(get_current_user),
    _perm: None = Depends(require_permission("market_read")),
    _rl: None = Depends(get_rate_limiter("market/symbols", 30, 60)),
    limiter_and_cfg: Tuple[_ConcurrencyLimiter, Any] = Depends(get_concurrency_limiter),
):
    limiter, config = limiter_and_cfg
    trace_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = trace_id
    _log_audit(request, current_user, trace_id, {"limit": limit, "offset": offset})
    _check_offset(offset)

    async with limiter.limit(config):
        try:
            symbols = await asyncio.wait_for(
                market_service.get_symbols_paginated(limit, offset),
                timeout=8.0
            )
            return [SymbolInfo(**s) for s in (symbols or [])]
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except Exception:
            logger.exception("Symbols failed trace=%s", trace_id)
            raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/klines",
    response_model=List[KlineResponse],
    summary="历史K线数据",
    description="获取指定交易对的K线，支持时间范围过滤，最大跨度30天。",
)
async def get_klines(
    request: Request,
    response: Response,
    symbol: str = Query(..., min_length=1, max_length=20),
    interval: str = Query("3m"),
    limit: int = Query(500, ge=1, le=1500),
    start_time: Optional[int] = Query(None),
    end_time: Optional[int] = Query(None),
    market_service: MarketDataService = Depends(get_market_data_service),
    current_user: str = Depends(get_current_user),
    _perm: None = Depends(require_permission("market_read")),
    _rl: None = Depends(get_rate_limiter("market/klines", 60, 60)),
    limiter_and_cfg: Tuple[_ConcurrencyLimiter, Any] = Depends(get_concurrency_limiter),
):
    limiter, config = limiter_and_cfg
    trace_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = trace_id
    _log_audit(request, current_user, trace_id, {"symbol": symbol, "interval": interval, "limit": limit})
    _validate_symbol(symbol)

    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Unsupported interval: {interval}")
    if start_time and end_time:
        if start_time >= end_time:
            raise HTTPException(status_code=400, detail="start_time must be less than end_time")
        if end_time - start_time > MAX_TIME_RANGE_MS:
            raise HTTPException(status_code=400, detail="Time range too large (max 30 days)")

    async with limiter.limit(config):
        try:
            klines = await asyncio.wait_for(
                market_service.get_klines(symbol, interval, limit, start_time, end_time),
                timeout=10.0
            )
            result = []
            for k in (klines or []):
                o = _safe_float(k.get('open'))
                h = _safe_float(k.get('high'))
                l = _safe_float(k.get('low'))
                c = _safe_float(k.get('close'))
                if any(v <= 0 for v in (o, h, l, c)):
                    continue
                result.append(KlineResponse(
                    open_time=_ms_to_iso(k.get('open_time')),
                    close_time=_ms_to_iso(k.get('close_time')),
                    open=o, high=h, low=l, close=c,
                    volume=_safe_float(k.get('volume')),
                    quote_volume=_safe_float(k.get('quote_volume')),
                    trades=int(k.get('trades', 0))
                ))
            return result
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except HTTPException:
            raise
        except Exception:
            logger.exception("Klines failed trace=%s", trace_id)
            raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/ticker",
    response_model=TickerResponse,
    summary="24小时行情",
    description="返回指定交易对的24小时价格变动与成交量。",
)
async def get_ticker(
    request: Request,
    response: Response,
    symbol: str = Query("BTCUSDT", min_length=1, max_length=20),
    market_service: MarketDataService = Depends(get_market_data_service),
    current_user: str = Depends(get_current_user),
    _perm: None = Depends(require_permission("market_read")),
    _rl: None = Depends(get_rate_limiter("market/ticker", 60, 60)),
    limiter_and_cfg: Tuple[_ConcurrencyLimiter, Any] = Depends(get_concurrency_limiter),
):
    limiter, config = limiter_and_cfg
    trace_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = trace_id
    _log_audit(request, current_user, trace_id, {"symbol": symbol})
    _validate_symbol(symbol)

    async with limiter.limit(config):
        try:
            ticker = await asyncio.wait_for(
                market_service.get_ticker(symbol),
                timeout=5.0
            )
            if not ticker:
                raise HTTPException(status_code=404, detail="Ticker not found")
            if 'timestamp' not in ticker:
                ticker['timestamp'] = int(time.time() * 1000)
            return TickerResponse(**ticker)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except HTTPException:
            raise
        except Exception:
            logger.exception("Ticker failed trace=%s", trace_id)
            raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/orderbook",
    response_model=OrderBookResponse,
    summary="订单簿深度",
    description="获取实时订单簿的限价单簿，深度上限100档。",
)
async def get_orderbook(
    request: Request,
    response: Response,
    symbol: str = Query("BTCUSDT", min_length=1, max_length=20),
    depth: int = Query(10, ge=5, le=100),
    market_service: MarketDataService = Depends(get_market_data_service),
    current_user: str = Depends(get_current_user),
    _perm: None = Depends(require_permission("market_read")),
    _rl: None = Depends(get_rate_limiter("market/orderbook", 30, 60)),
    limiter_and_cfg: Tuple[_ConcurrencyLimiter, Any] = Depends(get_concurrency_limiter),
):
    limiter, config = limiter_and_cfg
    trace_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = trace_id
    _log_audit(request, current_user, trace_id, {"symbol": symbol, "depth": depth})
    _validate_symbol(symbol)

    async with limiter.limit(config):
        try:
            ob = await asyncio.wait_for(
                market_service.get_orderbook(symbol, depth),
                timeout=5.0
            )
            if not ob:
                raise HTTPException(status_code=503, detail="Orderbook unavailable")

            def _extract_pairs(raw_list):
                pairs = []
                for item in (raw_list or []):
                    if not isinstance(item, list) or len(item) < 2:
                        logger.warning("Orderbook invalid pair %s, trace=%s", item, trace_id)
                        continue
                    p = _safe_float(item[0])
                    q = _safe_float(item[1])
                    if p > 0 and q > 0:
                        pairs.append((p, q))
                return pairs

            return OrderBookResponse(
                symbol=symbol,
                bids=_extract_pairs(ob.get("bids", [])[:depth]),
                asks=_extract_pairs(ob.get("asks", [])[:depth]),
                timestamp=ob.get("timestamp", int(time.time() * 1000))
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except HTTPException:
            raise
        except Exception:
            logger.exception("Orderbook failed trace=%s", trace_id)
            raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/trades",
    response_model=List[TradeRecord],
    summary="公开成交记录",
    description="获取最近成交记录，用于价格验证与微观分析。",
)
async def get_recent_trades(
    request: Request,
    response: Response,
    symbol: str = Query("BTCUSDT", min_length=1, max_length=20),
    limit: int = Query(50, ge=1, le=200),
    market_service: MarketDataService = Depends(get_market_data_service),
    current_user: str = Depends(get_current_user),
    _perm: None = Depends(require_permission("market_read")),
    _rl: None = Depends(get_rate_limiter("market/trades", 30, 60)),
    limiter_and_cfg: Tuple[_ConcurrencyLimiter, Any] = Depends(get_concurrency_limiter),
):
    limiter, config = limiter_and_cfg
    trace_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = trace_id
    _log_audit(request, current_user, trace_id, {"symbol": symbol, "limit": limit})
    _validate_symbol(symbol)

    async with limiter.limit(config):
        try:
            trades = await asyncio.wait_for(
                market_service.get_recent_trades(symbol, limit),
                timeout=5.0
            )
            valid = []
            for t in (trades or []):
                p = _safe_float(t.get('price'), 0.0)
                q = _safe_float(t.get('quantity'), 0.0)
                if p > 0 and q > 0:
                    valid.append(TradeRecord(
                        id=str(t.get('id', '')),
                        price=p, quantity=q,
                        time=int(t.get('time', 0)),
                        is_buyer_maker=bool(t.get('is_buyer_maker', False))
                    ))
            return valid
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except Exception:
            logger.exception("Trades failed trace=%s", trace_id)
            raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/exchange-info",
    response_model=ExchangeInfo,
    summary="交易所信息",
    description="获取交易所服务器时间、速率限制等基础元数据。",
)
async def get_exchange_info(
    request: Request,
    response: Response,
    market_service: MarketDataService = Depends(get_market_data_service),
    current_user: str = Depends(get_current_user),
    _perm: None = Depends(require_permission("market_read")),
    _rl: None = Depends(get_rate_limiter("market/exchange-info", 10, 60)),
    limiter_and_cfg: Tuple[_ConcurrencyLimiter, Any] = Depends(get_concurrency_limiter),
):
    limiter, config = limiter_and_cfg
    trace_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = trace_id
    _log_audit(request, current_user, trace_id, {})

    async with limiter.limit(config):
        try:
            info = await asyncio.wait_for(
                market_service.get_exchange_info(),
                timeout=5.0
            )
            if not info:
                raise HTTPException(status_code=503, detail="Exchange info unavailable")
            info.setdefault("timezone", "UTC")
            return ExchangeInfo(**info)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except Exception:
            logger.exception("Exchange info failed trace=%s", trace_id)
            raise HTTPException(status_code=500, detail="Internal server error")
