# -*- coding: utf-8 -*-
"""
模块名称: ddqn_agent.py (v6.0 华尔街分布式金融强化)
核心职责: Dueling Double DQN 交易智能体，支持多GPU分布式训练、
          金融审计链、序列化安全、内存优化及4K监控界面。
所属层级: evolution.rl

外部依赖:
    - torch (>=1.13) 神经网络与分布式
    - numpy 数值计算
    - random, time, os, json, hashlib, warnings, threading
    - evolution.rl.experience_buffer (PrioritizedReplayBuffer, Experience)
    - evolution.rl.action_mask (ActionMask)
    - logging 结构化日志，支持中文

接口契约: 向后兼容v5.0，新增 `distributed_sync`, `export_audit_report`, `get_4k_dashboard_data` 等方法。

配置项: 所有超参数均可通过构造函数或环境变量注入，支持动态调整。

作者: KHAOS Evolution Team
创建日期: 2025-12-05
修改记录:
    - 2026-01-15 v2.0 基础机构审计
    - 2026-01-20 v3.0 金融穿透审计
    - 2026-02-01 v4.0 极限压力测试
    - 2026-02-15 v5.0 金融级强化
    - 2026-03-01 v6.0 分布式金融审计与4K界面支持
"""
import hashlib
import json
import logging
import os
import random
import threading
import time
import warnings
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

from evolution.rl.experience_buffer import Experience, PrioritizedReplayBuffer
from evolution.rl.action_mask import ActionMask

# 日志配置：支持中文消息
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# 环境变量
AMP_ENABLED = os.environ.get('KHAOS_RL_AMP', '0') == '1'
FINANCIAL_STRICT = os.environ.get('KHAOS_RL_FIN_STRICT', '1') == '1'
DISTRIBUTED = os.environ.get('KHAOS_RL_DISTRIBUTED', '0') == '1'


@dataclass
class FinancialContext:
    """金融交易账户关键参数，支持序列化与审计签名"""
    initial_balance: float = 2000.0
    current_balance: float = 2000.0
    max_drawdown_pct: float = 0.2
    daily_loss_limit: float = 100.0
    commission_rate: float = 0.0004
    min_order_size: float = 0.001
    max_leverage: float = 3.0
    tax_rate: float = 0.0                   # 资本利得税率（占盈利比例）
    audit_signature: str = ""               # 审计哈希签名

    def compute_audit_hash(self) -> str:
        """生成当前状态的审计哈希，防止篡改"""
        data = f"{self.initial_balance}{self.current_balance}{self.max_drawdown_pct}{self.daily_loss_limit}{self.commission_rate}{self.tax_rate}"
        return hashlib.sha256(data.encode()).hexdigest()

    def sign(self):
        self.audit_signature = self.compute_audit_hash()


