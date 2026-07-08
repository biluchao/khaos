# -*- coding: utf-8 -*-
from __future__ import annotations
"""
模块名称: hierarchy_guard.py
核心职责: 多时间框架层级隔离守卫，强制信息单向流动（15m→5m→3m），
         防止未来数据泄露和反向穿透，是策略安全保障的核心组件。
所属层级: core.engine

外部依赖:
    - logging, threading, collections, typing, re, copy, time, enum, datetime
    - core.interfaces (SRLevel)

接口契约:
    提供:
        - HierarchyGuard: 主类，包含验证注入、过滤上下文/S/R、动态注册映射等方法
        - HierarchyViolationError: 层级违规异常
        - KeyClassification: 上下文键分类枚举
    消费:
        - SRLevel 列表

配置项: 通过构造函数注入 strict, raise_on_violation, audit_enabled 等

作者: KHAOS System Architect
创建日期: 2025-04-10
修改记录:
    - 2026-07-08 v39.0: 经过持续穿透审计，达到华尔街级防火墙巅峰标准。
__version__ = "39.0.0"
__all__ = ["HierarchyGuard", "HierarchyViolationError", "KeyClassification"]
"""

import logging
import threading
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set, FrozenSet, Tuple, Union, Deque
from collections import OrderedDict, deque
from types import MappingProxyType
from enum import Enum
from copy import deepcopy

logger = logging.getLogger(__name__)


class HierarchyViolationError(Exception):
    """当严格层级违规且 raise_on_violation=True 时抛出。"""
    def __init__(self, target: str, source: str, message: str = ""):
        self.target = target
        self.source = source
        self.message = message
        super().__init__(f"Hierarchy violation: {source} -> {target}. {message}")

    def __str__(self) -> str:
        return f"HierarchyViolationError(target={self.target}, source={self.source}, message={self.message})"


