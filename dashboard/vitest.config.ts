import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('.', import.meta.url)),
      'server-only': fileURLToPath(
        new URL('./src/test/server-only-stub.ts', import.meta.url),
      ),
    },
  },
  test: {
    environment: 'jsdom',
    environmentOptions: {
      jsdom: {
        pretendToBeVisual: true,
      },
    },
    restoreMocks: true,
    setupFiles: ['./src/test/setup.ts'],
  },
});
