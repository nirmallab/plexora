/**
 * Plexora licence service: one host serving /v1 (the Plexora client),
 * /portal (licence holders), /admin (us) and /healthz, plus the daily cron.
 */
import { Hono } from 'hono';

import { signingConfigured } from './certs';
import { withSettings } from './ai/settings';
import { scheduled } from './cron';
import { ensureLateColumns } from './db';
import type { Env } from './env';
import { nowSeconds } from './env';
import { ApiError, type AppEnv, fail, NO_STORE } from './http';
import { admin } from './routes/admin';
import { ai } from './routes/ai';
import { portal } from './routes/portal';
import { v1 } from './routes/v1';

const app = new Hono<AppEnv>();

// Plexora AI first: its paths sit under /v1 but it is its own group.
app.route('/v1/ai', ai);
app.route('/v1', v1);
app.route('/portal', portal);
app.route('/admin', admin);

/** Liveness. No D1 read, so a monitor polling it costs nothing. */
app.get('/healthz', (c) => c.json({ ok: true, ts: nowSeconds(), signing: signingConfigured(c.env) }, 200, NO_STORE));

app.get('/', (c) => c.redirect('/portal'));

app.notFound((c) => fail(c, new ApiError(404, 'not_found', 'Not found.')));
app.onError((error, c) => {
  if (error instanceof ApiError) return fail(c, error);
  console.error('unhandled', error);
  return fail(c, new ApiError(500, 'internal_error', 'Internal error.'));
});

/** The admin's AI settings (`ai_settings`) apply to the gateway, the admin pages and the cron; /healthz and
 * the licence paths never read them. */
const SETTINGS_PATHS = ['/v1/ai/', '/admin'];

export default {
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    const ai = SETTINGS_PATHS.some((p) => path.startsWith(p));
    // Every path: /v1/refresh writes environments.last_mcp_at, a late column.
    // After the first request in an isolate this is one boolean check.
    await ensureLateColumns(env);
    return app.fetch(request, ai ? await withSettings(env) : env, ctx);
  },
  async scheduled(event, env, ctx) {
    await ensureLateColumns(env);
    return scheduled(event, await withSettings(env), ctx);
  },
} satisfies ExportedHandler<Env>;
