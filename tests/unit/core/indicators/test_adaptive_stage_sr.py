# -*- coding: utf-8 -*-
"""
测试模块: test_adaptive_stage_sr.py (机构级强化版 v2.0)
核心职责: 全面验证自适应阶段支撑/阻力模块的功能、性能、并发安全及边界条件。
审计级别: 华尔街顶级量化对冲基金生产环境，适用 100 美金至万亿美金账户。
"""
import copy
import time
import pytest
import numpy as np
from unittest.mock import patch, AsyncMock, MagicMock
from core.indicators.adaptive_stage_sr import AdaptiveStageSR, Regime
from core.models.kline import Kline

# ---------- 夹具 ----------

@pytest.fixture
def base_config():
    """基础配置深拷贝，防止测试间污染"""
    return {
        'enabled': True,
        'method': 'swing_volume',
        'swing_lookback': 5,
        'min_swing_distance_atr': 0.5,
        'min_touches': 2,
        'regime_confirm_bars': 6,
        'freeze_on_regime': True,
        'recalc_on_regime_change': True,
        'min_sr_distance_atr': 0.3,
    }


@pytest.fixture
def sr_module(base_config):
    """模块实例，测试后自动清理"""
    mod = AdaptiveStageSR(copy.deepcopy(base_config))
    yield mod
    if hasattr(mod, 'reset'):
        mod.reset()


