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
        'context.recent_volatility': '近期波动率，用于观测噪声估计 (可选，需与价格同量纲)'
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
    - 2026-07-25b 第二轮加固：
        * sanitize 不再把 level 置 0（保留上次有效值）
        * q 最终强制 clip，防止 jitter 越界
        * 过程噪声增加相对地板，避免低波动期 Q→0
        * 100k 次触发改为协方差膨胀而非完全重置，消除状态跳变
        * recent_volatility 量纲合理性检查
        * P 条件数保护与 set_state 形状校验
        * 创新保护与 σ 更新的边界更严谨
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import numpy as np
from numpy.linalg import eigvalsh, cond

from core.interfaces import FeatureComputer
from core.models import Kline

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_Q_RATIO = 0.01
DEFAULT_DELTA = 1.0
DEFAULT_ADAPTIVE_Q = True
DEFAULT_MIN_Q = 0.001
DEFAULT_MAX_Q = 0.1
DEFAULT_JITTER = 0.01
MAX_JITTER = 0.02
MAX_CALLS_BEFORE_RESET = 100_000
EPS = 1e-12
MAX_INNOVATION_RATIO = 8.0
# 过程噪声相对地板：即使 σ 很小，也保证 Q 不会完全消失
MIN_PROCESS_NOISE_RATIO = 1e-8
# 协方差条件数上限，超过则对角加载
MAX_COND = 1e12
# recent_volatility 合理性：超过价格 50% 视为可能量纲错误
MAX_VOL_PRICE_RATIO = 0.5


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

        self._rng = np.random.default_rng(random_seed)

        self.x = np.zeros(2, dtype=np.float64)
        self.P = np.eye(2, dtype=np.float64) * 1000.0
        self.sigma_obs = 1.0
        self._q = self.base_q_ratio
        self._initialized = False
        self._call_count = 0
        self._lock = asyncio.Lock()
        self._last_duration_ms = 0.0
        # 记录上一次有效 level，供 sanitize 回退使用
        self._last_valid_level = 0.0

    async def compute(self, kline: Kline, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        处理一根新 K 线，更新卡尔曼估计。
        """
        async with self._lock:
            start_ts = time.monotonic()
            context = context or {}

            price = self._validate_price(kline.close)
            if price is None:
                logger.warning("收到无效收盘价，返回当前状态估计。")
                return self._current_estimate()

            if not self._initialized:
                self._bootstrap(price, context)
            else:
                self._update_sigma_obs(price, context)

            q = self._compute_q(price)
            self._predict_update(price, q)

            self._ensure_positive_definite()
            self._sanitize_state(price)

            self._call_count += 1
            if self._call_count > MAX_CALLS_BEFORE_RESET:
                # 温和处理：协方差膨胀 + 重置计数，避免状态突变
                logger.warning(
                    "卡尔曼滤波器调用次数过多，执行协方差膨胀（保留状态向量）。"
                )
                self.P = self.P * 4.0 + np.eye(2, dtype=np.float64) * (self.sigma_obs ** 2)
                self._call_count = 0
                self._ensure_positive_definite()

            self._last_valid_level = float(self.x[0])
            self._last_duration_ms = (time.monotonic() - start_ts) * 1000
            return self._current_estimate()

    def _validate_price(self, raw_price: Any) -> Optional[float]:
        try:
            price = float(raw_price)
            if price <= 0 or not np.isfinite(price):
                raise ValueError
            return price
        except (TypeError, ValueError):
            return None

    def _bootstrap(self, price: float, context: Dict[str, Any]) -> None:
        """首根或恢复后的快速初始化，保证 sigma 与 P 一致。"""
        self.sigma_obs = self._resolve_sigma(price, context, fallback_ratio=0.01)
        self.x[0] = price
        self.x[1] = 0.0
        self.P = np.eye(2, dtype=np.float64) * (self.sigma_obs ** 2)
        self._q = self.base_q_ratio
        self._initialized = True
        self._last_valid_level = price

    def _resolve_sigma(
        self, price: float, context: Dict[str, Any], fallback_ratio: float = 0.01
    ) -> float:
        """统一解析观测噪声，带量纲合理性检查。"""
        recent_vol = context.get('recent_volatility')
        if recent_vol is not None and isinstance(recent_vol, (int, float)) and recent_vol > 0:
            vol = float(recent_vol)
            if vol > MAX_VOL_PRICE_RATIO * price:
                logger.warning(
                    "recent_volatility=%.6g 超过价格的 %.0f%%，可能量纲错误，回退到相对比例。",
                    vol, MAX_VOL_PRICE_RATIO * 100,
                )
                return max(price * fallback_ratio, 1e-8)
            return max(vol, 1e-8)
        return max(price * fallback_ratio, 1e-8)

    def _update_sigma_obs(self, price: float, context: Dict[str, Any]) -> None:
        """更新观测噪声标准差。"""
        recent_vol = context.get('recent_volatility')
        if recent_vol is not None and isinstance(recent_vol, (int, float)) and recent_vol > 0:
            vol = float(recent_vol)
            if vol > MAX_VOL_PRICE_RATIO * price:
                logger.warning(
                    "recent_volatility=%.6g 异常偏大，忽略本次外部值。", vol
                )
            else:
                self.sigma_obs = vol
                self.sigma_obs = max(float(self.sigma_obs), 1e-8)
                return

        # 无可靠外部值时用创新的指数平滑
        innovation = abs(price - self.x[0])
        self.sigma_obs = 0.9 * self.sigma_obs + 0.1 * innovation
        self.sigma_obs = max(float(self.sigma_obs), 1e-8)

    def _compute_q(self, price: float) -> float:
        """计算自适应过程噪声比（带记忆 + 可选微扰 + 最终强制 clip）。"""
        if self.adaptive_q and price > 0:
            vol_ratio = self.sigma_obs / price
            target_q = self.base_q_ratio * (0.5 + 0.5 * np.tanh(vol_ratio * 100.0))
            self._q = 0.95 * self._q + 0.05 * target_q
            self._q = float(np.clip(self._q, self.min_q, self.max_q))
        else:
            self._q = self.base_q_ratio

        if self.q_jitter > 0:
            jitter = float(self._rng.normal(0.0, self.q_jitter))
            jitter = float(np.clip(jitter, -self.max_jitter, self.max_jitter))
            q = self._q + jitter
        else:
            q = self._q

        # 最终强制落入合法区间，防止 jitter 越界
        return float(np.clip(q, self.min_q, self.max_q))

    def _predict_update(self, price: float, q: float) -> None:
        """执行卡尔曼预测与更新（Joseph 形式 + 极端创新保护 + 过程噪声地板）。"""
        dt = self.delta
        q11 = (dt ** 4) / 4.0
        q12 = (dt ** 3) / 2.0
        q22 = dt ** 2

        # 过程噪声 = 自适应部分 + 相对地板，防止低波动期 Q 完全消失
        sigma2 = max(self.sigma_obs ** 2, EPS)
        process_scale = max(q * sigma2, MIN_PROCESS_NOISE_RATIO * (price ** 2 + EPS))
        Q = np.array([[q11, q12], [q12, q22]], dtype=np.float64) * process_scale

        F = np.array([[1.0, dt], [0.0, 1.0]], dtype=np.float64)
        H = np.array([[1.0, 0.0]], dtype=np.float64)
        R_val = max(sigma2, EPS)
        R = np.array([[R_val]], dtype=np.float64)

        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + Q
        P_pred = 0.5 * (P_pred + P_pred.T)

        y = float(price - (H @ x_pred).item())

        # 极端创新保护
        innov_scale = abs(y) / (self.sigma_obs + EPS)
        if innov_scale > MAX_INNOVATION_RATIO:
            R_val = R_val * (innov_scale / MAX_INNOVATION_RATIO) ** 2
            R[0, 0] = R_val
            logger.debug(
                "极端创新 (ratio=%.2f)，临时放大 R 至 %.6g", innov_scale, R_val
            )

        S = (H @ P_pred @ H.T + R).item()
        S = max(S, EPS)

        K = (P_pred @ H.T) / S
        self.x = x_pred + (K.flatten() * y)

        I = np.eye(2, dtype=np.float64)
        KH = K @ H
        self.P = (I - KH) @ P_pred @ (I - KH).T + (K * R_val) @ K.T
        self.P = 0.5 * (self.P + self.P.T)

    def _ensure_positive_definite(self) -> None:
        """确保协方差对称正定，并控制条件数。"""
        self.P = 0.5 * (self.P + self.P.T)

        try:
            eigvals = eigvalsh(self.P)
            min_eig = float(np.min(eigvals))
            if min_eig < EPS:
                loading = EPS - min_eig
                self.P[0, 0] += loading
                self.P[1, 1] += loading
                logger.debug("协方差最小特征值 %.3e，已对角加载 %.3e", min_eig, loading)

            # 条件数保护：过大时增加对角加载
            c = float(cond(self.P))
            if c > MAX_COND or not np.isfinite(c):
                loading = float(np.max(eigvals)) * 1e-8
                self.P[0, 0] += loading
                self.P[1, 1] += loading
                logger.debug("协方差条件数过大 (%.3e)，已额外加载 %.3e", c, loading)
        except np.linalg.LinAlgError:
            logger.warning("协方差特征值分解失败，回退到与 sigma_obs 同量级的对角矩阵。")
            self.P = np.eye(2, dtype=np.float64) * max(self.sigma_obs ** 2, 1e-8)

    def _sanitize_state(self, current_price: Optional[float] = None) -> None:
        """防止 NaN/Inf 污染；level 回退到上次有效值或当前价格，绝不置 0。"""
        if not np.all(np.isfinite(self.x)):
            logger.warning("状态向量出现非有限值，回退 level 并清零 slope。")
            fallback = (
                current_price
                if current_price is not None and current_price > 0
                else self._last_valid_level
            )
            if fallback <= 0:
                fallback = 1.0  # 最后兜底，避免后续除零
            self.x = np.array([fallback, 0.0], dtype=np.float64)
            self._initialized = False  # 下一根重新 bootstrap 更安全

        if not np.all(np.isfinite(self.P)):
            logger.warning("协方差矩阵出现非有限值，重置。")
            self.P = np.eye(2, dtype=np.float64) * max(self.sigma_obs ** 2, 1e-8)

    def _current_estimate(self) -> Dict[str, Any]:
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
        self._last_valid_level = 0.0

    def get_state(self) -> Dict[str, Any]:
        """返回当前内部状态，用于检查点保存。"""
        return {
            'x': self.x.tolist(),
            'P': self.P.tolist(),
            'sigma_obs': self.sigma_obs,
            'initialized': self._initialized,
            'q': self._q,
            'last_valid_level': self._last_valid_level,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """从检查点恢复内部状态，带形状与数值校验。"""
        x = np.asarray(state['x'], dtype=np.float64)
        P = np.asarray(state['P'], dtype=np.float64)
        if x.shape != (2,) or P.shape != (2, 2):
            raise ValueError(
                f"状态形状错误: x{x.shape}, P{P.shape}，期望 x(2,), P(2,2)"
            )
        self.x = x
        self.P = P
        self.sigma_obs = float(state['sigma_obs'])
        self._initialized = bool(state.get('initialized', True))
        self._q = float(state.get('q', self.base_q_ratio))
        self._last_valid_level = float(
            state.get('last_valid_level', self.x[0] if np.isfinite(self.x[0]) else 0.0)
        )
        self._call_count = 0
        self._ensure_positive_definite()
        self._sanitize_state()
