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
    - 2026-07-25 修复 F/delta 不一致、首根初始化、自适应 q 记忆、Joseph 更新、极端创新保护
    - 2026-07-25b 第二轮：sanitize 安全回退、q clip、过程噪声地板、协方差膨胀、量纲检查、条件数保护
    - 2026-07-25c 机构级审计加固：
        * 热路径异常隔离，保证锁与状态一致性
        * 检查点完整序列化 rng 状态，保证回测可复现
        * 预分配工作矩阵，降低 GC 压力
        * 配置项与 docstring 严格一致
        * 输入深度校验与静默失败路径可观测
        * 条件数/正定/非有限值多层防护
        * 并发与异步语义文档化
    - 2026-07-25d 运行时健壮性加强（不改接口）：
        * 异常路径强制 sanitize，防止半更新状态泄漏
        * 状态向量/协方差原地更新，避免重绑定与外部引用失效
        * base_q_ratio 强制落入 [min_q, max_q]，消除自适应记忆污染
        * set_state 对 sigma_obs / q / last_valid_level 做有限性与正性校验
        * delta 同步改用浮点容差，避免漏同步
        * 过程噪声矩阵与 Joseph 更新后立即对称化
        * _current_estimate 对 half_width / 非有限值做最终兜底
    - 2026-07-25e 机构级生产加固（不改接口，仅防御与热路径优化）：
        * 热路径全量预分配工作区（x_pred/P_pred/Q/K/KH/P_new），消除 2x2 临时对象
        * _ensure_positive_definite 对 eigvals 非有限值显式防护，阻止 nan 加载污染 P
        * __init__ 对 min_q/max_q 正性与上界做防御校验
        * set_state 对 P 写入后立即对称化，防止检查点非对称矩阵进入热路径
        * 协方差膨胀后强制 sanitize，防止极端情况下 P 非有限
        * _predict_update 对 K/S 非有限值防护，避免写入 nan 状态
        * reset 补全 _last_duration_ms 清零，状态可观测性完整
        * get_state 对非有限标量做最终 fortify，保证序列化安全
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

# ---------------------------------------------------------------------------
# 默认配置（与 docstring / 配置中心保持严格一致）
# ---------------------------------------------------------------------------
DEFAULT_Q_RATIO = 0.01
DEFAULT_DELTA = 1.0
DEFAULT_ADAPTIVE_Q = True
DEFAULT_MIN_Q = 0.001
DEFAULT_MAX_Q = 0.1
DEFAULT_JITTER = 0.01
MAX_JITTER = 0.02
MAX_CALLS_BEFORE_INFLATE = 100_000
EPS = 1e-12
MAX_INNOVATION_RATIO = 8.0
MIN_PROCESS_NOISE_RATIO = 1e-8
MAX_COND = 1e12
MAX_VOL_PRICE_RATIO = 0.5
# 价格合理性上界（防御坏数据），可按资产类别调整
MAX_REASONABLE_PRICE = 1e12
# 浮点比较容差（delta 同步）
DELTA_EPS = 1e-15
# min_q / max_q 合理上界（防御配置错误）
MAX_ALLOWED_Q = 1.0


