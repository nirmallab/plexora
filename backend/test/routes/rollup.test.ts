import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { addDays, dayOf, type Env } from '../../src/env';
import { tick } from '../../src/telemetry/cron';
import { binsOf, percentile } from '../../src/telemetry/hist';
import { ROLLUP_TABLES } from '../../src/telemetry/rollup';
import { hexId, sampleBatch, seconds, tokenFor, upload } from './helpers';

const DAY = 86400;

async function seed(installs: number): Promise<string[]> {
  const ids: string[] = [];
  for (let i = 0; i < installs; i += 1) {
    const id = hexId(`${i}a`);
    ids.push(id);
    const response = await upload(sampleBatch(id), await tokenFor(id));
    expect(response.status).toBe(202);
  }
  return ids;
}

/** Ticks until the fold step for `day` is done. */
async function foldToDone(e: Env, day: string, now: number, maxTicks = 50) {
  for (let i = 0; i < maxTicks; i += 1) {
    const result = await tick(e, now);
    const row = await env.TELEMETRY_DB.prepare(`SELECT state FROM maintenance_log WHERE day = ? AND step = 'fold'`)
      .bind(day)
      .first<{ state: string }>();
    if (row?.state === 'done') return i + 1;
    expect(result.error).toBeUndefined();
  }
  throw new Error('fold did not finish');
}

async function snapshot(): Promise<Record<string, unknown[]>> {
  const out: Record<string, unknown[]> = {};
  for (const table of [...ROLLUP_TABLES, 'error_fingerprints']) {
    out[table] = (await env.TELEMETRY_DB.prepare(`SELECT * FROM ${table} ORDER BY 1, 2, 3, 4`).all()).results;
  }
  return out;
}

async function one(sql: string, ...binds: unknown[]) {
  return env.TELEMETRY_DB.prepare(sql).bind(...binds).first<Record<string, any>>();
}

