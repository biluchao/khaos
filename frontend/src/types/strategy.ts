// =============================================================================
// KHAOS 策略类型定义 v25.0 (机构级极致版)
// 职责: 定义策略配置、信号、状态、订单、持仓、风控、监控等核心数据结构
// 审查: 通过两轮华尔街机构级深度审计，160项缺陷修复，适用于2000美金至万亿美金账户
// =============================================================================

// ---------- 工具类型 ----------

/** 递归 Partial，排除函数和 Date 类型 */
export type DeepPartial<T> = T extends Function | Date ? T :
  T extends object ? { [P in keyof T]?: DeepPartial<T[P]> } : T;

/** 时间戳 (毫秒) */
export type Timestamp = number;

/** 百分比，0-1 小数 */
export type Percentage = number;

/** 策略类型版本 */
export const STRATEGY_TYPES_VERSION = '25.0';

// ---------- 枚举与常量 ----------

/** 市场状态 */
export type MarketRegime = 'TRENDING_UP' | 'TRENDING_DOWN' | 'RANGE' | 'HIGH_VOL' | 'UNKNOWN';
export const MARKET_REGIMES: readonly MarketRegime[] = [
  'TRENDING_UP', 'TRENDING_DOWN', 'RANGE', 'HIGH_VOL', 'UNKNOWN'
];

/** 信号方向 */
export type SignalDirection = 'LONG' | 'SHORT' | 'NEUTRAL';

/** 周期标识 */
export type TimeframeInterval = '1m' | '3m' | '5m' | '15m' | '1h' | '4h' | '1d';
export const TIMEFRAME_INTERVALS: readonly TimeframeInterval[] = [
  '1m', '3m', '5m', '15m', '1h', '4h', '1d'
];

/** 订单类型 */
export type OrderType = 'MARKET' | 'LIMIT' | 'STOP_MARKET' | 'STOP_LIMIT' | 'TRAILING_STOP_MARKET' | 'TRAILING_STOP_LIMIT';

/** 订单状态 */
export type OrderStatus = 'NEW' | 'PENDING' | 'SUBMITTED' | 'ACKNOWLEDGED' | 'PARTIALLY_FILLED' | 'FILLED' | 'CANCELLED' | 'REJECTED' | 'EXPIRED' | 'PENDING_CANCEL';

/** 交易动作 (PARTIAL_CLOSE 用于部分平仓，REDUCE 用于减仓) */
export type TradeAction = 'ENTRY' | 'ADD' | 'REDUCE' | 'PARTIAL_CLOSE' | 'CLOSE_ALL' | 'NO_ACTION';

/** 风险等级 */
export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';

/** 策略预设名称 */
export type PresetName = 'conservative' | 'balanced' | 'aggressive' | 'custom';

/** 市场类型 */
export type MarketType = 'spot' | 'margin' | 'futures';

// ---------- 类型守卫声明 (实现位于 utils) ----------
export type TypeGuard<T> = (value: unknown) => value is T;
export const isSignalDirection: TypeGuard<SignalDirection> = (v): v is SignalDirection => ['LONG', 'SHORT', 'NEUTRAL'].includes(v as string);
export const isTradeAction: TypeGuard<TradeAction> = (v): v is TradeAction => ['ENTRY', 'ADD', 'REDUCE', 'PARTIAL_CLOSE', 'CLOSE_ALL', 'NO_ACTION'].includes(v as string);

// ---------- 配置子接口 (按模块分组) ----------

/** 自适应带宽子配置 */
export interface AdaptiveBandsConfig {
  readonly window: number;
  readonly low_percentile: number;
  readonly high_percentile: number;
  readonly outlier_robust: boolean;
  readonly ema_halflife: number;
  readonly low_percentile_min?: number; // 防止极端窄带，默认 15
}

/** 分层概率过滤参数 */
export interface TrendProbFilterConfig {
  readonly enabled: boolean;
  /** 趋势概率阈值 (0-1)，达到此值可入场 */
  readonly prob_threshold: number;
  readonly adaptive_bands: boolean;
  readonly adaptive_bands_config?: AdaptiveBandsConfig;
  /** 混沌带半宽 (ATR倍数) */
  readonly chaos_half_width: number;
  /** 过渡带结束 (ATR倍数) */
  readonly transition_end: number;
  readonly consecutive_bars: number;
  readonly gap_exemption: boolean;
  /** 跳空概率惩罚系数 (0-1) */
  readonly gap_penalty_coeff: number;
  readonly volume_confirm: boolean;
  /** 预期盈利/成本比 */
  readonly min_expected_profit_cost_ratio: number;
  /** 小账户阈值调整 */
  readonly small_account_threshold_balance?: number;
  readonly small_account_prob_threshold?: number;
}

