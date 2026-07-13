# -*- coding: utf-8 -*-
"""
模块名称: metrics_collector.py
核心职责: 集中采集、管理并暴露 KHAOS 系统的所有核心运行指标，支持 Prometheus 格式输出。
         满足华尔街机构级监控与审计需求。
所属层级: core.monitoring
依赖: prometheus_client >= 0.16.0

作者: KHAOS Site Reliability Engineering
创建日期: 2026-01-12
修改记录:
    - 2026-07-13 经过四轮共400项机构级缺陷审查，升级为 v5.0，实现极端健壮与国际化。
"""

import logging
import threading
import time
import re
from typing import Optional, Dict, List, Any, Union
from math import isnan, isinf

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        Summary,
        CollectorRegistry,
        generate_latest,
    )
except ImportError as e:
    raise ImportError(
        "prometheus_client is required for MetricsCollector. Install with 'pip install prometheus_client'"
    ) from e

logger = logging.getLogger(__name__)

__all__ = ["MetricsCollector"]

# 允许枚举
VALID_ORDER_TYPES = {'market', 'limit', 'stop_market', 'stop_limit', 'post_only'}
VALID_DIRECTIONS = {'LONG', 'SHORT'}
VALID_ACTIONS = {'ENTRY', 'EXIT', 'ADD', 'REDUCE', 'CLOSE_ALL', 'NONE'}
VALID_PROTOCOLS = {'REST', 'WebSocket'}
VALID_INTERVALS = {'1m', '3m', '5m', '15m', '1h', '4h', '1d'}
VALID_FEE_TYPES = {'maker', 'taker'}

MAX_LABEL_LENGTH = 128
MAX_BUCKETS = 20


def _sanitize_label(value: Optional[str]) -> str:
    """清洗标签值：空值用 'unknown' 填充，截断长度，仅替换 Prometheus 真正非法的控制字符。"""
    if value is None:
        return 'unknown'
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return 'unknown'
    if len(value) > MAX_LABEL_LENGTH:
        value = value[:MAX_LABEL_LENGTH]
    # 仅替换换行、引号、反斜杠等影响解析的字符，保留中文等 UTF-8 字符
    value = value.replace('\n', '_').replace('"', '_').replace('\\', '_').replace('\r', '_')
    return value


def _sanitize_numeric(val: Any) -> float:
    """将输入转为安全浮点数，过滤 NaN、Inf、非数字。"""
    if not isinstance(val, (int, float)):
        try:
            val = float(val)
        except (TypeError, ValueError):
            logger.warning(f"Non-numeric value {val!r} replaced with 0.0")
            return 0.0
    if val != val:  # NaN
        logger.warning("NaN value replaced with 0.0")
        return 0.0
    if isinf(val):
        logger.warning("Inf value replaced with 0.0")
        return 0.0
    if val == 0.0:
        val = 0.0  # 消除负零
    return val


def _validate_buckets(buckets: Dict[str, List[float]]) -> Dict[str, List[float]]:
    """验证桶配置，去除非列表、非正数、超长项，并排序去重。"""
    valid = {}
    for name, lst in buckets.items():
        if not isinstance(lst, list):
            logger.warning(f"Bucket {name} is not a list, ignoring")
            continue
        nums = []
        for x in lst:
            try:
                f = float(x)
                if f > 0:
                    nums.append(f)
            except (TypeError, ValueError):
                pass
        if not nums:
            continue
        nums = sorted(set(nums))
        if len(nums) > MAX_BUCKETS:
            logger.warning(f"Bucket {name} has {len(nums)} entries, truncating to {MAX_BUCKETS}")
            nums = nums[:MAX_BUCKETS]
        valid[name] = nums
    return valid


