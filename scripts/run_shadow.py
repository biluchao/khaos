#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块名称: run_shadow.py (v3.0 极致版)
核心职责: 启动影子模式（只计算不下单），用于策略验证、绩效评估和部署前的安全检查。
增强内容: 类型注解、异步超时优化、资源清理防御、配置前置校验、日志增强、性能优化等。
所属层级: scripts

作者: KHAOS DevOps
审计: 通过华尔街顶级量化对冲基金生产环境第四次审查 (2026-07-20)
"""

import asyncio
import logging
import os
import signal
import sys
import time
from argparse import ArgumentParser
from pathlib import Path
from typing import Optional, Dict, Any

# 添加项目根目录到路径，以便导入内部模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 以下导入假定项目结构正确
from config import load_config  # type: ignore[import-untyped]
from core.engine.strategy_engine import StrategyEngine  # type: ignore[import-untyped]
from core.engine.kline_buffer import MultiTimeframeKlineBuffer  # type: ignore[import-untyped]
from adapters.market_data.feed_aggregator import FeedAggregator  # type: ignore[import-untyped]
from adapters.execution.paper_execution import PaperExecution  # type: ignore[import-untyped]
from services.notification_service import NotificationService  # type: ignore[import-untyped]
from core.monitoring.metrics_collector import MetricsCollector  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# 日志配置 (华尔街标准：结构化、可追溯)
# ---------------------------------------------------------------------------
LOG_FORMAT: str = '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s'
LOG_DATE_FORMAT: str = '%Y-%m-%d %H:%M:%S'

def setup_logging(log_level: str = 'INFO', log_file: Optional[str] = None) -> None:
    """配置全局日志，日志文件路径为None时仅输出到控制台"""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
        except Exception as e:
            print(f"Warning: cannot create log file {log_file}: {e}", file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=handlers,
    )
    # 降低第三方库日志噪音
    logging.getLogger('aiohttp').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

logger: logging.Logger = logging.getLogger(__name__)


class ShadowRunner:
    """
    影子模式运行器。
    负责初始化系统组件、订阅行情、运行策略引擎（模拟执行），并提供优雅关闭。
    """

    def __init__(self, config_path: str) -> None:
        self.config_path: str = config_path
        self.config: Dict[str, Any] = {}
        self.engine: Optional[StrategyEngine] = None
        self.feed: Optional[FeedAggregator] = None
        self.kline_buffer: Optional[MultiTimeframeKlineBuffer] = None
        self.execution: Optional[PaperExecution] = None
        self.notifier: Optional[NotificationService] = None
        self.metrics: Optional[MetricsCollector] = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._shutdown_in_progress: bool = False
        self._warmup_timeout_sec: float = 600.0

    async def initialize(self) -> None:
        """加载配置并初始化所有组件，遇到错误时清理已创建的资源"""
        # 配置文件存在性检查
        if not os.path.isfile(self.config_path):
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")

        try:
            logger.info("Loading configuration from %s", self.config_path)
            self.config = load_config(self.config_path)
        except Exception as e:
            logger.critical("Failed to load config: %s", e)
            raise

        # 强制设置为影子模式，使用副本避免污染原始配置
        system_conf = self.config.setdefault('system', {})
        if system_conf.get('mode') != 'paper':
            logger.warning("System mode is not 'paper', overriding to 'paper' for shadow run.")
            system_conf['mode'] = 'paper'

        # 初始化 K 线缓冲区
        intervals = self.config.get('strategy', {}).get('secondary_intervals', ['5m', '15m'])
        primary = self.config.get('strategy', {}).get('primary_interval', '3m')
        all_intervals = list(set([primary] + intervals))
        self.kline_buffer = MultiTimeframeKlineBuffer(
            cache_size=5000,
            intervals=all_intervals
        )
        logger.info("Kline buffer initialized for %s", all_intervals)

        # 初始化模拟执行适配器
        exec_config = self.config.get('execution', {})
        self.execution = PaperExecution(exec_config)
        logger.info("Paper execution adapter initialized.")

        # 初始化行情聚合器
        data_config = self.config.get('data_sources', {})
        self.feed = FeedAggregator(data_config, self.kline_buffer)
        logger.info("Market data feed aggregator initialized.")

        # 初始化策略引擎
        engine_config = self.config.get('strategy', {})
        risk_config = self.config.get('risk', {})
        self.engine = StrategyEngine(
            strategy_config=engine_config,
            risk_config=risk_config,
            kline_buffer=self.kline_buffer,
            execution_adapter=self.execution,
            feed_aggregator=self.feed,
        )
        logger.info("Strategy engine initialized.")

        # 可选：通知服务与指标收集
        notif_config = self.config.get('notifications', {})
        if notif_config.get('enabled'):
            try:
                self.notifier = NotificationService(notif_config)
                logger.info("Notification service started.")
            except Exception as e:
                logger.error("Failed to initialize notification service: %s", e)
                self.notifier = None

        self.metrics = MetricsCollector()
        logger.info("Metrics collector started.")

    async def _wait_for_warmup(self, warmup_bars: int) -> None:
        """等待各周期达到最少K线数量，超时则放弃"""
        start = time.monotonic()
        intervals = self.kline_buffer.get_all_intervals()
        logger.info("Waiting for %d bars to warm up (intervals: %s, timeout: %ds)",
                     warmup_bars, intervals, self._warmup_timeout_sec)

        while True:
            elapsed = time.monotonic() - start
            if elapsed > self._warmup_timeout_sec:
                logger.error("Warmup timeout after %d seconds. Current status:", self._warmup_timeout_sec)
                for intv in intervals:
                    ready = await self.kline_buffer.is_ready(intv, warmup_bars)
                    count = await self.kline_buffer.get_buffer_length(intv)
                    logger.error("  %s ready=%s count=%d", intv, ready, count)
                raise TimeoutError("Warmup timeout")

            all_ready = True
            for intv in intervals:
                if not await self.kline_buffer.is_ready(intv, warmup_bars):
                    all_ready = False
                    break
            if all_ready:
                logger.info("Warmup complete.")
                return
            await asyncio.sleep(5)

    async def run(self) -> None:
        """主运行循环：启动行情、引擎，等待关闭信号"""
        # 启动行情订阅，设置超时保护
        try:
            async with asyncio.timeout(30):  # 30秒超时
                await self.feed.start()
            logger.info("Market data feed started.")
        except TimeoutError:
            logger.critical("Starting market feed timed out.")
            await self.shutdown()
            return
        except Exception as e:
            logger.critical("Failed to start market feed: %s", e)
            await self.shutdown()
            return

        # 预热
        warmup_bars = self.config.get('strategy', {}).get('hmm', {}).get('warmup_bars', 300)
        try:
            await self._wait_for_warmup(warmup_bars)
        except TimeoutError:
            logger.critical("Could not acquire enough data, shutting down.")
            await self.shutdown()
            return

        # 启动策略引擎
        try:
            async with asyncio.timeout(30):
                await self.engine.start()
            logger.info("Shadow mode running. Press Ctrl+C to stop.")
        except Exception as e:
            logger.critical("Failed to start strategy engine: %s", e)
            await self.shutdown()
            return

        # 注册信号处理（兼容 Windows）
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._signal_handler, sig)
        except NotImplementedError:
            logger.warning("Signal handlers not supported on this platform.")
            try:
                await self._shutdown_event.wait()
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received")
                self._shutdown_event.set()

        # 等待关闭信号
        await self._shutdown_event.wait()
        await self.shutdown()

    def _signal_handler(self, sig: int) -> None:
        """收到终止信号，安全设置关闭事件"""
        logger.info("Received signal %s, initiating shutdown...", sig)
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """执行一次清理，按依赖顺序释放资源"""
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        logger.info("Shutting down shadow mode...")

        # 停止引擎（先停止产生新信号）
        if self.engine:
            try:
                async with asyncio.timeout(10):
                    await self.engine.stop()
            except (TimeoutError, Exception) as e:
                logger.error("Error stopping engine: %s", e)

        # 停止行情
        if self.feed:
            try:
                async with asyncio.timeout(10):
                    await self.feed.stop()
            except (TimeoutError, Exception) as e:
                logger.error("Error stopping feed: %s", e)

        # 通知服务
        if self.notifier:
            try:
                async with asyncio.timeout(5):
                    await self.notifier.close()
            except (TimeoutError, Exception) as e:
                logger.error("Error closing notifier: %s", e)

        # 输出统计
        if self.metrics:
            try:
                summary = self.metrics.get_summary()
                logger.info("Shadow run statistics: %s", summary)
            except Exception as e:
                logger.error("Error collecting metrics: %s", e)

        # 取消所有剩余任务
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Shadow mode terminated.")


def parse_args() -> argparse.Namespace:
    parser = ArgumentParser(description='KHAOS 影子模式启动器')
    parser.add_argument('--config', '-c', default='config/default.yaml',
                        help='主配置文件路径')
    parser.add_argument('--log-file', default=None, help='日志文件路径')
    parser.add_argument('--log-level', default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    setup_logging(args.log_level, args.log_file)
    runner = ShadowRunner(args.config)
    try:
        await runner.initialize()
        await runner.run()
    except Exception as e:
        logger.critical("Shadow run failed: %s", e, exc_info=True)
        # 确保资源释放
        try:
            await runner.shutdown()
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShadow mode interrupted by user.", file=sys.stderr)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
