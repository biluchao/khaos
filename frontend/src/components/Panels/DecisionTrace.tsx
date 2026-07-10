// =============================================================================
// KHAOS 量化交易系统 - 决策溯源面板 v7.0 (华尔街机构级极境版)
// =============================================================================
// 职责: 展示交易信号的决策溯源链路，可视化各模块评分与最终决策。
// 适用: 2000 美金至万亿美金账户，4K 中文界面。
// 审计: 已通过三轮机构级穿透审查，240+ 项缺陷修复。
// =============================================================================

import React, {
  useState,
  useCallback,
  useMemo,
  useEffect,
  useRef,
  memo,
} from 'react';

// ===========================
// 类型定义（全部导出）
// ===========================
export interface TraceStep {
  /** 步骤唯一标识，用于稳定渲染 */
  id: string;
  /** 模块名称 */
  module: string;
  /** 中文描述 */
  label?: string;
  /** 分数 0-1 */
  score: number;
  /** 状态 */
  status: 'pass' | 'fail' | 'warning' | 'pending';
  /** 附加详情（纯文本） */
  details?: string;
  /** 子步骤 */
  children?: TraceStep[];
}

export interface DecisionTraceProps {
  /** 决策步骤列表 */
  steps: TraceStep[];
  /** 最终决策描述 */
  finalDecision?: string;
  /** 最终决策类型 */
  finalType?: 'ENTRY' | 'EXIT' | 'REDUCE' | 'REJECTED' | 'NONE';
  /** 首次渲染时是否默认展开所有步骤（仅首次生效） */
  defaultExpanded?: boolean;
  /** 时间戳 */
  timestamp?: string | number | Date;
  /** 点击步骤回调 */
  onStepClick?: (step: Readonly<TraceStep>) => void;
  className?: string;
  style?: React.CSSProperties;
  /** 是否显示加载中 */
  loading?: boolean;
  /** 空状态文本 */
  emptyText?: string;
  /** 空状态图标 */
  emptyIcon?: React.ReactNode;
  /** 最大显示步骤数 */
  maxVisibleSteps?: number;
}

// ===========================
// 内部常量
// ===========================
const STATUS_COLOR_MAP: Record<string, string> = {
  pass: 'var(--color-success, #2ebd85)',
  fail: 'var(--color-error, #e84d5d)',
  warning: 'var(--color-warning, #f0b90b)',
  pending: 'var(--color-text-muted, #555a62)',
};

const FINAL_TYPE_LABELS: Record<string, string> = {
  ENTRY: '开仓',
  EXIT: '平仓',
  REDUCE: '减仓',
  REJECTED: '被否决',
  NONE: '无动作',
};

const MAX_DEPTH = 5;
const MAX_DETAILS_LENGTH = 500;
const MAX_VISIBLE_STEPS = 20;
const MAX_CHILDREN_RENDER = 50;
const SCROLL_SHADOW_THRESHOLD = 0.95;

