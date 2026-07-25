# -*- coding: utf-8 -*-
"""
模块名称: core/indicators/kma.py
核心职责: 提供自适应卡尔曼均线 (Kalman Moving Average, KMA) 计算，输出动态均线值及斜率。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - core.interfaces.FeatureComputer (特征计算基类)
    - core.models.Kline (K线数据结构)

接口契约:
    提供: {
        'KalmanTrendline': {
            'input': 'kline: Kline, context: dict',
            'output': 'dict {kma: float, kma_slope: float, kma_upper: float, kma_lower: float, sigma_obs: float}',
            'side_effects': ['更新内部状态 (状态向量, 协方差矩阵)']
        }
    }
    消费: {
        'kline.close': '当前 K 线收盘价',
        'context.recent_volatility': '近期波动率，用于观测噪声估计 (可选)'
    }

配置项:
    - strategy.kalman.q_ratio (float, 0.01): 基础过程噪声比
    - strategy.kalman.delta (float, 1.0): 时间增量（K 线场景下通常为 1.0）
    - strategy.kalman.adaptive_q (bool, True): 是否根据波动率自适应调整 q_ratio
    - strategy.kalman.min_q_ratio (float, 0.001): 最小噪声比
    - strategy.kalman.max_q_ratio (float, 0.1): 最大噪声比

作者: KHAOS System Architect
创建日期: 2025-03-15
修改记录:
    - 2026-01-10 增加自适应 q_ratio 及数值稳定性保护
    - 2026-07-12 通过机构级审计，增强鲁棒性与可观测性
    - 2026-07-25 修复：F 与 delta 不一致、首根初始化顺序错误、自适应 q 无记忆、
                   更新形式数值不稳定、极端创新无保护等问题；加强正定维护与状态校验
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import numpy as np
from numpy.linalg import eigvalsh

from core.interfaces import FeatureComputer
from core.models import Kline

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_Q_RATIO = 0.01
DEFAULT_DELTA = 1.0          # K 线离散步长，原 1e-5 会导致 Q ≈ 0 使滤波器失效
DEFAULT_ADAPTIVE_Q = True
DEFAULT_MIN_Q = 0.001
DEFAULT_MAX_Q = 0.1
DEFAULT_JITTER = 0.01
MAX_JITTER = 0.02
MAX_CALLS_BEFORE_RESET = 100_000
EPS = 1e-12                  # 数值保护下限
MAX_INNOVATION_RATIO = 8.0   # 创新超过此倍数时临时放大 R，防止单点拉飞


class KalmanTrendline(FeatureComputer):
    """
    自适应卡尔曼均线。
    使用局部线性趋势模型（constant-velocity），通过卡尔曼滤波估计真实价格水平与趋势斜率。
    观测噪声根据近期波动率动态调整，过程噪声比可自适应变化并带记忆。
    输出: {'kma': float, 'kma_slope': float, 'kma_upper': float, 'kma_lower': float, 'sigma_obs': float}
    """

    def __init__(
        self,
        q_ratio: float = DEFAULT_Q_RATIO,
        delta: float = DEFAULT_DELTA,
        adaptive_q: bool = DEFAULT_ADAPTIVE_Q,
        min_q_ratio: float = DEFAULT_MIN_Q,
        max_q_ratio: float = DEFAULT_MAX_Q,
        q_ratio_jitter: float = DEFAULT_JITTER,
        max_q_jitter: float = MAX_JITTER,
        random_seed: Optional[int] = None,
    ):
        # 参数验证
        if not 0 < q_ratio <= 1.0:
            raise ValueError(f"q_ratio 必须在 (0, 1] 之间，当前: {q_ratio}")
        if delta <= 0:
            raise ValueError(f"delta 必须为正数，当前: {delta}")
        if min_q_ratio >= max_q_ratio:
            raise ValueError("min_q_ratio 必须小于 max_q_ratio")

        self.base_q_ratio = float(q_ratio)
        self.delta = float(delta)
        self.adaptive_q = bool(adaptive_q)
        self.min_q = float(min_q_ratio)
        self.max_q = float(max_q_ratio)
        self.q_jitter = float(q_ratio_jitter)
        self.max_jitter = float(max_q_jitter)

        # 随机数生成器，支持固定种子以保证回测可复现
        self._rng = np.random.default_rng(random_seed)

        # 状态向量: [level, slope]
        self.x = np.zeros(2, dtype=np.float64)
        # 协方差矩阵
        self.P = np.eye(2, dtype=np.float64) * 1000.0
        # 观测噪声标准差 (动态估计)
        self.sigma_obs = 1.0
        # 自适应过程噪声比（带记忆）
        self._q = self.base_q_ratio
        # 是否已初始化 (接收到第一个有效价格)
        self._initialized = False
        # 调用计数器 (防止模型退化)
        self._call_count = 0
        # 异步锁，防止并发调用导致状态混乱
        self._lock = asyncio.Lock()
        # 性能计时
        self._last_duration_ms = 0.0

    async def compute(self, kline: Kline, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        处理一根新 K 线，更新卡尔曼估计。

        Args:
            kline: 当前 K 线数据，至少需包含 close 价格。
            context: 包含 'recent_volatility' 等可选字段的上下文字典。

        Returns:
            dict: 包含当前估计的均线值、斜率、置信区间上下界及观测噪声。
        """
        async with self._lock:
            start_ts = time.monotonic()
            context = context or {}

            # 1. 提取并验证价格
            price = self._validate_price(kline.close)
            if price is None:
                logger.warning("收到无效收盘价，返回当前状态估计。")
                return self._current_estimate()

            # 2. 首次接收有效价格：先确定合理的 sigma_obs 与 P，再标记已初始化
            if not self._initialized:
                self._bootstrap(price, context)
            else:
                # 3. 动态观测噪声估计（非首根）
                self._update_sigma_obs(price, context)

            # 4. 动态过程噪声比（带记忆）
            q = self._compute_q(price)

            # 5. 预测与更新
            self._predict_update(price, q)

            # 6. 数值稳定性保护
            self._ensure_positive_definite()
            self._sanitize_state()

            # 7. 更新计数器与重置保护
            self._call_count += 1
            if self._call_count > MAX_CALLS_BEFORE_RESET:
                logger.warning("卡尔曼滤波器调用次数过多，执行预防性重置。")
                self.reset()
                self._bootstrap(price, context)  # 立即用当前价格恢复

            self._last_duration_ms = (time.monotonic() - start_ts) * 1000
            return self._current_estimate()

    def _validate_price(self, raw_price: Any) -> Optional[float]:
        """验证并转换价格输入。"""
        try:
            price = float(raw_price)
            if price <= 0 or not np.isfinite(price):
                raise ValueError
            return price
        except (TypeError, ValueError):
            return None

    def _bootstrap(self, price: float, context: Dict[str, Any]) -> None:
        """首根或重置后的快速初始化，保证 sigma 与 P 一致。"""
        recent_vol = context.get('recent_volatility')
        if recent_vol is not None and isinstance(recent_vol, (int, float)) and recent_vol > 0:
            self.sigma_obs = float(recent_vol)
        else:
            # 无外部波动率时，用价格相对比例作为初始观测噪声
            self.sigma_obs = max(price * 0.01, 1e-8)

        self.x[0] = price
        self.x[1] = 0.0
        # 初始不确定性与观测噪声同量级，加速收敛同时避免过度自信
        self.P = np.eye(2, dtype=np.float64) * (self.sigma_obs ** 2)
        self._q = self.base_q_ratio
        self._initialized = True

    def _update_sigma_obs(self, price: float, context: Dict[str, Any]) -> None:
        """更新观测噪声标准差。"""
        recent_vol = context.get('recent_volatility')
        if recent_vol is not None and isinstance(recent_vol, (int, float)) and recent_vol > 0:
            self.sigma_obs = float(recent_vol)
        else:
            # 使用相对创新的指数平滑，避免价格尺度差异
            innovation = abs(price - self.x[0])
            self.sigma_obs = 0.9 * self.sigma_obs + 0.1 * innovation
        # 钳位到合理范围
        self.sigma_obs = max(float(self.sigma_obs), 1e-8)

    def _compute_q(self, price: float) -> float:
        """计算自适应过程噪声比（带记忆 + 可选微扰）。"""
        if self.adaptive_q and price > 0:
            vol_ratio = self.sigma_obs / price
            # tanh 映射将相对波动平滑压缩到 [0.5, 1.0] 倍 base
            target_q = self.base_q_ratio * (0.5 + 0.5 * np.tanh(vol_ratio * 100.0))
            # 缓慢跟踪，保留历史记忆
            self._q = 0.95 * self._q + 0.05 * target_q
            self._q = float(np.clip(self._q, self.min_q, self.max_q))
        else:
            self._q = self.base_q_ratio

        # 可复现的随机微扰（仅在 jitter > 0 时生效）
        if self.q_jitter > 0:
            jitter = self._rng.normal(0.0, self.q_jitter)
            jitter = float(np.clip(jitter, -self.max_jitter, self.max_jitter))
            q = self._q + jitter
        else:
            q = self._q

        return max(q, 1e-10)

    def _predict_update(self, price: float, q: float) -> None:
        """执行卡尔曼预测与更新（Joseph 形式，含极端创新保护）。"""
        dt = self.delta
        # 连续白噪声加速度模型离散化过程噪声
        q11 = (dt ** 4) / 4.0
        q12 = (dt ** 3) / 2.0
        q22 = dt ** 2
        Q = np.array([[q11, q12], [q12, q22]], dtype=np.float64) * q * (self.sigma_obs ** 2)

        # 状态转移与观测矩阵（F 与 delta 严格一致）
        F = np.array([[1.0, dt], [0.0, 1.0]], dtype=np.float64)
        H = np.array([[1.0, 0.0]], dtype=np.float64)
        R_val = max(self.sigma_obs ** 2, EPS)
        R = np.array([[R_val]], dtype=np.float64)

        # 预测
        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + Q
        # 强制对称
        P_pred = 0.5 * (P_pred + P_pred.T)

        # 创新
        y = float(price - (H @ x_pred).item())

        # 极端创新保护：单点异常时临时放大观测噪声，避免状态被拉飞
        innov_scale = abs(y) / (self.sigma_obs + EPS)
        if innov_scale > MAX_INNOVATION_RATIO:
            R_val = R_val * (innov_scale / MAX_INNOVATION_RATIO) ** 2
            R[0, 0] = R_val
            logger.debug(
                "极端创新 (ratio=%.2f)，临时放大 R 至 %.6g", innov_scale, R_val
            )

        # 更新
        S = (H @ P_pred @ H.T + R).item()
        S = max(S, EPS)  # 防止除零或过小增益爆炸

        K = (P_pred @ H.T) / S          # shape (2, 1)
        self.x = x_pred + (K.flatten() * y)

        # Joseph 形式：数值更稳定，保证对称正半定
        I = np.eye(2, dtype=np.float64)
        KH = K @ H
        self.P = (I - KH) @ P_pred @ (I - KH).T + (K * R_val) @ K.T

    def _ensure_positive_definite(self) -> None:
        """确保协方差矩阵对称正定；退化时对角加载而非粗暴重置。"""
        # 强制对称
        self.P = 0.5 * (self.P + self.P.T)

        try:
            eigvals = eigvalsh(self.P)
            min_eig = float(np.min(eigvals))
            if min_eig < EPS:
                # 温和对角加载，保留已有信息
                loading = EPS - min_eig
                self.P[0, 0] += loading
                self.P[1, 1] += loading
                logger.debug("协方差最小特征值 %.3e，已对角加载 %.3e", min_eig, loading)
        except np.linalg.LinAlgError:
            logger.warning("协方差特征值分解失败，回退到与 sigma_obs 同量级的对角矩阵。")
            self.P = np.eye(2, dtype=np.float64) * max(self.sigma_obs ** 2, 1e-8)

    def _sanitize_state(self) -> None:
        """防止 NaN/Inf 污染后续计算。"""
        if not np.all(np.isfinite(self.x)):
            logger.warning("状态向量出现非有限值，重置 level 并清零 slope。")
            # 尽量保留上一次有效 level；此处用 0 兜底，下一根会重新 bootstrap
            self.x = np.array([0.0, 0.0], dtype=np.float64)
            self._initialized = False
        if not np.all(np.isfinite(self.P)):
            logger.warning("协方差矩阵出现非有限值，重置。")
            self.P = np.eye(2, dtype=np.float64) * max(self.sigma_obs ** 2, 1e-8)

    def _current_estimate(self) -> Dict[str, Any]:
        """返回当前状态估计结果。"""
        p00 = max(float(self.P[0, 0]), 0.0)
        half_width = 2.0 * np.sqrt(p00)
        return {
            'kma': float(self.x[0]),
            'kma_slope': float(self.x[1]),
            'kma_upper': float(self.x[0] + half_width),
            'kma_lower': float(self.x[0] - half_width),
            'sigma_obs': float(self.sigma_obs),
        }

    def reset(self) -> None:
        """重置卡尔曼滤波器状态。"""
        self.x = np.zeros(2, dtype=np.float64)
        self.P = np.eye(2, dtype=np.float64) * 1000.0
        self.sigma_obs = 1.0
        self._q = self.base_q_ratio
        self._initialized = False
        self._call_count = 0

    def get_state(self) -> Dict[str, Any]:
        """返回当前内部状态，用于检查点保存。"""
        return {
            'x': self.x.tolist(),
            'P': self.P.tolist(),
            'sigma_obs': self.sigma_obs,
            'initialized': self._initialized,
            'q': self._q,  # 新增：支持自适应 q 的断点续传
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """从检查点恢复内部状态。"""
        self.x = np.asarray(state['x'], dtype=np.float64)
        self.P = np.asarray(state['P'], dtype=np.float64)
        self.sigma_obs = float(state['sigma_obs'])
        self._initialized = bool(state.get('initialized', True))
        self._q = float(state.get('q', self.base_q_ratio))
        self._call_count = 0
        # 恢复后立即做一次数值消毒
        self._ensure_positive_definite()
        self._sanitize_state()
