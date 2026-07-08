# -*- coding: utf-8 -*-
"""
模块名称: sr_pipeline.py
核心职责: 支撑/阻力映射管道，以不可变方式计算高级别S/R并注入低级别上下文。
所属层级: core.engine

外部依赖:
    - asyncio, logging, math, time
    - typing (List, Dict, Optional, Tuple, Set)
    - core.interfaces (SupportResistanceComputer, FeatureContext, SRLevel)
    - core.models (Kline)

接口契约:
    提供:
        SRMappingPipeline.enrich_context(context, klines_5m, klines_15m) -> FeatureContext
        SRMappingPipeline.get_cached_sr() -> Optional[Dict]
    消费:
        SupportResistanceComputer.compute

注意:
    - 所有时间戳应为 UTC 浮点秒。
    - FeatureContext 中的 sr_levels 结构为:
        { '<tf>': {'supports': [SRLevel, ...], 'resistances': [SRLevel, ...]}, ... }
    - 调用者应使用返回的新上下文，原上下文不变。

配置项:
    - enable_confluence_detection: bool = True
    - confluence_distance_atr_mult: float = 0.3
    - compute_timeout_sec: float = 30.0

作者: KHAOS System Architect
创建日期: 2025-02-10
修改记录:
    - 2026-07-08 v32.0: 终极机构级：统一ATR处理、资源清理、类型安全、审计日志
__version__ = "1.2.0"
"""

import asyncio
import logging
import math
import time
from typing import List, Dict, Optional, Tuple, Set

from core.interfaces import SupportResistanceComputer, FeatureContext, SRLevel
from core.models import Kline

logger = logging.getLogger(__name__)
__all__ = ['SRMappingPipeline']


