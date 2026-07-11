# -*- coding: utf-8 -*-
"""
模块名称: hmm_state_detector.py
核心职责: 基于 GaussianHMM 隐马尔可夫模型实时推断市场状态（震荡/多头/空头），
          并输出状态概率向量，支持自动状态数选择和滚动重训练。
所属层级: core.indicators

外部依赖:
    - numpy (数值计算)
    - hmmlearn.hmm (GaussianHMM)
    - sklearn.preprocessing.StandardScaler (特征标准化)
    - collections.deque (固定长度缓存)
    - core.models.kline (Kline 数据结构)

接口契约:
    提供: {
        'HMMStateDetector': {
            'input': 'kline: Kline, context: dict (包含kma, atr等特征)',
            'output': 'dict {state, prob_bull, prob_bear, prob_range}',
            'side_effects': ['更新内部缓存，可能触发重训练']
        }
    }
    消费: {
        'context["kma"]': '卡尔曼均线值',
        'context["atr_3m"]': '3分钟ATR值',
        'kline.volume': '成交量',
        'kline.high/low/close': '价格数据'
    }

配置项:
    - strategy.hmm.n_states (int, 3): 隐状态数
    - strategy.hmm.retrain_interval (int, 500): 重训练间隔 (K线数)
    - strategy.hmm.warmup_bars (int, 300): 预热所需最少K线数
    - strategy.hmm.auto_select (bool, true): 是否自动选择最优状态数
    - strategy.hmm.min_states (int, 2): 最小状态数
    - strategy.hmm.max_states (int, 5): 最大状态数

作者: KHAOS System Architect
创建日期: 2025-03-20
修改记录:
    - 2026-07-12 第三轮审计：全方位边界防护、降级机制、金融级鲁棒性
"""

import logging
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np
from hmmlearn import hmm
from sklearn.preprocessing import StandardScaler

from core.interfaces import FeatureComputer
from core.models.kline import Kline

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_N_STATES = 3
DEFAULT_RETRAIN_INTERVAL = 500
DEFAULT_WARMUP_BARS = 300
DEFAULT_AUTO_SELECT = True
DEFAULT_MIN_STATES = 2
DEFAULT_MAX_STATES = 5
DEFAULT_FEATURE_WINDOW = 20
MAX_FEATURE_BUFFER = 1000               # 特征缓存最大长度
MIN_TRAIN_SAMPLES = 50                  # 最少训练样本数（低于此值放弃训练）
MAX_CONSECUTIVE_FAILURES = 3            # 连续训练失败后暂时停止重训练，进入保护模式