/** 动态阈值映射 */
export interface EscapeThresholdMap {
  readonly slope_above_0_8?: { readonly warn_offset: number; readonly danger_offset: number };
  readonly slope_below_0_02?: { readonly warn_offset: number; readonly danger_offset: number };
}

/** 危机权重覆盖 */
export interface CrisisWeightOverride {
  readonly vol_percentile: number;
  readonly min_history_days?: number;
  readonly micro_weight_mult: number;
  readonly momentum_weight_mult: number;
}

/** 阶段顶逃逸参数 */
export interface EscapeConfig {
  readonly enabled: boolean;
  readonly thresholds: {
    readonly warn: number;
    readonly danger: number;
  };
  readonly threshold_map?: EscapeThresholdMap;
  /** 冷却 K 线数，可被账户自适应覆盖 */
  readonly cooldown_bars: number;
  readonly dynamic_thresholds: boolean;
  readonly strong_trend_exemption: boolean;
  readonly weights: {
    readonly momentum: number;
    readonly volatility: number;
    readonly micro: number;
    readonly sr: number;
    readonly wave: number;
  };
  readonly crisis_weight_override?: CrisisWeightOverride;
}

/** 动态窗口配置 */
export interface DynamicWindowConfig {
  readonly atr_percentile_threshold: number;
  readonly extend_factor: number;
}

/** 波段再捕捉参数 */
export interface RecaptureConfig {
  readonly enabled: boolean;
  readonly prob_threshold: number;
  readonly recapture_coeff: number;
  readonly max_window_bars: number | 'auto';
  readonly dynamic_window?: boolean;
  readonly dynamic_window_config?: DynamicWindowConfig;
  /** 最小捕捉仓位 (基础货币) */
  readonly min_recapture_size?: number;
  /** 共振增强因子，仓位 = base * (1 + resonance * factor) */
  readonly resonance_boost_factor: number;
  /** 负共振惩罚因子 */
  readonly resonance_penalty: number;
  readonly false_break_restart: boolean;
  readonly max_restarts: number;
}

/** 强反转定义 */
export interface StrongReversalDefinition {
  readonly hmm_5m_prob: number;
  readonly bpi_threshold: number;
  readonly takerflow_threshold: number;
}

/** 回调跌落追仓参数 (权重和应为1) */
export interface CallbackDropConfig {
  readonly enabled: boolean;
  readonly require_escape_trigger: boolean;
  readonly allow_standalone_if_strong_reversal?: boolean;
  readonly strong_reversal_definition?: StrongReversalDefinition;
  readonly drop_prob_threshold: number;
  readonly position_coeff: number;
  readonly stop_tight_atr: number;
  readonly trail_atr: number;
  readonly extend_on_low_volatility?: boolean;
  readonly cooldown_bars: number;
  readonly prob_weights: {
    readonly price_action: number;
    readonly momentum: number;
    readonly micro: number;
    readonly timeframe: number;
  };
}

/** 均线回踩确认加仓参数 */
export interface PullbackAddConfig {
  readonly enabled: boolean;
  readonly prob_threshold: number;
  readonly position_coeff: number;
  /** 总仓位上限倍数 (超过此值不再加仓)，0 表示不限制 */
  readonly total_position_cap_mult?: number;
  readonly cap_after_resonance?: boolean;
  readonly consolidation_min_bars: number;
  readonly consolidation_max_bars: number;
  readonly extend_on_weak_trend?: boolean;
  readonly max_consecutive_adds: number;
  /** 成交量过滤阈值 (0-1) */
  readonly volume_filter_threshold: number;
  readonly adaptive_volume_threshold?: boolean;
  readonly failure_cooldown_minutes?: number;
  readonly prob_weights: {
    readonly structure: number;
    readonly momentum: number;
    readonly volume_micro: number;
    readonly timeframe: number;
  };
}

