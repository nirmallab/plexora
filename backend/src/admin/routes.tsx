/**
 * /admin: auth, the HTML-or-JSON pages, and the API. Every mutation passes
 * the same-origin guard; every request is metered into budget_daily.
 */
import { Hono } from 'hono';

import { nowSeconds } from '../env';
import { type App, type AppEnv, jsonError, NO_STORE, page, readJson, sameOriginGuard, wantsHtml } from '../http';
import { Meter } from '../telemetry/budget';
import * as q from '../telemetry/queries';
import { accessConfigured } from './access';
import { api } from './api';
import { adminIdentity, checkAdminToken, clearSessionCookie, mintSessionCookie, setSessionCookie } from './auth';
import {
  BackupsPage,
  BudgetPage,
  DataPage,
  ErrorDetailPage,
  ErrorsPage,
  FeaturesPage,
  InstallPage,
  LoginPage,
  PerformancePage,
  UsagePage,
} from './pages';

export const admin = new Hono<AppEnv>();

/** Only same-origin admin paths are honoured as a post-login destination. */
export function safeNext(value: string | undefined | null): string {
  if (!value || value.includes('//') || value.includes('\\')) return '/admin';
  return /^\/admin(?:[/?][\w\-./?=&%]*)?$/.test(value) ? value : '/admin';
}

admin.use('*', sameOriginGuard);

// Only an authenticated request pays for its own budget write; an anonymous
// one is counted in isolate memory, so a flood of 401s cannot spend D1 writes.
admin.use('*', async (c, next) => {
  const meter = new Meter(c.env.TELEMETRY_DB);
  c.set('meter', meter);
  await next();
  if (c.get('admin')) await meter.flush(c.env, nowSeconds(), { admin_requests: 1 });
});

admin.get('/login', (c) => page(c, (<LoginPage next={safeNext(c.req.query('next'))} access={accessConfigured(c.env)} />).toString()));

admin.post('/login', async (c) => {
  const body = await readJson(c);
  if (typeof body.token !== 'string' || !(await checkAdminToken(c, body.token))) {
    return jsonError(c, 401, 'Wrong token.');
  }
  const cookie = await mintSessionCookie(c, 'token', nowSeconds());
  if (!cookie) return jsonError(c, 503, 'Admin sign-in is not configured.');
  setSessionCookie(c, cookie);
  return c.json({ ok: true }, 200, NO_STORE);
});

admin.post('/logout', (c) => {
  clearSessionCookie(c);
  return c.json({ ok: true }, 200, NO_STORE);
});

// Everything below needs an identity: 302 to sign-in for a browser, 401 JSON otherwise.
admin.use('*', async (c, next) => {
  const who = await adminIdentity(c, nowSeconds());
  if (!who) {
    if (wantsHtml(c)) {
      const url = new URL(c.req.url);
      return c.redirect(`/admin/login?next=${encodeURIComponent(safeNext(url.pathname + url.search))}`, 302);
    }
    return jsonError(c, 401, 'Admin authentication required.');
  }
  c.set('admin', who);
  return next();
});

admin.get('/', (c) => c.redirect('/admin/usage', 302));

async function view<T>(c: App, data: T | null, render: (d: T) => unknown) {
  if (data === null) return jsonError(c, 404, 'Not found.');
  if (!wantsHtml(c)) return c.json(data as object, 200, NO_STORE);
  return page(c, String(await (render(data) as Promise<string> | string)));
}

const query = (c: App) => c.req.query() as Record<string, string | undefined>;

admin.get('/usage', async (c) => {
  const d = await q.usage(c.var.meter, q.parseFilters(query(c), nowSeconds(), 30));
  return view(c, d, (x) => <UsagePage d={x} who={c.var.admin} />);
});
admin.get('/data', async (c) => {
  const d = await q.data(c.var.meter, q.parseFilters(query(c), nowSeconds(), 30));
  return view(c, d, (x) => <DataPage d={x} who={c.var.admin} />);
});
admin.get('/features', async (c) => {
  const d = await q.features(c.var.meter, q.parseFilters(query(c), nowSeconds(), 30));
  return view(c, d, (x) => <FeaturesPage d={x} who={c.var.admin} />);
});
admin.get('/performance', async (c) => {
  const d = await q.performance(c.var.meter, q.parseFilters(query(c), nowSeconds(), 14), c.req.query('axis') ?? 'none');
  return view(c, d, (x) => <PerformancePage d={x} who={c.var.admin} />);
});
admin.get('/errors', async (c) => {
  const d = await q.errors(c.var.meter, q.parseFilters(query(c), nowSeconds(), 30));
  return view(c, d, (x) => <ErrorsPage d={x} who={c.var.admin} />);
});
admin.get('/errors/:fp', async (c) => {
  const d = await q.errorDetail(c.var.meter, c.req.param('fp'), nowSeconds());
  return view(c, d, (x) => <ErrorDetailPage d={x} who={c.var.admin} />);
});
admin.get('/budget', async (c) => {
  const d = await q.budget(c.var.meter, c.env, nowSeconds());
  return view(c, d, (x) => <BudgetPage d={x} who={c.var.admin} />);
});
admin.get('/backups', async (c) => {
  const d = await q.backups(c.var.meter);
  return view(c, d, (x) => <BackupsPage d={x} who={c.var.admin} />);
});
admin.get('/installs/:id', async (c) => {
  const d = await q.install(c.var.meter, c.req.param('id'), nowSeconds());
  return view(c, d, (x) => <InstallPage d={x} who={c.var.admin} />);
});

admin.route('/api', api);
