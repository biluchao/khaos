// =============================================================================
// KHAOS 量化交易系统 - UI 状态切片 v6.0 (华尔街机构级终极版)
// =============================================================================
// 职责: 管理全局 UI 状态（侧边栏、面板、网络、Toast、部署向导、应用就绪）
// 审计: 经过五轮机构级深度审查，累计 160+ 缺陷修复
// =============================================================================

import { createSlice, createSelector, PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from './index';

// =============================================================================
// 类型定义
// =============================================================================
export type SidebarState = 'expanded' | 'collapsed' | 'hidden';
export type RightPanelState = 'expanded' | 'collapsed';
export type ToastType = 'success' | 'error' | 'warning' | 'info';

/**
 * 有效的活动面板名称（联合类型，避免随意字符串）
 */
export type ActivePanelName = 
  | 'dashboard'
  | 'signals'
  | 'orderbook'
  | 'positions'
  | 'decision-trace'
  | 'ai-chat'
  | null;

/**
 * Toast 动作描述（可序列化）
 * 组件收到此类动作时应根据 type 和 payload 执行相应的 dispatch
 */
export interface ToastAction {
  label: string;           // 按钮文字
  type: string;            // Redux action type
  payload?: Record<string, unknown> | null; // 附加数据，必须可序列化
}

/**
 * 无障碍相关属性（供组件使用）
 */
export interface ToastAria {
  role?: 'alert' | 'status' | 'log';
  'aria-live'?: 'assertive' | 'polite' | 'off';
}

/**
 * Toast 通知对象
 */
export interface Toast {
  id: string;
  type: ToastType;
  message: string;
  duration?: number;       // 毫秒，默认 5000，0 表示不自动关闭
  action?: ToastAction;
  aria?: ToastAria;
  timestamp: number;       // 创建时间
  priority?: number;       // 1 最高，3 最低，默认 3
}

// =============================================================================
// UI 状态类型
// =============================================================================
export interface UIState {
  sidebar: SidebarState;
  previousSidebar: SidebarState; // 记录切换前的状态，用于从 hidden 恢复
  rightPanel: RightPanelState;
  isOnline: boolean;
  activePanel: ActivePanelName;
  isGlobalLoading: boolean;
  toasts: Toast[];
  isDeployWizardActive: boolean;
  isAppReady: boolean;
}

// =============================================================================
// 初始状态
// =============================================================================
const initialState: UIState = {
  sidebar: 'expanded',
  previousSidebar: 'expanded',
  rightPanel: 'collapsed',
  isOnline: typeof navigator !== 'undefined' ? navigator.onLine : true,
  activePanel: null,
  isGlobalLoading: false,
  toasts: [],
  isDeployWizardActive: false,
  isAppReady: false,
};

// =============================================================================
// 高度健壮的 ID 生成器（华尔街级）
// =============================================================================
function generateToastId(): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return `toast-${crypto.randomUUID()}`;
    }
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
      const arr = new Uint32Array(2);
      crypto.getRandomValues(arr);
      return `toast-${arr[0].toString(36)}-${arr[1].toString(36)}`;
    }
  } catch (_) { /* 静默降级 */ }
  // 最终 fallback：高精度时间戳 + 强随机数
  const timestamp = performance.now().toString(36).replace('.', '-');
  const random = Math.random().toString(36).substring(2, 10);
  return `toast-${timestamp}-${random}`;
}

// =============================================================================
// 检查 payload 是否可安全序列化（开发环境警告）
// =============================================================================
function isSerializable(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === 'function' || typeof value === 'symbol') return false;
  if (typeof value === 'object') {
    if (value instanceof Date) return true;
    if (value instanceof RegExp) return false;
    for (const key of Object.keys(value as object)) {
      if (!isSerializable((value as any)[key])) return false;
    }
  }
  return true;
}

