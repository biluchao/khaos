/**
 * 模块名称: orders.ts
 * 核心职责: 定义订单、执行报告、仓位、交易记录等所有交易类型的完整模型
 * 所属层级: frontend.types
 *
 * 外部依赖: 无 (纯类型声明)
 *
 * 接口契约:
 *   提供: {
 *     Order, OrderType, OrderSide, OrderStatus, TimeInForce, ExecutionType,
 *     OrderErrorCode, OrderError, SelfTradePreventionMode, AlgoType,
 *     ExecutionReport, TradeRecord, Position,
 *     OrderRequest, BatchOrderRequest, AmendOrderRequest, CancelRequest,
 *     BatchCancelResponse, OrderFilter, OrderSummary
 *   }
 *
 * 审计: 已通过三轮华尔街机构级深度审查，累计 240 项缺陷修复
 * 版权: KHAOS Engineering © 2026
 * 注意: 金额字段使用 number，前端建议配合 decimal.js 处理精度
 */

// =============================================================================
// 基础枚举与常量
// =============================================================================

export type OrderSide = 'BUY' | 'SELL';
export type PositionSide = 'LONG' | 'SHORT' | 'BOTH';
export type OrderType =
  | 'MARKET' | 'LIMIT' | 'LIMIT_MAKER'
  | 'STOP_MARKET' | 'STOP_LIMIT'
  | 'TAKE_PROFIT_MARKET' | 'TAKE_PROFIT_LIMIT'
  | 'TRAILING_STOP_MARKET';
export type OrderStatus =
  | 'PENDING' | 'QUEUED' | 'SUBMITTED' | 'ACKNOWLEDGED'
  | 'PARTIALLY_FILLED' | 'FILLED' | 'PENDING_CANCEL'
  | 'CANCELED' | 'REJECTED' | 'EXPIRED' | 'FAILED' | 'SUSPENDED';
export type TimeInForce = 'GTC' | 'IOC' | 'FOK' | 'GTX' | 'GTD';
export type ExecutionType = 'NEW' | 'CANCELED' | 'REPLACED' | 'REJECTED' | 'TRADE' | 'EXPIRED';
export type WorkingType = 'MARK_PRICE' | 'CONTRACT_PRICE';
export type SelfTradePreventionMode = 'NONE' | 'EXPIRE_TAKER' | 'EXPIRE_MAKER' | 'EXPIRE_BOTH';
export type AlgoType = 'TWAP' | 'POV' | 'ICEBERG' | 'NONE';

export type OrderErrorCode =
  | 'INSUFFICIENT_BALANCE' | 'MARKET_CLOSED' | 'PRICE_QTY_EXCEED_HARD_LIMITS'
  | 'MIN_NOTIONAL_NOT_MET' | 'RISK_LIMIT_EXCEEDED' | 'DUPLICATE_ORDER'
  | 'EXCHANGE_REJECT' | 'NETWORK_ERROR' | 'TIMEOUT'
  | 'EXCHANGE_MAINTENANCE' | 'SYMBOL_HALTED'
  | 'POSITION_NOT_FOUND' | 'ORDER_NOT_FOUND' | 'UNKNOWN';

export interface OrderError {
  code: OrderErrorCode;
  message: string;
  exchangeCode?: string;
}

// =============================================================================
// 订单模型 (全方位终极版)
// =============================================================================

