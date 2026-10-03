/**
 * Capacity: how close Plexora AI is to each limit that would make users wait,
 * and which change lifts it.
 *
 * Three kinds of limit, each graded ok / watch / act from the busiest recent
 * day or minute:
 *
 * - Cloudflare's plan. Workers Free allows 100,000 requests a day and 10 ms of
 *   CPU per request; D1 Free allows 100,000 rows written and 5 million read a
 *   day. These are ACCOUNT-wide: the telemetry Worker counts too. Measured from
 *   Cloudflare's GraphQL analytics when CF_ANALYTICS_TOKEN and CF_ACCOUNT_ID
 *   are set; otherwise estimated from this Worker's own AI calls, and labelled
 *   so.
 * - One D1 database. It runs one query at a time, roughly 1,000 a second; no
 *   plan changes that. Past it, balances move to a Durable Object per account.
 * - The provider. Its limits are per Plexora key, so they are shared by every
 *   user. Measured as the 429 share of calls, and against the written limit an
 *   admin enters (AI_PROVIDER_RPM / AI_PROVIDER_RPD) when there is one.
 *
 * `assess` is pure; `gather` reads D1 and the analytics API. Nothing here
 * writes, and a failed analytics call degrades to the estimate.
 */
import type { Env } from '../env';
import { DAY, knob } from '../env';
import { providerFetch } from './providers';

export type Level = 'ok' | 'watch' | 'act' | 'unknown';
export type Source = 'measured' | 'estimated' | 'config';

/** Published plan limits (developers.cloudflare.com, 2026-10). */
export const PLAN = {
  free: { worker_requests_day: 100_000, cpu_ms: 10, d1_rows_written_day: 100_000, d1_rows_read_day: 5_000_000,
    d1_db_bytes: 500_000_000 },
  paid: { worker_requests_month: 10_000_000, cpu_ms: 30_000, d1_rows_written_month: 50_000_000,
    d1_rows_read_month: 25_000_000_000, d1_db_bytes: 10_000_000_000 },
} as const;

/**
 * What one model call costs the database and the Worker, for the estimate.
 * Production on 2026-10-02: about 80 calls against 1,416 rows written, 6,515
 * read and 2,576 queries in 24 h (`wrangler d1 info`). Those totals include
 * licence and admin traffic, so these are upper bounds.
 */
export const PER_CALL = { rows_written: 18, rows_read: 80, queries: 32, worker_requests: 1.1 } as const;

/** A single D1 database at about 1 ms a query. */
export const D1_QUERIES_PER_SECOND = 1_000;

export const WATCH = 0.5;
export const ACT = 0.8;

export interface DayCount { day: string; value: number }

export interface Cloudflare {
  /** Account-wide, per UTC day, newest last; 30 days when the API returns them. */
  worker_requests: DayCount[];
  d1_rows_written: DayCount[];
  d1_rows_read: DayCount[];
  /** Requests that ended on a resource limit (CPU, memory) in the window. */
  exceeded: number;
  /** The highest daily p99 CPU time, ms, and the Worker and day it came from. */
  cpu_p99_ms: number | null;
  cpu_p99_at?: { script: string; day: string } | null;
  /** The largest database in the account, bytes. */
  db_bytes: number | null;
  errors: string[];
}

export interface Usage {
  now_ms: number;
  /** AI calls per UTC day, last 7 days (shadow calls included: they cost the same). */
  calls_by_day: DayCount[];
  calls_30d: number;
  peak_minute: { calls: number; at_ms: number } | null;
  calls_7d: number;
  rate_limited_7d: number;
  db_bytes_local: number | null;
  /** Scopes whose primary route is a free model, as "scope: provider/model". */
  free_serving: string[];
  /** Scopes served by a single route, with nothing to fail over to. */
  no_fallback: string[];
  cloudflare: Cloudflare | null;
  analytics_configured: boolean;
  paid: boolean;
  provider_rpm: number;
  provider_rpd: number;
}

export interface Signal {
  key: string;
  group: 'Cloudflare' | 'Database' | 'Provider';
  title: string;
  value: number | null;
  limit: number | null;
  unit: string;
  level: Level;
  source: Source;
  detail: string;
  action: string;
}

