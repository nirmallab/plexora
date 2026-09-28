import { env } from 'cloudflare:test';
import { gunzipSync } from 'node:zlib';

import { describe, expect, it } from 'vitest';

import { addDays, dayOf, type Env } from '../../src/env';
import { dailyKey, monthlyBase, partKey, type Part } from '../../src/telemetry/archive';
import { tick, type TickResult } from '../../src/telemetry/cron';
import { sha256Hex } from '../../src/telemetry/tokens';
import { count, hexId, sampleBatch, seconds, tokenFor, upload } from './helpers';

const DAY = 86400;

async function seed(installs: number, perInstall = 1): Promise<void> {
  for (let i = 0; i < installs; i += 1) {
    const id = hexId(`${i}c`);
    const token = await tokenFor(id);
    for (let j = 0; j < perInstall; j += 1) expect((await upload(sampleBatch(id), token)).status).toBe(202);
  }
}

/** Ticks until nothing is left to do for the tick's D-1; returns what each tick did. */
async function drain(e: Env, now: number, max = 60): Promise<TickResult[]> {
  const out: TickResult[] = [];
  for (let i = 0; i < max; i += 1) {
    const result = await tick(e, now);
    if (!result.step) return out;
    out.push(result);
  }
  throw new Error('maintenance did not settle');
}

async function archiveRow(key: string) {
  return env.TELEMETRY_DB.prepare('SELECT * FROM archives WHERE key = ?').bind(key).first<Record<string, any>>();
}

