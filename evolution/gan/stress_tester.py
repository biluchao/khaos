# -*- coding: utf-8 -*-
"""
模块名称: stress_tester.py
核心职责: 基于 TimeGAN 生成极端市场场景，对策略进行全方位压力测试，评估尾部风险并输出机构级报告。
所属层级: evolution.gan

外部依赖:
    - numpy, time, copy, json, logging, os, uuid, dataclasses, typing
    - evolution.gan.timegan_model (TimeGANModel)
    - evolution.bapo.replay_engine (ShadowReplayEngine)

接口契约:
    提供: StressTester (run_stress_test, generate_scenarios, evaluate_scenario, reset, dispose, export_report_json)
    消费: TimeGANModel, ShadowReplayEngine

配置项:
    - evolution.gan_stress.* (场景数、质量检查、报告格式等)

作者: KHAOS Evolution Team
创建日期: 2025-11-01
修改记录:
    - v1: 初始版本
    - v2: 增加 VaR/CVaR，场景过滤
    - v3: 100 项机构缺陷修复
    - v4: 第二轮 100 项修复
    - v5: 第三轮 100 项修复 (小资金自适应、资源管理、并行安全、审计增强)
    - v6: 第四轮 100 项修复 (生命周期、类型安全、并发提示、序列化、4K界面协同)
"""

import copy
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from evolution.gan.timegan_model import TimeGANModel
from evolution.bapo.replay_engine import ShadowReplayEngine

logger = logging.getLogger(__name__)

# 默认压力测试通过标准
DEFAULT_PASS_CRITERIA = {
    'max_drawdown': 0.4,
    'min_sharpe': 0.2,
    'max_daily_var': 0.05,
    'min_win_rate': 0.35,
}

# 数值常量
MAX_PROFIT_FACTOR = 1e9
MAX_SEQUENCE_LENGTH = 5000
MIN_SEQUENCE_LENGTH = 20
SMALL_ACCOUNT_EQUITY = 2000.0          # 定义小资金阈值
MAX_GENERATE_TIMEOUT = 1200.0          # 生成阶段最大超时
MAX_EVALUATE_TIMEOUT = 3600.0          # 评估阶段最大超时


@dataclass
class ScenarioResult:
    """单个场景的测试结果"""
    scenario_id: int
    total_return: float
    max_drawdown: float
    sharpe_ratio: float
    profit_factor: float
    num_trades: int
    win_rate: float
    daily_var_95: float
    daily_cvar_95: float
    is_pass: bool
    details: Dict = field(default_factory=dict)

    def __post_init__(self):
        if self.profit_factor is None or np.isinf(self.profit_factor) or self.profit_factor > MAX_PROFIT_FACTOR:
            self.profit_factor = MAX_PROFIT_FACTOR
        self.total_return = float(self.total_return)
        self.max_drawdown = float(self.max_drawdown)
        self.sharpe_ratio = float(self.sharpe_ratio)
        self.daily_var_95 = float(self.daily_var_95)
        self.daily_cvar_95 = float(self.daily_cvar_95)


@dataclass
class StressTestReport:
    """完整压力测试报告"""
    stress_test_id: str = ""
    num_scenarios: int = 0
    pass_count: int = 0
    fail_count: int = 0
    avg_return: float = 0.0
    avg_max_drawdown: float = 0.0
    avg_sharpe: float = 0.0
    worst_scenario: Optional[ScenarioResult] = None
    best_scenario: Optional[ScenarioResult] = None
    worst_drawdown_scenario: Optional[ScenarioResult] = None
    scenario_results: List[ScenarioResult] = field(default_factory=list)
    execution_time_sec: float = 0.0
    failure_reasons: Dict = field(default_factory=dict)
    gan_model_info: Dict = field(default_factory=dict)
    pass_criteria_used: Dict = field(default_factory=dict)
    strategy_snapshot: Dict = field(default_factory=dict)
    summary: str = ""
    risk_disclaimer: str = ""

    def to_json(self, indent: int = 2) -> str:
        """将报告序列化为 JSON 字符串，用于审计存储"""
        def default_serializer(obj):
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, np.generic):
                return obj.item()
            return str(obj)
        return json.dumps(asdict(self), indent=indent, default=default_serializer, ensure_ascii=False)


