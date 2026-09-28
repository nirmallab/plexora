import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { addDays, dayOf, type Env } from '../../src/env';
import { dailyKey } from '../../src/telemetry/archive';
import { STEP_ORDER, tick } from '../../src/telemetry/cron';
import { count, hexId, seconds } from './helpers';

const DAY = 86400;
const db = () => env.TELEMETRY_DB;

/** Runs one step now, as the admin "run step" does. */
async function runOnly(e: Env, now: number, step: string) {
  await db()
    .prepare(
      `INSERT INTO maintenance_log (day, step, step_order, state) VALUES (?1, ?2, ?3, 'pending')
       ON CONFLICT(day, step) DO UPDATE SET state = 'pending', cursor = NULL`,
    )
    .bind(addDays(dayOf(now), -1), step, STEP_ORDER[step]!)
    .run();
  return tick(e, now, step);
}

async function batchRows(day: string, n: number) {
  for (let i = 0; i < n; i += 1) {
    await db()
      .prepare(
        `INSERT INTO batches (day, install_id, id, received, events, errors, bytes, priority_max, has_session, events_json, events_gz)
         VALUES (?1, ?2, ?3, 0, 1, 0, 1, 1, 0, '[]', '')`,
      )
      .bind(day, hexId('e'), hexId('f'))
      .run();
  }
}

async function archive(day: string, options: { verified?: boolean; bytes?: number; created?: number; expires?: number } = {}) {
  const key = dailyKey(day);
  const bytes = options.bytes ?? 10;
  await env.ARCHIVES.put(key, new Uint8Array(bytes));
  await db()
    .prepare(
      `INSERT INTO archives (key, kind, period, segments, parts_json, bytes, records, created, verified_at, expires)
       VALUES (?1, 'daily', ?2, 1, ?3, ?4, 1, ?5, ?6, ?7)`,
    )
    .bind(
      key,
      day,
      JSON.stringify([{ key, bytes, sha256: '', records: 1 }]),
      bytes,
      options.created ?? seconds(),
      options.verified === false ? null : seconds(),
      options.expires ?? seconds() + 100 * DAY,
    )
    .run();
  return key;
}

async function foldDone(day: string) {
  await db()
    .prepare(`INSERT INTO maintenance_log (day, step, step_order, state) VALUES (?1, 'fold', 10, 'done')`)
    .bind(day)
    .run();
}

describe('purge_batches', () => {
  it('purges only days past retention with a verified archive and a finished fold', async () => {
    const today = dayOf(seconds());
    const verified = addDays(today, -40);
    const unverified = addDays(today, -35);
    const recent = addDays(today, -10);
    for (const day of [verified, unverified, recent]) {
      await batchRows(day, 3);
      await foldDone(day);
    }
    await archive(verified);
    await archive(unverified, { verified: false });
    await archive(recent);

    expect(await runOnly(env, seconds(), 'purge_batches')).toMatchObject({ outcome: 'done' });
    expect(await count('batches', 'day = ?', verified)).toBe(0);
    expect(await count('batches', 'day = ?', unverified)).toBe(3);
    expect(await count('batches', 'day = ?', recent)).toBe(3);
  });

  it('past the hard window, unverified days are kept and logged, unless R2 is at HARD', async () => {
    const today = dayOf(seconds());
    const old = addDays(today, -50);
    await batchRows(old, 2);
    await foldDone(old);
    await runOnly(env, seconds(), 'purge_batches');
    expect(await count('batches', 'day = ?', old)).toBe(2);
    expect(await count('maintenance_log', `day = ? AND step = 'purge_blocked' AND state = 'failed'`, old)).toBe(1);

    await archive(addDays(today, -5), { bytes: 100 });
    await runOnly({ ...env, R2_HARD_BYTES: '50' }, seconds(), 'purge_batches');
    expect(await count('batches', 'day = ?', old)).toBe(0);
    expect(await count('maintenance_log', `day = ? AND step = 'purge_unverified' AND state = 'failed'`, old)).toBe(1);
  });

  it('deletes at most PURGE_SLICE_ROWS per tick and resumes', async () => {
    const day = addDays(dayOf(seconds()), -40);
    await batchRows(day, 5);
    await foldDone(day);
    await archive(day);
    const e = { ...env, PURGE_SLICE_ROWS: '2' };
    expect(await runOnly(e, seconds(), 'purge_batches')).toMatchObject({ outcome: 'running' });
    expect(await count('batches', 'day = ?', day)).toBe(3);
    await tick(e, seconds(), 'purge_batches');
    await tick(e, seconds(), 'purge_batches');
    expect(await count('batches', 'day = ?', day)).toBe(0);
  });
});

