// =============================================================================
// KHAOS 量化交易系统 - Vitest 测试配置 v2.0 (华尔街机构级强化)
// =============================================================================
// 提供全局测试环境、路径别名、覆盖率高门槛、确定性与性能保障。
// 适用于 2000 美金账户到万亿美金账户的金融软件质量保障。
// 注意：请确保在 tsconfig.json 的 "types" 中加入 "vitest/globals"。
// =============================================================================

import { defineConfig } from 'vitest/config';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  test: {
    // ---------- 根目录与文件发现 ----------
    root: process.cwd(),
    include: ['tests/**/*.{test,spec}.{ts,tsx}'],
    exclude: [
      'tests/fixtures/**',
      'tests/setup.ts',
      'node_modules',
      'dist',
    ],

    // ---------- 全局变量 ----------
    globals: true,

    // ---------- 测试环境 ----------
    environment: 'jsdom',
    environmentOptions: {
      jsdom: {
        url: 'http://localhost:3000',
      },
    },

    // ---------- 安装文件 ----------
    // 需要在项目 tests/ 目录下创建 setup.ts，引入 @testing-library/jest-dom 等
    setupFiles: ['./tests/setup.ts'],

    // ---------- Mock 清理与恢复 ----------
    clearMocks: true,
    restoreMocks: true,

    // ---------- 超时与重试 ----------
    testTimeout: 10000,               // 单测试用例最大毫秒数
    hookTimeout: 8000,               // 钩子最大毫秒数
    suiteTimeout: 60000,             // 整个套件最大毫秒数
    slowTestThreshold: 3000,         // 超过 3 秒标记为慢测试
    retry: 0,                        // 金融系统禁止重试，确保每次失败可追溯

    // ---------- 并发与快速失败 ----------
    maxConcurrency: 4,               // 限制同时运行的测试文件数
    bail: 1,                         // 首个失败即停止，节省 CI 资源

    // ---------- 覆盖率配置 ----------
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/**/*.spec.{ts,tsx}',
        'src/**/*.d.ts',
        'src/vite-env.d.ts',
        'src/serviceWorker.ts',
        'scripts/',
        'deploy/',
        'migrations/',
      ],
      thresholds: {
        lines: 90,
        functions: 90,
        branches: 85,
        statements: 90,
        perFile: true,               // 每个文件都需达到阈值
      },
      reporter: ['text', 'html', 'lcov', 'json-summary'],
      reportsDirectory: './coverage',
    },

    // ---------- 路径别名 ----------
    alias: {
      '@': path.resolve(__dirname, './src'),
    },

    // ---------- 依赖内联 ----------
    deps: {
      inline: ['@testing-library/react', '@testing-library/jest-dom'],
    },

    // ---------- 报告器 ----------
    reporters: ['default', 'json'],
    outputFile: {
      json: path.resolve(__dirname, './reports/test-results.json'),
    },

    // ---------- 内存与性能日志 ----------
    logHeapUsage: true,
  },
});
