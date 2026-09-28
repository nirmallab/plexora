import { gunzipSync } from 'node:zlib';

import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import worker from '../../src/index';
import { dayOf, type Env } from '../../src/env';
import { dailyKey } from '../../src/telemetry/archive';
import { tick } from '../../src/telemetry/cron';
import { CLIENT_JS } from '../../src/ui/client';
import { STYLES } from '../../src/ui/styles';
import { admin, ADMIN_TOKEN, adminPost, BASE, count, hexId, sampleBatch, seconds, tokenFor, upload } from './helpers';

const DAY = 86400;

async function seedAndMaintain() {
  const ids: string[] = [];
  for (let i = 0; i < 2; i += 1) {
    const id = hexId(`${i}9`);
    ids.push(id);
    expect((await upload(sampleBatch(id), await tokenFor(id))).status).toBe(202);
  }
  for (let i = 0; i < 10; i += 1) if (!(await tick(env, seconds() + DAY)).step) break;
  return ids;
}

/** Calls the Worker directly, so a test can override a [vars] knob. */
function direct(path: string, overrides: Partial<Env> = {}, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set('Authorization', `Bearer ${ADMIN_TOKEN}`);
  return worker.fetch(new Request(`${BASE}${path}`, { ...init, headers }), { ...env, ...overrides } as Env, {
    waitUntil() {},
    passThroughOnException() {},
  } as unknown as ExecutionContext);
}

async function b64sha256(text: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(digest)));
}