describe('the fold', () => {
  it('fills every rollup table from the fixture, per the vectors mapping', async () => {
    const today = dayOf(seconds());
    await seed(2);
    await foldToDone(env, today, seconds() + DAY);

    expect(await one('SELECT * FROM daily_usage WHERE day = ?', today)).toMatchObject({ installs: 2, sessions: 2, batches: 2, errors: 24 });
    expect(await one('SELECT * FROM version_usage WHERE day = ?', today)).toMatchObject({ version: '0.0.25', installs: 2, new_installs: 2 });
    expect(await one('SELECT * FROM daily_installs WHERE day = ? LIMIT 1', today)).toMatchObject({ sessions: 1, batches: 1, errors: 12 });
    expect(await one(`SELECT * FROM plugin_usage WHERE day = ? AND plugin = 'gating'`, today)).toMatchObject({
      opens: 24, closes: 24, folds: 24, activates: 24, load_failed: 24, installs: 2, b0: 6, b1: 10, b8: 2,
    });
    expect(await one(`SELECT * FROM feature_usage WHERE day = ?`, today)).toMatchObject({ plugin: 'gating', feature: 'gate.brush', n: 24, installs: 2 });
    expect(await one(`SELECT * FROM function_usage WHERE day = ? AND source = 'python'`, today)).toMatchObject({
      fn: 'import_sample', n: 24, err: 24, installs: 2, b0: 6,
    });
    // capability.summary n rows are folded in as source=agent.
    expect(await one(`SELECT * FROM function_usage WHERE day = ? AND source = 'agent'`, today)).toMatchObject({
      fn: 'gating.auto', n: 24, err: 24, b0: 6,
    });
    expect(await one(`SELECT * FROM capability_usage WHERE day = ?`, today)).toMatchObject({
      capability: 'gating.auto', owner: 'gating', ok: 0, err: 24, refused: 0, nested: 0, installs: 2, b0: 6,
    });
    expect(await one(`SELECT * FROM capability_transitions WHERE day = ?`, today)).toMatchObject({ from_cap: 'gating.auto', to_cap: 'gating.auto', n: 24 });
    expect(await one(`SELECT * FROM modality_usage WHERE day = ?`, today)).toMatchObject({
      modality: 'he', image_kind: 'brightfield', run_format: 'spatialdata', table_kind: 'parquet', n: 2, installs: 2, distributed: 2,
    });
    const scale = (await env.TELEMETRY_DB.prepare('SELECT dim, band, n FROM data_scale_usage WHERE day = ? ORDER BY dim').bind(today).all()).results;
    expect(scale).toHaveLength(11);
    expect(scale.every((row) => row.band === '1' && row.n === 2)).toBe(true);

    expect(await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'server.summary.route_ms'`, today)).toMatchObject({
      dims_key: 'route=generate_png', n: 24, ok: 24, err: 0, sum: 2 * 9876.5, max: 6012, b0: 6,
    });
    expect(await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'render.summary.tile_ms'`, today)).toMatchObject({
      dims_key: 'path=proxy;label_renderer=cpu;browser=firefox;gpu=amd', n: 24,
    });
    expect(await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'capability.summary.job_ms'`, today)).toMatchObject({
      dims_key: 'name=gating.auto;status=failed', err: 24, ok: 0,
    });
    expect(await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'node.summary.tile_total_ms'`, today)).toMatchObject({ dims_key: '', n: 24 });
    expect(await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'tool.summary.load_ms'`, today)).toMatchObject({ dims_key: 'tool=gating' });
    // Record band props: project.load outcome 'missing' is a failure; 16-50 is bin 1.
    expect(await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'project.load.total_ms'`, today)).toMatchObject({
      dims_key: 'modality=he', n: 2, ok: 0, err: 2, b1: 2, b0: 0,
    });
    expect(await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'remote.connect.connect_ms'`, today)).toMatchObject({
      dims_key: 'kind=server', n: 2, err: 2,
    });

    expect(await one(`SELECT * FROM error_daily WHERE day = ?`, today)).toMatchObject({ fp: '0123456789abcdef', n: 24, installs: 2 });
    expect(await one(`SELECT * FROM error_fingerprints`)).toMatchObject({
      fp: '0123456789abcdef', side: 'browser', exception: 'KeyError', component: 'plugin', first_seen: today, first_version: '0.0.25',
    });
  });

  it('is idempotent: a re-fold produces identical rollups and counts days_active once', async () => {
    const today = dayOf(seconds());
    const ids = await seed(2);
    await foldToDone(env, today, seconds() + DAY);
    const first = await snapshot();
    expect((await one('SELECT days_active FROM installs WHERE install_id = ?', ids[0]))!.days_active).toBe(1);

    await env.TELEMETRY_DB.prepare(`UPDATE maintenance_log SET state = 'pending', cursor = NULL WHERE day = ? AND step = 'fold'`).bind(today).run();
    await foldToDone(env, today, seconds() + DAY);
    expect(await snapshot()).toEqual(first);
    expect((await one('SELECT days_active FROM installs WHERE install_id = ?', ids[0]))!.days_active).toBe(1);
  });

  it('slicing by one batch gives the same result as one slice', async () => {
    const today = dayOf(seconds());
    await seed(3);
    await foldToDone(env, today, seconds() + DAY);
    const whole = await snapshot();

    await env.TELEMETRY_DB.prepare(`UPDATE maintenance_log SET state = 'pending', cursor = NULL WHERE day = ? AND step = 'fold'`).bind(today).run();
    const ticks = await foldToDone({ ...env, FOLD_SLICE_BATCHES: '1' }, today, seconds() + DAY);
    expect(ticks).toBeGreaterThanOrEqual(3);
    expect(await snapshot()).toEqual(whole);
  });

  it('reopens an earlier day only when a batch arrived after its fold finished', async () => {
    const today = dayOf(seconds());
    const finished = async () => (await one(`SELECT finished FROM maintenance_log WHERE day = ? AND step = 'fold'`, today))!.finished as number;
    await seed(1);
    const t1 = seconds() + DAY;
    await foldToDone(env, today, t1);
    expect(await finished()).toBe(t1);

    // Two days on: today is D-2, its batches all predate the fold, so it stays done.
    await tick(env, seconds() + 2 * DAY);
    expect(await finished()).toBe(t1);

    // Simulate an early fold (say, a manual run during the day): a batch was
    // received after the fold finished. Three days on, today is D-3 and is re-folded.
    await env.TELEMETRY_DB.prepare(`UPDATE maintenance_log SET finished = ?2 WHERE day = ?1 AND step = 'fold'`).bind(today, seconds() - 60).run();
    const t3 = seconds() + 3 * DAY;
    const result = await tick(env, t3);
    expect(result).toMatchObject({ step: 'fold', day: today, outcome: 'done' });
    expect(await finished()).toBe(t3);
  });

  it('percentiles recombine from summed bins', async () => {
    const today = dayOf(seconds());
    await seed(2);
    await foldToDone(env, today, seconds() + DAY);
    const row = await one(`SELECT * FROM performance_rollups WHERE day = ? AND metric = 'server.summary.route_ms'`, today);
    const bins = binsOf(row!);
    expect(bins).toEqual([6, 10, 4, 2, 0, 0, 0, 0, 2]);
    // 24 samples: the median (rank 12) falls in bin 1 (16-50 ms).
    const p50 = percentile(bins, 0.5)!;
    expect(p50).toBeGreaterThan(16);
    expect(p50).toBeLessThan(50);
    expect(percentile(bins, 1, undefined, row!.max)).toBeCloseTo(6012);
  });
});
