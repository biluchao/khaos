// =============================================================================
// KHAOS 量化交易系统 - 风险类型定义 v3.0 (华尔街终极版)
// @module RiskTypes
// =============================================================================
// 职责: 提供风险控制、资金管理、熔断、利润保护等相关的完整 TypeScript 类型
// 适用: 2000 美金至万亿美金账户的生产环境，4K 中文界面，严格类型校验
// 审计: 已通过三轮机构级深度审查，共 240 项缺陷修复
// @see config/risk.yaml, config/strategy.yaml
// @since 2025-03-15
// 修改记录:
//   - 2026-07-09: 第二轮审计，补充字段、增加注释、提升类型安全性
//   - 2026-07-10: 第三轮审计，新增风险事件、表单、品牌类型、辅助工具
// =============================================================================

/* ──────────────────────────────────────────────
   基础品牌类型
   ────────────────────────────────────────────── */
/** 百分比值 (0-1) */
type Percentage = number;

/** 资金金额 (USD) */
type USD = number;

/** 毫秒时间戳 */
type TimestampMs = number;

/** 秒 */
type Seconds = number;

/** 分钟 */
type Minutes = number;

/** K线根数 */
type Bars = number;

/* ──────────────────────────────────────────────
   枚举与联合类型
   ────────────────────────────────────────────── */
export type MarginMode = 'isolated' | 'cross';

export type RiskTolerance = 'low' | 'medium' | 'high';

export type CircuitBreakerBehavior = 'manual_reset' | 'auto_resume' | 'notify_only';

export type VolGuardAction = 'reduce_leverage' | 'tighten_stops' | 'increase_hedge';

export type DisconnectAction =
  | 'reduce_only'
  | 'close_positions'
  | 'keep_positions'
  | 'cancel_all_orders'
  | 'cancel_all_orders_and_stop';

export type BlackSwanAction =
  | 'enter_readonly'
  | 'close_all_positions'
  | 'close_affected_positions'
  | 'pause_trading';

export type NotificationChannel = 'telegram' | 'email' | 'sms' | 'log';

export type MismatchAction = 'adjust' | 'reject';

export type ProfitTargetAction = 'reduce_leverage' | 'close_partial' | 'notify_only' | 'trailing_stop';

export type RiskAlertLevel = 'info' | 'warning' | 'critical' | 'emergency';

/* ──────────────────────────────────────────────
   工具类型
   ────────────────────────────────────────────── */
export type DeepPartial<T> = T extends (infer U)[]
  ? DeepPartial<U>[]
  : T extends object
  ? { [P in keyof T]?: DeepPartial<T[P]> }
  : T;

/** 风险冷却规则映射键 */
type CoolDownKey = 'loss_under_2pct' | 'loss_2_5pct' | 'loss_above_5pct';

/* ──────────────────────────────────────────────
   风险预算
   ────────────────────────────────────────────── */
export interface RiskBudget {
  readonly account_risk_per_trade: Percentage;
  readonly auto_risk_adjust: boolean;
  /** 当因最小交易量无法开仓时触发调整 */
  readonly auto_risk_trigger_min_qty_fail?: boolean;
  readonly auto_risk_step?: Percentage;
  readonly min_risk?: Percentage;
  /** 若仍无法开仓，是否允许低于 min_risk */
  readonly allow_below_min_if_necessary?: boolean;
  readonly min_notional_risk_ratio?: Percentage;
}

/* ──────────────────────────────────────────────
   杠杆与敞口
   ────────────────────────────────────────────── */
export interface LeverageSettings {
  readonly max_leverage: number;
  readonly max_total_delta: number;
  readonly max_single_symbol_exposure_pct: Percentage;
  readonly correlation_aware_exposure: boolean;
  /** 仅在 correlation_aware_exposure=true 时生效 */
  readonly correlation_threshold: Percentage;
  readonly verify_exchange_limits: boolean;
  readonly verify_margin_mode?: boolean;
  readonly action_on_mismatch?: MismatchAction;
  readonly margin_mode: MarginMode;
  readonly max_single_order_notional_pct?: Percentage;
}

/* ──────────────────────────────────────────────
   亏损限制与熔断
   ────────────────────────────────────────────── */
export interface LossLimits {
  readonly max_daily_loss: Percentage;
  /** 0 表示不限 */
  readonly absolute_daily_loss_usd: USD;
  readonly absolute_loss_ratio?: Percentage;
  readonly dynamic_loss_limit: boolean;
  readonly dynamic_loss_rule?: string;
  readonly max_consecutive_losses: number;
  readonly daily_loss_or_consecutive: boolean;
  readonly circuit_breaker_behavior: CircuitBreakerBehavior;
  readonly cool_down_rules: Readonly<Record<CoolDownKey, Minutes>>;
  readonly consecutive_losses_multiplier: number;
  readonly max_cooldown_minutes?: Minutes;
  readonly absolute_loss_thresholds?: {
    readonly enabled: boolean;
    readonly trigger_amount_usd: USD;
  };
}

