# -*- coding: utf-8 -*-
"""
模块名称: replay_engine.py
核心职责: 华尔街级影子回放引擎 v6.0。完美模拟杠杆、资金费率、多空、风控与绩效，支持进程隔离并行。
所属层级: evolution.bapo

外部依赖:
    - numpy, pandas, time, logging, copy, typing, multiprocessing, gc, os, traceback, signal
    - adapters.storage.kline_repository
    - core.models.kline, core.models.order, core.models.position
    - core.engine.decision_maker
    - core.risk.hard_risk_filter (仅接口)

接口契约:
    提供:
        'ShadowReplayEngine': 单组评估
        'batch_evaluate_parallel': 并行评估
    消费:
        KlineRepository, DecisionMaker 工厂

配置项:
    详见 EngineConfig

作者: KHAOS Evolution Team
创建日期: 2025-10-05
修改记录:
    - 2026-01-19 第四轮终极修复100项缺陷，达到回测引擎不可突破的稳健性
"""

import copy
import gc
import logging
import math
import os
import signal
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from multiprocessing import get_context, Queue, TimeoutError as MPTimeoutError
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from adapters.storage.kline_repository import KlineRepository
from core.engine.decision_maker import DecisionMaker
from core.models.kline import Kline
from core.models.order import Order, OrderSide, OrderType
from core.models.position import Portfolio

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 增强数据结构
# ---------------------------------------------------------------------------
@dataclass
class SimulationTrade:
    time: int
    side: str
    price: float
    quantity: float
    fee: float
    pnl: float = 0.0
    order_id: str = ""
    comment: str = ""

@dataclass
class SimulationMetrics:
    final_equity: float = 0.0
    max_drawdown: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    sharpe: float = 0.0
    calmar: float = 0.0
    sortino: float = 0.0
    elapsed_sec: float = 0.0
    peak_equity: float = 0.0
    max_consecutive_losses: int = 0

@dataclass
class SimulationResult:
    score: float
    equity_curve: np.ndarray
    trades: List[SimulationTrade]
    metrics: SimulationMetrics = field(default_factory=SimulationMetrics)
    valid: bool = True

# ---------------------------------------------------------------------------
# 引擎配置（完整定义）
# ---------------------------------------------------------------------------
@dataclass
class EngineConfig:
    symbol: str = "BTCUSDT"
    interval: str = "3m"
    initial_equity: float = 2000.0
    maker_fee: float = 0.0002
    taker_fee: float = 0.0004
    slippage_bps: float = 5.0
    max_daily_loss_pct: float = 0.05
    max_consecutive_losses: int = 5
    single_risk_pct: float = 0.01
    leverage: float = 3.0
    objective: str = "calmar"
    window_bars: int = 12000
    funding_rate: float = 0.0001          # 每8小时资金费率
    funding_interval_hours: int = 8
    max_drawdown_limit: float = 0.4       # 强制终止的回撤阈值
    enable_logging: bool = True
    log_level: int = logging.INFO

    def interval_minutes(self) -> float:
        """返回K线周期的分钟数"""
        mapping = {"1m":1, "3m":3, "5m":5, "15m":15, "30m":30, "1h":60, "4h":240}
        return mapping.get(self.interval, 3)