describe('purge_rollups', () => {
  it('applies each retention window', async () => {
    const today = dayOf(seconds());
    const put = (sql: string, ...binds: unknown[]) => db().prepare(sql).bind(...binds).run();
    for (const back of [61, 59]) {
      await put(`INSERT INTO error_daily VALUES (?1, 'v', 'ffffffff', 1, 1)`, addDays(today, -back));
    }
    for (const back of [366, 364]) {
      await put(
        `INSERT INTO performance_rollups VALUES (?1, 'v', 'm', '', 1, 1, 0, NULL, NULL, 0,0,0,0,0,0,0,0,0)`,
        addDays(today, -back),
      );
    }
    for (const back of [401, 399]) {
      await put(`INSERT INTO daily_installs (day, install_id, sessions, batches, events, errors) VALUES (?1, 'i', 0, 0, 0, 0)`, addDays(today, -back));
    }
    for (const back of [8, 6]) await put(`INSERT INTO register_quota VALUES ('h', ?1, 1)`, addDays(today, -back));
    await put(
      `INSERT INTO installs (install_id, first_seen, last_seen, os, country) VALUES ('silent', 0, ?1, 'mac', 'GB'), ('active', 0, ?2, 'mac', 'GB')`,
      seconds() - 731 * DAY,
      seconds() - DAY,
    );

    expect(await runOnly(env, seconds(), 'purge_rollups')).toMatchObject({ outcome: 'done' });
    expect(await count('error_daily')).toBe(1);
    expect(await count('performance_rollups')).toBe(1);
    expect(await count('daily_installs')).toBe(1);
    expect(await count('register_quota')).toBe(1);
    const silent = await db().prepare(`SELECT * FROM installs WHERE install_id = 'silent'`).first<Record<string, unknown>>();
    expect(silent).toMatchObject({ os: null, country: null });
    expect(silent!.erased_at).not.toBeNull();
    expect(await count('installs', `install_id = 'active' AND erased_at IS NULL AND os = 'mac'`)).toBe(1);
  });
});

describe('archive_sweep and r2_reconcile', () => {
  it('deletes expired archives (object and row)', async () => {
    const key = await archive(addDays(dayOf(seconds()), -200), { expires: seconds() - 1 });
    await runOnly(env, seconds(), 'archive_sweep');
    expect(await env.ARCHIVES.head(key)).toBeNull();
    const row = await db().prepare('SELECT deleted_at, verified_at FROM archives WHERE key = ?').bind(key).first<Record<string, unknown>>();
    expect(row!.deleted_at).not.toBeNull();
    expect(row!.verified_at).toBeNull();
  });

  it('enforces the byte target oldest first, sparing dailies younger than ARCHIVE_MIN_DAYS', async () => {
    const today = dayOf(seconds());
    const oldest = await archive(addDays(today, -100), { bytes: 100, created: seconds() - 100 * DAY });
    const older = await archive(addDays(today, -90), { bytes: 100, created: seconds() - 90 * DAY });
    const young = await archive(addDays(today, -5), { bytes: 100, created: seconds() - 5 * DAY });
    await runOnly({ ...env, R2_TARGET_BYTES: '250' }, seconds(), 'archive_sweep');
    expect(await env.ARCHIVES.head(oldest)).toBeNull();
    expect(await env.ARCHIVES.head(older)).not.toBeNull();
    expect(await env.ARCHIVES.head(young)).not.toBeNull();

    // Past AGGRESSIVE even young dailies go, oldest first.
    await runOnly({ ...env, R2_TARGET_BYTES: '50', R2_AGGRESSIVE_BYTES: '150' }, seconds(), 'archive_sweep');
    expect(await env.ARCHIVES.head(older)).toBeNull();
    expect(await env.ARCHIVES.head(young)).not.toBeNull();
  });

  it('reconcile records orphans and marks archives whose objects are gone', async () => {
    const today = dayOf(seconds());
    await env.ARCHIVES.put('archives/daily/stray.jsonl.gz', new Uint8Array(7));
    const missing = await archive(addDays(today, -3));
    await env.ARCHIVES.delete(missing);
    await runOnly(env, seconds(), 'r2_reconcile');
    expect(await count('archives', `key = 'archives/daily/stray.jsonl.gz' AND kind = 'orphan' AND bytes = 7`)).toBe(1);
    const row = await db().prepare('SELECT deleted_at, verified_at FROM archives WHERE key = ?').bind(missing).first<Record<string, unknown>>();
    expect(row!.deleted_at).not.toBeNull();
    expect(row!.verified_at).toBeNull();
  });
});
