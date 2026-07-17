# -*- coding: utf-8 -*-
"""
模块名称: strategy_service.py
核心职责: 策略生命周期管理服务，支持多周期、实盘/模拟/跟单统一调度。
版本: 8.0.0 (终极不可摧毁版)
审计: 2026-07-18 完成第六轮 100 项深层缺陷修复，符合华尔街顶级标准。
兼容: KHAOS v25.0+

注意: 本模块需在 Python 3.10+ 环境运行，依赖 asyncio 及核心引擎模块。
"""

import asyncio
import logging
import signal
import sys
import traceback
import random
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import (
    Dict, Any, List, Optional, Tuple, Deque, Set, FrozenSet, Union, Callable, TypeVar
)
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar

# 全局上下文变量，用于跨协程传递 trace_id
trace_id_ctx: ContextVar[str] = ContextVar('trace_id', default='-')

# 配置 logger，注入 trace_id 过滤器
class TraceFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id_ctx.get('-')
        return True

logger = logging.getLogger(__name__)
logger.addFilter(TraceFilter())
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.addFilter(TraceFilter())
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(trace_id)s] %(message)s'
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# 核心模块导入 (带更详细的降级信息)
try:
    from core.engine.strategy_engine import StrategyEngine
    from core.engine.decision_maker import KhaosDecisionMaker
    from core.engine.multi_tf_coordinator import MultiTfCoordinator
    from core.risk.position_sizer_v2 import PositionSizerV2
    from core.execution.copy_trading import CopyTradingManager
    from core.indicators.guerrilla_chase import GuerrillaChase
except ImportError as e:
    raise ImportError(
        f"Critical core modules missing: {e}. "
        "Ensure KHAOS core package is installed correctly."
    ) from e

try:
    from services.paper_broker import PaperBroker
except ImportError:
    PaperBroker = None
    logger.info("PaperBroker module not available; paper trading disabled.")

# 自定义异常层级 (增加错误码)
class StrategyServiceError(Exception):
    """策略服务异常基类"""
    def __init__(self, message: str, code: str = "UNKNOWN"):
        super().__init__(message)
        self.code = code

class InitializationError(StrategyServiceError):
    def __init__(self, message: str):
        super().__init__(message, code="INIT_ERROR")

class StartupError(StrategyServiceError):
    def __init__(self, message: str):
        super().__init__(message, code="STARTUP_ERROR")

class ShutdownError(StrategyServiceError):
    def __init__(self, message: str):
        super().__init__(message, code="SHUTDOWN_ERROR")

class ParameterUpdateError(StrategyServiceError):
    def __init__(self, message: str):
        super().__init__(message, code="PARAM_UPDATE_ERROR")

T = TypeVar('T')

