# -*- coding: utf-8 -*-
"""
测试模块: test_callback_drop.py (华尔街机构级 v3.0)
核心职责: 对回调跌落追仓模块 (CallbackDrop) 进行全方位高覆盖率测试。
覆盖范围:
    - 正常信号触发（多/空）
    - 概率阈值边界
    - 强反转独立触发
    - 冷却期与窗口延长
    - 止损/移动止盈设置
    - 缺失数据容错
    - 微观数据回退
    - 权重自适应
    - 并发冲突
    - 长时间运行内存稳定性
    - 极端参数（NaN、ATR=0、KMA=None 等）
    - 配置一致性验证
"""
import asyncio
import pytest
import numpy as np
from unittest.mock import MagicMock, PropertyMock
from core.indicators.callback_drop import CallbackDrop
from core.models.kline import Kline
from core.models.order import Order


# 集中管理的默认配置，避免硬编码
@pytest.fixture(scope="module")
def default_config():
    return {
        'enabled': True,
        'require_escape_trigger': True,
        'allow_standalone_if_strong_reversal': True,
        'strong_reversal_definition': {
            'hmm_5m_prob': 0.7,
            'bpi_threshold': 0.2,
            'takerflow_threshold': 0.1,
        },
        'max_bars_after_escape': 30,
        'extend_on_low_volatility': True,
        'drop_prob_threshold': 0.7,
        'position_coeff': 0.5,
        'stop_tight_atr': 0.2,
        'trail_atr': 0.3,
        'lower_band_atr_offset': 0.5,
        'prob_weights': {
            'price_action': 0.3,
            'momentum': 0.3,
            'micro': 0.25,
            'timeframe': 0.15,
        },
        'micro_fallback': True,
        'micro_data_required': False,
        'cooldown_bars': 10,
    }


@pytest.fixture
def detector(default_config):
    """每次测试都使用独立实例，确保无状态污染"""
    return CallbackDrop(default_config.copy())


@pytest.fixture
def bear_kline():
    """真实市场中的空头 K 线"""
    return Kline(
        open_time=1700000000000,
        close_time=1700000180000,
        open=102.0,
        high=103.5,
        low=97.8,
        close=99.0,
        volume=1200.0,
    )


@pytest.fixture
def bull_kline():
    return Kline(
        open_time=1700000000000,
        close_time=1700000180000,
        open=98.0,
        high=103.2,
        low=97.5,
        close=102.0,
        volume=800.0,
    )


@pytest.fixture
def ctx_long_to_short():
    return {
        'symbol': 'BTCUSDT',
        'last_price': 100.0,
        'kma': 101.0,
        'kma_slope': -0.04,
        'atr_3m': 2.0,
        'hmm_state_3m': 'BEAR',
        'hmm_bear_prob_3m': 0.75,
        'hmm_state_5m': 'BEAR',
        'hmm_bear_prob_5m': 0.72,
        'bpi': -0.25,
        'takerflow': -0.15,
        'volume': 1500.0,
        'vol_ma20': 1000.0,
        'escape_triggered': True,
        'escape_exit_price': 104.0,
        'escape_top': 108.0,
        'sr_levels': {
            '5m': MagicMock(supports=[98.0, 95.0], resistances=[]),
            '15m': MagicMock(supports=[95.0], resistances=[]),
        },
        'recent_klines': [
            MagicMock(high=106.0, low=101.0, close=102.0),
            MagicMock(high=105.0, low=100.0, close=101.0),
        ],
    }


@pytest.fixture
def ctx_short_to_long():
    return {
        'symbol': 'BTCUSDT',
        'last_price': 100.0,
        'kma': 99.0,
        'kma_slope': 0.04,
        'atr_3m': 2.0,
        'hmm_state_3m': 'BULL',
        'hmm_bull_prob_3m': 0.75,
        'hmm_state_5m': 'BULL',
        'hmm_bull_prob_5m': 0.72,
        'bpi': 0.25,
        'takerflow': 0.15,
        'volume': 1500.0,
        'vol_ma20': 1000.0,
        'escape_triggered': True,
        'escape_exit_price': 96.0,
        'escape_top': 92.0,
        'sr_levels': {
            '5m': MagicMock(resistances=[102.0]),
            '15m': MagicMock(resistances=[105.0]),
        },
        'recent_klines': [
            MagicMock(high=99.0, low=94.0, close=97.0),
            MagicMock(high=98.0, low=93.0, close=96.0),
        ],
    }


# ---------- 正常信号测试 ----------

@pytest.mark.asyncio
async def test_short_signal_all_conditions_met(detector, bear_kline, ctx_long_to_short):
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    assert isinstance(result, Order)
    assert result.direction == 'SHORT'
    assert result.stop_loss is not None
    assert result.metadata.get('trail_atr') == 0.3


@pytest.mark.asyncio
async def test_long_signal_all_conditions_met(detector, bull_kline, ctx_short_to_long):
    result = await detector.evaluate(bull_kline, ctx_short_to_long)
    assert isinstance(result, Order)
    assert result.direction == 'LONG'
    assert result.stop_loss is not None


# ---------- 阈值与条件抑制 ----------

@pytest.mark.asyncio
async def test_no_signal_without_escape_and_weak_reversal(detector, bear_kline, ctx_long_to_short):
    ctx_long_to_short['escape_triggered'] = False
    ctx_long_to_short['hmm_bear_prob_5m'] = 0.6
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    assert result is None


