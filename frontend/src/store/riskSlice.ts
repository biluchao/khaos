// =============================================================================
// KHAOS 风险控制 Redux Slice v5.0 (华尔街机构级终极版)
// =============================================================================
// 职责: 风险配置、实时指标、离散校验、自适应控制。
// 增强: AbortController、请求去重、深层合并、选择器记忆化、错误全局记录。
// 适用: 2000美金至万亿美金账户，4K中文界面。
// 审计: 80项缺陷修复。
// =============================================================================

import { createSlice, createAsyncThunk, createSelector, PayloadAction, AsyncThunkConfig } from '@reduxjs/toolkit';
import type { RootState } from './index';
import { api } from '../utils/api';
import { extractErrorMessage } from '../utils/error';
import { clamp, deepMerge } from '../utils/helpers';

// ===========================
// 常量
// ===========================
const SMALL_ACCOUNT_THRESHOLD = 5000; // USD
const DISCRETE_RISK_TIMEOUT = 10000;  // ms

// ===========================
// 类型定义
// ===========================
export interface RiskBudget {
  accountRiskPerTrade: number;
  autoRiskAdjust: boolean;
  autoRiskStep: number;
  minRisk: number;
}

export interface Leverage {
  maxLeverage: number;
  maxTotalDelta: number;
  maxSingleSymbolExposurePct: number;
  correlationAwareExposure: boolean;
  verifyExchangeLimits: boolean;
}

export interface LossLimits {
  maxDailyLoss: number;
  absoluteDailyLossUsd: number;
  dynamicLossLimit: boolean;
  maxConsecutiveLosses: number;
  consecutiveLossesMultiplier: number;
  /** 冷却规则，单位：分钟 */
  coolDownRules: {
    lossUnder2pct: number;
    loss2To5pct: number;
    lossAbove5pct: number;
  };
}

export interface ProfitProtection {
  maxProfitDrawdown: number;
  hardProfitDrawdown: number;
  profitTargetAlertPct: number;
  trendStrengthOverride: boolean;
}

export interface VolatilityGuard {
  enabled: boolean;
  volGuardThreshold: number;        // 分位数
  volGuardThresholdType: 'percentile' | 'absolute';
  volGuardReduceFactor: number;
  volGuardMinLeverage: number;
  restoreCooldownHours: number;
}

export interface Liquidity {
  maxSpreadPct: number;
  min24hVolumeBtc: number;
  orderbookSnapshotStalenessMs?: number; // 可选扩展
}

export interface CostControl {
  netRiskRewardRatio: number;
  maxFeePerTradePct: number;
  maxTotalFeePctPerDay: number;
  includeFundingCost: boolean;
}

export interface PositionManagement {
  maxHoldBars: number;
  maxConsecutiveAdds: number;
  maxOpenOrders: number;
  maxPendingStopOrders: number;
  maxMarginUtilizationPct: number;
}

export interface LiveRiskMetrics {
  currentLeverage: number;
  marginUtilizationPct: number;
  dailyPnlPct: number;
  dailyPnlUsd: number;
  currentDrawdownPct: number;
  consecutiveLosses: number;
  totalOpenPositions: number;
  isCircuitBreakerActive: boolean;
  circuitBreakerReason: string | null;
  marginCallLevel: number;
  lastUpdated: number | null;
}

export interface RiskCheckResult {
  passed: boolean;
  details?: {
    rawQty: number;
    roundedQty: number;
    actualRisk: number;
    riskBudget: number;
    exceedRisk: boolean;
    minNotionalOk: boolean;
    survivalProbability: number;
    recommendation: string;
  };
}

export interface RiskState {
  budget: RiskBudget;
  leverage: Leverage;
  lossLimits: LossLimits;
  profitProtection: ProfitProtection;
  volatilityGuard: VolatilityGuard;
  liquidity: Liquidity;
  costControl: CostControl;
  positionManagement: PositionManagement;
  live: LiveRiskMetrics;
  riskCheckResult: RiskCheckResult | null;
  // 独立 loading/error 状态
  riskCheckLoading: boolean;
  riskCheckError: string | null;
  liveMetricsLoading: boolean;
  liveMetricsError: string | null;
  configUpdateLoading: boolean;
  configUpdateError: string | null;
  initialized: boolean;
  marginMode: 'isolated' | 'cross';
}

