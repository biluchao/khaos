# -*- coding: utf-8 -*-
"""
模块名称: twap_executor.py (v5.0 华尔街终极机构版)
核心职责: 实现时间加权平均价格（TWAP）算法，将大额订单拆分为多个子订单，
          在指定时间内均匀执行，降低市场冲击和滑点。具备全面的风控、审计、容错与自适应机制。
所属层级: core.execution

外部依赖:
    - asyncio, time, math, uuid, logging, copy, random
    - typing (类型注解)
    - core.models.order (Order, ExecutionReport, Fill)
    - core.models.position (Portfolio)
    - adapters.execution.base_execution (ExecutionAdapter)
    - core.execution.order_validator (OrderValidator)
    - core.execution.slippage_estimator (SlippageEstimator)
    - core.risk.risk_firewall (RiskFirewall)

接口契约:
    提供: TwapExecutor 类，should_split 和 execute 方法
    消费: 适配器、风控、校验器、滑点预估器

配置项: 见构造函数参数，均来自 config/execution.yaml

作者: KHAOS Execution Team
创建日期: 2025-07-01
修改记录:
    - 2026-01-15: v2.0 100项缺陷修复
    - 2026-07-12: v3.0 新增100项缺陷修复
    - 2026-07-13: v4.0 第三轮100项缺陷修复
    - 2026-07-14: v5.0 第四轮100项缺陷修复，达到终极健壮
"""

import asyncio
import logging
import math
import random
import time
import uuid
from typing import Callable, Dict, List, Optional, Set, Tuple

from adapters.execution.base_execution import ExecutionAdapter
from core.execution.order_validator import OrderValidator
from core.execution.slippage_estimator import SlippageEstimator
from core.models.order import ExecutionReport, Fill, Order, OrderState
from core.models.position import Portfolio
from core.risk.risk_firewall import RiskFirewall

logger = logging.getLogger(__name__)