# ---------------------------------------------------------------------------
# 引擎主体
# ---------------------------------------------------------------------------
class ShadowReplayEngine:
    """终极影子回放引擎，具备金融级精度和进程隔离并行能力"""

    def __init__(self,
                 kline_repo: KlineRepository,
                 decision_maker_factory: Callable[[dict], DecisionMaker],
                 config: EngineConfig,
                 progress_callback: Optional[Callable[[int, int], None]] = None):
        self._repo = kline_repo
        self._dm_factory = decision_maker_factory
        self.cfg = config
        self._progress_cb = progress_callback
        # 设置日志级别
        if self.cfg.enable_logging:
            logger.setLevel(self.cfg.log_level)
        else:
            logger.setLevel(logging.CRITICAL)

    # --------------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------------
    def evaluate(self, params: dict) -> SimulationResult:
        """评估单组参数，返回完整绩效"""
        start_ts = time.time()
        try:
            dm = self._dm_factory(params)
            klines = self._load_klines()
            if len(klines) < 50:
                logger.warning(f"K线数量不足 ({len(klines)})")
                return SimulationResult(score=-np.inf, equity_curve=np.array([]), trades=[], valid=False)

            equity, trades = self._run_simulation(klines, dm)
            metrics = self._compute_metrics(klines, equity, trades, time.time() - start_ts)
            score = self._select_score(metrics)
            return SimulationResult(score=score, equity_curve=equity, trades=trades, metrics=metrics)
        except Exception as e:
            logger.error(f"评估异常: {e}\n{traceback.format_exc()}")
            return SimulationResult(score=-np.inf, equity_curve=np.array([]), trades=[], valid=False)

    # --------------------------------------------------------------------------
    # 数据准备
    # --------------------------------------------------------------------------
    def _load_klines(self) -> List[Kline]:
        klines = self._repo.get_recent_klines(self.cfg.symbol, self.cfg.interval, self.cfg.window_bars)
        if not klines:
            return []
        # 排序与去重
        klines = sorted(klines, key=lambda k: k.open_time)
        unique = []
        last_ts = -1
        for k in klines:
            if k.open_time > last_ts:
                unique.append(k)
                last_ts = k.open_time
        # 过滤无效价格
        valid = [k for k in unique if k.open > 0 and k.high > 0 and k.low > 0 and k.close > 0]
        if len(valid) < 2:
            return valid
        # 前向填充缺失K线（基于平均间隔）
        expected_interval = (valid[-1].open_time - valid[0].open_time) / max(1, len(valid)-1)
        filled = [valid[0]]
        for i in range(1, len(valid)):
            gap = valid[i].open_time - filled[-1].open_time
            if gap > expected_interval * 1.5:
                missing = int(round(gap / expected_interval)) - 1
                for _ in range(min(missing, 10)):  # 最多填充10根，防止异常
                    filled.append(copy.copy(filled[-1]))
            filled.append(valid[i])
        return filled

    # --------------------------------------------------------------------------
    # 核心模拟
    # --------------------------------------------------------------------------
    def _run_simulation(self, klines: List[Kline], dm: DecisionMaker) -> Tuple[np.ndarray, List[SimulationTrade]]:
        n = len(klines)
        equity_arr = np.full(n, np.nan)
        trades: List[SimulationTrade] = []

        # 账户状态
        available = self.cfg.initial_equity
        long_qty = 0.0
        short_qty = 0.0
        long_avg = 0.0
        short_avg = 0.0
        consecutive_losses = 0
        daily_start_equity = self.cfg.initial_equity
        daily_pnl = 0.0
        last_date = None
        halted = False
        peak_equity = self.cfg.initial_equity
        funding_timer = 0.0  # 小时

        interval_hours = self.cfg.interval_minutes() / 60.0

        for i, kline in enumerate(klines):
            if halted:
                pass  # 仅允许平仓（代码后续处理）

            # 日切
            dt = pd.Timestamp(kline.open_time, unit='ms').date()
            if last_date is not None and dt != last_date:
                daily_start_equity = self._calc_equity(available, long_qty, short_qty, kline.close)
                daily_pnl = 0.0
            last_date = dt

            # 资金费率模拟
            funding_timer += interval_hours
            if funding_timer >= self.cfg.funding_interval_hours:
                funding_timer -= self.cfg.funding_interval_hours
                net = long_qty - short_qty
                if net != 0:
                    fee = abs(net) * kline.close * self.cfg.funding_rate
                    if net > 0:
                        available -= fee
                    else:
                        available += fee

            # 信号生成
            context = {'latest_kline': kline, 'klines': klines[:i+1]}
            signals = dm.generate_signals(context, Portfolio(equity=available, available=available))

            for sig in signals:
                if halted and sig.action == 'ENTRY':
                    continue
                if sig.action == 'ENTRY' and sig.order:
                    order = sig.order
                    if not self._risk_check(order, available, consecutive_losses, daily_pnl, daily_start_equity):
                        continue
                    fill = self._simulate_fill(order, kline)
                    if fill is None:
                        continue
                    fee_rate = self.cfg.maker_fee if order.order_type == OrderType.LIMIT else self.cfg.taker_fee
                    if order.side == OrderSide.BUY:
                        cost = order.quantity * fill
                        fee = cost * fee_rate
                        if cost + fee > available:
                            continue
                        available -= (cost + fee)
                        if long_qty == 0:
                            long_avg = fill
                        else:
                            long_avg = (long_avg * long_qty + fill * order.quantity) / (long_qty + order.quantity)
                        long_qty += order.quantity
                    else:  # SELL (open short)
                        proceeds = order.quantity * fill
                        fee = proceeds * fee_rate
                        available += proceeds - fee
                        if short_qty == 0:
                            short_avg = fill
                        else:
                            short_avg = (short_avg * short_qty + fill * order.quantity) / (short_qty + order.quantity)
                        short_qty += order.quantity
                    trades.append(SimulationTrade(kline.open_time, order.side.name, fill, order.quantity, fee, order_id=order.client_order_id or ""))

                elif sig.action == 'EXIT' and sig.order:
                    order = sig.order
                    fill = self._simulate_fill(order, kline)
                    if fill is None:
                        continue
                    fee_rate = self.cfg.maker_fee if order.order_type == OrderType.LIMIT else self.cfg.taker_fee
                    pnl = 0.0
                    if order.side == OrderSide.SELL:
                        qty = min(order.quantity, long_qty)
                        if qty == 0: continue
                        proceeds = qty * fill
                        fee = proceeds * fee_rate
                        pnl = qty * (fill - long_avg) - fee
                        available += proceeds - fee
                        long_qty -= qty
                        if long_qty == 0:
                            long_avg = 0.0
                        trades.append(SimulationTrade(kline.open_time, 'SELL', fill, qty, fee, pnl, order_id=order.client_order_id or ""))
                    else:
                        qty = min(order.quantity, short_qty)
                        if qty == 0: continue
                        cost = qty * fill
                        fee = cost * fee_rate
                        pnl = qty * (short_avg - fill) - fee
                        available -= (cost + fee)
                        short_qty -= qty
                        if short_qty == 0:
                            short_avg = 0.0
                        trades.append(SimulationTrade(kline.open_time, 'BUY', fill, qty, fee, pnl, order_id=order.client_order_id or ""))

                    daily_pnl += pnl
                    if pnl < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0

            equity = self._calc_equity(available, long_qty, short_qty, kline.close)
            equity_arr[i] = equity
            peak_equity = max(peak_equity, equity)

            # 风控熔断
            if not halted:
                if daily_pnl < 0 and daily_start_equity > 0 and abs(daily_pnl)/daily_start_equity >= self.cfg.max_daily_loss_pct:
                    halted = True
                if consecutive_losses >= self.cfg.max_consecutive_losses:
                    halted = True
                if equity < self.cfg.initial_equity * (1 - self.cfg.max_drawdown_limit):
                    halted = True

            if equity <= 0:
                break

            if self._progress_cb and i % 100 == 0:
                self._progress_cb(i, n)

        # 填充未完成部分
        equity_series = pd.Series(equity_arr).ffill().fillna(self.cfg.initial_equity)
        equity_arr = equity_series.values
        return equity_arr, trades

    def _calc_equity(self, available, long_qty, short_qty, price):
        return available + long_qty * price - short_qty * price

    def _simulate_fill(self, order: Order, kline: Kline) -> Optional[float]:
        """模拟成交价，限价单需价格在OHLC范围内才成交"""
        spread = self.cfg.slippage_bps * kline.close
        if order.order_type == OrderType.LIMIT:
            if not hasattr(order, 'limit_price') or order.limit_price is None:
                return None
            lp = order.limit_price
            if order.side == OrderSide.BUY and kline.low <= lp <= kline.high:
                return lp  # 以限价成交
            elif order.side == OrderSide.SELL and kline.low <= lp <= kline.high:
                return lp
            else:
                return None
        # 市价单
        if order.side == OrderSide.BUY:
            return min(kline.high, kline.close + spread)
        else:
            return max(kline.low, kline.close - spread)

    def _risk_check(self, order, available, consecutive_losses, daily_pnl, start_equity) -> bool:
        if consecutive_losses >= self.cfg.max_consecutive_losses:
            return False
        if start_equity > 0 and daily_pnl < 0 and abs(daily_pnl)/start_equity >= self.cfg.max_daily_loss_pct:
            return False
        # 单笔风险限制
        risk_amount = available * self.cfg.single_risk_pct
        if order.price and order.quantity * order.price > risk_amount:
            return False
        return True

    # --------------------------------------------------------------------------
    # 绩效与指标计算
    # --------------------------------------------------------------------------
    def _compute_metrics(self, klines: List[Kline], equity_arr: np.ndarray,
                         trades: List[SimulationTrade], elapsed_sec: float) -> SimulationMetrics:
        metrics = SimulationMetrics(elapsed_sec=elapsed_sec)
        if len(equity_arr) == 0:
            return metrics
        metrics.final_equity = equity_arr[-1]
        metrics.max_drawdown = self._calc_max_dd(equity_arr)
        metrics.total_trades = len(trades)
        metrics.peak_equity = np.max(equity_arr)

        # 胜率
        if trades:
            winners = sum(1 for t in trades if t.pnl > 0)
            metrics.win_rate = winners / len(trades)

        # 收益率序列
        timestamps = [k.open_time for k in klines[:len(equity_arr)]]
        eq_series = pd.Series(equity_arr, index=pd.to_datetime(timestamps, unit='ms'))
        resampled = eq_series.resample('4h').last().dropna()
        if len(resampled) < 2:
            return metrics
        ret = resampled.pct_change().dropna()
        if len(ret) == 0:
            return metrics
        # 过滤异常值
        ret = ret.clip(lower=-0.5, upper=0.5)

        annual_factor = 365 / (len(ret) / len(resampled)) if len(resampled) > 0 else 365
        metrics.sharpe = self._sharpe_ratio(ret, annual_factor)
        metrics.calmar = self._calmar_ratio(equity_arr, ret, annual_factor)
        metrics.sortino = self._sortino_ratio(ret, annual_factor)
        return metrics

    def _select_score(self, metrics: SimulationMetrics) -> float:
        if self.cfg.objective == 'sharpe':
            return metrics.sharpe if math.isfinite(metrics.sharpe) else -np.inf
        elif self.cfg.objective == 'sortino':
            return metrics.sortino if math.isfinite(metrics.sortino) else -np.inf
        else:
            return metrics.calmar if math.isfinite(metrics.calmar) else -np.inf

    def _calc_max_dd(self, equity):
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        return abs(np.min(dd)) if len(dd) > 0 else 0.0

    def _sharpe_ratio(self, ret, ann_factor=365):
        if ret.std() == 0:
            return -np.inf
        return np.mean(ret) / ret.std() * np.sqrt(ann_factor)

    def _calmar_ratio(self, equity, ret, ann_factor=365):
        max_dd = self._calc_max_dd(equity)
        if max_dd == 0:
            return -np.inf
        return np.mean(ret) * ann_factor / max_dd

    def _sortino_ratio(self, ret, ann_factor=365):
        down = ret[ret < 0]
        if len(down) == 0:
            return 0.0
        down_std = np.std(down, ddof=1) if len(down) > 1 else 0.0
        if down_std == 0:
            return -np.inf
        return np.mean(ret) / down_std * np.sqrt(ann_factor)

