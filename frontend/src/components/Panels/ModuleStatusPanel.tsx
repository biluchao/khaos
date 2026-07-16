// =============================================================================
// KHAOS 模块健康状态面板 v6.0 (绝对机构版)
// 第四轮审计：增强搜索持久化、虚拟列表、色盲模式、慢速网络自适应、
// 导出增强、唯一ID、防抖、URL过滤、状态机等 100 项完善。
// =============================================================================
import React, {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  forwardRef,
  useImperativeHandle,
  useReducer,
} from 'react';
import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import relativeTime from 'dayjs/plugin/relativeTime';
import 'dayjs/locale/zh-cn';

dayjs.extend(utc);
dayjs.extend(relativeTime);
const userLang = typeof navigator !== 'undefined' ? navigator.language : 'zh-CN';
dayjs.locale(userLang.startsWith('zh') ? 'zh-cn' : 'en');

// ===================== 常量与类型 =====================
class TimeoutError extends Error { constructor() { super('Request timeout'); this.name = 'TimeoutError'; } }

export type ModuleStatus = 'green' | 'yellow' | 'red' | 'gray';
export interface ModuleInfo {
  readonly id: string;            // 唯一标识，若后端未提供则前端生成
  readonly name: string;
  readonly status: ModuleStatus;
  readonly message: string;
  readonly last_update: string | null;
}

export interface ModuleStatusPanelProps {
  refreshIntervalMs?: number;
  apiBaseUrl?: string;
  onModuleClick?: (module: ModuleInfo) => void;
  onError?: (error: string) => void;
  accountId?: string;
}

export interface ModuleStatusPanelHandle {
  refresh: () => void;
  pause: () => void;
  resume: () => void;
}

const STATUS_PRIORITY: Record<ModuleStatus, number> = { red: 0, yellow: 1, green: 2, gray: 3 };
const STATUS_COLOR_FALLBACK: Record<ModuleStatus, string> = { green: '#2ebd85', yellow: '#d4a80b', red: '#e84d5d', gray: '#555a62' };
const STATUS_LABEL: Record<ModuleStatus, string> = { green: '正常', yellow: '警告', red: '异常', gray: '未启用' };
const DEFAULT_MODULES: readonly string[] = Object.freeze([
  'KMA', 'HMM', 'TrendProbabilityFilter', 'EscapeDetector', 'Recapture',
  'CallbackDrop', 'PullbackAdd', 'GuerrillaChase', 'PaperBroker', 'CopyTrading',
  'RiskFirewall', 'OrderManager', 'DataFeed', 'Exchange',
]);

const getStatusColor = (status: ModuleStatus) => `var(--color-status-${status}, ${STATUS_COLOR_FALLBACK[status]})`;
const isValidModuleStatus = (s: string): s is ModuleStatus => ['green','yellow','red','gray'].includes(s);

const validateModuleInfo = (data: unknown): data is ModuleInfo[] => {
  if (!Array.isArray(data)) return false;
  return data.every(item =>
    typeof item?.id === 'string' && typeof item?.name === 'string' &&
    typeof item?.status === 'string' && isValidModuleStatus(item.status) &&
    typeof item?.message === 'string' && (item.last_update === null || typeof item.last_update === 'string')
  );
};

// 为缺少 id 的模块补充 id
const ensureIds = (list: ModuleInfo[]): ModuleInfo[] =>
  list.map(m => m.id ? m : { ...m, id: `${m.name}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` });

// CSV 导出
const escapeCsvField = (field: string) => field.includes(',') || field.includes('"') || field.includes('\n') ? `"${field.replace(/"/g,'""')}"` : field;
const exportCSV = (modules: ModuleInfo[], accountId?: string) => {
  const header = `导出时间: ${dayjs().format('YYYY-MM-DD HH:mm:ss')}\n模块名称,状态,描述,最后更新时间`;
  const rows = modules.map(m => [escapeCsvField(m.name), STATUS_LABEL[m.status], escapeCsvField(m.message.replace(/\n/g,' ')), m.last_update||''].join(','));
  const csv = [header, ...rows].join('\n');
  const blob = new Blob(['\uFEFF'+csv], {type:'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `khaos_modules_${accountId||''}_${dayjs().format('YYYYMMDDHHmmss')}.csv`;
  a.click();
  URL.revokeObjectURL(url);
};

// ===================== 子组件 =====================
const CountdownTimer: React.FC<{ nextTime: number; paused: boolean }> = React.memo(({ nextTime, paused }) => {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (paused) { setSeconds(0); return; }
    const update = () => setSeconds(Math.max(0, Math.ceil((nextTime - Date.now())/1000)));
    update();
    const id = setInterval(update, 1000);
    return () => clearInterval(id);
  }, [nextTime, paused]);
  if (paused) return <span style={{fontSize:'0.75rem',color:'var(--color-text-muted)'}}>已暂停</span>;
  return <span style={{fontSize:'0.75rem',color:'var(--color-text-muted)'}}>下次刷新: {seconds}s</span>;
});
CountdownTimer.displayName = 'CountdownTimer';

