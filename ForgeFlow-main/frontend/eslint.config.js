import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  // `dist` is build output. `_*.tsx` / `_*.ts` are ad-hoc dev harnesses kept at the
  // frontend root (never imported by `src/`, never part of the Vite build, and
  // excluded from git by the repo's `_*` hygiene rule) — linting them only produces
  // noise that keeps the gate red.
  globalIgnores(['dist', '_*.tsx', '_*.ts']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // The `const { a, b, ...rest } = obj` idiom uses the named bindings purely to
      // *omit* them from `rest` (see `e2e/inc36_conversation.spec.ts::summaryOf`).
      // That is intentional, not dead code — `ignoreRestSiblings` is the canonical
      // option for exactly this pattern.
      '@typescript-eslint/no-unused-vars': [
        'error',
        { ignoreRestSiblings: true, argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
])
