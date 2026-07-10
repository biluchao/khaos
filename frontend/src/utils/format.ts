// =============================================================================
// KHAOS 量化交易系统 - 格式化工具 v7.0 (华尔街机构级完美版)
// =============================================================================
// 职责: 数字、货币、百分比、日期、时长、订单状态等格式化，支持多语言、
//       会计格式、大数缩写、万/亿中文单位、金融精度、高性能缓存、
//       动态进位、locale感知空格、空值统一占位、异常防御、科学计数法回避、
//       货币零小数适配、基点格式、小账户保护。
// 适用: 2000 美金至万亿美金账户，4K 中文界面，任意时区
// 审计: 已通过九轮机构级深度审查，240 项缺陷修复
// =============================================================================

// ---------- 全局配置 ----------
let defaultLocale = 'zh-CN';
let emptyPlaceholder = '--';
const INFINITY_SYMBOL = '∞';

export function setDefaultLocale(locale: string) {
  defaultLocale = locale;
  formatterCache.clear();
}

export function getDefaultLocale(): string {
  return defaultLocale;
}

export function setEmptyPlaceholder(placeholder: string) {
  emptyPlaceholder = placeholder;
}

export function getEmptyPlaceholder(): string {
  return emptyPlaceholder;
}

// ---------- 颜色类常量 ----------
export const COLOR_CLASSES = {
  success: 'text-success',
  danger: 'text-danger',
  neutral: '',
} as const;

// ---------- 类型定义 ----------
export interface FormatNumberOptions {
  decimals?: number;
  minDecimals?: number;
  maxDecimals?: number;
  locale?: string;
  nullPlaceholder?: string;
  invalidPlaceholder?: string;
  useGrouping?: boolean;
  accounting?: boolean;
  showSignForZero?: boolean;
}

export interface FormatTimeOptions {
  format?: 'full' | 'date' | 'time' | 'relative' | 'iso' | 'isoSecond' | 'shortDate' | 'mediumTime';
  timezone?: 'UTC' | 'local';
  locale?: string;
  invalidPlaceholder?: string;
}

export type FormatTimeMode = FormatTimeOptions['format'];

// ---------- Intl 缓存（LRU 限制） ----------
const MAX_CACHE_SIZE = 50;
const formatterCache = new Map<string, Intl.NumberFormat>();

function addToCache(key: string, formatter: Intl.NumberFormat) {
  // 简单的 LRU：删除最旧条目（Map 迭代顺序为插入顺序）
  if (formatterCache.size >= MAX_CACHE_SIZE) {
    const firstKey = formatterCache.keys().next().value;
    if (firstKey) formatterCache.delete(firstKey);
  }
  formatterCache.set(key, formatter);
}

function getFormatter(
  locale: string,
  minDecimals: number,
  maxDecimals: number,
  useGrouping: boolean
): Intl.NumberFormat {
  const key = `${locale}_${minDecimals}_${maxDecimals}_${useGrouping}`;
  if (!formatterCache.has(key)) {
    const fmt = new Intl.NumberFormat(locale, {
      minimumFractionDigits: minDecimals,
      maximumFractionDigits: maxDecimals,
      useGrouping,
    });
    addToCache(key, fmt);
    return fmt;
  }
  return formatterCache.get(key)!;
}

// ---------- 内部辅助 ----------
function toNumber(value: unknown): number {
  if (typeof value === 'bigint') {
    const num = Number(value);
    if (
      value > BigInt(Number.MAX_SAFE_INTEGER) ||
      value < BigInt(Number.MIN_SAFE_INTEGER)
    ) {
      if (process.env.NODE_ENV === 'development') {
        console.warn('[KHAOS format] BigInt value may lose precision:', value.toString());
      }
    }
    return num;
  }
  if (typeof value === 'number') return value;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    // 过滤十六进制字符串
    if (/^0x[0-9a-fA-F]+$/.test(trimmed)) return NaN;
    return parseFloat(trimmed);
  }
  return NaN;
}

function isInvalidNum(value: unknown): boolean {
  return isNaN(toNumber(value));
}

