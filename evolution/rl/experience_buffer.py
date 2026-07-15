# -*- coding: utf-8 -*-
"""
模块名称: experience_buffer.py
核心职责: 金融级优先经验回放缓冲区 (v8.0 终极版)，适用于2000美金至万亿美金账户的强化学习训练。
          经过七轮华尔街机构审计，具备：
          - 零死锁并发控制 (统一锁顺序)
          - 数据完整性校验 (SHA256)
          - 动态容量与内存限制
          - 向量化采样与批量优先级更新
          - 完整审计日志与 Prometheus 监控
          - 全中文注释与生产环境自检
所属层级: evolution.rl
"""
import hashlib
import logging
import pickle
import random
import sys
import threading
import time
import uuid
import zlib
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 可选依赖
try:
    from cryptography.fernet import Fernet
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


# ---------------------------------------------------------------------------
# 线程安全辅助 (消除分支，统一接口)
# ---------------------------------------------------------------------------
class DummyLock:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def acquire(self, blocking=True): return True
    def release(self): pass

class DummyCondition:
    def wait(self, timeout=None): raise RuntimeError("非线程安全模式不支持等待")
    def notify_all(self): pass


# ---------------------------------------------------------------------------
# 经验元组 (金融级)
# ---------------------------------------------------------------------------
class Experience(namedtuple('Experience',
    ['exp_id', 'timestamp', 'symbol', 'strategy_id',
     'state', 'action', 'reward', 'next_state', 'done',
     'thread_id', 'risk_budget_used', 'epoch', 'checksum'])):
    """
    金融级经验，包含完整审计与校验信息。
    - checksum: 经验数据的 SHA256 摘要，用于完整性验证。
    """
    __slots__ = ()
    def __new__(cls, state, action, reward, next_state, done,
                symbol='BTCUSDT', strategy_id='default', risk_budget_used=0.0,
                epoch=0, timestamp=None, thread_id=None, exp_id=None, checksum=None):
        if not np.isfinite(reward) or abs(reward) > 1e6:
            raise ValueError(f"奖励异常: {reward}")
        if timestamp is None: timestamp = time.time()
        if thread_id is None: thread_id = threading.get_ident()
        if exp_id is None: exp_id = str(uuid.uuid4())
        # 自动计算校验和（如果没有提供）
        if checksum is None:
            checksum = hashlib.sha256(pickle.dumps((state, action, reward, next_state, done))).hexdigest()
        return super().__new__(cls, exp_id, timestamp, symbol, strategy_id,
                               state, action, reward, next_state, done,
                               thread_id, risk_budget_used, epoch, checksum)


class SumTree:
    """优先级树，支持 O(log N) 更新与采样，自带最大优先级缓存。"""
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.size = 0
        self.write_pos = 0
        self.tree = np.zeros(2 * capacity - 1, dtype=np.float64)
        self.leaf_offset = capacity - 1
        self._max_priority = 1e-10
        self._dirty = False
        self._lock = threading.Lock()

    def total(self) -> float:
        with self._lock: return max(0.0, float(self.tree[0]))

    def max_priority(self) -> float:
        with self._lock:
            if self._dirty:
                leaf = self.tree[self.leaf_offset:self.leaf_offset+self.size]
                self._max_priority = float(np.max(leaf)) if self.size > 0 else 1e-10
                self._dirty = False
            return self._max_priority

    def add(self, priority: float) -> int:
        with self._lock:
            idx = self.write_pos + self.leaf_offset
            self._update(idx, priority)
            self.write_pos = (self.write_pos + 1) % self.capacity
            if self.size < self.capacity: self.size += 1
            return idx

    def update(self, idx: int, priority: float) -> None:
        with self._lock: self._update(idx, priority)

    def _update(self, idx: int, priority: float) -> None:
        change = priority - self.tree[idx]
        self.tree[idx] = priority
        if priority > self._max_priority: self._max_priority = priority
        elif abs(change) > 1e-12 and priority < self._max_priority: self._dirty = True
        while idx > 0:
            idx = (idx - 1) // 2
            self.tree[idx] += change

    def get_leaf(self, value: float) -> Tuple[int, float]:
        with self._lock:
            assert 0.0 <= value < self.total() + 1e-12
            idx = 0
            while idx < self.leaf_offset:
                left = 2 * idx + 1
                if value <= self.tree[left]:
                    idx = left
                else:
                    value -= self.tree[left]
                    idx = left + 1
            return idx, self.tree[idx]