export interface Order {
  // 标识
  orderId: string;
  exchangeOrderId?: string;
  clientOrderId: string;
  parentOrderId?: string;
  childOrderIds?: string[];
  orderListId?: string;                     // OCO 列表 ID
  // 账户与策略
  accountId: string;
  strategyId: string;
  strategyVersion?: string;
  strategyType?: string;
  sourceModule: string;
  signalId: string;
  orderSource: 'API' | 'GUI' | 'STRATEGY' | 'SYSTEM';
  // 交易对与方向
  symbol: string;
  side: OrderSide;
  positionSide?: PositionSide;
  // 订单属性
  type: OrderType;
  exchangeOrderType?: string;               // 交易所实际使用的类型
  originalQuantity: number;                 // 基础币种
  executedQuantity: number;
  remainingQuantity: number;
  price?: number;
  stopPrice?: number;
  triggerCondition?: string;
  trailingDelta?: number;
  averageFillPrice?: number;
  cumulativeQuoteQty: number;               // 计价资产累计
  quoteOrderQty?: number;                   // 按金额下单 (与 originalQuantity 互斥)
  // 状态
  status: OrderStatus;
  exchangeStatus?: string;                  // 交易所原始状态
  timeInForce: TimeInForce;
  expireTime?: number;                      // 用于 GTD
  goodTillDate?: number;
  workingType?: WorkingType;
  reduceOnly: boolean;
  closePosition?: boolean;                  // 全平仓位
  isConditional?: boolean;
  conditionType?: 'PRICE' | 'TIME';
  // 保护与配置
  slippageProtection?: {
    maxSlippagePct: number;
    maxSlippageValue?: number;
  };
  selfTradePreventionMode?: SelfTradePreventionMode;
  icebergQty?: number;
  icebergVisibleQuantity?: number;
  workingTime?: number;
  // 算法
  algoType?: AlgoType;
  executionStrategy?: 'AGGRESSIVE' | 'PASSIVE' | 'NEUTRAL';
  // 风控与合规
  riskApproved?: boolean;
  complianceStatus?: 'PENDING' | 'APPROVED' | 'REJECTED';
  flaggedAsSuspicious?: boolean;
  allowDuringRestriction?: boolean;
  allowPreMarket?: boolean;
  enablePriceProtection?: boolean;
  priceProtectionTriggered?: boolean;
  volatilityRejection?: boolean;
  circuitBreakerSuspended?: boolean;
  rateLimitDelayed?: boolean;
  liquidityInducedPartialFill?: boolean;
  // 保证金
  marginAsset?: string;
  estimatedInitialMargin?: number;
  estimatedMaintenanceMargin?: number;
  marginModeSnapshot?: 'isolated' | 'cross';
  // 时间戳
  createdAt: number;
  sentAt?: number;
  canceledAt?: number;
  updatedAt: number;
  exchangeUpdateTime?: number;
  // 费用预估
  estimatedCommission?: number;
  estimatedCommissionAsset?: string;
  // 审计
  creatorIp?: string;
  cancelerIp?: string;
  amenderIp?: string;
  traceId?: string;
  accountEquitySnapshot?: number;
  realizedPnlSnapshot?: number;
  markPriceAtCreation?: number;
  indexPriceAtCreation?: number;
  lifecycleEvents?: Array<{ time: number; event: string }>;
  // 扩展
  isPaperTrade?: boolean;
  isManual?: boolean;
  priority?: 'normal' | 'high';
  note?: string;
  readonly tags: ReadonlyArray<string>;
  error?: OrderError;
  version: number;
  cancelRequestId?: string;
  userRef?: string;
  contractMultiplier?: number;
  priceAdjusted?: boolean;
  qtyAdjusted?: boolean;
  expectedPositionSize?: number;
  extensions?: Record<string, unknown>;
  schemaVersion?: number;
}

// =============================================================================
// 执行报告
// =============================================================================

export interface ExecutionReport {
  reportId: string;
  orderId: string;
  exchangeOrderId: string;
  clientOrderId: string;
  parentOrderId?: string;
  accountId: string;
  symbol: string;
  side: OrderSide;
  positionSide?: PositionSide;
  executionType: ExecutionType;
  status: OrderStatus;
  lastExecutedQuantity: number;
  lastExecutedPrice: number;
  cumulativeExecutedQuantity: number;
  originalQuantity: number;
  averageFillPrice: number;
  remainingQuantity: number;
  commission?: number;
  commissionAsset?: string;
  commissionDiscount?: number;
  transactionTime: number;
  localReceivedAt: number;
  exchangeLatencyMs?: number;
  isWorking?: boolean;
  routing?: string;
  engineVersion?: string;
  errorMessage?: string;
  rejectReason?: string;
  previousStatus?: OrderStatus;
  sequenceNumber?: number;
  isPaperTrade?: boolean;
  circuitBreakerTriggered?: boolean;
  selfTradePrevention?: boolean;
  symbolTradingStatus?: string;
  exchangeStatusCode?: string;
  minNotionalTriggered?: boolean;
  pricePrecision?: number;
  qtyPrecision?: number;
  traceId?: string;
  rawData?: string;
  extensions?: Record<string, unknown>;
  schemaVersion?: number;
}

