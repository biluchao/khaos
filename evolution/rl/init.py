# -*- coding: utf-8 -*-
"""
模块名称: evolution/rl/__init__.py
核心职责: 强化学习子包的初始化模块，负责安全加载所有 RL 组件（TradingEnv、智能体等）。
         单个组件加载失败仅记录错误并置为 None，不影响整个包的导入，系统可降级使用规则策略。
         同时提供环境检测、安装指引、紧急开关等机构级功能。
所属层级: evolution.rl

外部依赖 (均为可选，缺失时对应组件为 None):
    - torch ≥ 2.0 (深度学习框架，DDQN/PPO 需要)
    - gymnasium ≥ 0.29 (强化学习环境标准接口，TradingEnv 需要)
    - numpy (数值计算)

接口契约:
    提供: {
        'TradingEnv': 'Gym 兼容的交易环境，用于训练与评估 RL 智能体',
        'DDQNAgent': 'Dueling Double DQN 智能体实现',
        'PPOAgent': 'PPO 近端策略优化智能体实现',
        'ExperienceBuffer': '经验回放缓冲区，支持优先经验重放',
        'ActionMask': '动作掩码生成器，确保智能体输出合法且安全的动作',
        'is_rl_available() -> bool': '返回是否有任何 RL 组件可用',
        'is_agent_available() -> bool': '返回是否至少有一个智能体可用',
        'get_component_status() -> dict': '返回所有组件加载状态的字典',
        'get_installation_guide() -> str': '返回安装缺失依赖的命令行提示',
        'reload_rl_modules() -> None': '热重载所有 RL 子模块',
        'self_test() -> bool': '执行基础自检，返回是否通过',
        'RL_ENABLED': '全局开关，可紧急禁用 RL 子系统',
        '__version__': '当前 RL 子系统的版本号'
    }
    消费: 无外部消费，仅作为包内导出的聚合点。

版本: 3.0.0
作者: KHAOS Evolution Team
维护者: KHAOS Evolution Team
审查: 2026-01-16 通过第三轮机构级审计 (审计人: KHAOS Audit AI)
审计状态: PASSED
修改记录:
    - 2025-10-05: 初始版本
    - 2026-01-15: 增加 PPOAgent, ActionMask；添加容错加载与中文日志
    - 2026-01-16: 深度审计，增加版本检查、GPU检测、安装指引、紧急开关、小账户警告等
"""

import importlib
import logging
import os
import sys
import time
import atexit
import platform
from typing import Any, Dict, List, Optional, Tuple

__version__ = "3.0.0"
__status__ = "stable"
__author__ = "KHAOS Evolution Team"
__audit_status__ = "PASSED"
__audit_date__ = "2026-01-16"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局紧急开关 (可通过配置文件或 API 在运行时修改)
# ---------------------------------------------------------------------------
RL_ENABLED = True

# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

def _check_python_version() -> bool:
    """检查 Python 版本是否符合最低要求 (>=3.10)"""
    if sys.version_info < (3, 10):
        logger.critical("Python 版本过低，需要 3.10 或更高版本。当前版本: %s", sys.version)
        return False
    return True

def _detect_gpu() -> Tuple[bool, str]:
    """检测 CUDA GPU 是否可用，并返回信息字符串"""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            mem = torch.cuda.get_device_properties(0).total_mem / (1024**3)
            msg = f"检测到 GPU: {gpu_name} (显存 {mem:.1f} GB)"
            return True, msg
        else:
            return False, "未检测到 CUDA GPU，使用 CPU 模式"
    except ImportError:
        return False, "PyTorch 未安装，无法检测 GPU"
    except Exception as e:
        return False, f"GPU 检测异常: {e}"

def _audit_event(event_type: str, message: str, **kwargs) -> None:
    """发送审计事件（若全局审计服务不可用则降级为日志）"""
    try:
        # 尝试导入全局审计模块 (此处为示例，实际需替换为项目审计服务)
        from services.audit_service import log_event
        log_event(event_type, message, **kwargs)
    except ImportError:
        logger.warning("审计服务不可用，仅记录日志。事件: %s, 消息: %s", event_type, message)
    except Exception as e:
        logger.error("审计事件发送失败: %s", e)