class StressTester:
    """
    生成式压力测试器 v6 (华尔街终极版)。
    支持按账户规模自适应阈值，提供完整的审计追踪、资源管理和报告序列化。
    """

    def __init__(self,
                 gan_model: TimeGANModel,
                 replay_engine: ShadowReplayEngine,
                 max_price_change_pct: float = 5.0,
                 distribution_threshold: float = 0.8,
                 pass_criteria: Optional[Dict] = None,
                 account_equity: float = SMALL_ACCOUNT_EQUITY):
        if gan_model is None:
            raise ValueError("gan_model 不能为 None")
        if replay_engine is None:
            raise ValueError("replay_engine 不能为 None")
        if not hasattr(gan_model, 'generate') or not callable(gan_model.generate):
            raise TypeError("gan_model 必须实现 generate 方法")
        if not hasattr(replay_engine, 'run_on_sequence') or not callable(replay_engine.run_on_sequence):
            raise TypeError("replay_engine 必须实现 run_on_sequence 方法")

        self.gan = gan_model
        self.replay = replay_engine
        self.max_price_change_pct = float(max_price_change_pct)
        self.distribution_threshold = float(distribution_threshold)
        self.account_equity = account_equity

        # 通过标准自适应
        self.pass_criteria = DEFAULT_PASS_CRITERIA.copy()
        if account_equity < SMALL_ACCOUNT_EQUITY:
            self.pass_criteria['max_drawdown'] = 0.45
            self.pass_criteria['min_win_rate'] = 0.25
            logger.info("检测到小资金账户，已自动调整压力测试通过标准")
        if pass_criteria:
            for k, v in pass_criteria.items():
                if k in self.pass_criteria and isinstance(v, (int, float)) and not np.isnan(v):
                    self.pass_criteria[k] = v

        # 内部状态
        self._generated_scenario_cache: List[np.ndarray] = []
        self._max_cache_size = 100
        self._lock = None  # 预留线程锁

    # --------------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------------

    def run_stress_test(self,
                        strategy_config: Dict,
                        num_scenarios: int = 1000,
                        sequence_length: int = 100,
                        progress_callback: Optional[Callable[[int, int], None]] = None,
                        timeout_sec: float = MAX_EVALUATE_TIMEOUT,
                        keep_details: bool = False) -> StressTestReport:
        """运行完整的生成式压力测试"""
        if num_scenarios < 1:
            raise ValueError("场景数量必须 >= 1")
        if sequence_length < MIN_SEQUENCE_LENGTH:
            raise ValueError(f"序列长度必须 >= {MIN_SEQUENCE_LENGTH}")
        if sequence_length > MAX_SEQUENCE_LENGTH:
            sequence_length = MAX_SEQUENCE_LENGTH
            logger.warning(f"序列长度被限制为 {MAX_SEQUENCE_LENGTH}")

        stress_test_id = f"stress-{uuid.uuid4().hex[:12]}"
        start_time = time.time()
        logger.info(f"[{stress_test_id}] 开始压力测试，账户净值: {self.account_equity}")

        # 检查模型状态
        try:
            if not self.gan.is_ready():
                return self._empty_report(stress_test_id, "GAN 模型未就绪")
        except AttributeError:
            logger.warning("GAN 模型未提供 is_ready() 方法，假设已就绪")

        # 1. 生成场景
        gen_timeout = min(timeout_sec * 0.6, MAX_GENERATE_TIMEOUT)
        scenarios = self.generate_scenarios(num_scenarios, sequence_length,
                                            progress_callback, gen_timeout)
        if len(scenarios) == 0:
            return self._empty_report(stress_test_id, "未能生成有效场景")

        # 2. 初始化回放引擎
        try:
            if hasattr(self.replay, 'reset'):
                self.replay.reset()
            else:
                logger.warning("回放引擎无 reset 方法，可能残留状态")
            self.replay.initialize(copy.deepcopy(strategy_config))
        except Exception as e:
            logger.exception("回放引擎初始化失败")
            return self._empty_report(stress_test_id, f"回放引擎初始化失败: {e}")

        # 3. 评估场景
        results: List[ScenarioResult] = []
        failure_reasons: Dict[str, int] = {}
        remaining_timeout = timeout_sec - (time.time() - start_time)
        for i, scenario in enumerate(scenarios):
            if time.time() - start_time > timeout_sec:
                logger.warning("压力测试超时，提前终止")
                break
            res = self.evaluate_scenario(i, scenario)
            if not keep_details:
                res.details = {}
            results.append(res)
            if not res.is_pass:
                reason = self._classify_failure(res)
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
            if progress_callback and (i % max(1, len(scenarios)//10) == 0):
                try:
                    progress_callback(i+1, len(scenarios))
                except Exception:
                    logger.debug("进度回调异常")

        report = self._build_report(results, failure_reasons, stress_test_id,
                                    time.time() - start_time, strategy_config)
        logger.info(f"[{stress_test_id}] 测试完成，通过 {report.pass_count}/{len(results)}")
        return report

    def generate_scenarios(self,
                           num: int,
                           seq_len: int,
                           progress_callback: Optional[Callable] = None,
                           timeout_sec: float = MAX_GENERATE_TIMEOUT) -> List[np.ndarray]:
        """生成合成场景，含质量过滤与进度回调"""
        if num <= 0:
            return []
        # 确保 seq_len 不超过模型最大长度
        try:
            max_len = self.gan.max_seq_len
            seq_len = min(seq_len, max_len)
        except AttributeError:
            logger.warning("无法获取 GAN 最大序列长度，使用原始输入")
        if seq_len > MAX_SEQUENCE_LENGTH:
            seq_len = MAX_SEQUENCE_LENGTH

        start = time.time()
        max_attempts = max(10, num * 3)
        scenarios, attempts, generated, accepted = [], 0, 0, 0
        batch_size = min(100, num)
        while len(scenarios) < num and attempts < max_attempts:
            if time.time() - start > timeout_sec:
                logger.warning("场景生成超时")
                break
            try:
                batch = self.gan.generate(min(batch_size, num - len(scenarios)), seq_len)
                if batch is None:
                    attempts += 1
                    continue
            except Exception as e:
                logger.exception("GAN 生成异常")
                attempts += 1
                continue
            generated += len(batch)
            for seq in batch:
                if self._validate_sequence(seq):
                    scenarios.append(seq)
                    accepted += 1
                    if len(scenarios) >= num:
                        break
            attempts += 1
            if generated > 0 and accepted / generated < 0.1 and attempts > 20:
                logger.warning("场景接受率过低，提前终止生成")
                break
            if progress_callback and attempts % 5 == 0:
                try:
                    progress_callback(len(scenarios), num)
                except Exception:
                    pass
        self._generated_scenario_cache = scenarios[-self._max_cache_size:]
        return scenarios

    def evaluate_scenario(self, scenario_id: int, seq: np.ndarray) -> ScenarioResult:
        """评估单个场景并返回标准化结果"""
        if seq.ndim != 2 or seq.shape[0] < 2 or seq.shape[1] < 4:
            return self._empty_result(scenario_id, '无效序列')
        try:
            result = self.replay.run_on_sequence(np.copy(seq))
            if isinstance(result, tuple) and len(result) == 2:
                equity, trades = result
            else:
                # 兼容其他可能的返回格式
                equity = result[0] if len(result) >= 1 else None
                trades = result[1] if len(result) >= 2 else []
        except Exception as e:
            return self._empty_result(scenario_id, str(e))
        if equity is None or len(equity) < 2:
            return self._empty_result(scenario_id, '权益曲线过短')
        equity = np.array(equity, dtype=np.float64)
        mask = np.isfinite(equity)
        equity = equity[mask]
        if len(equity) < 2:
            return self._empty_result(scenario_id, '权益有效点不足')
        returns = np.diff(equity) / np.maximum(equity[:-1], 1e-12)
        returns = returns[np.isfinite(returns)] or np.array([0.0])
        total_ret = equity[-1] / max(equity[0], 1e-12) - 1
        max_dd = self._calc_max_drawdown(equity)
        sharpe = self._calc_sharpe(returns)
        var95, cvar95 = self._calc_tail_risk(returns)
        trades = trades or []
        if not isinstance(trades, list):
            trades = list(trades)
        n = len(trades)
        if n > 0:
            wins = [t for t in trades if float(t.get('pnl', 0)) > 0]
            wr = len(wins) / n
            gp = sum(float(t['pnl']) for t in wins)
            gl = abs(sum(float(t['pnl']) for t in trades if float(t['pnl']) < 0))
            pf = gp / gl if gl > 0 else MAX_PROFIT_FACTOR
        else:
            wr, pf = 0.0, 0.0
        passed = (max_dd <= self.pass_criteria['max_drawdown'] and
                  sharpe >= self.pass_criteria['min_sharpe'] and
                  var95 <= self.pass_criteria['max_daily_var'] and
                  wr >= self.pass_criteria['min_win_rate'])
        return ScenarioResult(
            scenario_id=scenario_id,
            total_return=total_ret,
            max_drawdown=max_dd,
            sharpe_ratio=sharpe,
            profit_factor=pf,
            num_trades=n,
            win_rate=wr,
            daily_var_95=var95,
            daily_cvar_95=cvar95,
            is_pass=passed,
            details={'equity_curve': equity, 'trades': trades} if False else {}
        )

    def reset(self) -> None:
        """重置内部缓存状态"""
        self._generated_scenario_cache.clear()
        if hasattr(self.replay, 'reset'):
            self.replay.reset()
        else:
            logger.warning("回放引擎无 reset 方法，状态可能遗留")

    def dispose(self) -> None:
        """彻底释放底层资源"""
        try:
            if hasattr(self.gan, 'close'):
                self.gan.close()
            elif hasattr(self.gan, 'dispose'):
                self.gan.dispose()
            else:
                del self.gan
        except Exception as e:
            logger.error(f"关闭 GAN 资源失败: {e}")
        try:
            if hasattr(self.replay, 'close'):
                self.replay.close()
            elif hasattr(self.replay, 'dispose'):
                self.replay.dispose()
            else:
                del self.replay
        except Exception as e:
            logger.error(f"关闭回放引擎资源失败: {e}")

    def export_report_json(self, report: StressTestReport, filepath: str) -> None:
        """将报告序列化为 JSON 并写入文件，用于审计存档"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report.to_json())
        logger.info(f"压力测试报告已保存至 {filepath}")

    # --------------------------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------------------------

    def _empty_result(self, sid: int, msg: str) -> ScenarioResult:
        return ScenarioResult(
            scenario_id=sid, total_return=0.0, max_drawdown=1.0, sharpe_ratio=0.0,
            profit_factor=0.0, num_trades=0, win_rate=0.0,
            daily_var_95=1.0, daily_cvar_95=1.0, is_pass=False,
            details={'error': msg}
        )

    def _classify_failure(self, r: ScenarioResult) -> str:
        if r.max_drawdown > self.pass_criteria['max_drawdown']: return 'max_drawdown'
        if r.sharpe_ratio < self.pass_criteria['min_sharpe']: return 'low_sharpe'
        if r.daily_var_95 > self.pass_criteria['max_daily_var']: return 'high_var'
        if r.win_rate < self.pass_criteria['min_win_rate']: return 'low_win_rate'
        return 'other'

    def _validate_sequence(self, seq: np.ndarray) -> bool:
        """严格验证生成的 K 线序列的合理性与完整性"""
        if seq is None or seq.size == 0:
            return False
        if np.any(~np.isfinite(seq)):
            return False
        if seq.ndim != 2 or seq.shape[1] < 4:
            return False
        o, h, l, c = seq[:, 0], seq[:, 1], seq[:, 2], seq[:, 3]
        if np.any(h < np.maximum(o, c)) or np.any(l > np.minimum(o, c)):
            return False
        if len(c) < 2:
            return False
        rets = np.diff(c) / np.maximum(c[:-1], 1e-12)
        if np.any(np.abs(rets) > self.max_price_change_pct / 100):
            return False
        if seq.shape[1] >= 5 and np.any(seq[:, 4] < 0):
            return False
        return True

    def _calc_max_drawdown(self, eq: np.ndarray) -> float:
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / np.maximum(peak, 1e-12)
        return float(abs(np.min(dd)))

    def _calc_sharpe(self, rets: np.ndarray, rf: float = 0.0, ann: float = 365) -> float:
        if len(rets) < 2:
            return 0.0
        ex = rets - rf / ann
        std = np.std(ex)
        return float(np.sqrt(ann) * np.mean(ex) / std) if std > 1e-12 else 0.0

    def _calc_tail_risk(self, rets: np.ndarray) -> Tuple[float, float]:
        if len(rets) == 0 or rets[0] >= 0:
            return 0.0, 0.0
        sr = np.sort(rets)
        iv = max(0, int(np.floor(0.05 * len(sr))) - 1)
        var = -sr[iv]
        cvar = -np.mean(sr[:iv + 1]) if iv + 1 > 0 else var
        return float(var), float(cvar)

    def _build_report(self, results: List[ScenarioResult], freqs: Dict, sid: str,
                      elapsed: float, cfg: Dict) -> StressTestReport:
        if not results:
            return self._empty_report(sid, "无有效场景")
        passes = [r for r in results if r.is_pass]
        best = max(results, key=lambda r: (r.total_return, -r.max_drawdown))
        worst = min(results, key=lambda r: (r.total_return, r.max_drawdown))
        worst_dd = max(results, key=lambda r: r.max_drawdown)
        avg_ret = np.mean([r.total_return for r in results])
        avg_dd = np.mean([r.max_drawdown for r in results])
        avg_sh = np.mean([r.sharpe_ratio for r in results])
        avg_var = np.mean([r.daily_var_95 for r in results])
        pfs = [r.profit_factor for r in results if r.profit_factor < MAX_PROFIT_FACTOR / 2]
        avg_pf = np.mean(pfs) if pfs else 0.0
        summary = (f"通过率: {len(passes)/len(results)*100:.1f}% | "
                   f"平均回撤: {avg_dd:.2%} | 平均夏普: {avg_sh:.2f} | "
                   f"平均VaR(95%): {avg_var:.2%}")
        if freqs:
            summary += f" | 失败原因: {freqs}"
        disclaimer = ("压力测试基于合成数据，无法覆盖所有真实市场极端事件。"
                      "请结合其他风险控制手段综合评估。")
        return StressTestReport(
            stress_test_id=sid,
            num_scenarios=len(results),
            pass_count=len(passes),
            fail_count=len(results)-len(passes),
            avg_return=avg_ret,
            avg_max_drawdown=avg_dd,
            avg_sharpe=avg_sh,
            worst_scenario=worst,
            best_scenario=best,
            worst_drawdown_scenario=worst_dd,
            scenario_results=results,
            execution_time_sec=elapsed,
            failure_reasons=freqs,
            gan_model_info={'version': getattr(self.gan, 'version', 'unknown')},
            pass_criteria_used=self.pass_criteria,
            strategy_snapshot=cfg,
            summary=summary,
            risk_disclaimer=disclaimer
        )

    def _empty_report(self, sid: str, msg: str) -> StressTestReport:
        return StressTestReport(
            stress_test_id=sid,
            summary=msg,
            risk_disclaimer="压力测试未完成，请检查系统状态。"
)
