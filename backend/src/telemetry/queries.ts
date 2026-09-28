/**
 * Read models for the admin pages. Each returns plain JSON; the same object
 * renders as HTML or is sent as JSON depending on `Accept`.
 *
 * Every query ranges on a day-first primary key; the few that cannot (an
 * install's history, a fingerprint's series) are bounded to a window in SQL.
 * Filters are validated by regex before they reach a bind, never interpolated.
 */
import { addDays, dayOf, type Env, knob, knobs } from '../env';
import { type Counters, budgetRatio, type Meter, stateFor } from './budget';
import { addBins, binsOf, percentile } from './hist';
import { VECTORS } from './schema';

export interface Filters {
  from: string;
  to: string;
  version: string | null;
  launch_mode: string | null;
  deployment: string | null;
}

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const TOKEN_RE = /^[0-9A-Za-z._+-]{1,48}$/;

export function parseFilters(query: Record<string, string | undefined>, now: number, defaultDays = 30): Filters {
  const today = dayOf(now);
  const to = query.to && DAY_RE.test(query.to) ? query.to : today;
  let from = query.from && DAY_RE.test(query.from) ? query.from : addDays(to, -(defaultDays - 1));
  if (from > to) from = to;
  if (from < addDays(to, -400)) from = addDays(to, -400);
  const token = (value: string | undefined) => (value && TOKEN_RE.test(value) ? value : null);
  return { from, to, version: token(query.version), launch_mode: token(query.launch_mode), deployment: token(query.deployment) };
}

/** `AND version = ?n` style clauses for the filters a table has. */
function where(filters: Filters, columns: (keyof Filters)[], start: number): { sql: string; binds: unknown[] } {
  const parts: string[] = [];
  const binds: unknown[] = [];
  for (const column of columns) {
    const value = filters[column];
    if (value === null) continue;
    binds.push(value);
    parts.push(`AND ${column} = ?${start + binds.length - 1}`);
  }
  return { sql: parts.join(' '), binds };
}

function daysBetween(from: string, to: string): string[] {
  const out: string[] = [];
  for (let day = from; day <= to; day = addDays(day, 1)) out.push(day);
  return out;
}

// ---------------------------------------------------------------------------

export async function usage(meter: Meter, f: Filters) {
  const w = where(f, ['version', 'launch_mode', 'deployment'], 3);
  const active = await meter.all<{ day: string; installs: number; sessions: number; batches: number; events: number; errors: number }>(
    meter
      .prepare(
        `SELECT day, COUNT(*) AS installs, SUM(sessions) AS sessions, SUM(batches) AS batches, SUM(events) AS events,
           SUM(errors) AS errors FROM daily_installs WHERE day >= ?1 AND day <= ?2 ${w.sql} GROUP BY day ORDER BY day`,
      )
      .bind(f.from, f.to, ...w.binds),
  );
  const distinct = async (from: string) =>
    (
      await meter.first<{ n: number }>(
        meter
          .prepare(`SELECT COUNT(DISTINCT install_id) AS n FROM daily_installs WHERE day >= ?1 AND day <= ?2 ${w.sql}`)
          .bind(from, f.to, ...w.binds),
      )
    )?.n ?? 0;
  const byDay = new Map(active.map((row) => [row.day, row]));
  const vw = where(f, ['version'], 3);
  const versions = await meter.all<{ day: string; version: string; installs: number; new_installs: number }>(
    meter
      .prepare(
        `SELECT day, version, installs, new_installs FROM version_usage WHERE day >= ?1 AND day <= ?2 ${vw.sql} ORDER BY day, version`,
      )
      .bind(f.from, f.to, ...vw.binds),
  );
  const splits = await meter.all<{ launch_mode: string; deployment: string; os: string; installs: number; sessions: number }>(
    meter
      .prepare(
        `SELECT launch_mode, deployment, os, SUM(installs) AS installs, SUM(sessions) AS sessions FROM daily_usage
         WHERE day >= ?1 AND day <= ?2 ${w.sql} GROUP BY 1, 2, 3 ORDER BY installs DESC`,
      )
      .bind(f.from, f.to, ...w.binds),
  );
  const sum = (key: 'launch_mode' | 'deployment' | 'os') => {
    const totals = new Map<string, number>();
    for (const row of splits) totals.set(row[key] || '(unknown)', (totals.get(row[key] || '(unknown)') ?? 0) + row.installs);
    return [...totals.entries()].map(([name, installs]) => ({ name, installs })).sort((a, b) => b.installs - a.installs);
  };
  const newByDay = new Map<string, number>();
  for (const row of versions) newByDay.set(row.day, (newByDay.get(row.day) ?? 0) + row.new_installs);
  return {
    filters: f,
    dau: byDay.get(f.to)?.installs ?? 0,
    wau: await distinct(addDays(f.to, -6)),
    mau: await distinct(addDays(f.to, -29)),
    daily: daysBetween(f.from, f.to).map((day) => ({
      day,
      installs: byDay.get(day)?.installs ?? 0,
      sessions: byDay.get(day)?.sessions ?? 0,
      errors: byDay.get(day)?.errors ?? 0,
      new_installs: newByDay.get(day) ?? 0,
    })),
    versions,
    launch_mode: sum('launch_mode'),
    deployment: sum('deployment'),
    os: sum('os'),
  };
}