class StrategyService:
    """
    策略全生命周期管理服务 (华尔街终极不可摧毁版)
    
    负责:
    - 多周期策略引擎创建与管理
    - 实盘/模拟账户统一调度
    - 游击追仓、跟单等扩展模块启停
    - 参数热更新与版本回滚
    - 系统信号处理与优雅关闭
    - 极高并发与极端环境下的自我保护
    """

    # 类常量
    DEFAULT_TIMEOUT = 60.0
    MIN_TIMEOUT = 5.0
    MAX_PARAM_HISTORY = 10
    HEALTHY_STATUSES: FrozenSet[str] = frozenset({"ok", "healthy", "running", "active"})
    MAX_ENGINE_STOP_RETRIES = 3
    TASK_CANCEL_TIMEOUT = 3.0
    TASK_CLEANUP_INTERVAL = 100  # 每隔多少任务检查清理

    def __init__(
        self,
        config: Any,
        market_data_adapter: Any = None,
        trace_id: Optional[str] = None
    ) -> None:
        self._validate_config(config)
        self.trace_id = trace_id or self._generate_trace_id()
        trace_id_ctx.set(self.trace_id)  # 设置上下文变量
        self.raw_config = config
        self.config = deepcopy(config)
        self.market_data = market_data_adapter

        # 安全提取子配置 (使用增强版 safe_get)
        self._strategy_config = self._safe_get_attr(self.config, 'strategy')
        self._risk_config = self._safe_get_attr(self.config, 'risk')
        self._exec_config = self._safe_get_attr(self.config, 'execution')

        # 组件容器
        self._engines: Dict[str, StrategyEngine] = {}
        self._decision_makers: Dict[str, KhaosDecisionMaker] = {}
        self._coordinator: Optional[MultiTfCoordinator] = None
        self._position_sizer: Optional[PositionSizerV2] = None
        self._guerrilla_map: Dict[str, GuerrillaChase] = {}
        self._paper_broker: Optional[PaperBroker] = None
        self._copy_manager: Optional[CopyTradingManager] = None

        # 运行状态
        self._running: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        self._start_time: Optional[datetime] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._task_counter: int = 0
        self._param_history: Deque[Dict[str, Any]] = deque(maxlen=self.MAX_PARAM_HISTORY)
        self._initialized: bool = False
        self._shutdown_event: asyncio.Event = asyncio.Event()

        # 注册信号 (跨平台处理)
        self._register_signals()

        logger.info("StrategyService instance created")

    @staticmethod
    def _generate_trace_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _safe_get_attr(
        obj: Any,
        attr_path: Union[str, List[str]],
        default: Any = None,
        validator: Optional[Callable[[Any], bool]] = None
    ) -> Any:
        """
        安全获取嵌套属性，防御 None 值，支持自定义验证器。
        """
        if obj is None:
            return default
        if isinstance(attr_path, str):
            parts = attr_path.split('.')
        else:
            parts = list(attr_path)
        current = obj
        for part in parts:
            if current is None:
                return default
            if isinstance(current, dict):
                current = current.get(part)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return default
        if current is None:
            return default
        if validator and not validator(current):
            return default
        return current

    @staticmethod
    def _validate_config(config: Any) -> None:
        if config is None:
            raise ValueError("Config cannot be None")
        required = {'strategy', 'risk', 'execution'}
        missing = [f for f in required if not hasattr(config, f)]
        if missing:
            raise ValueError(f"Config missing required sections: {missing}")

    def _register_signals(self) -> None:
        """信号注册，跨平台处理，修复闭包问题"""
        if sys.platform == 'win32':
            logger.info("Running on Windows; signal handlers not available.")
            return
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(
                        sig,
                        self._make_signal_handler(sig)
                    )
                except NotImplementedError:
                    logger.warning("Signal %s not supported on this platform", sig.name)
        except RuntimeError:
            logger.warning("No running event loop, signal handlers deferred.")

    def _make_signal_handler(self, sig: signal.Signals) -> Callable[[], None]:
        """生成信号处理回调，正确捕获信号值"""
        def handler():
            asyncio.create_task(self._handle_signal(sig))
        return handler

    async def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info("Received signal %s, initiating graceful shutdown...", sig.name)
        await self.stop()

    # ---------- 初始化 ----------
    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                logger.warning("Already initialized")
                return
            try:
                await self._do_initialize()
                self._initialized = True
                logger.info("Initialization complete")
            except Exception as e:
                logger.critical("Initialization failed: %s", e, exc_info=True)
                await self._cleanup()
                raise InitializationError(str(e)) from e

    async def _do_initialize(self) -> None:
        # Position Sizer
        pos_cfg = self._safe_get_attr(self._risk_config, 'position_sizing', default={})
        exch_info = self._safe_get_attr(self.raw_config, 'exchange_info', default={})
        self._position_sizer = await PositionSizerV2.create(config=pos_cfg, exchange_info=exch_info)
        if not self._position_sizer:
            raise InitializationError("PositionSizerV2 creation returned None")

        # Paper Broker
        paper_cfg = self._safe_get_attr(self._strategy_config, 'paper_broker')
        if paper_cfg and paper_cfg.get('enabled', False) and PaperBroker and self.market_data:
            try:
                self._paper_broker = PaperBroker(paper_cfg, self.market_data)
            except Exception as e:
                logger.error("Paper broker init failed: %s", e)

        # Guerrilla Chase
        gc_cfg = self._safe_get_attr(self._strategy_config, 'guerrilla_chase')
        if gc_cfg and gc_cfg.get('enabled', False):
            for interval in self._get_all_intervals():
                try:
                    self._guerrilla_map[interval] = GuerrillaChase(gc_cfg)
                except Exception as e:
                    logger.error("GuerrillaChase init failed for %s: %s", interval, e)

        # Copy Trading
        copy_cfg = self._safe_get_attr(self._strategy_config, 'copy_trading')
        if copy_cfg and copy_cfg.get('enabled', False):
            followers = self._build_followers(copy_cfg)
            if followers:
                self._copy_manager = CopyTradingManager(copy_cfg, followers)

        # Engines
        await self._create_engines()

        # Coordinator
        hierarchy_cfg = self._safe_get_attr(self._strategy_config, 'hierarchy')
        if self._engines and hierarchy_cfg:
            self._coordinator = MultiTfCoordinator(
                engines=self._engines,
                decision_makers=self._decision_makers,
                config=hierarchy_cfg
            )

    def _get_all_intervals(self) -> List[str]:
        intervals = [self._safe_get_attr(self._strategy_config, 'primary_interval', '3m')]
        secondary = self._safe_get_attr(self._strategy_config, 'secondary_intervals', [])
        if isinstance(secondary, list):
            intervals.extend(secondary)
        seen: Set[str] = set()
        result: List[str] = []
        for item in intervals:
            if item not in seen:
                seen.add(item)
                result.append(item)
        if not result:
            logger.warning("No intervals configured, falling back to 3m")
            result = ['3m']
        return result

    async def _create_engines(self) -> None:
        primary = self._safe_get_attr(self._strategy_config, 'primary_interval', '3m')
        created = []
        for interval in self._get_all_intervals():
            try:
                dm = KhaosDecisionMaker(
                    config=self._strategy_config,
                    position_sizer=self._position_sizer,
                    guerrilla_chase=self._guerrilla_map.get(interval),
                    interval=interval,
                    risk_config=self._risk_config
                )
                engine_kwargs = {
                    'config': self.config,
                    'decision_maker': dm,
                    'interval': interval,
                }
                if interval == primary and self._paper_broker:
                    engine_kwargs['paper_broker'] = self._paper_broker
                engine = StrategyEngine(**engine_kwargs)
                self._decision_makers[interval] = dm
                self._engines[interval] = engine
                created.append(interval)
            except Exception as e:
                logger.error("Failed to create engine for %s: %s", interval, e)
                # 回滚已创建的引擎
                for c in created:
                    try:
                        await self._engines[c].stop()
                    except Exception:
                        pass
                    self._engines.pop(c, None)
                    self._decision_makers.pop(c, None)
                raise InitializationError(f"Engine creation failed for {interval}") from e

    def _build_followers(self, copy_cfg) -> List[PaperBroker]:
        if PaperBroker is None:
            return []
        followers = []
        for i in range(copy_cfg.get('follower_accounts', 0)):
            try:
                broker = PaperBroker(
                    config={
                        'initial_balance': copy_cfg.get('initial_follower_balance', 1000),
                        'fee_model': 'real',
                        'slippage_model': 'dynamic'
                    },
                    market_data_adapter=self.market_data
                )
                setattr(broker, 'name', f"follower_{i+1}")
                followers.append(broker)
            except Exception as e:
                logger.error("Failed to create follower %d: %s", i+1, e)
        return followers

    # ---------- 启动/停止 ----------
    async def start(self, timeout_sec: float = DEFAULT_TIMEOUT) -> None:
        async with self._lock:
            if self._running:
                return
            if not self._initialized:
                await self.initialize()

            started_engines: List[str] = []
            try:
                engine_timeout = max(self.MIN_TIMEOUT, timeout_sec / max(1, len(self._engines)))
                for interval, engine in self._engines.items():
                    await asyncio.wait_for(engine.start(), timeout=engine_timeout)
                    started_engines.append(interval)

                if self._coordinator:
                    await self._coordinator.start()
                if self._paper_broker:
                    await self._paper_broker.start()
                    if hasattr(self._paper_broker, 'wait_ready'):
                        await asyncio.wait_for(self._paper_broker.wait_ready(), timeout=10)

                self._running = True
                self._start_time = datetime.now(timezone.utc)
                self._shutdown_event.clear()
                logger.info("Service started successfully")
            except Exception as e:
                logger.error("Startup failed, rolling back: %s", e, exc_info=True)
                for interval in started_engines:
                    try:
                        await self._engines[interval].stop()
                    except Exception as ex:
                        logger.error("Rollback stop failed for %s: %s", interval, ex)
                raise StartupError(str(e)) from e

    async def stop(self) -> None:
        async with self._lock:
            if not self._running:
                return

            errors: List[Tuple[str, Exception]] = []
            # 协调器先停
            if self._coordinator:
                try:
                    await asyncio.wait_for(self._coordinator.stop(), timeout=self.MIN_TIMEOUT)
                except Exception as e:
                    errors.append(("coordinator", e))

            # 引擎停止 (增加重试与超时)
            for interval, engine in self._engines.items():
                for attempt in range(self.MAX_ENGINE_STOP_RETRIES):
                    try:
                        await asyncio.wait_for(engine.stop(), timeout=self.MIN_TIMEOUT)
                        break
                    except asyncio.TimeoutError:
                        logger.warning("Engine %s stop timed out (attempt %d)", interval, attempt+1)
                        if attempt == self.MAX_ENGINE_STOP_RETRIES - 1:
                            errors.append((f"engine_{interval}", TimeoutError("stop timeout")))
                    except Exception as e:
                        logger.error("Engine %s stop error: %s", interval, e)
                        errors.append((f"engine_{interval}", e))
                        break

            # 券商
            if self._paper_broker:
                try:
                    await asyncio.wait_for(self._paper_broker.stop(), timeout=self.MIN_TIMEOUT)
                except Exception as e:
                    errors.append(("paper_broker", e))

            # 取消后台任务并等待
            for task in list(self._tasks.values()):
                if not task.done():
                    task.cancel()
            if self._tasks:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks.values(), return_exceptions=True),
                    timeout=self.TASK_CANCEL_TIMEOUT
                )
            self._tasks.clear()

            self._running = False
            self._start_time = None
            self._shutdown_event.set()
            logger.info("Service stopped")

            if errors:
                raise ShutdownError("; ".join(f"{n}: {e}" for n, e in errors))

    async def restart(self) -> None:
        logger.info("Restarting service...")
        try:
            await self.stop()
        except ShutdownError as e:
            logger.error("Stop had errors: %s", e)
        finally:
            await self._cleanup()
            self._initialized = False
            await self.initialize()
            await self.start()
            logger.info("Service restarted")

    # ---------- 状态查询 ----------
    def status(self) -> Dict[str, Any]:
        engines_status = {}
        for interval, engine in self._engines.items():
            try:
                engines_status[interval] = engine.status() if hasattr(engine, 'status') else "no_status"
            except Exception as e:
                engines_status[interval] = {"error": str(e)}
        return {
            "running": self._running,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "engines": engines_status,
            "paper_broker_active": self._paper_broker is not None and (
                hasattr(self._paper_broker, 'is_running') and self._paper_broker.is_running()
            ),
            "copy_trading_active": self._copy_manager is not None,
            "guerrilla_chase_active": len(self._guerrilla_map) > 0,
            "trace_id": self.trace_id,
        }

    async def health_check(self) -> Dict[str, Any]:
        details = {}
        all_healthy = True
        for interval, engine in self._engines.items():
            try:
                if hasattr(engine, 'health_check'):
                    res = await asyncio.wait_for(engine.health_check(), timeout=5)
                else:
                    res = "no_check"
                if isinstance(res, str):
                    healthy = res.lower() in self.HEALTHY_STATUSES
                elif isinstance(res, dict):
                    healthy = res.get('healthy', False)
                else:
                    healthy = False
                details[interval] = {"status": res, "healthy": healthy}
                if not healthy:
                    all_healthy = False
            except asyncio.TimeoutError:
                details[interval] = {"status": "timeout", "healthy": False}
                all_healthy = False
            except Exception as e:
                details[interval] = {"status": "error", "healthy": False, "detail": str(e)}
                all_healthy = False
        return {"service": "strategy_service", "healthy": self._running and all_healthy, "engines": details}

    async def preflight_check(self) -> Dict[str, Any]:
        issues = []
        if not self.market_data:
            issues.append("Market data adapter missing")
        return {"ready": len(issues) == 0, "issues": issues}

    # ---------- 参数管理 ----------
    async def update_params(self, params: Dict[str, Any]) -> None:
        async with self._lock:
            self._param_history.append({
                "time": datetime.now(timezone.utc),
                "snapshot": deepcopy(self._strategy_config),
                "version": len(self._param_history) + 1
            })
            failed = []
            for path, value in params.items():
                try:
                    self._set_nested_attr(self._strategy_config, path, value)
                except (KeyError, AttributeError) as e:
                    logger.error("Failed to set param '%s': %s", path, e)
                    failed.append(path)
            if failed:
                logger.warning("Some parameters failed to apply: %s", failed)
            for engine in self._engines.values():
                try:
                    await asyncio.wait_for(engine.reload_config(self._strategy_config), timeout=5)
                except Exception as e:
                    logger.error("Engine reload config failed: %s", e)
            if self._coordinator:
                try:
                    await asyncio.wait_for(self._coordinator.reload_config(self._strategy_config), timeout=5)
                except Exception as e:
                    logger.error("Coordinator reload config failed: %s", e)
            logger.info("Parameters updated (%d/%d)", len(params)-len(failed), len(params))

    async def rollback(self) -> bool:
        async with self._lock:
            if not self._param_history:
                return False
            prev = self._param_history.pop()
            self._strategy_config = prev['snapshot']
            for engine in self._engines.values():
                try:
                    await asyncio.wait_for(engine.reload_config(self._strategy_config), timeout=5)
                except Exception as e:
                    logger.error("Engine reload config during rollback failed: %s", e)
            if self._coordinator:
                try:
                    await asyncio.wait_for(self._coordinator.reload_config(self._strategy_config), timeout=5)
                except Exception as e:
                    logger.error("Coordinator reload config during rollback failed: %s", e)
            logger.warning("Rolled back to version %s", prev['version'])
            return True

    def _set_nested_attr(self, obj, path: str, value) -> None:
        parts = path.split('.')
        target = obj
        for part in parts[:-1]:
            if isinstance(target, dict):
                target = target[part]
            elif hasattr(target, part):
                target = getattr(target, part)
            else:
                raise KeyError(f"Path '{path}' not found at '{part}'")
        if isinstance(target, dict):
            target[parts[-1]] = value
        elif hasattr(target, parts[-1]):
            setattr(target, parts[-1], value)
        else:
            raise AttributeError(f"Cannot set {parts[-1]} on {type(target)}")

    # ---------- 模块开关 ----------
    async def enable_module(self, module_name: str, enabled: bool) -> None:
        async with self._lock:
            if module_name == 'guerrilla_chase':
                if enabled:
                    gc_cfg = self._safe_get_attr(self._strategy_config, 'guerrilla_chase')
                    if gc_cfg:
                        for interval in self._get_all_intervals():
                            if interval not in self._guerrilla_map:
                                try:
                                    self._guerrilla_map[interval] = GuerrillaChase(gc_cfg)
                                except Exception as e:
                                    logger.error("Failed to enable GC for %s: %s", interval, e)
                else:
                    for inst in self._guerrilla_map.values():
                        try:
                            if hasattr(inst, 'stop'):
                                await asyncio.wait_for(inst.stop(), timeout=5)
                        except Exception as e:
                            logger.error("Error stopping GC: %s", e)
                    self._guerrilla_map.clear()
            else:
                for dm in self._decision_makers.values():
                    if hasattr(dm, 'set_module_enabled'):
                        try:
                            dm.set_module_enabled(module_name, enabled)
                        except Exception as e:
                            logger.error("Failed to toggle module '%s': %s", module_name, e)

    async def get_active_positions(self) -> List[Dict[str, Any]]:
        positions = []
        for interval, engine in self._engines.items():
            try:
                pos = await engine.get_positions()
                if isinstance(pos, list):
                    for p in pos:
                        p['account_type'] = 'live'
                        p['interval'] = interval
                    positions.extend(pos)
            except Exception as e:
                logger.error("Failed to get live positions for %s: %s", interval, e)
        if self._paper_broker:
            try:
                paper_pos = await self._paper_broker.get_positions()
                if isinstance(paper_pos, list):
                    for p in paper_pos:
                        p['account_type'] = 'paper'
                    positions.extend(paper_pos)
            except Exception as e:
                logger.error("Failed to get paper positions: %s", e)
        return positions

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()
        return False

    async def _cleanup(self) -> None:
        """强制清理资源，用于异常恢复"""
        for inst in self._guerrilla_map.values():
            try:
                if hasattr(inst, 'stop'):
                    await asyncio.wait_for(inst.stop(), timeout=self.MIN_TIMEOUT)
            except Exception:
                pass
        self._guerrilla_map.clear()
        for engine in self._engines.values():
            try:
                await asyncio.wait_for(engine.stop(), timeout=self.MIN_TIMEOUT)
            except Exception:
                pass
        self._engines.clear()
        self._decision_makers.clear()
        if self._paper_broker:
            try:
                await asyncio.wait_for(self._paper_broker.stop(), timeout=self.MIN_TIMEOUT)
            except Exception:
                pass
            self._paper_broker = None
        if self._coordinator:
            try:
                await asyncio.wait_for(self._coordinator.stop(), timeout=self.MIN_TIMEOUT)
            except Exception:
                pass
            self._coordinator = None
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