describe('daily export, verify, purge', () => {
  it('runs fold, export, verify, then purges, in order, and the archive holds every batch line', async () => {
    const today = dayOf(seconds());
    await seed(3);
    const steps = (await drain(env, seconds() + DAY)).map((r) => `${r.step}:${r.outcome}`);
    expect(steps).toEqual([
      'fold:done',
      'export:done',
      'verify:done',
      'purge_batches:done',
      'purge_rollups:done',
      'archive_sweep:done',
      'finalize:done',
    ]);

    const row = await archiveRow(dailyKey(today));
    expect(row).toMatchObject({ kind: 'daily', period: today, segments: 1, records: 3 });
    expect(row!.verified_at).not.toBeNull();
    const object = await env.ARCHIVES.get(dailyKey(today));
    const bytes = new Uint8Array(await object!.arrayBuffer());
    expect(object!.customMetadata!.sha256).toBe(await sha256Hex(bytes));
    // Concatenated gzip members: one per batch. node's zlib (like zcat and
    // Python's gzip) reads them as one stream; workerd's DecompressionStream
    // stops after the first member.
    const lines = new TextDecoder().decode(gunzipSync(bytes)).trim().split('\n').map((l) => JSON.parse(l));
    expect(lines).toHaveLength(3);
    expect(lines.every((l) => l.day === today && Array.isArray(l.events))).toBe(true);

    // Thirty-one days on, the verified day is purged.
    expect(await count('batches', 'day = ?', today)).toBe(3);
    await drain(env, seconds() + 31 * DAY);
    expect(await count('batches', 'day = ?', today)).toBe(0);
  });

  it('a rewritten customMetadata.sha256 fails verify, and purge then leaves the rows', async () => {
    const today = dayOf(seconds());
    await seed(2);
    const e = { ...env, EXPORT_MAX_ATTEMPTS: '1' };
    const now = seconds() + DAY;
    expect((await tick(e, now)).step).toBe('fold');
    expect((await tick(e, now)).step).toBe('export');
    const object = await env.ARCHIVES.get(dailyKey(today));
    const bytes = await object!.arrayBuffer();
    await env.ARCHIVES.put(dailyKey(today), bytes, { customMetadata: { ...object!.customMetadata, sha256: 'f'.repeat(64) } });

    const verify = await tick(e, now);
    expect(verify).toMatchObject({ step: 'verify', outcome: 'failed' });
    expect(verify.error).toContain('checksum mismatch');
    // Nothing downstream ran for that day.
    const states = (
      await env.TELEMETRY_DB.prepare('SELECT step, state FROM maintenance_log WHERE day = ? AND step_order < 1000 ORDER BY step_order')
        .bind(today)
        .all<{ step: string; state: string }>()
    ).results;
    expect(states.filter((s) => s.step.startsWith('purge') || s.step === 'finalize').every((s) => s.state === 'skipped')).toBe(true);
    expect((await archiveRow(dailyKey(today)))!.verified_at).toBeNull();

    // Past the retention window the unverified day's rows are still there.
    await drain(e, seconds() + 31 * DAY);
    expect(await count('batches', 'day = ?', today)).toBe(2);
  });

  it('a failed verify re-queues the export while attempts remain, and the re-put heals it', async () => {
    const today = dayOf(seconds());
    await seed(1);
    const now = seconds() + DAY;
    await tick(env, now); // fold
    await tick(env, now); // export
    const object = await env.ARCHIVES.get(dailyKey(today));
    await env.ARCHIVES.put(dailyKey(today), await object!.arrayBuffer(), { customMetadata: { sha256: 'bad' } });
    expect(await tick(env, now)).toMatchObject({ step: 'verify', outcome: 'pending' });
    expect(await tick(env, now)).toMatchObject({ step: 'export', outcome: 'done' });
    expect(await tick(env, now)).toMatchObject({ step: 'verify', outcome: 'done' });
    expect((await archiveRow(dailyKey(today)))!.verified_at).not.toBeNull();
  });

  it('splits a day into deterministic segments and re-exports byte-identical parts', async () => {
    const today = dayOf(seconds());
    await seed(3, 2);
    const e = { ...env, EXPORT_SEGMENT_BYTES: '2500', EXPORT_PAGE_ROWS: '2' };
    const now = seconds() + DAY;
    await tick(e, now); // fold
    let exportTicks = 0;
    for (;;) {
      const result = await tick(e, now);
      if (result.step !== 'export') break;
      exportTicks += 1;
    }
    const row = await archiveRow(dailyKey(today));
    const parts = JSON.parse(row!.parts_json) as Part[];
    expect(parts.length).toBeGreaterThan(1);
    // One tick per part, plus possibly one that finds the day already exhausted.
    expect(exportTicks - parts.length).toBeGreaterThanOrEqual(0);
    expect(exportTicks - parts.length).toBeLessThanOrEqual(1);
    expect(parts.reduce((sum, p) => sum + p.records, 0)).toBe(6);
    expect(parts[1]!.key).toBe(`${dailyKey(today).replace('.jsonl.gz', '')}.0001.jsonl.gz`);
    expect(row!.records).toBe(6);

    // Re-export from scratch: same keys, same bytes.
    await env.TELEMETRY_DB.prepare(`UPDATE maintenance_log SET state = 'pending', cursor = NULL WHERE day = ? AND step IN ('export', 'verify')`).bind(today).run();
    for (;;) {
      const result = await tick(e, now);
      if (result.step !== 'export') break;
    }
    const again = JSON.parse((await archiveRow(dailyKey(today)))!.parts_json) as Part[];
    expect(again).toEqual(parts);
  });

  it('writes the monthly rollup archive on the 1st and verifies it', async () => {
    const today = dayOf(seconds());
    await seed(2);
    await drain(env, seconds() + DAY);
    // The first day of the month after `today`.
    let first = addDays(today, 1);
    while (!first.endsWith('-01')) first = addDays(first, 1);
    const at = Date.parse(`${first}T00:05:00Z`) / 1000;
    const steps = (await drain(env, at)).map((r) => r.step);
    expect(steps).toContain('monthly_export');
    expect(steps.indexOf('monthly_verify')).toBeGreaterThan(steps.indexOf('monthly_export'));

    const key = partKey(monthlyBase(today.slice(0, 7)), 0);
    const row = await archiveRow(key);
    expect(row).toMatchObject({ kind: 'monthly', period: today.slice(0, 7) });
    expect(row!.verified_at).not.toBeNull();
    const lines = new TextDecoder()
      .decode(gunzipSync(new Uint8Array(await (await env.ARCHIVES.get(key))!.arrayBuffer())))
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    const tables = new Set(lines.map((l) => l.table));
    expect(tables.has('plugin_usage')).toBe(true);
    expect(tables.has('performance_rollups')).toBe(true);
    const month = today.slice(0, 7);
    const perf = await count('performance_rollups', 'day >= ? AND day <= ?', `${month}-01`, `${month}-31`);
    expect(lines.filter((l) => l.table === 'performance_rollups')).toHaveLength(perf);
  });
});