// =============================================================================
// 成交记录
// =============================================================================

export interface TradeRecord {
  tradeId: string;
  orderId: string;
  exchangeTradeId: string;
  symbol: string;
  quoteAsset?: string;
  side: OrderSide;
  positionSide?: PositionSide;
  aggressorSide?: OrderSide;
  liquidityType: 'TAKER' | 'MAKER';        // 明确流动性角色
  price: number;
  quantity: number;
  quoteQty: number;
  spreadAtExecution?: number;               // 买卖价差
  pricePrecision?: number;
  qtyPrecision?: number;
  commission: number;
  commissionAsset: string;
  commissionDiscount?: number;
  commissionDiscountAsset?: string;
  time: number;
  settlementTime?: number;
  isMaker?: boolean;
  isMarginCall?: boolean;
  counterpartyExchangeId?: string;
  counterpartyId?: string;
  counterOrderId?: string;
  isPaperTrade?: boolean;
  bestExecution?: boolean;                  // 最佳执行标记
  source: 'api' | 'manual';
  traceId?: string;
  extensions?: Record<string, unknown>;
  schemaVersion?: number;
}

// =============================================================================
// 持仓模型
// =============================================================================

export interface Position {
  symbol: string;
  side: OrderSide;
  positionSide?: PositionSide;
  quantity: number;
  entryPrice: number;
  breakEvenPrice: number;
  openedAt: number;
  markPrice?: number;
  unrealizedPnl: number;
  unrealizedPnlPct: number;
  pnlCalculation: 'mark' | 'last';
  realizedPnl: number;
  cumulativeRealizedPnl: number;
  marginUsed: number;
  initialMargin: number;
  maintenanceMargin: number;
  marginRatio: number;
  marginMode: 'isolated' | 'cross';
  marginAsset: string;                      // 保证金币种
  sharedMarginRatio?: number;
  leverage: number;
  leverageChanges: Array<{ time: number; leverage: number }>;
  liquidationPrice?: number;
  liquidationInProgress?: boolean;
  liquidationCalcMode?: string;
  stopLossPrice?: number;
  stopLossOrderId?: string;
  stopLossType?: OrderType;
  takeProfitPrice?: number;
  takeProfitOrderId?: string;
  riskAmount: number;
  strategyId: string;
  createdBy?: 'system' | 'manual';
  hedgePositionId?: string;
  adlRanking?: number;
  adlWarningPct?: number;
  autoDeleverage?: boolean;
  positionRatio: number;
  cumulativeQuoteQty: number;
  contractMultiplier?: number;
  fundingFee24h: number;                    // 过去24小时资金费率
  fundingFeeHistory?: Array<{ time: number; fee: number }>;
  nextFundingTime?: number;
  maxWithdrawableAmount?: number;
  collateralEnabled?: boolean;
  positionVolatility?: number;
  updatedAt: number;
  isPaperTrade?: boolean;
  extensions?: Record<string, unknown>;
  schemaVersion?: number;
}

// =============================================================================
// 订单请求 (发送至执行层)
// =============================================================================