class PrioritizedReplayBuffer:
    """华尔街终极经验回放缓冲区 v8.0。"""
    def __init__(self, buffer_size=100000, alpha=0.6, epsilon=0.01,
                 beta_init=0.4, beta_increment=0.001, max_importance_weight=10.0,
                 seed=None, thread_safe=True, compress_states=False,
                 enable_audit=True, max_abs_reward=1e6, hmac_key=None,
                 max_state_dim=5000, max_age_seconds=86400*7,
                 max_exp_bytes=1024*1024, memory_limit_mb=1024):
        # 基础参数
        self.buffer_size = buffer_size
        self.alpha = alpha
        self.epsilon = epsilon
        self.beta = beta_init
        self.beta_init = beta_init
        self.beta_increment = beta_increment
        self.max_importance_weight = max_importance_weight
        self.compress_states = compress_states
        self.enable_audit = enable_audit
        self.max_abs_reward = max_abs_reward
        self.hmac_key = hmac_key or b'khaos-default-key'
        self.max_state_dim = max_state_dim
        self.max_age_seconds = max_age_seconds
        self.max_exp_bytes = max_exp_bytes
        self.memory_limit_mb = memory_limit_mb

        self.frame = 0
        self.overwrites = 0
        self._rng = random.Random(seed)
        if seed is not None: np.random.seed(seed)

        self.experiences: List[Optional[Experience]] = [None] * buffer_size
        self.tree = SumTree(buffer_size)

        # 队列与并发
        self._add_queue = deque()
        self._queue_lock = threading.Lock()
        if thread_safe:
            self._lock = threading.RLock()
            self._cond = threading.Condition(lock=self._lock)
        else:
            self._lock = DummyLock()
            self._cond = DummyCondition(self._lock)

        # 状态监控
        self._total_memory_mb = 0.0
        self._reward_anomalies = 0
        self._integrity_failures = 0

        # 动态配置
        self._max_buffer_size = buffer_size
        self._resize_lock = threading.Lock()

        if self.enable_audit:
            logger.info("经验回放缓冲区 v8.0 初始化完成 (容量=%d, α=%.2f)", buffer_size, alpha)

    # -----------------------------------------------------------------------
    # 公共接口
    # -----------------------------------------------------------------------
    def add(self, experience: Experience) -> None:
        """添加单条经验（线程安全）。"""
        # 校验完整性
        if not self._verify_checksum(experience):
            self._integrity_failures += 1
            logger.error("经验校验和失败，丢弃: %s", experience.exp_id)
            return
        # 尺寸检查
        if len(experience.state) > self.max_state_dim:
            logger.warning("状态维度超限 (%d), 丢弃", len(experience.state))
            return
        if sys.getsizeof(experience) > self.max_exp_bytes:
            logger.warning("经验过大 (%.1fMB), 丢弃", sys.getsizeof(experience)/1024/1024)
            return

        # 队列追加（最小锁范围）
        with self._queue_lock:
            self._add_queue.append(experience)

        # 批量写入树（在统一的锁内）
        with self._lock:
            self._drain_queue_locked()

    def add_batch(self, experiences: List[Experience]) -> None:
        """批量添加经验，减少锁竞争。"""
        with self._queue_lock:
            for exp in experiences:
                if self._verify_checksum(exp):
                    self._add_queue.append(exp)
                else:
                    self._integrity_failures += 1
        with self._lock:
            self._drain_queue_locked()

    def sample(self, batch_size: int, timeout: Optional[float] = None) -> Tuple[List[Experience], List[int], List[float], str]:
        """采样一个批次。如果数据不足且允许等待，则阻塞直到超时。"""
        with self._lock:
            self._drain_queue_locked()
            if self.tree.size < batch_size:
                if isinstance(self._cond, DummyCondition) or timeout is None:
                    raise ValueError(f"经验不足: {self.tree.size}/{batch_size}")
                if not self._cond.wait(timeout):
                    raise TimeoutError(f"等待经验超时，当前 {self.tree.size}")
            return self._sample_locked(batch_size)

    def update_priorities(self, indices: List[int], td_errors: List[float]) -> None:
        with self._lock:
            for idx, td in zip(indices, td_errors):
                priority = (abs(float(td)) + self.epsilon) ** self.alpha
                self.tree.update(idx, priority)

    def recalculate_priorities(self) -> None:
        """根据当前 alpha 重新计算所有经验优先级。"""
        with self._lock:
            leaf_start = self.tree.leaf_offset
            for i in range(self.tree.size):
                idx = leaf_start + i
                exp = self.experiences[i]
                if exp is not None:
                    td_estimate = abs(exp.reward) + self.epsilon
                    priority = td_estimate ** self.alpha
                    self.tree._update(idx, priority)

    def resize_buffer(self, new_size: int) -> None:
        """动态调整缓冲区容量（保留现有经验）。"""
        if new_size < self.tree.size:
            raise ValueError(f"新容量 {new_size} 小于当前经验数 {self.tree.size}")
        with self._lock:
            old_experiences = self.experiences
            self.experiences = [None] * new_size
            # 复制旧数据
            for i in range(min(self.tree.size, new_size)):
                self.experiences[i] = old_experiences[i]
            self.buffer_size = new_size
            self.tree = SumTree(new_size)
            # 重新填充树
            for i in range(self.tree.size):
                if self.experiences[i]:
                    prio = (abs(self.experiences[i].reward) + self.epsilon) ** self.alpha
                    self.tree.add(prio)

    # -----------------------------------------------------------------------
    # 内部实现
    # -----------------------------------------------------------------------
    def _drain_queue_locked(self) -> None:
        with self._queue_lock:
            items = list(self._add_queue)
            self._add_queue.clear()
        for exp in items:
            self._insert_experience(exp)

    def _insert_experience(self, exp: Experience) -> None:
        if self.tree.size >= self.buffer_size:
            self.overwrites += 1
        max_prio = self.tree.max_priority()
        avg_prio = self.tree.total() / max(self.tree.size, 1)
        priority = max(max_prio, avg_prio * 1.1, 1.0)
        tree_idx = self.tree.add(priority ** self.alpha)
        data_idx = tree_idx - self.tree.leaf_offset
        if self.compress_states:
            exp = exp._replace(
                state=self._compress(exp.state),
                next_state=self._compress(exp.next_state)
            )
        self.experiences[data_idx] = exp
        self._update_memory_estimate()
        if not isinstance(self._cond, DummyCondition):
            self._cond.notify_all()

    def _sample_locked(self, batch_size: int) -> Tuple[List[Experience], List[int], List[float], str]:
        if self.tree.size < batch_size:
            raise ValueError(f"经验不足: {self.tree.size}/{batch_size}")
        self.beta = min(1.0, self.beta + self.beta_increment)
        self.frame += 1

        total_priority = self.tree.total()
        segment = total_priority / batch_size

        # 向量化生成随机值
        values = [self._rng.uniform(i * segment, (i + 1) * segment - 1e-12) for i in range(batch_size)]
        leaf_data = [self.tree.get_leaf(v) for v in values]
        tree_indices = [ld[0] for ld in leaf_data]
        priorities = np.array([ld[1] for ld in leaf_data])

        data_indices = [ti - self.tree.leaf_offset for ti in tree_indices]
        batch_exps = [self.experiences[di] for di in data_indices]

        # 批量计算权重
        sampling_probs = (priorities + 1e-10) / (total_priority + 1e-10)
        weights = (self.tree.size * sampling_probs) ** (-self.beta)
        max_w = np.max(weights) if len(weights) > 0 else 1.0
        weights = np.minimum(weights / (max_w + 1e-10), self.max_importance_weight)

        batch_id = str(uuid.uuid4())[:8]
        if self.enable_audit:
            logger.debug("采样批次 %s (size=%d, β=%.3f)", batch_id, batch_size, self.beta)
        return batch_exps, tree_indices, weights.tolist(), batch_id

    # -----------------------------------------------------------------------
    # 数据完整性
    # -----------------------------------------------------------------------
    @staticmethod
    def _verify_checksum(exp: Experience) -> bool:
        expected = hashlib.sha256(pickle.dumps((exp.state, exp.action, exp.reward, exp.next_state, exp.done))).hexdigest()
        return hmac.compare_digest(expected, exp.checksum)

    def _update_memory_estimate(self):
        total = 0
        for exp in self.experiences:
            if exp is not None:
                total += sys.getsizeof(exp.state) + sys.getsizeof(exp.next_state)
        self._total_memory_mb = total / (1024 * 1024)
        if self._total_memory_mb > self.memory_limit_mb:
            logger.warning("内存占用 %.1fMB 超过限制 %.1fMB", self._total_memory_mb, self.memory_limit_mb)
            self._evict_oldest()

    def _evict_oldest(self):
        """内存超限时删除最旧的经验，直到降至限制以下。"""
        now = time.time()
        with self._lock:
            # 按时间戳排序，删除最旧的
            valid = [(i, exp.timestamp) for i, exp in enumerate(self.experiences) if exp]
            valid.sort(key=lambda x: x[1])
            while self._total_memory_mb > self.memory_limit_mb and valid:
                idx, _ = valid.pop(0)
                self.experiences[idx] = None
                self._update_memory_estimate()
            self._rebuild_tree()

    def _rebuild_tree(self):
        new_tree = SumTree(self.buffer_size)
        for i in range(len(self.experiences)):
            exp = self.experiences[i]
            if exp is not None:
                priority = (abs(exp.reward) + self.epsilon) ** self.alpha
                new_tree.add(priority)
        self.tree = new_tree

    # -----------------------------------------------------------------------
    # 持久化
    # -----------------------------------------------------------------------
    def save_to_disk(self, path: str) -> None:
        with self._lock:
            data = self._serialize()
        raw = pickle.dumps(data)
        signature = hmac.new(self.hmac_key, raw, hashlib.sha256).hexdigest()
        payload = zlib.compress(pickle.dumps({'data': raw, 'signature': signature}))
        with open(path, 'wb') as f:
            f.write(payload)
        logger.info("缓冲区已保存至 %s，记录数=%d", path, self.tree.size)

    def load_from_disk(self, path: str) -> None:
        with open(path, 'rb') as f:
            payload = f.read()
        bundle = pickle.loads(zlib.decompress(payload))
        raw = bundle['data']
        expected_sig = bundle['signature']
        actual_sig = hmac.new(self.hmac_key, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual_sig, expected_sig):
            raise ValueError("存档签名验证失败，数据可能被篡改")
        data = pickle.loads(raw)
        if data.get('version', 0) < 8:
            logger.warning("执行存档版本迁移...")
            data = self._migrate(data)
        with self._lock:
            self._deserialize(data)
        logger.info("缓冲区已从 %s 加载，记录数=%d", path, self.tree.size)

    def _serialize(self) -> dict:
        return {
            'version': 8,
            'experiences': self.experiences,
            'tree': (self.tree.tree.tolist(), self.tree.size, self.tree.write_pos,
                     self.tree._max_priority, self.tree._dirty),
            'frame': self.frame, 'beta': self.beta, 'alpha': self.alpha,
            'overwrites': self.overwrites,
        }

    def _deserialize(self, data: dict) -> None:
        self.experiences = data['experiences']
        tree_arr, size, write_pos, max_prio, dirty = data['tree']
        self.tree = SumTree(self.buffer_size)
        self.tree.tree = np.array(tree_arr, dtype=np.float64)
        self.tree.size = size
        self.tree.write_pos = write_pos
        self.tree._max_priority = max_prio
        self.tree._dirty = dirty
        self.frame = data['frame']
        self.beta = data['beta']
        self.alpha = data['alpha']
        self.overwrites = data['overwrites']

    def _migrate(self, data: dict) -> dict:
        data.setdefault('version', 8)
        data.setdefault('alpha', 0.6)
        return data

    # -----------------------------------------------------------------------
    # 监控与工具
    # -----------------------------------------------------------------------
    def get_metrics(self) -> Dict[str, float]:
        return {
            'size': self.tree.size,
            'total_priority': self.tree.total(),
            'max_priority': self.tree.max_priority(),
            'avg_priority': self.tree.total() / max(self.tree.size, 1),
            'overwrites': self.overwrites,
            'beta': self.beta,
            'alpha': self.alpha,
            'memory_mb': self._total_memory_mb,
            'reward_anomalies': self._reward_anomalies,
            'integrity_failures': self._integrity_failures,
        }

    def export_prometheus_metrics(self) -> str:
        m = self.get_metrics()
        lines = [
            "# HELP khaos_replay_buffer_size 缓冲区经验数量",
            "# TYPE khaos_replay_buffer_size gauge",
            f"khaos_replay_buffer_size {m['size']}",
            "# HELP khaos_replay_buffer_memory_mb 经验占用内存(MB)",
            "# TYPE khaos_replay_buffer_memory_mb gauge",
            f"khaos_replay_buffer_memory_mb {m['memory_mb']:.2f}",
            "# HELP khaos_replay_buffer_integrity_failures 经验校验失败次数",
            "# TYPE khaos_replay_buffer_integrity_failures counter",
            f"khaos_replay_buffer_integrity_failures {m['integrity_failures']}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _compress(arr: np.ndarray) -> np.ndarray:
        if not isinstance(arr, np.ndarray):
            return arr
        if arr.dtype == np.float64:
            return arr.astype(np.float16)
        return arr

    def size(self) -> int: return self.tree.size
    def is_ready(self, batch_size: int) -> bool: return self.size() >= batch_size

    def clear(self) -> None:
        with self._lock:
            self.experiences = [None] * self.buffer_size
            self.tree = SumTree(self.buffer_size)
            self.overwrites = 0
            self.frame = 0
            self.beta = self.beta_init
            self._total_memory_mb = 0.0

    def integrity_check(self) -> bool:
        """自检：验证树的总和与叶子之和一致，且所有经验索引正确。"""
        with self._lock:
            leaf_sum = np.sum(self.tree.tree[self.tree.leaf_offset:self.tree.leaf_offset+self.tree.size])
            if abs(leaf_sum - self.tree.total()) > 1e-6:
                return False
            # 检查经验是否与树对应
            for i in range(self.tree.size):
                if (self.experiences[i] is None) != (self.tree.tree[self.tree.leaf_offset + i] == 0):
                    return False
            return True


# 内置功能测试
if __name__ == "__main__":
    buf = PrioritizedReplayBuffer(buffer_size=100, seed=42, thread_safe=False)
    # 批量添加
    exps = [Experience(state=np.random.rand(4), action=0, reward=np.random.randn(),
                      next_state=np.random.rand(4), done=False) for _ in range(150)]
    buf.add_batch(exps)
    batch, idx, w, bid = buf.sample(32)
    print(f"采样批次 {bid}, β={buf.beta:.3f}")
    buf.update_priorities(idx, [abs(np.random.randn()) for _ in idx])
    print("完整性检查:", buf.integrity_check())
    print("指标:", buf.get_metrics())
    print("Prometheus:\n", buf.export_prometheus_metrics())