export interface Assessment {
  level: Level;
  headline: string;
  move_to_paid: boolean;
  plan: 'free' | 'paid';
  signals: Signal[];
  analytics: { configured: boolean; errors: string[] };
}

function grade(share: number | null): Level {
  if (share === null || !Number.isFinite(share)) return 'unknown';
  return share >= ACT ? 'act' : share >= WATCH ? 'watch' : 'ok';
}

const busiest = (rows: DayCount[]) => rows.reduce((m, r) => (r.value > m.value ? r : m), { day: '', value: 0 });
const total = (rows: DayCount[]) => rows.reduce((s, r) => s + r.value, 0);
const lastDays = (rows: DayCount[], days: number, nowMs: number) => {
  const from = new Date(nowMs - days * DAY * 1000).toISOString().slice(0, 10);
  return rows.filter((r) => r.day > from);
};

const fmt = (n: number) => Math.round(n).toLocaleString('en-US');

/**
 * One limit read from a daily series: on Free the busiest of the last 7 days
 * against the daily limit, on Paid the last 30 days against the monthly
 * inclusion (past it is billed overage, not an outage, so it is only watched).
 */
function planSignal(u: Usage, key: string, title: string, measured: DayCount[] | null, per: number,
  freeDay: number, paidMonth: number, unit: string, what: string, overage: string): Signal {
  const estimate = u.calls_by_day.map((r) => ({ day: r.day, value: r.value * per }));
  const source: Source = measured ? 'measured' : 'estimated';
  const series = measured ?? estimate;
  const scope = measured ? 'whole Cloudflare account' : `estimated from AI calls alone, ${what}`;
  if (!u.paid) {
    const peak = busiest(lastDays(series, 7, u.now_ms));
    const level = grade(peak.value / freeDay);
    return { key, group: 'Cloudflare', title, value: peak.value, limit: freeDay, unit, level, source,
      detail: `Busiest UTC day of the last 7${peak.day ? ` (${peak.day})` : ''}, ${scope}. Free stops serving at the limit until 00:00 UTC.`,
      action: level === 'ok' ? 'Nothing to do.' : 'Move the account to Workers Paid ($5 a month).' };
  }
  const month = measured ? total(lastDays(series, 30, u.now_ms)) : u.calls_30d * per;
  const level = grade(month / paidMonth);
  return { key, group: 'Cloudflare', title: `${title}, 30 days`, value: month, limit: paidMonth, unit,
    level: level === 'act' ? 'watch' : level, source,
    detail: `Last 30 days against the monthly inclusion, ${scope}.`,
    action: level === 'ok' ? 'Nothing to do.' : `Past the inclusion it is billed: ${overage}. Not an outage.` };
}

