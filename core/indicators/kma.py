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
    - strategy.kalman.delta (float, 1e-5): 时间增量
    - strategy.kalman.adaptive_q (bool, True): 是否根据波动率自适应调整 q_ratio
    - strategy.kalman.min_q_ratio (float, 0.001): 最小噪声比
    - strategy.kalman.max_q_ratio (float, 0.1): 最大噪声比

作者: KHAOS System Architect
创建日期: 2025-03-15
修改记录:
    - 2026-01-10 增加自适应 q_ratio 及数值稳定性保护
    - 2026-07-12 通过机构级审计，增强鲁棒性与可观测性
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np
from numpy.linalg import eigvalsh

from core.interfaces import FeatureComputer
from core.models import Kline

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_Q_RATIO = 0.01
DEFAULT_DELTA = 1e-5
DEFAULT_ADAPTIVE_Q = True
DEFAULT_MIN_Q = 0.001
DEFAULT_MAX_Q = 0.1
DEFAULT_JITTER = 0.01
MAX_JITTER = 0.02
MAX_CALLS_BEFORE_RESET = 100_000


class KalmanTrendline(FeatureComputer):
    """
    自适应卡尔曼均线。
    使用局部线性趋势模型，通过卡尔曼滤波估计真实价格水平与趋势斜率。
    观测噪声根据近期波动率动态调整，过程噪声比可自适应变化。
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

        self.base_q_ratio = q_ratio
        self.delta = delta
        self.adaptive_q = adaptive_q
        self.min_q = min_q_ratio
        self.max_q = max_q_ratio
        self.q_jitter = q_ratio_jitter
        self.max_jitter = max_q_jitter

        # 随机数生成器，支持固定种子以保证回测可复现
        self._rng = np.random.default_rng(random_seed)

        # 状态向量: [level, slope]
        self.x = np.zeros(2)
        # 协方差矩阵
        self.P = np.eye(2) * 1000.0
        # 观测噪声标准差 (动态估计)
        self.sigma_obs = 1.0
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

            # 2. 首次接收有效价格时加速收敛
            if not self._initialized:
                self.x[0] = price
                self.x[1] = 0.0
                self.P = np.eye(2) * (self.sigma_obs ** 2)
                self._initialized = True

            # 3. 动态观测噪声估计
            self._update_sigma_obs(price, context)

            # 4. 动态过程噪声比
            q = self._compute_q(price)

            # 5. 预测与更新
            self._predict_update(price, q)

            # 6. 数值稳定性保护
            self._ensure_positive_definite()

            # 7. 更新计数器与重置保护
            self._call_count += 1
            if self._call_count > MAX_CALLS_BEFORE_RESET:
                logger.warning("卡尔曼滤波器调用次数过多，执行预防性重置。")
                self.reset()
                self.x[0] = price  # 立即使用当前价格恢复
                self._initialized = True

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

    def _update_sigma_obs(self, price: float, context: Dict[str, Any]) -> None:
        """更新观测噪声标准差。"""
        recent_vol = context.get('recent_volatility')
        if recent_vol is not None and isinstance(recent_vol, (int, float)) and recent_vol > 0:
            self.sigma_obs = float(recent_vol)
        else:
            # 使用价格变化的指数平滑
            if self._initialized:
                innovation = abs(price - self.x[0])
                self.sigma_obs = 0.9 * self.sigma_obs + 0.1 * innovation
            else:
                self.sigma_obs = price * 0.01
        # 钳位到合理范围
        self.sigma_obs = max(self.sigma_obs, 1e-8)

    def _compute_q(self, price: float) -> float:
        """计算自适应过程噪声比。"""
        q = self.base_q_ratio
        if self.adaptive_q and price > 0:
            # 基于波动率比率平滑调整，避免突变
            vol_ratio = self.sigma_obs / price
            # 使用对数平滑映射
            target_q = self.base_q_ratio * (0.5 + 0.5 * np.tanh(vol_ratio * 100))
            q = 0.95 * q + 0.05 * target_q  # 缓慢移动
            q = np.clip(q, self.min_q, self.max_q)

        # 添加可复现的随机微扰
        jitter = self._rng.normal(0, self.q_jitter)
        jitter = np.clip(jitter, -self.max_jitter, self.max_jitter)
        return max(q + jitter, 1e-10)

    def _predict_update(self, price: float, q: float) -> None:
        """执行卡尔曼预测与更新。"""
        # 离散时间过程噪声协方差矩阵
        q11 = (self.delta ** 4) / 4.0
        q12 = (self.delta ** 3) / 2.0
        q22 = self.delta ** 2
        Q = np.array([[q11, q12], [q12, q22]]) * q * (self.sigma_obs ** 2)

        F = np.array([[1.0, 1.0], [0.0, 1.0]])
        H = np.array([[1.0, 0.0]])
        R = np.array([[self.sigma_obs ** 2]])

        # 预测
        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + Q

        # 更新
        y = price - (H @ x_pred)
        S = H @ P_pred @ H.T + R
        K = P_pred @ H.T / S[0, 0]

        self.x = x_pred + K.flatten() * y
        self.P = P_pred - np.outer(K, H @ P_pred)

    def _ensure_positive_definite(self) -> None:
        """确保协方差矩阵对称正定，若退化则修复。"""
        try:
            # 检查最小特征值
            eigvals = eigvalsh(self.P)
            if np.any(eigvals <= 0):
                raise np.linalg.LinAlgError
        except np.linalg.LinAlgError:
            logger.warning("卡尔曼协方差矩阵非正定，重置为单位矩阵。")
            self.P = np.eye(2) * max(self.sigma_obs ** 2, 1e-8)
        # 强制对称
        self.P = (self.P + self.P.T) / 2.0

    def _current_estimate(self) -> Dict[str, Any]:
        """返回当前状态估计结果。"""
        half_width = 2.0 * np.sqrt(max(abs(self.P[0, 0]), 0.0))
        return {
            'kma': float(self.x[0]),
            'kma_slope': float(self.x[1]),
            'kma_upper': float(self.x[0] + half_width),
            'kma_lower': float(self.x[0] - half_width),
            'sigma_obs': float(self.sigma_obs),
        }

    def reset(self) -> None:
        """重置卡尔曼滤波器状态。"""
        self.x = np.zeros(2)
        self.P = np.eye(2) * 1000.0
        self.sigma_obs = 1.0
        self._initialized = False
        self._call_count = 0

    def get_state(self) -> Dict[str, Any]:
        """返回当前内部状态，用于检查点保存。"""
        return {
            'x': self.x.tolist(),
            'P': self.P.tolist(),
            'sigma_obs': self.sigma_obs,
            'initialized': self._initialized,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """从检查点恢复内部状态。"""
        self.x = np.array(state['x'])
        self.P = np.array(state['P'])
        self.sigma_obs = state['sigma_obs']
        self._initialized = state.get('initialized', True)
        self._call_count = 0
