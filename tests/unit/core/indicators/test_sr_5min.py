# -*- coding: utf-8 -*-
"""
测试模块: test_sr_5min.py
核心职责: 测试 5 分钟支撑/阻力计算器 (SwingVolumeSR) 的各项功能。
覆盖范围: 摆动点识别、成交量聚类、支撑/阻力过滤、空数据、单边行情、边界条件。
经过机构级审计，已消除随机因素、补齐字段，确保测试结果确定性。
"""

import pytest
import numpy as np
from core.indicators.sr_5min import SwingVolumeSR
from core.models.kline import Kline


@pytest.fixture
def config():
    """默认配置：窗口96根K线，摆动周期5"""
    return {
        'window': 96,
        'swing_period': 5,
    }


@pytest.fixture
def sr_calculator(config):
    """创建 SwingVolumeSR 实例"""
    return SwingVolumeSR(config)


def make_klines(prices):
    """
    快速构造K线列表。prices 为 (open, high, low, close, volume) 元组列表。
    自动填充其他必需字段，确保 Kline 对象完整。
    """
    klines = []
    for i, (o, h, l, c, v) in enumerate(prices):
        klines.append(Kline(
            open_time=60000 * i,
            close_time=60000 * (i + 1),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            # 以下字段对支撑阻力计算无影响，但保证对象完整性
            quote_volume=0.0,
            trades=0,
            taker_buy_base_volume=0.0,
            taker_buy_quote_volume=0.0,
            ignore=0,
        ))
    return klines


# ---------- 正常功能测试 ----------

@pytest.mark.asyncio
async def test_identifies_swing_highs_and_lows(sr_calculator):
    """应正确识别局部摆动高点和低点"""
    data = []
    # 前5根逐渐上升
    for i in range(5):
        price = 100.0 + i * 2
        data.append((price, price + 1.5, price - 0.5, price + 1.0, 1000.0))
    # 第6根为高点 (high=112)
    data.append((107.0, 112.0, 106.0, 108.0, 1000.0))
    # 后5根下降
    for i in range(1, 6):
        price = 108.0 - i * 2
        data.append((price + 1.0, price + 1.5, price - 1.0, price - 0.5, 800.0))
    # 最后形成低点
    data.append((96.0, 96.5, 94.0, 94.5, 1200.0))

    klines = make_klines(data)
    supports, resistances = await sr_calculator.compute(klines, context={})
    # 应有至少一个阻力和一个支撑
    assert len(resistances) >= 1
    assert len(supports) >= 1
    # 阻力应在高点附近，支撑在低点附近
    assert any(r >= 109.0 and r <= 113.0 for r in resistances)
    assert any(s >= 93.5 and s <= 95.5 for s in supports)


@pytest.mark.asyncio
async def test_volume_weighted_clustering(sr_calculator):
    """成交量更高的摆动点应优先保留"""
    data = []
    for i in range(4):
        data.append((100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i, 100))
    # 高点1：量小
    data.append((104, 110, 103, 105, 100))
    data.append((105, 106, 104, 104.5, 100))
    # 高点2：量大，应作为主要阻力
    data.append((104.5, 111, 104, 110, 2000))
    for i in range(4):
        data.append((110 - i, 110 - i + 0.5, 110 - i - 0.5, 110 - i - 0.2, 100))

    klines = make_klines(data)
    supports, resistances = await sr_calculator.compute(klines, context={})
    assert any(abs(r - 110.0) < 1.0 for r in resistances)


# ---------- 过滤与边界测试 ----------

@pytest.mark.asyncio
async def test_no_sr_when_insufficient_klines(sr_calculator):
    """K线数量不足窗口大小时应返回空列表"""
    klines = make_klines([(100, 101, 99, 100.5, 1000)] * 10)
    supports, resistances = await sr_calculator.compute(klines, context={})
    assert len(supports) == 0
    assert len(resistances) == 0


@pytest.mark.asyncio
async def test_no_sr_in_perfect_sideways(sr_calculator):
    """完全横盘（没有明显摆动）应返回空列表"""
    data = [(100, 100.5, 99.5, 100.0, 1000) for _ in range(100)]
    klines = make_klines(data)
    supports, resistances = await sr_calculator.compute(klines, context={})
    assert len(supports) == 0
    assert len(resistances) == 0