describe('admin pages', () => {
  it('every page renders HTML under a default-src none CSP, with no external asset', async () => {
    const ids = await seedAndMaintain();
    const paths = ['usage', 'data', 'features', 'performance?axis=browser', 'errors', 'errors/0123456789abcdef', 'budget', 'backups', `installs/${ids[0]}`];
    for (const path of paths) {
      const response = await admin(`/admin/${path}`, { headers: { Accept: 'text/html' } });
      expect(response.status, path).toBe(200);
      const csp = response.headers.get('Content-Security-Policy')!;
      expect(csp).toContain("default-src 'none'");
      expect(csp).toContain(`script-src 'sha256-${await b64sha256(CLIENT_JS)}'`);
      expect(csp).toContain(`style-src 'sha256-${await b64sha256(STYLES)}'`);
      const html = await response.text();
      expect(html.startsWith('<!doctype html>')).toBe(true);
      expect(html).not.toMatch(/(src|href)="https?:/);
      expect(html).not.toMatch(/ style="/);
      expect(html).not.toMatch(/ on[a-z]+="/);
      expect(html).toContain(`<script>${CLIENT_JS}</script>`);
    }
  });

  it('the same URLs answer JSON for scripts', async () => {
    await seedAndMaintain();
    const features = (await (await admin('/admin/features')).json()) as Record<string, any>;
    expect(features.plugins[0]).toMatchObject({ plugin: 'gating', opens: 24 });
    expect(features.unused).toContain('roi.create');
    const perf = (await (await admin('/admin/performance?axis=browser')).json()) as Record<string, any>;
    expect(perf.metrics.find((m: any) => m.metric === 'render.summary.tile_ms').value).toBe('firefox');
    const errors = (await (await admin('/admin/errors')).json()) as Record<string, any>;
    expect(errors.errors[0]).toMatchObject({ fp: '0123456789abcdef', count: 24, exception: 'KeyError' });
    const budget = (await (await admin('/admin/api/budget')).json()) as Record<string, any>;
    expect(budget.meters).toHaveLength(3);
    expect(budget.state).toBe('ok');
    expect((await admin('/admin/errors/zzzz')).status).toBe(404);
    expect((await admin(`/admin/installs/${'0'.repeat(32)}`)).status).toBe(404);
  });
});

describe('admin API', () => {
  it('client-config changes what the next upload is told', async () => {
    const set = await adminPost('/admin/api/client-config', { version: '0.0.25', level_max: 'anonymous', sample: 0.5, disabled: true });
    expect(set.status).toBe(200);
    const id = hexId('7');
    const body = (await (await upload(sampleBatch(id), await tokenFor(id))).json()) as Record<string, any>;
    expect(body.config.level_max).toBe('anonymous');
    expect(body.config.sample).toBe(0.5);
    expect(body.config.disabled_until).toBeGreaterThan(seconds() + DAY - 60);
    expect((await adminPost('/admin/api/client-config', { version: 'bad version!' })).status).toBe(400);
  });

  it('mutes a fingerprint and erases an install', async () => {
    const ids = await seedAndMaintain();
    expect((await adminPost('/admin/api/errors/0123456789abcdef/mute', { muted: true })).status).toBe(200);
    const row = await env.TELEMETRY_DB.prepare('SELECT muted_at, muted_by FROM error_fingerprints').first<Record<string, unknown>>();
    expect(row!.muted_by).toBe('token');
    const erased = await adminPost(`/admin/api/erase/${ids[0]}`, {});
    expect(await erased.json()).toMatchObject({ ok: true, batches: 1, daily_installs: 1, install: 1 });
    expect(await count('batches', 'install_id = ?', ids[0])).toBe(0);
    expect(await count('installs', 'install_id = ? AND erased_at IS NOT NULL AND os IS NULL', ids[0])).toBe(1);
  });

  it('exports raw batches as gzip, capped at EXPORT_MAX_DAYS', async () => {
    await seedAndMaintain();
    const today = dayOf(seconds());
    const response = await admin(`/admin/api/export?from=${today}&to=${today}`);
    expect(response.status).toBe(200);
    const lines = new TextDecoder().decode(gunzipSync(new Uint8Array(await response.arrayBuffer()))).trim().split('\n');
    expect(lines).toHaveLength(2);
    expect((await admin(`/admin/api/export?from=2026-01-01&to=2026-02-01`)).status).toBe(400);
  });

  it('rollup CSV is gzipped and capped at CSV_MAX_ROWS', async () => {
    await seedAndMaintain();
    const response = await direct('/admin/api/rollups/performance_rollups.csv.gz', { CSV_MAX_ROWS: '2' });
    expect(response.status).toBe(200);
    expect(response.headers.get('X-Truncated')).toBe('true');
    const csv = new TextDecoder().decode(gunzipSync(new Uint8Array(await response.arrayBuffer()))).trim().split('\n');
    expect(csv).toHaveLength(3);
    expect(csv[0]).toContain('metric');
    expect((await admin('/admin/api/rollups/batches.csv.gz')).status).toBe(404);
  });

  it('lists, downloads and deletes backups; delete clears verified_at', async () => {
    await seedAndMaintain();
    const key = dailyKey(dayOf(seconds()));
    const list = (await (await admin('/admin/api/backups')).json()) as { archives: Record<string, unknown>[] };
    expect(list.archives.map((a) => a.key)).toContain(key);
    const download = await admin(`/admin/api/backups/${key}`);
    expect(download.status).toBe(200);
    expect(gunzipSync(new Uint8Array(await download.arrayBuffer())).byteLength).toBeGreaterThan(0);
    expect((await admin('/admin/api/backups/archives/../secrets')).status).toBe(400);

    const deleted = await admin(`/admin/api/backups/${key}`, { method: 'DELETE' });
    expect(deleted.status).toBe(200);
    expect(await env.ARCHIVES.head(key)).toBeNull();
    const row = await env.TELEMETRY_DB.prepare('SELECT verified_at, deleted_at FROM archives WHERE key = ?').bind(key).first<Record<string, unknown>>();
    expect(row!.verified_at).toBeNull();
    expect(row!.deleted_at).not.toBeNull();
  });

  it('runs one maintenance slice on demand and meters authenticated requests only', async () => {
    const response = await adminPost('/admin/api/maintenance/run', {});
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ state: 'ok' });
    expect((await adminPost('/admin/api/maintenance/run', { step: 'drop_tables' })).status).toBe(400);
    const before = await env.TELEMETRY_DB.prepare('SELECT admin_requests FROM budget_daily WHERE day = ?').bind(dayOf(seconds())).first<{ admin_requests: number }>();
    await admin('/admin/usage', {}, 'wrong');
    const after = await env.TELEMETRY_DB.prepare('SELECT admin_requests FROM budget_daily WHERE day = ?').bind(dayOf(seconds())).first<{ admin_requests: number }>();
    expect(after!.admin_requests).toBe(before!.admin_requests);
  });
});

describe('schema.sql', () => {
  it('has no secondary indexes and every table is WITHOUT ROWID', async () => {
    const indexes = await env.TELEMETRY_DB.prepare(`SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL`).all();
    expect(indexes.results).toHaveLength(0);
    const tables = (
      await env.TELEMETRY_DB.prepare(`SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_%' ESCAPE '\\' AND name NOT LIKE 'sqlite%'`).all<{ name: string; sql: string }>()
    ).results;
    expect(tables.length).toBeGreaterThanOrEqual(20);
    for (const table of tables) expect(table.sql, table.name).toMatch(/WITHOUT ROWID\s*$/);
  });
});
