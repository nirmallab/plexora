import { cloudflareTest } from '@cloudflare/vitest-plugin';
import { defineConfig } from 'vitest/config';

/**
 * The Worker and its Durable Object in workerd, as `wrangler dev` runs them, driven through the exported fetch handler
 * (so a test can hand it another env: the licence switch, the private-host switch). The globalSetup's TCP and HTTP
 * servers are on 127.0.0.1, which `ALLOW_PRIVATE_HOSTS` lets /v1/ssh reach.
 */
export default defineConfig({
  plugins: [
    cloudflareTest(({ inject }) => {
      // test/fixtures.ts `Fixtures`, the ports part (not imported: the config loader wants no extensionless imports).
      const fixtures = inject<{ echoPort: number; bannerPort: number; deadPort: number; featuresPort: number }>('fixtures');
      return {
        wrangler: { configPath: './wrangler.toml' },
        miniflare: {
          bindings: {
            ALLOWED_ORIGINS: 'https://viewer.plexoraapp.com http://localhost:*',
            SSH_PORTS: `22,${fixtures.echoPort},${fixtures.bannerPort},${fixtures.deadPort}`,
            ALLOW_PRIVATE_HOSTS: '1',
            FEATURES_URL: `http://127.0.0.1:${fixtures.featuresPort}/free`,
            ROOM_IDLE_DAYS: '7',
          },
          // The real local limiters, raised so the suites never meet them; ratelimit.test.ts stubs its own.
          ratelimits: {
            SSH_LIMIT: { namespace_id: '7301', simple: { limit: 100_000, period: 60 } },
            ROOM_CREATE_LIMIT: { namespace_id: '7302', simple: { limit: 100_000, period: 60 } },
          },
        },
      };
    }),
  ],
  test: {
    name: 'workers',
    include: ['test/workers/**/*.test.ts'],
    globalSetup: ['./test/workers/global-setup.ts'],
  },
});