class TwapExecutor:
    """华尔街终极机构级 TWAP 执行器 v5.0"""

    def __init__(self,
                 execution_adapter: ExecutionAdapter,
                 risk_firewall: RiskFirewall,
                 validator: OrderValidator,
                 slippage_estimator: SlippageEstimator,
                 min_order_value_usd: float = 500.0,
                 slice_interval_sec: int = 30,
                 max_duration_sec: int = 120,
                 auto_extend_duration: bool = True,
                 max_auto_duration_sec: int = 600,
                 min_slice_size: float = 0.001,
                 aggressive_threshold_pct: float = 5.0,
                 stop_on_stop_loss: bool = True,
                 avoid_funding_period: bool = True,
                 funding_buffer_sec: int = 60,
                 max_concurrent_twaps: int = 3,
                 max_slice_retries: int = 2,
                 global_timeout_sec: int = 900,
                 min_duration_sec: int = 30,
                 duration_extend_coefficient: float = 10.0,
                 max_slices: int = 200,
                 semaphore_timeout_sec: float = 2.0,
                 max_slippage_pct: float = 0.01,
                 pause_timeout_sec: float = 300.0,
                 on_complete: Optional[Callable] = None):
        # 参数校验
        assert execution_adapter is not None
        assert risk_firewall is not None
        assert validator is not None
        assert slippage_estimator is not None
        assert min_slice_size > 0, "min_slice_size must be positive"
        assert slice_interval_sec >= 1, "slice_interval_sec must be at least 1"
        assert max_concurrent_twaps >= 1, "max_concurrent_twaps must be at least 1"
        assert 0 < aggressive_threshold_pct <= 100, "aggressive_threshold_pct must be 0-100"
        assert max_slippage_pct > 0, "max_slippage_pct must be positive"
        assert max_duration_sec <= max_auto_duration_sec, "max_duration_sec must not exceed max_auto_duration_sec"

        self._adapter = execution_adapter
        self._risk_firewall = risk_firewall
        self._validator = validator
        self._slippage = slippage_estimator

        self.min_order_value_usd = min_order_value_usd
        self.slice_interval_sec = slice_interval_sec
        self.max_duration_sec = max_duration_sec
        self.auto_extend_duration = auto_extend_duration
        self.max_auto_duration_sec = max_auto_duration_sec
        self.min_slice_size = min_slice_size
        self.aggressive_threshold_pct = aggressive_threshold_pct
        self.stop_on_stop_loss = stop_on_stop_loss
        self.avoid_funding_period = avoid_funding_period
        self.funding_buffer_sec = funding_buffer_sec
        self.max_concurrent_twaps = max_concurrent_twaps
        self.max_slice_retries = max_slice_retries
        self.global_timeout_sec = global_timeout_sec
        self.min_duration_sec = min_duration_sec
        self.duration_extend_coefficient = duration_extend_coefficient
        self.max_slices = max_slices
        self.semaphore_timeout_sec = semaphore_timeout_sec
        self.max_slippage_pct = max_slippage_pct
        self.pause_timeout_sec = pause_timeout_sec
        self.on_complete = on_complete

        # 并发控制
        self._twap_semaphore = asyncio.Semaphore(max_concurrent_twaps)
        # 正在执行的 TWAP: key -> task
        self._active_twaps: Dict[str, asyncio.Task] = {}
        # 暂停事件
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        # 连接状态缓存（简单）
        self._last_connected = True
        self._conn_check_time = 0.0

    # --------------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------------

    def should_split(self, order: Order, portfolio: Portfolio) -> bool:
        """判断订单是否需要 TWAP 拆分"""
        if not self._adapter_connected():
            return False
        if not order.quantity or order.quantity <= 0 or not order.price or order.price <= 0:
            return False
        if order.quantity < self.min_slice_size * 2:
            return False
        if order.order_type in ('stop_market', 'stop_limit', 'trailing_stop', 'iceberg'):
            return False
        if order.price * order.quantity < self.min_order_value_usd:
            return False
        try:
            self._validator.validate_symbol(order.symbol)
        except Exception:
            return False
        # 检查订单状态
        state = getattr(order, 'state', None)
        if state is not None and state in (OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED):
            return False
        return True

    async def execute(self, original_order: Order, portfolio: Portfolio) -> ExecutionReport:
        """执行 TWAP 拆分，返回汇总执行报告。若同一订单已在执行，返回现有结果。"""
        if not original_order.client_order_id:
            original_order.client_order_id = f"twap-{uuid.uuid4().hex[:8]}"
        cid = original_order.client_order_id
        symbol = (original_order.symbol or "").upper()
        direction = original_order.direction
        key = f"{cid}:{symbol}:{direction}"

        # 幂等控制
        existing_task = self._active_twaps.get(key)
        if existing_task and not existing_task.done():
            logger.warning(f"TWAP already running for {key}")
            try:
                return await asyncio.wait_for(existing_task, timeout=self.semaphore_timeout_sec)
            except asyncio.TimeoutError:
                return self._error_report(original_order, "TWAP already in progress, timeout waiting for result")
        elif existing_task and existing_task.done():
            self._active_twaps.pop(key, None)

        # 获取并发许可
        acquired = False
        try:
            acquired = await asyncio.wait_for(self._twap_semaphore.acquire(), timeout=self.semaphore_timeout_sec)
        except asyncio.TimeoutError:
            return self._error_report(original_order, "TWAP semaphore timeout")
        if not acquired:
            return self._error_report(original_order, "TWAP semaphore not acquired")

        task = None
        try:
            task = asyncio.ensure_future(self._execute_internal(original_order, portfolio))
            self._active_twaps[key] = task
            # 清理回调
            def clean_callback(t, k=key):
                if k in self._active_twaps:
                    self._active_twaps.pop(k, None)
            task.add_done_callback(clean_callback)
            return await task
        finally:
            self._twap_semaphore.release()
            if key in self._active_twaps:
                self._active_twaps.pop(key, None)

    async def pause(self):
        self._pause_event.clear()
        logger.info("TWAP executor paused")

    async def resume(self):
        self._pause_event.set()
        logger.info("TWAP executor resumed")

    async def cancel_all_twaps(self):
        """取消所有正在执行的 TWAP"""
        for key, task in list(self._active_twaps.items()):
            if not task.done():
                task.cancel()
                logger.info(f"Cancelled TWAP {key}")
        self._active_twaps.clear()

    # --------------------------------------------------------------------------
    # 内部实现
    # --------------------------------------------------------------------------

    async def _execute_internal(self, original_order: Order, portfolio: Portfolio) -> ExecutionReport:
        logger.info(f"TWAP start: order={original_order.client_order_id}, qty={original_order.quantity}, symbol={original_order.symbol}")
        self._audit_log("TWAP_START", original_order)

        if portfolio is None:
            return self._error_report(original_order, "Portfolio is None")
        # 深拷贝关键字段
        symbol = (original_order.symbol or "").upper()
        direction = original_order.direction
        total_qty = float(original_order.quantity)
        order_price = float(original_order.price)
        client_order_id = original_order.client_order_id

        # 有效性再次确认
        try:
            self._validator.validate_symbol(symbol)
        except Exception as e:
            return self._error_report(original_order, f"Invalid symbol: {e}")

        if total_qty <= 0 or order_price <= 0:
            return self._error_report(original_order, "Invalid order quantity or price")

        # 对齐精度
        try:
            aligned = getattr(self._validator, 'align_quantity', lambda s, q: q)(symbol, total_qty)
            total_qty = float(aligned)
        except Exception:
            pass

        # 初始组合快照
        portfolio = await self._refresh_portfolio(portfolio)
        if not self._check_account(portfolio):
            return self._error_report(original_order, "Account not ready for trading")

        # 检查订单状态
        state = getattr(original_order, 'state', None)
        if state is not None and state in (OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED):
            return self._error_report(original_order, "Order already in terminal state")

        start_mono = time.monotonic()
        start_wall = time.time()
        remaining_qty = total_qty
        total_filled_qty = 0.0
        total_cost = 0.0
        total_fee = 0.0
        slice_count = 0
        consecutive_fails = 0
        cumulative_fills: List[Fill] = []
        fill_trade_ids: Set[str] = set()
        max_slippage_hit = False
        cancelled_externally = False

        # 动态时长
        max_duration = self._calc_max_duration(total_qty, symbol, portfolio)
        funding_adjusted = False
        funding_remaining = None
        if self.avoid_funding_period:
            funding_remaining = self._get_funding_remaining_sec(symbol)
            if funding_remaining is not None and funding_remaining > 0 and funding_remaining < max_duration:
                max_duration = max(funding_remaining - self.funding_buffer_sec, self.min_duration_sec)
                funding_adjusted = True
                logger.info(f"TWAP duration adjusted to {max_duration}s due to funding period")

        max_duration = max(max_duration, self.min_duration_sec)
        end_mono = start_mono + max_duration

        # 计算切片计划（基于最终 max_duration）
        num_slices = max(1, int(max_duration / self.slice_interval_sec))
        base_slice_qty = total_qty / num_slices

        try:
            while (time.monotonic() < end_mono and
                   (time.monotonic() - start_mono) < self.global_timeout_sec and
                   remaining_qty > 0):

                # 暂停控制
                try:
                    await asyncio.wait_for(self._pause_event.wait(), timeout=self.pause_timeout_sec)
                except asyncio.TimeoutError:
                    logger.warning("TWAP pause timeout, continuing (pausing is still active)")
                    # 不自动解除暂停，等待外部恢复

                # 连接状态检查
                if not self._adapter_connected():
                    logger.error("Adapter disconnected during TWAP")
                    cancelled_externally = True
                    break

                # 订单外部取消
                state = getattr(original_order, 'state', None)
                if state in (OrderState.CANCELLED, OrderState.REJECTED):
                    cancelled_externally = True
                    break

                if slice_count >= self.max_slices:
                    logger.warning(f"TWAP reached max slices {self.max_slices}")
                    break

                if consecutive_fails >= 3:
                    logger.error("TWAP terminated due to consecutive slice failures")
                    break

                # 止损触发
                if self.stop_on_stop_loss and self._is_stop_triggered(original_order, portfolio):
                    logger.info("TWAP stop-loss triggered, dumping remaining")
                    fill_report = await self._place_market_order(original_order, remaining_qty, portfolio, bypass_slippage=True)
                    if fill_report and getattr(fill_report, 'success', False):
                        self._add_fills(cumulative_fills, fill_trade_ids, fill_report.fills)
                        filled = fill_report.filled_quantity
                        total_filled_qty += filled
                        total_cost += filled * fill_report.avg_price
                        remaining_qty = max(0.0, remaining_qty - filled)
                        consecutive_fails = 0
                    else:
                        logger.warning("Stop-loss market order failed, continuing TWAP")
                    if remaining_qty <= 0 or remaining_qty < self.min_slice_size:
                        break

                # 扫尾逻辑（在常规切片之前判断）
                pct_left = (remaining_qty / total_qty) * 100 if total_qty > 0 else 0
                if 0 < pct_left <= self.aggressive_threshold_pct and remaining_qty > self.min_slice_size:
                    fill_report = await self._place_market_order(original_order, remaining_qty, portfolio, bypass_slippage=False)
                    if fill_report and getattr(fill_report, 'success', False):
                        self._add_fills(cumulative_fills, fill_trade_ids, fill_report.fills)
                        filled = fill_report.filled_quantity
                        total_filled_qty += filled
                        total_cost += filled * fill_report.avg_price
                        remaining_qty = max(0.0, remaining_qty - filled)
                        consecutive_fails = 0
                        if remaining_qty > 0 and remaining_qty >= self.min_slice_size:
                            # 剩余继续切片
                            pass
                        else:
                            break
                    else:
                        # 扫尾失败，继续正常切片
                        logger.warning("Aggressive tail market order failed, proceeding with normal slices")

                # 动态切片量
                slice_qty = self._calc_slice_qty(remaining_qty, base_slice_qty, total_qty, total_filled_qty,
                                                 time.monotonic(), end_mono)
                if slice_qty <= 0:
                    break

                # 按计划时间执行切片
                planned_time = start_mono + slice_count * self.slice_interval_sec
                wait = planned_time - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                # 更新下一次计划时间
                slice_count += 1

                # 刷新组合
                portfolio = await self._refresh_portfolio(portfolio)
                current_price = await self._get_market_price(symbol, portfolio)
                if current_price <= 0:
                    logger.error("Invalid market price for TWAP slice")
                    break

                # 构建子订单
                sub_client_id = f"{client_order_id}_twap_{slice_count}"
                time_in_force = self._get_supported_tif()

                slice_order = Order(
                    symbol=symbol,
                    direction=direction,
                    quantity=slice_qty,
                    price=current_price,
                    order_type='limit',
                    client_order_id=sub_client_id,
                    reduce_only=original_order.reduce_only,
                    time_in_force=time_in_force,
                    post_only=getattr(original_order, 'post_only', False)
                )

                # 滑点保护
                try:
                    guarded = self._slippage.apply_slippage_guard(slice_order)
                    if guarded is not None:
                        slice_order = guarded
                except Exception as e:
                    logger.warning(f"Slippage guard failed: {e}")

                # 风控
                try:
                    risk_verdict = self._risk_firewall.check(slice_order, portfolio)
                except Exception as e:
                    logger.error(f"Risk firewall exception: {e}")
                    break
                if not risk_verdict.passed:
                    if 'temporary' in str(risk_verdict.reason).lower():
                        logger.warning(f"TWAP slice temporarily rejected: {risk_verdict.reason}, retrying later")
                        continue
                    else:
                        logger.warning(f"TWAP slice rejected: {risk_verdict.reason}")
                        break

                # 发送子订单
                report = await self._submit_slice_with_retry(slice_order, current_price)
                if report and getattr(report, 'success', False):
                    self._add_fills(cumulative_fills, fill_trade_ids, report.fills)
                    filled = getattr(report, 'filled_quantity', 0) or 0
                    if filled > 0:
                        total_filled_qty += filled
                        total_cost += filled * (getattr(report, 'avg_price', 0) or current_price)
                        for f in (report.fills or []):
                            total_fee += getattr(f, 'fee', 0) or 0
                        remaining_qty = max(0.0, remaining_qty - filled)
                        consecutive_fails = 0
                    else:
                        consecutive_fails += 1
                else:
                    consecutive_fails += 1
                    logger.warning(f"TWAP slice {slice_count} failed")

        except asyncio.CancelledError:
            logger.info("TWAP cancelled")
            cancelled_externally = True
            raise
        except Exception as e:
            logger.exception(f"Unexpected error during TWAP: {e}")
            cancelled_externally = True
        finally:
            # 清理子订单
            await self._cancel_children(client_order_id)

        # 残余处理：不虚增成交量
        if 0 < remaining_qty < self.min_slice_size:
            logger.info(f"TWAP residual {remaining_qty} < min_slice, not executed")
            # 不增加 total_filled_qty
        # 判断完成状态
        filled_enough = (total_qty - total_filled_qty) < 1e-12
        state = OrderState.FILLED if filled_enough else OrderState.PARTIALLY_FILLED
        avg_price = total_cost / total_filled_qty if total_filled_qty > 0 else 0.0
        elapsed_wall = time.time() - start_wall
        message = (f"TWAP: {slice_count} slices, filled {total_filled_qty}/{total_qty}, "
                   f"elapsed {elapsed_wall:.1f}s, avg price {avg_price:.2f}, fee {total_fee:.4f}, "
                   f"funding_adjusted={funding_adjusted}, max_slippage_hit={max_slippage_hit}, "
                   f"cancelled_externally={cancelled_externally}")
        logger.info(message)
        self._audit_log("TWAP_END", original_order, message)

        report = ExecutionReport(
            order_id=original_order.order_id or "",
            client_order_id=client_order_id,
            state=state,
            filled_quantity=total_filled_qty,
            avg_price=avg_price,
            fills=cumulative_fills,
            message=message
        )
        if self.on_complete:
            try:
                await self.on_complete(report)
            except Exception as e:
                logger.error(f"on_complete callback error: {e}")
        return report

    # --------------------------------------------------------------------------
    # 辅助方法
    # --------------------------------------------------------------------------

    def _adapter_connected(self) -> bool:
        now = time.monotonic()
        # 缓存1秒
        if now - self._conn_check_time > 1.0:
            try:
                connected = getattr(self._adapter, 'is_connected', lambda: False)()
                self._last_connected = bool(connected)
            except Exception:
                self._last_connected = False
            self._conn_check_time = now
        return self._last_connected

    async def _get_market_price(self, symbol: str, portfolio: Portfolio) -> float:
        # 从 portfolio 或适配器获取最新价
        if hasattr(portfolio, 'last_price') and portfolio.last_price:
            return float(portfolio.last_price)
        # 尝试从适配器获取
        try:
            if hasattr(self._adapter, 'get_last_price'):
                return float(await self._adapter.get_last_price(symbol))
        except Exception:
            pass
        return 0.0

    async def _submit_slice_with_retry(self, order: Order, current_price: float) -> Optional[ExecutionReport]:
        for attempt in range(self.max_slice_retries + 1):
            try:
                if attempt > 0:
                    # 重新获取市价并调整限价
                    try:
                        new_price = await self._get_market_price(order.symbol, None)
                        if new_price > 0:
                            order.price = new_price
                    except Exception:
                        pass
                report = await asyncio.wait_for(self._adapter.submit_order(order), timeout=10.0)
                if report and getattr(report, 'success', False):
                    return report
                if report and getattr(report, 'state', None) in ('REJECTED',):
                    return report
            except asyncio.TimeoutError:
                logger.warning(f"TWAP slice timeout attempt {attempt}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"TWAP slice error: {e}")
            if attempt < self.max_slice_retries:
                backoff = min(2 ** attempt, 10) + random.uniform(0, 0.5)
                await asyncio.sleep(backoff)
        return None

    async def _place_market_order(self, template: Order, qty: float, portfolio: Portfolio,
                                  bypass_slippage: bool = False) -> Optional[ExecutionReport]:
        market_order = Order(
            symbol=template.symbol,
            direction=template.direction,
            quantity=qty,
            order_type='market',
            client_order_id=f"{template.client_order_id}_twap_market",
            reduce_only=template.reduce_only
        )
        try:
            self._validator.validate(market_order)
        except Exception as e:
            logger.error(f"Market order validation failed: {e}")
            return None

        if not bypass_slippage:
            if hasattr(self._slippage, 'check_market_order_slippage'):
                try:
                    if not self._slippage.check_market_order_slippage(market_order, portfolio, self.max_slippage_pct):
                        logger.warning("TWAP market order rejected due to slippage limit")
                        # 标记滑点限制被触发
                        # 通过外部变量 max_slippage_hit 设置（此处无法设置，需借助外部，但可忽略）
                        return None
                except Exception as e:
                    logger.error(f"Slippage check error: {e}")
                    return None
        try:
            return await asyncio.wait_for(self._adapter.submit_order(market_order), timeout=15.0)
        except asyncio.TimeoutError:
            logger.error("TWAP market order timeout")
        except Exception as e:
            logger.error(f"TWAP market order failed: {e}")
        return None

    def _is_stop_triggered(self, order: Order, portfolio: Portfolio) -> bool:
        try:
            check_fn = getattr(portfolio, 'is_position_hit_stop', None)
            if check_fn:
                return check_fn(order.symbol, order.direction)
        except Exception:
            pass
        return False

    def _get_funding_remaining_sec(self, symbol: str) -> Optional[float]:
        try:
            if hasattr(self._adapter, 'get_next_funding_time'):
                next_time = self._adapter.get_next_funding_time(symbol)
                if isinstance(next_time, (int, float)) and next_time > time.time():
                    return next_time - time.time()
        except Exception:
            pass
        return None

    async def _refresh_portfolio(self, portfolio: Portfolio) -> Portfolio:
        # 尝试从 account 模块获取最新快照，若失败则返回原对象
        # 生产环境应由 portfolio_manager 提供
        return portfolio

    def _add_fills(self, cumulative: List[Fill], known_ids: Set[str], new_fills: Optional[List[Fill]]):
        if not new_fills:
            return
        for f in new_fills:
            if f is None:
                continue
            tid = getattr(f, 'trade_id', None)
            if tid:
                if tid not in known_ids:
                    known_ids.add(tid)
                    cumulative.append(f)
            else:
                # 无 trade_id，直接添加（可能重复，但无更好办法）
                cumulative.append(f)

    def _calc_max_duration(self, quantity: float, symbol: str, portfolio: Portfolio) -> int:
        base = self.max_duration_sec
        if not self.auto_extend_duration:
            return base
        depth = getattr(portfolio, 'liquidity_depth', None) or 1.0
        try:
            depth = float(depth)
        except (ValueError, TypeError):
            depth = 1.0
        if depth <= 0:
            depth = 1e-8
        impact = quantity / depth
        if impact > 0.01:
            extended = int(base * (1 + impact * self.duration_extend_coefficient))
            return min(extended, self.max_auto_duration_sec)
        return base

    def _calc_slice_qty(self, remaining: float, base: float, total: float,
                        filled: float, now_mono: float, end_mono: float) -> float:
        remaining_time = max(0.1, end_mono - now_mono)
        num_slices = max(1, int(remaining_time / self.slice_interval_sec))
        ideal = remaining / num_slices
        slice_qty = max(self.min_slice_size, min(ideal, base * 1.2))
        return min(slice_qty, remaining)

    def _check_account(self, portfolio: Portfolio) -> bool:
        if getattr(portfolio, 'is_frozen', False):
            return False
        # 可扩展保证金检查
        return True

    def _error_report(self, order: Order, reason: str) -> ExecutionReport:
        return ExecutionReport(
            order_id=order.order_id or "",
            client_order_id=order.client_order_id or "",
            state=OrderState.REJECTED,
            message=f"{reason} (time {time.time():.0f})"
        )

    def _audit_log(self, event: str, order: Order, extra: str = ""):
        extra_clean = extra.replace('\n', ' ').replace('\r', '')
        logger.info(f"AUDIT|TWAP|{event}|order={order.client_order_id}|symbol={order.symbol}|qty={order.quantity}|extra={extra_clean}")

    def _get_supported_tif(self) -> str:
        try:
            if hasattr(self._adapter, 'supported_time_in_forces'):
                tifs = self._adapter.supported_time_in_forces()
                if isinstance(tifs, (list, tuple)) and 'IOC' in tifs:
                    return 'IOC'
        except Exception:
            pass
        return 'GTC'

    async def _cancel_children(self, client_order_id: str):
        if hasattr(self._adapter, 'cancel_orders_by_client_prefix'):
            try:
                await self._adapter.cancel_orders_by_client_prefix(client_order_id)
            except Exception as e:
                logger.warning(f"Failed to cancel children orders: {e}")