export async function data(meter: Meter, f: Filters) {
  const w = where(f, ['version'], 3);
  const modalities = await meter.all(
    meter
      .prepare(
        `SELECT modality, image_kind, run_format, table_kind, SUM(n) AS n, SUM(installs) AS installs, SUM(distributed) AS distributed
         FROM modality_usage WHERE day >= ?1 AND day <= ?2 ${w.sql} GROUP BY 1, 2, 3, 4 ORDER BY n DESC`,
      )
      .bind(f.from, f.to, ...w.binds),
  );
  const scale = await meter.all<{ dim: string; band: string; n: number }>(
    meter
      .prepare(
        `SELECT dim, band, SUM(n) AS n FROM data_scale_usage WHERE day >= ?1 AND day <= ?2 ${w.sql} GROUP BY 1, 2`,
      )
      .bind(f.from, f.to, ...w.binds),
  );
  const labels = (VECTORS.band10_labels as string[]).concat(VECTORS.pow2_labels as string[]);
  const dims = new Map<string, { band: string; n: number }[]>();
  for (const row of scale) {
    const list = dims.get(row.dim) ?? [];
    list.push({ band: row.band, n: row.n });
    dims.set(row.dim, list);
  }
  for (const list of dims.values()) list.sort((a, b) => labels.indexOf(a.band) - labels.indexOf(b.band));
  return { filters: f, modalities, scale: Object.fromEntries(dims) };
}

function withPercentiles<T extends Record<string, unknown>>(row: T) {
  const bins = binsOf(row);
  return { ...row, p50: percentile(bins, 0.5), p95: percentile(bins, 0.95) };
}

export async function features(meter: Meter, f: Filters) {
  const w = where(f, ['version'], 3);
  const bins = Array.from({ length: VECTORS.hist_bins }, (_, i) => `SUM(b${i}) AS b${i}`).join(', ');
  const run = (sql: string) => meter.all<Record<string, unknown>>(meter.prepare(sql).bind(f.from, f.to, ...w.binds));
  const range = `day >= ?1 AND day <= ?2 ${w.sql}`;
  const plugins = await run(
    `SELECT plugin, SUM(opens) AS opens, SUM(closes) AS closes, SUM(load_failed) AS load_failed, SUM(installs) AS installs, ${bins}
     FROM plugin_usage WHERE ${range} GROUP BY 1 ORDER BY opens DESC`,
  );
  const used = await run(
    `SELECT plugin, feature, SUM(n) AS n, SUM(installs) AS installs FROM feature_usage WHERE ${range} GROUP BY 1, 2 ORDER BY n DESC`,
  );
  const functions = await run(
    `SELECT source, fn, SUM(n) AS n, SUM(err) AS err, SUM(installs) AS installs, ${bins}
     FROM function_usage WHERE ${range} GROUP BY 1, 2 ORDER BY n DESC LIMIT 200`,
  );
  const capabilities = await run(
    `SELECT capability, owner, SUM(ok) AS ok, SUM(err) AS err, SUM(refused) AS refused, SUM(nested) AS nested,
       SUM(installs) AS installs, ${bins} FROM capability_usage WHERE ${range} GROUP BY 1, 2 ORDER BY ok + err DESC`,
  );
  const transitions = await run(
    `SELECT from_cap, to_cap, SUM(n) AS n FROM capability_transitions WHERE ${range} GROUP BY 1, 2 ORDER BY n DESC LIMIT 30`,
  );
  const usedKeys = new Set(used.map((row) => row.feature));
  return {
    filters: f,
    plugins: plugins.map(withPercentiles),
    features: used,
    unused: VECTORS.feature_keys.filter((key) => !usedKeys.has(key)),
    functions: functions.map(withPercentiles),
    capabilities: capabilities.map(withPercentiles),
    transitions,
  };
}

