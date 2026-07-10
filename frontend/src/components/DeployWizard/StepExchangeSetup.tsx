import React, { useState, useCallback, useEffect, useRef, useMemo } from 'react';

// ========== 类型定义 ==========
interface ExchangeInfo {
  symbol: string;
  baseAsset: string;
  quoteAsset: string;
  minNotional: number;
  minQty: number;
  stepSize: number;
  tickSize: number;
  currentPrice?: number;
  atr?: number;
  makerFee?: number;
  takerFee?: number;
}

interface RiskCheckResult {
  pass: boolean;
  rawQty: number;
  roundedQty: number;
  actualRisk: number;
  riskBudget: number;
  exceedRisk: boolean;
  minNotionalOk: boolean;
  survivalProbability: number;
  expectedCost: number;
  recommendation: string;
}

interface ExchangeConfig {
  exchange: string;
  apiKey: string;
  secretKey: string;
  symbol: string;
  riskPerTrade: number;
}

interface StepExchangeSetupProps {
  onComplete: (config: ExchangeConfig) => void;
  initialData?: Partial<ExchangeConfig>;
}

// ========== 常量 ==========
const MAX_RETRY_ATTEMPTS = 2;
const RETRY_BASE_DELAY_MS = 1000;
const CONNECTION_TIMEOUT_MS = 10000;
const RISK_CHECK_DEBOUNCE_MS = 500;

// ========== 模拟 API（生产替换为实际请求） ==========
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

const testConnection = async (exchange: string, apiKey: string, secret: string, signal?: AbortSignal) => {
  const timeout = setTimeout(() => { throw new Error('连接超时'); }, CONNECTION_TIMEOUT_MS);
  await delay(800);
  clearTimeout(timeout);
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  if (apiKey.length < 8 || secret.length < 8) {
    throw new Error('API Key 或 Secret 长度不足');
  }
  return { success: true, balances: { USDT: 2000 } };
};

const fetchSymbols = async (exchange: string, signal?: AbortSignal) => {
  await delay(600);
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  return [
    { symbol: 'BTCUSDT', baseAsset: 'BTC', quoteAsset: 'USDT', minNotional: 10, minQty: 0.001, stepSize: 0.001, tickSize: 0.01, currentPrice: 60000, atr: 180, makerFee: 0.0002, takerFee: 0.0004 },
    { symbol: 'ETHUSDT', baseAsset: 'ETH', quoteAsset: 'USDT', minNotional: 10, minQty: 0.01, stepSize: 0.01, tickSize: 0.01, currentPrice: 3000, atr: 80, makerFee: 0.0002, takerFee: 0.0004 },
  ];
};

const checkRisk = async (
  symbol: string,
  balance: number,
  riskPct: number,
  exchangeInfo: ExchangeInfo,
  signal?: AbortSignal
): Promise<RiskCheckResult> => {
  await delay(300);
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  const price = exchangeInfo.currentPrice ?? 60000;
  const atr = exchangeInfo.atr ?? 180;
  const alpha = 2.5;
  const stopDistance = alpha * atr;
  const rawQty = (balance * riskPct) / stopDistance;
  const stepSize = exchangeInfo.stepSize || 0.001;
  const roundedQty = Math.floor(rawQty / stepSize) * stepSize;
  const actualRisk = roundedQty * stopDistance;
  const riskBudget = balance * riskPct;
  const exceedRisk = actualRisk > riskBudget;
  const minNotionalOk = roundedQty * price >= (exchangeInfo.minNotional || 10);
  // 考虑手续费的成本
  const expectedCost = roundedQty * price * ((exchangeInfo.makerFee ?? 0.0002) + (exchangeInfo.takerFee ?? 0.0004));
  const adjustedRisk = actualRisk + expectedCost;
  const survivalProbability = riskBudget > 0 ? Math.min(0.99, Math.exp(-adjustedRisk / riskBudget)) : 0;
  let recommendation = '';
  if (!minNotionalOk) recommendation = '名义价值低于交易所最小限额，建议增加资金或调整风险比例。';
  else if (exceedRisk) recommendation = `止损风险超过预算。建议降低风险比例至 ${((riskBudget / stopDistance / roundedQty) * 100).toFixed(2)}% 或增加资金。`;
  else if (survivalProbability < 0.7) recommendation = '存活概率较低，建议降低仓位或扩大止损。';
  else recommendation = '配置合理，可安全下单。';
  return {
    pass: !exceedRisk && minNotionalOk,
    rawQty,
    roundedQty,
    actualRisk,
    riskBudget,
    exceedRisk,
    minNotionalOk,
    survivalProbability,
    expectedCost,
    recommendation,
  };
};

