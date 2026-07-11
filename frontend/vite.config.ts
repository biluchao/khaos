// =============================================================================
// KHAOS 前端构建配置 v2.0 (华尔街机构级强化)
// =============================================================================
// 整合 PWA、brotli 压缩、可视化分析、路径别名、代理与性能优化。
// 适用于 2000 美金至万亿美金账户的量化交易系统前端。
// 所有敏感配置已锁定，生产构建自动剔除调试代码。
// =============================================================================

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';
import compression from 'vite-plugin-compression';
import { visualizer } from 'rollup-plugin-visualizer';
import autoprefixer from 'autoprefixer';
import { resolve } from 'path';

export default defineConfig(({ command, mode }) => {
  const isProduction = mode === 'production';
  const isAnalysis = process.env.ANALYZE === 'true';

  return {
    root: process.cwd(),
    base: './',

    // 开发服务器
    server: {
      port: 3000,
      strictPort: false,
      open: false,
      cors: true,
      fs: {
        strict: true,
        allow: ['.'], // 允许项目根目录
      },
      proxy: {
        '/api': {
          target: 'http://localhost:8000',
          changeOrigin: true,
          secure: false,
          timeout: 30000,
        },
        '/ws': {
          target: 'http://localhost:8000',
          ws: true,
          changeOrigin: true,
          timeout: 30000,
          rewrite: (path) => path.replace(/^\/ws/, '/ws'),
        },
      },
    },

    // 预览服务器
    preview: {
      port: 4173,
      strictPort: true,
    },

    // 构建配置
    build: {
      target: 'es2022',
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: isAnalysis ? 'hidden' : false,
      minify: 'terser',
      cssCodeSplit: false,
      assetsInlineLimit: 4096,
      chunkSizeWarningLimit: 1000, // 适配 4K 中文界面大资源
      terserOptions: {
        compress: {
          drop_console: false,
          pure_funcs: ['console.log', 'console.debug', 'console.time', 'console.timeEnd'],
        },
        mangle: {
          reserved: ['$super', 'exports', 'require'], // 金融系统避免混淆关键变量
        },
        output: {
          comments: false,
        },
      },
      rollupOptions: {
        output: {
          manualChunks: {
            vendor: ['react', 'react-dom', 'react-router-dom'],
            antd: ['antd', '@ant-design/icons'],
            charts: ['echarts', 'echarts-for-react', 'klinecharts'],
            redux: ['@reduxjs/toolkit', 'react-redux'],
          },
        },
      },
    },

    // ESBuild 目标
    esbuild: {
      target: 'es2022',
    },

    // 解析别名
    resolve: {
      alias: {
        '@': resolve(__dirname, 'src'),
      },
    },

    // CSS 配置
    css: {
      postcss: {
        plugins: [
          autoprefixer({
            overrideBrowserslist: [
              'last 2 Chrome versions',
              'last 2 Firefox versions',
              'last 2 Safari versions',
              'last 2 Edge versions',
            ],
          }),
        ],
      },
    },

    // 优化依赖预构建
    optimizeDeps: {
      include: [
        'react',
        'react-dom',
        'react-router-dom',
        '@reduxjs/toolkit',
        'react-redux',
        'antd',
        '@ant-design/icons',
        'echarts',
        'echarts-for-react',
        'klinecharts',
        'dayjs',
        'axios',
        'react-helmet-async',
      ],
    },

    // 全局变量定义
    define: {
      __APP_VERSION__: JSON.stringify(process.env.npm_package_version || '1.0.0'),
      __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
    },

    // 插件
    plugins: [
      react({
        jsxRuntime: 'automatic',
      }),

      VitePWA({
        registerType: 'prompt',
        includeAssets: [
          'favicon.ico',
          'apple-touch-icon.png',
          'offline.html',
          'fonts/inter-var.woff2',
          'icons/logo48.png',
          'icons/logo96.png',
          'icons/logo144.png',
          'icons/logo192.png',
          'icons/logo512.png',
          'icons/maskable-192.png',
          'icons/maskable-512.png',
        ],
        workbox: {
          globPatterns: [
            '**/*.{html,js,css,png,ico,woff2,json}',
          ],
          runtimeCaching: [
            {
              urlPattern: /^https:\/\/api\.example\.com\/.*/,
              handler: 'NetworkFirst',
              options: {
                cacheName: 'api-cache',
                networkTimeoutSeconds: 5,
                expiration: {
                  maxEntries: 50,
                  maxAgeSeconds: 60 * 60 * 24,
                },
                cacheableResponse: {
                  statuses: [0, 200],
                },
              },
            },
          ],
        },
        manifest: false,
      }),

      compression({
        algorithm: 'brotli',
        ext: '.br',
        threshold: 10240,
        deleteOriginFile: false,
      }),

      compression({
        algorithm: 'gzip',
        ext: '.gz',
        threshold: 10240,
        deleteOriginFile: false,
      }),

      isAnalysis
        ? visualizer({
            open: true,
            gzipSize: true,
            brotliSize: true,
            filename: '.analysis/stats.html', // 避免泄露到 dist
          })
        : null,
    ].filter(Boolean),
  };
});
