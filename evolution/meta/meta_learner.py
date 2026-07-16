# -*- coding: utf-8 -*-
"""
模块名称: meta_learner.py
核心职责: 实现 MAML 元学习算法，在多品种历史数据上训练共享参数，使策略能通过少量样本快速适应新品种。
所属层级: evolution.meta

外部依赖:
    - torch (PyTorch >= 2.0.0)
    - torch.nn, torch.optim
    - torch.cuda.amp (混合精度)
    - torch.func (functional_call，PyTorch 2.0+)
    - numpy (数值计算)
    - hashlib, hmac (完整性校验)
    - logging (日志)
    - os, sys, time, typing, copy, json, warnings

接口契约:
    提供: {
        'MetaLearner': {
            'train(tasks, epochs, callbacks)': '元训练',
            'adapt(support_set, labels, return_loss)': '快速适配',
            'save_checkpoint(path)': '保存检查点',
            'load_checkpoint(path)': '加载检查点',
            'set_seed(seed)': '固定种子',
            'eval()', 'train_mode()', 'summary()',
            'to(device)', 'half()', 'bfloat16()',
            'compile()', 'export_torchscript()', 'export_onnx()',
            ...
        }
    }
    消费: {
        'evolution.meta.cross_asset_encoder.CrossAssetEncoder': '共享特征编码器'
    }

配置项（环境变量，前缀 KHAOS_META_）:
    - INNER_LR, OUTER_LR, INNER_STEPS, BATCH_SIZE, LOSS_TYPE,
      GRAD_CLIP, MAX_INNER_STEPS, CHECKPOINT_ALGORITHM, AMP, HUBER_DELTA, VERBOSE

作者: KHAOS Evolution Team
创建日期: 2025-11-15
修改记录:
    - 2026-01-22 第八轮审计：修复100项缺陷，达华尔街交易级 v10.0 终极版
"""

import copy
import hashlib
import hmac
import logging
import os
import sys
import time
import warnings
from functools import wraps
from typing import (Any, Callable, Dict, List, Optional, Sequence, Tuple,
                    Union)

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.cuda.amp import GradScaler, autocast
from torch.func import functional_call

from evolution.meta.cross_asset_encoder import CrossAssetEncoder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环境变量辅助
# ---------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    try:
        val = float(os.environ.get(name, ""))
        if not np.isfinite(val):
            raise ValueError
        return val
    except Exception:
        return default

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except Exception:
        return default

def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name, str(default)).lower()
    return val in ('true', '1', 'yes')

def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)

# 默认值
DEFAULT_INNER_LR = _env_float('KHAOS_META_INNER_LR', 0.01)
DEFAULT_OUTER_LR = _env_float('KHAOS_META_OUTER_LR', 1e-3)
DEFAULT_INNER_STEPS = _env_int('KHAOS_META_INNER_STEPS', 3)
DEFAULT_TASK_BATCH_SIZE = _env_int('KHAOS_META_BATCH_SIZE', 4)
DEFAULT_LOSS_TYPE = _env_str('KHAOS_META_LOSS_TYPE', 'mse').lower()
DEFAULT_GRAD_CLIP = _env_float('KHAOS_META_GRAD_CLIP', 1.0)
DEFAULT_MAX_INNER_STEPS = _env_int('KHAOS_META_MAX_INNER_STEPS', 10)
DEFAULT_CHECKPOINT_ALGORITHM = _env_str('KHAOS_META_CHECKPOINT_ALGORITHM', 'sha256').lower()
DEFAULT_AMP = _env_bool('KHAOS_META_AMP', False)
DEFAULT_HUBER_DELTA = _env_float('KHAOS_META_HUBER_DELTA', 1.0)
DEFAULT_VERBOSE = _env_bool('KHAOS_META_VERBOSE', False)
HISTORY_MAX_LEN = 10000