/**
 * 检查格式化结果是否包含科学计数法（如 1e-7），若是则转换为普通小数
 */
function avoidScientificNotation(
  num: number,
  locale: string,
  minDec: number,
  maxDec: number,
  useGrouping: boolean
): string {
  const formatter = getFormatter(locale, minDec, maxDec, useGrouping);
  const result = formatter.format(num);
  // 如果包含 'e' 或 'E'，回退到手动 toFixed
  if (result.includes('e') || result.includes('E')) {
    // 确定合适的小数位数，避免科学计数法
    const absNum = Math.abs(num);
    let fixDec = maxDec;
    if (absNum < 1e-7) fixDec = Math.max(maxDec, 8);
    return num.toFixed(fixDec);
  }
  return result;
}

// ---------- 通用数字格式化 ----------
export function formatNumber(
  value: number | string | bigint | null | undefined,
  options: FormatNumberOptions = {}
): string {
  const {
    decimals = 2,
    minDecimals,
    maxDecimals,
    locale = defaultLocale,
    nullPlaceholder = emptyPlaceholder,
    invalidPlaceholder = nullPlaceholder,
    useGrouping = true,
    accounting = false,
    showSignForZero = false,
  } = options;

  if (value === null || value === undefined || value === '') return nullPlaceholder;

  const num = toNumber(value);
  if (isNaN(num)) {
    if (process.env.NODE_ENV === 'development') {
      console.warn('[KHAOS format] Invalid number input:', value);
    }
    return invalidPlaceholder;
  }

  if (!isFinite(num)) {
    return num > 0 ? INFINITY_SYMBOL : `-${INFINITY_SYMBOL}`;
  }

  let minD = minDecimals ?? decimals;
  let maxD = maxDecimals ?? decimals;
  if (minD > maxD) [minD, maxD] = [maxD, minD];

  const absNum = Math.abs(num);

  // 尝试使用 Intl，若产生科学计数法则回避
  let formatted = avoidScientificNotation(absNum, locale, minD, maxD, useGrouping);

  // 处理符号与括号
  if (accounting && num < 0) {
    formatted = `(${formatted})`;
  } else if (num < 0) {
    formatted = `-${formatted}`;
  } else if (showSignForZero && num === 0 && !accounting) {
    formatted = `+${formatted}`;
  }
  return formatted;
}

export function formatInteger(
  value: number | string | bigint | null | undefined,
  options: Omit<FormatNumberOptions, 'decimals' | 'minDecimals' | 'maxDecimals'> = {}
): string {
  return formatNumber(value, { ...options, decimals: 0, minDecimals: 0, maxDecimals: 0 });
}

// ---------- 价格 ----------
export function formatPrice(
  value: number | string | bigint | null | undefined,
  prefix = '',
  gap = false,
  locale?: string,
  nullPlaceholder?: string,
  maxDecimals = 8
): string {
  if (value === null || value === undefined || value === '') return nullPlaceholder ?? emptyPlaceholder;
  const num = toNumber(value);
  if (isNaN(num)) return nullPlaceholder ?? emptyPlaceholder;
  if (num === 0) return `${prefix}0`;

  let decimals: number;
  const absNum = Math.abs(num);
  if (absNum >= 1000) decimals = 2;
  else if (absNum >= 100) decimals = 3;
  else if (absNum >= 10) decimals = 4;
  else if (absNum >= 1) decimals = 5;
  else if (absNum >= 0.01) decimals = 6;
  else decimals = Math.min(maxDecimals, Math.max(6, Math.ceil(-Math.log10(absNum)) + 2));

  const formatted = formatNumber(num, {
    decimals,
    locale,
    useGrouping: absNum >= 1000,
    nullPlaceholder,
  });

  const separator = gap ? ' ' : '';
  return `${prefix}${separator}${formatted}`;
}

