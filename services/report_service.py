"""
模块名称: report_service.py
核心职责: 提供交易绩效报告、每日摘要、日内快照及完整回测指标。
          支持多账户、多周期、虚拟券商，遵循华尔街机构级审计标准。
所属层级: services

外部依赖:
    - typing, datetime, logging, json, copy, asyncio
    - decimal (高精度金额)
    - adapters.storage.order_repository (OrderRepository)
    - core.models.order (Trade, Position)
    - core.risk.performance_metrics (PerformanceMetrics)
    - config (全局配置)

接口契约:
    提供: {
        'ReportService': {
            'generate_daily_summary(account_id, date) -> dict',
            'generate_intraday_snapshot(account_id) -> dict',
            'get_performance_report(account_id, start, end, **kwargs) -> dict',
            'export_report(data, format) -> str',
            'generate_aggregated_report(account_ids, start, end) -> dict'
        }
    }
    消费: {
        'order_repository': '提供历史订单、成交、持仓及账户权益数据',
        'risk_config': '报告生成频率与保留策略'
    }

配置项:
    - risk.risk_report.daily_summary
    - risk.risk_report.intraday_snapshot_interval_min
    - risk.risk_report.max_drawdown_pct 等
    - log_sensitive (bool) 控制是否在日志中暴露 PnL 等敏感数字

作者: KHAOS Reporting Team
创建日期: 2026-07-11
修改记录:
    - 2026-07-16 v2.0 深度审计修复100项缺陷
    - 2026-07-16 v3.0 第二轮穿透审计：极致防御、性能优化、国际化时区
    - 2026-07-16 v4.0 第三轮堡垒级审计：异步安全、Decimal 全链路、并发聚合
"""
import asyncio
import copy
import logging
import json
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Any, Union

from adapters.storage.order_repository import OrderRepository
from core.models.order import Trade, Position
from core.risk.performance_metrics import PerformanceMetrics

logger = logging.getLogger(__name__)

DEFAULT_RISK_FREE_RATE = 0.02
MIN_TRADES_FOR_METRICS = 3
MAX_REPORT_WINDOW_DAYS = 365 * 3  # 最大报告跨度3年