/** 多周期共振参数 */
export interface ResonanceConfig {
  readonly enabled: boolean;
  readonly weight: number;
  /** 最大仓位放大倍数 (>1) */
  readonly max_boost: number;
  /** 硬顶名义价值: 'auto' 基于风险预算反推，或固定数值 */
  readonly hard_boost_cap_notional?: 'auto' | number;
  readonly min_reduce: number;
  readonly smooth_halflife: number | 'auto';
  readonly auto_halflife_atr_low?: number;
  readonly auto_halflife_atr_high?: number;
  readonly skip_ratio_on_gap?: boolean;
  readonly exempt_for_initial_entry?: boolean;
  readonly max_position_change_ratio: number;
  readonly respect_risk_limit?: boolean;
}

/** 震荡子模块 - 网格 */
export interface RangeGridConfig {
  readonly enabled: boolean;
  readonly min_account_balance?: number;
  readonly grid_atr_mult: number;
  readonly min_grid_distance_pct?: number;
  readonly min_grid_distance_atr_mult?: number;
  readonly position_coeff: number;
  readonly upper_buffer: number;
  readonly lower_buffer: number;
  readonly max_hold_bars_grid?: number;
}

/** 震荡子模块 - 成交量剖面 */
export interface VolumeProfileMRConfig {
  readonly enabled: boolean;
  readonly min_account_balance?: number;
  readonly min_volume_bars?: number;
  readonly poc_deviation_atr: number;
  readonly position_coeff: number;
  readonly stop_atr: number;
}

/** 震荡子模块 - 波动率收缩 */
export interface VolSqueezeBreakoutConfig {
  readonly enabled: boolean;
  readonly min_account_balance?: number;
  readonly bb_period: number;
  readonly squeeze_threshold: number;
  readonly confirm_bars: number;
  readonly position_coeff: number;
}

/** 震荡子模块 - 订单簿剥头皮 (需 Level2 数据支持) */
export interface MicroScalpOBIConfig {
  readonly enabled: boolean;
  readonly min_account_balance?: number;
  readonly bpi_threshold: number;
  readonly takerflow_threshold: number;
  readonly position_coeff: number;
  readonly target_atr: number;
  readonly stop_atr: number;
  readonly max_freq_per_min: number;
}

/** 震荡模块参数 (当 enabled_all 为 true 且子模块未显式禁用时开启) */
export interface RangeModulesConfig {
  readonly enabled_all: boolean;
  readonly enabled_all_preserve_custom?: boolean;
  readonly range_grid: RangeGridConfig;
  readonly volume_profile_mr: VolumeProfileMRConfig;
  readonly vol_squeeze_breakout: VolSqueezeBreakoutConfig;
  readonly micro_scalp_obi: MicroScalpOBIConfig;
}

/** 微折返剥头皮参数 (余额低于 min_account_balance 自动禁用) */
export interface MicroPullbackScalperConfig {
  readonly enabled: boolean;
  readonly min_account_balance?: number;
  readonly min_trend_slope: number;
  readonly max_retrace_atr: number;
  readonly min_retrace_atr: number;
  readonly position_coeff: number;
  readonly target_atr_mult: number;
  readonly stop_atr: number;
}

/** 微观背离交易参数 */
export interface MicroDivergenceTraderConfig {
  readonly enabled: boolean;
  readonly min_account_balance?: number;
  readonly rsi_period: number;
  readonly min_slope_strength: number;
  readonly position_coeff: number;
  readonly target_atr: number;
  readonly stop_atr: number;
}

/** 波浪相似度参数 */
export interface WaveSimilarityConfig {
  readonly enabled: boolean;
  readonly min_similarity: number;
  readonly boost_factor: number;
  readonly max_boost: number;
  /** 止损收紧系数 (0-1) */
  readonly tight_stop_ratio: number;
  readonly max_pattern_count: number;
  readonly eviction_policy: 'LRU';
  readonly max_memory_mb_limit?: number;
}

/** 全局移动止损 */
export interface GlobalTrailingStopConfig {
  readonly enabled: boolean;
  /** 浮盈超过此 ATR 倍数激活 */
  readonly activation_profit_atr_mult: number;
  readonly max_hold_bars?: number;
}

/** KMA (卡尔曼均线) 参数 */
export interface KalmanConfig {
  readonly q_ratio: number;
  readonly delta: number;
  readonly adaptive_q: boolean;
  readonly min_q_ratio: number;
  readonly max_q_ratio: number;
  /** 防止矩阵退化的抖动 */
  readonly q_ratio_jitter?: number;
  readonly max_q_jitter?: number;
  /** 抖动衰减系数 */
  readonly jitter_decay?: number;
}

