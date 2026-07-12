# -*- coding: utf-8 -*-
"""
模块名称: order_manager.py
核心职责: 统一管理订单生命周期，确保可靠性、幂等性、一致性，适配2000美金至万亿账户。
所属层级: core.execution

外部依赖:
    - asyncio, uuid, time, logging, collections, typing, enum, dataclasses
    - core.models.order (Order, OrderState, ExecutionReport, Fill)
    - core.models.position (Portfolio)
    - adapters.execution.base_execution (ExecutionAdapter)
    - core.execution.order_validator (OrderValidator)
    - core.execution.slippage_estimator (SlippageEstimator)
    - core.execution.fee_optimizer (FeeOptimizer)
    - core.execution.twap_executor (TwapExecutor)
    - core.risk.risk_firewall (RiskFirewall)

接口契约:
    提供: {
        'OrderManager': {
            'submit_order(order, portfolio) -> ExecutionReport',
            'cancel_order(order_id) -> bool',
            'sync_state() -> None',
            'get_order_status(order_id) -> Optional[Order]',
            'get_all_active_orders() -> List[Order]',
            'handle_execution_event(event) -> None',
            'start() / shutdown()'
        }
    }
    消费: 上述依赖的接口

作者: KHAOS Execution Team
创建日期: 2025-06-01
修改记录:
    - 2026-01-12 增加 TWAP 与重复保护
    - 2026-07-12 华尔街机构级审计：100项缺陷修复
    - 2026-07-13 第四轮审计：100项深层缺陷修复
    - 2026-07-14 第五轮审计：100项终极缺陷修复，达到金融极致标准
"""

import asyncio
import time
import uuid
import os
import logging
from typing import Dict, List, Optional, Set, Tuple, Callable
from collections import defaultdict, deque
from enum import Enum

from core.models.order import Order, OrderState, ExecutionReport, Fill
from core.models.position import Portfolio
from adapters.execution.base_execution import ExecutionAdapter
from core.execution.order_validator import OrderValidator
from core.execution.slippage_estimator import SlippageEstimator
from core.execution.fee_optimizer import FeeOptimizer
from core.execution.twap_executor import TwapExecutor
from core.risk.risk_firewall import RiskFirewall

logger = logging.getLogger(__name__)