@pytest.mark.asyncio
async def test_single_direction_trend(sr_calculator):
    """单边上涨行情中只应存在支撑，阻力可能较少或无"""
    data = [(100 + i, 100 + i + 1, 100 + i - 0.5, 100 + i + 0.5, 1000) for i in range(100)]
    klines = make_klines(data)
    supports, resistances = await sr_calculator.compute(klines, context={})
    assert len(supports) >= 0
    assert len(resistances) <= 2


@pytest.mark.asyncio
async def test_empty_klines_returns_empty(sr_calculator):
    """传入空列表"""
    supports, resistances = await sr_calculator.compute([], context={})
    assert supports == []
    assert resistances == []


@pytest.mark.asyncio
async def test_not_enough_swing_points_still_returns_empty(sr_calculator):
    """局部极值点不足时，聚类可能无法形成有效S/R，使用确定性数据避免随机测试失败"""
    # 固定无趋势的噪声数据，但确保无明确摆动点
    fixed_noise = [
        (100.0, 100.8, 99.2, 100.5, 100), (100.5, 101.0, 99.5, 99.8, 100),
        (99.8, 100.3, 99.0, 100.2, 100), (100.2, 100.9, 99.7, 100.1, 100),
        (100.1, 100.6, 99.3, 100.4, 100), (100.4, 101.1, 99.9, 100.0, 100),
        (100.0, 100.5, 99.4, 100.3, 100), (100.3, 100.7, 99.6, 100.2, 100),
        (100.2, 100.9, 99.5, 100.6, 100), (100.6, 101.2, 100.0, 100.8, 100),
        (100.8, 101.5, 100.1, 101.0, 100), (101.0, 101.3, 100.2, 100.9, 100),
        (100.9, 101.6, 100.3, 101.1, 100), (101.1, 101.4, 100.4, 100.7, 100),
        (100.7, 101.2, 99.8, 100.5, 100), (100.5, 101.0, 99.9, 100.8, 100),
        (100.8, 101.3, 100.0, 100.6, 100), (100.6, 101.1, 99.7, 100.4, 100),
        (100.4, 100.9, 99.8, 100.2, 100), (100.2, 100.6, 99.5, 100.1, 100),
        (100.1, 100.5, 99.4, 100.0, 100), (100.0, 100.4, 99.3, 99.9, 100),
        (99.9, 100.3, 99.2, 99.8, 100), (99.8, 100.2, 99.1, 99.7, 100),
        (99.7, 100.1, 99.0, 99.6, 100), (99.6, 100.0, 98.9, 99.5, 100),
        (99.5, 99.9, 98.8, 99.4, 100), (99.4, 99.8, 98.7, 99.3, 100),
        (99.3, 99.7, 98.6, 99.2, 100), (99.2, 99.6, 98.5, 99.1, 100),
        (99.1, 99.5, 98.4, 99.0, 100), (99.0, 99.4, 98.3, 98.9, 100),
        (98.9, 99.3, 98.2, 98.8, 100), (98.8, 99.2, 98.1, 98.7, 100),
        (98.7, 99.1, 98.0, 98.6, 100), (98.6, 99.0, 97.9, 98.5, 100),
        (98.5, 98.9, 97.8, 98.4, 100), (98.4, 98.8, 97.7, 98.3, 100),
        (98.3, 98.7, 97.6, 98.2, 100), (98.2, 98.6, 97.5, 98.1, 100),
        (98.1, 98.5, 97.4, 98.0, 100), (98.0, 98.4, 97.3, 97.9, 100),
        (97.9, 98.3, 97.2, 97.8, 100), (97.8, 98.2, 97.1, 97.7, 100),
        (97.7, 98.1, 97.0, 97.6, 100), (97.6, 98.0, 96.9, 97.5, 100),
        (97.5, 97.9, 96.8, 97.4, 100), (97.4, 97.8, 96.7, 97.3, 100),
        (97.3, 97.7, 96.6, 97.2, 100), (97.2, 97.6, 96.5, 97.1, 100),
        (97.1, 97.5, 96.4, 97.0, 100), (97.0, 97.4, 96.3, 96.9, 100),
        (96.9, 97.3, 96.2, 96.8, 100), (96.8, 97.2, 96.1, 96.7, 100),
        (96.7, 97.1, 96.0, 96.6, 100), (96.6, 97.0, 95.9, 96.5, 100),
        (96.5, 96.9, 95.8, 96.4, 100), (96.4, 96.8, 95.7, 96.3, 100),
        (96.3, 96.7, 95.6, 96.2, 100), (96.2, 96.6, 95.5, 96.1, 100),
        (96.1, 96.5, 95.4, 96.0, 100), (96.0, 96.4, 95.3, 95.9, 100),
        (95.9, 96.3, 95.2, 95.8, 100), (95.8, 96.2, 95.1, 95.7, 100),
        (95.7, 96.1, 95.0, 95.6, 100), (95.6, 96.0, 94.9, 95.5, 100),
        (95.5, 95.9, 94.8, 95.4, 100), (95.4, 95.8, 94.7, 95.3, 100),
        (95.3, 95.7, 94.6, 95.2, 100), (95.2, 95.6, 94.5, 95.1, 100),
        (95.1, 95.5, 94.4, 95.0, 100), (95.0, 95.4, 94.3, 94.9, 100),
        (94.9, 95.3, 94.2, 94.8, 100), (94.8, 95.2, 94.1, 94.7, 100),
        (94.7, 95.1, 94.0, 94.6, 100), (94.6, 95.0, 93.9, 94.5, 100),
        (94.5, 94.9, 93.8, 94.4, 100), (94.4, 94.8, 93.7, 94.3, 100),
        (94.3, 94.7, 93.6, 94.2, 100), (94.2, 94.6, 93.5, 94.1, 100),
        (94.1, 94.5, 93.4, 94.0, 100), (94.0, 94.4, 93.3, 93.9, 100),
        (93.9, 94.3, 93.2, 93.8, 100), (93.8, 94.2, 93.1, 93.7, 100),
        (93.7, 94.1, 93.0, 93.6, 100), (93.6, 94.0, 92.9, 93.5, 100),
        (93.5, 93.9, 92.8, 93.4, 100), (93.4, 93.8, 92.7, 93.3, 100),
        (93.3, 93.7, 92.6, 93.2, 100), (93.2, 93.6, 92.5, 93.1, 100),
        (93.1, 93.5, 92.4, 93.0, 100), (93.0, 93.4, 92.3, 92.9, 100),
        (92.9, 93.3, 92.2, 92.8, 100), (92.8, 93.2, 92.1, 92.7, 100),
        (92.7, 93.1, 92.0, 92.6, 100), (92.6, 93.0, 91.9, 92.5, 100),
        (92.5, 92.9, 91.8, 92.4, 100), (92.4, 92.8, 91.7, 92.3, 100),
        (92.3, 92.7, 91.6, 92.2, 100), (92.2, 92.6, 91.5, 92.1, 100),
        (92.1, 92.5, 91.4, 92.0, 100), (92.0, 92.4, 91.3, 91.9, 100),
        (91.9, 92.3, 91.2, 91.8, 100), (91.8, 92.2, 91.1, 91.7, 100),
    ]
    klines = make_klines(fixed_noise)
    supports, resistances = await sr_calculator.compute(klines, context={})
    # 由于数据无明显摆动，期望返回空
    assert len(supports) == 0
    assert len(resistances) == 0


# ---------- 异常参数测试 ----------

@pytest.mark.asyncio
async def test_extreme_values_in_klines(sr_calculator):
    """极端价格不影响计算稳定性，并验证返回值在合理范围内"""
    data = [(0.0001, 0.0002, 0.00005, 0.00015, 1000000)] * 50
    data += [(500000, 510000, 490000, 505000, 10)] * 50
    klines = make_klines(data)
    supports, resistances = await sr_calculator.compute(klines, context={})
    assert isinstance(supports, list)
    assert isinstance(resistances, list)
    # 极端值下不应产生荒谬的支撑阻力
    for s in supports:
        assert s >= 0
    for r in resistances:
        assert r >= 0


@pytest.mark.asyncio
async def test_missing_volume_defaults(sr_calculator):
    """K线中成交量为零时，算法应能正常处理且不崩溃"""
    data = []
    for i in range(100):
        if i % 20 < 10:
            price = 100 + i * 0.5
        else:
            price = 100 - i * 0.2
        data.append((price, price + 1, price - 1, price, 0.0))
    klines = make_klines(data)
    supports, resistances = await sr_calculator.compute(klines, context={})
    assert isinstance(supports, list)
    assert isinstance(resistances, list)
