import { defineConfig } from 'vitest/config';

/** `npm test` runs all three projects; `npm run test:node|test:workers|test:dev` runs one. */
export default defineConfig({
  test: {
    projects: ['./vitest.node.config.ts', './vitest.workers.config.ts', './vitest.dev.config.ts'],
  },
});