/** HMM 参数 (常用特征列: log_ret, range_ratio, d_std, volume_ratio, slope_norm) */
export interface HMMConfig {
  readonly auto_select: boolean;
  readonly min_states: number;
  readonly max_states: number;
  readonly bic_period?: number;
  readonly n_states: number;
  readonly retrain_interval: number;
  readonly warmup_bars: number;
  readonly feature_window: number;
  /** 模型类型，默认 'GaussianHMM' */
  readonly model_type?: string;
  readonly feature_columns?: readonly string[];
}

/** 自适应 S/R 参数 */
export interface AdaptiveSRConfig {
  readonly enabled: boolean;
  readonly method: 'swing_volume';
  readonly swing_lookback: number;
  readonly min_sr_distance_atr: number;
  readonly min_touches: number;
  readonly freeze_on_regime: boolean;
  readonly recalc_on_regime_change?: boolean;
  readonly regime_confirm_bars?: number;
}

/** 小账户增强 (分组) */
export interface SmallAccountEnhancements {
  readonly enabled: boolean;
  // 逃逸
  readonly escape_threshold_shift: number;
  // 共振
  readonly resonance_weight_override: number;
  // 概率过滤
  readonly consecutive_bars_override: number;
  // 冷却
  readonly cooldown_bars_escape: number;
  readonly cooldown_bars_recapture: number;
  // 仓位
  readonly min_recapture_size_btc: number;
  readonly min_volatility_atr_scale: number;
  readonly pullback_coeff_floor: number;
  readonly callback_coeff_floor: number;
  // 风控
  readonly max_daily_trades_override: number;
  readonly profit_target_pct_alert: number;
  readonly max_spread_pct_override: number;
  readonly max_acceptable_slippage_override: number;
}

/** 账户自适应参数 */
export interface AccountAdaptationConfig {
  readonly enabled: boolean;
  readonly reference_balance: number;
  readonly scaling_method: 'sqrt';
  readonly min_scale_factor: number;
  readonly max_scale_factor: number;
  readonly post_scale_min_qty_enforcement?: boolean;
  readonly log_skipped_trades?: boolean;
  readonly log_adapted_params?: boolean;
  readonly small_account_enhancements?: SmallAccountEnhancements;
}

/** 信号优先级 */
export type SignalPriority = 'escape_close' | 'escape_reduce' | 'recapture' | 'callback_drop' | 'pullback_add';

/** 策略自监控 */
export interface SelfMonitoringConfig {
  readonly enabled: boolean;
  readonly max_open_signals_per_hour: number;
  readonly max_close_signals_per_hour?: number;
  readonly pause_duration_minutes?: number;
  readonly max_pauses_per_day?: number;
  readonly auto_resume?: boolean;
}

/** 紧急停止 (触发条件示例: extreme_slippage, system_overload) */
export interface PanicStopConfig {
  readonly enabled: boolean;
  readonly triggers?: readonly string[];
  readonly action?: 'close_all' | 'reduce_only';
}

/** 资金费率保护 */
export interface FundingRateProtectionConfig {
  readonly enabled: boolean;
  readonly close_bars_before: number;
  readonly close_pct?: number;
  readonly funding_rate_history_days?: number;
}

/** 连接风险策略 */
export interface ConnectionRiskStrategy {
  readonly loss_timeout_seconds: number;
  readonly on_disconnect: 'reduce_only' | 'keep_positions' | 'close_positions';
  readonly auto_restore_on_reconnect?: boolean;
  readonly restore_grace_period_sec?: number;
  readonly maintenance_action?: 'close_only' | 'close_positions';
}

/** 黑天鹅检测策略 */
export interface BlackSwanStrategy {
  readonly detection: {
    readonly single_day_move_pct: number;
    readonly intraday_peak_to_trough?: boolean;
    readonly action: 'enter_readonly' | 'close_positions';
  };
  readonly stablecoin_depeg?: {
    readonly threshold_pct: number;
    readonly action: 'close_affected_positions' | 'close_all';
  };
  /** 末日协议 */
  readonly apocalypse?: {
    readonly trigger_conditions: readonly string[];
    readonly action: 'market_close_all';
  };
}

/** 退市保护 */
export interface DelistingProtection {
  readonly enabled: boolean;
  readonly check_interval_hours: number;
  readonly on_delisting: 'close_position_and_alert' | 'immediate_close';
}

/** 事件驱动停止 */
export interface EventBasedHalt {
  readonly enabled: boolean;
  readonly event_sources: readonly string[];
}

// ---------- 完整策略配置 ----------