// ========== 组件 ==========
const StepExchangeSetup: React.FC<StepExchangeSetupProps> = ({ onComplete, initialData }) => {
  // 表单状态
  const [exchange, setExchange] = useState(initialData?.exchange || 'binance');
  const [apiKey, setApiKey] = useState(initialData?.apiKey || '');
  const [secretKey, setSecretKey] = useState(initialData?.secretKey || '');
  const [symbol, setSymbol] = useState(initialData?.symbol || 'BTCUSDT');
  const [riskPct, setRiskPct] = useState(initialData?.riskPerTrade || 1.0);
  const [showSecret, setShowSecret] = useState(false);

  // 连接状态
  const [connectionStatus, setConnectionStatus] = useState<'idle' | 'testing' | 'success' | 'fail'>('idle');
  const [connectionError, setConnectionError] = useState('');
  const [balance, setBalance] = useState(0);
  const [symbols, setSymbols] = useState<ExchangeInfo[]>([]);
  const [selectedSymbolInfo, setSelectedSymbolInfo] = useState<ExchangeInfo | null>(null);

  // 风险状态
  const [riskResult, setRiskResult] = useState<RiskCheckResult | null>(null);
  const [riskLoading, setRiskLoading] = useState(false);
  const [riskError, setRiskError] = useState('');

  const abortRef = useRef<AbortController | null>(null);
  const retryCountRef = useRef(0);

  // 清理
  useEffect(() => () => abortRef.current?.abort(), []);

  // 选中的交易对信息
  useEffect(() => {
    const info = symbols.find(s => s.symbol === symbol);
    setSelectedSymbolInfo(info || null);
  }, [symbol, symbols]);

  // 测试连接（带重试）
  const handleTestConnection = useCallback(async () => {
    if (!apiKey.trim() || !secretKey.trim()) {
      setConnectionStatus('fail');
      setConnectionError('请输入完整的 API 密钥');
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setConnectionStatus('testing');
    setConnectionError('');
    retryCountRef.current = 0;

    const attemptConnection = async (): Promise<void> => {
      try {
        const res = await testConnection(exchange, apiKey, secretKey, controller.signal);
        if (controller.signal.aborted) return;
        setConnectionStatus('success');
        setBalance(res.balances?.USDT ?? 0);
        const syms = await fetchSymbols(exchange, controller.signal);
        if (!controller.signal.aborted) {
          setSymbols(syms);
          if (syms.length > 0 && !symbol) setSymbol(syms[0].symbol);
        }
      } catch (err: any) {
        if (err.name === 'AbortError') return;
        if (retryCountRef.current < MAX_RETRY_ATTEMPTS) {
          retryCountRef.current++;
          const delay = RETRY_BASE_DELAY_MS * Math.pow(2, retryCountRef.current);
          await new Promise(r => setTimeout(r, delay));
          return attemptConnection();
        }
        setConnectionStatus('fail');
        setConnectionError(err.message || '连接失败，请稍后重试');
      }
    };

    attemptConnection();
  }, [apiKey, secretKey, exchange, symbol]);

  // 风险校验（防抖）
  const handleRiskCheck = useCallback(async () => {
    if (!symbol || balance <= 0 || !selectedSymbolInfo) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setRiskLoading(true);
    setRiskError('');
    try {
      const res = await checkRisk(symbol, balance, riskPct / 100, selectedSymbolInfo, controller.signal);
      if (!controller.signal.aborted) setRiskResult(res);
    } catch (err: any) {
      if (err.name === 'AbortError') return;
      setRiskError(err.message || '风险校验失败');
    } finally {
      if (!controller.signal?.aborted) setRiskLoading(false);
    }
  }, [symbol, balance, riskPct, selectedSymbolInfo]);

  // 自动校验（防抖）
  useEffect(() => {
    if (connectionStatus === 'success' && balance > 0 && selectedSymbolInfo) {
      const timer = setTimeout(() => handleRiskCheck(), RISK_CHECK_DEBOUNCE_MS);
      return () => clearTimeout(timer);
    }
  }, [connectionStatus, balance, symbol, riskPct, selectedSymbolInfo, handleRiskCheck]);

  // 完成步骤
  const handleComplete = useCallback(() => {
    if (connectionStatus !== 'success') return;
    if (riskResult && !riskResult.pass) {
      // 风险未通过时给出强提示，但允许继续
      const proceed = window.confirm('风险校验未通过，继续使用当前配置吗？');
      if (!proceed) return;
    }
    onComplete({
      exchange,
      apiKey: apiKey.trim(),
      secretKey: secretKey.trim(),
      symbol,
      riskPerTrade: riskPct,
    });
    // 清除内存中的敏感数据
    setApiKey('');
    setSecretKey('');
  }, [connectionStatus, riskResult, onComplete, exchange, apiKey, secretKey, symbol, riskPct]);

  // 快捷键支持
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === 'Enter') {
        handleComplete();
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [handleComplete]);

  return (
    <div className="wizard-content space-y-6">
      {/* 交易所连接 */}
      <div className="space-y-4">
        <h4 className="text-md font-semibold">🔗 交易所连接</h4>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="form-group">
            <label className="form-label" htmlFor="exchange-select">交易所</label>
            <select id="exchange-select" className="form-input" value={exchange} onChange={e => setExchange(e.target.value)} aria-label="选择交易所">
              <option value="binance">Binance</option>
              <option value="okx">OKX</option>
            </select>
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="api-key-input">API Key</label>
            <input id="api-key-input" type="password" autoComplete="off" className="form-input" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder="输入 API Key" />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="secret-key-input">Secret Key</label>
            <div className="relative">
              <input id="secret-key-input" type={showSecret ? 'text' : 'password'} autoComplete="off" className="form-input pr-10" value={secretKey} onChange={e => setSecretKey(e.target.value)} placeholder="输入 Secret" />
              <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-[var(--color-gold)]" onClick={() => setShowSecret(!showSecret)}>
                {showSecret ? '隐藏' : '显示'}
              </button>
            </div>
          </div>
        </div>
        <div className="flex gap-3 items-center flex-wrap">
          <button className="btn btn-secondary" onClick={handleTestConnection} disabled={connectionStatus === 'testing'}>
            {connectionStatus === 'testing' ? '🔄 测试中...' : '测试连接'}
          </button>
          {connectionStatus === 'testing' && (
            <progress className="w-32" />
          )}
          {connectionStatus === 'fail' && (
            <span className="text-sm text-[var(--color-error)]" role="alert">{connectionError}</span>
          )}
        </div>
        {connectionStatus === 'success' && (
          <div className="alert alert-success">✅ 连接成功 (余额: {balance} USDT)</div>
        )}
      </div>

      {/* 交易对与风险校验 */}
      {connectionStatus === 'success' && (
        <div className="space-y-4">
          <h4 className="text-md font-semibold">📊 交易对与风险校验</h4>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="form-group">
              <label className="form-label" htmlFor="symbol-select">交易对</label>
              <select id="symbol-select" className="form-input" value={symbol} onChange={e => setSymbol(e.target.value)} aria-label="选择交易对">
                {symbols.map(s => (
                  <option key={s.symbol} value={s.symbol}>{s.symbol}</option>
                ))}
              </select>
              {selectedSymbolInfo && (
                <div className="text-xs mt-1 text-[var(--color-text-muted)]">
                  价格: {selectedSymbolInfo.currentPrice} | ATR: {selectedSymbolInfo.atr} | 最小量: {selectedSymbolInfo.minQty}
                </div>
              )}
            </div>
            <div className="form-group">
              <label className="form-label" htmlFor="risk-pct">单笔风险 (%)</label>
              <input id="risk-pct" type="number" className="form-input" value={riskPct} onChange={e => setRiskPct(Math.max(0.1, Number(e.target.value)))} step={0.1} min={0.1} max={10} />
              <div className="text-xs mt-1 text-[var(--color-text-muted)]">
                风险预算: ${((balance * riskPct) / 100).toFixed(2)}
              </div>
            </div>
            <div className="flex items-end">
              <button className="btn btn-primary" onClick={handleRiskCheck} disabled={riskLoading || !selectedSymbolInfo}>
                {riskLoading ? '⏳ 校验中...' : '💰 离散风险校验'}
              </button>
            </div>
          </div>

          {/* 校验结果 */}
          {riskResult && (
            <div className={`alert ${riskResult.pass ? 'alert-success' : 'alert-danger'}`} role="alert" aria-live="polite">
              <p className="font-medium">{riskResult.pass ? '✅ 通过' : '❌ 未通过'}</p>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-2 mt-2 text-sm">
                <div>理论仓位: {riskResult.rawQty.toFixed(6)}</div>
                <div>实际仓位: {riskResult.roundedQty}</div>
                <div>止损风险: ${riskResult.actualRisk.toFixed(2)}</div>
                <div>风险预算: ${riskResult.riskBudget.toFixed(2)}</div>
                <div>预估成本: ${riskResult.expectedCost.toFixed(2)}</div>
                <div>存活概率: {(riskResult.survivalProbability * 100).toFixed(1)}%</div>
              </div>
              <p className="mt-2 text-xs">{riskResult.recommendation}</p>
            </div>
          )}
          {riskError && <div className="alert alert-danger" role="alert">{riskError}</div>}
        </div>
      )}

      {/* 帮助提示 */}
      <div className="bg-[var(--color-dark-surface-hover)] rounded p-3 text-xs text-[var(--color-text-muted)]">
        💡 提示: 使用 <kbd>Ctrl + Enter</kbd> 快速完成设置。
      </div>

      {/* 完成按钮 */}
      <div className="flex justify-end pt-4">
        <button
          className="btn btn-primary btn-lg"
          disabled={connectionStatus !== 'success' || riskLoading}
          onClick={handleComplete}
        >
          确认配置，进入下一步
        </button>
      </div>
    </div>
  );
};

export default StepExchangeSetup;