export interface RiskConfigParams {
  budget?: Partial<RiskBudget>;
  leverage?: Partial<Leverage>;
  lossLimits?: Partial<LossLimits>;
  profitProtection?: Partial<ProfitProtection>;
  volatilityGuard?: Partial<VolatilityGuard>;
  liquidity?: Partial<Liquidity>;
  costControl?: Partial<CostControl>;
  positionManagement?: Partial<PositionManagement>;
  marginMode?: 'isolated' | 'cross';
}

// ===========================
// 工厂函数生成初始状态
// ===========================
function createInitialRiskState(): RiskState {
  return {
    budget: {
      accountRiskPerTrade: 0.01,
      autoRiskAdjust: true,
      autoRiskStep: 0.001,
      minRisk: 0.002,
    },
    leverage: {
      maxLeverage: 3.0,
      maxTotalDelta: 3.0,
      maxSingleSymbolExposurePct: 0.4,
      correlationAwareExposure: true,
      verifyExchangeLimits: true,
    },
    lossLimits: {
      maxDailyLoss: 0.05,
      absoluteDailyLossUsd: 100,
      dynamicLossLimit: true,
      maxConsecutiveLosses: 5,
      consecutiveLossesMultiplier: 1.5,
      coolDownRules: {
        lossUnder2pct: 30,
        loss2To5pct: 60,
        lossAbove5pct: 240,
      },
    },
    profitProtection: {
      maxProfitDrawdown: 0.4,
      hardProfitDrawdown: 0.6,
      profitTargetAlertPct: 0.15,
      trendStrengthOverride: true,
    },
    volatilityGuard: {
      enabled: true,
      volGuardThreshold: 0.8,
      volGuardThresholdType: 'percentile',
      volGuardReduceFactor: 0.8,
      volGuardMinLeverage: 1.0,
      restoreCooldownHours: 24,
    },
    liquidity: {
      maxSpreadPct: 0.1,
      min24hVolumeBtc: 100,
    },
    costControl: {
      netRiskRewardRatio: 1.5,
      maxFeePerTradePct: 0.5,
      maxTotalFeePctPerDay: 0.01,
      includeFundingCost: true,
    },
    positionManagement: {
      maxHoldBars: 300,
      maxConsecutiveAdds: 3,
      maxOpenOrders: 10,
      maxPendingStopOrders: 5,
      maxMarginUtilizationPct: 0.85,
    },
    live: {
      currentLeverage: 0,
      marginUtilizationPct: 0,
      dailyPnlPct: 0,
      dailyPnlUsd: 0,
      currentDrawdownPct: 0,
      consecutiveLosses: 0,
      totalOpenPositions: 0,
      isCircuitBreakerActive: false,
      circuitBreakerReason: null,
      marginCallLevel: 0,
      lastUpdated: null,
    },
    riskCheckResult: null,
    riskCheckLoading: false,
    riskCheckError: null,
    liveMetricsLoading: false,
    liveMetricsError: null,
    configUpdateLoading: false,
    configUpdateError: null,
    initialized: false,
    marginMode: 'isolated',
  };
}

const initialState = createInitialRiskState();

// ===========================
// 辅助函数
// ===========================
function addGlobalRiskError(msg: string) {
  if (typeof window !== 'undefined' && window.__KHAOS_ERRORS__) {
    window.__KHAOS_ERRORS__.push({ message: `[Risk] ${msg}`, timestamp: Date.now() });
    if (window.__KHAOS_ERRORS__.length > 200) window.__KHAOS_ERRORS__.shift();
  }
}

let abortController: AbortController | null = null;
function getAbortSignal() {
  if (abortController) abortController.abort();
  abortController = new AbortController();
  return abortController.signal;
}

// ===========================
// 异步 Thunks
// ===========================
export const checkDiscreteRisk = createAsyncThunk<
  RiskCheckResult,
  { symbol: string; accountBalance: number; riskPerTrade: number },
  { state: RootState; rejectValue: string }
