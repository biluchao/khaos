# -*- coding: utf-8 -*-
"""
模块名称: env.py (v5.0 华尔街极境版)
核心职责: 定义 KHAOS 强化学习交易环境，完全符合 Gymnasium 0.26+ 规范，具备金融级安全、
          中文日志、动作掩码、状态导入/导出、参数严格验证、非法动作惩罚可配、支持 ansi 渲染。
所属层级: evolution.rl

外部依赖:
    - numpy
    - gymnasium (若不可用则回退到内建基类及离散动作空间)
    - typing
    - logging
    - core.engine.strategy_engine.StrategyEngine

接口契约:
    提供: {
        'KhaosTradingEnv': {
            'reset(seed, options) -> (obs, info)',
            'step(action) -> (obs, reward, terminated, truncated, info)',
            'action_masks() -> ndarray',
            'seed(seed) -> List[int]',
            'render() -> Optional[str]',
            'close() -> None',
            'get_state() -> dict',
            'set_state(state) -> None'
        }
    }

配置项:
    - rl.state_dim, max_steps, initial_balance, min_balance, reward_clip,
      state_clip_range, position_penalty, max_position, low_balance_ratio,
      enable_chinese_log, allow_long, allow_short, illegal_penalty

作者: KHAOS Evolution Team
创建日期: 2025-12-01
最后修改: 2026-07-16 (第四轮100项缺陷修复)
版权: Copyright © 2025-2026 KHAOS Engineering. All rights reserved.
"""

import logging
from typing import Optional, Tuple, Dict, List, Any

import numpy as np

# 尝试导入 gymnasium，若不可用则使用内建兼容基类及离散动作空间
try:
    import gymnasium as gym
    DiscreteAction = gym.spaces.Discrete
    EnvBase = gym.Env
except ImportError:
    class DiscreteAction:
        """内建离散动作空间，兼容 gymnasium 接口"""
        def __init__(self, n: int):
            self.n = n
    class EnvBase:
        """简易环境基类，兼容 gymnasium 核心接口"""
        def __init__(self):
            self.action_space = DiscreteAction(8)
            self.observation_space = None
            self.metadata = {'render_modes': []}
            self.np_random = np.random.RandomState()
            self.reward_range = (-float('inf'), float('inf'))
            self._terminated = False
            self._truncated = False
            self.max_episode_steps = None
        def reset(self, seed=None, options=None):
            if seed is not None:
                self.np_random.seed(seed)
            raise NotImplementedError
        def step(self, action):
            raise NotImplementedError
        def render(self):
            pass
        def close(self):
            pass
        def seed(self, seed=None):
            if seed is not None:
                self.np_random.seed(seed)
            return [seed] if seed is not None else []

from core.engine.strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class Action:
    """交易动作枚举"""
    HOLD = 0
    LONG_ENTRY = 1
    SHORT_ENTRY = 2
    INCREASE_LONG = 3
    INCREASE_SHORT = 4
    DECREASE_LONG = 5
    DECREASE_SHORT = 6
    CLOSE_ALL = 7

    ACTION_DIM = 8
    NAMES = ["HOLD", "LONG_ENTRY", "SHORT_ENTRY",
             "INCREASE_LONG", "INCREASE_SHORT",
             "DECREASE_LONG", "DECREASE_SHORT", "CLOSE_ALL"]
    NAME_MAP = dict(enumerate(NAMES))