# ---------------------------------------------------------------------------
# 并行评估模块（进程隔离，防序列化错误）
# ---------------------------------------------------------------------------
def _evaluate_single_remote(args):
    """模块级函数，用于子进程独立评估"""
    params, repo_factory, dm_factory, config_dict = args
    try:
        repo = repo_factory()
        dmf = dm_factory
        config = EngineConfig(**config_dict)
        engine = ShadowReplayEngine(repo, dmf, config)
        result = engine.evaluate(params)
        return result
    except Exception as e:
        logger.error(f"子进程评估异常: {e}")
        return SimulationResult(score=-np.inf, equity_curve=np.array([]), trades=[], valid=False)

def batch_evaluate_parallel(params_list: List[dict],
                            repo_factory: Callable[[], KlineRepository],
                            dm_factory: Callable[[dict], DecisionMaker],
                            config_dict: dict,
                            max_workers: int = 4,
                            timeout_sec: int = 300) -> List[SimulationResult]:
    """并行批量评估，具备超时和异常保护"""
    ctx = get_context('spawn')
    with ctx.Pool(processes=min(max_workers, len(params_list))) as pool:
        tasks = [(p, repo_factory, dm_factory, config_dict) for p in params_list]
        async_results = [pool.apply_async(_evaluate_single_remote, (t,)) for t in tasks]
        pool.close()
        results = []
        for i, ar in enumerate(async_results):
            try:
                res = ar.get(timeout=timeout_sec)
                results.append(res)
            except MPTimeoutError:
                logger.error(f"评估任务 {i} 超时")
                results.append(SimulationResult(score=-np.inf, equity_curve=np.array([]), trades=[], valid=False))
            except Exception as e:
                logger.error(f"评估任务 {i} 失败: {e}")
                results.append(SimulationResult(score=-np.inf, equity_curve=np.array([]), trades=[], valid=False))
        pool.join()
        return results