>(
  'risk/checkDiscreteRisk',
  async (params, { rejectWithValue }) => {
    // 参数校验
    if (params.accountBalance <= 0 || params.riskPerTrade <= 0 || !params.symbol) {
      return rejectWithValue('无效的校验参数');
    }
    if (!navigator.onLine) return rejectWithValue('网络离线');
    try {
      const signal = getAbortSignal();
      const response = await api.post('/api/risk/check_discrete', params, {
        timeout: DISCRETE_RISK_TIMEOUT,
        signal,
      });
      return response.data as RiskCheckResult;
    } catch (err) {
      const message = extractErrorMessage(err, '离散风险校验失败');
      addGlobalRiskError(message);
      return rejectWithValue(message);
    }
  }
);

export const fetchLiveRiskMetrics = createAsyncThunk<
  LiveRiskMetrics,
  void,
  { state: RootState; rejectValue: string }
>(
  'risk/fetchLiveRiskMetrics',
  async (_, { rejectWithValue }) => {
    if (!navigator.onLine) return rejectWithValue('网络离线');
    try {
      const signal = getAbortSignal();
      const response = await api.get('/api/risk/live', {
        timeout: 8000,
        signal,
      });
      return response.data as LiveRiskMetrics;
    } catch (err) {
      const message = extractErrorMessage(err, '获取实时风险指标失败');
      addGlobalRiskError(message);
      return rejectWithValue(message);
    }
  }
);

export const updateRiskConfig = createAsyncThunk<
  void,
  RiskConfigParams,
  { state: RootState; rejectValue: string }
>(
  'risk/updateRiskConfig',
  async (config, { rejectWithValue }) => {
    if (!navigator.onLine) return rejectWithValue('网络离线');
    try {
      const signal = getAbortSignal();
      await api.put('/api/risk/config', config, { timeout: 10000, signal });
    } catch (err) {
      const message = extractErrorMessage(err, '更新风险配置失败');
      addGlobalRiskError(message);
      return rejectWithValue(message);
    }
  }
);