export function assess(u: Usage): Assessment {
  const cf = u.cloudflare;
  const signals: Signal[] = [];

  // -- Cloudflare's plan ------------------------------------------------------------
  const requests = planSignal(u, 'worker_requests', 'Worker requests per day', cf?.worker_requests.length ? cf.worker_requests : null,
    PER_CALL.worker_requests, PLAN.free.worker_requests_day, PLAN.paid.worker_requests_month, 'requests',
    'not counting licence traffic or the telemetry Worker', '$0.30 per million');
  const written = planSignal(u, 'd1_rows_written', 'D1 rows written per day', cf?.d1_rows_written.length ? cf.d1_rows_written : null,
    PER_CALL.rows_written, PLAN.free.d1_rows_written_day, PLAN.paid.d1_rows_written_month, 'rows',
    `about ${PER_CALL.rows_written} per call`, '$1.00 per million');
  const read = planSignal(u, 'd1_rows_read', 'D1 rows read per day', cf?.d1_rows_read.length ? cf.d1_rows_read : null,
    PER_CALL.rows_read, PLAN.free.d1_rows_read_day, PLAN.paid.d1_rows_read_month, 'rows',
    `about ${PER_CALL.rows_read} per call`, '$0.001 per million');
  signals.push(requests, written, read);

  const cpuLimit = u.paid ? PLAN.paid.cpu_ms : PLAN.free.cpu_ms;
  if (cf && (cf.cpu_p99_ms !== null || cf.exceeded > 0)) {
    // Only a request Cloudflare stopped is an outage; running over with none stopped is a warning.
    const over = grade((cf.cpu_p99_ms ?? 0) / cpuLimit);
    const level: Level = cf.exceeded > 0 ? (u.paid ? 'watch' : 'act') : over === 'act' ? 'watch' : over;
    const where = cf.cpu_p99_at ? ` (${cf.cpu_p99_at.script}, ${cf.cpu_p99_at.day})` : '';
    signals.push({ key: 'cpu', group: 'Cloudflare', title: 'CPU per request, p99', value: cf.cpu_p99_ms,
      limit: cpuLimit, unit: 'ms', level, source: 'measured',
      detail: `Highest daily p99 of the last 7 days${where}, any Worker on the account. ${fmt(cf.exceeded)} request(s) `
        + 'were stopped on a resource limit. Streams from OrcaRouter and OpenRouter are translated in the Worker, so '
        + 'long answers cost CPU.',
      action: level === 'ok' ? 'Nothing to do.'
        : cf.exceeded > 0 ? (u.paid ? 'Raise limits.cpu_ms in wrangler.toml.'
          : 'Requests are being stopped: move the account to Workers Paid (30 s per request).')
          : 'Some requests run over the limit but none were stopped yet. Cloudflare may stop them without notice; '
            + 'Workers Paid removes the risk.' });
  } else {
    signals.push({ key: 'cpu', group: 'Cloudflare', title: 'CPU per request, p99', value: null, limit: cpuLimit,
      unit: 'ms', level: 'unknown', source: 'measured',
      detail: u.analytics_configured ? 'Cloudflare analytics returned no CPU figures; see the errors below.'
        : 'Only Cloudflare analytics can see CPU time.',
      action: u.analytics_configured ? 'Check the analytics token\'s permissions.' : 'Set CF_ANALYTICS_TOKEN to measure it.' });
  }

  const bytes = cf?.db_bytes ?? u.db_bytes_local;
  const bytesLimit = u.paid ? PLAN.paid.d1_db_bytes : PLAN.free.d1_db_bytes;
  const storage = grade(bytes === null ? null : bytes / bytesLimit);
  signals.push({ key: 'd1_storage', group: 'Cloudflare', title: 'D1 database size', value: bytes, limit: bytesLimit,
    unit: 'bytes', level: storage, source: cf?.db_bytes != null ? 'measured' : 'estimated',
    detail: `The per-database cap on ${u.paid ? 'Paid' : 'Free'}. Call records are kept for AI_REQUEST_RETENTION_DAYS.`,
    action: storage === 'ok' ? 'Nothing to do.' : u.paid ? 'Shorten call retention, or archive ai_requests to R2.'
      : 'Move to Workers Paid (10 GB per database), or shorten call retention.' });

  // -- the one database ---------------------------------------------------------------
  const perSecond = u.peak_minute ? (u.peak_minute.calls / 60) * PER_CALL.queries : 0;
  const throughput = grade(perSecond / D1_QUERIES_PER_SECOND);
  signals.push({ key: 'd1_throughput', group: 'Database', title: 'D1 queries per second at the busiest minute',
    value: Math.round(perSecond), limit: D1_QUERIES_PER_SECOND, unit: 'queries/s', level: throughput,
    source: 'estimated',
    detail: `${u.peak_minute ? `${fmt(u.peak_minute.calls)} calls in the busiest minute of the last 7 days` : 'No calls in the last 7 days'}, `
      + `about ${PER_CALL.queries} queries each. One D1 database runs one query at a time on any plan.`,
    action: throughput === 'ok' ? 'Nothing to do.'
      : 'Move balances and holds to a Durable Object per account; a paid plan does not lift this.' });

  // -- the provider -------------------------------------------------------------------
  const share = u.calls_7d ? u.rate_limited_7d / u.calls_7d : 0;
  const limited: Level = u.calls_7d === 0 ? 'ok' : share >= 0.02 ? 'act' : share >= 0.005 ? 'watch' : 'ok';
  signals.push({ key: 'provider_429', group: 'Provider', title: 'Calls the provider rate-limited, 7 days',
    value: u.rate_limited_7d, limit: null, unit: 'calls', level: limited, source: 'measured',
    detail: `${(share * 100).toFixed(1)}% of ${fmt(u.calls_7d)} calls. Watch at 0.5%, act at 2%. The limit is per Plexora key, so every user shares it.`,
    action: limited === 'ok' ? 'Nothing to do.'
      : 'Ask the provider for a higher workspace limit, or add a fallback route on a second provider.' });

  const peakDay = busiest(lastDays(u.calls_by_day, 7, u.now_ms));
  for (const [key, title, value, limit, knobName] of [
    ['provider_rpm', 'Calls in the busiest minute', u.peak_minute?.calls ?? 0, u.provider_rpm, 'AI_PROVIDER_RPM'],
    ['provider_rpd', 'Calls on the busiest day', peakDay.value, u.provider_rpd, 'AI_PROVIDER_RPD'],
  ] as const) {
    const level = limit > 0 ? grade(value / limit) : 'unknown';
    signals.push({ key, group: 'Provider', title, value, limit: limit > 0 ? limit : null, unit: 'calls', level,
      source: limit > 0 ? 'config' : 'measured',
      detail: limit > 0 ? `Against the provider's written limit (${knobName}).`
        : `No written provider limit is entered (${knobName} = 0).`,
      action: level === 'unknown' ? 'Enter the limit from the provider\'s contract under Capacity settings.'
        : level === 'ok' ? 'Nothing to do.' : 'Ask the provider for a higher limit before it is reached.' });
  }

  signals.push({ key: 'free_model', group: 'Provider', title: 'Serving on a free model',
    value: u.free_serving.length, limit: null, unit: 'scopes', level: u.free_serving.length ? 'act' : 'ok',
    source: 'config', detail: u.free_serving.length ? `${u.free_serving.join(', ')}. Free tiers allow tens of calls a day for the whole product.`
      : 'Every serving route is on a paid model.',
    action: u.free_serving.length ? 'For testing only: assign a paid model on Task routing before real users.'
      : 'Nothing to do.' });

  signals.push({ key: 'fallback', group: 'Provider', title: 'Scopes with no fallback route',
    value: u.no_fallback.length, limit: null, unit: 'scopes', level: u.no_fallback.length ? 'watch' : 'ok',
    source: 'config', detail: u.no_fallback.length ? `${u.no_fallback.join(', ')}: an open circuit stops these for every user.`
      : 'Every capability has a rank 1 route.',
    action: u.no_fallback.length ? 'Publish a rank 1 route on a second provider.' : 'Nothing to do.' });

  const plan = signals.filter((s) => s.group === 'Cloudflare');
  const move = !u.paid && plan.some((s) => s.level === 'act');
  const acting = signals.filter((s) => s.level === 'act');
  const watching = signals.filter((s) => s.level === 'watch');
  const level: Level = acting.length ? 'act' : watching.length ? 'watch' : 'ok';
  const many = (n: number) => (n === 1 ? '1 limit' : `${n} limits`);
  const headline = move ? 'Time to move to Workers Paid'
    : acting.length ? `Act now on ${many(acting.length)}`
      : watching.length ? `Watch ${many(watching.length)}`
        : 'Within every limit';
  return { level, headline, move_to_paid: move, plan: u.paid ? 'paid' : 'free', signals,
    analytics: { configured: u.analytics_configured, errors: cf?.errors ?? [] } };
}

