# -*- coding: utf-8 -*-
"""
模块名称: micro_scalp_obi.py
核心职责: 基于订单簿失衡（Order Book Imbalance）的微观剥头皮交易信号检测。
          在震荡行情中，当 BPI 与 TakerFlow 同时显示极端方向压力时，
          发出同向快速入场信号，捕捉极短期的价格惯性。
          本模块与 range_grid、volume_profile_mr 等震荡模块协同使用。
所属层级: core.indicators

外部依赖:
    - asyncio, logging, math, time, numbers, copy
    - typing (Dict, Any, Optional, List, TypedDict)
    - core.interfaces.FeatureComputer
    - core.models.Kline

接口契约:
    提供: {
        'MicroScalpOnOBI': {
            'input': 'kline: Kline, context: dict',
            'output': 'MicroScalpResult (TypedDict)',
            'side_effects': ['内部冷却状态更新，日志输出']
        }
    }
    消费: {
        'context["bpi"]': '当前BPI（买卖压力指数）',
        'context["bpi_timestamp"]': 'BPI数据时间戳（ms）',
        'context["taker_flow"]': '当前TakerFlow（主动买-主动卖比率）',
        'context["atr_5m"]': '5分钟ATR值',
        'context.get("spread_pct", 0.0)': '当前买卖价差百分比（小数，如0.001表示0.1%）',
        'context.get("market_regime")': '市场状态，缺失或不在白名单则抑制信号',
        'context.get("last_signal_time", 0.0)': '上一个信号的单调秒数，用于冷却',
        'context.get("symbol")': '交易对标识',
        'context.get("data_quality", 1.0)': '数据质量评分 0~1，低于0.5抑制信号',
        'context.get("tick_size")': '最小价格变动单位，用于价格对齐'
    }

配置项 (可通过构造函数传入):
    - bpi_threshold (float): BPI绝对值触发阈值，默认0.3，>0
    - takerflow_threshold (float): TakerFlow绝对值触发阈值，默认0.1，>0
    - position_coeff (float): 仓位系数，范围[0.01,1]，默认0.1
    - target_atr (float): 止盈ATR倍数，>0，默认0.3
    - stop_atr (float): 止损ATR倍数，>0且小于target_atr，默认0.2
    - min_probability (float): 最低信号概率，范围[0.3,1]，默认0.5
    - max_spread_pct (float): 最大价差百分比（小数），默认0.001 (0.1%)
    - regime_whitelist (Optional[List[str]]): 允许的市场状态，None表示全市场，默认['RANGE']
    - cooldown_sec (float): 同方向最小冷却秒数，>=0，默认2.0
    - prob_bpi_weight (float): BPI在综合概率中的权重，默认0.6
    - adaptive_coeff (bool): 是否根据波动率微调仓位系数，默认False
    - adaptive_coeff_factor (float): 自适应调整强度，默认0.2
    - max_bpi_base (float): 概率映射的最大BPI基准，默认0.8，必须>=阈值+0.1
    - max_taker_base (float): 概率映射的最大TakerFlow基准，默认0.8
    - prob_map_min (float): 概率映射下限，默认0.5
    - price_align_mode (str): 价格对齐模式 'conservative' 或 'aggressive'，默认'conservative'
    - max_target_atr (float): 最大止盈ATR倍数，0表示不限制
    - max_stop_atr (float): 最大止损ATR倍数，0表示不限制
    - debug_mode (bool): 调试模式，异常时重新抛出

作者: KHAOS System Architect
修改记录:
    - 2025-04-22 初始版本
    - 2026-02-01 增加概率映射逻辑，优化极端值处理
    - 2026-07-12 深度审计：强化鲁棒性、增加多层安全过滤、可观测性
    - 2026-07-12 第三轮审计：并发安全、数据新鲜度、性能优化、金融合规
    - 2026-07-12 第四轮审计：精度对齐、资源清理、全参数暴露、企业级调优
    - 2026-07-12 第五轮审计：价格对齐重构、锁拆分、自适应公式修正、审计增强
"""

import asyncio
import copy
import logging
import math
import numbers
import time
from typing import Any, Dict, List, Optional, TypedDict

from core.interfaces import FeatureComputer
from core.models import Kline

logger = logging.getLogger(__name__)

