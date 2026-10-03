/**
 * Which route serves a call: approved models, their provider routes, task
 * assignments, circuit breakers per provider and model, sticky routes per
 * session, and shadow candidates.
 *
 * Resolution is by task. An assignment (ai_task_routes) names approved models
 * in order for a task, a module (`gating.*`) or every task (`*`); the most
 * specific level with an enabled, able model wins (tasks.levelsFor). Each
 * model contributes its provider routes in their rank order, so a call tries
 * every provider of the primary model before the fallback model: a provider
 * switch keeps the same model, and so the same answers. With no assignment at
 * any level the built-in default serves (catalog.ROUTES); while no assignment
 * exists at all, the v3 route table does (routing_legacy.ts).
 *
 * A session sticks to the route that last served it, so its prompt cache stays
 * warm; only a failure (per the route's `failover`) moves it.
 *
 * Circuits live in D1 (one small row per provider:model): a 30-second window
 * of outcomes; at AI_CIRCUIT_MIN_FAILURES failures and half or more of the
 * window failing it opens for AI_CIRCUIT_OPEN_S, after which one call probes
 * it. An admin can force a provider or one model open (the kill switch).
 */
import type { Env } from '../env';
import { knob } from '../env';
import { all, one } from '../db';
import { type Capability, COSTS, type Level, ROUTES, type Route, type UnitCosts } from './catalog';
import { admits, isProvider, type Provider, SPECS } from './providers';
import { legacyCandidates, legacyModelCost, legacyShadowFor } from './routing_legacy';
import { levelOf, levelsFor, type Requirements, TASKS } from './tasks';

export interface ModelCost extends UnitCosts {
  provider: Provider;
  model: string;
  fee_bps: number;
  structured: boolean;
  tools: boolean;
  vision: boolean;
}

// -- the catalogue ------------------------------------------------------------------------

export interface CatalogRow {
  id: string; name: string; family: string | null; context_window: number | null; max_output: number | null;
  supports_vision: number; supports_tools: number; supports_structured: number; reasoning: number;
  status: 'active' | 'preview' | 'deprecated'; enabled: number; note: string | null; updated_at: number;
  updated_by: string | null;
}

export interface CatalogRouteRow {
  model_id: string; provider: string; provider_model: string; rank: number; enabled: number;
  failover: Route['failover']; in_micro: number; cache_read_micro: number; cache_write_5m_micro: number;
  cache_write_1h_micro: number; out_micro: number; fee_bps: number; extra_json: string | null;
  availability: 'ok' | 'degraded' | 'down' | 'unknown'; rate_limit_json: string | null;
  latency_p50_ms: number | null; price_source: 'api' | 'builtin' | 'manual'; priced_at: number | null;
  source_url: string | null; note: string | null; updated_at: number; updated_by: string | null;
}

export interface TaskRouteRow {
  id: string; task: string; role: 'serve' | 'shadow'; rank: number; model_id: string; effort: Route['effort'];
  max_tokens_cap: number | null; requires_vision: number | null; requires_reasoning: number | null;
  max_cost_micro: number | null; latency_ms: number | null; shadow_pct: number; unbenched: number;
  evaluation_id: number | null; enabled: number; note: string | null; updated_at: number;
  updated_by: string | null;
}

export const routeIdOf = (modelId: string, provider: string) => `${modelId}@${provider}`;

export function costOf(model: Pick<CatalogRow, 'supports_vision' | 'supports_tools' | 'supports_structured'>,
  r: CatalogRouteRow): ModelCost {
  return { provider: r.provider as Provider, model: r.provider_model, in: r.in_micro, cache_read: r.cache_read_micro,
    cache_write_5m: r.cache_write_5m_micro, cache_write_1h: r.cache_write_1h_micro, out: r.out_micro,
    fee_bps: r.fee_bps, structured: !!model.supports_structured, tools: !!model.supports_tools,
    vision: !!model.supports_vision };
}

/** Approved models and their routes, by id (one query each). */
export async function catalogFor(env: Env, ids: string[]): Promise<Map<string, { model: CatalogRow;
  routes: CatalogRouteRow[] }>> {
  const out = new Map<string, { model: CatalogRow; routes: CatalogRouteRow[] }>();
  if (!ids.length) return out;
  const marks = ids.map((_, i) => `?${i + 1}`).join(', ');
  const [models, routes] = await Promise.all([
    all<CatalogRow>(env, `SELECT * FROM ai_catalog WHERE id IN (${marks})`, ...ids),
    all<CatalogRouteRow>(env, `SELECT * FROM ai_catalog_routes WHERE model_id IN (${marks}) ORDER BY rank`, ...ids),
  ]);
  for (const model of models) out.set(model.id, { model, routes: routes.filter((r) => r.model_id === model.id) });
  return out;
}