class KhaosTradingEnv(EnvBase):
    """
    KHAOS 强化学习交易环境 v5.0 (极境版)
    完全符合 Gymnasium 0.26+，适用于2000美金至万亿美金账户。
    """
    metadata = {'render_modes': ['human', 'ansi']}

    def __init__(self,
                 strategy_engine: StrategyEngine,
                 state_dim: Optional[int] = None,
                 max_steps: int = 10000,
                 initial_balance: float = 2000.0,
                 min_balance: float = 1.0,
                 reward_clip: float = 100.0,
                 state_clip_range: float = 50.0,
                 position_penalty: float = 0.0,
                 max_position: float = 1.0,
                 low_balance_ratio: float = 0.1,
                 enable_chinese_log: bool = True,
                 allow_long: bool = True,
                 allow_short: bool = True,
                 illegal_penalty: float = -0.1,
                 render_mode: str = 'human'):
        super().__init__()
        # 参数显式校验（避免 assert 被优化）
        if max_steps <= 0:
            raise ValueError("max_steps must be > 0")
        if initial_balance <= 0:
            raise ValueError("initial_balance must be > 0")
        if min_balance >= initial_balance:
            raise ValueError("min_balance must be < initial_balance")
        if reward_clip <= 0:
            raise ValueError("reward_clip must be > 0")
        if state_clip_range <= 0:
            raise ValueError("state_clip_range must be > 0")
        if position_penalty < 0 or position_penalty > 1:
            raise ValueError("position_penalty must be in [0, 1]")
        if low_balance_ratio <= 0 or low_balance_ratio > 1:
            raise ValueError("low_balance_ratio must be in (0, 1]")
        if not allow_long and not allow_short:
            logger.warning("allow_long 和 allow_short 均为 False，环境只能 HOLD")

        self.engine = strategy_engine
        self._state_dim = state_dim
        self.max_episode_steps = max_steps
        self.max_steps = max_steps
        self.initial_balance = initial_balance
        self.min_balance = min_balance
        self.reward_clip = abs(reward_clip)
        self.state_clip_range = abs(state_clip_range)
        self.position_penalty = position_penalty
        self.max_position = max_position if max_position > 0 else np.inf
        self.low_balance_ratio = low_balance_ratio
        self.zh_log = enable_chinese_log
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.illegal_penalty = illegal_penalty
        self.render_mode = render_mode
        if self.max_position is np.inf:
            logger.info("max_position 已设为无限制")

        # 动作空间
        self.action_space = DiscreteAction(Action.ACTION_DIM)
        self.reward_range = (-self.reward_clip, self.reward_clip)

        # 观测空间（临时，reset后精确确定）
        temp_dim = state_dim if state_dim else 51
        self.observation_space = gym.spaces.Box(
            low=-np.inf, high=np.inf, shape=(temp_dim,), dtype=np.float32
        ) if 'gym' in globals() else None

        # 内部状态
        self.current_step = 0
        self.balance = initial_balance
        self.position = 0.0
        self.entry_price = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.last_action: Optional[int] = None
        self._terminated = False
        self._truncated = False
        self._episode_reset = False
        self.episode_id = 0
        self._illegal_action_count = 0

        if not hasattr(self.engine, 'execute_action'):
            raise AttributeError("引擎必须提供 execute_action 方法")
        if not hasattr(self.engine, 'get_observation'):
            raise AttributeError("引擎必须提供 get_observation 方法")

        logger.info("KHAOS RL 环境 v5.0 初始化完成 (资金: %.2f, 最大步数: %d)", initial_balance, max_steps)

    def reset(self, seed: int = None, options: dict = None) -> Tuple[np.ndarray, dict]:
        if self.engine is None:
            raise RuntimeError("引擎已关闭，无法重置环境")
        if seed is not None:
            self.np_random.seed(seed)
            if hasattr(self.engine, 'seed'):
                try:
                    self.engine.seed(seed)
                except Exception:
                    pass

        self.current_step = 0
        self.balance = self.initial_balance
        self.position = 0.0
        self.entry_price = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.last_action = None
        self._terminated = False
        self._truncated = False
        self._episode_reset = True
        self.episode_id += 1
        self._illegal_action_count = 0

        for attempt in range(3):
            try:
                self.engine.reset()
                break
            except Exception as e:
                logger.warning("引擎重置失败 (尝试 %d/3): %s", attempt+1, e)
                if attempt == 2:
                    raise RuntimeError("引擎重置连续失败") from e

        obs = self._get_observation()
        if obs is None:
            obs = np.zeros(self._state_dim or 51, dtype=np.float32)

        if self._state_dim is None:
            self._state_dim = len(obs)
        elif len(obs) != self._state_dim:
            logger.warning("引擎输出维度 %d 与配置维度 %d 不符，采用实际维度", len(obs), self._state_dim)
            self._state_dim = len(obs)

        if 'gym' in globals() and self.observation_space is not None:
            self.observation_space = gym.spaces.Box(
                low=-np.inf, high=np.inf, shape=(self._state_dim,), dtype=np.float32
            )

        obs = self._pad_or_truncate(obs, self._state_dim)
        info = {'balance': self.balance, 'position': self.position, 'episode_id': self.episode_id}
        if self.zh_log:
            logger.info("环境已重置，episode %d，初始资金 %.2f", self.episode_id, self.balance)
        return obs, info

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, bool, dict]:
        if self.engine is None:
            raise RuntimeError("引擎已关闭")
        if not self._episode_reset:
            logger.warning("step() called before reset()")
        self._episode_reset = False
        if self._terminated or self._truncated:
            logger.warning("环境已终止，step 无效")
            return self._get_observation(), 0.0, self._terminated, self._truncated, {'info': 'already done'}

        self.current_step += 1
        self.last_action = action

        # 1. 动作合法性检查
        masks = self.action_masks()
        illegal = not masks[action]
        info: Dict[str, Any] = {'illegal_action_count': self._illegal_action_count}

        if illegal:
            self._illegal_action_count += 1
            penalty = self.illegal_penalty - 0.02 * min(self._illegal_action_count, 10)
            reward = penalty
            # 余额更新（先计算新余额再判断终止）
            new_balance = self.balance + reward
            terminated = new_balance < self.min_balance
            truncated = self.current_step >= self.max_steps
            self.balance = max(new_balance, 0.0)
            self._terminated = terminated
            self._truncated = truncated
            info.update({
                'balance': self.balance,
                'position': self.position,
                'realized_pnl': self.realized_pnl,
                'unrealized_pnl': self.unrealized_pnl,
                'total_pnl': self.realized_pnl + self.unrealized_pnl,
                'invalid_action': True,
                'terminated': terminated,
                'truncated': truncated,
                'available_funds_ratio': self.balance / self.initial_balance if self.initial_balance > 0 else 0.0,
                'low_balance_warning': self.balance < self.initial_balance * self.low_balance_ratio,
                'balance_depleted': terminated,
                'termination_reason': 'balance_depleted' if terminated else 'none',
                'episode_id': self.episode_id,
            })
            return self._get_observation(), reward, terminated, truncated, info

        # 合法动作：重置非法计数器
        self._illegal_action_count = 0

        # 2. 执行合法动作
        try:
            result = self.engine.execute_action(action)
            if not isinstance(result, (tuple, list)) or len(result) != 4:
                raise ValueError(f"引擎返回格式错误: {result}")
            reward, engine_done, next_state, engine_info = result
        except Exception as e:
            logger.error("执行动作 %s 失败: %s", Action.NAME_MAP.get(action, str(action)), e)
            reward = -self.reward_clip
            engine_done = True
            next_state = self._get_observation()
            engine_info = {'error': str(e)}

        # 确保 reward 为 float
        reward = float(reward) if reward is not None else 0.0

        # 清理 engine_info
        if not isinstance(engine_info, dict):
            engine_info = {'raw_info': str(engine_info)}
        for k, v in list(engine_info.items()):
            if not isinstance(v, (int, float, str, bool, np.integer, np.floating, np.bool_)):
                engine_info[k] = str(v)

        # 提取引擎返回的盈亏增量（应返回单步变化量）
        self.position = float(engine_info.get('position', self.position))
        realized_delta = float(engine_info.get('realized_pnl_delta', engine_info.get('realized_pnl', 0.0)))
        unrealized = float(engine_info.get('unrealized_pnl', 0.0))
        self.realized_pnl += realized_delta
        self.unrealized_pnl = unrealized

        # 持仓惩罚
        if self.position != 0 and self.position_penalty > 0 and self.max_position > 0:
            reward -= self.position_penalty * (abs(self.position) / self.max_position)

        # 裁剪
        reward = np.clip(reward, -self.reward_clip, self.reward_clip)

        # 更新余额
        new_balance = self.balance + reward
        balance_depleted = new_balance < self.min_balance
        terminated = engine_done or balance_depleted
        truncated = self.current_step >= self.max_steps
        self.balance = max(new_balance, 0.0)
        self._terminated = terminated
        self._truncated = truncated

        # 后处理观测
        if next_state is None or not isinstance(next_state, (np.ndarray, list)):
            logger.warning("next_state 无效，使用当前观测")
            next_state = self._get_observation()
        next_state = np.array(next_state, dtype=np.float32).flatten()
        if len(next_state) == 0:
            next_state = np.zeros(self._state_dim or 1, dtype=np.float32)
        next_state = self._pad_or_truncate(next_state, self._state_dim)
        next_state = np.nan_to_num(next_state, nan=0.0, posinf=1e4, neginf=-1e4)
        next_state = np.clip(next_state, -self.state_clip_range, self.state_clip_range)

        # 组装 info
        info.update({
            'balance': self.balance,
            'position': self.position,
            'realized_pnl': float(self.realized_pnl),
            'unrealized_pnl': float(self.unrealized_pnl),
            'total_pnl': float(self.realized_pnl + self.unrealized_pnl),
            'invalid_action': False,
            'terminated': terminated,
            'truncated': truncated,
            'available_funds_ratio': self.balance / self.initial_balance if self.initial_balance > 0 else 0.0,
            'low_balance_warning': self.balance < self.initial_balance * self.low_balance_ratio,
            'balance_depleted': balance_depleted,
            'termination_reason': 'balance_depleted' if balance_depleted else 'engine_done' if engine_done else 'truncated' if truncated else 'none',
            'episode_id': self.episode_id,
            'illegal_action_count': self._illegal_action_count,
        })
        info.update({k: v for k, v in engine_info.items() if k not in info})

        if balance_depleted and self.zh_log:
            logger.warning("余额耗尽 (%.2f)，环境终止", self.balance)

        return next_state, float(reward), terminated, truncated, info

    def action_masks(self) -> np.ndarray:
        mask = np.zeros(Action.ACTION_DIM, dtype=bool)
        mask[Action.HOLD] = True

        is_zero = np.isclose(self.position, 0.0)
        is_long = self.position > 0 and not is_zero
        is_short = self.position < 0 and not is_zero

        # 允许开仓条件：只要余额大于0即可尝试，由引擎最终决定
        can_open = self.balance > 0
        position_at_max = abs(self.position) >= self.max_position

        if is_zero:
            if can_open:
                if self.allow_long:
                    mask[Action.LONG_ENTRY] = True
                if self.allow_short:
                    mask[Action.SHORT_ENTRY] = True
        else:
            if is_long:
                if can_open and not position_at_max:
                    mask[Action.INCREASE_LONG] = True
                mask[Action.DECREASE_LONG] = True
                mask[Action.CLOSE_ALL] = True
            if is_short:
                if can_open and not position_at_max:
                    mask[Action.INCREASE_SHORT] = True
                mask[Action.DECREASE_SHORT] = True
                mask[Action.CLOSE_ALL] = True

        return mask

    def seed(self, seed: int = None) -> List[int]:
        if seed is not None:
            self.np_random.seed(seed)
            if self.engine is not None and hasattr(self.engine, 'seed'):
                try:
                    self.engine.seed(seed)
                except Exception:
                    pass
            return [seed]
        return []

    def render(self) -> Optional[str]:
        if self.render_mode == 'ansi':
            return (f"Step {self.current_step}/{self.max_steps} | "
                    f"Balance: {self.balance:.2f} | Position: {self.position:.6f} | "
                    f"Realized PnL: {self.realized_pnl:.2f}")
        if self.render_mode == 'human':
            print(self.render() if hasattr(self, 'render') else "", flush=True)
        return None

    def close(self) -> None:
        if self.engine is not None:
            try:
                if hasattr(self.engine, 'shutdown'):
                    self.engine.shutdown()
            except Exception as e:
                logger.error("关闭引擎时异常: %s", e)
            finally:
                self.engine = None
        logger.info("RL 环境已关闭")

    def get_state(self) -> Dict[str, Any]:
        return {
            'current_step': self.current_step,
            'balance': self.balance,
            'position': self.position,
            'entry_price': self.entry_price,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
            'last_action': self.last_action,
            'terminated': self._terminated,
            'truncated': self._truncated,
            'episode_id': self.episode_id,
            'initial_balance': self.initial_balance,
            'illegal_action_count': self._illegal_action_count,
            'state_dim': self._state_dim,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        self.current_step = state.get('current_step', 0)
        self.balance = state.get('balance', self.initial_balance)
        self.position = state.get('position', 0.0)
        self.entry_price = state.get('entry_price', 0.0)
        self.unrealized_pnl = state.get('unrealized_pnl', 0.0)
        self.realized_pnl = state.get('realized_pnl', 0.0)
        self.last_action = state.get('last_action', None)
        self._terminated = state.get('terminated', False)
        self._truncated = state.get('truncated', False)
        self.episode_id = state.get('episode_id', self.episode_id)
        self._illegal_action_count = state.get('illegal_action_count', 0)
        self._state_dim = state.get('state_dim', self._state_dim)
        logger.warning("环境状态已手动恢复，请确保引擎状态同步")

    def _get_observation(self) -> np.ndarray:
        try:
            if self.engine is not None:
                obs = self.engine.get_observation()
                if obs is not None:
                    return np.array(obs, dtype=np.float32).flatten()
        except Exception as e:
            logger.warning("获取观测失败: %s", e)
        return np.zeros(self._state_dim or 51, dtype=np.float32)

    def _pad_or_truncate(self, arr: np.ndarray, target_len: int) -> np.ndarray:
        if target_len <= 0:
            return np.zeros(1, dtype=np.float32)
        if arr.ndim != 1:
            raise ValueError(f"观测数组必须为一维，实际维度: {arr.ndim}")
        if len(arr) > target_len:
            return arr[:target_len]
        elif len(arr) < target_len:
            return np.pad(arr, (0, target_len - len(arr)), mode='edge')
        return arr