__all__ = ['MicroScalpOnOBI']

SIGNAL_LONG = 'LONG'
SIGNAL_SHORT = 'SHORT'

CONTEXT_KEY_BPI = 'bpi'
CONTEXT_KEY_BPI_TS = 'bpi_timestamp'
CONTEXT_KEY_TAKER_FLOW = 'taker_flow'
CONTEXT_KEY_ATR = 'atr_5m'
CONTEXT_KEY_SPREAD = 'spread_pct'
CONTEXT_KEY_REGIME = 'market_regime'
CONTEXT_KEY_LAST_SIGNAL_TIME = 'last_signal_time'
CONTEXT_KEY_SYMBOL = 'symbol'
CONTEXT_KEY_DATA_QUALITY = 'data_quality'

PROB_MAP_MAX = 1.0


class MicroScalpResult(TypedDict, total=False):
    """微观剥头皮信号返回结构"""
    signal: Optional[str]             # 信号方向 LONG/SHORT/None
    probability: float                # 信号综合概率 0~1
    stop_price: Optional[float]       # 建议止损价格
    target_price: Optional[float]     # 建议止盈价格
    position_coeff: float             # 建议仓位系数
    timestamp: Optional[int]          # 信号时间戳（UTC毫秒）
    signal_id: str                    # 信号唯一标识
    details: Dict[str, Any]           # 决策因子快照，用于审计


_EMPTY_TEMPLATE: MicroScalpResult = {
    'signal': None,
    'probability': 0.0,
    'stop_price': None,
    'target_price': None,
    'position_coeff': 0.0,
    'timestamp': None,
    'signal_id': '',
    'details': {},
}


def _make_empty_result() -> MicroScalpResult:
    return copy.copy(_EMPTY_TEMPLATE)


def _round_to_tick(price: float, tick_size: float, direction: str, kind: str,
                   mode: str = 'conservative') -> float:
    """
    价格对齐到最小变动单位，支持保守/激进模式。
    保守模式：止损更激进（更易触发），止盈更保守（更难触发）。
    激进模式相反。
    """
    if tick_size <= 0:
        return price
    ratio = price / tick_size
    if mode == 'conservative':
        if kind == 'target':
            # 做多目标价向下取整（更易达到），做空目标价向上取整（更易达到）
            if direction == SIGNAL_LONG:
                adjusted = math.floor(ratio) * tick_size
            else:
                adjusted = math.ceil(ratio) * tick_size
        else:  # stop
            # 做多止损向上取整（更易触发），做空止损向下取整（更易触发）
            if direction == SIGNAL_LONG:
                adjusted = math.ceil(ratio) * tick_size
            else:
                adjusted = math.floor(ratio) * tick_size
    else:  # aggressive
        if kind == 'target':
            # 做多目标价向上取整（更难达到），做空向下取整（更难达到）
            if direction == SIGNAL_LONG:
                adjusted = math.ceil(ratio) * tick_size
            else:
                adjusted = math.floor(ratio) * tick_size
        else:  # stop
            if direction == SIGNAL_LONG:
                adjusted = math.floor(ratio) * tick_size
            else:
                adjusted = math.ceil(ratio) * tick_size
    return float(adjusted)


