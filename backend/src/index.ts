/**
 * Plexora telemetry Worker: one host serving /v1/telemetry/*, /healthz and
 * /admin/*, plus the quarter-hourly maintenance cron.
 */
import { Hono } from 'hono';

import { admin } from './admin/routes';
import type { Env } from './env';
import { nowSeconds } from './env';
import { type AppEnv, NO_STORE } from './http';
import { addPending, cachedState } from './telemetry/budget';
import { scheduled } from './telemetry/cron';
import { telemetry } from './telemetry/ingest';

const app = new Hono<AppEnv>();

// Every request counts against the Worker request share. Counted in isolate
// memory and added to the next budget_daily upsert, so a request that writes
// nothing (a 401, /healthz) costs no D1 write to meter.
app.use('*', async (_c, next) => {
  addPending('requests');
  await next();
});

// No CORS on /v1: the only caller is the Plexora server process, never a page.
app.route('/v1/telemetry', telemetry);

/** Liveness, from the isolate cache only: never a D1 read. */
app.get('/healthz', (c) => {
  const now = nowSeconds();
  return c.json({ ok: true, ts: now, budget_state: cachedState(now) }, 200, NO_STORE);
});

app.route('/admin', admin);

app.notFound((c) => c.json({ error: 'Not found.' }, 404, NO_STORE));
app.onError((error, c) => {
  console.error('unhandled', error);
  return c.json({ error: 'Internal error.' }, 500, NO_STORE);
});

export default {
  fetch: app.fetch,
  scheduled,
} satisfies ExportedHandler<Env>;