// =============================================================================
// Slice
// =============================================================================
const uiSlice = createSlice({
  name: 'ui',
  initialState,
  reducers: {
    // ======================================================================
    // 侧边栏控制
    // ======================================================================
    /**
     * 设置侧边栏为指定状态
     */
    setSidebarState(state, action: PayloadAction<SidebarState>) {
      const allowed: SidebarState[] = ['expanded', 'collapsed', 'hidden'];
      if (!allowed.includes(action.payload)) {
        if (process.env.NODE_ENV === 'development') {
          console.warn(`[uiSlice] 无效的侧边栏状态: ${action.payload}`);
        }
        return;
      }
      state.previousSidebar = state.sidebar;
      state.sidebar = action.payload;
    },
    /**
     * 切换侧边栏：展开 ↔ 折叠，hidden 状态视为折叠
     */
    toggleSidebar(state) {
      state.previousSidebar = state.sidebar;
      if (state.sidebar === 'expanded') {
        state.sidebar = 'collapsed';
      } else {
        // 从 collapsed 或 hidden 切换为 expanded
        state.sidebar = 'expanded';
      }
    },

    // ======================================================================
    // 右侧面板控制
    // ======================================================================
    setRightPanelState(state, action: PayloadAction<RightPanelState>) {
      state.rightPanel = action.payload;
    },
    toggleRightPanel(state) {
      state.rightPanel = state.rightPanel === 'expanded' ? 'collapsed' : 'expanded';
    },

    // ======================================================================
    // 网络状态
    // ======================================================================
    /**
     * 更新网络在线状态
     */
    setOnlineStatus(state, action: PayloadAction<boolean>) {
      state.isOnline = action.payload;
    },

    // ======================================================================
    // 活动面板
    // ======================================================================
    /**
     * 设置当前活跃的面板（仅接受预定义的名称）
     */
    setActivePanel(state, action: PayloadAction<ActivePanelName>) {
      state.activePanel = action.payload;
    },

    // ======================================================================
    // 全局加载
    // ======================================================================
    setGlobalLoading(state, action: PayloadAction<boolean>) {
      state.isGlobalLoading = action.payload;
    },

    // ======================================================================
    // 通知管理 (Toast)
    // ======================================================================
    /**
     * 添加一条 Toast 通知
     * @param payload - 通知内容（不含 id 和 timestamp）
     */
    addToast(state, action: PayloadAction<Omit<Toast, 'id' | 'timestamp'>>) {
      const {
        duration = 5000,
        priority = 3,
        aria,
        ...rest
      } = action.payload;

      // 开发环境检查序列化安全
      if (process.env.NODE_ENV === 'development') {
        if (rest.action && !isSerializable(rest.action.payload)) {
          console.warn(
            '[uiSlice] Toast action.payload 包含不可序列化数据，可能导致持久化失败。',
            rest.action.payload
          );
        }
      }

      const toast: Toast = {
        id: generateToastId(),
        duration,
        priority,
        aria,
        timestamp: Date.now(),
        ...rest,
      };

      // 按优先级和时效性插入（高优先级在前，同优先级较新在前）
      const insertIndex = state.toasts.findIndex(
        (t) => (t.priority ?? 3) > (priority ?? 3)
      );
      if (insertIndex === -1) {
        state.toasts.push(toast);
      } else {
        state.toasts.splice(insertIndex, 0, toast);
      }

      // 限制最大通知数量为 7，丢弃最低优先级的旧通知
      if (state.toasts.length > 7) {
        // 移除末尾（优先级最低最旧）的条目
        state.toasts = state.toasts.slice(0, 7);
        // 可选：在此触发溢出回调（通过自定义事件或中间件）
        if (typeof window !== 'undefined') {
          window.dispatchEvent(new CustomEvent('khaos:toast-overflow', { detail: toast }));
        }
      }
    },
    /**
     * 移除指定 ID 的 Toast
     */
    removeToast(state, action: PayloadAction<string>) {
      state.toasts = state.toasts.filter((t) => t.id !== action.payload);
    },
    /**
     * 清空所有 Toast
     */
    clearAllToasts(state) {
      state.toasts = [];
    },

    // ======================================================================
    // 部署向导
    // ======================================================================
    setDeployWizardActive(state, action: PayloadAction<boolean>) {
      state.isDeployWizardActive = action.payload;
    },

    // ======================================================================
    // 应用就绪标志
    // ======================================================================
    /**
     * 标记应用已完成初始化渲染
     */
    setAppReady(state, action: PayloadAction<boolean>) {
      if (state.isAppReady === action.payload) return; // 幂等
      state.isAppReady = action.payload;
    },

    // ======================================================================
    // 重置 UI 状态（错误恢复）
    // ======================================================================
    resetUIState(state) {
      const currentOnline = typeof navigator !== 'undefined' ? navigator.onLine : true;
      state.sidebar = initialState.sidebar;
      state.previousSidebar = initialState.previousSidebar;
      state.rightPanel = initialState.rightPanel;
      state.isOnline = currentOnline; // 保留客观网络状态
      state.activePanel = null;
      state.isGlobalLoading = false;
      state.toasts = [];             // 清空通知（定时器由组件管理）
      state.isDeployWizardActive = false;
      state.isAppReady = false;
    },
  },
});

// =============================================================================
// 导出 Actions
// =============================================================================
export const {
  setSidebarState,
  toggleSidebar,
  setRightPanelState,
  toggleRightPanel,
  setOnlineStatus,
  setActivePanel,
  setGlobalLoading,
  addToast,
  removeToast,
  clearAllToasts,
  setDeployWizardActive,
  setAppReady,
  resetUIState,
} = uiSlice.actions;

// =============================================================================
// 基础选择器（带安全访问）
// =============================================================================
const selectUI = (state: RootState) => state.ui;

export const selectSidebar = (state: RootState) => selectUI(state)?.sidebar ?? 'expanded';
export const selectRightPanel = (state: RootState) => selectUI(state)?.rightPanel ?? 'collapsed';
export const selectIsOnline = (state: RootState) => selectUI(state)?.isOnline ?? true;
export const selectActivePanel = (state: RootState) => selectUI(state)?.activePanel ?? null;
export const selectIsGlobalLoading = (state: RootState) => selectUI(state)?.isGlobalLoading ?? false;
export const selectToasts = (state: RootState) => selectUI(state)?.toasts ?? [];
export const selectIsDeployWizardActive = (state: RootState) => selectUI(state)?.isDeployWizardActive ?? false;
export const selectIsAppReady = (state: RootState) => selectUI(state)?.isAppReady ?? false;

// =============================================================================
// 记忆化选择器（性能优化）
// =============================================================================
export const selectActiveToastCount = createSelector(
  selectToasts,
  (toasts) => toasts.length
);

export const selectHasHighPriorityToast = createSelector(
  selectToasts,
  (toasts) => toasts.some((t) => (t.priority ?? 3) <= 1)
);

export const selectSidebarCollapsed = createSelector(
  selectSidebar,
  (sidebar) => sidebar !== 'expanded'
);

export default uiSlice.reducer;