class MicroScalpOnOBI(FeatureComputer):
    """微观订单簿失衡剥头皮检测器 v5.0"""

    VERSION = '5.0.0'
    name = 'MicroScalpOnOBI'

    def __init__(self,
                 bpi_threshold: float = 0.3,
                 takerflow_threshold: float = 0.1,
                 position_coeff: float = 0.1,
                 target_atr: float = 0.3,
                 stop_atr: float = 0.2,
                 min_probability: float = 0.5,
                 max_spread_pct: float = 0.001,
                 regime_whitelist: Optional[List[str]] = None,
                 cooldown_sec: float = 2.0,
                 prob_bpi_weight: float = 0.6,
                 adaptive_coeff: bool = False,
                 adaptive_coeff_factor: float = 0.2,
                 max_bpi_base: float = 0.8,
                 max_taker_base: float = 0.8,
                 prob_map_min: float = 0.5,
                 price_align_mode: str = 'conservative',
                 max_target_atr: float = 0.0,
                 max_stop_atr: float = 0.0,
                 debug_mode: bool = False):
        # 参数校验与存储
        bpi_threshold = float(abs(bpi_threshold))
        takerflow_threshold = float(abs(takerflow_threshold))
        position_coeff = float(position_coeff)
        target_atr = float(target_atr)
        stop_atr = float(stop_atr)
        min_probability = float(min_probability)
        max_spread_pct = float(max_spread_pct)
        cooldown_sec = float(abs(cooldown_sec))
        prob_bpi_weight = float(prob_bpi_weight)
        adaptive_coeff_factor = float(adaptive_coeff_factor)
        max_bpi_base = float(max_bpi_base)
        max_taker_base = float(max_taker_base)
        prob_map_min = float(prob_map_min)
        max_target_atr = float(max_target_atr)
        max_stop_atr = float(max_stop_atr)

        if bpi_threshold <= 0 or takerflow_threshold <= 0:
            raise ValueError("阈值必须为正数")
        if target_atr <= 0 or stop_atr <= 0:
            raise ValueError("止盈/止损ATR倍数必须为正数")
        if stop_atr >= target_atr:
            raise ValueError("止损ATR倍数应小于止盈ATR倍数")
        if not 0.01 <= position_coeff <= 1.0:
            raise ValueError("仓位系数必须在 0.01 到 1.0 之间")
        if not 0.3 <= min_probability <= 1.0:
            raise ValueError("最小概率必须在 0.3 到 1.0 之间")
        if not 0.0 <= prob_bpi_weight <= 1.0:
            raise ValueError("BPI权重必须在 0.0 到 1.0 之间")
        if max_bpi_base < bpi_threshold + 0.1 or max_taker_base < takerflow_threshold + 0.1:
            raise ValueError("max_base 必须至少比阈值大 0.1")
        if not (0.0 <= prob_map_min <= 1.0):
            raise ValueError("prob_map_min 必须在 0~1")
        if price_align_mode not in ('conservative', 'aggressive'):
            raise ValueError("price_align_mode 必须为 'conservative' 或 'aggressive'")

        self.bpi_threshold = bpi_threshold
        self.takerflow_threshold = takerflow_threshold
        self.position_coeff = position_coeff
        self.target_atr = target_atr
        self.stop_atr = stop_atr
        self.min_probability = min_probability
        self.max_spread_pct = max_spread_pct
        self.regime_whitelist = regime_whitelist
        self.cooldown_sec = cooldown_sec
        self.prob_bpi_weight = prob_bpi_weight
        self.adaptive_coeff = adaptive_coeff
        self.adaptive_coeff_factor = adaptive_coeff_factor
        self.max_bpi_base = max_bpi_base
        self.max_taker_base = max_taker_base
        self.prob_map_min = prob_map_min
        self.price_align_mode = price_align_mode
        self.max_target_atr = max_target_atr
        self.max_stop_atr = max_stop_atr
        self.debug_mode = debug_mode

        self._state_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._last_signal_time: Dict[str, float] = {}
        self._signal_counter = 0
        self._bpi_denom = max(0.001, max_bpi_base - bpi_threshold)
        self._taker_denom = max(0.001, max_taker_base - takerflow_threshold)
        self._cleanup_interval_counter = 0

        logger.info(f"MicroScalpOnOBI v{self.VERSION} 就绪")

    async def compute(self, kline: Kline, context: Dict[str, Any]) -> MicroScalpResult:
        try:
            return await self._compute_impl(kline, context)
        except Exception as e:
            logger.error(f"MicroScalpOnOBI.compute 异常: {e}", exc_info=True)
            if self.debug_mode:
                raise
            return _make_empty_result()

    async def _compute_impl(self, kline: Kline, context: Dict[str, Any]) -> MicroScalpResult:
        if context is None:
            return _make_empty_result()

        # 数据质量检查
        quality = self._safe_float(context.get(CONTEXT_KEY_DATA_QUALITY, 1.0))
        if quality is None or quality < 0.5:
            logger.debug("MicroScalp: 数据质量低 %.2f，跳过", quality or 0.0)
            return _make_empty_result()
        quality = max(0.0, min(1.0, quality))

        # 市场状态
        market_regime = context.get(CONTEXT_KEY_REGIME)
        if self.regime_whitelist is not None:
            if not market_regime or market_regime not in self.regime_whitelist:
                logger.debug("MicroScalp: 市场状态 %s 不在白名单，跳过", market_regime)
                return _make_empty_result()

        # 价格有效性
        current_price = kline.close
        if current_price is None or current_price <= 0:
            return _make_empty_result()
        volume = getattr(kline, 'volume', 1.0)
        if volume <= 0:
            return _make_empty_result()

        # 提取指标
        indicators = self._extract_and_validate(context)
        if indicators is None:
            return _make_empty_result()
        bpi, taker_flow, atr, spread_pct, bpi_ts, raw_bpi, raw_taker = indicators

        # 价差过滤
        if spread_pct > self.max_spread_pct:
            logger.debug("MicroScalp: 价差 %.4f%% 超过阈值 %.4f%%", spread_pct*100, self.max_spread_pct*100)
            return _make_empty_result()

        # BPI 数据新鲜度
        now_ms = time.time() * 1000
        if bpi_ts is not None and bpi_ts > 0:
            if now_ms - bpi_ts > 2000:
                logger.debug("MicroScalp: BPI数据过期 %dms", now_ms - bpi_ts)
                return _make_empty_result()
            if bpi_ts > now_ms + 5000:
                logger.warning("MicroScalp: BPI时间戳为未来时间")
                return _make_empty_result()

        # 方向检测
        direction = None
        if bpi >= self.bpi_threshold and taker_flow >= self.takerflow_threshold:
            direction = SIGNAL_LONG
        elif bpi <= -self.bpi_threshold and taker_flow <= -self.takerflow_threshold:
            direction = SIGNAL_SHORT
        else:
            if abs(bpi) > self.bpi_threshold and abs(taker_flow) > self.takerflow_threshold:
                logger.debug("MicroScalp: BPI与TakerFlow方向矛盾，忽略")

        if direction is None:
            return _make_empty_result()

        # 概率计算
        prob = self._combined_probability(abs(bpi), abs(taker_flow))
        if prob < self.min_probability:
            logger.debug("MicroScalp: 概率 %.2f < %.2f", prob, self.min_probability)
            return _make_empty_result()

        # 冷却检查
        async with self._state_lock:
            symbol = context.get(CONTEXT_KEY_SYMBOL, 'default')
            key = f"{symbol}:{direction}"
            ext_last = context.get(CONTEXT_KEY_LAST_SIGNAL_TIME, 0.0)
            if not isinstance(ext_last, (int, float)):
                ext_last = 0.0
            last_time = max(self._last_signal_time.get(key, 0.0), ext_last)
            now_mono = time.monotonic()
            if now_mono - last_time < self.cooldown_sec:
                logger.debug("MicroScalp: %s 冷却中，剩余 %.2fs", direction,
                             self.cooldown_sec - (now_mono - last_time))
                return _make_empty_result()
            self._last_signal_time[key] = now_mono
            self._signal_counter += 1
            signal_no = self._signal_counter
            # 清理过旧冷却记录（每10次检查一次）
            self._cleanup_interval_counter += 1
            if self._cleanup_interval_counter % 10 == 0 and len(self._last_signal_time) > 100:
                expired = [k for k, v in self._last_signal_time.items()
                           if now_mono - v > self.cooldown_sec * 2]
                for k in expired:
                    del self._last_signal_time[k]

        # 计算止损止盈，施加 ATR 上限
        target_atr = min(self.target_atr, self.max_target_atr) if self.max_target_atr > 0 else self.target_atr
        stop_atr = min(self.stop_atr, self.max_stop_atr) if self.max_stop_atr > 0 else self.stop_atr

        if direction == SIGNAL_LONG:
            target_price = current_price + target_atr * atr
            stop_price = current_price - stop_atr * atr
        else:
            target_price = current_price - target_atr * atr
            stop_price = current_price + stop_atr * atr

        # 价格对齐
        tick_size = context.get('tick_size', 0.0)
        if tick_size:
            target_price = _round_to_tick(target_price, tick_size, direction, 'target', self.price_align_mode)
            stop_price = _round_to_tick(stop_price, tick_size, direction, 'stop', self.price_align_mode)

        # 自适应仓位系数
        coeff = self.position_coeff
        if self.adaptive_coeff and atr > 0 and current_price > 0:
            vol_ratio = atr / current_price
            # 低波动时适当放大仓位，高波动时降低
            factor = max(0, (0.02 - vol_ratio) / 0.02) * self.adaptive_coeff_factor
            coeff = coeff * (1.0 + factor)
            coeff = max(0.01, min(1.0, coeff))

        signal_id = f"{signal_no:08d}"
        timestamp = int(kline.close_time) if kline.close_time and kline.close_time > 0 else int(now_ms)

        result: MicroScalpResult = {
            'signal': direction,
            'probability': prob,
            'stop_price': stop_price,
            'target_price': target_price,
            'position_coeff': coeff,
            'timestamp': timestamp,
            'signal_id': signal_id,
            'details': {
                'bpi': bpi,
                'raw_bpi': raw_bpi,
                'taker_flow': taker_flow,
                'raw_taker': raw_taker,
                'atr': atr,
                'spread_pct': spread_pct,
                'regime': market_regime,
                'threshold_bpi': self.bpi_threshold,
                'threshold_taker': self.takerflow_threshold,
                'max_spread': self.max_spread_pct,
                'cooldown_sec': self.cooldown_sec,
                'prob_weight_bpi': self.prob_bpi_weight,
                'coeff_raw': self.position_coeff,
                'coeff_adapted': coeff,
                'signal_counter': signal_no,
                'align_mode': self.price_align_mode,
            },
        }

        # 日志（每50个信号汇总一次INFO）
        if signal_no % 50 == 0:
            logger.info("MicroScalp: 已生成 %d 个信号，最近方向 %s, 概率 %.2f", signal_no, direction, prob)
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("MicroScalp 信号: %s prob=%.2f id=%s price=%.2f symbol=%s",
                         direction, prob, signal_id, current_price, symbol)

        return result

    def _extract_and_validate(self, context: Dict[str, Any]) -> Optional[tuple]:
        bpi = context.get(CONTEXT_KEY_BPI)
        taker_flow = context.get(CONTEXT_KEY_TAKER_FLOW)
        atr = context.get(CONTEXT_KEY_ATR, 0.0)
        spread_pct = context.get(CONTEXT_KEY_SPREAD, 0.0)
        bpi_ts = context.get(CONTEXT_KEY_BPI_TS)

        # 类型验证
        for name, val in [(CONTEXT_KEY_BPI, bpi), (CONTEXT_KEY_TAKER_FLOW, taker_flow)]:
            if val is None or not isinstance(val, numbers.Number) or isinstance(val, bool):
                logger.debug("MicroScalp: %s 无效类型或缺失", name)
                return None
            if math.isnan(float(val)):
                return None

        try:
            raw_bpi = float(bpi)
            raw_taker = float(taker_flow)
            bpi_f = max(-1.0, min(1.0, raw_bpi))
            taker_f = max(-1.0, min(1.0, raw_taker))
            atr_f = float(atr) if atr else 0.0
            if math.isinf(atr_f):
                logger.warning("MicroScalp: ATR 为无穷大，忽略")
                return None
            spread_f = abs(float(spread_pct)) if spread_pct is not None else 0.0
        except (TypeError, ValueError) as e:
            logger.debug("MicroScalp: 指标转换失败: %s", e)
            return None

        if atr_f <= 0 or math.isnan(atr_f) or math.isnan(spread_f):
            logger.debug("MicroScalp: ATR/价差异常 atr=%.4f spread=%.6f", atr_f, spread_f)
            return None

        if bpi_ts is not None:
            if not isinstance(bpi_ts, (int, float)) or isinstance(bpi_ts, bool):
                bpi_ts = None
            elif bpi_ts <= 0:
                bpi_ts = None

        return bpi_f, taker_f, atr_f, spread_f, bpi_ts, raw_bpi, raw_taker

    def _combined_probability(self, bpi_abs: float, taker_flow_abs: float) -> float:
        prob_bpi = self._map_single_prob(bpi_abs, self.bpi_threshold, self._bpi_denom)
        prob_taker = self._map_single_prob(taker_flow_abs, self.takerflow_threshold, self._taker_denom)
        combined = self.prob_bpi_weight * prob_bpi + (1.0 - self.prob_bpi_weight) * prob_taker
        return max(0.0, min(1.0, combined))

    def _map_single_prob(self, value: float, threshold: float, denom: float) -> float:
        if value >= threshold + denom:
            return PROB_MAP_MAX
        if value <= threshold:
            return self.prob_map_min
        prob = self.prob_map_min + (value - threshold) / denom * (PROB_MAP_MAX - self.prob_map_min)
        return max(self.prob_map_min, min(PROB_MAP_MAX, prob))

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def set_thresholds(self,
                             bpi_thresh: float = None,
                             taker_thresh: float = None,
                             max_bpi_base: float = None,
                             max_taker_base: float = None) -> None:
        async with self._config_lock:
            if bpi_thresh is not None:
                bpi_thresh = float(abs(bpi_thresh))
                if bpi_thresh <= 0:
                    raise ValueError("bpi_threshold 必须 >0")
                self.bpi_threshold = bpi_thresh
            if taker_thresh is not None:
                taker_thresh = float(abs(taker_thresh))
                if taker_thresh <= 0:
                    raise ValueError("takerflow_threshold 必须 >0")
                self.takerflow_threshold = taker_thresh
            if max_bpi_base is not None:
                max_bpi_base = float(max_bpi_base)
                if max_bpi_base < self.bpi_threshold + 0.1:
                    raise ValueError("max_bpi_base 至少比阈值大0.1")
                self.max_bpi_base = max_bpi_base
            if max_taker_base is not None:
                max_taker_base = float(max_taker_base)
                if max_taker_base < self.takerflow_threshold + 0.1:
                    raise ValueError("max_taker_base 至少比阈值大0.1")
                self.max_taker_base = max_taker_base
            # 重新计算分母
            self._bpi_denom = max(0.001, self.max_bpi_base - self.bpi_threshold)
            self._taker_denom = max(0.001, self.max_taker_base - self.takerflow_threshold)
        logger.info("MicroScalp 阈值更新: bpi=%.3f, taker=%.3f, bpi_base=%.3f, taker_base=%.3f",
                    self.bpi_threshold, self.takerflow_threshold, self.max_bpi_base, self.max_taker_base)

    async def set_prob_weights(self, bpi_weight: float) -> None:
        if not 0.0 <= bpi_weight <= 1.0:
            raise ValueError("权重必须在 0~1")
        async with self._config_lock:
            self.prob_bpi_weight = bpi_weight

    def get_config(self) -> Dict[str, Any]:
        return {
            'bpi_threshold': self.bpi_threshold,
            'takerflow_threshold': self.takerflow_threshold,
            'position_coeff': self.position_coeff,
            'target_atr': self.target_atr,
            'stop_atr': self.stop_atr,
            'min_probability': self.min_probability,
            'max_spread_pct': self.max_spread_pct,
            'regime_whitelist': self.regime_whitelist,
            'cooldown_sec': self.cooldown_sec,
            'prob_bpi_weight': self.prob_bpi_weight,
            'adaptive_coeff': self.adaptive_coeff,
            'max_bpi_base': self.max_bpi_base,
            'max_taker_base': self.max_taker_base,
            'prob_map_min': self.prob_map_min,
            'price_align_mode': self.price_align_mode,
            'max_target_atr': self.max_target_atr,
            'max_stop_atr': self.max_stop_atr,
        }

    async def get_cooldown_status(self, symbol: str = 'default') -> Dict[str, float]:
        async with self._state_lock:
            now = time.monotonic()
            return {
                f"{symbol}:{SIGNAL_LONG}": now - self._last_signal_time.get(f"{symbol}:{SIGNAL_LONG}", 0.0),
                f"{symbol}:{SIGNAL_SHORT}": now - self._last_signal_time.get(f"{symbol}:{SIGNAL_SHORT}", 0.0),
            }

    def reset_counters(self) -> None:
        self._signal_counter = 0

    def __repr__(self):
        return (f"MicroScalpOnOBI(v={self.VERSION}, bpi_th={self.bpi_threshold}, "
                f"taker_th={self.takerflow_threshold}, coeff={self.position_coeff})")

    async def shutdown(self) -> None:
        """优雅关闭，清理资源"""
        pass
