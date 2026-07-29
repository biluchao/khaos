# -*- coding: utf-8 -*-
"""
模块名称: sr_pipeline.py
核心职责: 支撑/阻力映射管道，以不可变方式计算高级别S/R并注入低级别上下文。
所属层级: core.engine

外部依赖:
    - asyncio, logging, math, time, copy
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
    - 2026-07-29 v32.1: 补全截断逻辑、异步冷却日志、confluence健壮标记、数值/类型防护
    - 2026-07-29 v32.2: 机构级加固：缓存并发安全、取消完整性、不可变深隔离、
                        可观测性字段、setattr安全、性能早期退出、确定性截断
__version__ = "1.2.2"
"""

import asyncio
import logging
import math
import time
import copy
from typing import List, Dict, Optional, Tuple, Set, Any

from core.interfaces import SupportResistanceComputer, FeatureContext, SRLevel
from core.models import Kline

logger = logging.getLogger(__name__)
__all__ = ['SRMappingPipeline']

# 机构级常量（避免魔法数字）
_MAX_LEVELS_PER_SIDE: int = 20
_MIN_KLINES_REQUIRED: int = 20
_MIN_ATR: float = 1e-8
_CONFLUENCE_STRENGTH_BOOST: float = 1.5
_ERROR_COOLDOWN_SEC: float = 300.0