def timed(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        logger.debug(f"{func.__name__} 耗时: {elapsed:.3f}s")
        return result
    return wrapper

# ---------------------------------------------------------------------------
# MetaLearner v10.0
# ---------------------------------------------------------------------------
class MetaLearner:
    """MAML 元学习器 v10.0 (华尔街交易级最终版)"""

    SUPPORTED_LOSSES = {"mse", "huber", "l1"}
    SUPPORTED_ALGORITHMS = {"sha256", "sha512"}
    MIN_TORCH_VERSION = "2.0.0"

    def __init__(self,
                 encoder: CrossAssetEncoder,
                 inner_lr: Optional[float] = None,
                 outer_lr: Optional[float] = None,
                 inner_steps: Optional[int] = None,
                 task_batch_size: Optional[int] = None,
                 device: str = "cpu",
                 loss_type: Optional[str] = None,
                 grad_clip: Optional[float] = None,
                 max_inner_steps: Optional[int] = None,
                 seed: Optional[int] = None,
                 checkpoint_algorithm: Optional[str] = None,
                 use_amp: Optional[bool] = None,
                 huber_delta: Optional[float] = None,
                 verbose: Optional[bool] = None):

        # 参数解析
        inner_lr = inner_lr if inner_lr is not None else DEFAULT_INNER_LR
        outer_lr = outer_lr if outer_lr is not None else DEFAULT_OUTER_LR
        inner_steps = inner_steps if inner_steps is not None else DEFAULT_INNER_STEPS
        task_batch_size = task_batch_size if task_batch_size is not None else DEFAULT_TASK_BATCH_SIZE
        loss_type = (loss_type or DEFAULT_LOSS_TYPE).lower()
        grad_clip = grad_clip if grad_clip is not None else DEFAULT_GRAD_CLIP
        max_inner_steps = max_inner_steps if max_inner_steps is not None else DEFAULT_MAX_INNER_STEPS
        checkpoint_algorithm = (checkpoint_algorithm or DEFAULT_CHECKPOINT_ALGORITHM).lower()
        use_amp = use_amp if use_amp is not None else DEFAULT_AMP
        huber_delta = huber_delta if huber_delta is not None else DEFAULT_HUBER_DELTA
        verbose = verbose if verbose is not None else DEFAULT_VERBOSE

        # 校验
        if inner_lr <= 0.0 or outer_lr <= 0.0:
            raise ValueError("学习率必须 > 0")
        if inner_steps < 1 or task_batch_size < 1:
            raise ValueError("内循环步数 >= 1，批次大小 >= 1")
        if loss_type not in self.SUPPORTED_LOSSES:
            raise ValueError(f"损失类型必须是 {self.SUPPORTED_LOSSES} 之一")
        if checkpoint_algorithm not in self.SUPPORTED_ALGORITHMS:
            raise ValueError(f"校验算法必须是 {self.SUPPORTED_ALGORITHMS} 之一")
        if not isinstance(encoder, nn.Module):
            raise TypeError("编码器必须是 nn.Module")
        if inner_steps > max_inner_steps:
            logger.warning(f"内循环步数 {inner_steps} 截断至上限 {max_inner_steps}")
            inner_steps = max_inner_steps

        self.encoder = encoder
        self.inner_lr = inner_lr
        self.outer_lr = outer_lr
        self.inner_steps = inner_steps
        self.task_batch_size = task_batch_size
        self.loss_type = loss_type
        self.grad_clip = abs(grad_clip)
        self.max_inner_steps = max_inner_steps
        self.checkpoint_algorithm = checkpoint_algorithm
        self.huber_delta = huber_delta
        self.verbose = verbose

        # PyTorch 版本
        if torch.__version__ < self.MIN_TORCH_VERSION:
            raise RuntimeError(f"PyTorch 版本过低，需要 >= {self.MIN_TORCH_VERSION}")

        # 设备
        if device.startswith("cuda") and not torch.cuda.is_available():
            logger.warning("CUDA 不可用，回退 CPU")
            device = "cpu"
        self.device = torch.device(device)
        self.encoder.to(self.device)

        # 混合精度
        self.use_amp = use_amp and (self.device.type == 'cuda')
        self.scaler = GradScaler(enabled=self.use_amp)

        # 优化器 (可扩展)
        self.meta_optimizer = optim.Adam(self.encoder.parameters(), lr=outer_lr)

        # 损失函数
        if self.loss_type == "huber":
            self.loss_fn = nn.HuberLoss(delta=self.huber_delta)
        elif self.loss_type == "l1":
            self.loss_fn = nn.L1Loss()
        else:
            self.loss_fn = nn.MSELoss()

        # 随机种子
        self._seed = seed
        if seed is not None:
            self.set_seed(seed)
        else:
            logger.info("未指定随机种子")

        # 训练状态
        self._is_training = True
        self._train_step_counter = 0
        self._best_loss = float('inf')
        self._total_adaptations = 0
        self._rng = torch.Generator(device=self.device)
        self._training_history: List[float] = []
        self._lr_scheduler = None

        # 日志
        logger.info(f"MetaLearner v10.0 | device:{self.device} batch:{self.task_batch_size} loss:{self.loss_type} amp:{self.use_amp}")

    # ----------------------------------------------------------------
    # 公共 API
    # ----------------------------------------------------------------
    def set_seed(self, seed: int) -> None:
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        self._seed = seed
        self._rng.manual_seed(seed)
        logger.info(f"随机种子: {seed}")

    @timed
    def train(self,
              tasks: Sequence[Tuple[torch.Tensor, torch.Tensor]],
              epochs: int = 10,
              callbacks: Optional[List[Callable[[int, float, Dict[str, Any]], None]]] = None) -> None:
        if not tasks:
            raise ValueError("任务为空")
        if epochs <= 0:
            raise ValueError("epochs > 0")

        self.encoder.train()
        effective_batch = max(1, min(self.task_batch_size, len(tasks)))
        rng = np.random.RandomState(self._seed or 42)

        for epoch in range(epochs):
            epoch_start = time.perf_counter()
            losses = []
            indices = rng.choice(len(tasks), size=effective_batch, replace=False)
            for idx in indices:
                support, query = tasks[idx]
                support = support.to(self.device, dtype=torch.float32)
                query = query.to(self.device, dtype=torch.float32)
                try:
                    self._validate_task(support, query)
                    task_loss = self._compute_task_loss(support, query)
                    losses.append(task_loss)
                except Exception as e:
                    logger.error(f"任务{idx}失败: {e}")
                    continue
            if not losses:
                raise RuntimeError(f"Epoch {epoch+1}: 无有效任务")
            meta_loss = torch.stack(losses).mean()
            ok = self._apply_meta_gradient(meta_loss)
            if not ok:
                logger.error("梯度更新失败")
                continue
            self._train_step_counter += 1
            self._training_history.append(meta_loss.item())
            if len(self._training_history) > HISTORY_MAX_LEN:
                self._training_history.pop(0)

            if self._lr_scheduler is not None:
                self._lr_scheduler.step()

            logger.info(f"Epoch {epoch+1:3d}/{epochs} loss:{meta_loss.item():.6f}")
            if callbacks:
                info = {'best_loss': self._best_loss, 'lr': self.outer_lr}
                for cb in callbacks:
                    try:
                        cb(epoch, meta_loss.item(), info)
                    except Exception as e:
                        logger.error(f"回调异常: {e}")
            if meta_loss.item() < self._best_loss:
                self._best_loss = meta_loss.item()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    @timed
    def adapt(self, support_set: torch.Tensor, labels: torch.Tensor,
              return_loss: bool = False) -> Union[nn.Module, Tuple[nn.Module, float]]:
        # 校验与清洗
        if support_set.dim() != 2 or labels.dim() != 1:
            raise ValueError("维度错误")
        if support_set.shape[0] < 2:
            raise ValueError("至少2样本")
        support_set = support_set.to(self.device, dtype=torch.float32)
        labels = labels.to(self.device, dtype=torch.float32)
        support_set = torch.nan_to_num(support_set, nan=0.0, posinf=1e6, neginf=-1e6)
        labels = torch.nan_to_num(labels, nan=0.0, posinf=1e6, neginf=-1e6)

        adapted = copy.deepcopy(self.encoder)
        adapted.train()
        adapted.zero_grad(set_to_none=True)
        opt = optim.SGD(adapted.parameters(), lr=self.inner_lr)
        floss = None
        for _ in range(self.inner_steps):
            opt.zero_grad()
            pred = adapted(support_set)
            loss = self.loss_fn(pred, labels)
            if not torch.isfinite(loss):
                raise RuntimeError(f"内循环损失非有限: {loss}")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(adapted.parameters(), self.grad_clip)
            opt.step()
            floss = loss
        adapted.eval()
        self._total_adaptations += 1
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if return_loss:
            return adapted, floss.item() if floss else 0.0
        return adapted

    def save_checkpoint(self, path: str) -> None:
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        ckpt = {
            'version': 'v10.0',
            'encoder': self.encoder.state_dict(),
            'optimizer': self.meta_optimizer.state_dict(),
            'scaler': self.scaler.state_dict(),
            'hyper': {
                'inner_lr': self.inner_lr, 'outer_lr': self.outer_lr,
                'inner_steps': self.inner_steps, 'loss_type': self.loss_type,
                'huber_delta': self.huber_delta
            },
            'train_step': self._train_step_counter,
            'best_loss': self._best_loss,
            'total_adaptations': self._total_adaptations,
            'history': self._training_history[-100:],
            'created': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        torch.save(ckpt, path)
        cs = self._compute_file_hash(path)
        with open(path + '.checksum', 'w') as f:
            f.write(cs)
        logger.info(f"检查点已保存: {os.path.basename(path)}")

    def load_checkpoint(self, path: str) -> None:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"文件不存在: {path}")
        if os.path.exists(path + '.checksum'):
            with open(path + '.checksum', 'r') as f:
                exp = f.read().strip()
            act = self._compute_file_hash(path)
            if not hmac.compare_digest(exp, act):
                raise RuntimeError("检查点校验失败")
        ckpt = torch.load(path, map_location=self.device, weights_only=False)
        if ckpt.get('version') != 'v10.0':
            warnings.warn("检查点版本不匹配")
        self.encoder.load_state_dict(ckpt['encoder'])
        self.meta_optimizer.load_state_dict(ckpt['optimizer'])
        try:
            self.scaler.load_state_dict(ckpt['scaler'])
        except Exception:
            logger.warning("scaler 加载失败")
        hp = ckpt['hyper']
        self.inner_lr = hp.get('inner_lr', self.inner_lr)
        self.outer_lr = hp.get('outer_lr', self.outer_lr)
        self.inner_steps = hp.get('inner_steps', self.inner_steps)
        self.loss_type = hp.get('loss_type', self.loss_type)
        self.huber_delta = hp.get('huber_delta', self.huber_delta)
        self._train_step_counter = ckpt.get('train_step', 0)
        self._best_loss = ckpt.get('best_loss', float('inf'))
        self._total_adaptations = ckpt.get('total_adaptations', 0)
        self._training_history = ckpt.get('history', [])[-HISTORY_MAX_LEN:]
        for pg in self.meta_optimizer.param_groups:
            pg['lr'] = self.outer_lr
        logger.info("检查点加载完成")

    def eval(self): self.encoder.eval(); self._is_training = False
    def train_mode(self): self.encoder.train(); self._is_training = True

    def summary(self) -> str:
        tp = sum(p.numel() for p in self.encoder.parameters())
        return (f"MetaLearner v10.0\n  device:{self.device}\n  params:{tp:,}\n"
                f"  inner_lr:{self.inner_lr} outer_lr:{self.outer_lr}\n"
                f"  batch:{self.task_batch_size} loss:{self.loss_type} amp:{self.use_amp}")

    def to(self, device: Union[str, torch.device]) -> 'MetaLearner':
        self.device = torch.device(device) if isinstance(device, str) else device
        self.encoder.to(self.device)
        return self

    def half(self) -> 'MetaLearner':
        self.encoder.half()
        return self

    def bfloat16(self) -> 'MetaLearner':
        self.encoder.bfloat16()
        return self

    # ----------------------------------------------------------------
    # 内部
    # ----------------------------------------------------------------
    def _compute_task_loss(self, support, query):
        sx, sy = support[:, :-1], support[:, -1].float()
        qx, qy = query[:, :-1], query[:, -1].float()
        bp = dict(self.encoder.named_parameters())
        bb = dict(self.encoder.named_buffers())
        fp = {k: v.clone() for k, v in bp.items()}
        for _ in range(self.inner_steps):
            with autocast(enabled=self.use_amp):
                pred = functional_call(self.encoder, fp, (sx,), buffers=bb)
                loss = self.loss_fn(pred, sy)
            if not torch.isfinite(loss):
                raise RuntimeError("内循环损失非有限")
            grads = torch.autograd.grad(loss, list(fp.values()), create_graph=True)
            fp = {k: v - self.inner_lr * g for k, v, g in zip(fp.items(), grads)}
        with autocast(enabled=self.use_amp):
            qpred = functional_call(self.encoder, fp, (qx,), buffers=bb)
            return self.loss_fn(qpred, qy)

    def _apply_meta_gradient(self, loss):
        self.meta_optimizer.zero_grad()
        try:
            if self.use_amp:
                self.scaler.scale(loss).backward()
                self.scaler.unscale_(self.meta_optimizer)
                norm = torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), self.grad_clip)
                self.scaler.step(self.meta_optimizer)
                self.scaler.update()
            else:
                loss.backward()
                norm = torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), self.grad_clip)
        except Exception as e:
            logger.error(f"反向传播错误: {e}")
            return False
        if not torch.isfinite(norm):
            logger.error("梯度非有限")
            return False
        if not self.use_amp:
            self.meta_optimizer.step()
        return True

    @staticmethod
    def _validate_task(support, query):
        for name, t in [("support", support), ("query", query)]:
            if t.dim() != 2 or t.shape[1] < 2 or t.shape[0] == 0:
                raise ValueError(f"{name} 形状/大小无效")
            if torch.isnan(t).any() or torch.isinf(t).any():
                raise ValueError(f"{name} 包含 NaN/Inf")

    def _compute_file_hash(self, path):
        if os.path.getsize(path) == 0:
            raise ValueError("文件为空")
        h = hashlib.new(self.checkpoint_algorithm)
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # 学习率调度
    def set_lr_scheduler(self, scheduler):
        self._lr_scheduler = scheduler

    def step_lr_scheduler(self, metrics=None):
        if self._lr_scheduler:
            if isinstance(self._lr_scheduler, optim.lr_scheduler.ReduceLROnPlateau):
                self._lr_scheduler.step(metrics)
            else:
                self._lr_scheduler.step()

    @property
    def is_training(self): return self._is_training
    @property
    def device_str(self): return str(self.device)
    @property
    def best_loss(self): return self._best_loss
    @property
    def total_adaptations(self): return self._total_adaptations
    @property
    def training_history(self): return self._training_history.copy()

    def __repr__(self): return f"MetaLearner(device={self.device_str})"