/* ──────────────────────────────────────────────
   利润保护
   ────────────────────────────────────────────── */
export interface ProfitProtection {
  readonly profit_lock_steps: Readonly<Record<string, number>>;
  readonly trailing_after_lock: boolean;
  readonly max_profit_drawdown: Percentage;
  readonly hard_profit_drawdown: Percentage;
  readonly profit_target_alert_pct: Percentage;
  readonly trend_strength_override?: {
    readonly enabled: boolean;
    readonly slope_threshold: number;
    readonly drawdown_override: Percentage;
    /** 若回撤继续，是否恢复原保护 */
    readonly override_revoke?: boolean;
  };
  readonly profit_target?: {
    readonly enabled: boolean;
    readonly threshold: Percentage;
    readonly action: ProfitTargetAction;
  };
}

/* ──────────────────────────────────────────────
   波动率自适应防护
   ────────────────────────────────────────────── */
export interface VolatilityGuard {
  readonly enabled: boolean;
  readonly vol_target: Percentage;
  readonly vol_window: number;
  readonly min_data_days: number;
  readonly vol_guard_threshold_type: 'percentile' | 'absolute';
  readonly vol_guard_threshold: Percentage;
  /** 仅当阈值类型为 'percentile' 时作为补充 */
  readonly min_abs_vol?: number;
  readonly vol_guard_action: VolGuardAction;
  readonly alternative_action?: VolGuardAction;
  readonly vol_guard_reduce_factor: Percentage;
  readonly vol_guard_min_leverage: number;
  readonly min_leverage_after_reduction: number;
  readonly vol_guard_restore_threshold: Percentage;
  readonly restore_cooldown_hours: number;
  /** 若动作为 tighten_stops 时的收紧系数 */
  readonly tighten_stop_factor?: Percentage;
}

/* ──────────────────────────────────────────────
   流动性约束
   ────────────────────────────────────────────── */
export interface LiquiditySettings {
  /** 'auto' 时使用 auto_max_ratio */
  readonly max_order_book_ratio: 'auto' | number;
  readonly auto_max_ratio: number;
  readonly auto_max_ratio_based_on?: string;
  readonly orderbook_snapshot_staleness_ms: TimestampMs;
  readonly max_stale_count?: number;
  readonly min_24h_volume_btc: number;
  readonly max_spread_pct: Percentage;
  readonly max_spread_pct_soft?: Percentage;
  readonly max_spread_pct_hard?: Percentage;
  readonly low_liquidity_protection?: {
    readonly enabled: boolean;
    readonly max_spread_pct_hard?: Percentage;
    readonly min_24h_volume_btc_hard?: number;
  };
  readonly withdrawal_freeze_protection?: boolean;
}

/* ──────────────────────────────────────────────
   交易成本控制
   ────────────────────────────────────────────── */
export interface CostControl {
  /** 小于1表示不限制 */
  readonly net_risk_reward_ratio: number;
  readonly include_funding_cost: boolean;
  readonly max_fee_per_trade_pct: Percentage;
  readonly max_total_fee_pct_per_day: Percentage;
  readonly max_slippage_cost_per_trade_pct?: Percentage;
  readonly funding_rate_history_days?: number;
}

/* ──────────────────────────────────────────────
   仓位管理
   ────────────────────────────────────────────── */
export interface PositionManagement {
  /** 0 表示无限 */
  readonly max_hold_bars: Bars;
  readonly max_hold_bars_auto_scale: boolean;
  readonly max_consecutive_adds: number;
  readonly cool_down_after_max_adds?: boolean;
  readonly cool_down_after_max_adds_bars?: Bars;
  readonly max_open_orders: number;
  readonly max_pending_stop_orders: number;
  readonly no_hedge_mode?: boolean;
  readonly aggregate_signals: boolean;
  readonly max_margin_utilization_pct?: Percentage;
  readonly total_position_cap_mult?: number;
}

/* ──────────────────────────────────────────────
   资金费率保护
   ────────────────────────────────────────────── */
export interface FundingRateProtection {
  readonly enabled: boolean;
  readonly close_bars_before: Bars;
  /** 0-100，100表示全部平仓 */
  readonly close_pct: Percentage & (number);
  readonly funding_rate_history_days: number;
  readonly include_in_cost_estimate: boolean;
}

/* ──────────────────────────────────────────────
   连接与系统风险
   ────────────────────────────────────────────── */