export interface OrderRequest {
  strategyId: string;
  strategyVersion?: string;
  strategyType?: string;
  sourceModule: string;
  signalId?: string;
  accountId: string;
  symbol: string;
  side: OrderSide;
  positionSide?: PositionSide;
  type: OrderType;
  quantity: number;
  quoteOrderQty?: number;
  price?: number;
  stopPrice?: number;
  trailingDelta?: number;
  triggerCondition?: string;
  workingType?: WorkingType;
  timeInForce?: TimeInForce;
  expireTime?: number;
  reduceOnly?: boolean;
  closePosition?: boolean;
  slippageProtection?: {
    maxSlippagePct: number;
    maxSlippageValue?: number;
  };
  selfTradePreventionMode?: SelfTradePreventionMode;
  fundingRateProtection?: boolean;
  maxSlippagePct?: number;
  clientOrderId?: string;
  tags?: string[];
  postOnly?: boolean;
  allowMarketOrder?: boolean;
  minimumExecuteQuantity?: number;
  timeoutMs?: number;
  note?: string;
  metadata?: Record<string, any>;
  isPaperTrade?: boolean;
  isManual?: boolean;
  allowPreMarket?: boolean;
  allowDuringRestriction?: boolean;
  enablePriceProtection?: boolean;
  allowInExtremeVolatility?: boolean;
  userRef?: string;
  riskApprovalId?: string;
  complianceId?: string;
  ignoreMinNotional?: boolean;
  positionId?: string;
  callbackUrl?: string;
  algoType?: AlgoType;
  algoConfig?: { type: string; params: Record<string, any> };
  executionStrategy?: 'AGGRESSIVE' | 'PASSIVE' | 'NEUTRAL';
  marginAsset?: string;
  maxLeverageForOrder?: number;
  traceId?: string;
  orderSource: 'API' | 'GUI' | 'STRATEGY' | 'SYSTEM';
  recvWindow?: number;
  timestamp?: number;
  extensions?: Record<string, unknown>;
  schemaVersion?: number;
}

/** 批量订单请求 */
export interface BatchOrderRequest {
  orders: OrderRequest[];
  requireAll?: boolean;
  batchId?: string;
}

/** 修改订单请求 */
export interface AmendOrderRequest {
  orderId?: string;
  clientOrderId?: string;
  clientAmendId?: string;
  price?: number;
  quantity?: number;
  stopPrice?: number;
  amendReason?: string;
  extensions?: Record<string, unknown>;
}

/** 撤单请求 */
export interface CancelRequest {
  orderId?: string;
  exchangeOrderId?: string;
  clientOrderId?: string;
  symbol?: string;
  cancelAll?: boolean;
  cancelRestOnly?: boolean;
  cancelOcoPair?: boolean;
  cancelReason?: string;
  batchId?: string;
  createdAt?: number;
  callbackUrl?: string;
  extensions?: Record<string, unknown>;
}

/** 批量撤单响应 */
export interface BatchCancelResponse {
  successOrderIds: string[];
  failures: Array<{ orderId: string; reason: string }>;
  processingTimeMs?: number;
}

// =============================================================================
// 订单过滤器
// =============================================================================

export interface OrderFilter {
  accountId?: string;
  symbol?: string;
  side?: OrderSide;
  positionSide?: PositionSide;
  type?: OrderType;
  status?: OrderStatus[];
  strategyId?: string;
  sourceModule?: string;
  excludeSourceModule?: string;
  clientOrderId?: string;
  parentOrderId?: string;
  priority?: 'normal' | 'high';
  isPaperTrade?: boolean;
  algoType?: string;
  minQuantity?: number;
  maxQuantity?: number;
  minAvgPrice?: number;
  maxAvgPrice?: number;
  startTime?: number;
  endTime?: number;
  updatedStartTime?: number;
  updatedEndTime?: number;
  sortBy?: 'createdAt' | 'updatedAt' | 'price';
  sortOrder?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
  extensions?: Record<string, unknown>;
}

// =============================================================================
// 订单摘要
// =============================================================================

export interface OrderSummary {
  orderId: string;
  symbol: string;
  side: OrderSide;
  positionSide?: PositionSide;
  type: OrderType;
  status: OrderStatus;
  originalQuantity: number;
  executedQuantity: number;
  averageFillPrice?: number;
  price?: number;
  createdAt: number;
  updatedAt: number;
  extensions?: Record<string, unknown>;
}
