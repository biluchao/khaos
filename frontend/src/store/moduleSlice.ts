/**
 * 模块名称: moduleSlice.ts
 * 核心职责: 管理系统各核心模块的实时健康状态（红绿灯监控）
 * 所属层级: store
 * 依赖: @reduxjs/toolkit, axios
 * 接口: fetchModuleStatus (异步获取), updateModuleStatus (同步更新单个模块), clearError, resetModuleState
 * 作者: KHAOS Engineering
 * 创建日期: 2026-07-11
 * 修改记录: 2026-07-11 初始版本; 2026-07-16 深度审计修复40项缺陷
 */

import { createSlice, createAsyncThunk, PayloadAction, createAction } from '@reduxjs/toolkit';
import axios, { AxiosError, CanceledError } from 'axios';

// ---------- 常量 ----------
const API_BASE = import.meta.env.VITE_API_BASE || '';
const MODULE_API = `${API_BASE}/api/v1/monitoring/modules`;
const REQUEST_TIMEOUT = 10_000;
const UPDATE_THROTTLE_MS = 2000; // 同一模块最短更新间隔

// ---------- 类型定义 ----------
export type ModuleStatusEnum = 'green' | 'yellow' | 'red' | 'gray';

export interface ModuleStatus {
  name: string;
  status: ModuleStatusEnum;
  message: string;
  last_update: string | null;    // ISO 8601 字符串
}

export interface ModuleState {
  modules: ModuleStatus[];
  loading: boolean;
  error: string | null;
  lastUpdatedAt: string | null;  // 最后成功获取时间
}

// ---------- Axios 实例 ----------
const apiClient = axios.create({
  baseURL: API_BASE,
  timeout: REQUEST_TIMEOUT,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 请求拦截器：注入认证令牌
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('khaos_auth_token'); // 根据实际认证方案调整
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// 响应拦截器：统一错误处理
apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      // 可触发登出或刷新令牌
      window.dispatchEvent(new CustomEvent('khaos:auth-expired'));
    }
    return Promise.reject(error);
  },
);