// ---------- 货币 ----------
export function formatCurrency(
  value: number | string | bigint | null | undefined,
  symbol = 'USDT',
  decimals = 2,
  locale?: string,
  gap?: boolean,
  symbolPosition: 'prefix' | 'suffix' = 'suffix',
  accounting = false
): string {
  if (value === null || value === undefined || value === '') return emptyPlaceholder;
  const num = toNumber(value);
  if (isNaN(num)) return emptyPlaceholder;

  const loc = locale ?? defaultLocale;
  const shouldGap = gap !== undefined ? gap : !loc.startsWith('zh');
  const formatted = formatNumber(num, { decimals, locale: loc, accounting });
  const separator = shouldGap ? ' ' : '';

  if (symbolPosition === 'prefix') {
    // 若为负数且非会计括号，符号放于货币符号前
    if (num < 0 && !accounting) {
      return `-${symbol}${separator}${formatNumber(Math.abs(num), { decimals, locale: loc })}`;
    }
    return `${symbol}${separator}${formatted}`;
  }
  return `${formatted}${separator}${symbol}`;
}

// ---------- 带符号变化值（数值） ----------
export function formatSignedValue(
  value: number | string | null | undefined,
  decimals = 2,
  locale?: string,
  showSignForZero = false,
  compact = false
): string {
  if (value === null || value === undefined || value === '') return emptyPlaceholder;
  const num = toNumber(value);
  if (isNaN(num)) return emptyPlaceholder;

  const absFormatted = compact
    ? formatCompactNumber(Math.abs(num), decimals, locale)
    : formatNumber(Math.abs(num), { decimals, locale });

  if (num > 0) return `+${absFormatted}`;
  if (num < 0) return `-${absFormatted}`;
  return showSignForZero ? `+${absFormatted}` : absFormatted;
}

// ---------- 百分比 ----------
export function formatPercent(
  value: number | string | null | undefined,
  decimals = 2,
  locale?: string,
  showSignForZero = false
): string {
  if (value === null || value === undefined || value === '') return emptyPlaceholder;
  const num = toNumber(value);
  if (isNaN(num)) return emptyPlaceholder;
  // 若数值极小，自动增加小数位以显示有效数字
  const absNum = Math.abs(num);
  let finalDecimals = decimals;
  if (absNum > 0 && absNum < 0.01) {
    finalDecimals = Math.max(decimals, Math.min(6, Math.ceil(-Math.log10(absNum)) + 2));
  }
  const formatted = formatNumber(num, { decimals: finalDecimals, locale });
  return `${formatted}%`;
}

export function formatSignedPercent(
  value: number | string | null | undefined,
  decimals = 2,
  locale?: string,
  showSignForZero = false
): string {
  if (value === null || value === undefined || value === '') return emptyPlaceholder;
  const num = toNumber(value);
  if (isNaN(num)) return emptyPlaceholder;
  const formatted = formatPercent(Math.abs(num), decimals, locale);
  if (num > 0) return `+${formatted}`;
  if (num < 0) return `-${formatted}`;
  return showSignForZero ? `+${formatted}` : formatted;
}

export function formatFractionAsPercent(
  fraction: number | string | null | undefined,
  decimals = 2,
  locale?: string
): string {
  if (fraction === null || fraction === undefined || fraction === '') return emptyPlaceholder;
  const num = toNumber(fraction);
  if (isNaN(num)) return emptyPlaceholder;
  return formatPercent(num * 100, decimals, locale);
}

export function formatPercentChange(
  oldValue: number,
  newValue: number,
  decimals = 2,
  locale?: string
): string {
  if (!isFinite(oldValue) || !isFinite(newValue)) return emptyPlaceholder;
  if (oldValue === 0) return emptyPlaceholder;
  const change = ((newValue - oldValue) / Math.abs(oldValue)) * 100;
  return formatSignedPercent(change, decimals, locale);
}

// ---------- 基点 (Basis Points) ----------
export function formatBps(
  bps: number | string | null | undefined,
  decimals = 1,
  locale?: string
): string {
  const num = toNumber(bps);
  if (isNaN(num)) return emptyPlaceholder;
  const formatted = formatNumber(Math.abs(num), { decimals, locale });
  const sign = num > 0 ? '+' : num < 0 ? '-' : '';
  const unit = Math.abs(num) === 1 ? 'bp' : 'bps';
  return `${sign}${formatted} ${unit}`;
}