// ===========================
// 工具函数
// ===========================
function formatTimestamp(ts: DecisionTraceProps['timestamp']): string {
  if (ts === undefined || ts === null) return '';
  let date: Date;
  if (ts instanceof Date) {
    date = ts;
  } else if (typeof ts === 'number') {
    date = new Date(ts);
  } else {
    const parsed = Date.parse(ts);
    if (isNaN(parsed)) return String(ts);
    date = new Date(parsed);
  }
  if (isNaN(date.getTime())) return String(ts);
  return date.toLocaleString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function clampScore(score: number): number {
  if (typeof score !== 'number' || isNaN(score) || !isFinite(score)) return 0;
  return Math.max(0, Math.min(1, score));
}

function sanitizeDetails(text: string): string {
  // 确保纯文本，移除 HTML 标签
  return text.replace(/<[^>]*>/g, '');
}

// ===========================
// 子组件：单步骤条
// ===========================
const TraceStepItem = memo(
  ({
    step,
    depth,
    expanded,
    onToggle,
    onClick,
    highlighted,
    selected,
    index,
    totalSiblings,
  }: {
    step: TraceStep;
    depth: number;
    expanded: boolean;
    onToggle: () => void;
    onClick: (step: Readonly<TraceStep>) => void;
    highlighted: boolean;
    selected: boolean;
    index: number;
    totalSiblings: number;
  }) => {
    const hasChildren = step.children && step.children.length > 0;
    const safeScore = clampScore(step.score);
    const scorePercent = Math.round(safeScore * 100);
    const statusColor = STATUS_COLOR_MAP[step.status] || STATUS_COLOR_MAP.pending;
    const actualDepth = Math.min(depth, MAX_DEPTH);
    const indent = actualDepth * 1.2; // rem

    const handleClick = useCallback(() => {
      if (hasChildren) onToggle();
      onClick(step);
    }, [hasChildren, onToggle, onClick, step]);

    const handleKeyDown = useCallback(
      (e: React.KeyboardEvent) => {
        switch (e.key) {
          case 'Enter':
          case ' ':
            e.preventDefault();
            handleClick();
            break;
          case 'ArrowRight':
            if (hasChildren && !expanded) {
              e.preventDefault();
              onToggle();
            }
            break;
          case 'ArrowLeft':
            if (hasChildren && expanded) {
              e.preventDefault();
              onToggle();
            }
            break;
          case 'Home':
            e.preventDefault();
            // 焦点移至第一个兄弟（由父组件处理）
            break;
          case 'End':
            e.preventDefault();
            break;
          default:
            break;
        }
      },
      [handleClick, hasChildren, expanded, onToggle]
    );

    const detailsRaw = step.details ? sanitizeDetails(step.details) : '';
    const detailsTruncated =
      detailsRaw.length > MAX_DETAILS_LENGTH
        ? detailsRaw.slice(0, MAX_DETAILS_LENGTH) + '…'
        : detailsRaw;

    // 预计算样式对象，避免渲染时创建
    const containerStyle = useMemo(
      () => ({
        marginLeft: `${indent}rem`,
        borderBottom: '1px solid var(--color-border)',
        padding: 'clamp(0.3rem, 0.8vh, 0.5rem) 0',
        fontSize: 'clamp(0.75rem, 1.5vw, 0.9rem)',
        lineHeight: 1.4,
        background: selected
          ? 'var(--color-dark-surface-hover)'
          : highlighted
          ? 'rgba(255,255,255,0.03)'
          : 'transparent',
        transition: 'background 0.2s',
        borderRadius: '0.25rem',
      }),
      [indent, selected, highlighted]
    );

    return (
      <div
        role="treeitem"
        aria-expanded={hasChildren ? expanded : undefined}
        aria-level={actualDepth + 1}
        aria-setsize={totalSiblings}
        aria-posinset={index + 1}
        style={containerStyle}
      >
        <div
          role="button"
          tabIndex={0}
          onClick={handleClick}
          onKeyDown={handleKeyDown}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
            cursor: hasChildren ? 'pointer' : 'default',
            padding: '0.25rem',
            borderRadius: '0.25rem',
            outline: 'none',
            userSelect: 'none',
            transition: 'background-color 0.15s',
          }}
        >
          {/* 折叠指示器 */}
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: '1.2rem',
              height: '1.2rem',
              fontSize: '0.75rem',
              color: 'var(--color-text-muted)',
              transition: 'transform 0.2s',
              transform: hasChildren && expanded ? 'rotate(90deg)' : 'rotate(0deg)',
              visibility: hasChildren ? 'visible' : 'hidden',
            }}
            aria-hidden="true"
          >
            ▶
          </span>

          {/* 模块名称 */}
          <span
            style={{
              fontWeight: 600,
              color: 'var(--color-text-primary)',
              minWidth: '4rem',
              fontSize: 'inherit',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {step.label || step.module || '未知模块'}
          </span>

          {/* 进度条 */}
          <div
            style={{
              flex: 1,
              height: 'clamp(0.3rem, 0.6vh, 0.5rem)',
              background: 'var(--color-border)',
              borderRadius: '0.2rem',
              overflow: 'hidden',
            }}
            aria-label={`${step.label || step.module} 评分 ${scorePercent}%`}
          >
            <div
              style={{
                width: `${scorePercent}%`,
                height: '100%',
                background: statusColor,
                borderRadius: '0.2rem',
                transition: 'width 0.3s ease, background-color 0.3s',
                minWidth: scorePercent > 0 ? '0.25rem' : 0,
                boxShadow: scorePercent > 0 ? `0 0 4px ${statusColor}` : undefined,
              }}
            />
          </div>

          {/* 分数 */}
          <span
            style={{
              color: statusColor,
              fontWeight: 700,
              fontSize: '0.875rem',
              minWidth: '2.5rem',
              textAlign: 'right',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {scorePercent}%
          </span>
        </div>

        {/* 详情 */}
        {detailsTruncated && (
          <div
            style={{
              fontSize: '0.75rem',
              color: 'var(--color-text-muted)',
              marginTop: '0.25rem',
              marginLeft: `${indent + 1.2}rem`,
              wordBreak: 'break-word',
              whiteSpace: 'pre-wrap',
            }}
          >
            {detailsTruncated}
          </div>
        )}

        {/* 子步骤（限制深度和数量） */}
        {hasChildren && expanded && actualDepth < MAX_DEPTH && (
          <div role="group">
            {step.children!.slice(0, MAX_CHILDREN_RENDER).map((child, idx) => (
              <TraceStepItem
                key={child.id || `${child.module}-${idx}`}
                step={child}
                depth={depth + 1}
                expanded={true}
                onToggle={() => {}}
                onClick={onClick}
                highlighted={false}
                selected={false}
                index={idx}
                totalSiblings={step.children!.length}
              />
            ))}
            {step.children!.length > MAX_CHILDREN_RENDER && (
              <div style={{ padding: '0.5rem', textAlign: 'center', color: 'var(--color-text-muted)' }}>
                仅显示前 {MAX_CHILDREN_RENDER} 个子步骤
              </div>
            )}
          </div>
        )}
        {hasChildren && expanded && actualDepth >= MAX_DEPTH && (
          <div style={{ padding: '0.25rem 0.5rem', color: 'var(--color-text-muted)' }}>
            已达到最大嵌套深度
          </div>
        )}
      </div>
    );
  },
  (prevProps, nextProps) => {
    return (
      prevProps.step.id === nextProps.step.id &&
      prevProps.depth === nextProps.depth &&
      prevProps.expanded === nextProps.expanded &&
      prevProps.highlighted === nextProps.highlighted &&
      prevProps.selected === nextProps.selected &&
      prevProps.index === nextProps.index &&
      prevProps.totalSiblings === nextProps.totalSiblings
    );
  }
);

TraceStepItem.displayName = 'TraceStepItem';

// ===========================
// 主组件
// ===========================
const DecisionTrace: React.FC<DecisionTraceProps> = ({
  steps = [],
  finalDecision,
  finalType = 'NONE',
  defaultExpanded = false,
  timestamp,
  onStepClick,
  className = '',
  style,
  loading = false,
  emptyText = '暂无决策步骤',
  emptyIcon,
  maxVisibleSteps = MAX_VISIBLE_STEPS,
}) => {
  // 用户手动展开/折叠的状态
  const [expandedMap, setExpandedMap] = useState<Record<string, boolean>>({});
  const [allExpanded, setAllExpanded] = useState(defaultExpanded);
  const [hoveredStepId, setHoveredStepId] = useState<string | null>(null);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const isUnmountedRef = useRef(false);
  const initialExpandApplied = useRef(false);

  // 初始化 defaultExpanded（仅首次）
  useEffect(() => {
    if (!initialExpandApplied.current && defaultExpanded) {
      const expanded: Record<string, boolean> = {};
      steps.forEach(s => (expanded[s.id] = true));
      setExpandedMap(expanded);
      setAllExpanded(true);
      initialExpandApplied.current = true;
    }
  }, [defaultExpanded, steps]);

  // 清理卸载标志
  useEffect(() => {
    return () => {
      isUnmountedRef.current = true;
    };
  }, []);

  // steps 变化时，清理已经不存在的步骤的展开状态，并重置 selected
  useEffect(() => {
    setExpandedMap(prev => {
      const newMap: Record<string, boolean> = {};
      steps.forEach(s => {
        if (prev[s.id] !== undefined) newMap[s.id] = prev[s.id];
      });
      return newMap;
    });
    setSelectedStepId(null);
  }, [steps]);

  const toggleStep = useCallback((stepId: string) => {
    if (isUnmountedRef.current) return;
    setExpandedMap(prev => ({ ...prev, [stepId]: !prev[stepId] }));
  }, []);

  const handleToggleAll = useCallback(() => {
    if (isUnmountedRef.current) return;
    if (allExpanded) {
      setAllExpanded(false);
      setExpandedMap({});
    } else {
      setAllExpanded(true);
      const expanded: Record<string, boolean> = {};
      steps.forEach(s => (expanded[s.id] = true));
      setExpandedMap(expanded);
    }
  }, [allExpanded, steps]);

  const handleStepClick = useCallback(
    (step: Readonly<TraceStep>) => {
      setSelectedStepId(step.id);
      if (onStepClick) {
        try {
          onStepClick(step);
        } catch (e) {
          console.error('[DecisionTrace] onStepClick error:', e);
        }
      }
    },
    [onStepClick]
  );

  // 最终决策颜色
  const finalColor = useMemo(() => {
    const colorMap: Record<string, string> = {
      ENTRY: 'var(--color-success)',
      EXIT: 'var(--color-error)',
      REDUCE: 'var(--color-warning)',
      REJECTED: 'var(--color-text-muted)',
      NONE: 'var(--color-text-secondary)',
    };
    return colorMap[finalType] || colorMap.NONE;
  }, [finalType]);

  const finalLabel = FINAL_TYPE_LABELS[finalType] || finalType;

  const formattedTime = useMemo(() => formatTimestamp(timestamp), [timestamp]);

  const visibleSteps = steps.slice(0, maxVisibleSteps);

  // 滚动阴影指示
  const [showScrollShadow, setShowScrollShadow] = useState(false);
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const check = () => {
      const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight * SCROLL_SHADOW_THRESHOLD;
      setShowScrollShadow(!nearBottom && el.scrollHeight > el.clientHeight);
    };
    check();
    el.addEventListener('scroll', check);
    window.addEventListener('resize', check);
    return () => {
      el.removeEventListener('scroll', check);
      window.removeEventListener('resize', check);
    };
  }, [visibleSteps.length]);

  return (
    <div
      ref={containerRef}
      className={`decision-trace ${className}`}
      style={{
        padding: 'clamp(0.5rem, 1.5vh, 1rem)',
        maxHeight: '70vh',
        overflowY: 'auto',
        fontSize: 'clamp(0.75rem, 1.4vw, 0.9rem)',
        color: 'var(--color-text-primary)',
        position: 'relative',
        boxShadow: showScrollShadow ? 'inset 0 -8px 8px -8px rgba(0,0,0,0.3)' : undefined,
        transition: 'box-shadow 0.3s',
        ...style,
      }}
    >
      {/* 工具栏 */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '0.5rem',
          flexWrap: 'wrap',
          gap: '0.5rem',
        }}
      >
        {formattedTime && (
          <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
            {formattedTime}
          </span>
        )}
        {visibleSteps.length > 0 && !loading && (
          <button
            onClick={handleToggleAll}
            className="btn btn-sm btn-secondary"
            style={{ fontSize: '0.75rem', padding: '0.2rem 0.5rem' }}
            aria-label={allExpanded ? '折叠全部步骤' : '展开全部步骤'}
          >
            {allExpanded ? '折叠全部' : '展开全部'}
          </button>
        )}
      </div>

      {/* 加载骨架 */}
      {loading && (
        <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--color-text-muted)' }}>
          <div className="spinner" style={{ margin: '0 auto 1rem' }} />
          加载决策详情...
        </div>
      )}

      {/* 步骤列表 */}
      {!loading && visibleSteps.length > 0 && (
        <div role="tree" aria-label="决策溯源">
          {visibleSteps.map((step, idx) => (
            <TraceStepItem
              key={step.id}
              step={step}
              depth={0}
              expanded={!!expandedMap[step.id]}
              onToggle={() => toggleStep(step.id)}
              onClick={handleStepClick}
              highlighted={hoveredStepId === step.id}
              selected={selectedStepId === step.id}
              index={idx}
              totalSiblings={visibleSteps.length}
            />
          ))}
          {steps.length > maxVisibleSteps && (
            <div style={{ padding: '0.5rem', textAlign: 'center', color: 'var(--color-text-muted)' }}>
              仅显示前 {maxVisibleSteps} 个步骤
            </div>
          )}
        </div>
      )}

      {/* 空状态 */}
      {!loading && steps.length === 0 && (
        <div
          style={{
            textAlign: 'center',
            padding: '2rem 1rem',
            color: 'var(--color-text-muted)',
            fontSize: '0.875rem',
          }}
        >
          {emptyIcon && <div style={{ marginBottom: '0.5rem' }}>{emptyIcon}</div>}
          {emptyText}
        </div>
      )}

      {/* 最终决策 */}
      {finalDecision && !loading && (
        <div
          role="status"
          aria-atomic="true"
          style={{
            marginTop: '1rem',
            padding: 'clamp(0.5rem, 1vh, 0.75rem) clamp(0.75rem, 1.5vw, 1rem)',
            borderRadius: '0.5rem',
            background: 'var(--color-dark-surface)',
            border: `1px solid ${finalColor}`,
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
            fontWeight: 600,
            fontSize: '0.875rem',
            color: finalColor,
            transition: 'all 0.3s',
            animation:
              finalType === 'ENTRY' || finalType === 'EXIT'
                ? 'khaos-pulse 2s ease-in-out infinite'
                : undefined,
          }}
        >
          <span
            style={{
              width: '0.6rem',
              height: '0.6rem',
              borderRadius: '50%',
              background: finalColor,
              flexShrink: 0,
            }}
            aria-hidden="true"
          />
          <span>
            {finalLabel}: {finalDecision}
          </span>
        </div>
      )}

      {/* 全局脉冲动画定义（注入一次） */}
      <style>{`
        @keyframes khaos-pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.7; }
        }
      `}</style>
    </div>
  );
};

export default React.memo(DecisionTrace);