/** Axes the performance page can re-aggregate by: keys that appear in dims_key. */
export const PERF_AXES = ['none', 'path', 'label_renderer', 'browser', 'gpu', 'route', 'family', 'kind', 'source', 'modality', 'tool', 'status'];

export async function performance(meter: Meter, f: Filters, axis: string) {
  const w = where(f, ['version'], 3);
  const rows = await meter.all<Record<string, any>>(
    meter
      .prepare(`SELECT * FROM performance_rollups WHERE day >= ?1 AND day <= ?2 ${w.sql}`)
      .bind(f.from, f.to, ...w.binds),
  );
  const safeAxis = PERF_AXES.includes(axis) ? axis : 'none';
  const groups = new Map<string, { metric: string; value: string; n: number; ok: number; err: number; max: number | null; bins: number[] }>();
  for (const row of rows) {
    const dims = Object.fromEntries(
      String(row.dims_key)
        .split(';')
        .filter(Boolean)
        .map((pair) => pair.split('=') as [string, string]),
    );
    const value = safeAxis === 'none' ? '' : dims[safeAxis] ?? '(none)';
    const key = `${row.metric}|${value}`;
    const group = groups.get(key) ?? { metric: row.metric, value, n: 0, ok: 0, err: 0, max: null, bins: new Array(VECTORS.hist_bins).fill(0) };
    group.n += row.n;
    group.ok += row.ok;
    group.err += row.err;
    if (row.max !== null) group.max = Math.max(group.max ?? 0, row.max);
    group.bins = addBins(group.bins, binsOf(row));
    groups.set(key, group);
  }
  const metrics = [...groups.values()]
    .map((g) => ({
      ...g,
      p50: percentile(g.bins, 0.5, undefined, g.max),
      p95: percentile(g.bins, 0.95, undefined, g.max),
      failure_rate: g.n ? g.err / g.n : 0,
    }))
    .sort((a, b) => a.metric.localeCompare(b.metric) || a.value.localeCompare(b.value));

  // Version vs previous, paired bands, for the headline metric.
  const versions = new Map<string, number[]>();
  for (const row of rows.filter((r) => r.metric === 'render.summary.tile_ms')) {
    versions.set(row.version, addBins(versions.get(row.version) ?? new Array(VECTORS.hist_bins).fill(0), binsOf(row)));
  }
  const byVersion = [...versions.entries()]
    .map(([version, bins]) => ({ version, bins, p50: percentile(bins, 0.5), p95: percentile(bins, 0.95) }))
    .sort((a, b) => a.version.localeCompare(b.version, undefined, { numeric: true }));
  return { filters: f, axis: safeAxis, axes: PERF_AXES, metrics, tile_load_by_version: byVersion, labels: VECTORS.ms_labels };
}

export async function errors(meter: Meter, f: Filters) {
  const w = where(f, ['version'], 3);
  const rows = await meter.all<Record<string, unknown>>(
    meter
      .prepare(
        `SELECT d.fp, SUM(d.n) AS count, SUM(d.installs) AS installs, r.side, r.exception, r.component, r.route, r.action,
           r.file, r.plugin, r.first_seen, r.first_version, r.last_seen, r.last_version, r.muted_at
         FROM error_daily d LEFT JOIN error_fingerprints r ON r.fp = d.fp
         WHERE d.day >= ?1 AND d.day <= ?2 ${w.sql.replace(/version/g, 'd.version')}
         GROUP BY d.fp ORDER BY r.muted_at IS NOT NULL, count DESC LIMIT 200`,
      )
      .bind(f.from, f.to, ...w.binds),
  );
  return { filters: f, errors: rows };
}

export async function errorDetail(meter: Meter, fp: string, now: number) {
  if (!/^[0-9a-f]{8}(?:[0-9a-f]{8})?$/.test(fp)) return null;
  const registry = await meter.first(meter.prepare('SELECT * FROM error_fingerprints WHERE fp = ?1').bind(fp));
  if (!registry) return null;
  // error_daily's key is day-first: bound the scan to its 60-day retention.
  const from = addDays(dayOf(now), -60);
  const series = await meter.all(
    meter
      .prepare('SELECT day, version, n, installs FROM error_daily WHERE day >= ?1 AND fp = ?2 ORDER BY day, version')
      .bind(from, fp),
  );
  return { registry, series };
}

