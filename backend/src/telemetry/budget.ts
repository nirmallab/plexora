/**
 * The budget monitor.
 *
 * Every D1 row written and read, every request and every R2 operation lands in
 * one `budget_daily` row per UTC day. The ratio of the day's usage to the
 * configured *share* of the account's free tier picks a state, and the state
 * decides what ingest accepts and what the `config` block tells clients.
 *
 * Cost discipline: ingest never spends a read on this. It refreshes a
 * per-isolate cache from the RETURNING row of the `budget_daily` upsert it
 * already performs, and reads the row only when the cache is empty, from
 * another day, or (while stopped) older than BUDGET_CACHE_S. Isolates can each
 * overshoot a threshold by a request, which is noise against a 40k share.
 * Requests that write nothing (401s, 415s, /healthz) are counted in isolate
 * memory and added to the next upsert, as are the measured reads of the
 * previous ingest batch.
 */
import { dayOf, type Env, knob, secondsToMidnight } from '../env';

export type BudgetState = 'ok' | 'warn' | 'aggregate' | 'reduce' | 'critical' | 'stop';
export const STATES: BudgetState[] = ['ok', 'warn', 'aggregate', 'reduce', 'critical', 'stop'];

export interface Counters {
  requests: number;
  d1_rows_read: number;
  d1_rows_written: number;
}

export interface WireConfig {
  level_max: 'anonymous' | 'diagnostics';
  upload_interval_s: number;
  sample: number;
  disabled_until: number;
}

export function budgetRatio(env: Env, counters: Counters): number {
  return Math.max(
    counters.requests / knob(env, 'WORKER_REQUEST_BUDGET'),
    counters.d1_rows_written / knob(env, 'D1_WRITE_BUDGET'),
    counters.d1_rows_read / knob(env, 'D1_READ_BUDGET'),
  );
}

export function stateFor(env: Env, counters: Counters): BudgetState {
  const ratio = budgetRatio(env, counters);
  if (ratio >= knob(env, 'BUDGET_STOP')) return 'stop';
  if (ratio >= knob(env, 'BUDGET_CRITICAL')) return 'critical';
  if (ratio >= knob(env, 'BUDGET_REDUCE')) return 'reduce';
  if (ratio >= knob(env, 'BUDGET_AGGREGATE')) return 'aggregate';
  if (ratio >= knob(env, 'BUDGET_WARN')) return 'warn';
  return 'ok';
}

// ---------------------------------------------------------------------------
// Per-isolate cache
// ---------------------------------------------------------------------------

interface Cache {
  day: string;
  state: BudgetState;
  counters: Counters;
  readAt: number;
  /** A D1 quota error: stopped until the UTC day ends, whatever the counters say. */
  forced: boolean;
}

let cache: Cache | null = null;

/**
 * Counts waiting to ride on the next budget upsert: requests that wrote
 * nothing, measured reads of earlier statements, duplicates and rate limits
 * discovered after the fact.
 */
export const pending: Partial<Record<BudgetColumn, number>> = {};

export function addPending(column: BudgetColumn, amount = 1): void {
  pending[column] = (pending[column] ?? 0) + amount;
}

/** Test seam: module state would otherwise leak between test cases. */
export function resetBudgetCache(): void {
  cache = null;
  for (const key of Object.keys(pending)) delete pending[key as BudgetColumn];
  configCache = null;
}

/** What /healthz reports: the cache only, never a D1 read. */
export function cachedState(now: number): BudgetState | 'unknown' {
  if (!cache || cache.day !== dayOf(now)) return 'unknown';
  return cache.state;
}

export function noteCounters(env: Env, day: string, row: Partial<Counters> | null | undefined, now: number) {
  if (!row) return;
  const counters: Counters = {
    requests: Number(row.requests ?? 0),
    d1_rows_read: Number(row.d1_rows_read ?? 0),
    d1_rows_written: Number(row.d1_rows_written ?? 0),
  };
  const forced = cache?.day === day && cache.forced;
  cache = { day, counters, state: forced ? 'stop' : stateFor(env, counters), readAt: now, forced };
}

