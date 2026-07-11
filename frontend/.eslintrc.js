// =============================================================================
// KHAOS 前端 ESLint 配置 v2.0 (华尔街机构级强化)
// =============================================================================
// 确保代码零隐患，符合金融系统安全性与可维护性要求。
// 依赖说明：请确保在 package.json 的 devDependencies 中安装以下额外包：
//   - eslint-import-resolver-typescript (配合 import/resolver)
//   - @typescript-eslint/eslint-plugin、@typescript-eslint/parser 已包含
// =============================================================================

module.exports = {
  root: true,

  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
    project: './tsconfig.json',             // 关联 TS 配置，确保在项目根目录运行
    tsconfigRootDir: __dirname,
  },

  env: {
    browser: true,
    es2022: true,
    node: false,
  },

  // 规则集 (最后一项 prettier 覆盖格式)
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:@typescript-eslint/recommended-requiring-type-checking',
    'plugin:react/recommended',
    'plugin:react/jsx-runtime',
    'plugin:react-hooks/recommended',
    'plugin:import/recommended',
    'plugin:import/typescript',
    'prettier',
  ],

  plugins: [
    '@typescript-eslint',
    'react',
    'react-hooks',
    'import',
  ],

  settings: {
    react: { version: 'detect' },
    'import/resolver': {
      typescript: {
        alwaysTryTypes: true,
        project: './tsconfig.json',
      },
    },
  },

  rules: {
    // ---------- TypeScript 严格规则 ----------
    '@typescript-eslint/explicit-function-return-type': 'off',
    '@typescript-eslint/no-explicit-any': 'warn',                       // 金融系统提示风险
    '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    '@typescript-eslint/no-non-null-assertion': 'error',
    '@typescript-eslint/prefer-optional-chain': 'error',
    '@typescript-eslint/prefer-nullish-coalescing': 'error',
    '@typescript-eslint/consistent-type-imports': 'off',               // 不强制 type 导入
    '@typescript-eslint/consistent-type-exports': 'off',
    '@typescript-eslint/no-floating-promises': 'error',
    '@typescript-eslint/no-misused-promises': 'error',
    '@typescript-eslint/await-thenable': 'error',
    '@typescript-eslint/require-await': 'error',
    '@typescript-eslint/no-unnecessary-condition': 'warn',

    // ---------- React 与 JSX ----------
    'react/prop-types': 'off',
    'react/self-closing-comp': 'error',
    'react/jsx-no-target-blank': 'error',                               // 安全
    'react/jsx-no-useless-fragment': 'error',
    'react/no-danger': 'error',                                         // 禁止 dangerouslySetInnerHTML，防范 XSS
    'react-hooks/exhaustive-deps': 'error',                            // 强制正确的依赖数组

    // ---------- 导入规则 ----------
    'import/order': [
      'error',
      {
        groups: ['builtin', 'external', 'internal', 'parent', 'sibling', 'index', 'type'],
        pathGroups: [{ pattern: '@/**', group: 'internal', position: 'before' }],
        pathGroupsExcludedImportTypes: ['type'],
        'newlines-between': 'always',
        alphabetize: { order: 'asc', caseInsensitive: true },
      },
    ],
    'import/no-cycle': 'warn',
    'import/no-duplicates': 'error',
    'import/no-self-import': 'error',
    'import/named': 'off',                                              // TS 项目中关闭，提升性能
    'import/no-relative-packages': 'error',                             // 适用于 monorepo，保留

    // ---------- 通用最佳实践 ----------
    'no-console': ['warn', { allow: ['warn', 'error'] }],
    'no-debugger': 'error',
    'no-alert': 'error',
    'no-eval': 'error',
    'no-implied-eval': 'error',
    'no-new-func': 'error',
    'no-param-reassign': ['error', { props: true, ignorePropertyModificationsFor: ['draft'] }], // 兼容 Immer
    'prefer-const': 'error',
    'spaced-comment': ['error', 'always', { markers: ['/'] }],
    'max-lines': ['warn', { max: 350, skipBlankLines: true, skipComments: true }],
    'complexity': ['warn', 15],
  },

  // 报告未使用的 eslint-disable 注释，确保代码审查透明度
  reportUnusedDisableDirectives: true,

  ignorePatterns: [
    'node_modules',
    'dist',
    'build',
    'coverage',
    '*.js',
    '!src/**/*.js',
  ],

  overrides: [
    {
      files: ['*.test.ts', '*.test.tsx', '*.spec.ts', '*.spec.tsx'],
      env: { jest: true },
      rules: {
        'no-console': 'off',
        '@typescript-eslint/no-explicit-any': 'off',
        'max-lines': 'off',
      },
    },
    {
      files: ['vite.config.ts', 'vitest.config.ts'],
      rules: {
        'import/no-default-export': 'off',
      },
    },
  ],
};