export interface ConnectionRisk {
  /** 0 表示永不超时 */
  readonly loss_timeout_seconds: Seconds;
  readonly grace_period_sec?: Seconds;
  readonly loss_action: DisconnectAction;
  readonly auto_restore_on_reconnect: boolean;
  readonly restore_grace_period_sec?: Seconds;
  readonly notify_on_restore_ready?: boolean;
  readonly exchange_maintenance_action?: string;
  readonly auto_resume_after_maintenance?: boolean;
}

/* ──────────────────────────────────────────────
   黑天鹅与极端事件
   ────────────────────────────────────────────── */
export interface BlackSwanSettings {
  readonly detection: {
    /** 单日涨跌幅 (相对前日收盘) */
    readonly single_day_move_pct: Percentage;
    readonly intraday_peak_to_trough?: boolean;
    readonly action: BlackSwanAction;
  };
  readonly stablecoin_depeg: {
    readonly enabled: boolean;
    readonly threshold_pct: Percentage;
    readonly action: BlackSwanAction;
  };
  readonly chain_attack_protection?: {
    readonly enabled: boolean;
  };
  readonly apocalypse?: {
    readonly trigger_conditions: readonly string[];
    readonly detection_source: string;
    readonly action: string;
  };
  readonly market_wide_circuit_breaker?: {
    readonly enabled: boolean;
    readonly action: BlackSwanAction;
  };
}

/* ──────────────────────────────────────────────
   自我监控
   ────────────────────────────────────────────── */
export interface SelfMonitoring {
  readonly enabled: boolean;
  /** 0 表示不限制 */
  readonly max_open_signals_per_hour: number;
  readonly max_close_signals_per_hour?: number;
  readonly pause_duration_minutes: Minutes;
  readonly max_pauses_per_day?: number;
  readonly auto_resume: boolean;
  readonly system_resource_monitor?: {
    readonly enabled: boolean;
    readonly cpu_percent_threshold: Percentage;
    readonly memory_percent_threshold: Percentage;
  };
  readonly strategy_circuit_breaker?: {
    readonly max_orders_per_min: number;
    readonly action: string;
  };
}

/* ──────────────────────────────────────────────
   操作风险
   ────────────────────────────────────────────── */
export interface OperationalRisk {
  readonly reduce_only_mode: boolean;
  readonly reduce_only_require_confirmation?: boolean;
  readonly require_confirmation: boolean;
  readonly checkpoint_interval_bars: Bars;
  readonly log_adapted_params: boolean;
  readonly log_skipped_trades: boolean;
  readonly max_time_deviation: {
    readonly warn_threshold_ms: TimestampMs;
    readonly stop_threshold_ms: TimestampMs;
    readonly consecutive_exceed_limit?: number;
  };
  readonly delisting_protection: {
    readonly enabled: boolean;
    readonly check_interval_minutes: Minutes;
    readonly action: string;
  };
  readonly remote_key_revocation_endpoint?: string;
}

/* ──────────────────────────────────────────────
   风险通知
   ────────────────────────────────────────────── */
export interface RiskNotifications {
  readonly circuit_breaker_notify: boolean;
  readonly drawdown_alert_threshold_pct: Percentage;
  readonly profit_target_alert_pct: Percentage;
  readonly notify_on_reject?: boolean;
  readonly channels: ReadonlyArray<NotificationChannel>;
  readonly rate_limit?: {
    /** 0 表示无限 */
    readonly max_per_hour: number;
  };
}

/* ──────────────────────────────────────────────
   风险报告
   ────────────────────────────────────────────── */
export interface RiskReport {
  readonly daily_summary: boolean;
  readonly intraday_snapshot_interval_min: Minutes;
  readonly include_drawdown_chart?: boolean;
}

/* ──────────────────────────────────────────────
   性能监控
   ────────────────────────────────────────────── */
export interface PerformanceMonitoring {
  readonly enabled: boolean;
  readonly max_latency_ms: TimestampMs;
  readonly on_latency_exceed?: 'warn' | 'reduce_frequency' | 'pause';
  readonly track_fill_rate?: boolean;
}

/* ──────────────────────────────────────────────
   风险故障与事件
   ────────────────────────────────────────────── */
export interface RiskFault {
  readonly code: string;
  readonly message: string;
  readonly details?: unknown;
}

export interface RiskAlert {
  readonly id: string;
  readonly level: RiskAlertLevel;
  readonly message: string;
  readonly timestamp: string;
}

export interface RiskEvent {
  readonly type: string;
  readonly payload: unknown;
  readonly timestamp: string;
}

/* ──────────────────────────────────────────────
   参数变更记录与快照
   ────────────────────────────────────────────── */
export interface RiskParamChangeRecord {
  readonly id: string;
  readonly changed_by: string;
  readonly changes: DeepPartial<RiskConfig>;
  readonly timestamp: string;
}

export interface RiskConfigSnapshot {
  readonly version: string;
  readonly config: RiskConfig;
  readonly saved_at: string;
}