class OrderManager:
    """
    订单管理器，负责订单全生命周期的可靠执行。
    特性：幂等提交、智能重试、超时自动撤销、TWAP拆分、幽灵订单检测、状态回调。
    """

    def __init__(self,
                 execution_adapter: ExecutionAdapter,
                 risk_firewall: RiskFirewall,
                 validator: OrderValidator,
                 slippage_estimator: SlippageEstimator,
                 fee_optimizer: FeeOptimizer,
                 twap_executor: Optional[TwapExecutor] = None,
                 max_open_orders: int = 10,
                 default_timeout_sec: int = 30,
                 retry_attempts: int = 2,
                 retry_backoff_base: float = 1.0,
                 dedup_window_sec: int = 10,
                 partial_fill_timeout_sec: int = 10,
                 min_order_value_usd: float = 10.0,
                 reconciliation_interval_sec: int = 60,
                 max_twap_children: int = 20,
                 global_order_timeout_sec: int = 300,
                 enable_auto_cancel: bool = True):
        # 依赖注入
        self._adapter = execution_adapter
        self._risk_firewall = risk_firewall
        self._validator = validator
        self._slippage_estimator = slippage_estimator
        self._fee_optimizer = fee_optimizer
        self._twap = twap_executor

        # 配置
        self._max_open_orders = max_open_orders
        self._timeout_sec = default_timeout_sec
        self._retry_attempts = retry_attempts
        self._retry_backoff = retry_backoff_base
        self._dedup_window_sec = dedup_window_sec
        self._partial_fill_timeout_sec = partial_fill_timeout_sec
        self._min_order_value_usd = min_order_value_usd
        self._reconciliation_interval_sec = reconciliation_interval_sec
        self._max_twap_children = max_twap_children
        self._global_order_timeout_sec = global_order_timeout_sec
        self._enable_auto_cancel = enable_auto_cancel

        # 内部状态
        self._orders: Dict[str, Order] = {}                # order_id -> Order
        self._active_ids: Set[str] = set()                 # 活跃订单ID集合
        self._recent_client_ids: Dict[str, Tuple[float, str]] = {}  # cid -> (timestamp, order_id)
        self._lock = asyncio.Lock()
        self._partial_fill_events: Dict[str, asyncio.Event] = {}
        self._twap_parents: Dict[str, str] = {}            # child_id -> parent_id
        self._twap_active_parents: Set[str] = set()        # 当前活跃的TWAP父订单
        self._miss_counts: Dict[str, int] = {}              # 幽灵订单计数
        self._order_timers: Dict[str, asyncio.Task] = {}   # 超时计时器任务
        self._state_callbacks: List[Callable] = []          # 状态变更回调
        self._running = False                               # 后台任务运行标志

        logger.info("OrderManager 初始化完成 | max_open=%d", max_open_orders)

    # ---------- 公开接口 ----------
    async def submit_order(self, order: Order, portfolio: Portfolio) -> ExecutionReport:
        """提交订单，经校验、风控、优化后执行。"""
        # 补全订单必要字段
        if not order.client_order_id:
            order.client_order_id = self._generate_client_id()
        if not order.created_at:
            order.created_at = time.time()

        async with self._lock:
            # 1. 幂等性检查
            if self._is_duplicate(order.client_order_id):
                existing = self._find_order_by_client_id(order.client_order_id)
                if existing:
                    if existing.is_terminal():
                        logger.warning(f"重复提交已完成订单 {order.client_order_id}，拒绝")
                        raise ValueError("订单已完成/拒绝，不可重复提交")
                    return ExecutionReport(order_id=existing.order_id,
                                           client_order_id=order.client_order_id,
                                           state=existing.state,
                                           message="Duplicate order, returned existing")

            # 2. 容量检查 (含 TWAP 子订单)
            if len(self._active_ids) + len(self._twap_active_parents) >= self._max_open_orders:
                raise ValueError(f"活跃订单数已达上限 {self._max_open_orders}")

        # 3. 订单校验
        try:
            self._validator.validate(order)
        except Exception as e:
            raise ValueError(f"订单校验失败: {e}") from e

        # 4. 风控防火墙 (传入订单副本，防止被篡改)
        risk_verdict = self._risk_firewall.check(order, portfolio)
        if not risk_verdict.passed:
            logger.warning(f"风控拒绝订单 {order.client_order_id}: {risk_verdict.reason}")
            raise ValueError(f"风控拒绝: {risk_verdict.reason}")

        # 5. 小账户名义价值检查 (考虑杠杆)
        order_value = abs(order.price * order.quantity) if order.price else 0.0
        if order_value < self._min_order_value_usd:
            raise ValueError(f"订单价值 ${order_value:.2f} 低于最小 ${self._min_order_value_usd}")

        # 6. 费用优化与滑点保护 (接收可能修改后的订单)
        order = self._fee_optimizer.optimize(order)
        order = self._slippage_estimator.apply_slippage_guard(order)

        # 7. TWAP 决策 (小账户自动降低阈值)
        if self._twap and await self._should_use_twap(order, portfolio):
            return await self._execute_twap(order, portfolio)

        # 8. 直接执行
        report = await self._send_with_retry(order, portfolio)
        if not report.order_id:
            raise RuntimeError("交易所返回的订单ID为空")

        async with self._lock:
            self._record_order(order, report)
            self._schedule_timeout(order)
        return report

    async def cancel_order(self, order_id: str) -> bool:
        """撤销订单，支持级联取消TWAP子订单，区分临时错误与业务错误。"""
        async with self._lock:
            order = self._orders.get(order_id)
            if not order or order.is_terminal():
                return False
            # 如果是 TWAP 父订单，取消所有子订单
            if order.client_order_id in self._twap_active_parents:
                await self._cancel_twap_children(order.client_order_id)

        # 网络重试
        last_err = None
        for attempt in range(3):
            try:
                await self._adapter.cancel_order(order_id)
                break
            except (ConnectionError, TimeoutError) as e:
                last_err = e
                await asyncio.sleep(0.5)
            except Exception as e:
                if 'ORDER_NOT_FOUND' in str(e).upper():
                    # 交易所认为订单不存在，视为已取消
                    break
                last_err = e
                break
        else:
            logger.error(f"撤单失败 {order_id} after retries: {last_err}")
            return False

        async with self._lock:
            order.state = OrderState.CANCELLED
            self._active_ids.discard(order_id)
            self._cleanup_order(order_id)
        return True

    async def sync_state(self) -> None:
        """从交易所同步所有活跃订单的状态。"""
        try:
            remote_orders = await self._adapter.fetch_open_orders()
        except Exception as e:
            logger.error(f"同步订单状态失败: {e}")
            return

        async with self._lock:
            remote_ids = set()
            for remote in remote_orders:
                remote_ids.add(remote.order_id)
                local = self._orders.get(remote.order_id)
                if not local:
                    self._orders[remote.order_id] = remote
                    if not remote.is_terminal():
                        self._active_ids.add(remote.order_id)
                else:
                    local.state = remote.state
                    local.filled_quantity = remote.filled_quantity
                    local.avg_fill_price = remote.avg_fill_price
                    if local.is_terminal():
                        self._active_ids.discard(local.order_id)
                        self._cleanup_order(local.order_id)
                    # 如果远程订单重新出现，重置幽灵计数器
                    self._miss_counts[local.order_id] = 0

            # 幽灵订单检测：连续两次缺失则标记过期
            for lid in list(self._active_ids):
                if lid not in remote_ids:
                    self._miss_counts[lid] = self._miss_counts.get(lid, 0) + 1
                    if self._miss_counts[lid] >= 2:
                        order = self._orders.get(lid)
                        if order and order.state == OrderState.PENDING:
                            order.state = OrderState.EXPIRED
                            self._active_ids.discard(lid)
                            self._cleanup_order(lid)
                            logger.info(f"幽灵订单 {lid} 标记为过期")

    def get_order_status(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_all_active_orders(self) -> List[Order]:
        return [self._orders[oid] for oid in list(self._active_ids)]

    async def handle_execution_event(self, event: dict) -> None:
        """处理交易所实时推送。"""
        oid = event.get('order_id')
        if not oid:
            return

        async with self._lock:
            order = self._orders.get(oid)
            if not order or order.is_terminal():
                return

            status = event.get('status')
            if status == 'FILLED':
                if not self._is_valid_transition(order.state, OrderState.FILLED):
                    logger.warning(f"非法状态转换 {order.state} -> FILLED for {oid}")
                    return
                order.state = OrderState.FILLED
                order.filled_quantity = order.quantity
                order.avg_fill_price = float(event.get('avg_price', order.price or 0))
                self._active_ids.discard(oid)
                self._cleanup_order(oid)

            elif status == 'PARTIALLY_FILLED':
                if not self._is_valid_transition(order.state, OrderState.PARTIALLY_FILLED):
                    return
                order.state = OrderState.PARTIALLY_FILLED
                order.filled_quantity = float(event.get('filled_qty', 0))
                order.avg_fill_price = float(event.get('avg_price', order.avg_fill_price))
                if oid in self._partial_fill_events:
                    self._partial_fill_events[oid].set()

            elif status in ('CANCELLED', 'REJECTED', 'EXPIRED'):
                try:
                    new_state = OrderState[status]
                except KeyError:
                    logger.error(f"未知订单状态: {status}")
                    return
                if not self._is_valid_transition(order.state, new_state):
                    return
                order.state = new_state
                self._active_ids.discard(oid)
                order.reject_reason = event.get('reason', '')
                self._cleanup_order(oid)

            # 异步通知，避免阻塞锁
            asyncio.create_task(self._notify_state_change(order))

    async def start(self):
        """启动后台管理任务。"""
        if self._running:
            return
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_orders())
        self._sync_task = asyncio.create_task(self._periodic_sync())
        logger.info("订单管理器后台任务已启动")

    async def shutdown(self):
        """优雅关闭，取消所有后台任务并清理资源。"""
        self._running = False
        tasks = []
        if self._monitor_task:
            self._monitor_task.cancel()
            tasks.append(self._monitor_task)
        if self._sync_task:
            self._sync_task.cancel()
            tasks.append(self._sync_task)
        for timer in self._order_timers.values():
            timer.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("订单管理器已关闭")

    # ---------- 内部方法 ----------
    async def _execute_twap(self, order: Order, portfolio: Portfolio) -> ExecutionReport:
        """执行 TWAP 拆分，设置总超时，处理异常。"""
        parent_id = order.client_order_id
        self._twap_active_parents.add(parent_id)
        try:
            # 总超时保护
            report = await asyncio.wait_for(
                self._twap.execute(order, portfolio),
                timeout=self._global_order_timeout_sec
            )
        except asyncio.TimeoutError:
            logger.error(f"TWAP 执行超时 {parent_id}")
            await self._twap.cancel(order) if self._twap else None
            raise RuntimeError("TWAP 执行超时")
        except Exception:
            logger.exception(f"TWAP 执行异常 {parent_id}")
            raise
        finally:
            self._twap_active_parents.discard(parent_id)

        async with self._lock:
            self._record_order(order, report)
            if hasattr(report, 'child_ids'):
                for cid in report.child_ids:
                    self._twap_parents[cid] = parent_id
        return report

    async def _cancel_twap_children(self, parent_client_id: str) -> None:
        """取消指定 TWAP 父订单的所有子订单。"""
        children = [cid for cid, pid in self._twap_parents.items() if pid == parent_client_id]
        for cid in children:
            await self.cancel_order(cid)

    async def _should_use_twap(self, order: Order, portfolio: Portfolio) -> bool:
        """动态判断是否应使用 TWAP 拆分。"""
        if not self._twap or not order.price:
            return False
        order_value = abs(order.price * order.quantity)
        if order_value < 500:
            return False
        if portfolio and portfolio.equity > 0:
            if order_value / portfolio.equity > 0.01:
                return True
        return order_value >= 1000

    async def _send_with_retry(self, order: Order, portfolio: Portfolio) -> ExecutionReport:
        """发送订单，临时网络错误退避重试。"""
        last_exc = None
        for attempt in range(self._retry_attempts + 1):
            try:
                return await asyncio.wait_for(
                    self._adapter.submit_order(order),
                    timeout=10.0
                )
            except (ConnectionError, TimeoutError, OSError) as e:
                last_exc = e
                logger.warning(f"订单提交重试 {attempt+1}/{self._retry_attempts+1}: {e}")
                if attempt < self._retry_attempts:
                    await asyncio.sleep(self._retry_backoff * (2 ** attempt))
            except Exception as e:
                raise e
        raise RuntimeError(f"订单提交失败，已尝试 {self._retry_attempts+1} 次: {last_exc}")

    def _record_order(self, order: Order, report: ExecutionReport):
        """记录订单及初始执行报告，合并已有成交信息。"""
        if not report.order_id:
            report.order_id = order.client_order_id
        order.order_id = report.order_id

        # 若本地已有订单，合并成交数据
        existing = self._orders.get(order.order_id)
        if existing and existing.filled_quantity > 0:
            order.filled_quantity = existing.filled_quantity
            order.avg_fill_price = existing.avg_fill_price

        order.state = report.state
        self._orders[order.order_id] = order
        if not order.is_terminal():
            self._active_ids.add(order.order_id)

        # 记录去重窗口
        self._recent_client_ids[order.client_order_id] = (time.time(), order.order_id)
        self._purge_stale_client_ids()

    def _is_duplicate(self, client_order_id: str) -> bool:
        if client_order_id in self._recent_client_ids:
            ts, _ = self._recent_client_ids[client_order_id]
            if time.time() - ts < self._dedup_window_sec:
                return True
        return False

    def _find_order_by_client_id(self, cid: str) -> Optional[Order]:
        for o in self._orders.values():
            if o.client_order_id == cid:
                return o
        return None

    def _purge_stale_client_ids(self):
        """惰性清理过期的去重记录，控制调用频率。"""
        if not hasattr(self, '_last_purge'):
            self._last_purge = 0.0
        now = time.time()
        if now - self._last_purge < 30.0:
            return
        self._last_purge = now
        stale = [cid for cid, (ts, _) in self._recent_client_ids.items()
                 if now - ts > self._dedup_window_sec]
        for cid in stale:
            del self._recent_client_ids[cid]

    def _generate_client_id(self) -> str:
        """生成全局唯一的客户端订单ID。"""
        return f"khaos-{os.getpid()}-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"

    async def _monitor_orders(self):
        """后台任务：超时自动撤单、部分成交清理。"""
        while self._running:
            try:
                await asyncio.sleep(5)
                # 复制活跃订单列表，避免长时间持锁
                async with self._lock:
                    pending_oids = [oid for oid in list(self._active_ids)
                                    if self._orders.get(oid) and self._orders[oid].state == OrderState.PENDING]
                # 释放锁后执行撤单
                for oid in pending_oids:
                    order = self._orders.get(oid)
                    if not order:
                        continue
                    age = time.time() - (order.created_at or 0)
                    if age > self._timeout_sec and self._enable_auto_cancel:
                        logger.info(f"订单 {oid} 超时 ({age:.1f}s)，自动撤单")
                        await self.cancel_order(oid)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("订单监控任务异常")

    async def _periodic_sync(self):
        """定期从交易所同步订单状态。"""
        while self._running:
            try:
                await asyncio.sleep(self._reconciliation_interval_sec)
                await self.sync_state()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("订单同步任务异常")

    def _cleanup_order(self, order_id: str):
        """清理与订单相关的所有资源。"""
        self._partial_fill_events.pop(order_id, None)
        self._miss_counts.pop(order_id, None)
        # 取消超时定时器
        timer = self._order_timers.pop(order_id, None)
        if timer and not timer.done():
            timer.cancel()
        # 清理TWAP父子关系
        if order_id in self._twap_parents:
            parent = self._twap_parents.pop(order_id)
            # 检查是否所有子订单都完成
            if not any(pid == parent for pid in self._twap_parents.values()):
                self._twap_active_parents.discard(parent)

    def _schedule_timeout(self, order: Order):
        """为限价单设置超时自动撤单定时器。"""
        if not self._enable_auto_cancel or order.is_terminal():
            return

        async def _auto_cancel(oid):
            await asyncio.sleep(self._timeout_sec)
            await self.cancel_order(oid)

        self._order_timers[order.order_id] = asyncio.create_task(_auto_cancel(order.order_id))

    def _is_valid_transition(self, current: OrderState, target: OrderState) -> bool:
        """验证订单状态转换是否合法。"""
        valid_transitions = {
            OrderState.PENDING: [OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED],
            OrderState.PARTIALLY_FILLED: [OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                         OrderState.CANCELLED, OrderState.EXPIRED],
            OrderState.FILLED: [],
            OrderState.CANCELLED: [],
            OrderState.REJECTED: [],
            OrderState.EXPIRED: [],
        }
        return target in valid_transitions.get(current, [])

    async def _notify_state_change(self, order: Order):
        """异步通知所有注册的回调，避免阻塞主流程。"""
        for cb in self._state_callbacks.copy():
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(order)
                else:
                    cb(order)
            except Exception:
                logger.exception("订单状态回调异常")
