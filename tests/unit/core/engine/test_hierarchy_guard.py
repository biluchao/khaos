# -*- coding: utf-8 -*-
"""
测试模块: test_hierarchy_guard.py (机构级增强版)
核心职责: 全面验证 HierarchyGuard 的正确性、健壮性、并发安全性及资源管理。
覆盖范围: 合法/非法映射、上下文过滤、配置动态更新、并发压力、异常注入、
         内存泄漏检测、性能基准、序列化安全、兼容性。
"""
import pytest
import threading
import asyncio
import copy
import time
import gc
import weakref
from core.engine.hierarchy_guard import HierarchyGuard

# ======================== Fixtures ========================
@pytest.fixture
def strict_guard():
    return HierarchyGuard(strict=True)

@pytest.fixture
def permissive_guard():
    return HierarchyGuard(strict=False)

@pytest.fixture
def full_context():
    return {
        'sr_levels': {
            '5m': {'supports': [100.0], 'resistances': [105.0]},
            '15m': {'supports': [95.0], 'resistances': [110.0]},
        },
        'regime_states': {
            '5m': 'BULL',
            '15m': 'TRENDING_UP',
        },
        'hmm_states': {
            '5m': 'BULL',
            '15m': 'RANGE',
        },
        'other_data': 'should be removed',
        'timestamp': 1234567890,
    }

# ======================== 基础功能测试 ========================
class TestBasicValidation:
    def test_5m_receives_15m(self, strict_guard):
        assert strict_guard.validate_context_injection('5m', '15m')

    def test_3m_receives_5m(self, strict_guard):
        assert strict_guard.validate_context_injection('3m', '5m')

    def test_3m_blocked_from_15m(self, strict_guard):
        assert not strict_guard.validate_context_injection('3m', '15m')

    def test_15m_blocked_from_5m(self, strict_guard):
        assert not strict_guard.validate_context_injection('15m', '5m')

    def test_self_injection_blocked(self, strict_guard):
        assert not strict_guard.validate_context_injection('3m', '3m')

    def test_invalid_timeframes(self, strict_guard):
        assert not strict_guard.validate_context_injection('3m', '1h')
        assert not strict_guard.validate_context_injection('1h', '3m')

    def test_none_timeframe(self, strict_guard):
        assert not strict_guard.validate_context_injection(None, '5m')
        assert not strict_guard.validate_context_injection('5m', None)

class TestFiltering:
    def test_3m_context_strips_15m(self, strict_guard, full_context):
        filtered = strict_guard.filter_context('3m', full_context)
        assert '5m' in filtered['sr_levels']
        assert '15m' not in filtered['sr_levels']
        assert filtered['sr_levels']['5m'] == full_context['sr_levels']['5m']

    def test_5m_context_keeps_15m(self, strict_guard, full_context):
        filtered = strict_guard.filter_context('5m', full_context)
        assert '15m' in filtered['sr_levels']
        assert '5m' not in filtered['sr_levels']

    def test_15m_context_empty(self, strict_guard, full_context):
        filtered = strict_guard.filter_context('15m', full_context)
        assert filtered['sr_levels'] == {}
        assert '15m' not in filtered.get('regime_states', {})

    def test_missing_keys_handled(self, strict_guard):
        filtered = strict_guard.filter_context('3m', {'sr_levels': {'5m': {}}})
        assert 'sr_levels' in filtered

    def test_empty_input(self, strict_guard):
        assert strict_guard.filter_context('3m', {}) == {}

    def test_preserves_timestamp(self, strict_guard):
        ctx = {'timestamp': 99999}
        filtered = strict_guard.filter_context('3m', ctx)
        assert 'timestamp' in filtered  # 非敏感字段应当保留

class TestNonStrictMode:
    def test_allows_illegal_mapping(self, permissive_guard):
        assert permissive_guard.validate_context_injection('3m', '15m')

    def test_warning_count_increments(self, permissive_guard):
        initial = permissive_guard.warning_count
        permissive_guard.validate_context_injection('3m', '15m')
        assert permissive_guard.warning_count > initial