export async function budget(meter: Meter, env: Env, now: number) {
  const today = dayOf(now);
  const rows = await meter.all<Record<string, any>>(
    meter.prepare('SELECT * FROM budget_daily WHERE day >= ?1 ORDER BY day').bind(addDays(today, -13)),
  );
  const row = rows.find((r) => r.day === today) ?? null;
  const counters: Counters = {
    requests: Number(row?.requests ?? 0),
    d1_rows_read: Number(row?.d1_rows_read ?? 0),
    d1_rows_written: Number(row?.d1_rows_written ?? 0),
  };
  const r2 = await meter.first<{ bytes: number | null; count: number; oldest: string | null }>(
    meter.prepare(`SELECT SUM(bytes) AS bytes, COUNT(*) AS count, MIN(period) AS oldest FROM archives WHERE deleted_at IS NULL AND kind = 'daily'`),
  );
  const oldestBatch = await meter.first<{ day: string }>(meter.prepare('SELECT day FROM batches ORDER BY day LIMIT 1'));
  const steps = await meter.all(
    meter
      .prepare(`SELECT * FROM maintenance_log WHERE day >= ?1 AND state NOT IN ('done', 'skipped') ORDER BY day, step_order`)
      .bind(addDays(today, -30)),
  );
  const lastBackup = await meter.first(
    meter.prepare(`SELECT day, state, finished, error FROM maintenance_log WHERE day >= ?1 AND step = 'verify' ORDER BY day DESC LIMIT 1`).bind(addDays(today, -30)),
  );
  const month = today.slice(0, 7);
  const monthRows = rows.filter((r) => String(r.day).startsWith(month));
  const k = knobs(env);
  return {
    day: today,
    state: stateFor(env, counters),
    ratio: budgetRatio(env, counters),
    today: row,
    meters: [
      { name: 'Worker requests', used: counters.requests, share: k.WORKER_REQUEST_BUDGET, platform: 100000 },
      { name: 'D1 rows written', used: counters.d1_rows_written, share: k.D1_WRITE_BUDGET, platform: 100000 },
      { name: 'D1 rows read', used: counters.d1_rows_read, share: k.D1_READ_BUDGET, platform: 5000000 },
    ],
    thresholds: { warn: k.BUDGET_WARN, aggregate: k.BUDGET_AGGREGATE, reduce: k.BUDGET_REDUCE, critical: k.BUDGET_CRITICAL, stop: k.BUDGET_STOP },
    bytes_in: Number(row?.bytes_in ?? 0),
    r2: {
      bytes: Number(r2?.bytes ?? 0),
      archives: r2?.count ?? 0,
      target: k.R2_TARGET_BYTES,
      warn: k.R2_WARN_BYTES,
      aggressive: k.R2_AGGRESSIVE_BYTES,
      hard: k.R2_HARD_BYTES,
      class_a_this_month_seen: monthRows.reduce((sum, r) => sum + Number(r.r2_puts ?? 0), 0),
      class_b_this_month_seen: monthRows.reduce((sum, r) => sum + Number(r.r2_gets ?? 0), 0),
    },
    write_ratio_14d: rows.map((r) => ({ day: r.day, ratio: Number(r.d1_rows_written ?? 0) / k.D1_WRITE_BUDGET })),
    oldest_batch_day: oldestBatch?.day ?? null,
    oldest_archive_day: r2?.oldest ?? null,
    next_purge_day: oldestBatch ? addDays(oldestBatch.day, knob(env, 'BATCH_RETENTION_DAYS') + 1) : null,
    last_backup: lastBackup,
    open_steps: steps,
    knobs: k,
  };
}

export async function backups(meter: Meter) {
  const rows = await meter.all(
    meter.prepare(
      `SELECT key, kind, period, segments, bytes, records, sha256, created, verified_at, expires, deleted_at
       FROM archives ORDER BY created DESC LIMIT 500`,
    ),
  );
  const failed = await meter.all(
    meter.prepare(`SELECT day, step, state, error, finished FROM maintenance_log WHERE state = 'failed' ORDER BY day DESC LIMIT 50`),
  );
  return { archives: rows, failed };
}

export async function install(meter: Meter, id: string, now: number) {
  if (!/^[0-9a-f]{32}$/.test(id)) return null;
  const row = await meter.first(meter.prepare('SELECT * FROM installs WHERE install_id = ?1').bind(id));
  if (!row) return null;
  // No install_id index on daily_installs by design: bounded to 90 days.
  const days = await meter.all(
    meter
      .prepare('SELECT * FROM daily_installs WHERE day >= ?1 AND install_id = ?2 ORDER BY day')
      .bind(addDays(dayOf(now), -90), id),
  );
  return { install: row, days };
}