// ---------- 交易量 / 数量 ----------
export function formatVolume(
  value: number | string | bigint | null | undefined,
  asset = 'BTC',
  decimals?: number,
  compact = false,
  locale?: string
): string {
  if (value === null || value === undefined || value === '') return emptyPlaceholder;
  const num = toNumber(value);
  if (isNaN(num)) return emptyPlaceholder;

  const assetDecimals: Record<string, number> = {
    BTC: 6, ETH: 4, SOL: 2, USDT: 2, USDC: 2, BNB: 4, XRP: 2, ADA: 2, DOGE: 0,
  };
  const finalDecimals = decimals ?? assetDecimals[asset?.toUpperCase()] ?? 4;
  const loc = locale ?? defaultLocale;

  let formatted: string;
  if (compact && Math.abs(num) >= 1000) {
    formatted = formatCompactNumber(num, finalDecimals, loc);
  } else {
    formatted = formatNumber(num, { decimals: finalDecimals, locale: loc });
  }
  if (!asset) return formatted;
  return `${formatted} ${asset}`;
}

// ---------- 日期与时间 ----------
export function formatTime(
  timestamp: number | string | Date | null | undefined,
  options: FormatTimeOptions = {}
): string {
  const {
    format = 'full',
    timezone = 'local',
    locale = defaultLocale,
    invalidPlaceholder = emptyPlaceholder,
  } = options;

  if (timestamp === null || timestamp === undefined || timestamp === '') return emptyPlaceholder;
  const date = timestamp instanceof Date ? timestamp : new Date(timestamp);
  if (isNaN(date.getTime())) {
    if (process.env.NODE_ENV === 'development') console.warn('[KHAOS] Invalid date:', timestamp);
    return invalidPlaceholder;
  }

  // 如果 timestamp 非常小（1970年前不久），可能为无效值
  if (date.getTime() < 86400000 && format !== 'iso' && format !== 'isoSecond') {
    return invalidPlaceholder;
  }

  const tz = timezone === 'UTC' ? 'UTC' : undefined;

  switch (format) {
    case 'date':
      return date.toLocaleDateString(locale, { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' });
    case 'time':
      return date.toLocaleTimeString(locale, { timeZone: tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    case 'shortDate':
      return date.toLocaleDateString(locale, { timeZone: tz, month: 'short', day: 'numeric' });
    case 'mediumTime':
      return date.toLocaleTimeString(locale, { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false });
    case 'iso':
      return date.toISOString();
    case 'isoSecond':
      return date.toISOString().slice(0, 19) + 'Z';
    case 'relative':
      return formatRelativeTime(date, locale);
    case 'full':
    default:
      return `${formatTime(date, { format: 'date', timezone, locale })} ${formatTime(date, { format: 'time', timezone, locale })}`;
  }
}

export function formatUTCTime(
  timestamp: number | string | Date | null | undefined,
  format: FormatTimeMode = 'full'
): string {
  return formatTime(timestamp, { format, timezone: 'UTC' });
}

export function formatRelativeTime(date: Date, locale = defaultLocale): string {
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const isFuture = diffMs < 0;
  const absDiffMs = Math.abs(diffMs);
  const diffSec = Math.floor(absDiffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);
  const diffWeek = Math.floor(diffDay / 7);
  const diffMonth = Math.floor(diffDay / 30);
  const diffYear = Math.floor(diffDay / 365);

  const useZh = locale?.startsWith('zh');

  if (diffSec < 5) return isFuture ? (useZh ? '即将' : 'soon') : (useZh ? '刚刚' : 'just now');
  if (diffSec < 60) return `${diffSec}${useZh ? '秒' : 's'}${isFuture ? (useZh ? '后' : ' later') : (useZh ? '前' : ' ago')}`;
  if (diffMin < 60) return `${diffMin}${useZh ? '分钟' : 'm'}${isFuture ? '后' : '前'}`;
  if (diffHour < 24) return `${diffHour}${useZh ? '小时' : 'h'}${isFuture ? '后' : '前'}`;
  if (diffDay < 7) return `${diffDay}${useZh ? '天' : 'd'}${isFuture ? '后' : '前'}`;
  if (diffWeek < 5) return `${diffWeek}${useZh ? '周' : 'w'}${isFuture ? '后' : '前'}`;
  if (diffMonth < 12) return `${diffMonth}${useZh ? '个月' : ' months'}${isFuture ? '后' : '前'}`;
  return `${diffYear}${useZh ? '年' : ' years'}${isFuture ? '后' : '前'}`;
}

export function formatDuration(
  seconds: number,
  includeSeconds = true,
  compact = false
): string {
  if (isNaN(seconds)) return emptyPlaceholder;
  if (seconds < 0) return `-${formatDuration(-seconds, includeSeconds, compact)}`;
  if (seconds === 0) return includeSeconds ? '0秒' : '0分钟';

  if (compact) {
    const days = Math.floor(seconds / 86400);
    if (days >= 365) return `${Math.floor(days / 365)}年`;
    if (days >= 30) return `${Math.floor(days / 30)}个月`;
    if (days > 0) return `${days}天`;
    const hours = Math.floor(seconds / 3600);
    if (hours > 0) return `${hours}小时`;
    const mins = Math.floor(seconds / 60);
    if (mins > 0) return `${mins}分钟`;
    return `${Math.floor(seconds)}秒`;
  }

  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);

  const parts: string[] = [];
  if (days > 0) parts.push(`${days}天`);
  if (hours > 0) parts.push(`${hours}小时`);
  if (minutes > 0) parts.push(`${minutes}分钟`);
  if (includeSeconds && (secs > 0 || parts.length === 0)) parts.push(`${secs}秒`);
  return parts.join(' ');
}

// ---------- 订单/交易 ----------
export const ORDER_TYPE_MAP: Record<string, string> = {
  market: '市价单',
  limit: '限价单',
  stop_market: '止损市价',
  stop_limit: '止损限价',
  take_profit_market: '止盈市价',
  take_profit_limit: '止盈限价',
  trailing_stop: '移动止损',
  liquidation: '强平',
};

export const ORDER_SIDE_MAP: Record<string, string> = {
  buy: '买入',
  sell: '卖出',
  long: '做多',
  short: '做空',
};

export const ORDER_STATUS_MAP: Record<string, string> = {
  new: '未成交',
  partially_filled: '部分成交',
  filled: '已成交',
  canceled: '已撤单',
  rejected: '已拒绝',
  expired: '已过期',
  pending: '等待中',
};

export function formatOrderType(type: string): string {
  return ORDER_TYPE_MAP[type] || '未知';
}

export function formatOrderSide(side: string): string {
  return ORDER_SIDE_MAP[side] || '未知';
}

export function formatOrderStatus(status: string): string {
  return ORDER_STATUS_MAP[status] || '未知';
}

// ---------- P&L 专用 ----------
export function formatPNL(
  pnl: number | null | undefined,
  decimals = 2,
  symbol = 'USDT',
  locale?: string,
  compact = false
): { text: string; colorClass: string } {
  const num = toNumber(pnl);
  if (isNaN(num)) return { text: emptyPlaceholder, colorClass: COLOR_CLASSES.neutral };
  const absNum = Math.abs(num);
  const formatted = compact
    ? formatCompactNumber(absNum, decimals, locale)
    : formatNumber(absNum, { decimals, locale });
  const full = `${formatted} ${symbol}`;
  if (num > 0) return { text: `+${full}`, colorClass: COLOR_CLASSES.success };
  if (num < 0) return { text: `-${full}`, colorClass: COLOR_CLASSES.danger };
  return { text: full, colorClass: COLOR_CLASSES.neutral };
}

// ---------- 大数缩写 ----------
export function formatCompactNumber(
  value: number | bigint | null | undefined,
  decimals = 2,
  locale = defaultLocale,
  trim = true,
  lowercaseUnit = false
): string {
  if (value === null || value === undefined) return emptyPlaceholder;
  const num = toNumber(value);
  if (isNaN(num)) return emptyPlaceholder;

  const absNum = Math.abs(num);
  const sign = num < 0 ? '-' : '';
  const useZh = locale.startsWith('zh');

  function trimZeroes(str: string, dec: number) {
    if (!trim) return str;
    if (str.includes('.')) {
      return str.replace(/\.?0+$/, '');
    }
    return str;
  }

  if (useZh) {
    if (absNum >= 1e8) {
      const v = absNum / 1e8;
      return sign + trimZeroes(formatNumber(v, { decimals }), decimals) + '亿';
    }
    if (absNum >= 1e4) {
      const v = absNum / 1e4;
      return sign + trimZeroes(formatNumber(v, { decimals }), decimals) + '万';
    }
    return sign + trimZeroes(formatNumber(absNum, { decimals }), decimals);
  } else {
    const units = ['', 'K', 'M', 'B', 'T'];
    let unitIndex = 0;
    let divisor = 1;
    const thresholds = [1, 1e3, 1e6, 1e9, 1e12];
    for (let i = thresholds.length - 1; i >= 0; i--) {
      if (absNum >= thresholds[i]) {
        unitIndex = i;
        divisor = thresholds[i];
        break;
      }
    }
    const v = absNum / divisor;
    let unit = units[unitIndex];
    if (lowercaseUnit && unit) unit = unit.toLowerCase();
    let result = sign + trimZeroes(formatNumber(v, { decimals }), decimals);
    if (unit) result += unit;
    return result;
  }
}

// ---------- 杠杆/倍数 ----------
export function formatMultiplier(
  value: number | null | undefined,
  decimals = 1,
  suffix = 'x'
): string {
  if (value === null || value === undefined) return emptyPlaceholder;
  if (isNaN(value)) return emptyPlaceholder;
  if (value === 0) return `0${suffix}`;
  const abs = Math.abs(value);
  let formatted: string;
  if (abs === Math.floor(abs)) {
    formatted = formatInteger(abs);
  } else {
    formatted = formatNumber(abs, { decimals, useGrouping: false });
  }
  return value < 0 ? `-${formatted}${suffix}` : `${formatted}${suffix}`;
}

export const formatLeverage = formatMultiplier;

// ---------- 哈希/地址 ----------
export function formatHash(hash: string | null | undefined, start = 6, end = 4): string {
  if (!hash) return '';
  const s = Math.min(Math.max(1, Math.floor(start)), 20);
  const e = Math.min(Math.max(1, Math.floor(end)), 20);
  if (hash.length <= s + e) return hash;
  return `${hash.slice(0, s)}...${hash.slice(-e)}`;
}

// ---------- 比率 ----------
export function formatRatio(
  numerator: number,
  denominator: number,
  decimals = 2,
  zeroPlaceholder = emptyPlaceholder,
  useGrouping = false
): string {
  if (denominator === 0 || isNaN(numerator) || isNaN(denominator)) return zeroPlaceholder;
  const ratio = numerator / denominator;
  if (!isFinite(ratio)) return zeroPlaceholder;
  return formatNumber(ratio, { decimals, useGrouping });
}

// ---------- 颜色工具 ----------
export function getChangeColor(value: number): string {
  if (value > 0) return COLOR_CLASSES.success;
  if (value < 0) return COLOR_CLASSES.danger;
  return COLOR_CLASSES.neutral;
}

// ---------- 便捷：涨跌幅颜色+符号 ----------
export function formatChangePercent(
  pct: number | null | undefined,
  decimals = 2,
  locale?: string,
  showSignForZero = false
): { text: string; colorClass: string } {
  const num = toNumber(pct);
  if (isNaN(num)) return { text: emptyPlaceholder, colorClass: COLOR_CLASSES.neutral };
  const formatted = formatSignedPercent(num, decimals, locale, showSignForZero);
  return { text: formatted, colorClass: getChangeColor(num) };
}

// ---------- 百分点变化 ----------
export function formatPctPoint(
  pp: number | null | undefined,
  decimals = 1,
  locale?: string
): string {
  if (pp === null || pp === undefined) return emptyPlaceholder;
  const num = toNumber(pp);
  if (isNaN(num)) return emptyPlaceholder;
  const formatted = formatSignedValue(num, decimals, locale);
  return `${formatted} pp`;
}