# ======================== 并发安全测试 ========================
class TestConcurrency:
    def test_validate_under_threads(self, strict_guard):
        errors = []
        def task():
            try:
                for _ in range(500):
                    assert strict_guard.validate_context_injection('3m', '5m')
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=task) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert len(errors) == 0

    def test_filter_under_threads(self, strict_guard, full_context):
        errors = []
        def task():
            try:
                for _ in range(500):
                    f = strict_guard.filter_context('3m', full_context)
                    assert isinstance(f, dict)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=task) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_mixed_async_calls(self, strict_guard, full_context):
        async def validate():
            await asyncio.sleep(0)
            return strict_guard.validate_context_injection('5m', '15m')
        async def filter_ctx():
            await asyncio.sleep(0)
            return strict_guard.filter_context('5m', full_context)
        tasks = [validate() for _ in range(50)] + [filter_ctx() for _ in range(50)]
        results = await asyncio.gather(*tasks)
        assert all(r is not None for r in results)

# ======================== 边界与异常测试 ========================
class TestEdgeCases:
    def test_very_long_timeframe_string(self, strict_guard):
        long_tf = '3m' * 1000
        assert not strict_guard.validate_context_injection(long_tf, '5m')

    def test_special_characters_in_timeframe(self, strict_guard):
        assert not strict_guard.validate_context_injection('3m\n', '5m')

    def test_context_with_nested_objects(self, strict_guard):
        ctx = {'sr_levels': {'5m': {'supports': [100.0], 'extra': object()}}}
        filtered = strict_guard.filter_context('3m', ctx)
        assert '5m' in filtered['sr_levels']

    def test_null_context_value(self, strict_guard):
        ctx = {'sr_levels': None}
        filtered = strict_guard.filter_context('3m', ctx)
        assert filtered == {}

    def test_unhashable_context_keys(self, strict_guard):
        ctx = {('tuple', 'key'): 'value'}
        filtered = strict_guard.filter_context('3m', ctx)
        # 不应抛出异常，直接忽略不可哈希的键
        assert isinstance(filtered, dict)

# ======================== 资源与内存测试 ========================
class TestResources:
    def test_memory_leak_after_many_filters(self, strict_guard, full_context):
        gc.collect()
        before = len(gc.get_objects())
        for _ in range(1000):
            strict_guard.filter_context('3m', full_context)
        gc.collect()
        after = len(gc.get_objects())
        # 允许轻微增长，但不应暴增
        assert after - before < 200

    def test_guard_garbage_collection(self):
        guard = HierarchyGuard(strict=True)
        ref = weakref.ref(guard)
        del guard
        gc.collect()
        assert ref() is None  # 无循环引用

# ======================== 配置动态更新测试 ========================
class TestDynamicConfig:
    def test_hot_reload_allowed_mapping(self, strict_guard):
        strict_guard.allowed_mapping['3m'] = ['15m']  # 动态允许越级映射
        assert strict_guard.validate_context_injection('3m', '15m')
        # 重置为原始配置
        strict_guard.allowed_mapping['3m'] = ['5m']

# ======================== 性能基准测试 ========================
class TestPerformance:
    def test_validate_latency(self, strict_guard):
        import timeit
        duration = timeit.timeit(
            lambda: strict_guard.validate_context_injection('3m', '5m'),
            number=10000
        )
        assert duration < 0.1  # 10000次调用应在0.1秒内完成

    def test_filter_latency(self, strict_guard, full_context):
        import timeit
        duration = timeit.timeit(
            lambda: strict_guard.filter_context('3m', full_context),
            number=10000
        )
        assert duration < 0.5

# ======================== 兼容性与确定性测试 ========================
class TestCompatibility:
    def test_deepcopy_guard(self, strict_guard):
        guard2 = copy.deepcopy(strict_guard)
        assert guard2.strict == strict_guard.strict
        guard2.strict = not strict_guard.strict
        assert guard2.strict != strict_guard.strict

    def test_pickling(self, strict_guard):
        import pickle
        data = pickle.dumps(strict_guard)
        guard2 = pickle.loads(data)
        assert guard2.validate_context_injection('3m', '5m') == strict_guard.validate_context_injection('3m', '5m')

    def test_deterministic_result(self, strict_guard, full_context):
        f1 = strict_guard.filter_context('3m', full_context)
        f2 = strict_guard.filter_context('3m', full_context)
        assert f1 == f2