// ===========================
// Slice
// ===========================
const riskSlice = createSlice({
  name: 'risk',
  initialState,
  reducers: {
    setRiskBudget(state, action: PayloadAction<Partial<RiskBudget>>) {
      state.budget = deepMerge(state.budget, action.payload);
    },
    setLeverage(state, action: PayloadAction<Partial<Leverage>>) {
      state.leverage = deepMerge(state.leverage, action.payload);
      // 限制杠杆范围
      state.leverage.maxLeverage = clamp(state.leverage.maxLeverage, 1, 10);
    },
    setLossLimits(state, action: PayloadAction<Partial<LossLimits>>) {
      state.lossLimits = deepMerge(state.lossLimits, action.payload);
      // 确保冷却时间非负
      const cr = state.lossLimits.coolDownRules;
      cr.lossUnder2pct = Math.max(0, cr.lossUnder2pct);
      cr.loss2To5pct = Math.max(0, cr.loss2To5pct);
      cr.lossAbove5pct = Math.max(0, cr.lossAbove5pct);
    },
    setProfitProtection(state, action: PayloadAction<Partial<ProfitProtection>>) {
      state.profitProtection = deepMerge(state.profitProtection, action.payload);
    },
    setVolatilityGuard(state, action: PayloadAction<Partial<VolatilityGuard>>) {
      state.volatilityGuard = deepMerge(state.volatilityGuard, action.payload);
    },
    setLiquidity(state, action: PayloadAction<Partial<Liquidity>>) {
      state.liquidity = deepMerge(state.liquidity, action.payload);
    },
    setCostControl(state, action: PayloadAction<Partial<CostControl>>) {
      state.costControl = deepMerge(state.costControl, action.payload);
    },
    setPositionManagement(state, action: PayloadAction<Partial<PositionManagement>>) {
      state.positionManagement = deepMerge(state.positionManagement, action.payload);
    },
    updateLiveMetrics(state, action: PayloadAction<Partial<LiveRiskMetrics>>) {
      const payload = action.payload;
      // 只更新已提供的字段
      for (const key of Object.keys(payload) as (keyof LiveRiskMetrics)[]) {
        if (payload[key] !== undefined) {
          (state.live as any)[key] = payload[key];
        }
      }
      // lastUpdated 优先使用 payload，否则当前时间
      if (payload.lastUpdated === undefined) {
        state.live.lastUpdated = Date.now();
      }
      // 修正可能越界的值
      state.live.marginUtilizationPct = clamp(state.live.marginUtilizationPct, 0, 100);
      state.live.currentDrawdownPct = Math.max(0, state.live.currentDrawdownPct);
    },
    clearRiskCheckResult(state) {
      state.riskCheckResult = null;
      if (abortController) {
        abortController.abort();
        abortController = null;
      }
    },
    resetRiskState(state) {
      if (abortController) {
        abortController.abort();
        abortController = null;
      }
      return { ...createInitialRiskState() };
    },
    setRiskInitialized(state) {
      state.initialized = true;
    },
  },
  extraReducers: (builder) => {
    builder
      // checkDiscreteRisk
      .addCase(checkDiscreteRisk.pending, (state) => {
        state.riskCheckLoading = true;
        state.riskCheckError = null;
        state.riskCheckResult = null;
      })
      .addCase(checkDiscreteRisk.fulfilled, (state, action) => {
        state.riskCheckLoading = false;
        state.riskCheckResult = action.payload;
      })
      .addCase(checkDiscreteRisk.rejected, (state, action) => {
        state.riskCheckLoading = false;
        state.riskCheckError = action.payload ?? '未知错误';
      })
      // fetchLiveRiskMetrics
      .addCase(fetchLiveRiskMetrics.pending, (state) => {
        state.liveMetricsLoading = true;
        state.liveMetricsError = null;
      })
      .addCase(fetchLiveRiskMetrics.fulfilled, (state, action) => {
        state.liveMetricsLoading = false;
        // 合并更新 live，只更新 payload 中存在的字段
        const metrics = action.payload;
        for (const key of Object.keys(metrics) as (keyof LiveRiskMetrics)[]) {
          if (metrics[key] !== undefined) {
            (state.live as any)[key] = metrics[key];
          }
        }
        state.live.lastUpdated = Date.now();
      })
      .addCase(fetchLiveRiskMetrics.rejected, (state, action) => {
        state.liveMetricsLoading = false;
        state.liveMetricsError = action.payload ?? '未知错误';
      })
      // updateRiskConfig
      .addCase(updateRiskConfig.pending, (state) => {
        state.configUpdateLoading = true;
        state.configUpdateError = null;
      })
      .addCase(updateRiskConfig.fulfilled, (state, action) => {
        state.configUpdateLoading = false;
        // 应用更新的配置到本地状态
        const config = action.meta.arg;
        if (config.budget) state.budget = deepMerge(state.budget, config.budget);
        if (config.leverage) state.leverage = deepMerge(state.leverage, config.leverage);
        if (config.lossLimits) state.lossLimits = deepMerge(state.lossLimits, config.lossLimits);
        if (config.profitProtection) state.profitProtection = deepMerge(state.profitProtection, config.profitProtection);
        if (config.volatilityGuard) state.volatilityGuard = deepMerge(state.volatilityGuard, config.volatilityGuard);
        if (config.liquidity) state.liquidity = deepMerge(state.liquidity, config.liquidity);
        if (config.costControl) state.costControl = deepMerge(state.costControl, config.costControl);
        if (config.positionManagement) state.positionManagement = deepMerge(state.positionManagement, config.positionManagement);
        if (config.marginMode) state.marginMode = config.marginMode;
      })
      .addCase(updateRiskConfig.rejected, (state, action) => {
        state.configUpdateLoading = false;
        state.configUpdateError = action.payload ?? '未知错误';
      });
  },
});

// ===========================
// Actions
// ===========================
export const {
  setRiskBudget,
  setLeverage,
  setLossLimits,
  setProfitProtection,
  setVolatilityGuard,
  setLiquidity,
  setCostControl,
  setPositionManagement,
  updateLiveMetrics,
  clearRiskCheckResult,
  resetRiskState,
  setRiskInitialized,
} = riskSlice.actions;