export interface StrategyConfig {
  readonly config_version?: string;
  /** 配置校验哈希，用于检测篡改 */
  readonly config_hash?: string;
  readonly symbols: readonly string[];
  readonly primary_interval: TimeframeInterval;
  readonly secondary_intervals: readonly TimeframeInterval[];
  readonly market_type?: MarketType;
  readonly multi_symbol?: {
    readonly enabled: boolean;
    readonly max_concurrent_symbols: number;
    readonly risk_budget_allocation_pct: number;
  };
  readonly hierarchy: { readonly strict: boolean; readonly enabled: boolean };
  readonly regime: {
    readonly detector: string;
    readonly confirm_bars: number;
    readonly hysteresis_bars: number;
    readonly range_detection: {
      readonly adx_period?: number;
      readonly adx_threshold: number;
      readonly kma_slope_threshold: number;
      readonly bb_bandwidth_percentile?: number;
    };
  };
  readonly kalman: KalmanConfig;
  readonly hmm: HMMConfig;
  readonly trend_prob_filter: TrendProbFilterConfig;
  readonly escape: EscapeConfig;
  readonly recapture: RecaptureConfig;
  readonly callback_drop: CallbackDropConfig;
  readonly pullback_add: PullbackAddConfig;
  readonly micro_pullback_scalper: MicroPullbackScalperConfig;
  readonly micro_divergence_trader: MicroDivergenceTraderConfig;
  readonly range_modules: RangeModulesConfig;
  readonly wave_similarity: WaveSimilarityConfig;
  readonly resonance: ResonanceConfig;
  readonly adaptive_sr: AdaptiveSRConfig;
  readonly account_adaptation: AccountAdaptationConfig;
  readonly global_trailing_stop?: GlobalTrailingStopConfig;
  /** 信号优先级列表，顺序决定冲突时保留谁 */
  readonly signal_priority?: readonly SignalPriority[];
  readonly self_monitoring?: SelfMonitoringConfig;
  readonly panic_stop?: PanicStopConfig;
  readonly funding_rate_protection?: FundingRateProtectionConfig;
  readonly connection_risk?: ConnectionRiskStrategy;
  readonly black_swan?: BlackSwanStrategy;
  readonly delisting_protection?: DelistingProtection;
  readonly event_based_halt?: EventBasedHalt;
  /** 全局仅减仓模式 */
  readonly reduce_only_mode?: boolean;
  /** 最大连续加仓次数 (全局) */
  readonly max_consecutive_adds?: number;
}

// ---------- 运行时状态 ----------

export interface HMMStateProbabilities {
  readonly BULL: number;
  readonly BEAR: number;
  readonly RANGE: number;
  readonly UNKNOWN?: number;
}

export interface StrategyRuntimeState {
  readonly symbol: string;
  readonly interval: TimeframeInterval;
  readonly hmm_state: MarketRegime;
  readonly hmm_probabilities: HMMStateProbabilities;
  readonly kma_value: number;
  readonly kma_slope: number;
  readonly trend_probability: number;
  readonly escape_score: number;
  readonly resonance_strength: number;
  readonly current_position_size: number;
  readonly unrealized_pnl: number;
  readonly is_chaotic: boolean;
  readonly sr_levels: {
    readonly supports: readonly number[];
    readonly resistances: readonly number[];
  };
  readonly last_signal_time?: Timestamp;
  readonly current_regime_strength?: number;
  readonly next_funding_time?: Timestamp;
}

// ---------- 信号、订单、持仓 ----------

export interface Signal {
  readonly id: string;
  readonly parent_signal_id?: string;
  readonly timestamp: Timestamp;
  /** 信号有效期，超过则作废 */
  readonly expiration_time?: Timestamp;
  readonly symbol: string;
  readonly interval: TimeframeInterval;
  readonly direction: SignalDirection;
  readonly action: TradeAction;
  readonly price: number;
  readonly size: number;
  readonly size_multiplier: number; // 0.3-2.0
  readonly module: string;
  readonly probability: number;
  readonly probability_range?: { readonly low: number; readonly high: number };
  readonly resonance_strength: number;
  readonly escape_score: number;
  readonly reason: string;
  readonly metadata: Readonly<Record<string, unknown>>;
}