// -- gathering ----------------------------------------------------------------------------

const FREE_MODEL = /(:free|-free)$/i;

async function rows<T>(env: Env, sql: string, ...params: unknown[]): Promise<T[]> {
  return (await env.LICENSE_DB.prepare(sql).bind(...params).all<T>()).results ?? [];
}

/** One serving scope's effective chain, primary first: what the free-model and fallback rows read. */
export interface Chain { label: string; routes: Array<{ provider: string; model: string }> }

/**
 * The chains that serve, from the task routing view: the global default, and
 * each module or task with an assignment of its own. A scope that inherits is
 * left out, so one free default is one finding, not one per module.
 */
export function chainsOf(rows: Array<{ label: string; level: 'task' | 'module' | 'global'; serve: unknown[];
  effective: { chain: Array<{ routes: Array<{ provider: string; model: string }> }> } }>): Chain[] {
  return rows.filter((r) => r.level === 'global' || r.serve.length).map((r) => ({ label: r.label,
    routes: r.effective.chain.flatMap((link) => link.routes.map((x) => ({ provider: x.provider, model: x.model }))) }));
}

export async function gather(env: Env, nowMs: number, chains: Chain[]): Promise<Usage> {
  const since7 = nowMs - 7 * DAY * 1000;
  const since30 = nowMs - 30 * DAY * 1000;
  const [byDay, month, peak, limited, size] = await Promise.all([
    rows<{ day: string; n: number }>(env, `SELECT strftime('%Y-%m-%d', started_at_ms / 1000, 'unixepoch') AS day,
        COUNT(*) AS n FROM ai_requests WHERE started_at_ms >= ?1 GROUP BY day ORDER BY day`, since7),
    rows<{ n: number }>(env, 'SELECT COUNT(*) AS n FROM ai_requests WHERE started_at_ms >= ?1', since30),
    rows<{ m: number; n: number }>(env, `SELECT started_at_ms / 60000 AS m, COUNT(*) AS n FROM ai_requests
        WHERE started_at_ms >= ?1 GROUP BY m ORDER BY n DESC LIMIT 1`, since7),
    rows<{ calls: number; limited: number }>(env, `SELECT COUNT(*) AS calls,
        COALESCE(SUM(http_status = 429 OR failure_class = 'provider_429'), 0) AS limited
        FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow'`, since7),
    env.LICENSE_DB.prepare('SELECT 1').run().then((r) => (r.meta as { size_after?: number }).size_after ?? null)
      .catch(() => null),
  ]);
  const configured = Boolean(env.CF_ANALYTICS_TOKEN && env.CF_ACCOUNT_ID);
  return {
    now_ms: nowMs,
    calls_by_day: byDay.map((r) => ({ day: r.day, value: r.n })),
    calls_30d: month[0]?.n ?? 0,
    peak_minute: peak[0] ? { calls: peak[0].n, at_ms: peak[0].m * 60000 } : null,
    calls_7d: limited[0]?.calls ?? 0,
    rate_limited_7d: limited[0]?.limited ?? 0,
    db_bytes_local: typeof size === 'number' ? size : null,
    free_serving: chains.filter((c) => c.routes[0] && FREE_MODEL.test(c.routes[0].model))
      .map((c) => `${c.label}: ${c.routes[0]!.provider}/${c.routes[0]!.model}`),
    no_fallback: chains.filter((c) => c.routes.length < 2).map((c) => c.label),
    cloudflare: configured ? await cloudflare(env, nowMs) : null,
    analytics_configured: configured,
    paid: knob(env, 'AI_CLOUDFLARE_PAID') === 1,
    provider_rpm: knob(env, 'AI_PROVIDER_RPM'),
    provider_rpd: knob(env, 'AI_PROVIDER_RPD'),
  };
}