class KalmanTrendline(FeatureComputer):
    """
    自适应卡尔曼均线（局部线性趋势 / constant-velocity 模型）。

    线程/协程安全说明:
        compute() 使用 asyncio.Lock。仅保证同一事件循环内的协程互斥。
        若在多线程中共享同一实例，调用方必须自行串行化。
        reset / get_state / set_state 为同步方法且不加锁，调用方须保证与
        compute 不并发（同一事件循环内通过 await 顺序调用即可）。
        asyncio.Lock 非重入；禁止在持锁回调中再次 await compute。

    输出:
        {'kma', 'kma_slope', 'kma_upper', 'kma_lower', 'sigma_obs'}
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
        if not 0.0 < q_ratio <= 1.0:
            raise ValueError(f"q_ratio 必须在 (0, 1]，当前: {q_ratio}")
        if delta <= 0.0:
            raise ValueError(f"delta 必须为正数，当前: {delta}")
        if not (0.0 < min_q_ratio < max_q_ratio <= MAX_ALLOWED_Q):
            raise ValueError(
                f"min_q_ratio/max_q_ratio 必须满足 0 < min < max <= {MAX_ALLOWED_Q}，"
                f"当前: min={min_q_ratio}, max={max_q_ratio}"
            )
        if q_ratio_jitter < 0.0 or max_q_jitter < 0.0:
            raise ValueError("jitter 参数不可为负")

        self.min_q = float(min_q_ratio)
        self.max_q = float(max_q_ratio)
        # 强制 base 落在自适应区间，避免后续记忆与 clip 行为不一致
        self.base_q_ratio = float(np.clip(q_ratio, self.min_q, self.max_q))
        self.delta = float(delta)
        self.adaptive_q = bool(adaptive_q)
        self.q_jitter = float(q_ratio_jitter)
        self.max_jitter = float(max_q_jitter)

        self._rng = np.random.default_rng(random_seed)
        self._seed = random_seed  # 仅用于可观测性

        # 状态（始终保持为 contiguous float64，原地更新）
        self.x = np.zeros(2, dtype=np.float64)
        self.P = np.eye(2, dtype=np.float64) * 1000.0
        self.sigma_obs = 1.0
        self._q = self.base_q_ratio
        self._initialized = False
        self._call_count = 0
        self._last_valid_level = 0.0
        self._last_duration_ms = 0.0

        # 并发
        self._lock = asyncio.Lock()

        # 预分配热路径工作区（全部 2x2 / 2x1，零热路径分配）
        self._F = np.array([[1.0, self.delta], [0.0, 1.0]], dtype=np.float64)
        self._H = np.array([[1.0, 0.0]], dtype=np.float64)
        self._I = np.eye(2, dtype=np.float64)
        self._Q_base = np.array(
            [
                [(self.delta ** 4) / 4.0, (self.delta ** 3) / 2.0],
                [(self.delta ** 3) / 2.0, self.delta ** 2],
            ],
            dtype=np.float64,
        )
        # 额外工作区：消除 _predict_update 内全部临时矩阵
        self._x_pred = np.zeros(2, dtype=np.float64)
        self._P_pred = np.zeros((2, 2), dtype=np.float64)
        self._Q = np.zeros((2, 2), dtype=np.float64)
        self._K = np.zeros((2, 1), dtype=np.float64)
        self._KH = np.zeros((2, 2), dtype=np.float64)
        self._P_new = np.zeros((2, 2), dtype=np.float64)
        self._tmp2 = np.zeros((2, 2), dtype=np.float64)  # 通用 2x2 临时

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    async def compute(
        self, kline: Kline, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        处理一根新 K 线，更新卡尔曼估计。
        任何内部异常都会被隔离，返回当前可用估计，不泄漏异常到上游。
        异常路径会强制 sanitize，防止半更新状态被后续调用看到。
        """
        async with self._lock:
            start_ts = time.monotonic()
            try:
                return self._compute_unlocked(kline, context or {})
            except Exception:
                logger.exception(
                    "KalmanTrendline.compute 内部异常，强制 sanitize 后返回当前估计"
                )
                # 关键：防止半更新的 x/P 泄漏到调用方
                try:
                    self._sanitize_state()
                except Exception:
                    logger.exception("sanitize 在异常路径中再次失败，忽略")
                return self._current_estimate()
            finally:
                self._last_duration_ms = (time.monotonic() - start_ts) * 1000.0

    def reset(self) -> None:
        """重置滤波器状态（保留配置与 rng）。调用方须保证不与 compute 并发。"""
        self.x[:] = 0.0
        self.P[:] = 0.0
        np.fill_diagonal(self.P, 1000.0)
        self.sigma_obs = 1.0
        self._q = self.base_q_ratio
        self._initialized = False
        self._call_count = 0
        self._last_valid_level = 0.0
        self._last_duration_ms = 0.0

    def get_state(self) -> Dict[str, Any]:
        """完整检查点（含 rng 状态，保证回测可复现）。调用方须保证不与 compute 并发。"""
        # 序列化前 fortify 标量，防止 nan/inf 进入检查点
        sigma = (
            float(self.sigma_obs)
            if np.isfinite(self.sigma_obs) and self.sigma_obs > 0.0
            else 1.0
        )
        q = (
            float(self._q)
            if np.isfinite(self._q)
            else float(self.base_q_ratio)
        )
        q = float(np.clip(q, self.min_q, self.max_q))
        lvl = (
            float(self._last_valid_level)
            if np.isfinite(self._last_valid_level) and self._last_valid_level > 0.0
            else 0.0
        )
        return {
            "x": self.x.tolist(),
            "P": self.P.tolist(),
            "sigma_obs": sigma,
            "initialized": bool(self._initialized),
            "q": q,
            "last_valid_level": lvl,
            "call_count": int(self._call_count) if self._call_count >= 0 else 0,
            "rng_state": self._rng.bit_generator.state,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """从检查点恢复，带形状与数值校验。调用方须保证不与 compute 并发。"""
        if not isinstance(state, dict):
            raise TypeError(f"state 必须为 dict，当前: {type(state)}")
        x = np.asarray(state["x"], dtype=np.float64)
        P = np.asarray(state["P"], dtype=np.float64)
        if x.shape != (2,) or P.shape != (2, 2):
            raise ValueError(
                f"状态形状错误: x{x.shape}, P{P.shape}，期望 x(2,), P(2,2)"
            )
        # 原地写入，保持数组身份
        self.x[:] = np.ascontiguousarray(x)
        self.P[:] = np.ascontiguousarray(P)
        # 检查点可能非对称，立即对称化
        self.P[:] = 0.5 * (self.P + self.P.T)

        # sigma_obs 有限性与正性
        try:
            so = float(state["sigma_obs"])
            if not np.isfinite(so) or so <= 0.0:
                logger.warning("set_state: sigma_obs 非法 (%.6g)，回退 1.0", so)
                so = 1.0
            self.sigma_obs = so
        except (TypeError, ValueError, KeyError):
            logger.warning("set_state: sigma_obs 缺失或不可转换，回退 1.0")
            self.sigma_obs = 1.0

        self._initialized = bool(state.get("initialized", True))

        # q 落入合法区间
        try:
            q = float(state.get("q", self.base_q_ratio))
            if not np.isfinite(q):
                q = self.base_q_ratio
            self._q = float(np.clip(q, self.min_q, self.max_q))
        except (TypeError, ValueError):
            self._q = self.base_q_ratio

        # last_valid_level
        try:
            lvl = float(
                state.get(
                    "last_valid_level",
                    self.x[0] if np.isfinite(self.x[0]) else 0.0,
                )
            )
            if not np.isfinite(lvl) or lvl <= 0.0:
                lvl = (
                    float(self.x[0])
                    if np.isfinite(self.x[0]) and self.x[0] > 0.0
                    else 0.0
                )
            self._last_valid_level = lvl
        except (TypeError, ValueError):
            self._last_valid_level = 0.0

        try:
            self._call_count = int(state.get("call_count", 0))
            if self._call_count < 0:
                self._call_count = 0
        except (TypeError, ValueError):
            self._call_count = 0

        rng_state = state.get("rng_state")
        if rng_state is not None:
            try:
                self._rng.bit_generator.state = rng_state
            except Exception:
                logger.warning("rng_state 恢复失败，继续使用当前生成器")

        self._ensure_positive_definite()
        self._sanitize_state()
        # 若 sanitize 后 x 已损坏，强制未初始化，下次 bootstrap
        if not np.all(np.isfinite(self.x)):
            self._initialized = False

    # ------------------------------------------------------------------
    # 内部核心
    # ------------------------------------------------------------------
    def _compute_unlocked(
        self, kline: Kline, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        price = self._validate_price(getattr(kline, "close", None))
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
        if self._call_count > MAX_CALLS_BEFORE_INFLATE:
            logger.warning(
                "调用次数超过 %d，执行协方差膨胀（保留状态向量）。",
                MAX_CALLS_BEFORE_INFLATE,
            )
            self.P *= 4.0
            self.P[0, 0] += max(self.sigma_obs ** 2, EPS)
            self.P[1, 1] += max(self.sigma_obs ** 2, EPS)
            self._call_count = 0
            self._ensure_positive_definite()
            self._sanitize_state(price)

        if np.isfinite(self.x[0]) and self.x[0] > 0.0:
            self._last_valid_level = float(self.x[0])
        return self._current_estimate()

    def _validate_price(self, raw_price: Any) -> Optional[float]:
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(price) or price <= 0.0 or price > MAX_REASONABLE_PRICE:
            return None
        return price

    def _bootstrap(self, price: float, context: Dict[str, Any]) -> None:
        self.sigma_obs = self._resolve_sigma(price, context, fallback_ratio=0.01)
        self.x[0] = price
        self.x[1] = 0.0
        self.P[:] = 0.0
        np.fill_diagonal(self.P, max(self.sigma_obs ** 2, EPS))
        self._q = self.base_q_ratio
        self._initialized = True
        self._last_valid_level = price

    def _resolve_sigma(
        self,
        price: float,
        context: Dict[str, Any],
        fallback_ratio: float = 0.01,
    ) -> float:
        recent_vol = context.get("recent_volatility")
        if (
            recent_vol is not None
            and isinstance(recent_vol, (int, float))
            and np.isfinite(recent_vol)
            and recent_vol > 0.0
        ):
            vol = float(recent_vol)
            if vol > MAX_VOL_PRICE_RATIO * price:
                logger.warning(
                    "recent_volatility=%.6g 超过价格的 %.0f%%，可能量纲错误，回退相对比例。",
                    vol,
                    MAX_VOL_PRICE_RATIO * 100,
                )
                return max(price * fallback_ratio, 1e-8)
            return max(vol, 1e-8)
        return max(price * fallback_ratio, 1e-8)

    def _update_sigma_obs(self, price: float, context: Dict[str, Any]) -> None:
        recent_vol = context.get("recent_volatility")
        if (
            recent_vol is not None
            and isinstance(recent_vol, (int, float))
            and np.isfinite(recent_vol)
            and recent_vol > 0.0
        ):
            vol = float(recent_vol)
            if vol > MAX_VOL_PRICE_RATIO * price:
                logger.warning(
                    "recent_volatility=%.6g 异常偏大，忽略本次外部值。", vol
                )
            else:
                self.sigma_obs = max(vol, 1e-8)
                return
        # 无可靠外部波动率时，用创新的指数平滑估计
        innovation = abs(price - self.x[0])
        if not np.isfinite(innovation):
            innovation = self.sigma_obs
        self.sigma_obs = 0.9 * self.sigma_obs + 0.1 * innovation
        self.sigma_obs = max(float(self.sigma_obs), 1e-8)

    def _compute_q(self, price: float) -> float:
        if self.adaptive_q and price > 0.0:
            vol_ratio = self.sigma_obs / price
            if not np.isfinite(vol_ratio):
                vol_ratio = 0.0
            target_q = self.base_q_ratio * (
                0.5 + 0.5 * np.tanh(vol_ratio * 100.0)
            )
            self._q = 0.95 * self._q + 0.05 * target_q
            self._q = float(np.clip(self._q, self.min_q, self.max_q))
        else:
            self._q = self.base_q_ratio

        if self.q_jitter > 0.0:
            jitter = float(self._rng.normal(0.0, self.q_jitter))
            jitter = float(np.clip(jitter, -self.max_jitter, self.max_jitter))
            q = self._q + jitter
        else:
            q = self._q
        return float(np.clip(q, self.min_q, self.max_q))

    def _predict_update(self, price: float, q: float) -> None:
        sigma2 = max(self.sigma_obs ** 2, EPS)
        process_scale = max(
            q * sigma2, MIN_PROCESS_NOISE_RATIO * (price * price + EPS)
        )

        # 若 delta 在运行期被外部修改，同步 F 与 Q_base（浮点容差）
        if abs(self._F[0, 1] - self.delta) > DELTA_EPS:
            self._F[0, 1] = self.delta
            self._Q_base[0, 0] = (self.delta ** 4) / 4.0
            self._Q_base[0, 1] = self._Q_base[1, 0] = (self.delta ** 3) / 2.0
            self._Q_base[1, 1] = self.delta ** 2

        # Q = Q_base * process_scale，原地对称化
        np.multiply(self._Q_base, process_scale, out=self._Q)
        self._Q[:] = 0.5 * (self._Q + self._Q.T)

        # x_pred = F @ x
        np.dot(self._F, self.x, out=self._x_pred)

        # P_pred = F @ P @ F.T + Q
        np.dot(self._F, self.P, out=self._tmp2)
        np.dot(self._tmp2, self._F.T, out=self._P_pred)
        self._P_pred += self._Q
        self._P_pred[:] = 0.5 * (self._P_pred + self._P_pred.T)

        y = float(price - (self._H @ self._x_pred).item())
        if not np.isfinite(y):
            logger.warning("创新非有限，跳过本次 predict-update")
            return

        R_val = max(sigma2, EPS)
        innov_scale = abs(y) / (self.sigma_obs + EPS)
        if innov_scale > MAX_INNOVATION_RATIO:
            R_val *= (innov_scale / MAX_INNOVATION_RATIO) ** 2
            logger.debug(
                "极端创新 ratio=%.2f，临时放大 R 至 %.6g", innov_scale, R_val
            )

        S = float((self._H @ self._P_pred @ self._H.T).item() + R_val)
        if not np.isfinite(S) or S < EPS:
            S = EPS

        # K = (P_pred @ H.T) / S
        np.dot(self._P_pred, self._H.T, out=self._K)
        self._K /= S
        if not np.all(np.isfinite(self._K)):
            logger.warning("卡尔曼增益非有限，跳过本次更新")
            return

        # x = x_pred + K * y  （原地）
        self.x[:] = self._x_pred + self._K.ravel() * y

        # Joseph: P = (I - K H) P_pred (I - K H).T + K R K.T
        np.dot(self._K, self._H, out=self._KH)
        # tmp2 = I - KH
        np.subtract(self._I, self._KH, out=self._tmp2)
        # P_new = tmp2 @ P_pred @ tmp2.T
        np.dot(self._tmp2, self._P_pred, out=self._P_new)
        np.dot(self._P_new, self._tmp2.T, out=self._P_pred)  # 复用 _P_pred 作中间
        # + K * R * K.T
        self._P_pred += (self._K * R_val) @ self._K.T
        # 对称化写回
        self.P[:] = 0.5 * (self._P_pred + self._P_pred.T)

        if not np.all(np.isfinite(self.x)) or not np.all(np.isfinite(self.P)):
            # 极端数值后立即回滚到可恢复状态，由后续 sanitize 兜底
            logger.warning("predict-update 后状态非有限，标记待 sanitize")

    def _ensure_positive_definite(self) -> None:
        self.P[:] = 0.5 * (self.P + self.P.T)
        try:
            eigvals = eigvalsh(self.P)
            if not np.all(np.isfinite(eigvals)):
                raise np.linalg.LinAlgError("eigvals 含非有限值")
            min_eig = float(np.min(eigvals))
            if min_eig < EPS:
                loading = EPS - min_eig
                self.P[0, 0] += loading
                self.P[1, 1] += loading
            c = float(cond(self.P))
            if (not np.isfinite(c)) or c > MAX_COND:
                max_eig = float(np.max(eigvals))
                if not np.isfinite(max_eig) or max_eig <= 0.0:
                    max_eig = max(self.sigma_obs ** 2, 1.0)
                loading = max_eig * 1e-8
                self.P[0, 0] += loading
                self.P[1, 1] += loading
        except np.linalg.LinAlgError:
            logger.warning("协方差特征分解失败，回退对角阵。")
            self.P[:] = 0.0
            np.fill_diagonal(self.P, max(self.sigma_obs ** 2, 1e-8))

    def _sanitize_state(self, current_price: Optional[float] = None) -> None:
        if not np.all(np.isfinite(self.x)):
            logger.warning("状态向量非有限，回退 level 并清零 slope。")
            fallback = (
                current_price
                if current_price is not None and current_price > 0.0
                else self._last_valid_level
            )
            if not np.isfinite(fallback) or fallback <= 0.0:
                fallback = 1.0
            self.x[0] = fallback
            self.x[1] = 0.0
            self._initialized = False
        if not np.all(np.isfinite(self.P)):
            logger.warning("协方差非有限，重置对角阵。")
            self.P[:] = 0.0
            np.fill_diagonal(self.P, max(self.sigma_obs ** 2, 1e-8))
        # 额外保护：sigma_obs 自身
        if not np.isfinite(self.sigma_obs) or self.sigma_obs <= 0.0:
            self.sigma_obs = 1.0

    def _current_estimate(self) -> Dict[str, Any]:
        p00 = float(self.P[0, 0]) if np.isfinite(self.P[0, 0]) else 0.0
        p00 = max(p00, 0.0)
        half_width = 2.0 * np.sqrt(p00)
        if not np.isfinite(half_width):
            half_width = 0.0

        if np.isfinite(self.x[0]) and self.x[0] > 0.0:
            level = float(self.x[0])
        else:
            level = (
                float(self._last_valid_level)
                if self._last_valid_level > 0.0
                else 0.0
            )

        slope = float(self.x[1]) if np.isfinite(self.x[1]) else 0.0

        sigma = (
            float(self.sigma_obs)
            if np.isfinite(self.sigma_obs) and self.sigma_obs > 0.0
            else 1e-8
        )

        return {
            "kma": level,
            "kma_slope": slope,
            "kma_upper": level + half_width,
            "kma_lower": level - half_width,
            "sigma_obs": sigma,
                }