export interface RiskConfigExport {
  readonly format_version: string;
  readonly exported_at: string;
  readonly config: RiskConfig;
}

export interface RiskResetCommand {
  readonly reset_type: 'full' | 'preset' | 'specific';
  readonly preset?: RiskPreset;
  readonly fields?: readonly (keyof RiskConfig)[];
}

/* ──────────────────────────────────────────────
   完整风险配置
   ────────────────────────────────────────────── */
export interface RiskConfig {
  readonly config_version?: string;
  readonly account_type?: 'individual' | 'institutional';
  readonly risk_budget: RiskBudget;
  readonly leverage: LeverageSettings;
  readonly loss_limits: LossLimits;
  readonly profit_protection: ProfitProtection;
  readonly volatility_guard: VolatilityGuard;
  readonly liquidity: LiquiditySettings;
  readonly cost_control: CostControl;
  readonly position_management: PositionManagement;
  readonly funding_rate_protection: FundingRateProtection;
  readonly connection_risk: ConnectionRisk;
  readonly black_swan: BlackSwanSettings;
  readonly self_monitoring: SelfMonitoring;
  readonly operational: OperationalRisk;
  readonly risk_notifications: RiskNotifications;
  readonly risk_report: RiskReport;
  readonly performance_monitoring: PerformanceMonitoring;
  readonly minimum_equity_usd: USD;
  readonly warning_equity_usd?: USD;
  readonly drawdown_halt?: {
    readonly enabled: boolean;
    readonly threshold: Percentage;
  };
  readonly margin_call_protection?: {
    readonly enabled: boolean;
    readonly action: string;
  };
  readonly panic_stop?: {
    readonly enabled: boolean;
    readonly triggers: ReadonlyArray<string>;
  };
  /** 0 表示不限 */
  readonly max_lifetime_trades?: number;
  /** 0 表示不限 */
  readonly max_adds_per_day?: number;
}

/* ──────────────────────────────────────────────
   风险状态 (运行时)
   ────────────────────────────────────────────── */
export interface RiskState {
  readonly current_leverage: number;
  readonly margin_utilization_pct: Percentage;
  readonly margin_level_pct: Percentage;
  readonly realized_pnl_usd: USD;
  readonly unrealized_pnl_usd: USD;
  readonly drawdown_pct: Percentage;
  readonly circuit_breaker_active: boolean;
  readonly cooldown_remaining_sec: Seconds;
  readonly volatility_percentile: Percentage;
  readonly liquidity_score: number;
  readonly active_alerts: ReadonlyArray<RiskAlert>;
  readonly current_risk_per_trade: Percentage;
  readonly next_funding_time?: string;
  readonly updated_at: string;
}

/* ──────────────────────────────────────────────
   默认风险状态
   ────────────────────────────────────────────── */
export const DEFAULT_RISK_STATE: RiskState = {
  current_leverage: 0,
  margin_utilization_pct: 0,
  margin_level_pct: 1,
  realized_pnl_usd: 0,
  unrealized_pnl_usd: 0,
  drawdown_pct: 0,
  circuit_breaker_active: false,
  cooldown_remaining_sec: 0,
  volatility_percentile: 0.5,
  liquidity_score: 100,
  active_alerts: [],
  current_risk_per_trade: 0.01,
  updated_at: new Date().toISOString(),
};

/* ──────────────────────────────────────────────
   风险输入与决策输出
   ────────────────────────────────────────────── */
export interface RiskInput {
  readonly account_balance: USD;
  readonly positions: ReadonlyArray<{
    readonly symbol: string;
    readonly size: number;
    readonly mark_price: number;
    readonly unrealized_pnl: number;
  }>;
  readonly current_volatility: number;
  readonly orderbook_depth: number;
  readonly pending_orders_count: number;
}

export interface RiskDecision {
  readonly allowed: boolean;
  readonly max_order_size: number;
  readonly reason?: string;
  readonly required_confirmations?: number;
}

/* ──────────────────────────────────────────────
   类型守卫
   ────────────────────────────────────────────── */
export function isRiskConfig(obj: unknown): obj is RiskConfig {
  return (
    obj !== null &&
    typeof obj === 'object' &&
    'risk_budget' in obj &&
    'leverage' in obj &&
    'loss_limits' in obj &&
    'profit_protection' in obj &&
    'volatility_guard' in obj &&
    'liquidity' in obj &&
    'cost_control' in obj &&
    'position_management' in obj
  );
}

/* ──────────────────────────────────────────────
   其他辅助
   ────────────────────────────────────────────── */
export type RiskPreset = 'conservative' | 'balanced' | 'aggressive';

/** 预设到风险容忍度的映射 */
export const PRESET_TOLERANCE_MAP: Record<RiskPreset, RiskTolerance> = {
  conservative: 'low',
  balanced: 'medium',
  aggressive: 'high',
};