/** Why an approved model cannot serve a task's requirements, or null when it can. */
export function lacks(model: Pick<CatalogRow, 'supports_vision' | 'reasoning'>, reqs: Requirements): string | null {
  if (reqs.vision && !model.supports_vision) return 'no vision';
  if (reqs.reasoning && !model.reasoning) return 'no reasoning';
  return null;
}

// -- resolution --------------------------------------------------------------------------

export interface Resolution {
  routes: Route[];
  costs: Map<string, ModelCost>;
  level: Level;
  /** The assignment pattern that served (`gating.*`), or null for the built-in and legacy levels. */
  pattern: string | null;
  /** The task-level constraints of that assignment. */
  max_cost_micro: number | null;
  latency_ms: number | null;
  /** Models an assignment named that were passed over, and why (the admin's "effective" view). */
  skipped: Array<{ pattern: string; model_id: string; reason: string }>;
  /** True while the v3 route table serves (no task assignment exists yet). */
  legacy: boolean;
}

export interface ResolveQuery {
  task: string | null;
  feature: string | null;
  capability: Capability;
}

/** Whether any task assignment exists: until one does, the v3 route table serves. */
export async function hasTaskRouting(env: Env): Promise<boolean> {
  const row = await one<{ n: number }>(env, `SELECT 1 AS n FROM ai_task_routes WHERE role = 'serve' LIMIT 1`)
    .catch(() => null);
  return row !== null;
}

/** The built-in route for a call: its task's capability class when the registry knows the task. */
function builtinRoute(q: ResolveQuery): Route {
  const spec = q.task ? TASKS[q.task] : undefined;
  return ROUTES[spec?.capability ?? q.capability];
}

function builtinCost(route: Route): ModelCost {
  return { provider: route.provider, model: route.model, ...COSTS[route.model]!, fee_bps: 0, structured: true,
    tools: true, vision: true };
}

/**
 * The routes that may serve a call, best first, with the unit costs of each,
 * before circuits, stickiness and the request's own needs (images, tools, a
 * cost cap) are applied.
 */
export async function resolve(env: Env, q: ResolveQuery): Promise<Resolution> {
  const empty = { max_cost_micro: null, latency_ms: null, skipped: [], legacy: true };
  const patterns = levelsFor(q.task, q.feature);
  const marks = patterns.map((_, i) => `?${i + 1}`).join(', ');
  const rows = await all<TaskRouteRow>(env,
    `SELECT * FROM ai_task_routes WHERE role = 'serve' AND enabled = 1 AND task IN (${marks}) ORDER BY rank`,
    ...patterns).catch(() => [] as TaskRouteRow[]);
  if (!rows.length && !(await hasTaskRouting(env))) {
    const routes = await legacyCandidates(env, q.feature, q.capability);
    const costs = new Map<string, ModelCost>();
    for (const r of routes) {
      const cost = await legacyModelCost(env, r.provider, r.model);
      if (cost) costs.set(r.id, cost);
    }
    return { routes, costs, level: routes[0]?.level ?? 'builtin', pattern: null, ...empty };
  }
  const catalog = await catalogFor(env, [...new Set(rows.map((r) => r.model_id))]);
  const skipped: Resolution['skipped'] = [];
  for (const pattern of patterns) {
    const chain = rows.filter((r) => r.task === pattern);
    if (!chain.length) continue;
    const head = chain[0]!;
    const routes: Route[] = [];
    const costs = new Map<string, ModelCost>();
    // A task's requirements are checked when a model is assigned to it (and flagged on the admin pages if
    // the model changes later), never here: a call is not moved to another model behind the admin's back.
    // What this request itself needs (images, tools) is checked per call by `unsuitable`.
    for (const assignment of chain) {
      const entry = catalog.get(assignment.model_id);
      const why = !entry ? 'not in the catalogue' : !entry.model.enabled ? 'disabled' : null;
      if (why) {
        skipped.push({ pattern, model_id: assignment.model_id, reason: why });
        continue;
      }
      const { model } = entry!;
      const cap = Math.min(assignment.max_tokens_cap ?? TASKS[q.task ?? '']?.max_tokens ??
        ROUTES[q.capability].max_tokens_cap, model.max_output ?? Number.MAX_SAFE_INTEGER);
      const group = entry!.routes.filter((r) => r.enabled && isProvider(r.provider) &&
        admits(r.provider as Provider, r.provider_model));
      if (!group.length) {
        skipped.push({ pattern, model_id: model.id, reason: 'no provider route on' });
        continue;
      }
      // A soft latency preference: routes known to be slower than the task asks go behind the rest of
      // their model's routes, never out.
      const slow = (r: CatalogRouteRow) => head.latency_ms !== null && r.latency_p50_ms !== null &&
        r.latency_p50_ms > head.latency_ms ? 1 : 0;
      for (const r of [...group].sort((a, b) => slow(a) - slow(b) || a.rank - b.rank)) {
        const route: Route = { id: routeIdOf(model.id, r.provider), provider: r.provider as Provider,
          model: r.provider_model, model_id: model.id, level: levelOf(pattern), effort: assignment.effort,
          max_tokens_cap: cap, failover: r.failover };
        routes.push(route);
        costs.set(route.id, costOf(model, r));
      }
    }
    if (routes.length) {
      return { routes, costs, level: levelOf(pattern), pattern, max_cost_micro: head.max_cost_micro,
        latency_ms: head.latency_ms, skipped, legacy: false };
    }
  }
  const route = builtinRoute(q);
  return { routes: [route], costs: new Map([[route.id, builtinCost(route)]]), level: 'builtin', pattern: null,
    max_cost_micro: null, latency_ms: null, skipped, legacy: false };
}

