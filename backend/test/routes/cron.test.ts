import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { addDays, dayOf } from '../../src/env';
import { dailyKey, type Part } from '../../src/telemetry/archive';
import { tick } from '../../src/telemetry/cron';
import worker from '../../src/index';
import { budgetRow, count, hexId, sampleBatch, seconds, setBudget, tokenFor, upload } from './helpers';

const DAY = 86400;

async function seed(installs: number) {
  for (let i = 0; i < installs; i += 1) {
    const id = hexId(`${i}d`);
    expect((await upload(sampleBatch(id), await tokenFor(id))).status).toBe(202);
  }
}

function at(day: string, hour = 0) {
  return Date.parse(`${day}T${String(hour).padStart(2, '0')}:05:00Z`) / 1000;
}

describe('the cron tick', () => {
  it('enqueues D-1 on the first tick of the day, in step order, and records itself', async () => {
    const tomorrow = addDays(dayOf(seconds()), 1);
    const result = await tick(env, at(tomorrow));
    expect(result.enqueued).toBe(true);
    const steps = (
      await env.TELEMETRY_DB.prepare('SELECT step FROM maintenance_log WHERE day = ? ORDER BY step_order').bind(addDays(tomorrow, -1)).all<{ step: string }>()
    ).results.map((r) => r.step);
    expect(steps.slice(0, 7)).toEqual(['fold', 'export', 'verify', 'purge_batches', 'purge_rollups', 'archive_sweep', 'finalize']);
    expect((await tick(env, at(tomorrow))).enqueued).toBe(false);
    expect((await budgetRow(tomorrow))!.cron_ticks).toBe(2);
  });

  it('adds r2_reconcile on Sundays and the monthly steps on the 1st', async () => {
    let sunday = dayOf(seconds());
    while (new Date(`${sunday}T00:00:00Z`).getUTCDay() !== 0) sunday = addDays(sunday, 1);
    await tick(env, at(sunday));
    expect(await count('maintenance_log', `day = ? AND step = 'r2_reconcile'`, addDays(sunday, -1))).toBe(1);

    let first = addDays(dayOf(seconds()), 1);
    while (!first.endsWith('-01')) first = addDays(first, 1);
    await tick(env, at(first));
    expect(await count('maintenance_log', `day = ? AND step IN ('monthly_export', 'monthly_verify')`, addDays(first, -1))).toBe(2);
  });

  it('self-heals a missed midnight: any later tick enqueues D-1', async () => {
    const tomorrow = addDays(dayOf(seconds()), 1);
    const result = await tick(env, at(tomorrow, 14));
    expect(result.enqueued).toBe(true);
    expect(result.step).toBe('fold');
  });

  it('runs exactly one slice of one step per tick', async () => {
    const today = dayOf(seconds());
    await seed(3);
    const e = { ...env, FOLD_SLICE_BATCHES: '1' };
    const now = seconds() + DAY;
    const seen: string[] = [];
    for (let i = 0; i < 6; i += 1) {
      const result = await tick(e, now);
      seen.push(`${result.step}:${result.outcome}`);
      if (result.outcome === 'done') break;
    }
    expect(seen.slice(0, -1).every((s) => s === 'fold:running')).toBe(true);
    expect(seen.at(-1)).toBe('fold:done');
    expect(seen.length).toBeGreaterThanOrEqual(3);
    // The export has not started: nothing ran alongside the fold.
    expect(await env.ARCHIVES.head(dailyKey(today))).toBeNull();
  });

  it('resumes a running step from its committed cursor after a failed slice', async () => {
    const today = dayOf(seconds());
    await seed(3);
    const e = { ...env, EXPORT_SEGMENT_BYTES: '1', EXPORT_PAGE_ROWS: '1' };
    const now = seconds() + DAY;
    await tick(e, now); // fold
    expect(await tick(e, now)).toMatchObject({ step: 'export', outcome: 'running' });
    const cursor = (await env.TELEMETRY_DB.prepare(`SELECT cursor FROM maintenance_log WHERE day = ? AND step = 'export'`).bind(today).first<{ cursor: string }>())!.cursor;
    expect(JSON.parse(cursor).part).toBe(1);

    // A tick whose R2 put throws: the slice does not commit, the cursor stays.
    const bucket = new Proxy(env.ARCHIVES, {
      get(target, prop) {
        if (prop === 'put') return () => Promise.reject(new Error('r2 down'));
        const value = Reflect.get(target, prop);
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
    const broken = { ...e, ARCHIVES: bucket };
    const failed = await tick(broken, now);
    expect(failed.error).toContain('r2 down');
    const after = await env.TELEMETRY_DB.prepare(`SELECT state, cursor FROM maintenance_log WHERE day = ? AND step = 'export'`).bind(today).first<{ state: string; cursor: string }>();
    expect(after).toMatchObject({ state: 'running', cursor });

    // Stalled rows are simply resumed.
    for (let i = 0; i < 5; i += 1) if ((await tick(e, now)).outcome === 'done') break;
    const parts = JSON.parse((await env.TELEMETRY_DB.prepare('SELECT parts_json FROM archives WHERE key = ?').bind(dailyKey(today)).first<{ parts_json: string }>())!.parts_json) as Part[];
    expect(parts.map((p) => p.records)).toEqual([1, 1, 1]);
  });

  it('at stop, reads its budget row and returns without writing anything', async () => {
    const tomorrow = addDays(dayOf(seconds()), 1);
    await setBudget(tomorrow, 40000);
    const result = await tick(env, at(tomorrow));
    expect(result).toMatchObject({ state: 'stop', step: null, enqueued: false });
    expect(await count('maintenance_log')).toBe(0);
    expect((await budgetRow(tomorrow))!.cron_ticks).toBe(0);
  });

  it('at critical, defers the fold but still exports', async () => {
    await seed(1);
    const tomorrow = addDays(dayOf(seconds()), 1);
    await setBudget(tomorrow, 40000 * 0.96);
    expect(await tick(env, at(tomorrow))).toMatchObject({ state: 'critical', step: 'export' });
    // In reduce the fold runs again.
    await setBudget(tomorrow, 40000 * 0.86);
    expect(await tick(env, at(tomorrow))).toMatchObject({ state: 'reduce', step: 'fold' });
  });

  it('the scheduled handler runs a tick', async () => {
    const waits: Promise<unknown>[] = [];
    await worker.scheduled!(
      { scheduledTime: at(addDays(dayOf(seconds()), 1)) * 1000, cron: '*/15 * * * *', noRetry() {} } as ScheduledController,
      env,
      { waitUntil: (p: Promise<unknown>) => waits.push(p), passThroughOnException() {} } as unknown as ExecutionContext,
    );
    await Promise.all(waits);
    expect(await count('maintenance_log', `step = 'fold'`)).toBe(1);
  });
});
