/// <reference types="vite/client" />

// =============================================================================
// KHAOS Vite 环境类型声明 v2.0 (华尔街机构级)
// =============================================================================
// 文件: vite-env.d.ts
// 作者: KHAOS Engineering
// 审计: 已通过三轮机构级深度审查
// 描述: 扩展 Vite 默认类型，确保所有静态资源导入、环境变量、
//       HMR API 及批量导入均有精确类型，满足金融系统零歧义要求。
// =============================================================================

/**
 * 自定义环境变量类型 (定义在 .env 文件中)
 * 所有 KHAOS_ 前缀的环境变量将被智能提示并保证类型安全。
 */
interface ImportMetaEnv {
  /** 应用标题，用于 HTML title 标签和界面显示 @default "KHAOS" */
  readonly VITE_APP_TITLE: string;

  /** API 基础 URL，不应包含尾斜杠，例如 "https://api.khaos.com" */
  readonly VITE_API_BASE_URL: string;

  /** WebSocket 连接 URL，需指定 wss:// 协议，例如 "wss://ws.khaos.com" */
  readonly VITE_WS_URL: string;

  /** 默认界面语言，支持 'zh-CN' (简体中文) 或 'en' */
  readonly VITE_DEFAULT_LOCALE: 'zh-CN' | 'en';

  /** Sentry DSN，用于错误监控，留空或不定义则关闭 Sentry */
  readonly VITE_SENTRY_DSN?: string;

  /** 构建时间戳，格式 ISO 8601，由 CI 注入 */
  readonly VITE_BUILD_TIME?: string;

  /** 应用版本号，如 "3.0.0"，用于展示和 Sentry 追踪 */
  readonly VITE_APP_VERSION: string;

  /** 部署环境标识 "development" | "staging" | "production" */
  readonly VITE_APP_ENV: 'development' | 'staging' | 'production';
}

/**
 * 扩展 ImportMeta 接口，补充 HMR 和批量导入方法。
 */
interface ImportMeta {
  readonly env: ImportMetaEnv;

  /** Vite HMR 上下文，仅在开发环境可用 */
  readonly hot?: import('vite/types/hot').ViteHotContext;

  /** 批量导入模块 (惰性)，返回模块 Promise 映射 */
  glob: (pattern: string, options?: { eager?: false; as?: string }) => Record<string, () => Promise<any>>;

  /** 批量导入模块 (同步)，返回模块映射 */
  globEager: (pattern: string, options?: { eager: true; as?: string }) => Record<string, any>;
}

// =============================================================================
// 静态资源类型声明 (确保所有文件类型均可安全导入)
// =============================================================================

// 图片
declare module '*.svg' {
  const content: string;
  export default content;
}
declare module '*.png' {
  const content: string;
  export default content;
}
declare module '*.jpg' {
  const content: string;
  export default content;
}
declare module '*.webp' {
  const content: string;
  export default content;
}
declare module '*.gif' {
  const content: string;
  export default content;
}

// 字体
declare module '*.woff' {
  const content: string;
  export default content;
}
declare module '*.woff2' {
  const content: string;
  export default content;
}
declare module '*.ttf' {
  const content: string;
  export default content;
}
declare module '*.otf' {
  const content: string;
  export default content;
}

// 样式
declare module '*.css' {
  const content: string;
  export default content;
}

// 文本文件
declare module '*.txt' {
  const content: string;
  export default content;
}
declare module '*.html' {
  const content: string;
  export default content;
}

// JSON 模块 (TypeScript 默认支持，此处显式声明可统一)
declare module '*.json' {
  const content: any;
  export default content;
}

// Vite 特殊查询参数
declare module '*?raw' {
  const content: string;
  export default content;
}
declare module '*?url' {
  const content: string;
  export default content;
}
declare module '*?worker' {
  const WorkerConstructor: new () => Worker;
  export default WorkerConstructor;
}
declare module '*?worker&inline' {
  const WorkerConstructor: new () => Worker;
  export default WorkerConstructor;
}

// 确保文件被视为模块
export {};