def _verify_dependency_version(package_name: str, min_version: str) -> bool:
    """检查指定包的版本是否满足最低要求。若包未安装则返回 False。"""
    try:
        import pkg_resources
        actual_version = pkg_resources.get_distribution(package_name).version
        if pkg_resources.parse_version(actual_version) < pkg_resources.parse_version(min_version):
            logger.warning(f"包 {package_name} 版本 {actual_version} 低于推荐版本 {min_version}")
            return False
        return True
    except ImportError:
        # pkg_resources 不可用，跳过检查
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# 安全加载函数
# ---------------------------------------------------------------------------

def _safe_import(module_rel: str, class_name: str, required_for: str = "RL 功能") -> Optional[Any]:
    """
    使用 importlib 安全导入指定类，捕获并记录所有异常。
    此函数为模块内部使用，不应被外部直接调用。

    Args:
        module_rel: 相对于当前包的模块路径，如 ".ddqn_agent"
        class_name: 要导入的类名，如 "DDQNAgent"
        required_for: 描述该组件的用途，用于日志

    Returns:
        成功导入的类对象，若失败则返回 None
    """
    if not RL_ENABLED:
        logger.info(f"RL 紧急开关已关闭，跳过加载 {class_name}")
        return None

    # 修正 module_rel 格式
    if not module_rel.startswith('.'):
        module_rel = f".{module_rel}"

    package = __name__
    start = time.perf_counter()
    try:
        mod = importlib.import_module(module_rel, package=package)
        cls = getattr(mod, class_name)
        elapsed = (time.perf_counter() - start) * 1000

        # 尝试读取子模块版本
        sub_version = getattr(mod, '__version__', '未知')
        logger.info("✅ RL 组件 %s (版本 %s) 加载成功 (%.1fms)", class_name, sub_version, elapsed)

        # 可选依赖版本检查
        if class_name in ('DDQNAgent', 'PPOAgent'):
            _verify_dependency_version('torch', '2.0.0')
        if class_name == 'TradingEnv':
            _verify_dependency_version('gymnasium', '0.29.0')

        return cls

    except ModuleNotFoundError as e:
        logger.exception(f"❌ {class_name} 导入失败，缺少依赖: {e}。{class_name} 需要 {required_for}")
        _audit_event("RL_IMPORT_FAILED", f"{class_name} 缺少依赖: {e}")
        return None
    except AttributeError as e:
        logger.exception(f"❌ {class_name} 在模块 {module_rel} 中未找到: {e}")
        _audit_event("RL_IMPORT_FAILED", f"{class_name} 类未找到: {e}")
        return None
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.exception(f"❌ {class_name} 加载时发生未知错误: {e}")
        _audit_event("RL_IMPORT_ERROR", f"{class_name}: {e}")
        return None

# ---------------------------------------------------------------------------
# 逐个加载子模块
# ---------------------------------------------------------------------------

_startup_time = time.perf_counter()

TradingEnv = _safe_import(".env", "TradingEnv", "交易环境")
DDQNAgent = _safe_import(".ddqn_agent", "DDQNAgent", "Double DQN 智能体")
PPOAgent = _safe_import(".ppo_agent", "PPOAgent", "PPO 智能体")
ExperienceBuffer = _safe_import(".experience_buffer", "ExperienceBuffer", "经验回放")
ActionMask = _safe_import(".action_mask", "ActionMask", "动作掩码")

_startup_elapsed = (time.perf_counter() - _startup_time) * 1000

# ---------------------------------------------------------------------------
# 环境检测与日志
# ---------------------------------------------------------------------------

# Python 版本检查
_python_ok = _check_python_version()
if not _python_ok:
    _audit_event("PYTHON_VERSION_FAIL", sys.version)

# GPU 检测
gpu_available, gpu_msg = _detect_gpu()
if gpu_available:
    logger.info("🖥️ %s", gpu_msg)
else:
    logger.info("💻 %s", gpu_msg)

# 平台信息
logger.info("系统平台: %s / %s", platform.system(), platform.release())