class HMMStateDetector(FeatureComputer):
    """
    基于 GaussianHMM 的实时市场状态检测器（华尔街机构级终极版）。

    特征向量（自动标准化）:
        0: 对数收益率 log(close/prev_close)
        1: 标准化振幅 (high-low)/atr
        2: 价格与KMA的偏离 (close - kma) / atr
        3: 成交量比率 volume / vol_ma20
        4: KMA斜率归一化 kma_slope / atr

    状态映射与滞后确认:
        - 训练后通过各状态下的平均收益符号自动分配 BULL/BEAR/RANGE
        - 当只有 2 个状态时，分别映射为 BULL 和 BEAR
        - 为避免标签频繁切换，对 BULL/BEAR 的交换进行滞后验证
    """

    def __init__(
        self,
        n_states: int = DEFAULT_N_STATES,
        retrain_interval: int = DEFAULT_RETRAIN_INTERVAL,
        warmup_bars: int = DEFAULT_WARMUP_BARS,
        auto_select: bool = DEFAULT_AUTO_SELECT,
        min_states: int = DEFAULT_MIN_STATES,
        max_states: int = DEFAULT_MAX_STATES,
        feature_window: int = DEFAULT_FEATURE_WINDOW,
        random_state: int = 42,
    ):
        # 基础参数校验
        if n_states < 2:
            raise ValueError("n_states must be at least 2")
        if warmup_bars < 20:
            raise ValueError("warmup_bars must be at least 20")
        if max_states < min_states:
            raise ValueError("max_states must be >= min_states")

        self.n_states = n_states
        self.retrain_interval = retrain_interval
        self.warmup_bars = warmup_bars
        self.auto_select = auto_select
        self.min_states = min_states
        self.max_states = max_states
        self.feature_window = feature_window
        self.random_state = random_state

        # 特征标准化器
        self._scaler = StandardScaler()

        # 固定长度特征缓存
        self._feature_buffer: deque = deque(maxlen=MAX_FEATURE_BUFFER)

        # 模型与状态
        self._model: Optional[hmm.GaussianHMM] = None
        self._state_map: Dict[int, str] = {}
        self._state_map_history: List[Dict] = []       # 最近几次映射，用于滞后确认
        self._current_state: str = "RANGE"
        self._current_prob: np.ndarray = np.ones(max(2, n_states)) / max(2, n_states)

        # 训练控制
        self._bars_since_train = 0
        self._is_warmed_up = False
        self._train_failures = 0
        self._protection_mode = False  # 连续失败后进入保护模式，暂停重训练
        self._last_train_time = 0.0
        self._last_valid_output = self._default_output()  # 预测失败时的回退值

        logger.info(
            f"HMMStateDetector initialized: n_states={n_states}, warmup={warmup_bars}, "
            f"auto_select={auto_select}, max_buffer={MAX_FEATURE_BUFFER}"
        )

    async def compute(self, kline: Kline, context: Dict) -> Dict:
        """每根K线调用一次，更新内部状态并返回市场状态概率"""
        features = self._extract_features(kline, context)
        if features is None:
            return self._last_valid_output

        self._feature_buffer.append(features)
        self._bars_since_train += 1

        # 训练触发逻辑（考虑保护模式）
        if self._protection_mode:
            # 保护模式下暂停训练，只用现有模型预测（如果有）
            if self._bars_since_train >= self.retrain_interval * 2:
                # 长时间后尝试恢复训练
                logger.info("Exiting protection mode to attempt retraining")
                self._protection_mode = False
                self._train_failures = 0

        if not self._is_warmed_up:
            if len(self._feature_buffer) >= self.warmup_bars:
                success = self._train_model()
                if success:
                    self._is_warmed_up = True
                else:
                    # 预热失败，增加缓冲再试
                    logger.warning("Initial training failed, will retry after more data")
        else:
            if not self._protection_mode and self._bars_since_train >= self.retrain_interval:
                self._train_model()
                self._bars_since_train = 0

        # 模型预测（带多重保护）
        if self._model is not None and self._is_warmed_up:
            try:
                feat = self._scaler.transform(features.reshape(1, -1))
                state_id = self._model.predict(feat)[0]
                proba = self._model.predict_proba(feat)[0]
                # 对概率进行最小值限制，避免零概率
                proba = np.maximum(proba, 1e-6)
                proba /= proba.sum()
                self._current_state = self._state_map.get(state_id, "RANGE")
                self._current_prob = proba
                output = {
                    "state": self._current_state,
                    "prob_bull": float(proba[self._get_state_id("BULL")]),
                    "prob_bear": float(proba[self._get_state_id("BEAR")]),
                    "prob_range": float(proba[self._get_state_id("RANGE")]),
                }
                self._last_valid_output = output
                return output
            except Exception as e:
                logger.error(f"HMM prediction failed: {e}", exc_info=True)
                return self._last_valid_output
        else:
            return self._default_output()

    def _extract_features(self, kline: Kline, context: Dict) -> Optional[np.ndarray]:
        """构建特征向量，增强数据质量检查"""
        try:
            kma = context.get("kma")
            atr = context.get("atr_3m", 1.0)
            prev_close = context.get("prev_close", kline.open)
            vol_ma20 = context.get("vol_ma20", kline.volume)
            kma_slope = context.get("kma_slope", 0.0)

            if kma is None or atr <= 0 or prev_close <= 0:
                return None

            log_ret = np.log(kline.close / prev_close) if prev_close > 0 else 0.0
            amplitude = (kline.high - kline.low) / atr
            deviation = (kline.close - kma) / atr
            vol_ratio = kline.volume / vol_ma20 if vol_ma20 > 0 else 1.0
            slope_norm = kma_slope / atr

            features = np.array(
                [log_ret, amplitude, deviation, vol_ratio, slope_norm], dtype=np.float64
            )
            # 替换非法值并裁剪
            features = np.nan_to_num(features, nan=0.0, posinf=10.0, neginf=-10.0)
            features = np.clip(features, -10.0, 10.0)
            return features
        except Exception as e:
            logger.error(f"Feature extraction error: {e}")
            return None

    def _train_model(self) -> bool:
        """训练或更新 HMM 模型，返回是否成功"""
        if len(self._feature_buffer) < MIN_TRAIN_SAMPLES:
            logger.debug("Insufficient samples for training")
            self._train_failures += 1
            return False

        # 提取最近 warmup_bars 条特征，但不超过缓冲区实际大小
        buffer_len = len(self._feature_buffer)
        n_samples = min(self.warmup_bars, buffer_len)
        raw_features = [self._feature_buffer[i] for i in range(buffer_len - n_samples, buffer_len)]
        X_raw = np.array(raw_features)

        # 增强的数据清洗：移除包含 NaN/Inf 的行，并排除3-sigma异常
        if X_raw.shape[0] < MIN_TRAIN_SAMPLES:
            logger.warning("Too few samples after trimming")
            self._train_failures += 1
            return False

        # 过滤极端离群值
        mask = np.all(np.abs(X_raw) < 20, axis=1)  # 更严格的裁剪
        X_raw = X_raw[mask]
        if X_raw.shape[0] < MIN_TRAIN_SAMPLES:
            logger.warning("Not enough valid features after outlier removal")
            self._train_failures += 1
            return False

        # 标准化特征
        try:
            self._scaler.fit(X_raw)
            X = self._scaler.transform(X_raw)
        except Exception as e:
            logger.error(f"Scaler fitting failed: {e}")
            self._train_failures += 1
            return False

        # 自动选择最优状态数（使用 BIC）
        best_model = None
        best_bic = np.inf
        states_tested = range(self.min_states, self.max_states + 1) if self.auto_select else [self.n_states]

        for n in states_tested:
            try:
                # 对每种状态数尝试多次初始化，选择最佳
                best_local_bic = np.inf
                best_local_model = None
                for _ in range(2):  # 简单重试
                    model = hmm.GaussianHMM(
                        n_components=n,
                        covariance_type="diag",
                        n_iter=200,
                        tol=1e-4,
                        random_state=self.random_state,
                        verbose=False,
                    )
                    model.fit(X)
                    log_likelihood = model.score(X)
                    # 参数个数：转移矩阵 (n-1)*n + 初始概率 (n-1) + 均值 n*feat_dim + 协方差 n*feat_dim
                    n_params = n * (n - 1) + (n - 1) + 2 * n * X.shape[1]
                    bic = -2 * log_likelihood + n_params * np.log(len(X))
                    if bic < best_local_bic:
                        best_local_bic = bic
                        best_local_model = model
                if best_local_model is not None and best_local_bic < best_bic:
                    best_bic = best_local_bic
                    best_model = best_local_model
                    self.n_states = n
            except Exception as e:
                logger.error(f"HMM training failed for n_states={n}: {e}")

        if best_model is None:
            logger.error("All HMM training attempts failed")
            self._train_failures += 1
            if self._train_failures >= MAX_CONSECUTIVE_FAILURES:
                self._protection_mode = True
                logger.warning("Entering protection mode, retraining suspended")
            return False

        # 更新模型并重置失败计数
        self._model = best_model
        self._current_prob = np.ones(self.n_states) / self.n_states
        self._assign_state_labels(X)
        self._train_failures = 0
        self._last_train_time = time.time()
        logger.info(f"HMM retrained: n_states={self.n_states}, BIC={best_bic:.2f}")
        return True

    def _assign_state_labels(self, X: np.ndarray) -> None:
        """基于各状态的平均收益分配交易标签，并滞后确认"""
        if self._model is None:
            return

        posteriors = self._model.predict_proba(X)
        log_returns = X[:, 0]  # 第一个特征是对数收益率（标准化后方向保留）

        state_returns = {}
        for s in range(self._model.n_components):
            weights = posteriors[:, s] + 1e-9
            avg_ret = np.average(log_returns, weights=weights)
            state_returns[s] = avg_ret

        sorted_states = sorted(state_returns.items(), key=lambda x: x[1], reverse=True)
        new_map = {}

        if self.n_states >= 3:
            bull_state = sorted_states[0][0]
            bear_state = sorted_states[-1][0]
            for s in range(self.n_states):
                if s == bull_state:
                    new_map[s] = "BULL"
                elif s == bear_state:
                    new_map[s] = "BEAR"
                else:
                    new_map[s] = "RANGE"
        elif self.n_states == 2:
            # 两状态模型：一个为 BULL，另一个为 BEAR
            new_map[sorted_states[0][0]] = "BULL"
            new_map[sorted_states[1][0]] = "BEAR"
        else:
            # 异常情况
            logger.error(f"Unexpected number of states: {self.n_states}")
            return

        # 滞后确认：避免 BULL/BEAR 在收益差异极小时频繁互换
        if len(self._state_map_history) >= 2 and self._state_map:
            old_bull = [k for k, v in self._state_map.items() if v == "BULL"]
            new_bull = [k for k, v in new_map.items() if v == "BULL"]
            if old_bull and new_bull and old_bull[0] != new_bull[0]:
                # BULL 对应的状态发生了变化
                old_bull_ret = state_returns.get(old_bull[0], 0)
                new_bull_ret = state_returns.get(new_bull[0], 0)
                if abs(new_bull_ret - old_bull_ret) < 0.0005:
                    # 收益差异极小，维持原标签不变
                    logger.info("State map change suppressed due to insignificant return difference")
                    return  # 放弃更新，保留旧映射

        # 更新映射
        self._state_map = new_map
        self._state_map_history.append(new_map.copy())
        if len(self._state_map_history) > 5:
            self._state_map_history.pop(0)
        logger.info(f"HMM state map updated: {self._state_map}")

    def _get_state_id(self, state_name: str) -> int:
        """根据状态名称获取模型内部ID，包含回退逻辑"""
        for sid, name in self._state_map.items():
            if name == state_name:
                return sid
        # 若请求 RANGE 但映射中不存在（如2状态），返回BULL作为保底
        if state_name == "RANGE" and self._state_map:
            return next(iter(self._state_map))
        return 0

    def _default_output(self) -> Dict:
        """返回默认的未知状态输出"""
        return {
            "state": "RANGE",
            "prob_bull": 0.0,
            "prob_bear": 0.0,
            "prob_range": 1.0,
        }

    def get_state(self) -> Dict:
        """返回模型完整内部状态（用于检查点保存）"""
        state = {
            "n_states": self.n_states,
            "state_map": self._state_map,
            "state_map_history": self._state_map_history,
            "is_warmed_up": self._is_warmed_up,
            "bars_since_train": self._bars_since_train,
            "train_failures": self._train_failures,
            "protection_mode": self._protection_mode,
            "scaler_mean": self._scaler.mean_.tolist() if hasattr(self._scaler, 'mean_') else [],
            "scaler_scale": self._scaler.scale_.tolist() if hasattr(self._scaler, 'scale_') else [],
            "last_valid_output": self._last_valid_output,
        }

        if self._model is not None:
            state.update({
                "startprob_": self._model.startprob_.tolist(),
                "transmat_": self._model.transmat_.tolist(),
                "means_": self._model.means_.tolist(),
                "covars_": self._model.covars_.tolist(),
                "n_components": self._model.n_components,
            })
        return state

    def set_state(self, state: Dict) -> None:
        """从检查点恢复模型完整状态"""
        self.n_states = state.get("n_states", self.n_states)
        self._state_map = state.get("state_map", {})
        self._state_map_history = state.get("state_map_history", [])
        self._is_warmed_up = state.get("is_warmed_up", False)
        self._bars_since_train = state.get("bars_since_train", 0)
        self._train_failures = state.get("train_failures", 0)
        self._protection_mode = state.get("protection_mode", False)
        self._last_valid_output = state.get("last_valid_output", self._default_output())

        # 恢复标准化器
        mean = state.get("scaler_mean", [])
        scale = state.get("scaler_scale", [])
        if mean and scale and len(mean) == len(scale):
            self._scaler.mean_ = np.array(mean)
            self._scaler.scale_ = np.array(scale)
            self._scaler.var_ = self._scaler.scale_ ** 2
            self._scaler.n_features_in_ = len(mean)

        # 恢复模型参数
        if "startprob_" in state:
            try:
                n_comp = state["n_components"]
                self._model = hmm.GaussianHMM(
                    n_components=n_comp,
                    covariance_type="diag",
                    random_state=self.random_state,
                )
                self._model.startprob_ = np.array(state["startprob_"])
                self._model.transmat_ = np.array(state["transmat_"])
                self._model.means_ = np.array(state["means_"])
                self._model.covars_ = np.array(state["covars_"])
                self._current_prob = np.ones(n_comp) / n_comp
                logger.info("HMM model restored from checkpoint")
            except Exception as e:
                logger.error(f"Failed to restore HMM model: {e}")
                self._model = None
                self._is_warmed_up = False
