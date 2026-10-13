import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { FlatCompat } from '@eslint/eslintrc';

const baseDirectory = dirname(fileURLToPath(import.meta.url));
const compat = new FlatCompat({
  baseDirectory,
  resolvePluginsRelativeTo: dirname(fileURLToPath(import.meta.resolve('eslint-config-next'))),
});

const config = [
  {
    ignores: [
      '.next/**',
      'node_modules/**',
      'next-env.d.ts',
      'test-results/**',
      'playwright-report/**',
    ],
  },
  ...compat.extends('next/core-web-vitals', 'next/typescript'),
  {
    files: ['tests/**/*.ts'],
    // Playwright's fixture callback named use is not a React hook.
    rules: { 'react-hooks/rules-of-hooks': 'off' },
  },
];

export default config;
