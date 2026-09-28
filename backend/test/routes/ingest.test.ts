import { env, SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { dayOf, addDays } from '../../src/env';
import { ingestStatements } from '../../src/telemetry/ingest';
import { cleanBatch } from '../../src/telemetry/schema';
import { buildLine, fromBase64, gunzip, gzip, toBase64 } from '../../src/telemetry/store';
import {
  BASE,
  count,
  eventOf,
  gzipBytes,
  hexId,
  register,
  sampleBatch,
  seconds,
  tokenFor,
  upload,
} from './helpers';

const CONFIG_KEYS = ['disabled_until', 'level_max', 'sample', 'upload_interval_s'];

describe('register', () => {
  it('mints a PLEXORAT1 token bound to the install id, with the four config keys', async () => {
    const id = hexId('a');
    const response = await register(id);
    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.schema).toBe(1);
    expect(String(body.install_token)).toMatch(/^PLEXORAT1\.[\w-]+\.[\w-]+$/);
    expect(body.expires).toBeGreaterThan(seconds() + 364 * 86400);
    expect(Object.keys(body.config as object).sort()).toEqual(CONFIG_KEYS);
    const payload = JSON.parse(atob(String(body.install_token).split('.')[1]!.replace(/-/g, '+').replace(/_/g, '/')));
    expect(payload).toMatchObject({ v: 1, kind: 'telemetry', tid: id });
    expect(await count('installs', 'install_id = ?', id)).toBe(1);

    // The token works for an upload.
    const up = await upload(sampleBatch(id), String(body.install_token));
    expect(up.status).toBe(202);
  });

  it('refuses a bad id, an unknown client key, and a body over 4 KB', async () => {
    expect((await register('nothex')).status).toBe(400);
    expect((await register(hexId(), { hostname: 'lab-mac' })).status).toBe(400);
    const big = await SELF.fetch(`${BASE}/v1/telemetry/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ schema: 1, install_id: hexId(), client: {}, pad: 'x'.repeat(5000) }),
    });
    expect(big.status).toBe(413);
  });

  it('rate-limits registrations per peppered address and day', async () => {
    await env.TELEMETRY_DB.prepare('DELETE FROM register_quota').run();
    // Pre-fill the quota row for this address to the limit.
    const first = await register(hexId(), {}, '198.51.100.9');
    expect(first.status).toBe(200);
    await env.TELEMETRY_DB.prepare('UPDATE register_quota SET n = 200').run();
    const refused = await register(hexId(), {}, '198.51.100.9');
    expect(refused.status).toBe(429);
    expect(Number(refused.headers.get('Retry-After'))).toBeGreaterThan(0);
    expect(Object.keys(((await refused.json()) as { config: object }).config).sort()).toEqual(CONFIG_KEYS);
    // Nothing identifying is stored: only a hash.
    const row = await env.TELEMETRY_DB.prepare('SELECT ip_hash FROM register_quota').first<{ ip_hash: string }>();
    expect(row!.ip_hash).not.toContain('198.51');
  });
});

describe('events: the happy path', () => {
  it('accepts the fixture, stores one row, and events_gz round-trips to the stored line', async () => {
    const id = hexId('c');
    const batch = sampleBatch(id);
    const response = await upload(batch, await tokenFor(id));
    expect(response.status).toBe(202);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ schema: 1, accepted: batch.events.length, rejected: 0, dropped: 0, duplicate: false, rotate: false });
    expect(Object.keys(body).sort()).toEqual(['accepted', 'config', 'dropped', 'duplicate', 'rejected', 'rotate', 'schema']);
    expect(Object.keys(body.config as object).sort()).toEqual(CONFIG_KEYS);
    expect(response.headers.get('Cache-Control')).toBe('no-store');
    expect(response.headers.get('Access-Control-Allow-Origin')).toBeNull();

    const row = await env.TELEMETRY_DB.prepare('SELECT * FROM batches WHERE install_id = ?').bind(id).first<Record<string, unknown>>();
    expect(row).not.toBeNull();
    expect(row!.day).toBe(dayOf(seconds()));
    expect(row!.has_session).toBe(1);
    expect(row!.errors).toBe(12);
    const bytes = fromBase64(row!.events_gz as string);
    const line = JSON.parse(new TextDecoder().decode(await gunzip(bytes)));
    expect(line.id).toBe(batch.batch_id);
    expect(line.install_id).toBe(id);
    expect(JSON.stringify(line.events)).toBe(row!.events_json);
    // The session summary created the installs row.
    const install = await env.TELEMETRY_DB.prepare('SELECT * FROM installs WHERE install_id = ?').bind(id).first<Record<string, unknown>>();
    expect(install).toMatchObject({ version: '0.0.25', os: 'windows', deployment: 'hpc', launch_mode: 'desktop' });
  });

  it('writes at most 3 D1 rows per upload (2 without a session summary), no index rows', async () => {
    for (const withSession of [true, false]) {
      const id = hexId('d');
      const raw = sampleBatch(id);
      if (!withSession) raw.events = raw.events.filter((e) => e.type !== 'session.summary');
      const cleaned = cleanBatch(raw);
      if (!cleaned.ok) throw new Error(cleaned.error);
      const now = seconds();
      const stored = buildLine(cleaned.batch, dayOf(now), now, null);
      const results = await env.TELEMETRY_DB.batch(
        ingestStatements(env, cleaned.batch, stored, toBase64(await gzip(stored.line)), 100),
      );
      const written = results.reduce((sum, r) => sum + (r.meta.rows_written ?? 0), 0);
      expect(written).toBe(withSession ? 3 : 2);
      // The constant ingest records for itself equals the real meta sum.
      const budget = await env.TELEMETRY_DB.prepare('SELECT d1_rows_written FROM budget_daily WHERE day = ?')
        .bind(dayOf(now))
        .first<{ d1_rows_written: number }>();
      expect(budget).not.toBeNull();
      await env.TELEMETRY_DB.prepare('DELETE FROM budget_daily').run();
      expect(budget!.d1_rows_written).toBe(written);
    }
  });

  it('a duplicate batch id is 202 duplicate and stores nothing new', async () => {
    const id = hexId('e');
    const batch = sampleBatch(id);
    const token = await tokenFor(id);
    expect((await upload(batch, token)).status).toBe(202);
    const again = await upload(batch, token);
    expect(again.status).toBe(202);
    expect(await again.json()).toMatchObject({ accepted: 0, duplicate: true });
    expect(await count('batches', 'install_id = ?', id)).toBe(1);
  });

  it('dedupes across midnight (a batch id stored yesterday)', async () => {
    const id = hexId('f');
    const batch = sampleBatch(id);
    const yesterday = addDays(dayOf(seconds()), -1);
    await env.TELEMETRY_DB.prepare(
      `INSERT INTO batches (day, install_id, id, received, events, errors, bytes, priority_max, has_session, events_json, events_gz)
       VALUES (?1, ?2, ?3, ?4, 1, 0, 1, 1, 0, '[]', '')`,
    )
      .bind(yesterday, id, batch.batch_id, seconds() - 3600)
      .run();
    const response = await upload(batch, await tokenFor(id));
    expect(await response.json()).toMatchObject({ duplicate: true });
    expect(await count('batches', 'install_id = ?', id)).toBe(1);
  });

  it('the 13th upload in an hour is 429 with Retry-After 3600', async () => {
    const id = hexId('g');
    const token = await tokenFor(id);
    for (let i = 0; i < 12; i += 1) {
      const batch = sampleBatch(id);
      batch.events = batch.events.filter((e) => e.type === 'tool.summary');
      expect((await upload(batch, token)).status).toBe(202);
    }
    const limited = await upload(sampleBatch(id), token);
    expect(limited.status).toBe(429);
    expect(limited.headers.get('Retry-After')).toBe('3600');
    expect(Object.keys(((await limited.json()) as { config: object }).config).sort()).toEqual(CONFIG_KEYS);
    expect(await count('batches', 'install_id = ?', id)).toBe(12);
  });
});

describe('events: refusals, cheapest first', () => {
  it('401 without or with a tampered token; 403 when the batch names another install', async () => {
    const id = hexId('h');
    expect((await upload(sampleBatch(id), null)).status).toBe(401);
    const token = await tokenFor(id);
    const [prefix, body, sig] = token.split('.');
    const forged = btoa(JSON.stringify({ v: 1, kind: 'telemetry', tid: hexId('z'), iat: 0, exp: 9e9 }))
      .replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
    expect((await upload(sampleBatch(id), `${prefix}.${forged}.${sig}`)).status).toBe(401);
    expect((await upload(sampleBatch(id), `${prefix}.${body}.${sig}x`)).status).toBe(401);
    expect((await upload(sampleBatch(hexId('i')), token)).status).toBe(403);
  });

  it('415 on a wrong content type, 413 on a declared length over 64 KB', async () => {
    const id = hexId('j');
    const token = await tokenFor(id);
    expect((await upload(sampleBatch(id), token, { headers: { 'Content-Type': 'text/plain' } })).status).toBe(415);
    const response = await upload(sampleBatch(id), token, { raw: new Uint8Array(70000), headers: { 'Content-Length': '70000' } });
    expect(response.status).toBe(413);
  });

  it('413 on a gzip bomb, cancelled at the decompressed cap', async () => {
    const id = hexId('k');
    const bomb = await gzipBytes(new Uint8Array(4 * 1024 * 1024).fill(32));
    expect(bomb.byteLength).toBeLessThan(65536);
    const response = await upload(null, await tokenFor(id), { raw: bomb });
    expect(response.status).toBe(413);
  });

  it('400 on broken gzip, invalid JSON, a wrong schema or an unknown client key', async () => {
    const id = hexId('l');
    const token = await tokenFor(id);
    expect((await upload(null, token, { raw: new Uint8Array([1, 2, 3, 4]) })).status).toBe(400);
    expect((await upload(null, token, { raw: await gzipBytes('{"schema": 1,') })).status).toBe(400);
    expect((await upload({ ...sampleBatch(id), schema: 2 }, token)).status).toBe(400);
    const batch = sampleBatch(id);
    batch.client.hostname = 'lab-mac';
    expect((await upload(batch, token)).status).toBe(400);
  });

  it('accepts an identity-encoded body (wrangler dev)', async () => {
    const id = hexId('m');
    expect((await upload(sampleBatch(id), await tokenFor(id), { gzip: false })).status).toBe(202);
  });
});

describe('events: the allowlist', () => {
  it('rejects an event with an unknown key whole, keeps the others, never stores the key', async () => {
    const id = hexId('n');
    const batch = sampleBatch(id);
    (eventOf(batch, 'dataset.opened').props as Record<string, unknown>).path = '/Users/someone/secret.ome.tiff';
    (eventOf(batch, 'tool.summary').rows as Record<string, unknown>[])[0]!.d = { tool: 'gating', user: 'someone' };
    batch.events.push({ type: 'made.up', window: '2026-09-27T14', props: {} });
    const response = await upload(batch, await tokenFor(id));
    const body = (await response.json()) as { accepted: number; rejected: number };
    expect(body.rejected).toBe(3);
    expect(body.accepted).toBe(batch.events.length - 3);
    const row = await env.TELEMETRY_DB.prepare('SELECT events_json FROM batches WHERE install_id = ?').bind(id).first<{ events_json: string }>();
    expect(row!.events_json).not.toContain('secret');
    expect(row!.events_json).not.toContain('someone');
    expect(row!.events_json).not.toContain('"type":"dataset.opened"');
  });

  it('accepts ext:<8hex> plugin ids and rejects anything else of that shape', async () => {
    const id = hexId('o');
    const token = await tokenFor(id);
    const good = sampleBatch(id);
    good.events = [{ type: 'tool.summary', window: '2026-09-27T14', rows: [{ k: 'open', d: { tool: 'ext:1a2b3c4d' }, n: 1 }] }];
    expect(await (await upload(good, token)).json()).toMatchObject({ accepted: 1, rejected: 0 });
    for (const tool of ['ext:1A2B3C4D', 'ext:1a2b3c4', 'ext:1a2b3c4d5', 'my_plugin']) {
      const bad = sampleBatch(id);
      bad.events = [{ type: 'tool.summary', window: '2026-09-27T14', rows: [{ k: 'open', d: { tool }, n: 1 }] }];
      expect(await (await upload(bad, token)).json()).toMatchObject({ accepted: 0, rejected: 1 });
    }
  });

  it('strips diagnostics-level fields from an anonymous-mode client', async () => {
    const id = hexId('p');
    const batch = sampleBatch(id);
    batch.client.mode = 'anonymous';
    await upload(batch, await tokenFor(id));
    const row = await env.TELEMETRY_DB.prepare('SELECT events_json FROM batches WHERE install_id = ?').bind(id).first<{ events_json: string }>();
    const events = JSON.parse(row!.events_json) as Record<string, any>[];
    const opened = events.find((e) => e.type === 'dataset.opened')!;
    expect(opened.props.channels).toBeUndefined();
    expect(opened.props.channels_band).toBe('1');
    const render = events.find((e) => e.type === 'render.summary')!;
    expect(render.rows.every((r: { d: object }) => !('gpu' in r.d))).toBe(true);
    const session = events.find((e) => e.type === 'session.summary')!;
    expect(session.props.gpu_present).toBeUndefined();
  });
});
