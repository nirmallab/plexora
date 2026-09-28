/**
 * Retention: purging batches and rollups, sweeping archives, reconciling R2.
 *
 * The ordering guarantee lives here: purge_batches deletes a day only when its
 * archive is verified and its fold is done. The single exception is R2 at
 * HARD, where a day past BATCH_RETENTION_HARD_DAYS without an archive is
 * purged anyway, and a `failed` maintenance_log row says so, so it is never
 * silent. Every delete is bounded (PURGE_SLICE_ROWS / SWEEP_PAGE_KEYS) and
 * the step stays `running` until a slice finds nothing left.
 */
import { addDays, knob } from '../env';
import { type ArchiveRow, dailyKey, type Part, r2Bytes } from './archive';
import type { StepContext, StepOutcome } from './cron';

/** Tables with a day-first primary key, the column list of that key, and their retention knob. */
const DAY_TABLES: { table: string; pk: string[]; keep: Parameters<typeof knob>[1] }[] = [
  { table: 'error_daily', pk: ['day', 'version', 'fp'], keep: 'ERROR_DAILY_RETENTION_DAYS' },
  { table: 'daily_usage', pk: ['day', 'version', 'launch_mode', 'deployment', 'os'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'version_usage', pk: ['day', 'version'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'plugin_usage', pk: ['day', 'version', 'plugin'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'feature_usage', pk: ['day', 'version', 'plugin', 'feature'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'function_usage', pk: ['day', 'version', 'source', 'fn'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'capability_usage', pk: ['day', 'version', 'capability', 'owner'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'capability_transitions', pk: ['day', 'version', 'from_cap', 'to_cap'], keep: 'ROLLUP_RETENTION_DAYS' },
  {
    table: 'modality_usage',
    pk: ['day', 'version', 'modality', 'image_kind', 'run_format', 'table_kind'],
    keep: 'ROLLUP_RETENTION_DAYS',
  },
  { table: 'data_scale_usage', pk: ['day', 'version', 'dim', 'band'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'performance_rollups', pk: ['day', 'version', 'metric', 'dims_key'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'license_summary', pk: ['day', 'version', 'tier'], keep: 'ROLLUP_RETENTION_DAYS' },
  { table: 'daily_installs', pk: ['day', 'install_id'], keep: 'DAILY_INSTALLS_RETENTION_DAYS' },
  { table: 'budget_daily', pk: ['day'], keep: 'MAINTENANCE_LOG_RETENTION_DAYS' },
  { table: 'maintenance_log', pk: ['day', 'step'], keep: 'MAINTENANCE_LOG_RETENTION_DAYS' },
];

/** A bounded delete over a day-first primary key (no rowid on these tables). */
function boundedDelete(ctx: StepContext, table: string, pk: string[], cutoff: string, limit: number) {
  const cols = pk.join(', ');
  return ctx.meter
    .prepare(`DELETE FROM ${table} WHERE (${cols}) IN (SELECT ${cols} FROM ${table} WHERE day < ?1 LIMIT ?2)`)
    .bind(cutoff, limit);
}

function deleteDayRows(ctx: StepContext, day: string, limit: number) {
  return ctx.meter
    .prepare(
      `DELETE FROM batches WHERE (day, install_id, id) IN
         (SELECT day, install_id, id FROM batches WHERE day = ?1 LIMIT ?2)`,
    )
    .bind(day, limit);
}

/** A never-silent note, keyed by the affected day; step_order ≥ 1000 so the scheduler ignores it. */
function logFailure(ctx: StepContext, day: string, step: string, error: string) {
  return ctx.meter
    .prepare(
      `INSERT INTO maintenance_log (day, step, step_order, state, started, finished, error)
       VALUES (?1, ?2, 1000, 'failed', ?3, ?3, ?4)
       ON CONFLICT(day, step) DO UPDATE SET finished = excluded.finished, error = excluded.error`,
    )
    .bind(day, step, ctx.now, error);
}

/**
 * purge_batches: oldest day first, only days older than BATCH_RETENTION_DAYS
 * with a verified archive (and a finished fold).
 */
export async function purgeBatchesSlice(ctx: StepContext): Promise<StepOutcome> {
  const cutoff = addDays(ctx.today, -knob(ctx.env, 'BATCH_RETENTION_DAYS'));
  const hardCutoff = addDays(ctx.today, -knob(ctx.env, 'BATCH_RETENTION_HARD_DAYS'));
  let budget = knob(ctx.env, 'PURGE_SLICE_ROWS');
  let r2AtHard: boolean | null = null;
  const statements: D1PreparedStatement[] = [];

  let candidate = (
    await ctx.meter.first<{ day: string }>(ctx.meter.prepare('SELECT day FROM batches ORDER BY day LIMIT 1'))
  )?.day;
  // Point lookups only; bounded so a long run of blocked days cannot eat the tick.
  for (let checked = 0; candidate && candidate < cutoff && checked < 20 && budget > 0; checked += 1) {
    const [archive, fold] = await ctx.meter.batch([
      ctx.meter
        .prepare('SELECT verified_at FROM archives WHERE key = ?1 AND deleted_at IS NULL')
        .bind(dailyKey(candidate)),
      ctx.meter.prepare(`SELECT state FROM maintenance_log WHERE day = ?1 AND step = 'fold'`).bind(candidate),
    ]);
    const verified = Boolean((archive?.results[0] as { verified_at?: number } | undefined)?.verified_at);
    const folded = (fold?.results[0] as { state?: string } | undefined)?.state === 'done';

    let purge = verified && folded;
    if (!purge && candidate < hardCutoff) {
      r2AtHard ??= (await r2Bytes(ctx)) >= knob(ctx.env, 'R2_HARD_BYTES');
      if (r2AtHard) {
        purge = true;
        statements.push(logFailure(ctx, candidate, 'purge_unverified', 'purged without a verified archive: R2 at HARD'));
      } else {
        statements.push(logFailure(ctx, candidate, 'purge_blocked', 'past hard retention without a verified archive; batches kept'));
      }
    }
    if (purge) {
      const [result] = await ctx.meter.batch([deleteDayRows(ctx, candidate, budget), ...statements.splice(0)]);
      const deleted = result?.meta.changes ?? 0;
      budget -= deleted;
      if (budget <= 0) return { state: 'running', statements };
    }
    candidate = (
      await ctx.meter.first<{ day: string }>(
        ctx.meter.prepare('SELECT day FROM batches WHERE day > ?1 ORDER BY day LIMIT 1').bind(candidate),
      )
    )?.day;
  }
  return { state: 'done', statements };
}

/** purge_rollups: every day-first table past its window, plus quota rows and silent installs. */
export async function purgeRollupsSlice(ctx: StepContext): Promise<StepOutcome> {
  const limit = knob(ctx.env, 'PURGE_SLICE_ROWS');
  const statements = DAY_TABLES.map(({ table, pk, keep }) =>
    boundedDelete(ctx, table, pk, addDays(ctx.today, -knob(ctx.env, keep)), limit),
  );
  statements.push(
    ctx.meter
      .prepare('DELETE FROM register_quota WHERE day < ?1')
      .bind(addDays(ctx.today, -knob(ctx.env, 'REGISTER_QUOTA_RETENTION_DAYS'))),
    ctx.meter
      .prepare(
        `UPDATE installs SET erased_at = ?1, python = NULL, os = NULL, arch = NULL, launch_mode = NULL,
           deployment = NULL, scheduler = NULL, install_kind = NULL, mode = NULL, country = NULL,
           machine_id_hash = NULL, license_id = NULL, license_tier = NULL
         WHERE install_id IN (SELECT install_id FROM installs WHERE erased_at IS NULL AND last_seen < ?2 LIMIT ?3)`,
      )
      .bind(ctx.now, ctx.now - knob(ctx.env, 'INSTALL_INACTIVE_ERASE_DAYS') * 86400, limit),
  );
  const results = await ctx.meter.batch(statements);
  const more = results.some((result, i) => i < DAY_TABLES.length && (result.meta.changes ?? 0) >= limit);
  return { state: more ? 'running' : 'done', statements: [] };
}

/**
 * archive_sweep: expired archives first (DeleteObject is free), then the byte
 * caps: ≥ TARGET deletes the oldest dailies past ARCHIVE_MIN_DAYS; ≥ AGGRESSIVE
 * the oldest dailies regardless of age, then monthlies. A deleted archive's
 * verified_at is cleared, so purge waits for a fresh export of that day.
 */
export async function archiveSweepSlice(ctx: StepContext): Promise<StepOutcome> {
  const rows = await ctx.meter.all<ArchiveRow>(
    ctx.meter.prepare('SELECT * FROM archives WHERE deleted_at IS NULL ORDER BY created'),
  );
  let keys = knob(ctx.env, 'SWEEP_PAGE_KEYS');
  const doomed: ArchiveRow[] = [];
  let total = rows.reduce((sum, row) => sum + row.bytes, 0);
  const take = (row: ArchiveRow) => {
    if (doomed.includes(row)) return;
    doomed.push(row);
    total -= row.bytes;
  };

  for (const row of rows) if (row.expires !== null && row.expires <= ctx.now) take(row);
  const target = knob(ctx.env, 'R2_TARGET_BYTES');
  const aggressive = knob(ctx.env, 'R2_AGGRESSIVE_BYTES');
  const minAge = ctx.now - knob(ctx.env, 'ARCHIVE_MIN_DAYS') * 86400;
  const dailies = rows.filter((row) => row.kind !== 'monthly');
  if (total >= aggressive) {
    for (const row of dailies) if (total >= aggressive) take(row);
    for (const row of rows.filter((r) => r.kind === 'monthly' && r.created < ctx.now - 365 * 86400)) {
      if (total >= aggressive) take(row);
    }
  }
  if (total >= target) {
    for (const row of dailies) if (total >= target && row.created < minAge) take(row);
  }

  const statements: D1PreparedStatement[] = [];
  let more = false;
  for (const row of doomed) {
    const parts = JSON.parse(row.parts_json) as Part[];
    if (parts.length > keys) {
      more = true;
      break;
    }
    await ctx.env.ARCHIVES.delete(parts.map((part) => part.key));
    keys -= parts.length;
    statements.push(
      ctx.meter.prepare('UPDATE archives SET deleted_at = ?2, verified_at = NULL WHERE key = ?1').bind(row.key, ctx.now),
    );
  }
  return { state: more ? 'running' : 'done', statements };
}

/**
 * r2_reconcile (weekly): list the bucket a page per tick; objects no archives
 * row knows become `orphan` rows (so the sweep expires them); on the last
 * page, archives whose parts are gone are marked deleted, not verified.
 */
export async function reconcileSlice(ctx: StepContext): Promise<StepOutcome> {
  const rows = await ctx.meter.all<ArchiveRow>(ctx.meter.prepare('SELECT * FROM archives WHERE deleted_at IS NULL'));
  const known = new Map<string, ArchiveRow>();
  for (const row of rows) for (const part of JSON.parse(row.parts_json) as Part[]) known.set(part.key, row);

  const page = await ctx.env.ARCHIVES.list({
    prefix: 'archives/',
    cursor: ctx.row.cursor ?? undefined,
    limit: knob(ctx.env, 'SWEEP_PAGE_KEYS'),
  });
  ctx.meter.r2Puts += 1; // ListObjects is Class A
  const statements: D1PreparedStatement[] = [];
  for (const object of page.objects) {
    if (known.has(object.key)) continue;
    const part: Part = { key: object.key, bytes: object.size, sha256: object.customMetadata?.sha256 ?? '', records: 0 };
    statements.push(
      ctx.meter
        .prepare(
          `INSERT INTO archives (key, kind, period, segments, parts_json, bytes, records, created, expires)
           VALUES (?1, 'orphan', '', 1, ?2, ?3, 0, ?4, ?5) ON CONFLICT(key) DO NOTHING`,
        )
        .bind(object.key, JSON.stringify([part]), object.size, ctx.now, ctx.now + knob(ctx.env, 'ARCHIVE_RETENTION_DAILY_DAYS') * 86400),
    );
  }
  if (page.truncated) return { state: 'running', cursor: page.cursor, statements };

  for (const row of rows) {
    if (row.kind === 'orphan') continue;
    for (const part of JSON.parse(row.parts_json) as Part[]) {
      ctx.meter.r2Gets += 1;
      if (await ctx.env.ARCHIVES.head(part.key)) continue;
      statements.push(
        ctx.meter
          .prepare('UPDATE archives SET deleted_at = ?2, verified_at = NULL WHERE key = ?1')
          .bind(row.key, ctx.now),
      );
      break;
    }
  }
  return { state: 'done', cursor: null, statements };
}