const GRAPHQL = 'https://api.cloudflare.com/client/v4/graphql';

/** One GraphQL query against the account; the `accounts[0]` object, or an error message. */
async function query(env: Env, body: string, variables: Record<string, string>): Promise<Record<string, any> | string> {
  try {
    const response = await providerFetch(GRAPHQL, {
      method: 'POST',
      headers: { authorization: `Bearer ${env.CF_ANALYTICS_TOKEN}`, 'content-type': 'application/json',
        'user-agent': 'plexora-licensing-capacity' },
      body: JSON.stringify({ query: body, variables: { account: env.CF_ACCOUNT_ID, ...variables } }),
      signal: AbortSignal.timeout(6000),
    });
    const json = await response.json<{ data?: any; errors?: Array<{ message?: string }> }>().catch(() => null);
    if (!response.ok || !json) return `HTTP ${response.status}`;
    if (json.errors?.length) return json.errors.map((e) => e.message ?? 'error').join('; ').slice(0, 300);
    const account = json.data?.viewer?.accounts?.[0];
    return account ?? 'no such account, or the token cannot read it';
  } catch (error) {
    return error instanceof Error ? error.message : 'unreachable';
  }
}

function sumByDay(groups: any[], field: (g: any) => number): DayCount[] {
  const days = new Map<string, number>();
  for (const g of groups ?? []) {
    const day = String(g?.dimensions?.date ?? '');
    if (day) days.set(day, (days.get(day) ?? 0) + (Number(field(g)) || 0));
  }
  return [...days].sort(([a], [b]) => a.localeCompare(b)).map(([day, value]) => ({ day, value }));
}