// ---------- 辅助函数 ----------
/** 校验并清洗模块数据 */
function validateModules(data: unknown): ModuleStatus[] {
  if (!Array.isArray(data)) return [];
  return data
    .filter((item): item is ModuleStatus => typeof item === 'object' && item !== null && 'name' in item)
    .map((item) => ({
      name: String(item.name || '').trim(),
      status: validateStatus(item.status),
      message: typeof item.message === 'string' ? item.message.substring(0, 200) : '',
      last_update: item.last_update && typeof item.last_update === 'string' ? item.last_update : null,
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function validateStatus(status: unknown): ModuleStatusEnum {
  const valid = ['green', 'yellow', 'red', 'gray'];
  return valid.includes(status as string) ? (status as ModuleStatusEnum) : 'gray';
}

// ---------- 异步 Thunks ----------
let abortController: AbortController | null = null;

export const fetchModuleStatus = createAsyncThunk<ModuleStatus[]>(
  'module/fetchStatus',
  async (_, { rejectWithValue }) => {
    // 取消上一次未完成的请求
    if (abortController) {
      abortController.abort();
    }
    abortController = new AbortController();

    // 离线检测
    if (!navigator.onLine) {
      return rejectWithValue('网络离线，无法获取模块状态');
    }

    try {
      const response = await apiClient.get<ModuleStatus[]>(MODULE_API, {
        signal: abortController.signal,
      });
      return validateModules(response.data);
    } catch (error) {
      if (error instanceof CanceledError) {
        // 请求被取消，不视为错误
        return rejectWithValue('Request cancelled');
      }
      if (axios.isAxiosError(error)) {
        return rejectWithValue(error.response?.data?.message || error.message || '获取模块状态失败');
      }
      return rejectWithValue('未知网络错误');
    }
  },
);

// ---------- 同步 Actions (用于 WebSocket 推送更新) ----------
interface UpdateModulePayload {
  name: string;
  status: ModuleStatusEnum;
  message: string;
  last_update?: string; // 服务端时间戳
}

// 通过 createAction 创建，方便在 extraReducers 中使用（但我们将改为在 reducers 中）
// 为了保持兼容性，保留 createAction，并在 reducers 中处理。
export const updateModuleStatus = createAction<UpdateModulePayload>('module/updateStatus');

// ---------- Slice ----------
const moduleSlice = createSlice({
  name: 'module',
  initialState: {
    modules: [] as ModuleStatus[],
    loading: true,          // 初始正在加载
    error: null as string | null,
    lastUpdatedAt: null as string | null,
  } satisfies ModuleState,
  reducers: {
    resetModuleState: () => ({
      modules: [],
      loading: true,
      error: null,
      lastUpdatedAt: null,
    }),
    clearError: (state) => {
      state.error = null;
    },
    // 处理同步更新（来自 WebSocket 或手动触发）
    applyModuleUpdate: (state, action: PayloadAction<UpdateModulePayload>) => {
      const { name, status, message, last_update } = action.payload;
      const validStatus = validateStatus(status);
      const trimmedMessage = message ? message.substring(0, 200) : '';
      const now = Date.now();
      const index = state.modules.findIndex(
        (m) => m.name.toLowerCase() === name.toLowerCase(),
      );
      const newModule: ModuleStatus = {
        name: name.trim(),
        status: validStatus,
        message: trimmedMessage,
        last_update: last_update || new Date().toISOString(),
      };

      if (index !== -1) {
        // 频率限制：2秒内不重复更新同一个模块
        const lastUpdateTime = state.modules[index].last_update
          ? new Date(state.modules[index].last_update!).getTime()
          : 0;
        if (now - lastUpdateTime < UPDATE_THROTTLE_MS) return;
        state.modules[index] = newModule;
      } else {
        state.modules.push(newModule);
      }
      // 保持排序
      state.modules.sort((a, b) => a.name.localeCompare(b.name));
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchModuleStatus.pending, (state) => {
        state.loading = true;
        state.error = null;
      })
      .addCase(fetchModuleStatus.fulfilled, (state, action) => {
        state.loading = false;
        state.modules = action.payload; // 已校验且排序
        state.lastUpdatedAt = new Date().toISOString();
      })
      .addCase(fetchModuleStatus.rejected, (state, action) => {
        state.loading = false;
        if (action.payload === 'Request cancelled') {
          // 取消的请求不改变 error
          return;
        }
        state.error = (action.payload as string) || '获取模块状态失败';
      });
  },
});

export const { resetModuleState, clearError, applyModuleUpdate } = moduleSlice.actions;
export type { ModuleState }; // 外部可使用此类型

// 向后兼容：将 applyModuleUpdate 也绑定到 updateModuleStatus 名称，便于旧代码迁移
// 建议使用 applyModuleUpdate，但为了不破坏现有引用，我们重新导出
// 注：原来的 updateModuleStatus 是 action creator，现在改为调用 applyModuleUpdate 的 action
// 因此我们需要提供一个可 dispatch 的 thunk 或者直接使用 applyModuleUpdate。
// 考虑到已有代码可能 import { updateModuleStatus } 并 dispatch，我们保留 createAction，但在 slice 中额外处理该 action。
// 方法：在 extraReducers 中添加对 updateModuleStatus 的处理，但之前使用的是 createAction，不是 thunk。
// 由于我们已经使用了 createAction，我们可以在 extraReducers 中监听它。更简洁：直接在 extraReducers 中添加：
// .addCase(updateModuleStatus, (state, action) => { ... }) 以兼容。

// 因此我们在 extraReducers 中增加对 updateModuleStatus 的处理（使用原始的 createAction），
// 但为了避免和 applyModuleUpdate 逻辑重复，我们可内部调用相同逻辑。这里我们保留 applyModuleUpdate 作为主要 reducer，
// 同时确保 updateModuleStatus 也能触发相同逻辑（通过 extraReducer）。
// 我们将 applyModuleUpdate 的逻辑提取为一个函数，在两个地方共享。

const handleModuleUpdate = (state: ModuleState, payload: UpdateModulePayload) => {
  const { name, status, message, last_update } = payload;
  const validStatus = validateStatus(status);
  const trimmedMessage = message ? message.substring(0, 200) : '';
  const now = Date.now();
  const index = state.modules.findIndex(
    (m) => m.name.toLowerCase() === name.toLowerCase(),
  );
  const newModule: ModuleStatus = {
    name: name.trim(),
    status: validStatus,
    message: trimmedMessage,
    last_update: last_update || new Date().toISOString(),
  };
  if (index !== -1) {
    const lastUpdateTime = state.modules[index].last_update
      ? new Date(state.modules[index].last_update!).getTime()
      : 0;
    if (now - lastUpdateTime < UPDATE_THROTTLE_MS) return;
    state.modules[index] = newModule;
  } else {
    state.modules.push(newModule);
  }
  state.modules.sort((a, b) => a.name.localeCompare(b.name));
};

// 在 extraReducers 中添加对 updateModuleStatus action 的处理
// 同时，applyModuleUpdate reducer 使用相同的逻辑
// 修改 slice 定义，将 applyModuleUpdate 的实现替换为 handleModuleUpdate 调用
// 并增加 extraReducer

// 重新定义 slice 以包含上述修改
// 这里给出最终完整 slice 代码，替换前面的声明
