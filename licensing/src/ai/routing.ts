/**
 * Which route serves a call: the published route table, the models it may
 * name, circuit breakers per provider and model, sticky routes per session,
 * and shadow candidates.
 *
 * Resolution is by (feature, capability): rows for the call's feature win,
 * then rows for '*', then the built-in default (catalog.ROUTES). Within a set,
 * rank 0 is tried first. A session sticks to the route that last served it,
 * so its prompt cache stays warm; only an open circuit (or a route published
 * with failover 'error') moves it.
 *
 * Circuits live in D1 (one small row per provider:model): a 30-second window
 * of outcomes; at AI_CIRCUIT_MIN_FAILURES failures and half or more of the
 * window failing it opens for AI_CIRCUIT_OPEN_S, after which one call probes
 * it. An admin can force a provider or one model open (the kill switch).
 */
import type { Env } from '../env';
import { knob } from '../env';
import { all, one } from '../db';
import { type Capability, COSTS, ROUTES, type Route, type UnitCosts } from './catalog';
import { admits, isProvider, type Provider, SPECS } from './providers';

export interface ModelCost extends UnitCosts {
  provider: Provider;
  model: string;
  fee_bps: number;
}

interface ModelRow {
  provider: string; model: string; in_micro: number; cache_read_micro: number; cache_write_5m_micro: number;
  cache_write_1h_micro: number; out_micro: number; fee_bps: number; enabled: number;
}

/** A catalogued, enabled model's unit costs: `ai_models`, else Anthropic's built-in list prices. */
export async function modelCost(env: Env, provider: Provider, model: string): Promise<ModelCost | null> {
  const row = await one<ModelRow>(env, 'SELECT * FROM ai_models WHERE provider = ?1 AND model = ?2', provider, model);
  if (row) {
    if (!row.enabled) return null;
    return { provider, model, in: row.in_micro, cache_read: row.cache_read_micro,
      cache_write_5m: row.cache_write_5m_micro, cache_write_1h: row.cache_write_1h_micro, out: row.out_micro,
      fee_bps: row.fee_bps };
  }
  if (provider === 'anthropic' && COSTS[model]) return { provider, model, ...COSTS[model]!, fee_bps: 0 };
  return null;
}

export interface RouteRow {
  id: string; feature: string; capability: string; role: 'serve' | 'shadow'; rank: number; provider: string;
  model: string; effort: Route['effort']; max_tokens_cap: number; failover: Route['failover'];
  evaluation_id: number | null; shadow_pct: number; enabled: number; note: string | null; updated_at: number;
  updated_by: string | null;
}

export function routeOf(row: RouteRow): Route {
  return { id: row.id, provider: row.provider as Provider, model: row.model, effort: row.effort,
    max_tokens_cap: row.max_tokens_cap, failover: row.failover };
}

/** The serving routes for a call, best first, before circuits and stickiness. */
export async function candidates(env: Env, feature: string | null, capability: Capability): Promise<Route[]> {
  const rows = await all<RouteRow>(env,
    `SELECT * FROM ai_routes WHERE capability = ?1 AND role = 'serve' AND enabled = 1 AND feature IN (?2, '*')
     ORDER BY rank`, capability, feature ?? '*');
  const specific = rows.filter((r) => r.feature !== '*');
  const chosen = specific.length ? specific : rows.filter((r) => r.feature === '*');
  const routes = chosen.filter((r) => isProvider(r.provider) && admits(r.provider as Provider, r.model)).map(routeOf);
  return routes.length ? routes : [ROUTES[capability]];
}

/** A stable bucket 0-99 for a session, so a whole session is shadowed or none of it. */
export function bucketOf(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619) >>> 0;
  return h % 100;
}

/** The shadow candidate for this session, if one is sampling it. */
export async function shadowFor(env: Env, feature: string | null, capability: Capability,
  sessionKey: string | null): Promise<Route | null> {
  if (!sessionKey) return null;
  const rows = await all<RouteRow>(env,
    `SELECT * FROM ai_routes WHERE capability = ?1 AND role = 'shadow' AND enabled = 1 AND shadow_pct > 0
       AND feature IN (?2, '*')
     ORDER BY CASE WHEN feature = '*' THEN 1 ELSE 0 END, rank`, capability, feature ?? '*');
  const bucket = bucketOf(sessionKey);
  const row = rows.find((r) => isProvider(r.provider) && admits(r.provider as Provider, r.model) &&
    bucket < r.shadow_pct);
  return row ? routeOf(row) : null;
}

// -- stickiness ------------------------------------------------------------------------

export async function stickyRouteId(env: Env, accountId: string, sessionId: string | null): Promise<string | null> {
  if (!sessionId) return null;
  const row = await one<{ route_id: string }>(env,
    'SELECT route_id FROM ai_sticky WHERE account_id = ?1 AND session_id = ?2', accountId, sessionId);
  return row?.route_id ?? null;
}

export async function stick(env: Env, accountId: string, sessionId: string | null, routeId: string,
  now: number): Promise<void> {
  if (!sessionId) return;
  await env.LICENSE_DB.prepare(
    `INSERT INTO ai_sticky (account_id, session_id, route_id, at) VALUES (?1, ?2, ?3, ?4)
     ON CONFLICT(account_id, session_id) DO UPDATE SET route_id = ?3, at = ?4`,
  ).bind(accountId, sessionId, routeId, now).run();
}