/** Account-wide Worker and D1 usage, last 30 days. Each dataset is its own query, so one failing keeps the rest. */
export async function cloudflare(env: Env, nowMs: number): Promise<Cloudflare> {
  const end = new Date(nowMs).toISOString().slice(0, 10);
  const start = new Date(nowMs - 29 * DAY * 1000).toISOString().slice(0, 10);
  const week = new Date(nowMs - 6 * DAY * 1000).toISOString().slice(0, 10);
  const vars = { start, end, week };
  const [workers, d1, storage] = await Promise.all([
    query(env, `query ($account: string, $start: Date, $end: Date) { viewer { accounts(filter: { accountTag: $account }) {
      workersInvocationsAdaptive(limit: 10000, filter: { date_geq: $start, date_leq: $end }) {
        sum { requests } quantiles { cpuTimeP99 } dimensions { date status scriptName } } } } }`, vars),
    query(env, `query ($account: string, $start: Date, $end: Date) { viewer { accounts(filter: { accountTag: $account }) {
      d1AnalyticsAdaptiveGroups(limit: 10000, filter: { date_geq: $start, date_leq: $end }) {
        sum { rowsRead rowsWritten } dimensions { date } } } } }`, vars),
    query(env, `query ($account: string, $week: Date, $end: Date) { viewer { accounts(filter: { accountTag: $account }) {
      d1StorageAdaptiveGroups(limit: 1000, filter: { date_geq: $week, date_leq: $end }) {
        max { databaseSizeBytes } dimensions { date databaseId } } } } }`, vars),
  ]);
  const errors: string[] = [];
  const out: Cloudflare = { worker_requests: [], d1_rows_written: [], d1_rows_read: [], exceeded: 0,
    cpu_p99_ms: null, db_bytes: null, errors };
  if (typeof workers === 'string') errors.push(`Workers analytics: ${workers}`);
  else {
    const groups: any[] = workers.workersInvocationsAdaptive ?? [];
    out.worker_requests = sumByDay(groups, (g) => g.sum?.requests);
    out.exceeded = groups.filter((g) => /exceeded/i.test(String(g.dimensions?.status ?? '')) &&
      String(g.dimensions?.date ?? '') >= week).reduce((s, g) => s + (Number(g.sum?.requests) || 0), 0);
    // cpuTimeP99 is in microseconds.
    let worst: any = null;
    for (const g of groups) {
      const v = Number(g.quantiles?.cpuTimeP99);
      if (String(g.dimensions?.date ?? '') >= week && Number.isFinite(v) && (!worst || v > Number(worst.quantiles.cpuTimeP99))) worst = g;
    }
    out.cpu_p99_ms = worst ? Math.round(Number(worst.quantiles.cpuTimeP99) / 100) / 10 : null;
    out.cpu_p99_at = worst ? { script: String(worst.dimensions?.scriptName ?? '?'), day: String(worst.dimensions?.date ?? '') }
      : null;
  }
  if (typeof d1 === 'string') errors.push(`D1 analytics: ${d1}`);
  else {
    const groups: any[] = d1.d1AnalyticsAdaptiveGroups ?? [];
    out.d1_rows_written = sumByDay(groups, (g) => g.sum?.rowsWritten);
    out.d1_rows_read = sumByDay(groups, (g) => g.sum?.rowsRead);
  }
  if (typeof storage === 'string') errors.push(`D1 storage: ${storage}`);
  else {
    const sizes = (storage.d1StorageAdaptiveGroups ?? []).map((g: any) => Number(g.max?.databaseSizeBytes))
      .filter((v: number) => Number.isFinite(v));
    out.db_bytes = sizes.length ? Math.max(...sizes) : null;
  }
  return out;
}
