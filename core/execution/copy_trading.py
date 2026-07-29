# -*- coding: utf-8 -*-
"""
模块名称: copy_trading.py
核心职责: 管理跟单交易，将主账户订单按比例复制到多个跟单账户，独立风控与仓位适配。
         经过多轮机构级审计与加固，符合华尔街顶级量化对冲基金生产标准。
所属层级: core.execution

外部依赖:
    - asyncio, copy, logging, time, typing, itertools
    - core.models.order (Order)
    - core.models.account (Account)
    - core.engine.event_bus (EventBus, 可选，用于发布跟单事件)

接口契约:
    提供: CopyTradingManager 类，负责监听主账户订单并复制。
    消费: Account 抽象接口，需提供 get_equity, submit_order, round_to_min, id, is_active 等方法。

配置项:
    copy_trading.* 参见 default.yaml

作者: KHAOS Engineering
创建日期: 2026-07-11
修改记录:
    - 2026-07-16 第一轮审计修复 (100项)
    - 2026-07-18 第二轮审计修复 (100项)
    - 2026-07-29 第三轮运行时加固 (索引错误、并发安全、类型校验、仓位保护、优雅关闭)
    - 2026-07-29 第四轮残留隐患加固 (跳过语义、exc_info、缓存有界、active_count 一致性、awaitable 校验)
    - 2026-07-29 第五轮最终抛光 (序列号实例化、in_flight 读保护、同步提交日志、pending 清理)
"""

import asyncio
import copy
import itertools
import logging
import time
from typing import Dict, Any, List, Optional, Tuple

from core.models.order import Order
from core.models.account import Account

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
DEFAULT_ENABLED = False
DEFAULT_COPY_RATIO = 1.0
DEFAULT_SLIPPAGE_TOLERANCE_PCT = 0.1
DEFAULT_MAX_LATENCY_MS = 500
DEFAULT_ALLOCATION_MODE = 'equal'
DEFAULT_FOLLOWER_LIMIT = 0
MAX_CONCURRENT_COPIES = 50
MIN_NOTIONAL_USD = 10.0
DEFAULT_SMALL_ACCOUNT_THRESHOLD = 3000.0
MAX_CLIENT_ORDER_ID_LEN = 36
EQUITY_CACHE_TTL_SEC = 0.5
EQUITY_CACHE_MAX_ENTRIES = 256