export function preferSticky(routes: Route[], stickyId: string | null): Route[] {
  if (!stickyId) return routes;
  const index = routes.findIndex((r) => r.id === stickyId);
  if (index <= 0) return routes;
  return [routes[index]!, ...routes.slice(0, index), ...routes.slice(index + 1)];
}

// -- circuits ---------------------------------------------------------------------------

export interface CircuitRow {
  route_key: string; window_start: number; ok: number; fail: number; open_until: number; forced: string | null;
  reason: string | null; updated_at: number;
}

export const circuitKey = (route: Pick<Route, 'provider' | 'model'>) => `${route.provider}:${route.model}`;

export interface CircuitState {
  open: boolean;
  forced: boolean;
  row: CircuitRow | null;
}

export async function circuit(env: Env, route: Route, now: number): Promise<CircuitState> {
  const rows = await all<CircuitRow>(env, 'SELECT * FROM ai_circuits WHERE route_key IN (?1, ?2)',
    route.provider, circuitKey(route));
  const forced = rows.some((r) => r.forced === 'open');
  const row = rows.find((r) => r.route_key === circuitKey(route)) ?? null;
  return { open: forced || (row !== null && row.open_until > now), forced, row };
}

/**
 * Count one outcome. A success writes only when the window holds failures (so
 * a healthy route costs no write per call); a failure may open the circuit.
 */
export async function recordOutcome(env: Env, route: Route, success: boolean, now: number,
  state: CircuitState | null): Promise<boolean> {
  const window = knob(env, 'AI_CIRCUIT_WINDOW_S');
  if (success && (!state?.row || state.row.fail === 0 || now - state.row.window_start > window)) return false;
  const key = circuitKey(route);
  const minFailures = knob(env, 'AI_CIRCUIT_MIN_FAILURES');
  const openFor = knob(env, 'AI_CIRCUIT_OPEN_S');
  const row = await one<CircuitRow>(env,
    `INSERT INTO ai_circuits (route_key, window_start, ok, fail, open_until, updated_at)
       VALUES (?1, ?2, ?3, ?4, 0, ?2)
     ON CONFLICT(route_key) DO UPDATE SET
       ok = CASE WHEN ?2 - window_start > ?5 THEN ?3 ELSE ok + ?3 END,
       fail = CASE WHEN ?2 - window_start > ?5 THEN ?4 ELSE fail + ?4 END,
       window_start = CASE WHEN ?2 - window_start > ?5 THEN ?2 ELSE window_start END,
       updated_at = ?2
     RETURNING *`, key, now, success ? 1 : 0, success ? 0 : 1, window);
  if (!row || success) return false;
  if (row.fail >= minFailures && row.fail * 2 >= row.ok + row.fail && row.open_until <= now) {
    await env.LICENSE_DB.prepare('UPDATE ai_circuits SET open_until = ?2 WHERE route_key = ?1')
      .bind(key, now + openFor).run();
    return true;
  }
  return row.open_until > now;
}

/** Kill switch: force a provider (`openai`) or one model (`openai:gpt-…`) open, or clear it. */
export async function force(env: Env, key: string, open: boolean, reason: string | null, now: number) {
  if (open) {
    await env.LICENSE_DB.prepare(
      `INSERT INTO ai_circuits (route_key, window_start, ok, fail, open_until, forced, reason, updated_at)
         VALUES (?1, ?2, 0, 0, 0, 'open', ?3, ?2)
       ON CONFLICT(route_key) DO UPDATE SET forced = 'open', reason = ?3, updated_at = ?2`,
    ).bind(key, now, reason).run();
  } else {
    await env.LICENSE_DB.prepare(
      `UPDATE ai_circuits SET forced = NULL, reason = NULL, fail = 0, ok = 0, open_until = 0, window_start = ?2,
         updated_at = ?2 WHERE route_key = ?1`,
    ).bind(key, now).run();
  }
}

/** Whether a route may be served without a routing-bench evaluation. */
export function needsEvaluation(route: { provider: Provider; feature: string; role: string }): boolean {
  return route.role === 'serve' && !(SPECS[route.provider].direct && route.feature === '*');
}

// -- shadow agreement -------------------------------------------------------------------

/** The fields that carry a decision in Plexora's answer schemas; prose fields never count. */
const DECISION_KEYS = ['kind', 'verdict', 'verdicts', 'direction', 'within', 'chosen_candidate', 'intervals',
  'per_image', 'features_layer', 'features_log', 'basis'];

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical((value as any)[k])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function parse(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

/** 1 if the shadow's answer makes the same decision as the served one, 0 if not, null if not comparable. */
export function agreement(served: string, shadow: string): number | null {
  const a = parse(served);
  const b = parse(shadow);
  if (!a || !b) return null;
  const keys = DECISION_KEYS.filter((k) => k in a || k in b);
  if (!keys.length) return canonical(a) === canonical(b) ? 1 : 0;
  return keys.every((k) => canonical(a[k]) === canonical(b[k])) ? 1 : 0;
}