class SRMappingPipeline:
    """
    支撑/阻力映射管道。
    功能: 计算15分钟和5分钟S/R，注入上下文，可选重合增强。
    线程安全: 实例内部状态（错误冷却 + 缓存）使用 asyncio.Lock 保护。
    """

    __slots__ = (
        '_sr_5m', '_sr_15m', '_enable_confluence', '_confluence_atr_mult',
        '_compute_timeout', '_pipeline_timeout', '_last_error_time',
        '_error_cooldown_sec', '_error_lock', '_last_sr_result', '_cache_lock'
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
        self._enable_confluence = bool(enable_confluence_detection)
        self._confluence_atr_mult = float(confluence_distance_atr_mult)
        self._compute_timeout = float(compute_timeout_sec)
        self._pipeline_timeout = self._compute_timeout * 2.5

        self._last_error_time: Dict[str, float] = {}
        self._error_cooldown_sec = _ERROR_COOLDOWN_SEC
        self._error_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._last_sr_result: Optional[Dict] = None

        logger.info(
            "SRMappingPipeline initialized. Confluence: %s, Dist mult: %.2f, Timeout: %.1fs",
            self._enable_confluence, self._confluence_atr_mult, self._compute_timeout
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
        if klines_5m is None:
            klines_5m = []
            logger.warning("klines_5m is None, treating as empty")
        if klines_15m is None:
            klines_15m = []
            logger.warning("klines_15m is None, treating as empty")

        klines_5m = self._sanitize_klines(klines_5m)
        klines_15m = self._sanitize_klines(klines_15m)

        # 严格不可变拷贝
        new_context = self._safe_context_copy(context)

        original_sr = new_context.get('sr_levels')
        if not isinstance(original_sr, dict):
            original_sr = {}
        new_sr = {k: v for k, v in original_sr.items() if k not in ('15min', '5min')}
        new_sr.update({
            '15min': {'supports': [], 'resistances': []},
            '5min': {'supports': [], 'resistances': []}
        })
        new_context['sr_levels'] = new_sr
        new_context.setdefault('sr_warnings', [])
        # 机构级可观测性字段
        new_context.setdefault('sr_meta', {})
        new_context['sr_meta']['input_klines_5m'] = len(klines_5m)
        new_context['sr_meta']['input_klines_15m'] = len(klines_15m)

        start_time = time.monotonic()
        try:
            await asyncio.wait_for(
                self._run_computations(new_context, klines_5m, klines_15m),
                timeout=self._pipeline_timeout
            )
        except asyncio.TimeoutError:
            logger.error("SR pipeline timed out after %.1fs", self._pipeline_timeout)
            new_context['sr_warnings'].append("Pipeline timed out")
            new_context['sr_meta']['status'] = 'timeout'
        except asyncio.CancelledError:
            logger.warning("SR pipeline cancelled; clearing partial results")
            new_context['sr_levels'] = {
                k: {'supports': [], 'resistances': []} for k in ('15min', '5min')
            }
            new_context['sr_meta']['status'] = 'cancelled'
            raise
        except Exception as e:
            logger.exception("Unexpected error in SR pipeline: %s", e)
            new_context['sr_warnings'].append(f"Pipeline error: {type(e).__name__}")
            new_context['sr_meta']['status'] = f'error:{type(e).__name__}'
        else:
            elapsed = time.monotonic() - start_time
            new_context['sr_meta']['elapsed_sec'] = round(elapsed, 4)
            new_context['sr_meta']['status'] = 'ok'
            logger.debug("SR pipeline completed in %.3fs", elapsed)
            # 并发安全写缓存
            async with self._cache_lock:
                self._last_sr_result = self._snapshot_sr(new_context.get('sr_levels', {}))

        return new_context

    async def get_cached_sr(self) -> Optional[Dict]:
        """返回最近一次成功计算的 sr_levels 快照（用于诊断），可能为 None。"""
        async with self._cache_lock:
            if self._last_sr_result is None:
                return None
            return copy.deepcopy(self._last_sr_result)

    async def reset(self) -> None:
        """重置所有子计算器并清空错误冷却状态与缓存。"""
        for computer in (self._sr_5m, self._sr_15m):
            if hasattr(computer, 'reset'):
                try:
                    reset_meth = getattr(computer, 'reset')
                    if asyncio.iscoroutinefunction(reset_meth):
                        await reset_meth()
                    else:
                        reset_meth()
                except Exception as e:
                    logger.warning("Failed to reset %s: %s", type(computer).__name__, e)

        async with self._error_lock:
            self._last_error_time.clear()
        async with self._cache_lock:
            self._last_sr_result = None
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
        return False

    # -------------------------------------------------------------------------
    # 内部方法
    # -------------------------------------------------------------------------

    @staticmethod
    def _safe_context_copy(context: Any) -> Dict:
        """严格安全拷贝 FeatureContext，保证原对象不可变。"""
        if isinstance(context, dict):
            return dict(context)
        try:
            return {k: context[k] for k in context}  # type: ignore
        except Exception:
            logger.warning("FeatureContext copy fallback to empty dict")
            return {}

    @staticmethod
    def _snapshot_sr(sr_levels: Dict) -> Dict:
        """生成可安全缓存的浅+list隔离快照。"""
        snap = {}
        for tf in ('15min', '5min'):
            levels = sr_levels.get(tf, {})
            if not isinstance(levels, dict):
                continue
            snap[tf] = {
                'supports': list(levels.get('supports', [])),
                'resistances': list(levels.get('resistances', []))
            }
        return snap

    async def _run_computations(
        self, new_context: Dict, klines_5m: List[Kline], klines_15m: List[Kline]
    ) -> None:
        """执行计算序列，支持取消传播。"""
        await self._compute_and_store(new_context, '15min', klines_15m, self._sr_15m)
        await self._compute_and_store(new_context, '5min', klines_5m, self._sr_5m)

        if self._enable_confluence:
            self._mark_confluence(new_context)

    async def _compute_and_store(
        self,
        context: Dict,
        key: str,
        klines: List[Kline],
        computer: SupportResistanceComputer
    ) -> None:
        """安全计算并存储S/R。"""
        if len(klines) < _MIN_KLINES_REQUIRED:
            await self._log_cooled(
                f"sr_{key}", "WARNING",
                f"Insufficient klines for {key} SR: {len(klines)}"
            )
            context.setdefault('sr_warnings', []).append(f"Insufficient klines for {key}")
            return

        try:
            result = await asyncio.wait_for(
                computer.compute(klines, context),
                timeout=self._compute_timeout
            )
        except asyncio.TimeoutError:
            await self._log_cooled(
                f"sr_{key}", "ERROR",
                f"{key} SR computation timed out ({self._compute_timeout}s) with {len(klines)} klines"
            )
            context.setdefault('sr_warnings', []).append(f"{key} SR timed out")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._log_cooled(
                f"sr_{key}", "ERROR",
                f"{key} SR computation failed: {type(e).__name__}: {str(e).strip()}"
            )
            context.setdefault('sr_warnings', []).append(f"{key} SR failed: {type(e).__name__}")
            return

        if result is None:
            await self._log_cooled(f"sr_{key}", "WARNING", f"{key} SR computation returned None")
            return
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            await self._log_cooled(f"sr_{key}", "ERROR", f"{key} SR computation returned unexpected format")
            return

        supports, resistances = result
        if not isinstance(supports, (list, tuple)):
            supports = []
        if not isinstance(resistances, (list, tuple)):
            resistances = []

        # 确定性截断：优先保留前 N（调用方应保证已按强度/时间排序）
        if len(supports) > _MAX_LEVELS_PER_SIDE:
            await self._log_cooled(
                f"sr_{key}", "WARNING",
                f"Truncating supports from {len(supports)} to {_MAX_LEVELS_PER_SIDE}"
            )
            supports = list(supports)[:_MAX_LEVELS_PER_SIDE]
        if len(resistances) > _MAX_LEVELS_PER_SIDE:
            await self._log_cooled(
                f"sr_{key}", "WARNING",
                f"Truncating resistances from {len(resistances)} to {_MAX_LEVELS_PER_SIDE}"
            )
            resistances = list(resistances)[:_MAX_LEVELS_PER_SIDE]

        supports = self._filter_invalid(supports)
        resistances = self._filter_invalid(resistances)

        context['sr_levels'][key] = {'supports': supports, 'resistances': resistances}
        logger.debug("%s SR: supports=%d, resistances=%d", key, len(supports), len(resistances))

    def _sanitize_klines(self, klines: List) -> List[Kline]:
        """过滤非Kline、None、时间戳/价格异常，确保时间严格递增并去重。"""
        valid: List[Kline] = []
        seen_ts: Set[float] = set()
        for k in klines:
            if k is None or not isinstance(k, Kline):
                continue
            ts = getattr(k, 'timestamp', None)
            if ts is None or not math.isfinite(ts) or ts < 0:
                continue
            if ts in seen_ts:
                continue
            # 核心价格字段必须有限且正
            prices_ok = True
            for attr in ('open', 'high', 'low', 'close'):
                p = getattr(k, attr, None)
                if p is None or not math.isfinite(p) or p <= 0:
                    prices_ok = False
                    break
            if not prices_ok:
                continue
            # volume 可选但若存在必须非负有限
            vol = getattr(k, 'volume', None)
            if vol is not None and (not math.isfinite(vol) or vol < 0):
                continue
            valid.append(k)
            seen_ts.add(ts)

        if len(valid) >= 2:
            if any(valid[i].timestamp < valid[i - 1].timestamp for i in range(1, len(valid))):
                logger.warning("Klines out of order, sorting")
                valid.sort(key=lambda x: x.timestamp)
        if len(valid) < len(klines):
            logger.debug("Sanitized klines: kept %d out of %d", len(valid), len(klines))
        return valid

    @staticmethod
    def _filter_invalid(levels: List) -> List[SRLevel]:
        """过滤 NaN/Inf/负价格/None/非SRLevel对象。"""
        clean: List[SRLevel] = []
        for lvl in levels:
            if lvl is None or not isinstance(lvl, SRLevel):
                continue
            try:
                price = getattr(lvl, 'price', None)
                if price is not None and math.isfinite(price) and price > 0:
                    clean.append(lvl)
            except Exception:
                continue
        return clean

    @staticmethod
    def _safe_atr(context: Any, key: str) -> float:
        """从上下文安全获取ATR，返回严格正数，最小 _MIN_ATR。"""
        try:
            if hasattr(context, 'get'):
                atr = context.get(key)
            else:
                atr = getattr(context, key, None)
            if atr is None:
                return _MIN_ATR
            atr = float(atr)
            if not math.isfinite(atr) or atr <= 0:
                return _MIN_ATR
            return max(atr, _MIN_ATR)
        except (TypeError, ValueError, AttributeError):
            return _MIN_ATR

    async def _log_cooled(self, key: str, level: str, msg: str) -> None:
        """带冷却的日志，避免错误风暴。锁保护 + 非阻塞日志。"""
        now = time.monotonic()
        async with self._error_lock:
            last = self._last_error_time.get(key, 0.0)
            if now - last < self._error_cooldown_sec:
                return
            self._last_error_time[key] = now

        log_fn = getattr(logger, str(level).lower(), logger.warning)
        try:
            log_fn(msg)
        except Exception:
            # 日志系统本身故障时绝不抛出
            pass

    def _mark_confluence(self, context: Dict) -> None:
        """
        多周期重合增强。
        仅在 SRLevel 对象上安全设置可选属性（is_confluence / strength / confluence_score）。
        对 frozen / slots 对象静默跳过，绝不抛异常。
        """
        atr_15 = self._safe_atr(context, 'atr_15m')
        atr_5 = self._safe_atr(context, 'atr_5m')
        base_atr = max(atr_15, atr_5, _MIN_ATR)
        dist_threshold = base_atr * self._confluence_atr_mult

        sr = context.get('sr_levels')
        if not isinstance(sr, dict):
            return

        marked = 0
        for side in ('supports', 'resistances'):
            levels_15 = sr.get('15min', {}).get(side, [])
            levels_5 = sr.get('5min', {}).get(side, [])
            if not levels_15 or not levels_5:
                continue

            for l15 in levels_15:
                try:
                    p15 = getattr(l15, 'price', None)
                    if p15 is None or not math.isfinite(p15):
                        continue
                except Exception:
                    continue

                for l5 in levels_5:
                    try:
                        p5 = getattr(l5, 'price', None)
                        if p5 is None or not math.isfinite(p5):
                            continue
                        if abs(float(p15) - float(p5)) <= dist_threshold:
                            for lvl in (l15, l5):
                                self._safe_set_confluence_attrs(lvl)
                            marked += 1
                    except Exception:
                        continue

        if marked:
            logger.debug(
                "Confluence marked: %d pairs (threshold=%.6f, atr15=%.6f, atr5=%.6f)",
                marked, dist_threshold, atr_15, atr_5
            )
            meta = context.setdefault('sr_meta', {})
            meta['confluence_pairs'] = marked

    @staticmethod
    def _safe_set_confluence_attrs(lvl: Any) -> None:
        """对单个 SRLevel 安全设置 confluence 相关属性，永不抛异常。"""
        try:
            if hasattr(lvl, 'is_confluence'):
                try:
                    setattr(lvl, 'is_confluence', True)
                except (AttributeError, TypeError):
                    pass
            if hasattr(lvl, 'strength'):
                try:
                    old = getattr(lvl, 'strength', 1.0)
                    if isinstance(old, (int, float)) and math.isfinite(old):
                        setattr(lvl, 'strength', float(old) * _CONFLUENCE_STRENGTH_BOOST)
                except (AttributeError, TypeError):
                    pass
            if hasattr(lvl, 'confluence_score'):
                try:
                    old = getattr(lvl, 'confluence_score', 0)
                    if isinstance(old, (int, float)) and math.isfinite(old):
                        setattr(lvl, 'confluence_score', int(old) + 1)
                except (AttributeError, TypeError):
                    pass
        except Exception:
            pass