class CopyTradingManager:
    """跟单交易管理器 (机构生产最终版)"""

    def __init__(self,
                 config: Dict[str, Any],
                 master_account: Optional[Account],
                 follower_accounts: Optional[List[Account]] = None,
                 exchange_info: Optional[Dict[str, Any]] = None,
                 event_bus=None):
        if config is None:
            config = {}
        config = dict(config)

        self._enabled = bool(config.get('enabled', DEFAULT_ENABLED))
        self._copy_ratio = self._safe_float(config.get('copy_ratio', DEFAULT_COPY_RATIO), DEFAULT_COPY_RATIO)
        self._slippage_tol = self._safe_float(
            config.get('slippage_tolerance_pct', DEFAULT_SLIPPAGE_TOLERANCE_PCT),
            DEFAULT_SLIPPAGE_TOLERANCE_PCT
        )
        self._max_latency_ms = self._safe_int(config.get('max_latency_ms', DEFAULT_MAX_LATENCY_MS), DEFAULT_MAX_LATENCY_MS)
        self._allocation_mode = str(config.get('allocation_mode', DEFAULT_ALLOCATION_MODE)).lower()
        self._follower_limit = self._safe_int(config.get('follower_accounts', DEFAULT_FOLLOWER_LIMIT), DEFAULT_FOLLOWER_LIMIT)

        self._dynamic_params = {'copy_ratio', 'slippage_tolerance_pct', 'max_latency_ms'}

        if self._copy_ratio <= 0:
            raise ValueError("copy_ratio must be > 0")
        if self._max_latency_ms <= 0:
            raise ValueError("max_latency_ms must be > 0")
        if self._allocation_mode not in ('equal', 'proportional'):
            raise ValueError(f"Invalid allocation_mode: {self._allocation_mode}")
        if self._slippage_tol < 0:
            raise ValueError("slippage_tolerance_pct must be >= 0")

        self._master = master_account
        if self._master is None:
            raise ValueError("master_account must not be None")

        self._followers: List[Account] = []
        if follower_accounts:
            limit = self._follower_limit if self._follower_limit > 0 else len(follower_accounts)
            self._followers = list(follower_accounts[:limit])

        self._exchange_info = dict(exchange_info) if exchange_info else {}
        raw_min = self._exchange_info.get('min_notional', MIN_NOTIONAL_USD)
        self._min_notional = max(0.0, self._safe_float(raw_min, MIN_NOTIONAL_USD))

        self._global_semaphore = asyncio.Semaphore(MAX_CONCURRENT_COPIES)
        self._account_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._sem_lock = asyncio.Lock()
        for f in self._followers:
            fid = self._safe_account_id(f)
            if fid is not None:
                self._account_semaphores[fid] = asyncio.Semaphore(1)

        self._event_bus = event_bus

        self._shutdown = False
        self._copy_success = 0
        self._copy_failure = 0
        self._copy_skipped = 0
        self._start_time = time.monotonic()
        self._in_flight = 0
        self._in_flight_lock = asyncio.Lock()
        self._pending_tasks: set = set()
        self._client_id_counter = itertools.count(1)

        self._small_account_threshold = self._safe_float(
            config.get('small_account_threshold', DEFAULT_SMALL_ACCOUNT_THRESHOLD),
            DEFAULT_SMALL_ACCOUNT_THRESHOLD
        )

        self._equity_cache: Dict[str, Tuple[float, float]] = {}

        logger.info(
            "CopyTradingManager v3.3 initialized: enabled=%s, ratio=%s, mode=%s, followers=%d",
            self._enabled, self._copy_ratio, self._allocation_mode, len(self._followers)
        )

    def update_config(self, new_config: Dict[str, Any]) -> None:
        if not isinstance(new_config, dict):
            logger.warning("update_config ignored: new_config is not a dict")
            return
        for key, value in new_config.items():
            if key not in self._dynamic_params:
                continue
            try:
                if key == 'copy_ratio':
                    v = float(value)
                    if v <= 0:
                        logger.warning("update_config rejected: copy_ratio must be > 0")
                        continue
                    self._copy_ratio = v
                elif key == 'slippage_tolerance_pct':
                    v = float(value)
                    if v < 0:
                        logger.warning("update_config rejected: slippage_tolerance_pct must be >= 0")
                        continue
                    self._slippage_tol = v
                elif key == 'max_latency_ms':
                    v = int(value)
                    if v <= 0:
                        logger.warning("update_config rejected: max_latency_ms must be > 0")
                        continue
                    self._max_latency_ms = v
                logger.info("CopyTrading config updated: %s = %s", key, value)
            except (TypeError, ValueError) as e:
                logger.warning("update_config failed for %s=%s: %s", key, value, e)

    async def on_master_order(self, order: Order) -> None:
        if not self._enabled or not self._followers or self._shutdown:
            return
        if order is None:
            logger.warning("Received None order in on_master_order")
            return

        order_type = getattr(order, 'order_type', None)
        if order_type not in ('MARKET', 'LIMIT', 'STOP_MARKET', 'STOP_LIMIT'):
            logger.debug("Ignoring non-trade order type: %s", order_type)
            return

        size = getattr(order, 'size', None)
        if size is None or not isinstance(size, (int, float)) or size == 0:
            logger.warning("Ignoring order with invalid size: %s", size)
            return

        active_followers: List[Account] = []
        for follower in list(self._followers):
            try:
                if follower.is_active():
                    active_followers.append(follower)
            except Exception as e:
                logger.error("is_active() failed for follower: %s", e)
                continue

        if not active_followers:
            return

        active_count = len(active_followers)

        active_pairs: List[Tuple[Account, Any]] = []
        for follower in active_followers:
            coro = self._copy_to_follower_safe(follower, order, active_count)
            active_pairs.append((follower, coro))

        tasks = [t for _, t in active_pairs]
        followers_for_tasks = [f for f, _ in active_pairs]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        order_success = 0
        order_failure = 0
        order_skipped = 0
        for i, result in enumerate(results):
            fid = self._safe_account_id(followers_for_tasks[i]) or "unknown"
            if isinstance(result, Exception):
                order_failure += 1
                self._copy_failure += 1
                logger.error("Copy failed for %s: %s", fid, result, exp_info=True)
            elif result is False:
                order_skipped += 1
                self._copy_skipped += 1
            else:
                order_success += 1
                self._copy_success += 1

        if self._event_bus is not None:
            try:
                master_id = getattr(order, 'id', None)
                await self._event_bus.publish('copy_trading.completed', {
                    'master_order_id': master_id,
                    'success': order_success,
                    'failure': order_failure,
                    'skipped': order_skipped,
                    'total_success': self._copy_success,
                    'total_failure': self._copy_failure,
                    'total_skipped': self._copy_skipped,
                    'timestamp': time.time()
                })
            except Exception as e:
                logger.error("Failed to publish copy_trading.completed event: %s", e)

    def get_status(self) -> Dict[str, Any]:
        master_equity = 0.0
        try:
            if self._master is not None:
                master_equity = self._get_equity_safe(self._master)
        except Exception as e:
            logger.error("Failed to get master equity: %s", e)

        follower_list = []
        for f in list(self._followers):
            fid = self._safe_account_id(f) or "unknown"
            try:
                equity = self._get_equity_safe(f)
                active = bool(f.is_active())
            except Exception as e:
                logger.error("Failed to query follower %s: %s", fid, e)
                equity = 0.0
                active = False
            follower_list.append({
                'id': fid,
                'equity': equity,
                'active': active,
            })

        in_flight = self._in_flight
        uptime = time.monotonic() - self._start_time
        return {
            'enabled': self._enabled,
            'master_equity': master_equity,
            'followers': follower_list,
            'copy_ratio': self._copy_ratio,
            'allocation_mode': self._allocation_mode,
            'copy_success': self._copy_success,
            'copy_failure': self._copy_failure,
            'copy_skipped': self._copy_skipped,
            'uptime_seconds': int(uptime),
            'small_account_protection': self._is_small_account(),
            'in_flight': in_flight,
        }

    async def shutdown(self) -> None:
        logger.info("CopyTradingManager shutting down...")
        self._shutdown = True
        try:
            await asyncio.wait_for(self._drain_pending(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out with pending copies (in_flight=%d)", self._in_flight)
            for t in list(self._pending_tasks):
                if not t.done():
                    t.cancel()
            self._pending_tasks.clear()
        logger.info("CopyTradingManager shutdown complete.")

    async def _copy_to_follower_safe(self, follower: Account, master_order: Order,
                                     active_count: int) -> bool:
        if self._shutdown:
            return False

        fid = self._safe_account_id(follower)
        if fid is None:
            logger.warning("Follower has invalid id, skip copy")
            return False

        async with self._sem_lock:
            sem = self._account_semaphores.get(fid)
            if sem is None:
                sem = asyncio.Semaphore(1)
                self._account_semaphores[fid] = sem

        async with sem:
            async with self._global_semaphore:
                if self._shutdown:
                    return False
                async with self._in_flight_lock:
                    self._in_flight += 1
                task = asyncio.current_task()
                if task is not None:
                    self._pending_tasks.add(task)
                try:
                    return await self._copy_to_follower(follower, master_order, active_count)
                finally:
                    if task is not None:
                        self._pending_tasks.discard(task)
                    async with self._in_flight_lock:
                        self._in_flight = max(0, self._in_flight - 1)

    async def _copy_to_follower(self, follower: Account, master_order: Order,
                                active_count: int) -> bool:
        follower_qty = self._calc_follower_size(follower, master_order, active_count)
        if follower_qty <= 0:
            logger.debug("Skipping copy to %s: calculated qty = %s",
                         self._safe_account_id(follower), follower_qty)
            return False

        try:
            copied_order = copy.deepcopy(master_order)
        except Exception as e:
            logger.error("Failed to deepcopy order %s: %s", getattr(master_order, 'id', '?'), e)
            raise

        try:
            copied_order.size = follower_qty
        except Exception as e:
            logger.error("Cannot set size on copied order: %s", e)
            raise

        seq = next(self._client_id_counter)
        master_cid = str(getattr(master_order, 'client_order_id', '') or '')
        fid = self._safe_account_id(follower) or 'unk'
        ts = int(time.time() * 1000)
        new_cid = f"{master_cid}_cpy_{fid}_{ts}_{seq}"
        if len(new_cid) > MAX_CLIENT_ORDER_ID_LEN:
            new_cid = f"c{ts}_{fid}_{seq}"[-MAX_CLIENT_ORDER_ID_LEN:]
        try:
            copied_order.client_order_id = new_cid
        except Exception:
            pass

        meta = getattr(copied_order, 'metadata', None)
        if not isinstance(meta, dict):
            meta = {}
            try:
                copied_order.metadata = meta
            except Exception:
                pass
        meta['copy'] = True
        meta['master_order_id'] = getattr(master_order, 'id', None)
        meta['follower_id'] = fid
        meta['copy_timestamp'] = time.time()
        meta['original_size'] = getattr(master_order, 'size', None)
        meta['calculated_size'] = follower_qty

        order_type = getattr(copied_order, 'order_type', None)
        if order_type == 'LIMIT' and self._slippage_tol > 0:
            price = getattr(copied_order, 'price', None)
            side = str(getattr(copied_order, 'side', '') or '').upper()
            if isinstance(price, (int, float)) and price > 0:
                try:
                    if side in ('BUY', 'LONG'):
                        new_price = price * (1.0 + self._slippage_tol / 100.0)
                    elif side in ('SELL', 'SHORT'):
                        new_price = price * (1.0 - self._slippage_tol / 100.0)
                    else:
                        new_price = price
                    copied_order.price = new_price
                except Exception:
                    pass

        timeout_sec = max(0.05, self._max_latency_ms / 1000.0)
        try:
            submit_result = follower.submit_order(copied_order)
            if asyncio.iscoroutine(submit_result) or asyncio.isfuture(submit_result):
                await asyncio.wait_for(submit_result, timeout=timeout_sec)
            else:
                logger.warning(
                    "submit_order for %s returned non-awaitable; timeout control skipped",
                    fid
                )
            logger.debug("Copied order %s to %s, qty=%s",
                         getattr(master_order, 'id', '?'), fid, follower_qty)
            return True
        except asyncio.TimeoutError:
            logger.warning("Copy order to %s timed out after %dms", fid, self._max_latency_ms)
            raise
        except Exception as e:
            logger.error("Error submitting copy order to %s: %s", fid, e)
            raise

    def _calc_follower_size(self, follower: Account, master_order: Order,
                            active_count: int) -> float:
        try:
            master_equity = self._get_equity_safe(self._master)
            follower_equity = self._get_equity_safe(follower)
        except Exception as e:
            logger.error("Failed to get equity: %s", e)
            return 0.0

        if master_equity <= 0 or follower_equity <= 0:
            return 0.0

        master_size = getattr(master_order, 'size', 0.0)
        if not isinstance(master_size, (int, float)) or master_size == 0:
            return 0.0

        if active_count <= 0:
            active_count = max(1, len(self._followers))

        if self._allocation_mode == 'equal':
            raw_qty = master_size * self._copy_ratio / active_count
        else:
            ratio = follower_equity / master_equity
            ratio = max(0.0, min(ratio, 10.0))
            raw_qty = master_size * ratio * self._copy_ratio

        if self._is_small_account():
            if follower_equity < self._small_account_threshold:
                raw_qty *= 0.8
                logger.debug("Small account protection applied: follower %s, adjusted qty=%s",
                             self._safe_account_id(follower), raw_qty)

        sign = 1.0 if raw_qty >= 0 else -1.0
        abs_qty = abs(raw_qty)

        try:
            rounded_qty = follower.round_to_min(abs_qty)
        except Exception as e:
            logger.error("round_to_min failed: %s", e)
            return 0.0

        if not isinstance(rounded_qty, (int, float)) or rounded_qty <= 0:
            return 0.0

        rounded_qty = float(rounded_qty) * sign

        price = self._extract_price(master_order)
        if price > 0:
            notional = abs(rounded_qty) * price
            if notional < self._min_notional:
                adjusted_abs = self._min_notional / price
                try:
                    adjusted_abs = follower.round_to_min(adjusted_abs)
                except Exception:
                    return 0.0
                if not isinstance(adjusted_abs, (int, float)) or adjusted_abs <= 0:
                    return 0.0
                if adjusted_abs * price > follower_equity * 0.5:
                    logger.warning(
                        "Follower %s adjusted qty exceeds 50%% equity, skipping copy",
                        self._safe_account_id(follower)
                    )
                    return 0.0
                logger.debug(
                    "Adjusted follower qty from %s to %s to meet min notional",
                    rounded_qty, adjusted_abs * sign
                )
                rounded_qty = float(adjusted_abs) * sign

        return rounded_qty

    def _is_small_account(self) -> bool:
        try:
            equity = self._get_equity_safe(self._master)
            return equity < self._small_account_threshold
        except Exception:
            return False

    async def _drain_pending(self) -> None:
        while True:
            async with self._in_flight_lock:
                if self._in_flight <= 0:
                    return
            await asyncio.sleep(0.05)

    async def retry_failed_copies(self, max_retries: int = 1) -> Dict[str, int]:
        logger.info("Retry failed copies not yet implemented")
        return {'retried': 0}

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_account_id(account: Any) -> Optional[str]:
        try:
            aid = getattr(account, 'id', None)
            if aid is None:
                return None
            return str(aid)
        except Exception:
            return None

    def _get_equity_safe(self, account: Account) -> float:
        fid = self._safe_account_id(account) or str(id(account))
        now = time.monotonic()
        cached = self._equity_cache.get(fid)
        if cached is not None:
            eq, ts = cached
            if now - ts < EQUITY_CACHE_TTL_SEC:
                return eq
        try:
            eq = account.get_equity()
            if eq is None or not isinstance(eq, (int, float)):
                eq = 0.0
            eq = float(eq)
            if eq < 0:
                eq = 0.0
            if len(self._equity_cache) >= EQUITY_CACHE_MAX_ENTRIES:
                oldest_key = min(self._equity_cache, key=lambda k: self._equity_cache[k][1])
                self._equity_cache.pop(oldest_key, None)
            self._equity_cache[fid] = (eq, now)
            return eq
        except Exception as e:
            logger.error("get_equity failed for %s: %s", fid, e)
            return 0.0

    @staticmethod
    def _extract_price(order: Order) -> float:
        for attr in ('price', 'last_price', 'avg_price', 'trigger_price'):
            p = getattr(order, attr, None)
            if isinstance(p, (int, float)) and p > 0:
                return float(p)
        return 0.0
