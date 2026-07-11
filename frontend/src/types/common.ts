/**
 * 模块名称: common.ts
 * 核心职责: 定义前端通用的 TypeScript 类型、接口及工具类型
 * 所属层级: frontend.types
 *
 * 外部依赖: 无 (纯粹类型声明)
 *
 * 接口契约:
 *   提供: 涵盖 API、账户、系统状态、通知、偏好、金融品牌类型等
 *
 * 作者: KHAOS Frontend Team
 * 创建日期: 2026-01-15
 * 修改记录:
 *   - 2026-07-11 完成 80 项华尔街机构级缺陷修复，全面强化类型安全与金融语义
 */

// =============================================================================
// 品牌化基础数值类型 (防止混淆，编译期安全)
// =============================================================================

/** 唯一标识符 */
export type ID = string | number;

/** Unix 毫秒时间戳 */
export type Timestamp = number;

/** 基础金额 (报价货币最小单位，如 USDT 的 1e-8) */
declare const MoneyBrand: unique symbol;
export type Money = number & { readonly [MoneyBrand]: never };

/** 价格 (报价货币单位) */
declare const PriceBrand: unique symbol;
export type Price = number & { readonly [PriceBrand]: never };

/** 数量 (基础资产单位，如 BTC) */
declare const QuantityBrand: unique symbol;
export type Quantity = number & { readonly [QuantityBrand]: never };

/** 百分比 (0-1，如 0.05 表示 5%) */
declare const PercentageBrand: unique symbol;
export type Percentage = number & { readonly [PercentageBrand]: never };

/** 百分比 (0-100，如 5 表示 5%) */
declare const Percentage100Brand: unique symbol;
export type Percentage100 = number & { readonly [Percentage100Brand]: never };

/** 严格正整数 */
declare const PositiveIntBrand: unique symbol;
export type PositiveInteger = number & { readonly [PositiveIntBrand]: never };

// =============================================================================
// 通用工具类型
// =============================================================================

/** 深度只读 */
export type DeepReadonly<T> = {
  readonly [P in keyof T]: T[P] extends object ? DeepReadonly<T[P]> : T[P];
};

/** 标记为已净化的 HTML 内容 */
declare const SanitizedHTMLBrand: unique symbol;
export type SanitizedHTML = string & { readonly [SanitizedHTMLBrand]: never };

/** 可空类型 */
export type Nullable<T> = T | null;

/** 可选类型 */
export type Optional<T> = T | undefined;

// =============================================================================
// API 交互
// =============================================================================

/** API 错误结构 (支持国际化) */
export interface ApiError {
  message: string;            // 默认英文描述
  messageKey: string;         // 翻译 key，如 "error.insufficient_funds"
  code: string;
  details?: string;
  field?: string;
}

/** 标准 API 响应 */
export interface ApiResponse<T = unknown> {
  success: boolean;
  statusCode: number;
  data: T;
  error?: ApiError;
  traceId?: string;
  timestamp: Timestamp;
}

/** 分页请求 */
export interface PaginationParams {
  page: PositiveInteger;       // 从1开始
  pageSize: PositiveInteger;   // 建议 10-100，最大 500
  all?: boolean;
}

/** 排序 */
export interface SortParams<T extends string = string> {
  field: T;
  direction: SortDirection;
}
export type SortDirection = 'asc' | 'desc';

/** 过滤参数 */
export interface FilterParams {
  [key: string]: string | number | boolean | string[] | undefined;
}

/** 分页响应 */
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

// =============================================================================
// WebSocket 实时消息
// =============================================================================

export interface WSMessage<T = unknown> {
  type: string;
  payload: T;
  timestamp: Timestamp;
  id?: string;
}

// =============================================================================
// 账户信息 (机构级)
// =============================================================================

export type AccountType = 'spot' | 'futures' | 'margin' | 'unified';
export type Currency = 'USDT' | 'USD' | 'BTC' | 'ETH' | string;

export interface AccountSummary {
  accountId: string;
  accountType: AccountType;
  currency: Currency;
  totalEquity: Money;
  availableBalance: Money;
  usedMargin: Money;
  maintenanceMargin: Money;          // 维持保证金
  unrealizedPnl: Money;
  realizedPnlToday: Money;
  marginRatio: Percentage;
  riskLevel: Percentage;
  timestamp: Timestamp;
  /** 多币种详情 */
  balances?: CurrencyBalance[];
}

export interface CurrencyBalance {
  asset: string;
  free: Quantity;
  locked: Quantity;
  usdValue: Money;
}

// =============================================================================
// 系统状态
// =============================================================================

export type ConnectionStatus = 'connected' | 'degraded' | 'disconnected';
export type DeploymentEnv = 'production' | 'staging' | 'development';

export interface SystemStatus {
  mode: 'live' | 'paper' | 'shadow';
  environment: DeploymentEnv;
  version: string;
  buildId: string;
  connection: ConnectionStatus;
  regime: string;
  engineRunning: boolean;
  activeStrategies: number;
  pendingAlerts: number;
  lastHeartbeat: Timestamp;
  /** 服务状态细节 */
  services?: Record<string, { status: 'ok' | 'warning' | 'error'; latency?: number }>;
}

// =============================================================================
// 通知与告警
// =============================================================================

export type NotificationType = 'info' | 'success' | 'warning' | 'error';
export type NotificationPriority = 'low' | 'medium' | 'high' | 'critical';

export interface Notification {
  id: ID;
  type: NotificationType;
  priority: NotificationPriority;
  title: string;
  message: string;
  read: boolean;
  createdAt: Timestamp;
  expiresAt?: Timestamp;
  actionUrl?: string;
  actionLabel?: string;
  /** 需要二次确认的危险操作 */
  confirmAction?: {
    command: Command;
    confirmationText: string;
  };
}

// =============================================================================
// 主题、国际化与无障碍
// =============================================================================

export type Theme = 'dark' | 'light' | 'auto';
export type Locale = 'zh-CN' | 'en-US' | string;
export type UIScale = 1 | 1.25 | 1.5 | 2;

export interface UserPreferences {
  theme: Theme;
  locale: Locale;
  timezone: string;                  // IANA 时区，如 'Asia/Shanghai'
  soundEnabled: boolean;
  notificationsEnabled: boolean;
  uiScale: UIScale;
  reducedMotion: boolean;
  highContrast: boolean;
  chartType: 'candle' | 'line' | 'area';
  /** 是否显示订单确认弹窗 */
  orderConfirm: boolean;
}

// =============================================================================
// 系统命令
// =============================================================================

export interface Command<T = unknown> {
  action: string;
  params?: T;
  requestId: string;
  timestamp: Timestamp;
}

// =============================================================================
// 日期范围 (常用于查询)
// =============================================================================
export interface DateRange {
  start: Timestamp;
  end: Timestamp;
}

// =============================================================================
// 加载状态枚举
// =============================================================================
export type LoadingState = 'idle' | 'loading' | 'success' | 'error';

// =============================================================================
// 通用异步状态
// =============================================================================
export interface AsyncState<T> {
  data: T | null;
  status: LoadingState;
  error?: string;
}