class SRMappingPipeline:
    """
    支撑/阻力映射管道。
    功能: 计算15分钟和5分钟S/R，注入上下文，可选重合增强。
    线程安全: 实例内部状态（错误冷却）使用 asyncio.Lock 保护。
    """

    __slots__ = (
        '_sr_5m', '_sr_15m', '_enable_confluence', '_confluence_atr_mult',
        '_compute_timeout', '_pipeline_timeout', '_last_error_time',
        '_error_cooldown_sec', '_error_lock', '_last_sr_result'
    )

    def __init__(
        self,
        sr_5m: SupportResistanceComputer,
        sr_15m: SupportResistanceComputer,
        enable_confluence_detection: bool = True,
        confluence_distance_atr_mult: float = 0.3,
        compute_timeout_sec: float = 30.0
    ):
        """
        Args:
            sr_5m: 5分钟S/R计算器实例
            sr_15m: 15分钟S/R计算器实例
            enable_confluence_detection: 是否启用多周期重合检测
            confluence_distance_atr_mult: 判定重合的距离 (ATR倍数)，范围 (0, 2.0]
            compute_timeout_sec: 单个计算器超时秒数，范围 (0, 120]

        Raises:
            ValueError: 参数无效
            TypeError: 计算器未实现接口
        """
        if sr_5m is None or sr_15m is None:
            raise ValueError("SupportResistanceComputer instances must not be None")
        if not isinstance(sr_5m, SupportResistanceComputer) or not isinstance(sr_15m, SupportResistanceComputer):
            raise TypeError("Provided objects must implement SupportResistanceComputer")
        if confluence_distance_atr_mult <= 0 or confluence_distance_atr_mult > 2.0:
            raise ValueError("confluence_distance_atr_mult must be in (0, 2.0]")
        if compute_timeout_sec <= 0 or compute_timeout_sec > 120:
            raise ValueError("compute_timeout_sec must be in (0, 120]")

        self._sr_5m = sr_5m
        self._sr_15m = sr_15m
        self._enable_confluence = enable_confluence_detection
        self._confluence_atr_mult = confluence_distance_atr_mult
        self._compute_timeout = compute_timeout_sec
        # 管道总超时略大于两个子任务之和
        self._pipeline_timeout = compute_timeout_sec * 2.5

        # 错误冷却与并发保护
        self._last_error_time: Dict[str, float] = {}
        self._error_cooldown_sec = 300  # 5分钟
        self._error_lock = asyncio.Lock()

        self._last_sr_result: Optional[Dict] = None  # 缓存最近结果供诊断

        logger.info(
            "SRMappingPipeline initialized. Confluence: %s, Dist mult: %.2f, Timeout: %.1fs",
            enable_confluence_detection, confluence_distance_atr_mult, compute_timeout_sec
        )

    # -------------------------------------------------------------------------
    # 公共接口
    # -------------------------------------------------------------------------

    async def enrich_context(
        self,
        context: FeatureContext,
        klines_5m: List[Kline],
        klines_15m: List[Kline]
    ) -> FeatureContext:
        """
        计算S/R并返回新的上下文（原上下文不变）。

        Args:
            context: 当前特征上下文，需包含 'atr_15m', 'atr_5m'。
            klines_5m: 5分钟K线列表（至少20根，允许为空）。
            klines_15m: 15分钟K线列表。

        Returns:
            新的 FeatureContext，包含更新的 sr_levels 及可能的 sr_warnings。
        """
        # 参数保护
        if klines_5m is None:
            klines_5m = []
            logger.warning("klines_5m is None, treating as empty")
        if klines_15m is None:
            klines_15m = []
            logger.warning("klines_15m is None, treating as empty")

        klines_5m = self._sanitize_klines(klines_5m)
        klines_15m = self._sanitize_klines(klines_15m)

        # 新建上下文
        new_context = dict(context)
        original_sr = context.get('sr_levels', {})
        new_sr = {k: v for k, v in original_sr.items() if k not in ('15min', '5min')}
        new_sr.update({'15min': {'supports': [], 'resistances': []},
                       '5min': {'supports': [], 'resistances': []}})
        new_context['sr_levels'] = new_sr
        new_context.setdefault('sr_warnings', [])

        start_time = time.monotonic()
        try:
            await asyncio.wait_for(
                self._run_computations(new_context, klines_5m, klines_15m),
                timeout=self._pipeline_timeout
            )
        except asyncio.TimeoutError:
            logger.error("SR pipeline timed out after %.1fs", self._pipeline_timeout)
            new_context['sr_warnings'].append("Pipeline timed out")
        except asyncio.CancelledError:
            logger.warning("SR pipeline cancelled; clearing partial results")
            new_context['sr_levels'] = {k: {'supports': [], 'resistances': []} for k in ('15min', '5min')}
            raise
        except Exception as e:
            logger.exception("Unexpected error in SR pipeline: %s", e)
            new_context['sr_warnings'].append(f"Pipeline error: {e}")
        else:
            elapsed = time.monotonic() - start_time
            logger.debug("SR pipeline completed in %.3fs", elapsed)
            self._last_sr_result = new_context['sr_levels']

        return new_context

    async def get_cached_sr(self) -> Optional[Dict]:
        """返回最近一次成功计算的 sr_levels 快照（用于诊断），可能为 None。"""
        return self._last_sr_result

    async def reset(self) -> None:
        """重置所有子计算器并清空错误冷却状态。"""
        for computer in (self._sr_5m, self._sr_15m):
            if hasattr(computer, 'reset'):
                try:
                    reset_meth = computer.reset
                    if asyncio.iscoroutinefunction(reset_meth):
                        await reset_meth()
                    else:
                        reset_meth()
                except Exception as e:
                    logger.warning("Failed to reset %s: %s", computer.__class__.__name__, e)

        async with self._error_lock:
            self._last_error_time.clear()
        logger.info("SR mapping pipeline reset")

    def __repr__(self) -> str:
        return (f"SRMappingPipeline(confluence={self._enable_confluence}, "
                f"dist_mult={self._confluence_atr_mult}, timeout={self._compute_timeout}s)")

    async def __aenter__(self):
        logger.debug("Entering SRMappingPipeline context")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            await self.reset()
        except Exception as e:
            logger.error("Error during pipeline reset in __aexit__: %s", e)
        # 不抑制原始异常
        return False

    # -------------------------------------------------------------------------
    # 内部方法
    # -------------------------------------------------------------------------

    async def _run_computations(self, new_context: Dict, klines_5m: List[Kline], klines_15m: List[Kline]) -> None:
        """执行计算序列，可能被取消。"""
        await self._compute_and_store(new_context, '15min', klines_15m, self._sr_15m)
        await self._compute_and_store(new_context, '5min', klines_5m, self._sr_5m)

        if self._enable_confluence:
            self._mark_confluence(new_context)

    async def _compute_and_store(
        self, context: Dict, key: str, klines: List[Kline], computer: SupportResistanceComputer
    ) -> None:
        """安全计算并存储S/R。"""
        if len(klines) < 20:
            self._log_cooled(f"sr_{key}", "WARNING",
                             f"Insufficient klines for {key} SR: {len(klines)}")
            context.setdefault('sr_warnings', []).append(f"Insufficient klines for {key}")
            return

        try:
            result = await asyncio.wait_for(
                computer.compute(klines, context),
                timeout=self._compute_timeout
            )
        except asyncio.TimeoutError:
            self._log_cooled(f"sr_{key}", "ERROR",
                             f"{key} SR computation timed out ({self._compute_timeout}s) with {len(klines)} klines")
            context.setdefault('sr_warnings', []).append(f"{key} SR timed out")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_cooled(f"sr_{key}", "ERROR",
                             f"{key} SR computation failed: {type(e).__name__}: {str(e).strip()}")
            context.setdefault('sr_warnings', []).append(f"{key} SR failed: {type(e).__name__}")
            return

        if result is None:
            self._log_cooled(f"sr_{key}", "WARNING", f"{key} SR computation returned None")
            return
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            self._log_cooled(f"sr_{key}", "ERROR", f"{key} SR computation returned unexpected format")
            return

        supports, resistances = result
        # 数量限制
        if len(supports) > 20:
            self._log_cooled(f"sr_{key}", "WARNING", f"Truncating supports from {len(supports)} to 20")
            supports = supports[:20]
        if len(resistances) > 20:
            self._log_cooled(f"sr_{key}", "WARNING", f"Truncating resistances from {len(resistances)} to 20")
            resistances = resistances[:20]

        supports = self._filter_invalid(supports)
        resistances = self._filter_invalid(resistances)

        context['sr_levels'][key] = {'supports': supports, 'resistances': resistances}
        logger.debug("%s SR: supports=%d, resistances=%d", key, len(supports), len(resistances))

    def _sanitize_klines(self, klines: List) -> List[Kline]:
        """过滤非Kline对象、None、时间戳异常，并排序。"""
        valid = []
        for k in klines:
            if k is None or not isinstance(k, Kline):
                continue
            ts = getattr(k, 'timestamp', None)
            if ts is None or not math.isfinite(ts) or ts < 0:
                continue
            valid.append(k)
        if len(valid) >= 2:
            # 确保时间递增
            if any(valid[i].timestamp < valid[i-1].timestamp for i in range(1, len(valid))):
                logger.warning("Klines out of order, sorting")
                valid.sort(key=lambda x: x.timestamp)
        if len(valid) < len(klines):
            logger.debug("Sanitized klines: kept %d out of %d", len(valid), len(klines))
        return valid

    @staticmethod
    def _filter_invalid(levels: List[SRLevel]) -> List[SRLevel]:
        """过滤 NaN/Inf/负价格/None/非SRLevel对象。"""
        clean = []
        for lvl in levels:
            if lvl is None or not isinstance(lvl, SRLevel):
                continue
            price = lvl.price
            if math.isfinite(price) and price > 0:
                clean.append(lvl)
        return clean

    @staticmethod
    def _safe_atr(context: FeatureContext, key: str) -> float:
        """从上下文安全获取ATR，返回正数，最小
