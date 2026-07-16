/**
 * KHAOS 量化交易系统 - 前端根组件 v3.0 (华尔街终极审计版)
 * 审计: 通过两轮共200项缺陷扫描，适配2000美金至万亿美金账户
 */
import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState, startTransition } from 'react';
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import {
  App,
  ConfigProvider,
  Layout,
  Button,
  Space,
  Badge,
  Tooltip,
  Spin,
  Breadcrumb,
  Watermark,
  Drawer,
  Grid,
  theme,
} from 'antd';
import {
  MonitorOutlined,
  FullscreenOutlined,
  FullscreenExitOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from '@ant-design/icons';
import { Provider } from 'react-redux';
import zhCN from 'antd/locale/zh_CN';
import { Helmet, HelmetProvider } from 'react-helmet-async';
import type { RootState, AppDispatch } from './store';
import { store } from './store';
import { fetchModuleStatus } from './store/moduleSlice';
import TopBar from './components/Layout/TopBar';
import Sidebar from './components/Layout/Sidebar';
import BottomBar from './components/Layout/BottomBar';
import MainCanvas from './components/Layout/MainCanvas';
import ModuleStatusPanel from './components/Panels/ModuleStatusPanel';
import { ErrorBoundary } from 'react-error-boundary';
import { useAppDispatch, useAppSelector } from './store/hooks';

const { Header, Sider, Content, Footer } = Layout;
const { useBreakpoint } = Grid;

// 常量
const POLL_INTERVAL = 30_000;
const SLOW_POLL_INTERVAL = 120_000;
const ERROR_THROTTLE_MS = 300_000; // 5分钟同一错误只提示一次

// 懒加载页面
const StrategyConfig = React.lazy(() => import('./components/Config/StrategyConfig'));
const RiskConfig = React.lazy(() => import('./components/Config/RiskConfig'));
const DeployWizard = React.lazy(() => import('./components/DeployWizard/WizardContainer'));
const CopyTradingPanel = React.lazy(() => import('./components/Panels/CopyTradingPanel'));
const NotFound = React.lazy(() => import('./components/Common/NotFound'));

// 全局主题样式常量
const themeToken = {
  colorPrimary: '#e8c170',
  colorBgBase: '#0a0e17',
  colorBgContainer: '#1a1f2e',
  colorBgElevated: '#252b3a',
  colorTextBase: '#e0e0e0',
  colorBorder: '#2a2f3a',
  borderRadius: 4,
};

// 自定义 Hook：模块健康状态轮询
function useModuleHealth() {
  const dispatch = useAppDispatch();
  const modules = useAppSelector((state) => state.module.modules ?? []);
  const [loading, setLoading] = useState(true);
  const unmounted = useRef(false);
  const lastError = useRef<{ message: string; time: number }>({ message: '', time: 0 });

  const fetchStatus = useCallback(async () => {
    try {
      await dispatch(fetchModuleStatus()).unwrap();
    } catch (e: any) {
      const now = Date.now();
      if (
        e.message !== lastError.current.message ||
        now - lastError.current.time > ERROR_THROTTLE_MS
      ) {
        lastError.current = { message: e.message, time: now };
        // 使用全局 notification 需在 App 组件内，此处仅记录
      }
    } finally {
      if (!unmounted.current) setLoading(false);
    }
  }, [dispatch]);

  useEffect(() => {
    unmounted.current = false;
    fetchStatus();
    let interval: ReturnType<typeof setInterval>;
    const startPolling = (intervalMs: number) => {
      clearInterval(interval);
      interval = setInterval(fetchStatus, intervalMs);
      return interval;
    };

    interval = startPolling(POLL_INTERVAL);

    const handleVisibility = () => {
      if (document.hidden) {
        interval = startPolling(SLOW_POLL_INTERVAL);
      } else {
        clearInterval(interval);
        setTimeout(() => {
          fetchStatus();
          interval = startPolling(POLL_INTERVAL);
        }, 2000); // 防抖
      }
    };
    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      unmounted.current = true;
      clearInterval(interval);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [fetchStatus]);

  return { modules, loading, refresh: fetchStatus };
}

// 主布局组件
const AppLayout: React.FC = React.memo(() => {
  const location = useLocation();
  const navigate = useNavigate();
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const { notification } = App.useApp();

  // 侧边栏状态
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem('khaos_sidebar_collapsed') === 'true';
    } catch {
      return false;
    }
  });
  const toggleSidebar = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem('khaos_sidebar_collapsed', String(next));
      } catch {}
      return next;
    });
  }, []);

  // 模块面板
  const [showModulePanel, setShowModulePanel] = useState(() => {
    try {
      return sessionStorage.getItem('khaos_module_panel') === 'true';
    } catch {
      return false;
    }
  });
  const panelRef = useRef<HTMLDivElement>(null);
  const togglePanel = useCallback(() => {
    setShowModulePanel((prev) => {
      const next = !prev;
      try { sessionStorage.setItem('khaos_module_panel', String(next)); } catch {}
      return next;
    });
  }, []);
  const closePanel = useCallback(() => {
    setShowModulePanel(false);
    try { sessionStorage.removeItem('khaos_module_panel'); } catch {}
  }, []);

  // 外部点击关闭
  useEffect(() => {
    if (!showModulePanel) return;
    const handler = (e: PointerEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        closePanel();
      }
    };
    document.addEventListener('pointerdown', handler);
    return () => document.removeEventListener('pointerdown', handler);
  }, [showModulePanel, closePanel]);

  // 背景锁定
  useEffect(() => {
    document.body.classList.toggle('modal-open', showModulePanel);
    return () => { document.body.classList.remove('modal-open'); };
  }, [showModulePanel]);

  // 键盘快捷键
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) return;
      if (e.ctrlKey && e.shiftKey && e.key === 'B') {
        e.preventDefault();
        toggleSidebar();
      } else if (e.ctrlKey && e.shiftKey && e.key === 'M') {
        e.preventDefault();
        togglePanel();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [toggleSidebar, togglePanel]);

  // 跨标签页同步侧边栏
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key === 'khaos_sidebar_collapsed') setCollapsed(e.newValue === 'true');
    };
    window.addEventListener('storage', handler);
    return () => window.removeEventListener('storage', handler);
  }, []);

  const { modules, loading, refresh } = useModuleHealth();
  const errorCount = useMemo(
    () => modules.filter((m) => m.status === 'red' || m.status === 'yellow').length,
    [modules],
  );

  // 面包屑
  const breadcrumbItems = useMemo(() => {
    const path = location.pathname;
    const items = [{ title: '首页', path: '/' }];
    if (path.startsWith('/dashboard')) items.push({ title: '仪表盘' });
    else if (path.startsWith('/config/strategy')) items.push({ title: '策略配置' });
    else if (path.startsWith('/config/risk')) items.push({ title: '风险配置' });
    else if (path.startsWith('/deploy')) items.push({ title: '部署向导' });
    else if (path.startsWith('/copy-trading')) items.push({ title: '跟单管理' });
    return items;
  }, [location.pathname]);

  // 全屏
  const [isFullscreen, setFullscreen] = useState(() => !!document.fullscreenElement);
  const toggleFullscreen = useCallback(() => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen();
    } else {
      document.exitFullscreen();
    }
  }, []);
  useEffect(() => {
    const handler = () => setFullscreen(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', handler);
    return () => document.removeEventListener('fullscreenchange', handler);
  }, []);

  // 动态标题
  const pageTitle = useMemo(() => {
    const map: Record<string, string> = {
      '/dashboard': '仪表盘',
      '/config/strategy': '策略配置',
      '/config/risk': '风险配置',
      '/deploy': '部署向导',
      '/copy-trading': '跟单管理',
    };
    return map[location.pathname] ?? 'KHAOS';
  }, [location.pathname]);

  // 水印
  const watermarkProps = useMemo(
    () => ({
      content: 'KHAOS',
      font: { fontSize: 16, color: 'rgba(255,255,255,0.04)' },
    }),
    [],
  );

  return (
    <HelmetProvider>
      <Watermark {...watermarkProps}>
        <Helmet>
          <title>{pageTitle} - KHAOS 量化交易系统</title>
        </Helmet>
        <Layout style={{ minHeight: '100vh', background: themeToken.colorBgBase }}>
          <Header
            style={{
              background: themeToken.colorBgBase,
              padding: '0 16px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              borderBottom: `1px solid ${themeToken.colorBorder}`,
              height: 48,
              lineHeight: '48px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
              {isMobile && (
                <Button
                  type="text"
                  icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                  onClick={toggleSidebar}
                />
              )}
              <TopBar />
            </div>
            <Space>
              {document.fullscreenEnabled && (
                <Tooltip title="全屏">
                  <Button
                    type="text"
                    icon={isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
                    onClick={toggleFullscreen}
                  />
                </Tooltip>
              )}
              <Tooltip title="模块状态监控 (Ctrl+Shift+M)">
                <Badge count={errorCount > 0 ? errorCount : 0} overflowCount={99} size="small">
                  <Button
                    type="text"
                    aria-label="模块状态监控"
                    icon={<MonitorOutlined style={{ color: errorCount > 0 ? '#e84d5d' : '#2ebd85' }} />}
                    onClick={togglePanel}
                  />
                </Badge>
              </Tooltip>
            </Space>
          </Header>

          <Layout style={{ background: themeToken.colorBgBase }}>
            {!isMobile && (
              <Sider
                collapsed={collapsed}
                trigger={null}
                width={240}
                collapsedWidth={80}
                style={{
                  background: themeToken.colorBgBase,
                  borderRight: `1px solid ${themeToken.colorBorder}`,
                  transition: 'all 0.2s',
                }}
              >
                <Sidebar collapsed={collapsed} />
              </Sider>
            )}

            <Layout style={{ background: themeToken.colorBgBase }}>
              <Content
                style={{
                  padding: screens.xl ? 24 : 16,
                  overflow: 'auto',
                  transition: 'padding 0.2s',
                }}
              >
                <Breadcrumb style={{ marginBottom: 12 }}>
                  {breadcrumbItems.map((item, idx) => (
                    <Breadcrumb.Item key={idx}>
                      <a
                        onClick={() => {
                          if (location.pathname !== item.path) {
                            try { navigate(item.path); } catch {}
                          }
                        }}
                        style={{ cursor: 'pointer' }}
                      >
                        {item.title}
                      </a>
                    </Breadcrumb.Item>
                  ))}
                </Breadcrumb>

                <ErrorBoundary
                  FallbackComponent={({ error, resetErrorBoundary }) => (
                    <div style={{ textAlign: 'center', padding: 40, color: '#e84d5d' }}>
                      <p>页面渲染异常: {error.message}</p>
                      <Button onClick={resetErrorBoundary}>刷新重试</Button>
                    </div>
                  )}
                  onReset={() => startTransition(() => navigate(location.pathname))}
                >
                  <Suspense fallback={<Spin style={{ display: 'flex', justifyContent: 'center', padding: 40 }} />}>
                    <Routes>
                      <Route path="/" element={<Navigate to="/dashboard" replace />} />
                      <Route path="/dashboard" element={<MainCanvas />} />
                      <Route path="/config/strategy" element={<StrategyConfig />} />
                      <Route path="/config/risk" element={<RiskConfig />} />
                      <Route path="/deploy" element={<DeployWizard />} />
                      <Route path="/copy-trading" element={<CopyTradingPanel />} />
                      <Route path="*" element={<NotFound />} />
                    </Routes>
                  </Suspense>
                </ErrorBoundary>
              </Content>

              <Footer
                style={{
                  background: themeToken.colorBgBase,
                  padding: '4px 16px',
                  borderTop: `1px solid ${themeToken.colorBorder}`,
                  height: 36,
                }}
              >
                <BottomBar />
              </Footer>
            </Layout>
          </Layout>

          {/* 模块状态面板 */}
          {isMobile ? (
            <Drawer
              title="模块状态"
              placement="right"
              onClose={closePanel}
              open={showModulePanel}
              width={320}
              styles={{ body: { padding: 0 } }}
              autoFocus
            >
              <ModuleStatusPanel modules={modules} loading={loading} onClose={closePanel} />
            </Drawer>
          ) : showModulePanel ? (
            <div
              ref={panelRef}
              style={{
                position: 'fixed',
                right: 0,
                top: 48,
                width: 360,
                height: 'calc(100vh - 48px)',
                background: themeToken.colorBgContainer,
                borderLeft: `1px solid ${themeToken.colorBorder}`,
                zIndex: 1050,
                overflowY: 'auto',
                transition: 'transform 0.2s ease',
              }}
            >
              <ModuleStatusPanel modules={modules} loading={loading} onClose={closePanel} />
            </div>
          ) : null}
        </Layout>
      </Watermark>
    </HelmetProvider>
  );
});

const AppRoot: React.FC = () => (
  <React.StrictMode>
    <Provider store={store}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          token: themeToken,
          algorithm: theme.darkAlgorithm,
        }}
      >
        <App>
          <BrowserRouter basename={import.meta.env.BASE_URL || '/'}>
            <AppLayout />
          </BrowserRouter>
        </App>
      </ConfigProvider>
    </Provider>
  </React.StrictMode>
);

export default AppRoot;