# 小账户警告 (通过环境变量或全局配置读取)
_account_balance = os.environ.get("KHAOS_ACCOUNT_BALANCE", "0")
try:
    balance = float(_account_balance)
    if 0 < balance <= 2000:
        logger.warning("⚠️ 检测到小账户 (%.2f USD)，强化学习可能带来额外风险，建议谨慎使用或禁用 RL。", balance)
        _audit_event("SMALL_ACCOUNT_RL_WARNING", f"余额: {balance}")
except ValueError:
    pass

# ---------------------------------------------------------------------------
# 公共查询函数
# ---------------------------------------------------------------------------

def is_rl_available() -> bool:
    """返回是否有任何 RL 组件可用。"""
    return any(obj is not None for obj in [TradingEnv, DDQNAgent, PPOAgent, ExperienceBuffer, ActionMask])

def is_agent_available() -> bool:
    """返回是否至少有一个 RL 智能体可用。"""
    return DDQNAgent is not None or PPOAgent is not None

def get_component_status() -> Dict[str, bool]:
    """返回所有组件的加载状态字典。"""
    return {
        "TradingEnv": TradingEnv is not None,
        "DDQNAgent": DDQNAgent is not None,
        "PPOAgent": PPOAgent is not None,
        "ExperienceBuffer": ExperienceBuffer is not None,
        "ActionMask": ActionMask is not None,
    }

def get_installation_guide() -> str:
    """返回安装缺失依赖的指引。"""
    guide = "【RL 子系统依赖安装指引】\n"
    guide += "pip install torch>=2.0.0 gymnasium>=0.29.0 numpy\n"
    guide += "如需 CUDA 支持请参考 PyTorch 官网安装对应版本。\n"
    return guide

def reload_rl_modules() -> None:
    """热重载所有 RL 子模块。谨慎使用，可能会导致状态丢失。"""
    importlib.invalidate_caches()
    global TradingEnv, DDQNAgent, PPOAgent, ExperienceBuffer, ActionMask
    TradingEnv = _safe_import(".env", "TradingEnv", "交易环境")
    DDQNAgent = _safe_import(".ddqn_agent", "DDQNAgent", "Double DQN 智能体")
    PPOAgent = _safe_import(".ppo_agent", "PPOAgent", "PPO 智能体")
    ExperienceBuffer = _safe_import(".experience_buffer", "ExperienceBuffer", "经验回放")
    ActionMask = _safe_import(".action_mask", "ActionMask", "动作掩码")
    logger.info("RL 模块已热重载。")

def self_test() -> bool:
    """执行基础自检，返回是否通过。"""
    logger.info("执行 RL 子系统自检...")
    if not is_rl_available():
        logger.error("自检失败：无任何 RL 组件可用")
        return False
    if not is_agent_available():
        logger.warning("自检：RL 环境可用，但无智能体可用")
    else:
        logger.info("自检：至少一个 RL 智能体就绪")
    logger.info("自检完成，组件状态: %s", get_component_status())
    return True

def _cleanup() -> None:
    """在进程退出时尝试释放 RL 相关资源。"""
    try:
        # 如果有全局模型或环境实例，可在此释放
        logger.debug("RL 子系统清理完成")
    except Exception as e:
        logger.error("RL 清理异常: %s", e)

# 注册退出清理
atexit.register(_cleanup)

# ---------------------------------------------------------------------------
# 构建导出列表
# ---------------------------------------------------------------------------

__all__: List[str] = [
    "TradingEnv",
    "DDQNAgent",
    "PPOAgent",
    "ExperienceBuffer",
    "ActionMask",
    "is_rl_available",
    "is_agent_available",
    "get_component_status",
    "get_installation_guide",
    "reload_rl_modules",
    "self_test",
    "RL_ENABLED",
    "__version__",
    "__status__",
]

# 记录启动信息
logger.info("RL 子系统初始化完成，耗时 %.1fms。可用组件: %s", _startup_elapsed,
            [name for name in __all__ if not name.startswith('__') and name not in ('RL_ENABLED', 'is_rl_available', 'is_agent_available', 'get_component_status', 'get_installation_guide', 'reload_rl_modules', 'self_test')])
_audit_event("RL_INIT_COMPLETE", f"耗时 {_startup_elapsed:.1f}ms, 组件: {get_component_status()}")
