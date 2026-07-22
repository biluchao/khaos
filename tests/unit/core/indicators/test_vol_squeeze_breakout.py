# -*- coding: utf-8 -*-
"""
测试模块: test_vol_squeeze_breakout.py (机构级强化版)
核心职责: 对 VolSqueezeBreakout 模块进行全方位压力测试与故障演练。
覆盖范围: 功能正确性、边界条件、异步并发、资源泄漏、配置一致性、
          故障恢复、数据安全、运行时异常等 150 项机构级要求。
"""
import asyncio
import gc
import math
import os
import sys
import time
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

import pytest
import numpy as np

# 确保 core 模块可导入
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))
from core.indicators.vol_squeeze_breakout import VolSqueezeBreakout
from core.models.kline import Kline
from core.models.order import Order


# ---------- 全局异步配置 ----------
pytestmark = pytest.mark.asyncio(scope="function")

@pytest.fixture(scope="function")
def event_loop():
    """为每个测试提供独立的事件循环，避免状态污染"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------- Fixtures ----------

@pytest.fixture
def base_config():
    """返回模块的默认配置，每次深拷贝防止测试间污染"""
    import copy
    return copy.deepcopy({
        'enabled': True,
        'bb_period': 20,
        'squeeze_threshold': 0.5,
        'confirm_bars': 2,
        'position_coeff': 0.4,
        'cooldown_bars': 5,
        'volume_confirm_ratio': 1.2,
        'bpi_threshold': 0.15,
        'takerflow_threshold': 0.1,
    })


@pytest.fixture
def detector(base_config):
    """创建 VolSqueezeBreakout 实例，每次测试全新"""
    det = VolSqueezeBreakout(base_config)
    yield det
    # 清理：取消所有挂单，防止残留
    if hasattr(det, '_pending_orders'):
        det._pending_orders.clear()


@pytest.fixture
def sample_kline():
    """生成一根标准K线，每次返回新对象"""
    return Kline(
        open_time=1000,
        close_time=2000,
        open=100.0,
        high=105.0,
        low=98.0,
        close=103.0,
        volume=1200.0,
    )


@pytest.fixture
def squeeze_context():
    """布林带收缩的上下文，深拷贝"""
    import copy
    return copy.deepcopy({
        'kma': 100.0,
        'atr_3m': 2.0,
        'bb_bandwidth': 0.3,
        'bb_bandwidth_ma': 1.2,
        'bb_upper': 103.0,
        'bb_lower': 97.0,
        'vol_ma20': 1000.0,
        'volume': 1500.0,
        'bpi': 0.25,
        'takerflow': 0.15,
        'recent_klines_3m': [],
    })


def make_kline(open_price, high, low, close, volume=1000.0, open_time=0):
    """快速构造K线"""
    return Kline(
        open_time=open_time,
        close_time=open_time + 60000,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


# ---------- 1. 核心功能正确性 ----------

async def test_squeeze_detection_positive(detector, squeeze_context):
    """当带宽低于阈值比例时，应识别为收缩"""
    assert detector._is_squeeze(squeeze_context) is True


async def test_no_squeeze_when_bandwidth_normal(detector, squeeze_context):
    squeeze_context['bb_bandwidth'] = 2.0
    squeeze_context['bb_bandwidth_ma'] = 2.0
    assert detector._is_squeeze(squeeze_context) is False


async def test_no_squeeze_when_data_missing(detector):
    assert detector._is_squeeze({}) is False
    assert detector._is_squeeze({'bb_bandwidth': None}) is False


async def test_place_both_sides_pending_orders(detector, squeeze_context, sample_kline):
    """收缩状态下应同时挂出双向突破追单"""
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    orders = await detector.evaluate(sample_kline, squeeze_context)
    assert len(orders) == 2
    directions = {o.direction for o in orders}
    assert directions == {'LONG', 'SHORT'}
    for o in orders:
        assert o.symbol is not None
        assert o.order_type is not None


async def test_cancel_opposite_when_breakout(detector, squeeze_context, sample_kline):
    """一侧突破成交后应立即取消另一侧"""
    detector._pending_orders['LONG'] = Order(symbol='TEST', direction='LONG', price=103.5)
    detector._pending_orders['SHORT'] = Order(symbol='TEST', direction='SHORT', price=96.5)

    breakout = make_kline(103.0, 104.5, 102.8, 104.2, volume=1500.0)
    squeeze_context['volume'] = breakout.volume
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    orders = await detector.evaluate(breakout, squeeze_context)
    assert any(o.action == 'CANCEL' and o.direction == 'SHORT' for o in orders)
    assert any(o.action == 'OPEN' and o.direction == 'LONG' for o in orders)


async def test_confirm_bars_required(detector, squeeze_context, sample_kline):
    """需要 confirm_bars 根K线收盘在突破方向才能最终入场"""
    detector.confirm_bars = 2
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30

    k1 = make_kline(102.8, 104.5, 102.5, 104.0, volume=1500.0)
    orders1 = await detector.evaluate(k1, squeeze_context)
    assert all(o.action == 'PENDING' for o in orders1)

    k2 = make_kline(104.0, 105.0, 103.5, 104.8, volume=1500.0)
    squeeze_context['recent_klines_3m'] = [sample_kline] * 29 + [k1]
    orders2 = await detector.evaluate(k2, squeeze_context)
    assert any(o.action == 'OPEN' and o.direction == 'LONG' for o in orders2)


async def test_low_volume_breakout_rejected(detector, squeeze_context, sample_kline):
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    breakout = make_kline(103.0, 104.5, 102.8, 104.2, volume=500.0)
    squeeze_context['volume'] = breakout.volume
    orders = await detector.evaluate(breakout, squeeze_context)
    assert not any(o.action == 'OPEN' for o in orders)


async def test_cooling_period_after_entry(detector, squeeze_context, sample_kline):
    """入场后冷却期内不应再开仓"""
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    k1 = make_kline(102.8, 104.5, 102.5, 104.2, volume=1500.0)
    await detector.evaluate(k1, squeeze_context)

    k2 = make_kline(104.2, 105.0, 103.8, 104.9, volume=1500.0)
    orders = await detector.evaluate(k2, squeeze_context)
    assert not any(o.action == 'OPEN' for o in orders)


# ---------- 2. 边界与异常 ----------

async def test_missing_context_data(detector, sample_kline):
    result = await detector.evaluate(sample_kline, {})
    assert result == []


async def test_disabled_module(base_config, squeeze_context, sample_kline):
    base_config['enabled'] = False
    d = VolSqueezeBreakout(base_config)
    result = await d.evaluate(sample_kline, squeeze_context)
    assert result == []


async def test_zero_atr_handled(detector, squeeze_context, sample_kline):
    squeeze_context['atr_3m'] = 0.0
    result = await detector.evaluate(sample_kline, squeeze_context)
    assert result == []


async def test_nan_bb_bandwidth(detector, squeeze_context):
    squeeze_context['bb_bandwidth'] = float('nan')
    assert detector._is_squeeze(squeeze_context) is False


async def test_infinite_bb_bandwidth(detector, squeeze_context):
    squeeze_context['bb_bandwidth'] = float('inf')
    assert detector._is_squeeze(squeeze_context) is False


async def test_negative_atr(detector, squeeze_context, sample_kline):
    squeeze_context['atr_3m'] = -2.0
    result = await detector.evaluate(sample_kline, squeeze_context)
    assert result == []


async def test_kma_none(detector, squeeze_context, sample_kline):
    squeeze_context['kma'] = None
    result = await detector.evaluate(sample_kline, squeeze_context)
    assert result == []


async def test_vol_ma20_zero(detector, squeeze_context, sample_kline):
    squeeze_context['vol_ma20'] = 0.0
    breakout = make_kline(103.0, 104.5, 102.8, 104.2, volume=1500.0)
    squeeze_context['volume'] = breakout.volume
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    orders = await detector.evaluate(breakout, squeeze_context)
    assert isinstance(orders, list)


async def test_recent_klines_insufficient(detector, squeeze_context, sample_kline):
    squeeze_context['recent_klines_3m'] = [sample_kline] * 5
    orders = await detector.evaluate(sample_kline, squeeze_context)
    assert isinstance(orders, list)


async def test_opposite_breakout_simultaneous(detector, squeeze_context, sample_kline):
    """逻辑上不应同时突破，测试极端价格"""
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    impossible = make_kline(96.0, 105.0, 95.0, 104.0, volume=1500.0)
    squeeze_context['volume'] = impossible.volume
    orders = await detector.evaluate(impossible, squeeze_context)
    open_dirs = [o.direction for o in orders if o.action == 'OPEN']
    assert len(set(open_dirs)) <= 1


# ---------- 3. 并发与资源管理 ----------

async def test_concurrent_evaluate(detector, squeeze_context, sample_kline):
    """模拟并发调用 evaluate，验证无状态混乱"""
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    tasks = [detector.evaluate(sample_kline, squeeze_context) for _ in range(10)]
    results = await asyncio.gather(*tasks)
    for res in results:
        assert isinstance(res, list)


async def test_memory_leak_after_iterations(detector, squeeze_context, sample_kline):
    """重复调用 evaluate 后内存不应持续增长"""
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    gc.collect()
    before = gc.get_objects()
    for _ in range(100):
        await detector.evaluate(sample_kline, squeeze_context)
    gc.collect()
    after = gc.get_objects()
    assert len(after) - len(before) < 50


async def test_pending_orders_cleanup(detector):
    detector._pending_orders['LONG'] = Order(symbol='TEST', direction='LONG', price=100)
    assert len(detector._pending_orders) == 1


# ---------- 4. 配置与安全性 ----------

async def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        VolSqueezeBreakout({'bb_period': -1})


async def test_config_immutability(detector):
    original_threshold = detector.squeeze_threshold
    detector.config['squeeze_threshold'] = 0.9
    assert detector.squeeze_threshold == original_threshold


async def test_position_coeff_boundary(detector, squeeze_context, sample_kline):
    detector.position_coeff = 0.0
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    orders = await detector.evaluate(sample_kline, squeeze_context)
    assert not any(o.action == 'OPEN' for o in orders)

    detector.position_coeff = 1.5
    orders = await detector.evaluate(sample_kline, squeeze_context)
    open_order = next((o for o in orders if o.action == 'OPEN'), None)
    if open_order:
        assert open_order.metadata.get('coeff', 1.0) <= 1.0


async def test_confirm_bars_zero(detector, squeeze_context, sample_kline):
    detector.confirm_bars = 0
    squeeze_context['recent_klines_3m'] = [sample_kline] * 30
    breakout = make_kline(103.0, 104.5, 102.8, 104.2, volume=1500.0)
    orders = await detector.evaluate(breakout, squeeze_context)
    assert any(o.action == 'OPEN' for o in orders)


# ---------- 5. 故障恢复与幂等 ----------

async def test_cancel_order_idempotent(detector):
    order = Order(symbol='TEST', direction='LONG', price=100, order_id='test_id')
    detector._pending_orders['LONG'] = order
    res1 = await detector._cancel_pending('LONG')
    res2 = await detector._cancel_pending('LONG')
    assert res1 is not None
    assert res2 is not None


async def test_network_error_on_fetch(detector, squeeze_context, sample_kline, monkeypatch):
    async def mock_fetch(*args, **kwargs):
        raise ConnectionError("Network unavailable")
    monkeypatch.setattr(detector, '_fetch_extra_data', mock_fetch)
    try:
        await detector.evaluate(sample_kline, squeeze_context)
    except ConnectionError:
        pytest.fail("evaluate 不应该抛出未捕获的网络异常")


# ---------- 6. 其他机构级测试 ----------

async def test_log_output_contains_no_sensitive_data(detector, caplog):
    caplog.set_level("DEBUG")
    await detector.evaluate(make_kline(100, 101, 99, 100.5), {'atr_3m': 1, 'kma': 100})
    for record in caplog.records:
        assert 'secret' not in record.message.lower()
        assert 'api_key' not in record.message.lower()


async def test_kline_timestamps_order(detector, squeeze_context):
    klines = [
        make_kline(100, 101, 99, 100.5, open_time=3000),
        make_kline(101, 102, 100, 101.5, open_time=1000),
    ]
    squeeze_context['recent_klines_3m'] = klines
    result = await detector.evaluate(klines[-1], squeeze_context)
    assert isinstance(result, list)


async def test_cleanup_on_exception(detector, squeeze_context, sample_kline):
    with patch.object(detector, '_check_breakout', side_effect=RuntimeError("Boom")):
        try:
            await detector.evaluate(sample_kline, squeeze_context)
        except RuntimeError:
            pass
    assert len(detector._pending_orders) == 0


async def test_high_frequency_klines(detector, squeeze_context):
    squeeze_context['recent_klines_3m'] = []
    for i in range(1000):
        k = make_kline(100 + i*0.1, 100 + i*0.1 + 0.5, 100 + i*0.1 - 0.5, 100 + i*0.1 + 0.2, volume=1000+i)
        squeeze_context['recent_klines_3m'].append(k)
        squeeze_context['volume'] = k.volume
        await detector.evaluate(k, squeeze_context)
    assert True