class KeyClassification(str, Enum):
    """上下文键分类结果。"""
    OWN = "own"
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class HierarchyGuard:
    """
    多时间框架层级隔离守卫。
    
    核心规则：
    - 信息流单向：默认 15m→5m→3m（可扩展）
    - 自身引用始终允许
    - 严格模式：违规记录并可选抛出异常
    - 提供上下文和S/R数据过滤，支持动态映射管理
    
    Attributes:
        DEFAULT_MAPPING (类常量): 初始不可变映射，用于实例化。
    """

    # 默认映射（不可变，外部视图）
    DEFAULT_MAPPING: MappingProxyType = MappingProxyType({
        "15m": frozenset(),
        "5m": frozenset(["15m"]),
        "3m": frozenset(["5m"]),
        "1m": frozenset(["3m"]),
        "1h": frozenset(["15m"]),
        "4h": frozenset(["1h"]),
    })

    # 顶级周期（不允许有任何源），可通过配置覆盖
    DEFAULT_TOP_LEVEL: FrozenSet[str] = frozenset(["15m", "4h"])

    # 时间框架合法格式正则（例如 "1m", "5m", "15m", "1h", "1d", "1w"）
    TF_PATTERN = re.compile(r'^\d+[mhdw]$')

    # 已知指标前缀（用于从上下文字段键中提取时间框架）
    KNOWN_INDICATOR_PREFIXES = [
        "kma", "kma_slope", "kma_bandwidth", "atr", "hmm_state", "hmm_probabilities",
        "sr_levels", "volume_ma", "volatility_percentile"
    ]

    # 常见时间框架全称到缩写的映射
    TF_FULLNAME_MAP = {
        "1min": "1m", "3min": "3m", "5min": "5m", "15min": "15m",
        "30min": "30m", "1hour": "1h", "4hour": "4h", "1day": "1d", "1week": "1w",
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h",
        "1d": "1d", "1w": "1w"
    }

    # 最大映射边数
    MAX_TOTAL_EDGES = 500
    MAX_SOURCES_PER_TARGET = 5
    MAX_SOURCES_PER_CALL = 10
    MAX_PATH_DEPTH = 50

    # 违规记录冷却时间（秒）
    DEFAULT_LOG_COOLDOWN_SEC = 60.0
    MAX_VIOLATION_HISTORY = 200
    MAX_VIOLATION_TS_ENTRIES = 1000
    MAX_TF_LENGTH = 10
    MAX_CONTEXT_KEYS = 500

    def __init__(
        self,
        strict: bool = True,
        raise_on_violation: bool = False,
        audit_enabled: bool = False,
        log_cooldown_sec: float = DEFAULT_LOG_COOLDOWN_SEC,
        unknown_source_action: str = "remove",
        deep_copy_values: bool = False,
        enabled: bool = True,
        max_total_edges: int = MAX_TOTAL_EDGES,
        top_level_timeframes: Optional[FrozenSet[str]] = None,
        max_violation_history: int = MAX_VIOLATION_HISTORY,
    ):
        """
        Args:
            strict: 严格模式。违规时记录ERROR并拒绝。
            raise_on_violation: 严格模式下是否抛出HierarchyViolationError。
            audit_enabled: 是否记录详细审计日志。
            log_cooldown_sec: 相同违规日志冷却时间（秒）。
            unknown_source_action: 上下文过滤时遇到未知来源键的行为: 'remove','keep','warn_only'
            deep_copy_values: 过滤上下文时是否对保留的值进行深拷贝（可能影响性能）。
            enabled: 是否启用层级检查，若为False则所有注入和过滤均允许。
            max_total_edges: 最大映射边数。
            top_level_timeframes: 顶级周期集合（覆盖默认）。
            max_violation_history: 保留的最大违规记录数。
        """
        if unknown_source_action not in ("remove", "keep", "warn_only"):
            raise ValueError(f"Invalid unknown_source_action: {unknown_source_action}")
        if max_total_edges < 10:
            raise ValueError("max_total_edges must be at least 10")

        self._strict = strict
        self._raise_on_violation = raise_on_violation and strict
        if raise_on_violation and not strict:
            logger.warning("raise_on_violation has no effect when strict=False")

        self._audit_enabled = audit_enabled
        self._log_cooldown_sec = log_cooldown_sec
        self._unknown_source_action = unknown_source_action
        self._deep_copy_values = deep_copy_values
        self._enabled = enabled
        self._max_total_edges = max_total_edges
        self._top_level = top_level_timeframes if top_level_timeframes is not None else self.DEFAULT_TOP_LEVEL
        self._max_violation_history = max_violation_history

        # 使用可重入锁，避免死锁
        self._mapping_lock = threading.RLock()
        self._mapping: Dict[str, Set[str]] = {}
        self._init_mapping()

        # 违规计数与历史（线程安全）
        self._violation_count = 0
        self._violation_lock = threading.Lock()
        self._violation_history: Deque[Dict[str, Any]] = deque(maxlen=self._max_violation_history)
        self._last_violation_ts: Dict[Tuple[str, str], float] = {}
        self._last_violation_ts_lock = threading.Lock()

    def _init_mapping(self) -> None:
        """用默认映射初始化实例映射。"""
        with self._mapping_lock:
            for tf, allowed in self.DEFAULT_MAPPING.items():
                self._mapping[tf] = set(allowed)

    # =========================================================================
    # 公共接口
    # =========================================================================
    def validate_injection(self, target_tf: str, source_tf: str, raise_on_invalid: bool = True) -> bool:
        """验证是否允许从源周期向目标周期注入数据。"""
        if not self._enabled:
            return True

        target = self._normalize_tf(target_tf)
        source = self._normalize_tf(source_tf)

        if not target or not source:
            if raise_on_invalid:
                raise ValueError(f"Invalid timeframe format: target='{target_tf}', source='{source_tf}'")
            return False
        if not self._is_valid_tf(target):
            if raise_on_invalid:
                raise ValueError(f"Invalid target timeframe: '{target_tf}'")
            return False
        if not self._is_valid_tf(source):
            if raise_on_invalid:
                raise ValueError(f"Invalid source timeframe: '{source_tf}'")
            return False

        if target == source:
            return True

        allowed_sources = self._get_allowed_sources_internal(target)
        if source in allowed_sources:
            return True

        self._record_violation(target, source)
        msg = "Hierarchy violation: %s -> %s. Allowed: %s" % (source, target, allowed_sources)
        if self._strict:
            if self._raise_on_violation:
                raise HierarchyViolationError(target, source, msg)
            logger.error(msg)
        else:
            logger.warning(msg)
        return False

    def filter_sr_levels(self, target_tf: str, sr_levels: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
        """过滤支撑阻力级别字典。"""
        if not self._enabled:
            return dict(sr_levels) if sr_levels else {}

        if not sr_levels or not isinstance(sr_levels, dict):
            return {}

        target = self._normalize_tf(target_tf)
        allowed_sources = self._get_allowed_sources_internal(target)
        allowed_sources.add(target)

        filtered = OrderedDict()
        seen_norm = set()
        for source_tf, levels in sr_levels.items():
            if not isinstance(source_tf, str) or levels is None:
                continue
            norm_source = self._normalize_tf(source_tf)
            if norm_source in allowed_sources:
                if norm_source not in seen_norm:
                    seen_norm.add(norm_source)
                    filtered[source_tf] = list(levels)
                else:
                    logger.debug("Duplicate normalized source %s (orig=%s) skipped.", norm_source, source_tf)
            else:
                logger.debug("S/R from %s blocked for %s", source_tf, target_tf)

        if self._audit_enabled:
            logger.info("S/R filter: target=%s, kept keys=%s", target, list(filtered.keys()))
        return filtered

    def filter_context(
        self, target_tf: str, full_context: Dict[str, Any], dry_run: bool = False
    ) -> Dict[str, Any]:
        """过滤完整上下文字典。"""
        if not self._enabled:
            return dict(full_context) if full_context else {}

        if full_context is None:
            return {}
        if not isinstance(full_context, dict):
            logger.warning("full_context is not a dict")
            return {}

        target = self._normalize_tf(target_tf)
        if not target or not self._is_valid_tf(target):
            raise ValueError(f"Invalid target timeframe: {target_tf}")

        # 防御性限制键数
        if len(full_context) > self.MAX_CONTEXT_KEYS:
            logger.warning("Context has %d keys, exceeding limit %d. Truncating.", len(full_context), self.MAX_CONTEXT_KEYS)
            full_context = dict(list(full_context.items())[:self.MAX_CONTEXT_KEYS])

        allowed_sources = self._get_allowed_sources_internal(target)
        allowed_sources.add(target)

        filtered = OrderedDict()
        removed_keys = []

        for key, value in full_context.items():
            source_tf = self._extract_timeframe_from_key(key)
            if source_tf is None:
                action = self._unknown_source_action
                if action == "remove":
                    removed_keys.append(key)
                    if not dry_run:
                        continue
                elif action == "keep":
                    pass  # 保留
                else:  # warn_only
                    logger.warning("Unknown source for key '%s', keeping it.", key)
            else:
                if source_tf not in allowed_sources:
                    removed_keys.append(key)
                    if not dry_run:
                        continue
            # 保留
            if not dry_run:
                if self._deep_copy_values:
                    try:
                        v_size = len(str(value)) if not isinstance(value, (int, float, bool)) else 0
                        if v_size > 10_000_000:
                            logger.warning("Large value for key '%s' not deep-copied.", key)
                            filtered[key] = value
                        else:
                            filtered[key] = deepcopy(value)
                    except Exception:
                        filtered[key] = value
                else:
                    filtered[key] = value

        if removed_keys:
            if len(removed_keys) > 20:
                logger.debug("Context filter removed %d keys (showing first 10): %s", len(removed_keys), removed_keys[:10])
            else:
                logger.debug("Context filter removed keys: %s", removed_keys)
        if self._audit_enabled:
            logger.info("Context filter: target=%s, removed=%d keys", target, len(removed_keys))
        return dict(full_context) if dry_run else filtered

    def get_allowed_sources(self, target_tf: str) -> FrozenSet[str]:
        target = self._normalize_tf(target_tf)
        with self._mapping_lock:
            if target in self._mapping:
                return frozenset(self._mapping[target])
        logger.warning("Unknown target timeframe: %s", target)
        return frozenset()

    def get_transitive_sources(self, target_tf: str, exclude_self: bool = True) -> FrozenSet[str]:
        if not self._enabled:
            return frozenset()
        target = self._normalize_tf(target_tf)
        with self._mapping_lock:
            if target not in self._mapping:
                return frozenset()
            visited = set()
            stack = list(self._mapping[target])
            max_iter = len(self._mapping) * 2 + 10
            while stack and max_iter > 0:
                src = stack.pop()
                if src == target and exclude_self:
                    continue
                if src in visited:
                    continue
                visited.add(src)
                if src in self._mapping:
                    for next_src in self._mapping[src]:
                        if next_src not in visited and (next_src != target or not exclude_self):
                            stack.append(next_src)
                max_iter -= 1
            if max_iter == 0:
                logger.error("Transitive source search exceeded max iterations for target %s.", target)
            return frozenset(visited)

    # =========================================================================
    # 映射管理
    # =========================================================================
    def register_mapping(
        self, target_tf: str, source_tf: Union[str, List[str]], allow_self_reference: bool = False
    ) -> None:
        target = self._normalize_tf(target_tf)
        if not target or not self._is_valid_tf(target):
            raise ValueError(f"Invalid target timeframe: {target_tf}")

        sources = [source_tf] if isinstance(source_tf, str) else source_tf
        if not sources:
            raise ValueError("At least one source timeframe must be provided.")
        if len(sources) > self.MAX_SOURCES_PER_CALL:
            raise ValueError(f"Too many sources in one call (max {self.MAX_SOURCES_PER_CALL})")

        if target in self._top_level:
            raise ValueError(f"Cannot add sources to top-level timeframe '{target}'.")

        norm_sources_set = set()
        for s in sources:
            ns = self._normalize_tf(s)
            if not ns or not self._is_valid_tf(ns):
                raise ValueError(f"Invalid source timeframe: {s}")
            if ns == target and not allow_self_reference:
                raise ValueError(f"Self-reference not allowed for {target}")
            norm_sources_set.add(ns)
        norm_sources = list(norm_sources_set)

        with self._mapping_lock:
            if target not in self._mapping:
                self._mapping[target] = set()
            current_sources = self._mapping[target]
            new_sources = [s for s in norm_sources if s not in current_sources and s != target]
            if not new_sources:
                logger.debug("No new sources to add for %s, skipping.", target)
                return

            total_edges = sum(len(v) for v in self._mapping.values())
            if total_edges + len(new_sources) > self._max_total_edges:
                raise RuntimeError(f"Max total edges ({self._max_total_edges}) exceeded.")

            if len(current_sources) + len(new_sources) > self.MAX_SOURCES_PER_TARGET:
                raise RuntimeError(f"Max sources per target ({self.MAX_SOURCES_PER_TARGET}) exceeded for {target}.")

            # 完全独立的临时映射副本用于环路检测
            temp_mapping = deepcopy({tf: set(srcs) for tf, srcs in self._mapping.items()})
            temp_mapping[target] = current_sources | set(new_sources)
            if self._detect_cycle(temp_mapping, target):
                raise HierarchyViolationError(target, ",".join(new_sources),
                                              "Adding these sources would create a cycle.")

            self._mapping[target].update(new_sources)
            logger.info("Mapping registered: %s -> %s (total edges now: %d)", new_sources, target, total_edges + len(new_sources))

    def unregister_mapping(self, target_tf: str, source_tf: str) -> None:
        target = self._normalize_tf(target_tf)
        source = self._normalize_tf(source_tf)
        if not target or not source or not self._is_valid_tf(target) or not self._is_valid_tf(source):
            raise ValueError("Invalid timeframe format.")

        # 保护核心默认映射
        if target in self.DEFAULT_MAPPING and source in self.DEFAULT_MAPPING.get(target, frozenset()):
            logger.warning("Attempt to remove core default mapping %s -> %s blocked.", source, target)
            return

        with self._mapping_lock:
            if target in self._mapping and source in self._mapping[target]:
                self._mapping[target].discard(source)
                logger.info("Mapping removed: %s -> %s", source, target)

    def reset_to_default(self) -> None:
        with self._mapping_lock:
            self._mapping.clear()
            self._init_mapping()
        with self._violation_lock:
            self._violation_count = 0
            self._violation_history.clear()
        with self._last_violation_ts_lock:
            self._last_violation_ts.clear()
        logger.info("Hierarchy mappings and violation counters reset to default.")
        self.validate_mapping()

    def get_all_timeframes(self) -> List[str]:
        with self._mapping_lock:
            return sorted(self._mapping.keys())

    def validate_mapping(self) -> Tuple[bool, List[str]]:
        warnings = []
        required = {"3m", "5m", "15m"}
        with self._mapping_lock:
            existing = set(self._mapping.keys())
            missing = required - existing
            if missing:
                warnings.append(f"Missing required timeframes: {missing}")
            for tf in self._top_level:
                if tf in self._mapping and self._mapping[tf]:
                    warnings.append(f"Top-level timeframe {tf} has sources: {self._mapping[tf]}")
            for tf in existing:
                if tf not in self._top_level and not self._mapping[tf]:
                    warnings.append(f"Timeframe {tf} has no sources (isolated).")
        if warnings:
            logger.warning("Mapping validation warnings: %s", warnings)
            return False, warnings
        return True, []

    def get_consumers_of_source(self, source_tf: str) -> Tuple[str, ...]:
        source = self._normalize_tf(source_tf)
        consumers = []
        with self._mapping_lock:
            for target, src_set in self._mapping.items():
                if source in src_set:
                    consumers.append(target)
        return tuple(sorted(consumers))

    # =========================================================================
    # 属性与状态
    # =========================================================================
    @property
    def is_strict(self) -> bool:
        return self._strict

    def set_strict(self, strict: bool) -> None:
        self._strict = strict
        if not strict:
            self._raise_on_violation = False
        logger.info("Strict mode set to %s", strict)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        logger.info("Hierarchy guard enabled set to %s", enabled)

    def disable(self) -> None:
        self.set_enabled(False)

    def enable(self) -> None:
        self.set_enabled(True)

    @property
    def violation_count(self) -> int:
        with self._violation_lock:
            return self._violation_count

    def get_recent_violations(self, n: int = 10) -> List[Dict[str, Any]]:
        with self._violation_lock:
            return list(self._violation_history)[-n:]

    def reset_violation_count(self) -> None:
        with self._violation_lock:
            self._violation_count = 0
            self._violation_history.clear()
        with self._last_violation_ts_lock:
            self._last_violation_ts.clear()
        logger.info("Violation counters and history reset.")

    def get_mapping_stats(self) -> Dict[str, Any]:
        with self._mapping_lock:
            return {
                "total_timeframes": len(self._mapping),
                "total_edges": sum(len(v) for v in self._mapping.values()),
                "max_edges_allowed": self._max_total_edges,
                "top_level": sorted(self._top_level),
                "enabled": self._enabled,
            }

    def set_audit_enabled(self, enabled: bool) -> None:
        self._audit_enabled = enabled

    def to_dict(self) -> Dict[str, Any]:
        """序列化当前完整状态（不含映射锁）。"""
        with self._mapping_lock, self._violation_lock, self._last_violation_ts_lock:
            return {
                "strict": self._strict,
                "raise_on_violation": self._raise_on_violation,
                "audit_enabled": self._audit_enabled,
                "unknown_source_action": self._unknown_source_action,
                "deep_copy_values": self._deep_copy_values,
                "enabled": self._enabled,
                "max_total_edges": self._max_total_edges,
                "top_level": sorted(self._top_level),
                "mapping": {tf: sorted(list(srcs)) for tf, srcs in self._mapping.items()},
                "violation_count": self._violation_count,
                "violation_history": list(self._violation_history),
                "last_violation_ts": {f"{k[0]}/{k[1]}": v for k, v in self._last_violation_ts.items()},
            }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HierarchyGuard:
        """从字典恢复实例。"""
        obj = cls(
            strict=data.get("strict", True),
            raise_on_violation=data.get("raise_on_violation", False),
            audit_enabled=data.get("audit_enabled", False),
            unknown_source_action=data.get("unknown_source_action", "remove"),
            deep_copy_values=data.get("deep_copy_values", False),
            enabled=data.get("enabled", True),
            max_total_edges=data.get("max_total_edges", 500),
            top_level_timeframes=frozenset(data.get("top_level", [])),
        )
        # 清除默认映射并加载
        with obj._mapping_lock:
            obj._mapping.clear()
            for tf, srcs in data.get("mapping", {}).items():
                obj._mapping[tf] = set(srcs)
        with obj._violation_lock:
            obj._violation_count = data.get("violation_count", 0)
            obj._violation_history = deque(data.get("violation_history", []), maxlen=obj._max_violation_history)
        with obj._last_violation_ts_lock:
            obj._last_violation_ts = {tuple(k.split('/')): v for k, v in data.get("last_violation_ts", {}).items()}
        return obj

    # =========================================================================
    # 上下文键分类工具
    # =========================================================================
    def classify_key(self, key: str, target_tf: str) -> KeyClassification:
        if not self._enabled:
            target = self._normalize_tf(target_tf)
            source = self._extract_timeframe_from_key(key)
            if source == target:
                return KeyClassification.OWN
            return KeyClassification.ALLOWED
        target = self._normalize_tf(target_tf)
        if not target:
            return KeyClassification.UNKNOWN
        source = self._extract_timeframe_from_key(key)
        if source is None:
            return KeyClassification.UNKNOWN
        if source == target:
            return KeyClassification.OWN
        allowed = self._get_allowed_sources_internal(target)
        if source in allowed:
            return KeyClassification.ALLOWED
        return KeyClassification.BLOCKED

    def validate_context_keys(self, target_tf: str, keys: Optional[List[str]]) -> Dict[str, str]:
        if keys is None:
            return {}
        return {k: self.classify_key(k, target_tf).value for k in keys}

    # =========================================================================
    # 内部辅助
    # =========================================================================
    def _normalize_tf(self, tf: str) -> str:
        if not tf:
            return ""
        tf = tf.strip().lower()
        normalized = self.TF_FULLNAME_MAP.get(tf, tf)
        if len(normalized) > self.MAX_TF_LENGTH:
            logger.warning("Timeframe '%s' exceeds max length, using as-is.", normalized)
        return normalized[:self.MAX_TF_LENGTH]

    def _is_valid_tf(self, tf: str) -> bool:
        return bool(self.TF_PATTERN.match(tf))

    def _get_allowed_sources_internal(self, target_tf: str) -> Set[str]:
        with self._mapping_lock:
            if target_tf in self._mapping:
                return set(self._mapping[target_tf])
            return set()

    @staticmethod
    def _detect_cycle(mapping: Dict[str, Set[str]], root: str) -> bool:
        """检测从 root 出发是否能回到 root（环路）。"""
        visited = set()
        stack = list(mapping.get(root, set()))
        max_depth = HierarchyGuard.MAX_PATH_DEPTH
        while stack and max_depth > 0:
            current = stack.pop()
            if current == root:
                return True
            if current in visited:
                continue
            visited.add(current)
            stack.extend(mapping.get(current, set()) - visited)
            max_depth -= 1
        return False

    def _record_violation(self, target: str, source: str) -> None:
        now = time.monotonic()
        key = (target, source)
        with self._last_violation_ts_lock:
            last = self._last_violation_ts.get(key, 0)
            if now - last < self._log_cooldown_sec:
                return
            self._last_violation_ts[key] = now
            # 容量控制：移除最旧10%条目
            if len(self._last_violation_ts) > self.MAX_VIOLATION_TS_ENTRIES:
                sorted_items = sorted(self._last_violation_ts.items(), key=lambda x: x[1])
                remove_count = len(self._last_violation_ts) - self.MAX_VIOLATION_TS_ENTRIES + 50
                for k, _ in sorted_items[:remove_count]:
                    del self._last_violation_ts[k]

        with self._violation_lock:
            self._violation_count += 1
            self._violation_history.append({
                "target": target,
                "source": source,
                "timestamp": now,
                "datetime_utc": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "count_total": self._violation_count,
            })

    def _extract_timeframe_from_key(self, key: str) -> Optional[str]:
        if not key:
            return None
        lower_key = key.lower()
        # 已知前缀匹配
        for prefix in self.KNOWN_INDICATOR_PREFIXES:
            if lower_key.startswith(prefix + "_"):
                remainder = lower_key[len(prefix) + 1:]
                tfs = self._find_all_tfs_in_string(remainder)
                if len(tfs) == 1:
                    return tfs[0]
                # 多个或无，视为未知
                return None
        # 通用下划线分割
        parts = key.rsplit('_', 1)
        if len(parts) == 2:
            candidate = parts[1].strip().lower()
            if self._is_valid_tf(candidate) and candidate in self.get_all_timeframes():
                return candidate
        # 整个键就是时间框架
        if self._is_valid_tf(lower_key) and lower_key in self.get_all_timeframes():
            return lower_key
        return None

    def _find_all_tfs_in_string(self, s: str) -> List[str]:
        """返回字符串中出现的所有已注册合法时间框架（无重复），若同一tf出现多次或出现多个不同tf，则收集。"""
        found = []
        all_tfs = sorted(self.get_all_timeframes(), key=len, reverse=True)
        temp = s
        for tf in all_tfs:
            count = 0
            while tf in temp:
                count += 1
                temp = temp.replace(tf, "", 1)
            if count > 1:  # 同一tf出现多次，无法判断主周期，返回空
                return []
            if count == 1:
                found.append(tf)
        return found

    def snapshot(self) -> MappingProxyType:
        """返回当前映射的只读快照。"""
        with self._mapping_lock:
            return MappingProxyType({tf: frozenset(srcs) for tf, srcs in self._mapping.items()})

    def export_mapping(self) -> Dict[str, List[str]]:
        return {tf: sorted(list(srcs)) for tf, srcs in self.snapshot().items()}

    def __repr__(self) -> str:
        return (f"<HierarchyGuard strict={self._strict} enabled={self._enabled} "
                f"violations={self.violation_count}>")
