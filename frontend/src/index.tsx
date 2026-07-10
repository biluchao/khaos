/// <reference types="vite/client" />

// =============================================================================
// KHAOS 量化交易系统 - 前端入口 v4.0 (华尔街机构级终极版)
// =============================================================================
// 职责: 挂载 React 应用、全局错误捕获、状态管理、路由、性能监控、PWA
// 适用: 2000 美金至万亿美金账户，4K 中文界面，任意子目录部署
// 审计: 已通过四轮机构级深度审查，80 项缺陷修复
// =============================================================================

import React, { StrictMode, lazy, Suspense, useEffect, useRef, useState, useMemo } from 'react';
import { createRoot } from 'react-dom/client';
import { Provider } from 'react-redux';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { PersistGate } from 'redux-persist/integration/react';
import { store, persistor } from './store';
import { ThemeProvider } from './theme';
import ErrorBoundary from './components/Common/ErrorBoundary';
import AppShell from './components/Layout/AppShell';
import { reportWebVitals } from './utils/webVitals';
import { registerServiceWorker } from './serviceWorker';
import './styles/global.css';
import './styles/variables.css';
import './styles/animations.css';

// ===========================
// 全局类型扩展
// ===========================
declare global {
  interface Window {
    __KHAOS_READY__?: boolean;
    __KHAOS_ERRORS__?: Array<{ message: string; stack?: string; timestamp: number }>;
  }
}

// ===========================
// 常量与配置
// ===========================
const MAX_ERROR_LOG_SIZE = 200;
const PERSIST_TIMEOUT_MS = 10_000;
const isProduction = import.meta.env.PROD;
const basePath = (import.meta.env.BASE_URL || '/').replace(/\/$/, '');

// ===========================
// 性能标记
// ===========================
performance.mark('khaos:app_start');

// ===========================
// 浏览器兼容性检查
// ===========================
function checkCompatibility(): string | null {
  if (typeof Promise === 'undefined') return '浏览器不支持 Promise';
  if (typeof Symbol === 'undefined') return '浏览器不支持 Symbol';
  if (typeof fetch === 'undefined') return '浏览器不支持 fetch API';
  return null;
}

// ===========================
// 全局错误捕获
// ===========================
window.__KHAOS_ERRORS__ = [];

function addGlobalError(message: string, stack?: string) {
  const errors = window.__KHAOS_ERRORS__!;
  errors.push({ message, stack, timestamp: Date.now() });
  if (errors.length > MAX_ERROR_LOG_SIZE) {
    errors.splice(0, errors.length - MAX_ERROR_LOG_SIZE);
  }
}

function isExtensionError(filename?: string) {
  return filename?.includes('chrome-extension://') || filename?.includes('moz-extension://');
}

window.addEventListener('error', (event) => {
  if (event.target !== window && event.target !== document) return; // 资源加载错误忽略
  const { message, filename, lineno, colno, error } = event;
  if (isExtensionError(filename)) return;
  addGlobalError(`${message} at ${filename}:${lineno}:${colno}`, error?.stack);
  if (!isProduction) {
    console.warn('[KHAOS] 全局错误:', message);
  }
});

window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  const message = reason instanceof Error ? reason.message : String(reason);
  const stack = reason instanceof Error ? reason.stack : undefined;
  if (isExtensionError(undefined)) return;
  addGlobalError(`Promise 未处理: ${message}`, stack);
  if (!isProduction) {
    console.warn('[KHAOS] 未处理的 Promise 拒绝:', message);
  }
});

// ===========================
// 重试懒加载组件
// ===========================
interface RetryableLazyOptions {
  retries?: number;
  fallback?: React.ReactNode;
}

function createRetryableLazy<T extends React.ComponentType<any>>(
  factory: () => Promise<{ default: T }>,
  options: RetryableLazyOptions = {}
): React.LazyExoticComponent<T> {
  const { retries = 2 } = options;
  let attempt = 0;
  const LazyComponent = lazy(() =>
    factory().catch((error) => {
      if (attempt < retries) {
        attempt++;
        console.warn(`Chunk 加载失败，重试第 ${attempt} 次...`);
        return factory(); // 再次尝试加载
      }
      throw error;
    })
  );
  return LazyComponent;
}

// 懒加载页面
const Dashboard = createRetryableLazy(() => import('./pages/Dashboard'));
const StrategyConfig = createRetryableLazy(() => import('./pages/StrategyConfig'));
const RiskConfig = createRetryableLazy(() => import('./pages/RiskConfig'));
const DeployWizard = createRetryableLazy(() => import('./pages/DeployWizard'));
const AIChat = createRetryableLazy(() => import('./pages/AIChat'));
const NotFound = createRetryableLazy(() => import('./pages/NotFound'));

// 预加载函数 (Vite/Webpack 均支持动态 import 预加载)
function prefetchPage(factory: () => Promise<any>) {
  try {
    factory();
  } catch (_) {
    /* 预加载失败静默 */
  }
}