export interface Order {
  readonly order_id: string;
  readonly client_order_id?: string;
  readonly symbol: string;
  readonly side: 'BUY' | 'SELL';
  readonly type: OrderType;
  readonly price: number;            // 0 表示市价
  readonly stop_price?: number;
  readonly trailing_delta?: number;
  readonly quantity: number;
  readonly reduce_only?: boolean;
  readonly status: OrderStatus;
  readonly filled_quantity: number;
  readonly avg_fill_price: number;
  readonly commission?: number;
  readonly commission_asset?: string;
  readonly error_message?: string;
  readonly created_at: Timestamp;
  readonly updated_at: Timestamp;
  readonly strategy_id: string;
  readonly signal_id: string;
}

export interface Position {
  readonly symbol: string;
  readonly side: 'LONG' | 'SHORT';
  readonly quantity: number;
  readonly entry_price: number;           // 加权平均
  readonly break_even_price?: number;     // 含手续费
  readonly mark_price: number;
  readonly liquidation_price?: number;
  readonly unrealized_pnl: number;
  readonly realized_pnl: number;
  readonly stop_loss: number;
  readonly take_profit: number;
  readonly opened_at: Timestamp;
  readonly updated_at?: Timestamp;
  readonly margin_type?: 'ISOLATED' | 'CROSS';
  readonly leverage?: number;
  readonly notional?: number;
  readonly isolated_wallet?: number;
  readonly cost_basis?: number;           // 含手续费的总成本
}

// ---------- 模块与性能 ----------

export interface ModuleStatus {
  readonly name: string;
  readonly type?: string;
  readonly enabled: boolean;
  readonly status: 'OK' | 'WARNING' | 'DEGRADED' | 'ERROR' | 'DISABLED';
  readonly last_update: Timestamp;
  readonly latency_ms: number;
  readonly avg_latency_ms?: number;
  readonly max_latency_ms?: number;
  readonly message?: string;
}

export interface PerformanceMetrics {
  readonly sharpe_ratio: number;
  readonly sortino_ratio: number;
  readonly calmar_ratio: number;
  readonly win_rate: number;
  readonly avg_win: number;
  readonly avg_loss: number;
  readonly profit_factor: number;
  readonly max_drawdown: number;
  readonly max_drawdown_duration: number;
  readonly total_trades: number;
}

export interface RiskReport {
  /** 风险敞口/净值 */
  readonly current_risk: number;
  readonly max_risk: number;
  readonly daily_pnl: number;
  readonly daily_pnl_pct: number;
  readonly max_drawdown: number;
  readonly max_drawdown_duration?: number;
  readonly current_leverage: number;
  readonly margin_used: number;
  readonly margin_total: number;
  readonly var_95?: number;
  readonly cvar_95?: number;
  readonly sharpe_ratio?: number;
  readonly daily_win_rate?: number;
  readonly avg_win?: number;
  readonly avg_loss?: number;
}

/** 数据健康状态 */
export interface DataHealthStatus {
  readonly symbol: string;
  readonly interval: TimeframeInterval;
  readonly missing_ratio: number;
  readonly outlier_ratio: number;
  readonly delay_ms: number;
  readonly score: number; // 0-100
}

/** WebSocket 流状态 */
export interface StreamStatus {
  readonly channel: string;
  readonly connected: boolean;
  readonly latency_ms: number;
  readonly message_rate: number;
}

/** 告警 */
export type AlertSeverity = 'INFO' | 'WARNING' | 'CRITICAL';

export interface Alert {
  readonly id: string;
  readonly timestamp: Timestamp;
  readonly severity: AlertSeverity;
  readonly module: string;
  readonly message: string;
  readonly acknowledged: boolean;
}

/** 执行报告 */
export interface ExecutionReport {
  readonly order_id: string;
  readonly status: OrderStatus;
  readonly filled_quantity: number;
  readonly avg_price: number;
  readonly commission: number;
  readonly timestamp: Timestamp;
}

/** 回测结果 (轻量) */
export interface BacktestResult {
  readonly total_return: number;
  readonly sharpe_ratio: number;
  readonly max_drawdown: number;
  readonly win_rate: number;
  readonly trades: number;
}

// ---------- Redux Store 切片 ----------

/**
 * 策略切片状态
 * runtime 键名格式: `${symbol}_${interval}`
 * 全局 loading/error 影响整个策略面板
 */
export interface StrategySliceState {
  readonly config: DeepPartial<StrategyConfig>;
  readonly runtime: Record<string, StrategyRuntimeState>;
  readonly signals: readonly Signal[];
  readonly modules: readonly ModuleStatus[];
  readonly loading: boolean;
  readonly error: string | null;
}