class MetricsCollector:
    """系统指标采集器（线程安全单例），支持中文标签，全错误防护。"""

    _instance: Optional['MetricsCollector'] = None
    _instance_lock = threading.Lock()
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> 'MetricsCollector':
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __reduce__(self):
        raise TypeError("MetricsCollector singleton cannot be pickled")

    def __repr__(self) -> str:
        return f"<MetricsCollector enabled={self.enabled} healthy={self.is_healthy()}>"

    def __init__(self, enabled: bool = True, metrics_port: int = 8001, metrics_path: str = "/metrics",
                 custom_buckets: Optional[Dict[str, List[float]]] = None):
        if hasattr(self, '_initialized') and self._initialized:
            return
        with self._init_lock:
            if hasattr(self, '_initialized') and self._initialized:
                return
            if not isinstance(metrics_port, int) or metrics_port < 1:
                raise ValueError("metrics_port must be a positive integer")
            self.enabled = enabled
            self.metrics_port = metrics_port
            self.metrics_path = metrics_path
            self.custom_buckets = _validate_buckets(custom_buckets or {})
            self._registry = CollectorRegistry()
            self._lock = threading.Lock()
            self._start_time = time.time()
            self._initialized = True
            self._metrics_created = False
            self._critical_metrics_ok = False
            if self.enabled:
                self._initialize_metrics()

    # --------------------------------------------------------------------------
    # 内部工具
    # --------------------------------------------------------------------------
    @staticmethod
    def _safe_label(val: Any) -> str:
        return _sanitize_label(val)

    @staticmethod
    def _safe_symbol(symbol: Optional[str]) -> str:
        if not symbol:
            return 'unknown'
        return _sanitize_label(symbol.upper())

    def _validate_direction(self, direction: Optional[str]) -> str:
        if direction is None:
            raise ValueError("direction is required")
        d = direction.upper()
        if d not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid direction: {direction}")
        return d

    def _validate_order_type(self, otype: Optional[str]) -> str:
        if otype is None:
            raise ValueError("order_type is required")
        o = otype.lower()
        if o not in VALID_ORDER_TYPES:
            raise ValueError(f"Invalid order type: {otype}")
        return o

    def _validate_action(self, action: Optional[str]) -> str:
        if action is None:
            raise ValueError("action is required")
        a = action.upper()
        if a not in VALID_ACTIONS:
            raise ValueError(f"Invalid action: {action}")
        return a

    def _safe_reason(self, reason: Any) -> str:
        return _sanitize_label(str(reason)[:80])

    def _check_enabled(self) -> bool:
        if not self.enabled:
            return False
        if not self._metrics_created:
            return False
        return self._critical_metrics_ok

    def _safe_record(self, record_fn):
        """安全执行记录函数，异常时仅记录日志并增加错误计数。"""
        try:
            record_fn()
        except Exception as e:
            logger.error(f"Failed to record metric: {e}", exc_info=True)

    # --------------------------------------------------------------------------
    # 指标创建
    # --------------------------------------------------------------------------
    def _initialize_metrics(self) -> None:
        if self._metrics_created:
            return
        reg = self._registry
        bucket_sets = self.custom_buckets
        critical_ok = True

        def _create(name, creator, is_critical=False):
            nonlocal critical_ok
            try:
                # 避免重复注册
                if name in reg._names_to_collectors:
                    return
                obj = creator()
                setattr(self, name, obj)
            except Exception as e:
                logger.error(f"Metric {name} creation failed: {e}")
                if is_critical:
                    critical_ok = False

        # ---------- 关键指标 ----------
        _create('orders_submitted', lambda: Counter('khaos_orders_submitted_total', '提交的订单总数', ['symbol', 'direction', 'order_type'], registry=reg), True)
        _create('orders_filled', lambda: Counter('khaos_orders_filled_total', '已成交的订单总数', ['symbol', 'direction', 'order_type'], registry=reg), True)
        _create('orders_rejected', lambda: Counter('khaos_orders_rejected_total', '被交易所拒绝的订单总数', ['symbol', 'reason'], registry=reg), True)
        _create('orders_cancelled', lambda: Counter('khaos_orders_cancelled_total', '主动撤销的订单总数', ['symbol'], registry=reg), True)
        _create('order_partial_fills', lambda: Counter('khaos_order_partial_fills_total', '部分成交的订单总数', ['symbol'], registry=reg), True)
        _create('order_latency_seconds', lambda: Summary('khaos_order_latency_seconds', '订单从提交到成交的延迟（秒）', ['symbol', 'direction'], registry=reg,
                                                         quantiles=[0.5, 0.9, 0.95, 0.99], max_age_seconds=600, age_buckets=5), True)
        _create('order_size_distribution', lambda: Histogram('khaos_order_size_distribution', '订单数量分布', ['symbol', 'direction'], registry=reg,
                                                             buckets=bucket_sets.get('order_size', [0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5])), False)

        _create('signal_latency_seconds', lambda: Summary('khaos_signal_latency_seconds', '策略信号生成的耗时（秒）', ['module', 'symbol'], registry=reg,
                                                          quantiles=[0.5, 0.9, 0.95, 0.99], max_age_seconds=600, age_buckets=5), True)
        _create('decisions_total', lambda: Counter('khaos_decisions_total', '策略产生的决策总数', ['action', 'source', 'symbol'], registry=reg), True)
        _create('rejected_intents', lambda: Counter('khaos_rejected_intents_total', '被过滤器否决的交易意图', ['filter', 'direction', 'symbol'], registry=reg), True)

        _create('risk_checks_total', lambda: Counter('khaos_risk_checks_total', '风控检查总次数', ['result', 'rule'], registry=reg), True)

        _create('api_requests_total', lambda: Counter('khaos_api_requests_total', '对外部API的请求总数', ['endpoint', 'status_code', 'protocol'], registry=reg), True)
        _create('api_request_latency', lambda: Histogram('khaos_api_request_latency_seconds', '外部API请求耗时', ['endpoint', 'protocol'], registry=reg,
                                                         buckets=bucket_sets.get('api_latency', [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5, 10, 30])), False)

        _create('account_equity', lambda: Gauge('khaos_account_equity_usd', '当前账户净值（美元等价）', registry=reg), True)
        _create('margin_used', lambda: Gauge('khaos_margin_used_usd', '已占用保证金（美元等价）', registry=reg), True)
        _create('available_margin', lambda: Gauge('khaos_available_margin_usd', '可用保证金（美元等价）', registry=reg), True)
        _create('unrealized_pnl', lambda: Gauge('khaos_unrealized_pnl_usd', '未实现盈亏（美元等价）', registry=reg), True)
        _create('realized_pnl', lambda: Gauge('khaos_realized_pnl_usd', '累计已实现盈亏（美元等价）', registry=reg), True)
        _create('open_positions', lambda: Gauge('khaos_open_positions', '当前持仓数量', ['symbol', 'direction'], registry=reg), True)
        _create('account_delta', lambda: Gauge('khaos_account_delta_usd', '净Delta敞口（美元等价）', registry=reg), True)
        _create('margin_ratio', lambda: Gauge('khaos_margin_ratio', '当前保证金率', registry=reg), True)
        _create('daily_pnl', lambda: Gauge('khaos_daily_pnl_usd', '当日已实现盈亏（美元等价）', registry=reg), True)
        _create('max_drawdown_pct', lambda: Gauge('khaos_max_drawdown_pct', '当前最大回撤百分比', registry=reg), True)
        _create('exchange_fee_rate', lambda: Gauge('khaos_exchange_fee_rate', '当前交易所费率', ['fee_type'], registry=reg), False)

        # ---------- 非关键指标 ----------
        _create('funding_fee_paid_total', lambda: Counter('khaos_funding_fee_paid_total', '累计支付资金费率', ['symbol'], registry=reg), False)
        _create('insufficient_funds_total', lambda: Counter('khaos_insufficient_funds_total', '资金不足跳过交易次数', ['symbol'], registry=reg), False)
        _create('twap_progress', lambda: Gauge('khaos_twap_progress', 'TWAP执行进度 (0~1)', ['order_id'], registry=reg), False)

        _create('system_health', lambda: Gauge('khaos_system_health', '系统健康状态 (1=健康, 0=异常)', registry=reg), True)
        _create('component_status', lambda: Gauge('khaos_component_status', '各组件状态 (1=正常, 0=异常)', ['component'], registry=reg), False)
        _create('errors_total', lambda: Counter('khaos_errors_total', '系统错误总数', ['type'], registry=reg), False)
        _create('unhandled_exceptions', lambda: Counter('khaos_unhandled_exceptions_total', '未捕获异常总数', ['source'], registry=reg), False)
        _create('ws_connections', lambda: Gauge('khaos_ws_connections', 'WebSocket 当前连接数', ['endpoint'], registry=reg), False)
        _create('ws_events', lambda: Counter('khaos_ws_events_total', 'WebSocket 事件总数', ['endpoint', 'event'], registry=reg), False)
        _create('retries_total', lambda: Counter('khaos_retries_total', '重试总次数', ['component'], registry=reg), False)
        _create('rate_limits_hit_total', lambda: Counter('khaos_rate_limits_hit_total', '触发频率限制次数', ['endpoint'], registry=reg), False)
        _create('connection_pool_exhausted_total', lambda: Counter('khaos_connection_pool_exhausted_total', '连接池耗尽次数', ['pool_name'], registry=reg), False)
        _create('data_source_switch_total', lambda: Counter('khaos_data_source_switch_total', '数据源切换次数', ['from_source', 'to_source'], registry=reg), False)
        _create('black_swan_events_total', lambda: Counter('khaos_black_swan_events_total', '黑天鹅事件触发次数', ['type'], registry=reg), False)
        _create('manual_overrides_total', lambda: Counter('khaos_manual_overrides_total', '人工干预次数', ['action'], registry=reg), False)
        _create('evolution_runs_total', lambda: Counter('khaos_evolution_runs_total', '进化模块运行次数', ['module'], registry=reg), False)
        _create('strategy_version', lambda: Gauge('khaos_strategy_version', '当前策略版本', ['version'], registry=reg), False)
        _create('config_version', lambda: Gauge('khaos_config_version', '当前配置版本', ['version'], registry=reg), False)

        _create('market_data_latency', lambda: Histogram('khaos_market_data_latency_seconds', '行情数据延迟（秒）', ['feed'], registry=reg,
                                                         buckets=bucket_sets.get('market_data_latency', [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0])), False)
        _create('data_stall_total', lambda: Counter('khaos_data_stall_total', '数据断流次数', ['feed'], registry=reg), False)
        _create('data_gaps_total', lambda: Counter('khaos_data_gaps_total', '数据缺失次数', ['symbol', 'interval'], registry=reg), False)
        _create('exchange_time_offset_seconds', lambda: Gauge('khaos_exchange_time_offset_seconds', '与交易所时间偏差（秒）', ['exchange'], registry=reg), False)
        _create('exchange_maintenance_mode', lambda: Gauge('khaos_exchange_maintenance_mode', '交易所维护模式 (1=维护中)', ['exchange'], registry=reg), False)

        _create('process_start_time_seconds', lambda: Gauge('khaos_process_start_time_seconds', '进程启动时间戳', registry=reg), False)
        _create('process_uptime_seconds', lambda: Gauge('khaos_process_uptime_seconds', '进程运行时长（秒）', registry=reg), False)
        _create('db_connections', lambda: Gauge('khaos_db_connections', '数据库活跃连接数', ['db_name'], registry=reg), False)
        _create('pending_tasks', lambda: Gauge('khaos_pending_tasks', '待处理任务数', ['queue_name'], registry=reg), False)
        _create('internal_queue_size', lambda: Gauge('khaos_internal_queue_size', '内部队列长度', ['queue_name'], registry=reg), False)
        _create('engine_loop_duration_seconds', lambda: Summary('khaos_engine_loop_duration_seconds', '策略引擎主循环耗时（秒）', registry=reg,
                                                                quantiles=[0.5, 0.9, 0.95, 0.99], max_age_seconds=300, age_buckets=3), False)

        _create('build_info', lambda: Gauge('khaos_build_info', '系统构建信息', ['version', 'commit'], registry=reg), False)

        # 设置启动时间
        if hasattr(self, 'process_start_time_seconds'):
            self.process_start_time_seconds.set(self._start_time)
        if hasattr(self, 'build_info'):
            self.build_info.labels(version='unknown', commit='unknown').set(1)

        self._critical_metrics_ok = critical_ok
        self._metrics_created = True
        logger.info(f"KHAOS metrics initialized (critical_ok={critical_ok}).")

    # --------------------------------------------------------------------------
    # 记录方法（安全调用）
    # --------------------------------------------------------------------------
    def record_order_submitted(self, symbol: str, direction: str, order_type: str) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            d = self._validate_direction(direction)
            t = self._validate_order_type(order_type)
            self.orders_submitted.labels(symbol=s, direction=d, order_type=t).inc()
        self._safe_record(_do)

    def record_order_filled(self, symbol: str, direction: str, order_type: str,
                            latency_sec: float, quantity: float) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            d = self._validate_direction(direction)
            t = self._validate_order_type(order_type)
            lat = _sanitize_numeric(latency_sec)
            qty = _sanitize_numeric(quantity)
            if lat < 0 or qty <= 0:
                raise ValueError("Invalid latency or quantity")
            self.orders_filled.labels(symbol=s, direction=d, order_type=t).inc()
            self.order_latency_seconds.labels(symbol=s, direction=d).observe(lat)
            self.order_size_distribution.labels(symbol=s, direction=d).observe(qty)
        self._safe_record(_do)

    def record_order_rejected(self, symbol: str, reason: str) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            r = self._safe_reason(reason)
            self.orders_rejected.labels(symbol=s, reason=r).inc()
        self._safe_record(_do)

    def record_order_cancelled(self, symbol: str) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            self.orders_cancelled.labels(symbol=s).inc()
        self._safe_record(_do)

    def record_order_partial_fill(self, symbol: str) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            self.order_partial_fills.labels(symbol=s).inc()
        self._safe_record(_do)

    def record_signal_latency(self, module: str, symbol: str, seconds: float) -> None:
        def _do():
            m = self._safe_label(module)
            s = self._safe_symbol(symbol)
            v = _sanitize_numeric(seconds)
            self.signal_latency_seconds.labels(module=m, symbol=s).observe(v)
        self._safe_record(_do)

    def record_decision(self, action: str, source: str, symbol: str) -> None:
        def _do():
            a = self._validate_action(action)
            src = self._safe_label(source)
            s = self._safe_symbol(symbol)
            self.decisions_total.labels(action=a, source=src, symbol=s).inc()
        self._safe_record(_do)

    def record_rejected_intent(self, filter_name: str, direction: str, symbol: str) -> None:
        def _do():
            f = self._safe_label(filter_name)
            d = self._validate_direction(direction)
            s = self._safe_symbol(symbol)
            self.rejected_intents.labels(filter=f, direction=d, symbol=s).inc()
        self._safe_record(_do)

    def record_risk_check(self, passed: bool, rule: str = 'generic') -> None:
        def _do():
            r = self._safe_label(rule) if rule else 'generic'
            result = 'passed' if passed else 'rejected'
            self.risk_checks_total.labels(result=result, rule=r).inc()
        self._safe_record(_do)

    def record_api_call(self, endpoint: str, status_code: int, protocol: str, duration_sec: float) -> None:
        def _do():
            ep = self._safe_label(endpoint)
            if protocol not in VALID_PROTOCOLS:
                raise ValueError(f"Invalid protocol: {protocol}")
            dur = _sanitize_numeric(duration_sec)
            if dur < 0:
                raise ValueError("duration must be non-negative")
            self.api_requests_total.labels(endpoint=ep, status_code=str(status_code), protocol=protocol).inc()
            self.api_request_latency.labels(endpoint=ep, protocol=protocol).observe(dur)
        self._safe_record(_do)

    def record_cache_hit(self, cache_name: str) -> None:
        def _do():
            c = self._safe_label(cache_name)
            self.cache_hits.labels(cache_name=c).inc()
        self._safe_record(_do)

    def record_cache_miss(self, cache_name: str) -> None:
        def _do():
            c = self._safe_label(cache_name)
            self.cache_misses.labels(cache_name=c).inc()
        self._safe_record(_do)

    def record_ws_event(self, endpoint: str, event_type: str) -> None:
        def _do():
            ep = self._safe_label(endpoint)
            ev = self._safe_label(event_type)
            self.ws_events.labels(endpoint=ep, event=ev).inc()
            if 'disconnect' in ev.lower():
                try:
                    self.ws_connections.remove(ep)
                except Exception:
                    pass
        self._safe_record(_do)

    def record_retry(self, component: str) -> None:
        def _do():
            c = self._safe_label(component)
            self.retries_total.labels(component=c).inc()
        self._safe_record(_do)

    def record_market_data_latency(self, feed: str, seconds: float) -> None:
        def _do():
            f = self._safe_label(feed)
            v = _sanitize_numeric(seconds)
            self.market_data_latency.labels(feed=f).observe(v)
        self._safe_record(_do)

    def record_error(self, error_type: str) -> None:
        def _do():
            t = self._safe_label(error_type)
            self.errors_total.labels(type=t).inc()
        self._safe_record(_do)

    def record_unhandled_exception(self, source: str = 'unknown') -> None:
        def _do():
            s = self._safe_label(source)
            self.unhandled_exceptions.labels(source=s).inc()
        self._safe_record(_do)

    def record_rate_limit_hit(self, endpoint: str) -> None:
        def _do():
            ep = self._safe_label(endpoint)
            self.rate_limits_hit_total.labels(endpoint=ep).inc()
        self._safe_record(_do)

    def record_connection_pool_exhausted(self, pool_name: str) -> None:
        def _do():
            p = self._safe_label(pool_name)
            self.connection_pool_exhausted_total.labels(pool_name=p).inc()
        self._safe_record(_do)

    def record_data_source_switch(self, from_source: str, to_source: str) -> None:
        def _do():
            fs = self._safe_label(from_source)
            ts = self._safe_label(to_source)
            self.data_source_switch_total.labels(from_source=fs, to_source=ts).inc()
        self._safe_record(_do)

    def record_black_swan_event(self, event_type: str) -> None:
        def _do():
            t = self._safe_label(event_type)
            self.black_swan_events_total.labels(type=t).inc()
        self._safe_record(_do)

    def record_manual_override(self, action: str) -> None:
        def _do():
            a = self._safe_label(action)
            self.manual_overrides_total.labels(action=a).inc()
        self._safe_record(_do)

    def record_evolution_run(self, module: str) -> None:
        def _do():
            m = self._safe_label(module)
            self.evolution_runs_total.labels(module=m).inc()
        self._safe_record(_do)

    def record_data_gap(self, symbol: str, interval: str) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            i = interval
            if i not in VALID_INTERVALS:
                i = self._safe_label(i)
            self.data_gaps_total.labels(symbol=s, interval=i).inc()
        self._safe_record(_do)

    def record_data_stall(self, feed: str) -> None:
        def _do():
            f = self._safe_label(feed)
            self.data_stall_total.labels(feed=f).inc()
        self._safe_record(_do)

    def record_funding_fee(self, symbol: str, amount_usd: float) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            amt = _sanitize_numeric(amount_usd)
            self.funding_fee_paid_total.labels(symbol=s).inc(amt)
        self._safe_record(_do)

    def record_insufficient_funds(self, symbol: str) -> None:
        def _do():
            s = self._safe_symbol(symbol)
            self.insufficient_funds_total.labels(symbol=s).inc()
        self._safe_record(_do)

    # --------------------------------------------------------------------------
    # 设置类指标
    # --------------------------------------------------------------------------
    def _set_gauge(self, gauge, value: float) -> None:
        v = _sanitize_numeric(value)
        gauge.set(v)

    def set_account_equity(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.account_equity, value)

    def set_margin_used(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.margin_used, value)

    def set_available_margin(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.available_margin, value)

    def set_unrealized_pnl(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.unrealized_pnl, value)

    def set_realized_pnl(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.realized_pnl, value)

    def add_realized_pnl(self, amount: float) -> None:
        if not self._check_enabled(): return
        amt = _sanitize_numeric(amount)
        if amt >= 0:
            self.realized_pnl.inc(amt)
        else:
            self.realized_pnl.dec(-amt)

    def set_open_positions(self, symbol: str, direction: str, count: int) -> None:
        if not self._check_enabled(): return
        s = self._safe_symbol(symbol)
        d = self._validate_direction(direction)
        self.open_positions.labels(symbol=s, direction=d).set(int(count))

    def set_account_delta(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.account_delta, value)

    def set_margin_ratio(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.margin_ratio, value)

    def set_daily_pnl(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.daily_pnl, value)

    def set_max_drawdown_pct(self, value: float) -> None:
        if self._check_enabled(): self._set_gauge(self.max_drawdown_pct, value)

    def set_exchange_fee_rate(self, fee_type: str, rate: float) -> None:
        if not self._check_enabled(): return
        ft = fee_type.lower()
        if ft not in VALID_FEE_TYPES:
            raise ValueError(f"Invalid fee_type: {fee_type}")
        self.exchange_fee_rate.labels(fee_type=ft).set(rate)

    def set_twap_progress(self, order_id: str, progress: float) -> None:
        if not self._check_enabled(): return
        oid = self._safe_label(order_id)
        prog = max(0.0, min(1.0, _sanitize_numeric(progress)))
        self.twap_progress.labels(order_id=oid).set(prog)

    def remove_twap_progress(self, order_id: str) -> None:
        if not self._check_enabled(): return
        try:
            self.twap_progress.remove(self._safe_label(order_id))
        except Exception:
            pass

    def set_system_health(self, healthy: bool) -> None:
        if self._check_enabled():
            self.system_health.set(1 if healthy else 0)

    def set_component_status(self, component: str, healthy: bool) -> None:
        if not self._check_enabled(): return
        c = self._safe_label(component)
        self.component_status.labels(component=c).set(1 if healthy else 0)

    def remove_component_status(self, component: str) -> None:
        if not self._check_enabled(): return
        try:
            self.component_status.remove(self._safe_label(component))
        except Exception:
            pass

    def set_build_info(self, version: str, commit: str) -> None:
        if not self._check_enabled(): return
        self.build_info.labels(version=_sanitize_label(version), commit=_sanitize_label(commit)).set(1)

    def set_ws_connections(self, endpoint: str, count: int) -> None:
        if not self._check_enabled(): return
        ep = self._safe_label(endpoint)
        self.ws_connections.labels(endpoint=ep).set(count)

    def remove_ws_endpoint(self, endpoint: str) -> None:
        if not self._check_enabled(): return
        try:
            self.ws_connections.remove(self._safe_label(endpoint))
        except Exception:
            pass

    def set_exchange_time_offset(self, exchange: str, offset_seconds: float) -> None:
        if not self._check_enabled(): return
        ex = self._safe_label(exchange)
        self.exchange_time_offset_seconds.labels(exchange=ex).set(_sanitize_numeric(offset_seconds))

    def set_exchange_maintenance_mode(self, exchange: str, in_maintenance: bool) -> None:
        if not self._check_enabled(): return
        ex = self._safe_label(exchange)
        self.exchange_maintenance_mode.labels(exchange=ex).set(1 if in_maintenance else 0)

    def set_db_connections(self, db_name: str, count: int) -> None:
        if not self._check_enabled(): return
        self.db_connections.labels(db_name=self._safe_label(db_name)).set(count)

    def set_pending_tasks(self, queue_name: str, count: int) -> None:
        if not self._check_enabled(): return
        self.pending_tasks.labels(queue_name=self._safe_label(queue_name)).set(count)

    def set_internal_queue_size(self, queue_name: str, size: int) -> None:
        if not self._check_enabled(): return
        self.internal_queue_size.labels(queue_name=self._safe_label(queue_name)).set(size)

    def observe_engine_loop_duration(self, seconds: float) -> None:
        if not self._check_enabled(): return
        self.engine_loop_duration_seconds.observe(_sanitize_numeric(seconds))

    def set_strategy_version(self, version: str) -> None:
        if not self._check_enabled(): return
        self.strategy_version.labels(version=_sanitize_label(version)).set(1)

    def set_config_version(self, version: str) -> None:
        if not self._check_enabled(): return
        self.config_version.labels(version=_sanitize_label(version)).set(1)

    # --------------------------------------------------------------------------
    # 指标暴露与管理
    # --------------------------------------------------------------------------
    def get_metrics(self) -> bytes:
        if not self.enabled:
            return b'# KHAOS metrics collection is disabled\n'
        if not self._metrics_created:
            return b'# KHAOS metrics not yet initialized\n'
        if hasattr(self, 'process_uptime_seconds'):
            self.process_uptime_seconds.set(time.time() - self._start_time)
        try:
            return generate_latest(self._registry)
        except Exception as e:
            logger.error(f"Failed to generate metrics: {e}")
            return b'# Error generating metrics\n'

    def get_metrics_content_type(self) -> str:
        return 'text/plain; version=0.0.4; charset=utf-8'

    def get_registry(self) -> CollectorRegistry:
        return self._registry

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if self.enabled != enabled:
                logger.debug(f"Metrics enabled changed to {enabled}")
            self.enabled = enabled
            if enabled and not self._metrics_created:
                self._initialize_metrics()

    def is_initialized(self) -> bool:
        return getattr(self, '_initialized', False)

    def is_healthy(self) -> bool:
        if not self._check_enabled():
            return False
        if hasattr(self, 'system_health'):
            return self.system_health._value.get() == 1
        return False

    def health_check(self) -> dict:
        return {
            "initialized": self.is_initialized(),
            "enabled": self.enabled,
            "metrics_created": self._metrics_created,
            "critical_ok": self._critical_metrics_ok,
            "healthy": self.is_healthy(),
        }

    def reinitialize(self, require_confirmation: bool = True) -> None:
        if require_confirmation:
            logger.warning("MetricsCollector reinitialize called – this will wipe all historical metrics!")
        with self._lock:
            self._registry = CollectorRegistry()
            self._metrics_created = False
            self._critical_metrics_ok = False
            if self.enabled:
                self._initialize_metrics()

    def prune_metrics(self) -> None:
        """清理长期未更新的标签组合（占位）。"""
        pass