/** D1's own daily limit: nothing more will be written today, so stop asking. */
export function forceStop(now: number): void {
  const day = dayOf(now);
  cache = {
    day,
    state: 'stop',
    counters: cache?.counters ?? { requests: 0, d1_rows_read: 0, d1_rows_written: 0 },
    readAt: now,
    forced: true,
  };
}

export function isQuotaError(error: unknown): boolean {
  return /limit|exceed|quota/i.test(String((error as Error)?.message ?? error));
}

/**
 * The state ingest gates on. Reads D1 only for a cold or stale cache (see the
 * module comment); a read failure is treated as "unknown, carry on" except for
 * a quota error, which is exactly the signal to stop.
 */
export async function currentState(env: Env, now: number): Promise<BudgetState> {
  const day = dayOf(now);
  const fresh = cache !== null && cache.day === day;
  if (fresh && cache!.forced) return 'stop';
  const stale = fresh && cache!.state === 'stop' && now - cache!.readAt >= knob(env, 'BUDGET_CACHE_S');
  if (fresh && !stale) return cache!.state;
  try {
    const result = await env.TELEMETRY_DB.prepare(
      'SELECT requests, d1_rows_read, d1_rows_written FROM budget_daily WHERE day = ?',
    )
      .bind(day)
      .all<Counters>();
    addPending('d1_rows_read', result.meta.rows_read ?? 0);
    noteCounters(env, day, result.results[0] ?? { requests: 0, d1_rows_read: 0, d1_rows_written: 0 }, now);
  } catch (error) {
    if (isQuotaError(error)) forceStop(now);
    else return cache?.day === day ? cache.state : 'ok';
  }
  return cache!.state;
}

// ---------------------------------------------------------------------------
// The budget_daily upsert
// ---------------------------------------------------------------------------

export type BudgetColumn =
  | 'requests'
  | 'register_requests'
  | 'admin_requests'
  | 'cron_ticks'
  | 'batches'
  | 'events'
  | 'duplicates'
  | 'rate_limited'
  | 'refused_503'
  | 'dropped_events'
  | 'd1_rows_read'
  | 'd1_rows_written'
  | 'bytes_in'
  | 'r2_puts'
  | 'r2_gets'
  | 'r2_bytes';

const COLUMNS: BudgetColumn[] = [
  'requests',
  'register_requests',
  'admin_requests',
  'cron_ticks',
  'batches',
  'events',
  'duplicates',
  'rate_limited',
  'refused_503',
  'dropped_events',
  'd1_rows_read',
  'd1_rows_written',
  'bytes_in',
  'r2_puts',
  'r2_gets',
  'r2_bytes',
];

/**
 * An optional "did the batch insert land?" probe for ingest: the upsert runs
 * in the same `db.batch()` as the insert, so a subquery can see whether it
 * wrote a row, and `perInsert` amounts are added only when it did.
 */
export interface InsertProbe {
  sql: string;
  binds: unknown[];
  perInsert: Partial<Record<BudgetColumn, number>>;
}

/**
 * One `budget_daily` upsert adding `delta` (plus everything pending in this
 * isolate), returning the day's totals. Exactly one row written.
 *
 * Column names come from the fixed list above, never from input, so the SQL
 * text is built from constants only.
 */