/** A route (and its cost) the dev route names: `provider/model` on the wire, or a catalogue id. */
export async function namedRoute(env: Env, named: string): Promise<{ route: Route; cost: ModelCost } | null> {
  const slash = named.indexOf('/');
  const head = slash > 0 ? named.slice(0, slash) : '';
  if (isProvider(head)) {
    const wire = named.slice(slash + 1);
    if (!admits(head, wire)) return null;
    const row = await one<CatalogRouteRow & { m_vision: number; m_tools: number; m_structured: number }>(env,
      `SELECT r.*, m.supports_vision AS m_vision, m.supports_tools AS m_tools, m.supports_structured AS m_structured
       FROM ai_catalog_routes r JOIN ai_catalog m ON m.id = r.model_id
       WHERE r.provider = ?1 AND r.provider_model = ?2 AND m.enabled = 1 LIMIT 1`, head, wire).catch(() => null);
    const cost = row ? costOf({ supports_vision: row.m_vision, supports_tools: row.m_tools,
      supports_structured: row.m_structured }, row) : await legacyModelCost(env, head, wire);
    if (!cost) return null;
    return { route: { id: `dev:${head}/${wire}`, provider: head, model: wire, model_id: row?.model_id ?? wire,
      level: 'dev', effort: null, max_tokens_cap: 0, failover: 'never' }, cost };
  }
  const entry = (await catalogFor(env, [named]).catch(() => new Map())).get(named);
  const primary = entry?.model.enabled ? entry.routes.find((r: CatalogRouteRow) => r.enabled &&
    isProvider(r.provider) && admits(r.provider as Provider, r.provider_model)) : undefined;
  if (primary) {
    return { route: { id: `dev:${primary.provider}/${primary.provider_model}`, provider: primary.provider as Provider,
      model: primary.provider_model, model_id: named, level: 'dev', effort: null, max_tokens_cap: 0,
      failover: 'never' }, cost: costOf(entry!.model, primary) };
  }
  const cost = await legacyModelCost(env, 'anthropic', named);
  if (!cost) return null;
  return { route: { id: `dev:anthropic/${named}`, provider: 'anthropic', model: named, model_id: named, level: 'dev',
    effort: null, max_tokens_cap: 0, failover: 'never' }, cost };
}

/** A stable bucket 0-99 for a session, so a whole session is shadowed or none of it. */
export function bucketOf(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619) >>> 0;
  return h % 100;
}

/**
 * The shadow candidate for this session, with its cost, if one is sampling it:
 * a task assignment's shadow, else (while the v3 table serves) a legacy one.
 */
export async function shadowFor(env: Env, q: ResolveQuery, sessionKey: string | null,
  legacy: boolean): Promise<{ route: Route; cost: ModelCost } | null> {
  if (!sessionKey) return null;
  const found = await taskShadow(env, q, sessionKey);
  if (found || !legacy) return found;
  const route = await legacyShadowFor(env, q.feature, q.capability, sessionKey);
  const cost = route ? await legacyModelCost(env, route.provider, route.model) : null;
  return route && cost ? { route, cost } : null;
}