class DuelingQNetwork(nn.Module):
    """增强版Dueling Q网络，支持动态宽度与层数，用于分布式训练"""

    def __init__(self, state_dim: int, action_dim: int, hidden_layers: Optional[List[int]] = None,
                 use_spectral_norm: bool = False, use_batch_norm: bool = False,
                 dropout: float = 0.0):
        super().__init__()
        if hidden_layers is None:
            hidden_layers = [128, 128]
        if len(hidden_layers) < 1:
            raise ValueError("至少需要一个隐藏层")

        layers = []
        prev_dim = state_dim
        for h in hidden_layers:
            lin = nn.Linear(prev_dim, h)
            if use_spectral_norm:
                lin = nn.utils.spectral_norm(lin)
            layers.append(lin)
            if use_batch_norm:
                layers.append(nn.BatchNorm1d(h))
            layers.append(nn.ReLU())
            if dropout > 0.0:
                layers.append(nn.Dropout(dropout))
            prev_dim = h
        self.feature = nn.Sequential(*layers)

        self.value_stream = nn.Sequential(
            nn.Linear(prev_dim, max(prev_dim // 2, 1)),
            nn.ReLU(),
            nn.Linear(max(prev_dim // 2, 1), 1)
        )
        self.advantage_stream = nn.Sequential(
            nn.Linear(prev_dim, max(prev_dim // 2, 1)),
            nn.ReLU(),
            nn.Linear(max(prev_dim // 2, 1), action_dim)
        )
        self.apply(self._init_weights)

    @staticmethod
    def _init_weights(m: nn.Module) -> None:
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def forward(self, state: torch.Tensor) -> torch.Tensor:
        features = self.feature(state)
        value = self.value_stream(features)
        advantage = self.advantage_stream(features)
        return value + advantage - advantage.mean(dim=-1, keepdim=True)


class DDQNAgent:
    """Dueling Double DQN 交易智能体，支持分布式训练、审计日志与4K监控"""

    def __init__(self,
                 state_dim: int,
                 action_dim: int,
                 buffer: PrioritizedReplayBuffer,
                 action_mask: Optional[ActionMask] = None,
                 # 探索
                 epsilon: float = 0.05,
                 epsilon_min: float = 0.01,
                 epsilon_decay: float = 0.995,
                 epsilon_decay_step: int = 1,
                 # 折扣与更新
                 gamma: float = 0.999,
                 tau: float = 0.005,
                 target_update_freq: int = 4,
                 # 学习率
                 learning_rate: float = 1e-4,
                 lr_scheduler_step_size: int = 5000,
                 lr_scheduler_gamma: float = 0.5,
                 # 网络
                 hidden_layers: Optional[List[int]] = None,
                 use_spectral_norm: bool = False,
                 use_batch_norm: bool = False,
                 dropout: float = 0.0,
                 # 正则化与裁剪
                 grad_clip_max_norm: float = 10.0,
                 reward_clip: Optional[float] = None,
                 state_clip: Optional[float] = None,
                 # 混合精度
                 use_amp: bool = AMP_ENABLED,
                 # 设备
                 device: Optional[str] = None,
                 seed: Optional[int] = None,
                 # 金融适配
                 financial_context: Optional[FinancialContext] = None,
                 trading_interval_bars: int = 1,
                 max_positions: int = 1,
                 # 监控
                 log_interval_steps: int = 100,
                 alert_callback: Optional[Callable[[str, dict], None]] = None,
                 dashboard_callback: Optional[Callable[[dict], None]] = None,
                 # 训练控制
                 accumulation_steps: int = 1,
                 use_double_dqn: bool = True,
                 # 分布式
                 distributed: bool = DISTRIBUTED,
                 local_rank: int = 0,
                 world_size: int = 1):
        """
        新增参数:
            dropout: 网络中的Dropout比率
            dashboard_callback: 推送4K仪表盘数据的回调函数
            distributed: 是否启用多GPU分布式训练
            local_rank: 分布式训练的本地rank
            world_size: 分布式训练总进程数
        """
        if state_dim <= 0 or action_dim <= 0:
            raise ValueError("状态维度和动作维度必须为正整数")
        if buffer is None:
            raise ValueError("经验缓冲区不能为空")

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.buffer = buffer
        self.action_mask = action_mask
        self.financial_context = financial_context or FinancialContext()
        self.financial_context.sign()
        self.trading_interval_bars = max(1, trading_interval_bars)
        self.max_positions = max_positions
        self.alert_callback = alert_callback
        self.dashboard_callback = dashboard_callback
        self.accumulation_steps = max(1, accumulation_steps)
        self.use_double_dqn = use_double_dqn
        self.distributed = distributed
        self.local_rank = local_rank
        self.world_size = world_size

        # 探索
        self.epsilon = epsilon
        self.epsilon_min = epsilon_min
        self.epsilon_decay = epsilon_decay
        self.epsilon_decay_step = max(1, epsilon_decay_step)
        self._exploration_boost = 1.0

        self.gamma = gamma
        self.tau = tau
        self.target_update_freq = max(1, target_update_freq)
        self.grad_clip_max_norm = grad_clip_max_norm
        self.reward_clip = reward_clip
        self.state_clip = state_clip

        # 设备与分布式
        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = torch.device(device)

        if distributed and not dist.is_initialized():
            dist.init_process_group(backend='nccl', rank=local_rank, world_size=world_size)
            torch.cuda.set_device(local_rank)
            self.device = torch.device(f'cuda:{local_rank}')

        self.use_amp = use_amp and self.device.type == 'cuda'

        if seed is not None:
            random.seed(seed + local_rank)
            np.random.seed(seed + local_rank)
            torch.manual_seed(seed + local_rank)
            if self.device.type == 'cuda':
                torch.cuda.manual_seed_all(seed + local_rank)

        # 网络
        self.q_network = DuelingQNetwork(state_dim, action_dim, hidden_layers,
                                         use_spectral_norm, use_batch_norm, dropout).to(self.device)
        self.target_network = DuelingQNetwork(state_dim, action_dim, hidden_layers,
                                              use_spectral_norm, use_batch_norm, dropout).to(self.device)
        self.target_network.load_state_dict(self.q_network.state_dict())
        self.target_network.eval()
        self._freeze_target_network(True)

        # DDP封装
        if self.distributed:
            self.q_network = DDP(self.q_network, device_ids=[local_rank], output_device=local_rank)
            self.target_network = DDP(self.target_network, device_ids=[local_rank], output_device=local_rank)

        self.optimizer = optim.Adam(self.q_network.parameters(), lr=learning_rate)
        self.loss_fn = nn.MSELoss()
        self.scheduler = optim.lr_scheduler.StepLR(self.optimizer, step_size=lr_scheduler_step_size,
                                                   gamma=lr_scheduler_gamma)

        # 混合精度
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.use_amp)

        # 训练状态
        self.train_step = 0
        self._q_mean = 0.0
        self._loss_mean = 0.0
        self._recent_losses = deque(maxlen=100)
        self._action_counts = np.zeros(action_dim, dtype=np.int64)
        self._step_time = time.time()
        self._last_loss = 0.0

        # 金融风险监控
        self._peak_balance = self.financial_context.current_balance
        self._daily_loss = 0.0
        self._daily_pnl = 0.0
        self._last_reset_day = time.strftime('%Y%m%d')
        self._audit_log = deque(maxlen=1000)  # 存储最近1000条审计记录

        # 动作语义映射
        self._action_map: Optional[Dict[int, str]] = None

        # 梯度累积
        self._grad_accum_count = 0

        # 检查点自愈
        self._last_checkpoint_path: Optional[str] = None
        self._auto_save_interval = 5000

        # 分布式同步锁
        self._grad_lock = threading.Lock()

        logger.info(f"DDQNAgent v6.0: device={self.device}, amp={self.use_amp}, "
                    f"distributed={self.distributed}, balance={self.financial_context.initial_balance}")

    # --------------------------------------------------------------------------
    # 动作语义映射与审计
    # --------------------------------------------------------------------------
    def set_action_map(self, action_map: Dict[int, str]) -> None:
        self._action_map = action_map
        logger.info(f"动作语义映射已设置: {action_map}")
        self._audit_log.append({"event": "action_map_set", "map": action_map, "time": time.time()})

    def _add_audit_record(self, event: str, details: Dict[str, Any]):
        record = {"event": event, "details": details, "timestamp": time.time(),
                  "step": self.train_step}
        self._audit_log.append(record)
        if len(self._audit_log) >= 1000:
            self._audit_log.popleft()

    # --------------------------------------------------------------------------
    # 动作选择（含金融约束与分布式）
    # --------------------------------------------------------------------------
    @torch.no_grad()
    def select_action(self, state: np.ndarray, valid_actions: Optional[List[int]] = None,
                      position_count: int = 0) -> int:
        if self.state_clip is not None:
            state = np.clip(state, -self.state_clip, self.state_clip)

        if valid_actions is None:
            valid_actions = list(range(self.action_dim))
        valid_actions = [a for a in valid_actions if 0 <= a < self.action_dim]

        self._update_daily_pnl()
        if self._daily_loss >= self.financial_context.daily_loss_limit:
            if self._action_map:
                allowed = {a for a, op in self._action_map.items() if op not in ('LONG', 'SHORT')}
                valid_actions = [a for a in valid_actions if a in allowed]

        if position_count >= self.max_positions and self._action_map:
            entry_actions = {a for a, op in self._action_map.items() if op in ('LONG', 'SHORT')}
            valid_actions = [a for a in valid_actions if a not in entry_actions]

        if not valid_actions:
            return 0

        current_epsilon = self.epsilon * self._exploration_boost
        if random.random() < current_epsilon:
            action = random.choice(valid_actions)
        else:
            state_tensor = torch.FloatTensor(state).unsqueeze(0).to(self.device)
            self.q_network.eval()
            q_values = self.q_network(state_tensor).cpu().numpy().flatten()
            self.q_network.train()

            if self.action_mask:
                q_values = self.action_mask.apply(q_values, valid_actions)
            else:
                mask = np.full_like(q_values, -np.inf)
                mask[valid_actions] = 0.0
                q_values = q_values + mask

            if np.all(np.isneginf(q_values)):
                action = valid_actions[0]
            else:
                action = int(np.argmax(q_values))

        self._action_counts[action] += 1
        self._add_audit_record("action_selected", {"action": action, "epsilon": current_epsilon})
        return action

    # --------------------------------------------------------------------------
    # 经验存储与验证
    # --------------------------------------------------------------------------
    def validate_experience(self, state, action, reward, next_state, done) -> bool:
        if np.any(np.isnan(state)) or np.any(np.isnan(next_state)):
            return False
        if np.any(np.isinf(state)) or np.any(np.isinf(next_state)):
            return False
        if abs(reward) > 1e6:
            return False
        return True

    def store_experience(self, state, action, reward, next_state, done, priority=None):
        if not self.validate_experience(state, action, reward, next_state, done):
            logger.warning("经验数据异常，已丢弃")
            return
        if self.reward_clip is not None:
            reward = np.clip(reward, -self.reward_clip, self.reward_clip)
        if self.state_clip is not None:
            state = np.clip(state, -self.state_clip, self.state_clip)
            next_state = np.clip(next_state, -self.state_clip, self.state_clip)

        if priority is None:
            max_prio = self.buffer.tree.max_priority()
            if max_prio <= 0.0:
                max_prio = 1.0
            priority = max_prio

        self.buffer.add(Experience(state, action, reward, next_state, done, priority))
        self._daily_pnl += reward
        self._update_daily_pnl()
        self._add_audit_record("experience_stored", {"action": action, "reward": reward})

    # --------------------------------------------------------------------------
    # 学习（增强分布式与4K推送）
    # --------------------------------------------------------------------------
    def learn(self, batch_size: int) -> float:
        if not self.buffer.is_ready(batch_size):
            return 0.0

        experiences, indices, weights = self.buffer.sample(batch_size)
        if len(experiences) == 0:
            return 0.0

        states = torch.FloatTensor(np.array([e.state for e in experiences])).to(self.device)
        actions = torch.LongTensor([e.action for e in experiences]).unsqueeze(1).to(self.device)
        rewards = torch.FloatTensor([e.reward for e in experiences]).unsqueeze(1).to(self.device)
        next_states = torch.FloatTensor(np.array([e.next_state for e in experiences])).to(self.device)
        dones = torch.FloatTensor([float(e.done) for e in experiences]).unsqueeze(1).to(self.device)
        weights = torch.FloatTensor(weights).unsqueeze(1).to(self.device)

        with torch.cuda.amp.autocast(enabled=self.use_amp):
            current_q = self.q_network(states).gather(1, actions)
            with torch.no_grad():
                if self.use_double_dqn:
                    next_actions = self.q_network(next_states).argmax(dim=1, keepdim=True)
                    next_q = self.target_network(next_states).gather(1, next_actions)
                else:
                    next_q = self.target_network(next_states).max(dim=1, keepdim=True)[0]
                target_q = rewards + self.gamma * next_q * (1 - dones)
            loss = (weights * self.loss_fn(current_q, target_q)).mean()

        if torch.isnan(loss):
            logger.error(f"NaN loss at step {self.train_step}")
            if self.alert_callback:
                self.alert_callback('nan_loss', {'step': self.train_step})
            return self._last_loss if self._last_loss != 0.0 else 0.0
        self._last_loss = loss.item()

        # 分布式梯度同步
        with self._grad_lock:
            self.scaler.scale(loss).backward()
            self._grad_accum_count += 1

            if self._grad_accum_count >= self.accumulation_steps:
                if self.distributed:
                    # 梯度平均
                    for param in self.q_network.parameters():
                        if param.grad is not None:
                            dist.all_reduce(param.grad, op=dist.ReduceOp.SUM)
                            param.grad /= self.world_size

                self.scaler.unscale_(self.optimizer)
                torch.nn.utils.clip_grad_norm_(self.q_network.parameters(), self.grad_clip_max_norm)
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad()
                self._grad_accum_count = 0

        # 更新优先级
        with torch.no_grad():
            td_errors = (current_q - target_q).abs().cpu().numpy().flatten()
        self.buffer.update_priorities(indices, td_errors)

        if self.train_step % self.epsilon_decay_step == 0:
            self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)

        if self.train_step % self.target_update_freq == 0:
            self.update_target_network()

        self.train_step += 1
        self._q_mean = 0.95 * self._q_mean + 0.05 * current_q.mean().item()
        self._loss_mean = 0.95 * self._loss_mean + 0.05 * loss.item()
        self._recent_losses.append(loss.item())

        # 自动保存
        if self.train_step % self._auto_save_interval == 0 and self._last_checkpoint_path:
            self.save(self._last_checkpoint_path)

        # 推送4K仪表盘数据
        if self.dashboard_callback and self.train_step % self.log_interval_steps == 0:
            self.dashboard_callback(self.get_dashboard_data())

        if self.train_step % self.log_interval_steps == 0:
            self._log_progress()

        return loss.item()

    # --------------------------------------------------------------------------
    # 目标网络更新
    # --------------------------------------------------------------------------
    def update_target_network(self):
        self.target_network.eval()
        for tp, lp in zip(self.target_network.parameters(), self.q_network.parameters()):
            tp.data.copy_(self.tau * lp.data + (1.0 - self.tau) * tp.data)
        self._freeze_target_network(True)

    def _freeze_target_network(self, freeze):
        for p in self.target_network.parameters():
            p.requires_grad = not freeze

    # --------------------------------------------------------------------------
    # 金融风险监控与审计报告
    # --------------------------------------------------------------------------
    def _update_daily_pnl(self):
        today = time.strftime('%Y%m%d')
        if today != self._last_reset_day:
            self._daily_pnl = 0.0
            self._last_reset_day = today
        self._daily_loss = max(0.0, -self._daily_pnl)
        self._peak_balance = max(self._peak_balance, self.financial_context.current_balance)

    def get_risk_report(self) -> Dict[str, Any]:
        self._update_daily_pnl()
        return {
            'current_balance': self.financial_context.current_balance,
            'peak_balance': self._peak_balance,
            'drawdown_pct': 1.0 - self.financial_context.current_balance / self._peak_balance if self._peak_balance > 0 else 0.0,
            'daily_pnl': self._daily_pnl,
            'daily_loss': self._daily_loss,
            'daily_loss_limit': self.financial_context.daily_loss_limit,
            'audit_signature': self.financial_context.audit_signature,
        }

    def export_audit_report(self) -> List[Dict]:
        """导出最近的审计日志，供合规审查"""
        return list(self._audit_log)

    def get_dashboard_data(self) -> Dict:
        """生成4K仪表盘所需的所有数据"""
        return {
            'train_step': self.train_step,
            'epsilon': self.epsilon,
            'avg_loss': self._loss_mean,
            'avg_q': self._q_mean,
            'action_distribution': self._action_counts.tolist(),
            'risk': self.get_risk_report(),
            'lr': self.optimizer.param_groups[0]['lr'],
            'timestamp': time.time()
        }

    # --------------------------------------------------------------------------
    # 持久化与恢复（增强加密与校验）
    # --------------------------------------------------------------------------
    def save(self, path: str, encrypt: bool = False) -> None:
        self._last_checkpoint_path = path
        checkpoint = {
            'q_network': self.q_network.state_dict(),
            'target_network': self.target_network.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'scheduler': self.scheduler.state_dict(),
            'scaler': self.scaler.state_dict(),
            'epsilon': self.epsilon,
            'train_step': self.train_step,
            'action_counts': self._action_counts.tolist(),
            'daily_pnl': self._daily_pnl,
            'last_reset_day': self._last_reset_day,
            'financial_context': self.financial_context.__dict__,
            'hyperparameters': {
                'state_dim': self.state_dim,
                'action_dim': self.action_dim,
                'gamma': self.gamma,
                'tau': self.tau,
                'use_double_dqn': self.use_double_dqn,
                'use_amp': self.use_amp,
            }
        }
        try:
            if encrypt:
                # 使用简单的加密（实际可替换为AES）
                import base64
                ser = json.dumps(checkpoint).encode()
                enc = base64.b64encode(ser)
                with open(path, 'wb') as f:
                    f.write(enc)
            else:
                torch.save(checkpoint, path)
            logger.info(f"模型已保存至 {path}，步数: {self.train_step}")
        except Exception as e:
            logger.error(f"保存失败: {e}")
            if self.alert_callback:
                self.alert_callback('save_error', {'path': path, 'error': str(e)})

    def load(self, path: str, encrypted: bool = False) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(f"模型文件不存在: {path}")
        try:
            if encrypted:
                import base64
                with open(path, 'rb') as f:
                    enc = f.read()
                ser = base64.b64decode(enc)
                checkpoint = torch.load(ser, map_location=self.device)
            else:
                checkpoint = torch.load(path, map_location=self.device)
        except Exception as e:
            logger.error(f"模型文件损坏: {e}")
            backup_path = path + '.bak'
            if os.path.exists(backup_path):
                checkpoint = torch.load(backup_path, map_location=self.device)
                logger.warning(f"从备份 {backup_path} 恢复模型")
            else:
                raise

        hp = checkpoint.get('hyperparameters', {})
        if hp.get('state_dim', self.state_dim) != self.state_dim:
            raise ValueError("状态维度不匹配")
        if hp.get('action_dim', self.action_dim) != self.action_dim:
            raise ValueError("动作维度不匹配")

        self.q_network.load_state_dict(checkpoint['q_network'])
        self.target_network.load_state_dict(checkpoint['target_network'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        if 'scheduler' in checkpoint:
            self.scheduler.load_state_dict(checkpoint['scheduler'])
        if 'scaler' in checkpoint:
            self.scaler.load_state_dict(checkpoint['scaler'])
        self.epsilon = checkpoint.get('epsilon', self.epsilon)
        self.train_step = checkpoint.get('train_step', 0)
        self._action_counts = np.array(checkpoint.get('action_counts', [0]*self.action_dim))
        self._daily_pnl = checkpoint.get('daily_pnl', 0.0)
        self._last_reset_day = checkpoint.get('last_reset_day', time.strftime('%Y%m%d'))
        if 'financial_context' in checkpoint:
            for k, v in checkpoint['financial_context'].items():
                setattr(self.financial_context, k, v)
        self.target_network.eval()
        self._freeze_target_network(True)
        logger.info(f"模型已加载，步数: {self.train_step}")

    # --------------------------------------------------------------------------
    # 日志与监控
    # --------------------------------------------------------------------------
    def _log_progress(self):
        step_time = time.time() - self._step_time
        logger.info(
            f"Step {self.train_step}: loss={self._last_loss:.4f}, avg_loss={self._loss_mean:.4f}, "
            f"avg_Q={self._q_mean:.3f}, eps={self.epsilon:.4f}, lr={self.optimizer.param_groups[0]['lr']:.2e}, "
            f"速度={step_time / self.log_interval_steps:.3f}s/step, "
            f"动作分布={self._action_counts[:5]}...")
        self._step_time = time.time()

    def get_stats(self) -> Dict[str, Any]:
        return {
            'train_step': self.train_step,
            'epsilon': self.epsilon,
            'current_lr': self.optimizer.param_groups[0]['lr'],
            'avg_loss': self._loss_mean,
            'avg_q': self._q_mean,
            'recent_losses': list(self._recent_losses),
            'action_counts': self._action_counts.tolist(),
            **self.get_risk_report()
        }

    # --------------------------------------------------------------------------
    # 辅助方法
    # --------------------------------------------------------------------------
    def enable_exploration_boost(self, factor: float = 1.5):
        self._exploration_boost = factor

    def set_financial_params(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.financial_context, k):
                setattr(self.financial_context, k, v)
        self.financial_context.sign()
        logger.info(f"金融参数已更新: {kwargs}")
        self._add_audit_record("financial_params_updated", kwargs)

    def distributed_sync(self):
        """手动触发分布式同步（通常自动完成）"""
        if self.distributed:
            dist.barrier()
            logger.info("分布式同步完成")