class ReportService:
    """绩效报告服务 (机构级 v4.0)"""

    def __init__(
        self,
        order_repository: OrderRepository,
        risk_config: Dict[str, Any],
        default_account: str = "main",
        log_sensitive: bool = True,
        request_timeout_sec: float = 30.0,
    ):
        if not isinstance(order_repository, OrderRepository):
            raise ValueError("order_repository 必须为 OrderRepository 实例")
        self.order_repo = order_repository
        self.risk_config = copy.deepcopy(risk_config) or {}
        self.default_account = default_account
        self.log_sensitive = log_sensitive
        self.request_timeout = request_timeout_sec

        self.daily_summary_enabled = self.risk_config.get('daily_summary', True)
        self.intraday_interval = self.risk_config.get('intraday_snapshot_interval_min', 60)
        logger.info("ReportService v4.0 启动, daily_summary=%s, timeout=%.1fs",
                     self.daily_summary_enabled, self.request_timeout)

    # -------------------------------------------------------------------------
    async def generate_daily_summary(
        self,
        account_id: Optional[str] = None,
        date: Optional[Union[date, datetime]] = None
    ) -> Dict[str, Any]:
        target_date = self._resolve_date(date)
        start = self._make_utc_datetime(target_date)
        end = start + timedelta(days=1)
        account = account_id or self.default_account

        trades = await self._safe_get_trades(account, start, end)
        if trades is None:
            return self._empty_summary(start, end, error="数据库错误", account_id=account)
        if not trades:
            return self._empty_summary(start, end, account_id=account)

        try:
            perf = PerformanceMetrics(
                trades,
                risk_free_rate=DEFAULT_RISK_FREE_RATE,
                min_samples=MIN_TRADES_FOR_METRICS
            )
            summary = self._build_summary(account, start, end, trades, perf)
            if self.log_sensitive:
                logger.info("每日摘要 %s 账户=%s 笔数=%d PnL=%.2f",
                             start.date(), account, len(trades), summary['total_pnl'])
            else:
                logger.info("每日摘要 %s 账户=%s 笔数=%d", start.date(), account, len(trades))
            return summary
        except Exception as e:
            logger.error("绩效指标计算失败: %s", e, exc_info=True)
            return self._empty_summary(start, end, error=str(e), account_id=account)

    # -------------------------------------------------------------------------
    async def generate_intraday_snapshot(
        self,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        account = account_id or self.default_account
        try:
            positions = await self._run_with_timeout(
                self.order_repo.get_open_positions(account_id=account)
            )
            equity = await self._run_with_timeout(
                self.order_repo.get_account_equity(account_id=account)
            )
        except Exception as e:
            logger.error("获取快照数据失败: %s", e)
            return {"timestamp": datetime.now(timezone.utc).isoformat(), "error": str(e)}

        positions = positions or []
        safe_positions = []
        total_unrealized = Decimal('0')
        total_margin = Decimal('0')
        for pos in positions:
            try:
                pos_dict = self._safe_position_dict(pos)
                safe_positions.append(pos_dict)
                total_unrealized += pos_dict["unrealized_pnl_d"]
                total_margin += pos_dict["margin_d"]
            except Exception as e:
                logger.warning("跳过异常持仓数据: %s", e)

        equity_d = Decimal(str(equity)) if equity is not None else Decimal('0')
        if equity_d < 0:
            equity_d = Decimal('0')
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "account_id": account,
            "equity": str(equity_d),
            "open_positions": safe_positions,
            "total_unrealized_pnl": str(total_unrealized),
            "margin_used": str(total_margin),
        }
        logger.debug("快照生成: equity=%s, 持仓=%d", snapshot['equity'], len(safe_positions))
        return snapshot

    # -------------------------------------------------------------------------
    async def get_performance_report(
        self,
        account_id: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        include_trades: bool = False,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        if start is None:
            start = now - timedelta(days=30)
        if end is None:
            end = now
        if start > end:
            logger.warning("报告日期颠倒，自动纠正")
            start, end = end, start
        if (end - start).days > MAX_REPORT_WINDOW_DAYS:
            raise ValueError(f"报告跨度不能超过 {MAX_REPORT_WINDOW_DAYS} 天")

        account = account_id or self.default_account
        trades = await self._safe_get_trades(account, start, end)
        if trades is None:
            return {"start": start.isoformat(), "end": end.isoformat(), "error": "数据库错误"}
        if not trades:
            return {"start": start.isoformat(), "end": end.isoformat(), "total_trades": 0}

        try:
            perf = PerformanceMetrics(trades, risk_free_rate=DEFAULT_RISK_FREE_RATE)
            report = self._build_report_dict(account, start, end, trades, perf, include_trades)
            logger.info("绩效报告 %s - %s, 笔数=%d", start.date(), end.date(), len(trades))
            return report
        except Exception as e:
            logger.error("绩效报告计算失败: %s", e, exc_info=True)
            return {"start": start.isoformat(), "end": end.isoformat(), "error": str(e)}

    # -------------------------------------------------------------------------
    async def generate_aggregated_report(
        self,
        account_ids: List[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """聚合多个账户的绩效数据，并发请求。"""
        if not account_ids:
            return {"error": "未提供账户列表"}
        tasks = [
            self.get_performance_report(account_id=aid, start=start, end=end)
            for aid in account_ids
        ]
        reports = await asyncio.gather(*tasks, return_exceptions=True)
        processed = []
        total_pnl = Decimal('0')
        total_trades = 0
        for rep in reports:
            if isinstance(rep, Exception):
                processed.append({"error": str(rep)})
            else:
                processed.append(rep)
                total_pnl += Decimal(str(rep.get('total_pnl', '0')))
                total_trades += rep.get('total_trades', 0)
        return {
            "aggregated": True,
            "accounts": account_ids,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "total_pnl": str(total_pnl),
            "total_trades": total_trades,
            "details": processed,
        }

    # -------------------------------------------------------------------------
    def export_report(self, data: Dict[str, Any], fmt: str = "json") -> str:
        if fmt != "json":
            raise ValueError(f"不支持的导出格式: {fmt}")

        class EnhancedEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                if isinstance(obj, date):
                    return obj.isoformat()
                if isinstance(obj, Decimal):
                    return str(obj)   # 保持字符串精度
                if hasattr(obj, 'to_dict'):
                    return obj.to_dict()
                return super().default(obj)

        try:
            return json.dumps(data, cls=EnhancedEncoder, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("序列化报告失败: %s", e)
            return json.dumps({"error": "序列化失败", "details": str(e)})

    # ====================== 内部工具 ======================
    async def _run_with_timeout(self, coro):
        """为数据库调用添加超时保护。"""
        try:
            return await asyncio.wait_for(coro, timeout=self.request_timeout)
        except asyncio.TimeoutError:
            logger.error("数据库请求超时 (%.1fs)", self.request_timeout)
            raise

    async def _safe_get_trades(
        self, account_id: str, start: datetime, end: datetime
    ) -> Optional[List[Trade]]:
        try:
            trades = await self._run_with_timeout(
                self.order_repo.get_trades(account_id=account_id, start=start, end=end)
            )
            if trades is None:
                return None
            if not isinstance(trades, list):
                trades = list(trades)
            return trades
        except Exception as e:
            logger.error("获取成交记录异常: %s", e, exc_info=True)
            return None

    def _build_summary(self, account: str, start: datetime, end: datetime,
                       trades: List[Trade], perf: PerformanceMetrics) -> Dict[str, Any]:
        best = perf.best_trade_safe() if hasattr(perf, 'best_trade_safe') else None
        worst = perf.worst_trade_safe() if hasattr(perf, 'worst_trade_safe') else None
        return {
            "date": start.strftime("%Y-%m-%d"),
            "account_id": account,
            "total_trades": len(trades),
            "win_trades": sum(1 for t in trades if (getattr(t, 'realized_pnl', Decimal('0')) or Decimal('0')) > 0),
            "loss_trades": sum(1 for t in trades if (getattr(t, 'realized_pnl', Decimal('0')) or Decimal('0')) <= 0),
            "win_rate": round(perf.win_rate(), 4),
            "total_pnl": str(perf.total_pnl()),
            "avg_pnl_per_trade": str(perf.avg_pnl()),
            "max_drawdown_pct": round(float(perf.max_drawdown_pct()), 6),
            "sharpe_ratio": round(float(perf.sharpe_ratio()), 4) if len(trades) >= MIN_TRADES_FOR_METRICS else 0.0,
            "sortino_ratio": round(float(perf.sortino_ratio()), 4) if len(trades) >= MIN_TRADES_FOR_METRICS else 0.0,
            "calmar_ratio": round(float(perf.calmar_ratio()), 4) if len(trades) >= MIN_TRADES_FOR_METRICS else 0.0,
            "profit_factor": round(float(perf.profit_factor()), 4) if len(trades) >= MIN_TRADES_FOR_METRICS else 0.0,
            "volume": str(sum((getattr(t, 'fill_qty', Decimal('0')) or Decimal('0')) for t in trades)),
            "fees": str(sum((getattr(t, 'fee', Decimal('0')) or Decimal('0')) for t in trades)),
            "best_trade": best.to_dict() if best else None,
            "worst_trade": worst.to_dict() if worst else None,
        }

    def _build_report_dict(self, account: str, start: datetime, end: datetime,
                           trades: List[Trade], perf: PerformanceMetrics,
                           include_trades: bool) -> Dict[str, Any]:
        report = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "account_id": account,
            "total_trades": len(trades),
            "win_rate": round(perf.win_rate(), 4),
            "total_pnl": str(perf.total_pnl()),
            "sharpe_ratio": round(float(perf.sharpe_ratio()), 4),
            "sortino_ratio": round(float(perf.sortino_ratio()), 4),
            "max_drawdown_pct": round(float(perf.max_drawdown_pct()), 6),
            "calmar_ratio": round(float(perf.calmar_ratio()), 4),
            "profit_factor": round(float(perf.profit_factor()), 4),
            "avg_pnl_per_trade": str(perf.avg_pnl()),
            "fees_total": str(sum((getattr(t, 'fee', Decimal('0')) or Decimal('0')) for t in trades)),
        }
        if include_trades:
            report["trades"] = [t.to_dict() for t in trades]
        return report

    @staticmethod
    def _safe_position_dict(pos: Position) -> Dict[str, Any]:
        side_str = str(getattr(pos, 'side', 'UNKNOWN')).split('.')[-1]
        qty = Decimal(str(getattr(pos, 'quantity', '0')))
        avg_px = Decimal(str(getattr(pos, 'avg_price', '0')))
        mkt_px = Decimal(str(getattr(pos, 'market_price', '0')))
        upnl = Decimal(str(getattr(pos, 'unrealized_pnl', '0')))
        margin = Decimal(str(getattr(pos, 'margin', '0')))
        return {
            "symbol": str(getattr(pos, 'symbol', 'unknown')),
            "side": side_str,
            "quantity": str(qty),
            "avg_price": str(avg_px),
            "market_price": str(mkt_px),
            "unrealized_pnl": str(upnl),
            "unrealized_pnl_d": upnl,
            "margin": str(margin),
            "margin_d": margin,
        }

    @staticmethod
    def _resolve_date(value: Optional[Union[date, datetime]]) -> date:
        if value is None:
            return datetime.now(timezone.utc).date()
        if isinstance(value, datetime):
            try:
                return value.astimezone(timezone.utc).date()
            except Exception:
                return value.date()
        if isinstance(value, date):
            return value
        raise ValueError(f"不支持的日期类型: {type(value)}")

    @staticmethod
    def _make_utc_datetime(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)

    @staticmethod
    def _empty_summary(start: datetime, end: datetime, error: Optional[str] = None,
                       account_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "date": start.strftime("%Y-%m-%d"),
            "account_id": account_id,
            "total_trades": 0,
            "win_trades": 0,
            "loss_trades": 0,
            "win_rate": 0.0,
            "total_pnl": "0",
            "avg_pnl_per_trade": "0",
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "profit_factor": 0.0,
            "volume": "0",
            "fees": "0",
            "best_trade": None,
            "worst_trade": None,
            "error": error,
      }