export function budgetStatement(
  env: Env,
  day: string,
  delta: Partial<Record<BudgetColumn, number>>,
  state?: BudgetState,
  probe?: InsertProbe,
): D1PreparedStatement {
  const binds: unknown[] = [day];
  const place = (value: unknown) => {
    binds.push(value);
    return `?${binds.length}`;
  };
  const values = COLUMNS.map((column) => {
    const base = Math.round((delta[column] ?? 0) + (pending[column] ?? 0));
    const per = Math.round(probe?.perInsert[column] ?? 0);
    return per ? `${place(base)} + i.n * ${place(per)}` : place(base);
  });
  for (const key of Object.keys(pending)) delete pending[key as BudgetColumn];
  const from = probe ? `(${probe.sql.replace(/\?(\d+)/g, (_, n: string) => place(probe.binds[Number(n) - 1]))}) i` : '(SELECT 0 AS n) i';
  const updates = COLUMNS.map((column) => `${column} = ${column} + excluded.${column}`).join(', ');
  const stateSql = state ? `, state = ${place(state)}` : '';
  const sql = `INSERT INTO budget_daily (day, ${COLUMNS.join(', ')})
    SELECT ?1, ${values.join(', ')} FROM ${from} WHERE true
    ON CONFLICT(day) DO UPDATE SET ${updates}${stateSql}
    RETURNING requests, d1_rows_read, d1_rows_written`;
  return env.TELEMETRY_DB.prepare(sql).bind(...binds);
}

// ---------------------------------------------------------------------------
// Metering for cron and admin paths
// ---------------------------------------------------------------------------

/**
 * Wraps D1 calls and sums their `meta`, so a cron tick or an admin request can
 * record what it cost in the single budget upsert it ends with.
 */
export class Meter {
  reads = 0;
  writes = 0;
  r2Puts = 0;
  r2Gets = 0;
  r2Bytes = 0;

  constructor(private readonly db: D1Database) {}

  private note(meta: D1Meta | undefined) {
    this.reads += meta?.rows_read ?? 0;
    this.writes += meta?.rows_written ?? 0;
  }

  async batch(statements: D1PreparedStatement[]): Promise<D1Result[]> {
    if (statements.length === 0) return [];
    const results = await this.db.batch(statements);
    for (const result of results) this.note(result.meta);
    return results;
  }

  async all<T = Record<string, unknown>>(statement: D1PreparedStatement): Promise<T[]> {
    const result = await statement.all<T>();
    this.note(result.meta);
    return result.results;
  }

  async first<T = Record<string, unknown>>(statement: D1PreparedStatement): Promise<T | null> {
    return (await this.all<T>(statement))[0] ?? null;
  }

  async run(statement: D1PreparedStatement): Promise<D1Result> {
    const result = await statement.run();
    this.note(result.meta);
    return result;
  }

  prepare(sql: string): D1PreparedStatement {
    return this.db.prepare(sql);
  }

  /**
   * Records this meter into budget_daily. The upsert's own row is counted as
   * one more write. Failures are swallowed: losing a meter reading must never
   * fail the work it measured.
   */
  async flush(
    env: Env,
    now: number,
    extra: Partial<Record<BudgetColumn, number>> = {},
    state?: BudgetState,
  ): Promise<void> {
    const day = dayOf(now);
    try {
      const row = await budgetStatement(
        env,
        day,
        {
          ...extra,
          d1_rows_read: this.reads + (extra.d1_rows_read ?? 0),
          d1_rows_written: this.writes + 1 + (extra.d1_rows_written ?? 0),
          r2_puts: this.r2Puts,
          r2_gets: this.r2Gets,
          r2_bytes: this.r2Bytes,
        },
        state,
      ).first<Counters>();
      noteCounters(env, day, row, now);
    } catch (error) {
      if (isQuotaError(error)) forceStop(now);
    }
  }
}

// ---------------------------------------------------------------------------
// The config block
// ---------------------------------------------------------------------------

interface ConfigRow {
  version: string;
  level_max: string | null;
  upload_interval_s: number | null;
  sample: number | null;
  disabled_until: number;
}

let configCache: { rows: Map<string, ConfigRow>; at: number } | null = null;

export function resetConfigCache(): void {
  configCache = null;
}