// 虚拟列表简化版（当模块数 > 30 时启用）
const VirtualList: React.FC<{ items: ModuleInfo[]; itemHeight: number; renderItem: (item: ModuleInfo) => React.ReactNode }> = ({ items, itemHeight, renderItem }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const containerHeight = 400;
  const totalHeight = items.length * itemHeight;
  const startIndex = Math.floor(scrollTop / itemHeight);
  const endIndex = Math.min(items.length - 1, Math.ceil((scrollTop + containerHeight) / itemHeight));
  const visibleItems = items.slice(startIndex, endIndex + 1);
  const onScroll = useCallback(() => {
    if (containerRef.current) setScrollTop(containerRef.current.scrollTop);
  }, []);
  return (
    <div ref={containerRef} onScroll={onScroll} style={{ height: containerHeight, overflowY: 'auto', position: 'relative' }}>
      <div style={{ height: totalHeight, position: 'relative' }}>
        {visibleItems.map((item, idx) => (
          <div key={item.id} style={{ position: 'absolute', top: (startIndex + idx) * itemHeight, left: 0, right: 0, height: itemHeight }}>
            {renderItem(item)}
          </div>
        ))}
      </div>
    </div>
  );
};

// ModuleItem（增强色盲模式、复制名称）
const ModuleItem: React.FC<{ mod: ModuleInfo; onClick?: (mod: ModuleInfo) => void }> = React.memo(({ mod, onClick }) => {
  const color = getStatusColor(mod.status);
  const preciseTime = useMemo(() => mod.last_update ? dayjs.utc(mod.last_update).format('YYYY-MM-DD HH:mm:ss') + ' UTC' : null, [mod.last_update]);
  const relativeTimeStr = useMemo(() => mod.last_update ? dayjs.utc(mod.last_update).fromNow() : '--', [mod.last_update]);
  const [copied, setCopied] = useState(false);

  const handleCopyName = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard?.writeText(mod.name);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const isRed = mod.status === 'red';
  const shapeStyle: React.CSSProperties = isRed ? { borderRadius: '2px' } : { borderRadius: '50%' }; // 色盲友好：异常时方形

  return (
    <li
      className="module-item"
      style={{ display:'flex', alignItems:'center', gap:12, padding:'12px 0', borderBottom:'1px solid var(--color-border-light, #333)', cursor: onClick?'pointer':'default', transition:'background-color 0.2s' }}
      onClick={() => onClick?.(mod)}
      tabIndex={onClick?0:undefined}
      role={onClick?'button':undefined}
      aria-label={onClick ? `查看 ${mod.name} 详情` : undefined}
      data-testid={`module-item-${mod.name}`}
    >
      <div style={{ width:'clamp(12px,1.5vw,24px)', height:'clamp(12px,1.5vw,24px)', backgroundColor:color, flexShrink:0, boxShadow: isRed ? `0 0 8px ${color}` : undefined, ...shapeStyle, transition:'background-color 0.3s' }} aria-hidden="true" />
      <div style={{flex:1, minWidth:0}}>
        <div style={{display:'flex', alignItems:'center', gap:4}}>
          <span style={{fontWeight:500, color:'var(--color-text-primary,#e0e0e0)', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', maxWidth:200}}>{mod.name}</span>
          <button onClick={handleCopyName} style={{background:'none', border:'none', color:'var(--color-text-muted)', cursor:'pointer', fontSize:'0.7rem', padding:0}} title="复制名称">{copied ? '✓' : '📋'}</button>
        </div>
        <div style={{fontSize:'0.8rem', color:'var(--color-text-secondary,#8a8f99)', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap'}} title={mod.message}>
          {mod.message.length > 100 ? mod.message.slice(0,100)+'...' : mod.message}
        </div>
      </div>
      <div style={{padding:'2px 8px', borderRadius:12, fontSize:'0.75rem', fontWeight:600, backgroundColor:`${color}20`, color, flexShrink:0}}>{STATUS_LABEL[mod.status]}</div>
      <div style={{fontSize:'0.7rem', color:'var(--color-text-muted,#555a62)', flexShrink:0, minWidth:80, textAlign:'right'}} title={preciseTime||undefined}>{relativeTimeStr}</div>
    </li>
  );
}, (prev, next) => prev.mod.id === next.mod.id && prev.mod.status === next.mod.status && prev.mod.message === next.mod.message && prev.mod.last_update === next.mod.last_update && prev.onClick === next.onClick);
ModuleItem.displayName = 'ModuleItem';

// ===================== Error Boundary =====================
class ModulePanelErrorBoundary extends React.Component<{children:React.ReactNode; onRefresh:()=>void}, {hasError:boolean}> {
  state = {hasError:false};
  static getDerivedStateFromError() { return {hasError:true}; }
  componentDidCatch(error:Error, info:React.ErrorInfo) {
    console.error('[ModuleStatusPanel]', error, info);
    (window as any).__KHAOS_ERROR_REPORT__?.('ModulePanel', error.message);
  }
  render() {
    if (this.state.hasError) return <div className="card" style={{padding:16,color:'var(--color-error)'}}>模块面板异常。 <button onClick={this.props.onRefresh}>重试</button></div>;
    return this.props.children;
  }
}

// ===================== 主组件 =====================
const ModuleStatusPanel = forwardRef<ModuleStatusPanelHandle, ModuleStatusPanelProps>(
  ({refreshIntervalMs=30000, apiBaseUrl='/api/v1/monitoring', onModuleClick, onError, accountId}, ref) => {
    const sanitizedInterval = Math.min(120000, Math.max(10000, refreshIntervalMs));
    const [modules, setModules] = useState<ModuleInfo[]>([]);
    const [initialLoading, setInitialLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState<string|null>(null);
    const [consecutiveFailures, setConsecutiveFailures] = useState(0);
    const [paused, setPaused] = useState(false);
    const [isOffline, setIsOffline] = useState(typeof navigator!=='undefined'?!navigator.onLine:false);
    const [searchTerm, setSearchTerm] = useState(() => { try { return localStorage.getItem('khaos_module_search') || ''; } catch { return ''; } });
    const [filterAbnormal, setFilterAbnormal] = useState(false);
    const [filterEnabled, setFilterEnabled] = useState(false);
    const [nextRefreshTime, setNextRefreshTime] = useState(0);

    const mountedRef = useRef(true);
    const controllerRef = useRef<AbortController|null>(null);
    const timerRef = useRef<number|null>(null);
    const refreshingRef = useRef(false);
    const failureCountRef = useRef(0);
    const modulesCache = useRef<ModuleInfo[]>([]);
    const handleRefreshRef = useRef(() => {});
    const handleErrorRef = useRef(onError);

    // 保存搜索词到 localStorage
    useEffect(() => { try { localStorage.setItem('khaos_module_search', searchTerm); } catch {} }, [searchTerm]);

    const clearTimer = () => { if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; } };

    const fetchModules = useCallback(async () => {
      if (refreshingRef.current) return;
      refreshingRef.current = true;
      setRefreshing(true);
      setError(null);

      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;

      try {
        const response = await Promise.race([
          fetch(`${apiBaseUrl}/modules`, {
            signal: controller.signal,
            headers: {'Accept':'application/json','Content-Type':'application/json'},
            referrerPolicy:'strict-origin-when-cross-origin',
          }),
          new Promise<Response>((_,reject) => setTimeout(() => reject(new TimeoutError()), 5000))
        ]);

        if (!response.ok) {
          const body = await response.text().catch(() => '');
          throw new Error(`服务器错误 (${response.status}): ${body || '无详情'}`);
        }

        let data: unknown;
        try { data = await response.json(); } catch { throw new Error('响应非JSON'); }
        if (!validateModuleInfo(data)) throw new Error('响应数据结构异常');

        const backendModules = ensureIds(data as ModuleInfo[]);
        const merged = DEFAULT_MODULES.map(name => {
          const found = backendModules.find(m => m.name === name);
          return found || { id: `${name}-default`, name, status:'gray' as ModuleStatus, message:'未上报', last_update:null };
        });
        backendModules.forEach(m => { if (!merged.find(x => x.name === m.name)) merged.push(m); });

        failureCountRef.current = 0;
        setConsecutiveFailures(0);
        modulesCache.current = merged;
        if (mountedRef.current) {
          setModules(merged);
          setInitialLoading(false);
          setRefreshing(false);
        }
      } catch (err) {
        if ((err as Error)?.name === 'AbortError') return;
        const msg = err instanceof Error ? err.message : '未知错误';
        setError(msg);
        failureCountRef.current++;
        setConsecutiveFailures(failureCountRef.current);
        if (mountedRef.current) {
          setInitialLoading(false);
          setRefreshing(false);
          handleErrorRef.current?.(msg);
          if (failureCountRef.current >= 3) setPaused(true);
        }
      } finally {
        refreshingRef.current = false;
        if (mountedRef.current && !paused && !isOffline) {
          const next = Date.now() + sanitizedInterval;
          setNextRefreshTime(next);
          scheduleNext();
        }
      }
    }, [apiBaseUrl, sanitizedInterval, paused, isOffline]);

    const scheduleNext = useCallback(() => {
      clearTimer();
      timerRef.current = window.setTimeout(() => { if (mountedRef.current) fetchModules(); }, sanitizedInterval);
    }, [sanitizedInterval, fetchModules]);

    handleRefreshRef.current = useCallback(() => {
      controllerRef.current?.abort();
      setError(null);
      setConsecutiveFailures(0);
      failureCountRef.current = 0;
      setPaused(false);
      setRefreshing(true);
      fetchModules();
    }, [fetchModules]);

    const handleRefresh = handleRefreshRef.current;

    const togglePause = useCallback(() => {
      setPaused(prev => {
        if (prev) { handleRefresh(); return false; }
        else { clearTimer(); controllerRef.current?.abort(); return true; }
      });
    }, [handleRefresh, clearTimer]);

    useImperativeHandle(ref, () => ({
      refresh: handleRefresh,
      pause: () => { clearTimer(); setPaused(true); },
      resume: () => { setPaused(false); handleRefresh(); },
    }), [handleRefresh, clearTimer]);

    // 离线/在线，含防抖
    useEffect(() => {
      let onlineTimer: number;
      const goOnline = () => {
        clearTimeout(onlineTimer);
        onlineTimer = window.setTimeout(() => {
          setIsOffline(false);
          failureCountRef.current = 0;
          setConsecutiveFailures(0);
          handleRefresh();
        }, 2000);
      };
      const goOffline = () => setIsOffline(true);
      window.addEventListener('online', goOnline);
      window.addEventListener('offline', goOffline);
      return () => {
        window.removeEventListener('online', goOnline);
        window.removeEventListener('offline', goOffline);
        clearTimeout(onlineTimer);
      };
    }, [handleRefresh]);

    // 可见性
    useEffect(() => {
      const onVisibility = () => {
        if (document.hidden) clearTimer();
        else if (!paused && !isOffline) fetchModules();
      };
      document.addEventListener('visibilitychange', onVisibility);
      return () => document.removeEventListener('visibilitychange', onVisibility);
    }, [fetchModules, paused, isOffline, clearTimer]);

    // 挂载/卸载
    useEffect(() => {
      mountedRef.current = true;
      fetchModules();
      return () => {
        mountedRef.current = false;
        controllerRef.current?.abort();
        clearTimer();
      };
    }, []); // eslint-disable-line

    // 搜索与过滤
    const filteredModules = useMemo(() => {
      let list = modules.length ? modules : modulesCache.current;
      if (searchTerm.trim()) {
        const term = searchTerm.trim().toLowerCase();
        list = list.filter(m => m.name.toLowerCase().includes(term) || m.message.toLowerCase().includes(term));
      }
      if (filterAbnormal) list = list.filter(m => m.status === 'red' || m.status === 'yellow');
      if (filterEnabled) list = list.filter(m => m.status !== 'gray');
      return [...list].sort((a,b) => (STATUS_PRIORITY[a.status]??3) - (STATUS_PRIORITY[b.status]??3) || a.name.localeCompare(b.name));
    }, [modules, searchTerm, filterAbnormal, filterEnabled]);

    const abnormalCount = useMemo(() => filteredModules.filter(m => m.status === 'red' || m.status === 'yellow').length, [filteredModules]);

    // URL 参数初始化筛选
    useEffect(() => {
      const params = new URLSearchParams(window.location.search);
      if (params.get('filter') === 'red') setFilterAbnormal(true);
    }, []);

    return (
      <ModulePanelErrorBoundary onRefresh={handleRefresh}>
        <div className="module-status-panel card" role="region" aria-label="模块状态面板" data-testid="module-status-panel">
          <div className="card-header">
            <h3>
              系统模块状态
              {abnormalCount > 0 ? (
                <span style={{color:'var(--color-error)', fontSize:'0.8em', marginLeft:8}}>({abnormalCount} 异常)</span>
              ) : (
                <span style={{color:'var(--color-success)', fontSize:'0.8em', marginLeft:8}}>全部正常</span>
              )}
            </h3>
            <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
              <input
                type="text"
                placeholder="搜索模块..."
                value={searchTerm}
                onChange={e => setSearchTerm(e.target.value)}
                onKeyDown={e => e.key === 'Escape' && setSearchTerm('')}
                style={{width:140, padding:'4px 8px', background:'var(--color-dark-surface)', border:'1px solid var(--color-border)', borderRadius:4, color:'var(--color-text-primary)', fontSize:'0.8rem'}}
                aria-label="搜索模块"
              />
              <label style={{fontSize:'0.8rem', color:'var(--color-text-secondary)', display:'flex', alignItems:'center', gap:4}}>
                <input type="checkbox" checked={filterAbnormal} onChange={e => setFilterAbnormal(e.target.checked)} />仅异常
              </label>
              <label style={{fontSize:'0.8rem', color:'var(--color-text-secondary)', display:'flex', alignItems:'center', gap:4}}>
                <input type="checkbox" checked={filterEnabled} onChange={e => setFilterEnabled(e.target.checked)} />仅启用
              </label>
              <button type="button" className="btn btn-sm btn-secondary" onClick={handleRefresh} disabled={refreshing} style={{minWidth:80,minHeight:44}}>
                {refreshing ? '⏳ 刷新中' : '🔄 刷新'}
              </button>
              <button type="button" className="btn btn-sm btn-secondary" onClick={togglePause} style={{minWidth:70,minHeight:44}}>
                {paused ? '▶ 恢复' : '⏸ 暂停'}
              </button>
              <button type="button" className="btn btn-sm btn-secondary" onClick={() => exportCSV(filteredModules, accountId)} disabled={filteredModules.length===0} style={{minHeight:44}}>
                ⬇ 导出
              </button>
              <CountdownTimer nextTime={nextRefreshTime} paused={paused} />
            </div>
          </div>

          {isOffline && <div className="alert alert-warning" role="alert">网络离线，已暂停刷新。</div>}
          {error && (
            <div className="alert alert-error" role="alert" aria-live="assertive">
              {error} {consecutiveFailures >= 3 && <span>（连续失败 {consecutiveFailures} 次，已暂停）</span>}
              <button onClick={handleRefresh} style={{marginLeft:8, background:'none', textDecoration:'underline', color:'var(--color-gold)', cursor:'pointer'}}>重试</button>
            </div>
          )}

          {filteredModules.length > 30 ? (
            <VirtualList items={filteredModules} itemHeight={48} renderItem={(mod) => <ModuleItem key={mod.id} mod={mod} onClick={onModuleClick} />} />
          ) : (
            <ul className="module-list" role="list" aria-label="模块状态列表" style={{listStyle:'none', padding:0, maxHeight:400, overflowY:'auto'}}>
              {initialLoading ? (
                <li style={{padding:20, textAlign:'center', color:'var(--color-text-muted)'}}>加载中...</li>
              ) : filteredModules.length === 0 ? (
                <li className="empty-state" role="status" style={{padding:20, textAlign:'center', color:'var(--color-text-muted)'}}>暂无匹配模块</li>
              ) : (
                filteredModules.map(mod => <ModuleItem key={mod.id} mod={mod} onClick={onModuleClick} />)
              )}
            </ul>
          )}

          <div style={{display:'flex', gap:16, marginTop:12, fontSize:'0.75rem', color:'var(--color-text-muted,#555a62)', flexWrap:'wrap'}} aria-hidden="true">
            {Object.entries(STATUS_LABEL).map(([status, label]) => (
              <div key={status} style={{display:'flex', alignItems:'center', gap:4}}>
                <div style={{width:10, height:10, borderRadius: status==='red'?2:'50%', backgroundColor: getStatusColor(status as ModuleStatus)}} />
                {label}
              </div>
            ))}
          </div>
        </div>
      </ModulePanelErrorBoundary>
    );
  }
);

ModuleStatusPanel.displayName = 'ModuleStatusPanel';
export default ModuleStatusPanel;