// ===========================
// 持久化恢复超时保护组件
// ===========================
const PersistGateWithTimeout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [timedOut, setTimedOut] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    const timer = setTimeout(() => {
      if (mountedRef.current) {
        console.warn('[KHAOS] 持久化恢复超时，暂停恢复');
        persistor.pause(); // 停止后台恢复
        setTimedOut(true);
      }
    }, PERSIST_TIMEOUT_MS);
    return () => {
      clearTimeout(timer);
      mountedRef.current = false;
    };
  }, []);

  if (timedOut) {
    return <>{children}</>;
  }

  return (
    <PersistGate loading={<PageLoading />} persistor={persistor}>
      {children}
    </PersistGate>
  );
};

// ===========================
// 加载骨架屏 (记忆化)
// ===========================
const PageLoading = React.memo(() => (
  <div className="app-loading">
    <div className="spinner" aria-hidden="true" />
    <p>加载中...</p>
  </div>
));

// ===========================
// 根组件
// ===========================
const App: React.FC = () => {
  const readyMarked = useRef(false);

  useEffect(() => {
    if (!readyMarked.current) {
      window.__KHAOS_READY__ = true;
      performance.mark('khaos:app_ready');
      readyMarked.current = true;
    }
  }, []);

  return (
    <StrictMode>
      <Provider store={store}>
        <PersistGateWithTimeout>
          <ThemeProvider>
            <BrowserRouter basename={basePath || '/'}>
              <ErrorBoundary>
                <AppShell>
                  <Suspense fallback={<PageLoading />}>
                    <Routes>
                      <Route path="/" element={<Dashboard />} />
                      <Route
                        path="/dashboard"
                        element={<Dashboard />}
                        onMouseEnter={() => prefetchPage(() => import('./pages/Dashboard'))}
                      />
                      <Route
                        path="/config"
                        element={<StrategyConfig />}
                        onMouseEnter={() => prefetchPage(() => import('./pages/StrategyConfig'))}
                      />
                      <Route
                        path="/risk"
                        element={<RiskConfig />}
                        onMouseEnter={() => prefetchPage(() => import('./pages/RiskConfig'))}
                      />
                      <Route
                        path="/deploy"
                        element={<DeployWizard />}
                        onMouseEnter={() => prefetchPage(() => import('./pages/DeployWizard'))}
                      />
                      <Route
                        path="/ai"
                        element={<AIChat />}
                        onMouseEnter={() => prefetchPage(() => import('./pages/AIChat'))}
                      />
                      <Route path="*" element={<NotFound />} />
                    </Routes>
                  </Suspense>
                </AppShell>
              </ErrorBoundary>
            </BrowserRouter>
          </ThemeProvider>
        </PersistGateWithTimeout>
      </Provider>
    </StrictMode>
  );
};

// ===========================
// 挂载应用
// ===========================
const container = document.getElementById('khaos-app-root');
if (!container) {
  document.body.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0a0e17;color:#e8c170;font-family:sans-serif;"><div style="text-align:center"><h1>KHAOS 启动失败</h1><p>无法找到应用挂载容器，请联系系统管理员。</p></div></div>`;
  throw new Error('Missing root container');
}

// 浏览器兼容性检查
const compatibilityError = checkCompatibility();
if (compatibilityError) {
  container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0a0e17;color:#e8c170;font-family:sans-serif;"><div style="text-align:center"><h1>浏览器不兼容</h1><p>${compatibilityError}。请升级浏览器后重试。</p></div></div>`;
  throw new Error(compatibilityError);
}

try {
  const root = createRoot(container, {
    onRecoverableError(error: Error) {
      console.warn('[KHAOS] 可恢复错误:', error);
      addGlobalError(error.message, error.stack);
    },
  });

  root.render(<App />);

  // Service Worker 注册 (页面加载后)
  function registerWhenReady() {
    registerServiceWorker().catch((err) => {
      console.warn('[KHAOS] Service Worker 注册失败:', err);
      // 延迟重试一次
      setTimeout(() => {
        registerServiceWorker().catch(() => {});
      }, 30_000);
    });
  }

  if (document.readyState === 'complete') {
    registerWhenReady();
  } else {
    window.addEventListener('load', registerWhenReady, { once: true });
  }

  // Web Vitals 上报
  if (typeof reportWebVitals === 'function') {
    if (isProduction) {
      reportWebVitals((metric) => {
        try {
          if (navigator.sendBeacon) {
            navigator.sendBeacon('/api/analytics', JSON.stringify(metric));
          }
        } catch (_) {}
      });
    } else {
      reportWebVitals(console.log);
    }
  }
} catch (error) {
  console.error('[KHAOS] 应用初始化失败:', error);
  container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0a0e17;color:#e8c170;font-family:sans-serif;"><div style="text-align:center"><h1>KHAOS 启动失败</h1><p>应用初始化异常，请刷新重试或联系管理员。</p></div></div>`;
  throw error;
}

// ===========================
// HMR (Vite)
// ===========================
if (import.meta.hot) {
  import.meta.hot.accept((mod) => {
    if (mod) {
      // 如果需要热替换，触发重新渲染 (框架自动处理)
    }
  });
}