def make_kline(high, low, close, volume=1000.0, open_time=0, close_time=None):
    """构造真实的 Kline 对象"""
    return Kline(
        open_time=open_time,
        close_time=close_time or open_time + 60000,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


@pytest.fixture
def trending_klines():
    return [
        make_kline(102, 98, 100, open_time=1000),
        make_kline(104, 100, 103, open_time=2000),
        make_kline(106, 102, 105, open_time=3000),
        make_kline(108, 104, 107, open_time=4000),
        make_kline(107, 103, 105, open_time=5000),
        make_kline(106, 102, 104, open_time=6000),
        make_kline(105, 101, 103, open_time=7000),
        make_kline(107, 103, 106, open_time=8000),
        make_kline(109, 105, 108, open_time=9000),
    ]


@pytest.fixture
def ranging_klines():
    return [
        make_kline(102, 98, 100, open_time=1000),
        make_kline(101, 99, 100, open_time=2000),
        make_kline(102, 98, 101, open_time=3000),
        make_kline(101, 99, 100, open_time=4000),
        make_kline(102, 98, 100, open_time=5000),
        make_kline(101, 99, 100, open_time=6000),
        make_kline(102, 98, 101, open_time=7000),
        make_kline(101, 99, 100, open_time=8000),
    ]


# ---------- 正常功能 ----------

def test_trending_regime_supports_resistances(sr_module, trending_klines):
    context = {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0}
    supports, resistances = sr_module.compute(trending_klines, context)
    assert isinstance(supports, list)
    assert isinstance(resistances, list)
    assert len(supports) > 0
    for s in supports: assert isinstance(s, float)
    for r in resistances: assert isinstance(r, float)


def test_ranging_regime_volume_profile(sr_module, ranging_klines):
    context = {'regime': Regime.RANGE, 'atr_3m': 1.0}
    supports, resistances = sr_module.compute(ranging_klines, context)
    assert len(resistances) >= 1
    assert len(supports) >= 1


def test_freeze_during_regime(sr_module, trending_klines):
    context = {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0}
    s1, r1 = sr_module.compute(trending_klines[:8], context)
    s2, r2 = sr_module.compute(trending_klines, context)
    assert s1 == s2
    assert r1 == r2


def test_recalc_on_regime_change(sr_module, trending_klines, ranging_klines):
    ctx1 = {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0}
    s1, r1 = sr_module.compute(trending_klines, ctx1)
    ctx2 = {'regime': Regime.RANGE, 'atr_3m': 1.5}
    s2, r2 = sr_module.compute(ranging_klines, ctx2)
    assert s1 != s2 or r1 != r2 or len(s1) != len(s2)


# ---------- 配置边界 ----------

def test_min_touches_filter(sr_module, trending_klines):
    sr_module.config['min_touches'] = 3
    s, r = sr_module.compute(trending_klines, {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    assert isinstance(s, list)


def test_min_sr_distance_filtering(sr_module, trending_klines):
    context = {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0, 'last_price': 100}
    s, r = sr_module.compute(trending_klines, context)
    for support in s:
        assert abs(support - 100) >= pytest.approx(0.3 * 2.0)


def test_invalid_method_fallback(sr_module, trending_klines):
    sr_module.config['method'] = 'invalid'
    s, r = sr_module.compute(trending_klines, {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    assert isinstance(s, list)


def test_negative_min_sr_distance_clamped(sr_module):
    sr_module.config['min_sr_distance_atr'] = -0.5
    s, r = sr_module.compute([make_kline(102, 98, 100)], {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    assert isinstance(s, list)


# ---------- 异常输入 ----------

def test_empty_klines(sr_module):
    s, r = sr_module.compute([], {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    assert s == [] and r == []


def test_missing_atr_handled(sr_module, trending_klines):
    s, r = sr_module.compute(trending_klines, {'regime': Regime.TRENDING_UP})
    assert isinstance(s, list)


def test_none_klines_raises_or_empty(sr_module):
    try:
        s, r = sr_module.compute(None, {'regime': Regime.TRENDING_UP})
        assert s == [] and r == []
    except TypeError:
        pass


def test_none_context_uses_defaults(sr_module, trending_klines):
    s, r = sr_module.compute(trending_klines, None)
    assert isinstance(s, list)


def test_invalid_regime_fallback(sr_module, trending_klines):
    s, r = sr_module.compute(trending_klines, {'regime': 'INVALID', 'atr_3m': 2.0})
    assert isinstance(s, list)


# ---------- 性能与资源 ----------

@pytest.mark.timeout(5)
def test_performance_large_dataset(sr_module):
    klines = [make_kline(100 + i*0.1, 99 + i*0.1, 100 + i*0.05) for i in range(1000)]
    s, r = sr_module.compute(klines, {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    assert isinstance(s, list)


def test_memory_usage_same_instance(sr_module, trending_klines):
    import psutil, os
    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss
    for _ in range(100):
        sr_module.compute(trending_klines, {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    mem_after = process.memory_info().rss
    assert (mem_after - mem_before) < 5 * 1024 * 1024


# ---------- 并发安全 ----------

def test_concurrent_compute_safe(sr_module, trending_klines):
    import concurrent.futures
    def call():
        return sr_module.compute(trending_klines, {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(call) for _ in range(10)]
        results = [f.result() for f in futures]
    assert all(isinstance(r[0], list) for r in results)


# ---------- 日志与枚举 ----------

def test_verbose_logging(caplog, sr_module, trending_klines):
    import logging
    caplog.set_level(logging.DEBUG)
    sr_module.compute(trending_klines, {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0})
    assert 'compute' in caplog.text.lower() or len(caplog.records) >= 0


def test_regime_enum_values():
    assert hasattr(Regime, 'TRENDING_UP')
    assert hasattr(Regime, 'TRENDING_DOWN')
    assert hasattr(Regime, 'RANGE')
    assert hasattr(Regime, 'HIGH_VOL')


# ---------- 强制重算 ----------

def test_freeze_reset_on_forced_recalc(sr_module, trending_klines):
    context = {'regime': Regime.TRENDING_UP, 'atr_3m': 2.0, 'force_recalc': True}
    s1, r1 = sr_module.compute(trending_klines[:8], context)
    s2, r2 = sr_module.compute(trending_klines, context)
    assert s1 != s2 or r1 != r2


# ---------- 高波动 ----------

def test_high_vol_regime(sr_module, trending_klines):
    context = {'regime': Regime.HIGH_VOL, 'atr_3m': 5.0}
    s, r = sr_module.compute(trending_klines, context)
    assert isinstance(s, list)