async function taskShadow(env: Env, q: ResolveQuery, sessionKey: string): Promise<{ route: Route; cost: ModelCost } | null> {
  const patterns = levelsFor(q.task, q.feature);
  const marks = patterns.map((_, i) => `?${i + 1}`).join(', ');
  const rows = await all<TaskRouteRow>(env,
    `SELECT * FROM ai_task_routes WHERE role = 'shadow' AND enabled = 1 AND shadow_pct > 0 AND task IN (${marks})
     ORDER BY rank`, ...patterns).catch(() => [] as TaskRouteRow[]);
  const bucket = bucketOf(sessionKey);
  const ordered = patterns.flatMap((p) => rows.filter((r) => r.task === p && bucket < r.shadow_pct));
  if (!ordered.length) return null;
  const catalog = await catalogFor(env, [...new Set(ordered.map((r) => r.model_id))]);
  for (const row of ordered) {
    const entry = catalog.get(row.model_id);
    if (!entry?.model.enabled) continue;
    const primary = entry.routes.find((r) => r.enabled && isProvider(r.provider) &&
      admits(r.provider as Provider, r.provider_model));
    if (!primary) continue;
    const route: Route = { id: routeIdOf(entry.model.id, primary.provider), provider: primary.provider as Provider,
      model: primary.provider_model, model_id: entry.model.id, level: levelOf(row.task), effort: row.effort,
      max_tokens_cap: row.max_tokens_cap ?? TASKS[q.task ?? '']?.max_tokens ?? ROUTES[q.capability].max_tokens_cap,
      failover: 'never' };
    return { route, cost: costOf(entry.model, primary) };
  }
  return null;
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

/** A pinned backend goes stale with the provider's cache; after this, let the aggregator choose again. */
export const UPSTREAM_PIN_SECONDS = 30 * 60;

/** The backend that last served this session on this route, while its cache may still be warm. */
export async function stickyUpstream(env: Env, accountId: string, sessionId: string | null, routeId: string,
  now: number): Promise<string | null> {
  if (!sessionId) return null;
  try {
    const row = await one<{ upstream: string; at: number }>(env,
      'SELECT upstream, at FROM ai_sticky_upstream WHERE account_id = ?1 AND session_id = ?2 AND route_id = ?3',
      accountId, sessionId, routeId);
    return row && now - row.at < UPSTREAM_PIN_SECONDS ? row.upstream : null;
  } catch {
    return null;   // a database without the table yet: no pin, the call goes ahead
  }
}

export async function stickUpstream(env: Env, accountId: string, sessionId: string | null, routeId: string,
  upstream: string, now: number): Promise<void> {
  if (!sessionId) return;
  try {
    await env.LICENSE_DB.prepare(
      `INSERT INTO ai_sticky_upstream (account_id, session_id, route_id, upstream, at) VALUES (?1, ?2, ?3, ?4, ?5)
       ON CONFLICT(account_id, session_id, route_id) DO UPDATE SET upstream = ?4, at = ?5`,
    ).bind(accountId, sessionId, routeId, upstream.slice(0, 128), now).run();
  } catch {
    // as above
  }
}

/**
 * The session's last route first, and the rest of its model's routes right
 * after it: a session that failed over to a fallback provider stays on that
 * model, and only then tries the others.
 */
export function preferSticky(routes: Route[], stickyId: string | null): Route[] {
  if (!stickyId) return routes;
  const sticky = routes.find((r) => r.id === stickyId);
  if (!sticky || routes[0] === sticky) return routes;
  const group = routes.filter((r) => r !== sticky && r.model_id === sticky.model_id);
  return [sticky, ...group, ...routes.filter((r) => r !== sticky && r.model_id !== sticky.model_id)];
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

/** Why this model cannot serve this request, or null when it can. */
export function unsuitable(cost: ModelCost, need: { images: number; tools: boolean }): string | null {
  if (need.images > 0 && !cost.vision) return 'no_vision';
  if (need.tools && !cost.tools) return 'no_tools';
  return null;
}

/**
 * Whether serving a model for a task needs a routing-bench evaluation: any
 * serve assignment whose model's primary route is not a direct provider's.
 * The global default (`*`) served by a direct provider is exempt, as before.
 */
export function needsEvaluation(a: { provider: Provider | null; pattern: string; role: string }): boolean {
  if (a.role !== 'serve') return false;
  return !(a.provider && SPECS[a.provider].direct && a.pattern === '*');
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
