/**
 * Plexora licence service: one host serving /v1 (the Plexora client),
 * /portal (licence holders), /admin (us) and /healthz, plus the daily cron.
 */
import { Hono } from 'hono';

import { signingConfigured } from './certs';
import { scheduled } from './cron';
import type { Env } from './env';
import { nowSeconds } from './env';
import { ApiError, type AppEnv, fail, NO_STORE } from './http';
import { admin } from './routes/admin';
import { portal } from './routes/portal';
import { v1 } from './routes/v1';

const app = new Hono<AppEnv>();

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

export default {
  fetch: app.fetch,
  scheduled,
} satisfies ExportedHandler<Env>;