async function configRows(env: Env, now: number): Promise<Map<string, ConfigRow>> {
  if (configCache && now - configCache.at < knob(env, 'CONFIG_CACHE_S')) return configCache.rows;
  try {
    const result = await env.TELEMETRY_DB.prepare(
      'SELECT version, level_max, upload_interval_s, sample, disabled_until FROM client_config',
    ).all<ConfigRow>();
    addPending('d1_rows_read', result.meta.rows_read ?? 0);
    configCache = { rows: new Map(result.results.map((row) => [row.version, row])), at: now };
  } catch {
    // A config lookup that fails is a config that does not change.
    configCache = { rows: configCache?.rows ?? new Map(), at: now };
  }
  return configCache.rows;
}

/**
 * The `config` block every 2xx/429/503 carries: exactly the four keys the
 * Python client reads. A per-version row overrides the `*` row; the budget
 * state can only make it more conservative.
 */
export async function configFor(
  env: Env,
  state: BudgetState,
  version: string | null | undefined,
  now: number,
): Promise<WireConfig> {
  const base = knob(env, 'UPLOAD_INTERVAL_S');
  const config: WireConfig = { level_max: 'diagnostics', upload_interval_s: base, sample: 1, disabled_until: 0 };

  // At stop, D1 may be refusing reads too; the config is then budget-only.
  const rows = state === 'stop' ? configCache?.rows ?? new Map<string, ConfigRow>() : await configRows(env, now);
  for (const row of [rows.get('*'), version ? rows.get(version) : undefined]) {
    if (!row) continue;
    if (row.level_max === 'anonymous' || row.level_max === 'diagnostics') config.level_max = row.level_max;
    if (typeof row.upload_interval_s === 'number' && row.upload_interval_s > 0) {
      config.upload_interval_s = row.upload_interval_s;
    }
    if (typeof row.sample === 'number' && row.sample >= 0 && row.sample <= 1) config.sample = row.sample;
    if (row.disabled_until > now) config.disabled_until = Math.max(config.disabled_until, row.disabled_until);
  }

  switch (state) {
    case 'aggregate':
      config.upload_interval_s *= 3;
      config.sample = Math.min(config.sample, 0.5);
      break;
    case 'reduce':
      config.level_max = 'anonymous';
      config.upload_interval_s *= 3;
      config.sample = Math.min(config.sample, 0.25);
      break;
    case 'critical':
      config.level_max = 'anonymous';
      config.upload_interval_s *= 6;
      config.sample = Math.min(config.sample, 0.25);
      break;
    case 'stop':
      config.level_max = 'anonymous';
      config.upload_interval_s *= 6;
      config.sample = Math.min(config.sample, 0.25);
      config.disabled_until = Math.max(config.disabled_until, now + retryAfterMidnight(now));
      break;
    default:
      break;
  }
  return config;
}

/** Seconds to UTC midnight plus up to 15 minutes of jitter, so clients do not stampede. */
export function retryAfterMidnight(now: number): number {
  return secondsToMidnight(now) + Math.floor(Math.random() * 900);
}

/** What ingest keeps in each state (see B.5). */
export function ingestPolicy(state: BudgetState): { levelMax: 'anonymous' | 'diagnostics'; maxPriority: number } {
  if (state === 'critical') return { levelMax: 'anonymous', maxPriority: 3 };
  if (state === 'reduce') return { levelMax: 'anonymous', maxPriority: 5 };
  return { levelMax: 'diagnostics', maxPriority: Infinity };
}

/** Which maintenance steps may run in a state (B.5 "cron" column). */
export function cronAllows(state: BudgetState, step: string): boolean {
  if (state === 'stop') return false;
  const exportish = step === 'export' || step === 'verify' || step === 'monthly_export' || step === 'monthly_verify';
  const purge = step === 'purge_batches' || step === 'purge_rollups';
  if (state === 'critical') return exportish || purge;
  if (state === 'reduce') return step === 'fold' || exportish || purge;
  return true;
}