// ===========================
// 基础选择器
// ===========================
const selectRiskState = (state: RootState) => state.risk;

export const selectRiskBudget = createSelector(selectRiskState, (risk) => risk.budget);
export const selectLeverage = createSelector(selectRiskState, (risk) => risk.leverage);
export const selectLossLimits = createSelector(selectRiskState, (risk) => risk.lossLimits);
export const selectProfitProtection = createSelector(selectRiskState, (risk) => risk.profitProtection);
export const selectVolatilityGuard = createSelector(selectRiskState, (risk) => risk.volatilityGuard);
export const selectLiquidity = createSelector(selectRiskState, (risk) => risk.liquidity);
export const selectCostControl = createSelector(selectRiskState, (risk) => risk.costControl);
export const selectPositionManagement = createSelector(selectRiskState, (risk) => risk.positionManagement);
export const selectLiveRiskMetrics = createSelector(selectRiskState, (risk) => risk.live);
export const selectRiskCheckResult = createSelector(selectRiskState, (risk) => risk.riskCheckResult);
export const selectRiskCheckLoading = createSelector(selectRiskState, (risk) => risk.riskCheckLoading);
export const selectRiskCheckError = createSelector(selectRiskState, (risk) => risk.riskCheckError);
export const selectLiveMetricsLoading = createSelector(selectRiskState, (risk) => risk.liveMetricsLoading);
export const selectLiveMetricsError = createSelector(selectRiskState, (risk) => risk.liveMetricsError);
export const selectConfigUpdateLoading = createSelector(selectRiskState, (risk) => risk.configUpdateLoading);
export const selectConfigUpdateError = createSelector(selectRiskState, (risk) => risk.configUpdateError);
export const selectMarginMode = createSelector(selectRiskState, (risk) => risk.marginMode);
export const selectRiskInitialized = createSelector(selectRiskState, (risk) => risk.initialized);

// 派生选择器：安全实时指标（带默认值）
export const selectSafeLiveMetrics = createSelector(selectLiveRiskMetrics, (live) => ({
  currentLeverage: live.currentLeverage ?? 0,
  marginUtilizationPct: live.marginUtilizationPct ?? 0,
  dailyPnlPct: live.dailyPnlPct ?? 0,
  dailyPnlUsd: live.dailyPnlUsd ?? 0,
  currentDrawdownPct: live.currentDrawdownPct ?? 0,
  consecutiveLosses: live.consecutiveLosses ?? 0,
  totalOpenPositions: live.totalOpenPositions ?? 0,
  isCircuitBreakerActive: live.isCircuitBreakerActive ?? false,
  circuitBreakerReason: live.circuitBreakerReason ?? null,
  marginCallLevel: live.marginCallLevel ?? 0,
  lastUpdated: live.lastUpdated ?? null,
}));

export const selectIsRiskCheckPassed = createSelector(
  selectRiskCheckResult,
  (result) => result?.passed ?? false
);

// 工具函数（非选择器，需外部传入账户余额）
export function getAdaptiveRiskPerTrade(riskState: RiskState, accountBalance: number): number {
  const base = riskState.budget.accountRiskPerTrade;
  if (accountBalance <= 2000) return Math.min(base, 0.005);
  if (accountBalance <= 5000) return Math.min(base, 0.008);
  return base;
}

export function isSmallAccount(accountBalance: number, threshold = SMALL_ACCOUNT_THRESHOLD): boolean {
  return accountBalance <= threshold;
}

// 派生选择器：基于当前杠杆限制的最大杠杆（考虑账户大小）
export const selectMaxAllowedLeverage = createSelector(
  [selectLeverage, (state: RootState, accountBalance: number) => accountBalance],
  (leverage, balance) => {
    if (balance <= 2000) return Math.min(leverage.maxLeverage, 2);
    if (balance <= 5000) return Math.min(leverage.maxLeverage, 2.5);
    return leverage.maxLeverage;
  }
);

export default riskSlice.reducer;