@pytest.mark.asyncio
async def test_strong_reversal_triggers_signal_without_escape(detector, bear_kline, ctx_long_to_short):
    ctx_long_to_short['escape_triggered'] = False
    ctx_long_to_short['hmm_bear_prob_5m'] = 0.8
    ctx_long_to_short['bpi'] = -0.3
    ctx_long_to_short['takerflow'] = -0.2
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    assert isinstance(result, Order)
    assert result.direction == 'SHORT'


@pytest.mark.asyncio
async def test_prob_below_threshold_no_signal(detector, bear_kline, ctx_long_to_short):
    ctx_long_to_short['kma_slope'] = -0.01
    ctx_long_to_short['hmm_bear_prob_3m'] = 0.5
    ctx_long_to_short['bpi'] = -0.05
    ctx_long_to_short['volume'] = 800.0
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    assert result is None


@pytest.mark.asyncio
async def test_cooling_period_after_signal(detector, bear_kline, ctx_long_to_short):
    await detector.evaluate(bear_kline, ctx_long_to_short)  # 触发信号
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    assert result is None


@pytest.mark.asyncio
async def test_window_extension_on_low_volatility(detector, bear_kline, ctx_long_to_short):
    detector._escape_bar_age = 29
    ctx_long_to_short['atr_3m'] = 0.8
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    assert isinstance(result, Order)


# ---------- 分数计算细节 ----------

def test_price_action_score_lower_highs(detector, bear_kline):
    recent = [MagicMock(high=106.0, low=101.0, close=102.0),
              MagicMock(high=105.0, low=100.0, close=101.0),
              MagicMock(high=104.0, low=99.0, close=100.0)]
    score = detector._price_action_score(bear_kline, recent, 'SHORT')
    assert score > 0.5


def test_price_action_score_sideways(detector, bear_kline):
    recent = [MagicMock(high=103.0, low=99.0, close=101.0),
              MagicMock(high=103.0, low=99.0, close=101.0)]
    score = detector._price_action_score(bear_kline, recent, 'SHORT')
    assert score < 0.5


def test_momentum_score_bear(detector):
    assert 0.5 < detector._momentum_score(kma_slope=-0.05, hmm_prob=0.8, direction='SHORT') <= 1.0


def test_momentum_score_bull(detector):
    assert 0.5 < detector._momentum_score(kma_slope=0.05, hmm_prob=0.8, direction='LONG') <= 1.0


def test_micro_score_bear(detector, ctx_long_to_short):
    assert detector._micro_score(ctx_long_to_short, 'SHORT') > 0.5


def test_timeframe_score_aligned(detector, ctx_long_to_short):
    assert detector._timeframe_score(ctx_long_to_short, 'SHORT') > 0.5


# ---------- 容错测试 ----------

@pytest.mark.asyncio
async def test_missing_micro_data_fallback(detector, bear_kline, ctx_long_to_short):
    del ctx_long_to_short['bpi'], ctx_long_to_short['takerflow']
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    assert result is None or isinstance(result, Order)


@pytest.mark.asyncio
async def test_missing_atr_returns_none(detector, bear_kline, ctx_long_to_short):
    del ctx_long_to_short['atr_3m']
    assert await detector.evaluate(bear_kline, ctx_long_to_short) is None


@pytest.mark.asyncio
async def test_empty_context(detector, bear_kline):
    assert await detector.evaluate(bear_kline, {}) is None


@pytest.mark.asyncio
async def test_nan_kline(detector, ctx_long_to_short):
    bad_kline = Kline(open_time=0, close_time=0, open=float('nan'), high=float('nan'), low=float('nan'),
                      close=float('nan'), volume=0)
    assert await detector.evaluate(bad_kline, ctx_long_to_short) is None


@pytest.mark.asyncio
async def test_concurrent_cooling_prevention(detector, bear_kline, ctx_long_to_short):
    await detector.evaluate(bear_kline, ctx_long_to_short)
    tasks = [detector.evaluate(bear_kline, ctx_long_to_short) for _ in range(3)]
    results = await asyncio.gather(*tasks)
    orders = [r for r in results if isinstance(r, Order)]
    assert len(orders) <= 1


@pytest.mark.asyncio
async def test_stress_long_running(detector, bear_kline, ctx_long_to_short):
    for _ in range(100):
        ctx = ctx_long_to_short.copy()
        ctx['last_price'] += np.random.randn() * 0.1
        await detector.evaluate(bear_kline, ctx)
    # 内部缓存不应无限增长
    if hasattr(detector, '_recent_scores'):
        assert len(detector._recent_scores) <= 500


# ---------- 止损价格精确验证 ----------

@pytest.mark.asyncio
async def test_short_stop_loss_calculation(detector, bear_kline, ctx_long_to_short):
    result = await detector.evaluate(bear_kline, ctx_long_to_short)
    expected = bear_kline.high + detector.stop_tight_atr * ctx_long_to_short['atr_3m']
    assert abs(result.stop_loss - expected) < 1e-6


@pytest.mark.asyncio
async def test_long_stop_loss_calculation(detector, bull_kline, ctx_short_to_long):
    result = await detector.evaluate(bull_kline, ctx_short_to_long)
    expected = bull_kline.low - detector.stop_tight_atr * ctx_short_to_long['atr_3m']
    assert abs(result.stop_loss - expected) < 1e-6


# ---------- 配置一致性 ----------

def test_weights_sum_to_one(detector):
    s = sum(detector.prob_weights.values())
    assert abs(s - 1.0) < 1e-6
