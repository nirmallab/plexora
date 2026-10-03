/**
 * What the admin sees of Plexora AI, computed once for both the JSON API
 * (routes/aiAdminCatalog.ts) and the pages (routes/adminAi/): the catalogue
 * with each route's live state, every task's effective chain (from the same
 * `resolve()` a call uses, never a re-implementation), pricing status, usage,
 * each provider's connection, the problems worth an admin's attention (each
 * with a stable key, so it can be dismissed until it changes), and the
 * one-line summary the admin's step strip and Overview read.
 */
import { all, one } from '../db';
import { DAY, type Env, knob } from '../env';
import { assess, type Assessment, chainsOf, gather } from './capacity';
import { BENCH, type Capability, type Level } from './catalog';
import { isUnpriced } from './catalog_store';
import { describeResolved, type EffortProfile, type EffortSpec, profileFor, type ProfileSource,
  resolveEffort } from './effort';
import { keysView, type KeyView } from './keys';
import { canList, isStale, listsPrices } from './pricing';
import { admits, configured, isProvider, LABELS, type Provider, PROVIDERS, SPECS } from './providers';
import {
  type CatalogRouteRow, type CatalogRow, hasTaskRouting, lacks, resolve, specOf, type TaskRouteRow, taskEffort,
} from './routing';
import { legacyRows } from './routing_legacy';
import { baseEnv } from './settings';
import { levelOf, MODULES, moduleOf, parentOf, patternLabel, type Requirements, requirementsFor, TASKS } from './tasks';

export interface CircuitRow { route_key: string; forced: string | null; reason: string | null; open_until: number;
  fail: number; ok: number; updated_at: number }

export interface ProviderStatusRow { provider: string; checked_at: number; ok: number; error: string | null;
  balance_json: string | null; rate_limit_json: string | null; models_seen: number; routes_updated: number;
  changes: number }

export interface RouteView extends CatalogRouteRow {
  configured: boolean;
  admitted: boolean;
  circuit: 'closed' | 'open' | 'forced';
  stale: boolean;
  /** Added without a price: off until it has one (catalog_store.isUnpriced). */
  unpriced: boolean;
  extra: Record<string, unknown> | null;
}

export interface EffortView extends EffortProfile {
  source: ProfileSource;
  /** The built-in profile that matches the model, whether or not it is the one in use. */
  builtin: { label: string; verified: string; source: string } | null;
}

export interface ModelView extends CatalogRow {
  routes: RouteView[];
  used_by: string[];
  effort: EffortView;
}

export function effortView(model: Pick<CatalogRow, 'id' | 'effort_json' | 'reasoning'>,
  providerModels: string[]): EffortView {
  const { profile, source, builtin } = profileFor(model, ...providerModels);
  return { levels: profile.levels, default: profile.default, wire: profile.wire,
    ...(profile.budgets ? { budgets: profile.budgets } : {}), source,
    builtin: builtin ? { label: builtin.label, verified: builtin.verified, source: builtin.source } : null };
}

export interface ProviderView {
  provider: Provider;
  wire: string;
  direct: boolean;
  configured: boolean;
  /** Its own list carries prices (the aggregators). */
  lists_prices: boolean;
  /** Its model list can be read now (a direct provider's needs its key). */
  can_list: boolean;
  forced: { reason: string | null; at: number } | null;
  status: ProviderStatusRow | null;
  routes: number;
}

const parse = (text: string | null): Record<string, unknown> | null => {
  if (!text) return null;
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
};

export async function circuits(env: Env): Promise<CircuitRow[]> {
  return all<CircuitRow>(env, 'SELECT * FROM ai_circuits ORDER BY route_key');
}

function circuitOf(rows: CircuitRow[], provider: string, wire: string, now: number): RouteView['circuit'] {
  const own = rows.find((r) => r.route_key === `${provider}:${wire}`);
  if (rows.some((r) => (r.route_key === provider || r.route_key === `${provider}:${wire}`) && r.forced)) return 'forced';
  return own && own.open_until > now ? 'open' : 'closed';
}

export async function catalogView(env: Env, now: number) {
  const [models, routes, assigned, status, open] = await Promise.all([
    all<CatalogRow>(env, 'SELECT * FROM ai_catalog ORDER BY enabled DESC, name'),
    all<CatalogRouteRow>(env, 'SELECT * FROM ai_catalog_routes ORDER BY model_id, rank'),
    all<{ task: string; model_id: string }>(env, 'SELECT DISTINCT task, model_id FROM ai_task_routes ORDER BY task'),
    all<ProviderStatusRow>(env, 'SELECT * FROM ai_provider_status'),
    circuits(env),
  ]);
  const view: ModelView[] = models.map((m) => ({
    ...m,
    routes: routes.filter((r) => r.model_id === m.id).map((r) => ({
      ...r,
      configured: isProvider(r.provider) && configured(env, r.provider),
      admitted: isProvider(r.provider) && admits(r.provider, r.provider_model),
      circuit: circuitOf(open, r.provider, r.provider_model, now),
      stale: isStale(env, r, now),
      unpriced: isUnpriced(r),
      extra: parse(r.extra_json),
    })),
    used_by: assigned.filter((a) => a.model_id === m.id).map((a) => a.task),
    effort: effortView(m, routes.filter((r) => r.model_id === m.id).map((r) => r.provider_model)),
  }));
  const providers: ProviderView[] = PROVIDERS.map((p) => {
    const forced = open.find((x) => x.route_key === p && x.forced);
    return { provider: p, wire: SPECS[p].wire, direct: SPECS[p].direct, configured: configured(env, p),
      lists_prices: listsPrices(p), can_list: canList(env, p), forced: forced ? { reason: forced.reason, at: forced.updated_at } : null,
      status: status.find((s) => s.provider === p) ?? null, routes: routes.filter((r) => r.provider === p).length };
  });
  const legacy = await legacyRows(env);
  const taskRouting = await hasTaskRouting(env);
  return { models: view, providers, circuits: open.filter((x) => x.forced || x.open_until > now),
    stale_after_h: knob(env, 'AI_PRICE_STALE_HOURS'),
    legacy: { ...legacy, serving: !taskRouting && legacy.routes > 0 }, task_routing: taskRouting };
}

// -- tasks -------------------------------------------------------------------------------

export interface ChainLink {
  model_id: string;
  name: string;
  routes: Array<{ id: string; provider: string; model: string; configured: boolean }>;
  /** What this model is sent for the serving assignment's effort: "high", "xhigh → high", "not sent", ... */
  effort: string;
  /** Effort was asked for but nothing describes how this model takes it. */
  effort_unknown: boolean;
}

export interface Effective {
  level: Level;
  /** The assignment that serves: the row's own pattern, or the one it inherits. */
  source: string | null;
  chain: ChainLink[];
  skipped: Array<{ pattern: string; model_id: string; reason: string }>;
}

export interface TaskRowView {
  pattern: string;
  label: string;
  blurb: string;
  level: 'task' | 'module' | 'global';
  parent: string | null;
  requires: Requirements & { overridden: boolean };
  serve: TaskRouteRow[];
  shadow: TaskRouteRow[];
  effective: Effective;
  /** Models serving this row that lack what it needs (abilities changed after they were assigned). */
  mismatch: Array<{ model_id: string; name: string; reason: string }>;
}

async function effectiveOf(env: Env, pattern: string, models: Map<string, CatalogRow>,
  specs: Map<string, EffortSpec | null>): Promise<Effective> {
  const level = levelOf(pattern);
  const task = level === 'task' ? pattern : null;
  const feature = level === 'module' ? moduleOf(pattern) : level === 'task' ? moduleOf(pattern) : null;
  const capability: Capability = task ? TASKS[task]?.capability ?? 'vision_judgement' : 'vision_judgement';
  const r = await resolve(env, { task, feature, capability });
  const spec = r.pattern ? specs.get(r.pattern) ?? null : null;
  const chain: ChainLink[] = [];
  for (const route of r.routes) {
    let link = chain.find((l) => l.model_id === route.model_id);
    if (!link) {
      const model = models.get(route.model_id);
      const { profile, source } = profileFor(model ?? null, route.model);
      const resolved = r.pattern ? resolveEffort(spec, taskEffort(task), profile) : null;
      link = { model_id: route.model_id, name: model?.name ?? route.model_id, routes: [],
        effort: resolved ? describeResolved(resolved, profile.budgets) : route.effort ?? 'model default',
        effort_unknown: !!resolved && resolved.asked !== null && source === 'unknown' };
      chain.push(link);
    }
    link.routes.push({ id: route.id, provider: route.provider, model: route.model,
      configured: configured(env, route.provider) });
  }
  return { level: r.level, source: r.pattern, chain, skipped: r.skipped };
}

/** Every assignable row: `*`, each module's default, each registry task, in registry order. */
export function patterns(): string[] {
  return ['*', ...MODULES.flatMap((m) => [`${m.id}.*`, ...m.tasks.map((t) => t.id)])];
}

export async function tasksView(env: Env) {
  const [assignments, models] = await Promise.all([
    all<TaskRouteRow>(env, 'SELECT * FROM ai_task_routes ORDER BY task, role, rank'),
    all<CatalogRow>(env, 'SELECT * FROM ai_catalog ORDER BY name'),
  ]);
  const byModel = new Map(models.map((m) => [m.id, m]));
  const specs = new Map<string, EffortSpec | null>();
  for (const a of assignments) if (a.role === 'serve' && a.rank === 0) specs.set(a.task, specOf(a));
  const known = new Set(patterns());
  // Assignments to a task the registry no longer lists are still shown, so they can be removed.
  const extra = [...new Set(assignments.map((a) => a.task))].filter((t) => !known.has(t));
  const rows: TaskRowView[] = [];
  for (const pattern of [...patterns(), ...extra]) {
    const level = levelOf(pattern);
    const spec = level === 'task' ? TASKS[pattern] : undefined;
    const serve = assignments.filter((a) => a.task === pattern && a.role === 'serve');
    const shadow = assignments.filter((a) => a.task === pattern && a.role === 'shadow');
    const base = spec ? { ...spec.requires } : { vision: false, reasoning: false };
    const head = serve[0];
    const requires = { vision: head?.requires_vision !== null && head?.requires_vision !== undefined
      ? !!head.requires_vision : base.vision,
    reasoning: head?.requires_reasoning !== null && head?.requires_reasoning !== undefined
      ? !!head.requires_reasoning : base.reasoning,
    overridden: !!head && (head.requires_vision !== null || head.requires_reasoning !== null) };
    const effective = await effectiveOf(env, pattern, byModel, specs);
    const byId = new Map(models.map((m) => [m.id, m]));
    const mismatch = effective.chain.flatMap((link) => {
      const model = byId.get(link.model_id);
      const reason = !model ? null : level === 'task' ? lacks(model, requires) : eligibility(model, pattern);
      return reason ? [{ model_id: link.model_id, name: link.name, reason }] : [];
    });
    rows.push({ pattern, label: spec?.label ?? patternLabel(pattern), blurb: spec?.blurb ?? '', level,
      parent: parentOf(pattern), requires, serve, shadow, effective, mismatch });
  }
  return { modules: MODULES, rows, models, bench: BENCH, unbenched_allowed: knob(env, 'AI_ALLOW_UNBENCHED_ROUTES') === 1,
    task_routing: await hasTaskRouting(env) };
}

/** Which models may be offered for a pattern, and why the others may not. */
export function eligibility(model: CatalogRow, pattern: string): string | null {
  if (!model.enabled) return 'disabled';
  const level = levelOf(pattern);
  if (level === 'task') return lacks(model, requirementsFor(pattern, null));
  // A module or global default must suit at least one task it covers.
  const scope = level === 'global' ? Object.values(TASKS) : Object.values(TASKS).filter((t) => t.module ===
    moduleOf(pattern));
  if (!scope.length) return null;
  const reasons = scope.map((t) => lacks(model, t.requires));
  return reasons.every((r) => r !== null) ? reasons[0]! : null;
}

// -- pricing ---------------------------------------------------------------------------

export async function pricingStatus(env: Env, now: number) {
  const view = await catalogView(env, now);
  const stale = view.models.flatMap((m) => m.routes.filter((r) => r.stale).map((r) => ({ model_id: m.id, name: m.name,
    provider: r.provider, provider_model: r.provider_model, priced_at: r.priced_at })));
  return { providers: view.providers, stale, stale_after_h: view.stale_after_h,
    refresh_on: knob(env, 'AI_PRICE_REFRESH') === 1 };
}

export async function priceHistory(env: Env, filter: { model_id?: string | null; provider?: string | null; days: number },
  now: number) {
  const rows = await all<{ id: number; at: number; actor: string; payload: string | null }>(env,
    `SELECT id, at, actor, payload FROM events WHERE kind = 'ai.price.changed' AND at >= ?1 ORDER BY at DESC LIMIT 500`,
    now - filter.days * DAY);
  return rows.map((r) => ({ id: r.id, at: r.at, actor: r.actor, ...(parse(r.payload) ?? {}) }))
    .filter((r: Record<string, unknown>) => (!filter.model_id || r.model_id === filter.model_id) &&
      (!filter.provider || r.provider === filter.provider));
}

// -- usage -----------------------------------------------------------------------------

export async function usageView(env: Env, days: number, now: number) {
  const since = (now - days * DAY) * 1000;
  const [totals, byDay, byTask, byModel, byProvider, accounts, recent] = await Promise.all([
    one<Record<string, number>>(env,
      `SELECT COUNT(*) AS calls, SUM(status != 'ok') AS failed, SUM(cost_micro) AS cost_micro,
         SUM(charged_micro) AS charged_micro, SUM(cache_read) AS cache_read,
         SUM(input_uncached + cache_read + cache_write_5m + cache_write_1h) AS input_total, SUM(failover) AS failovers
       FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow'`, since),
    all<{ day: string; calls: number; cost_micro: number }>(env,
      `SELECT strftime('%Y-%m-%d', started_at_ms / 1000, 'unixepoch') AS day, COUNT(*) AS calls,
         SUM(cost_micro) AS cost_micro
       FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow' GROUP BY day ORDER BY day`, since),
    all<{ task: string; calls: number; failed: number; cost_micro: number; charged_micro: number }>(env,
      `SELECT COALESCE(task, COALESCE(feature, '_') || '.' || '(' || capability || ')') AS task, COUNT(*) AS calls,
         SUM(status != 'ok') AS failed, SUM(cost_micro) AS cost_micro, SUM(charged_micro) AS charged_micro
       FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow' GROUP BY 1 ORDER BY calls DESC`, since),
    all<{ model_id: string; calls: number; failed: number; cost_micro: number; charged_micro: number;
      cache_read: number; input_total: number }>(env,
      `SELECT COALESCE(model_id, model) AS model_id, COUNT(*) AS calls, SUM(status != 'ok') AS failed,
         SUM(cost_micro) AS cost_micro, SUM(charged_micro) AS charged_micro, SUM(cache_read) AS cache_read,
         SUM(input_uncached + cache_read + cache_write_5m + cache_write_1h) AS input_total
       FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow' GROUP BY 1 ORDER BY calls DESC`, since),
    all<{ provider: string; calls: number; failed: number; cost_micro: number; failovers: number }>(env,
      `SELECT provider, COUNT(*) AS calls, SUM(status != 'ok') AS failed, SUM(cost_micro) AS cost_micro,
         SUM(failover) AS failovers
       FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow' GROUP BY provider ORDER BY calls DESC`, since),
    all<{ account_id: string; account_name: string | null; calls: number; cost_micro: number; charged_micro: number }>(env,
      `SELECT r.account_id, a.name AS account_name, COUNT(*) AS calls, SUM(r.cost_micro) AS cost_micro,
         SUM(r.charged_micro) AS charged_micro
       FROM ai_requests r LEFT JOIN accounts a ON a.id = r.account_id
       WHERE r.started_at_ms >= ?1 AND r.billing != 'shadow' GROUP BY r.account_id ORDER BY cost_micro DESC LIMIT 50`,
    since),
    all<Record<string, any>>(env,
      `SELECT id, account_id, billing, feature, capability, task, model_id, provider, model, status, failure_class,
         failover, attempts, cost_micro, charged_micro, started_at_ms, first_byte_ms, finished_at_ms
       FROM ai_requests WHERE billing != 'shadow' ORDER BY started_at_ms DESC LIMIT 50`),
  ]);
  return { days, totals: totals ?? {}, by_day: byDay, by_task: byTask, by_model: byModel, by_provider: byProvider,
    accounts, recent };
}

/** Calls and cost per assignment pattern over 30 days: a task's own, a module's all its tasks'. */
export async function taskUsage(env: Env, now: number): Promise<Map<string, { calls: number; cost_micro: number }>> {
  const rows = await all<{ task: string | null; feature: string | null; calls: number; cost_micro: number }>(env,
    `SELECT task, feature, COUNT(*) AS calls, SUM(cost_micro) AS cost_micro FROM ai_requests
     WHERE started_at_ms >= ?1 AND billing != 'shadow' GROUP BY task, feature`, (now - 30 * DAY) * 1000);
  const out = new Map<string, { calls: number; cost_micro: number }>();
  const add = (key: string, r: { calls: number; cost_micro: number }) => {
    const t = out.get(key) ?? { calls: 0, cost_micro: 0 };
    out.set(key, { calls: t.calls + r.calls, cost_micro: t.cost_micro + (r.cost_micro ?? 0) });
  };
  for (const r of rows) {
    const module = r.task ? moduleOf(r.task) : r.feature;
    if (r.task) add(r.task, r);
    if (module) add(`${module}.*`, r);
    add('*', r);
  }
  return out;
}

// -- problems --------------------------------------------------------------------------

export interface Problem {
  /** Stable while the problem is the same one: what a dismissal is kept under. */
  key: string;
  tone: 'bad' | 'warn';
  text: string;
  href?: string;
  /** An action the problem can be fixed by from where it is shown. */
  fix?: { label: string; action: string; body?: unknown };
  dismissed: boolean;
  dismissed_at?: number;
  dismissed_by?: string | null;
}

/** What a problem key may look like (the dismiss API checks it). */
export const PROBLEM_KEY = /^[a-z0-9_.*:+-]{1,200}$/;

const slug = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);

export interface ProblemOptions {
  /** The capacity assessment, when the caller has it; otherwise read here. null leaves capacity out (the step
   * strip on every page, which should not wait on Cloudflare's analytics). */
  capacity?: Assessment | null;
  /** Forget dismissals of problems that are gone (default), so one that comes back is shown again. */
  purge?: boolean;
}

/**
 * The problems worth an admin's attention, each with a stable key. A dismissed one stays in the list, marked,
 * until it goes away; then its dismissal is forgotten, so if it comes back it is shown again.
 */
export async function problems(env: Env, now: number, catalog?: Awaited<ReturnType<typeof catalogView>>,
  tasks?: Awaited<ReturnType<typeof tasksView>>, opts: ProblemOptions = {}): Promise<Problem[]> {
  const view = catalog ?? await catalogView(env, now);
  const out: Problem[] = [];
  const push = (p: Omit<Problem, 'dismissed'>) => {
    if (!out.some((x) => x.key === p.key)) out.push({ ...p, dismissed: false });
  };
  const t = tasks ?? await tasksView(env);
  const capacity = opts.capacity !== undefined ? opts.capacity
    : await gather(env, now * 1000, chainsOf(t.rows)).then(assess).catch(() => null);
  if (capacity?.level === 'act') {
    const keys = capacity.signals.filter((x) => x.level === 'act').map((x) => x.key).sort();
    push({ key: `capacity:${keys.join('+') || 'act'}`, tone: 'bad',
      text: `Capacity: ${capacity.headline.replace(/^Act now on/, 'act now on')}.`, href: '#capacity' });
  }
  if (view.legacy.serving) {
    push({ key: 'legacy', tone: 'warn', text: `The previous route table still serves every call (${view.legacy.routes
    } routes, ${view.legacy.models} catalogued models). Migrate it to approved models and task routing.`,
    href: '/admin/ai/settings', fix: { label: 'Migrate', action: '/admin/api/ai/migrate-legacy' } });
  }
  for (const p of view.providers) {
    if (p.forced) {
      push({ key: `provider:${p.provider}:off`, tone: 'bad', text: `${p.provider} is switched off${p.forced.reason
        ? ` (${p.forced.reason})` : ''}: ${p.routes} route${p.routes === 1 ? '' : 's'} skipped.`,
      href: '/admin/ai/providers' });
    }
    if (p.status && !p.status.ok) {
      push({ key: `provider:${p.provider}:list`, tone: 'warn', text: `${p.provider}'s model list could not be read: ${
        p.status.error ?? 'error'}.`, href: '/admin/ai/providers' });
    }
  }
  const usedModels = new Set(view.models.filter((m) => m.used_by.length).map((m) => m.id));
  for (const m of view.models) {
    for (const r of m.routes) {
      const key = `route:${m.id}:${r.provider}`;
      // Before the enabled check: an unpriced route is off because it is unpriced.
      if (r.unpriced) {
        push({ key: `${key}:unpriced`, tone: 'warn', text: `${m.name} via ${r.provider} has no price, so it is off. ` +
          'Set its price to use it.', href: `/admin/ai/models/${m.id}#prices` });
      }
      if (!r.enabled) continue;
      if (r.availability === 'down' && usedModels.has(m.id)) {
        push({ key: `${key}:down`, tone: 'bad', text: `${m.name} via ${r.provider} is down.`,
          href: `/admin/ai/models/${m.id}` });
      }
      if (r.circuit === 'open') {
        push({ key: `${key}:circuit`, tone: 'warn', text: `${m.name} via ${r.provider}: circuit open, calls go to the ` +
          'next route.', href: `/admin/ai/models/${m.id}` });
      }
      if (r.stale && usedModels.has(m.id)) {
        push({ key: `${key}:stale`, tone: 'warn', text: `${m.name} via ${r.provider}: price not confirmed for over ${
          view.stale_after_h} h.`, href: `/admin/ai/models/${m.id}`, fix: { label: 'Refresh',
          action: '/admin/api/ai/pricing/refresh', body: { model_id: m.id } } });
      }
      if (r.extra?.unlisted && usedModels.has(m.id)) {
        push({ key: `${key}:unlisted`, tone: 'warn', text: `${r.provider} no longer lists ${r.provider_model} (${
          m.name}).`, href: `/admin/ai/models/${m.id}` });
      }
    }
    if (usedModels.has(m.id) && m.routes.length && !m.routes.some((r) => r.enabled && r.configured)) {
      push({ key: `model:${m.id}:nokey`, tone: 'bad', text: `${m.name} is assigned, but none of its providers has a ` +
        'key set.', href: `/admin/ai/models/${m.id}` });
    }
  }
  for (const row of t.rows) {
    for (const s of row.effective.skipped.filter((x) => x.pattern === row.pattern)) {
      push({ key: `task:${row.pattern}:skipped:${s.model_id}`, tone: 'warn', text: `${patternLabel(row.pattern)}: ${
        s.model_id} is passed over (${s.reason}).`, href: `/admin/ai/tasks?edit=${encodeURIComponent(row.pattern)}` });
    }
  }
  for (const row of t.rows) {
    if (row.effective.source !== row.pattern) continue;
    for (const link of row.effective.chain.filter((l) => l.effort_unknown)) {
      push({ key: `task:${row.pattern}:effort:${link.model_id}`, tone: 'warn', text: `${patternLabel(row.pattern)
      }: no effort is sent to ${link.name}, since nothing says how it takes effort. Set its levels on its page.`,
      href: `/admin/ai/models/${link.model_id}` });
    }
  }
  // One line per model and reason, naming the rows it affects.
  const groups = new Map<string, { id: string; name: string; reason: string; rows: string[] }>();
  for (const row of t.rows) {
    for (const m of row.mismatch) {
      const key = `${m.model_id}|${m.reason}`;
      const g = groups.get(key) ?? { id: m.model_id, name: m.name, reason: m.reason, rows: [] };
      g.rows.push(row.pattern);
      groups.set(key, g);
    }
  }
  for (const g of groups.values()) {
    const names = g.rows.map(patternLabel);
    const shown = names.length > 2 ? `${names.slice(0, 2).join(', ')} and ${names.length - 2} more` : names.join(' and ');
    push({ key: `mismatch:${g.id}:${slug(g.reason)}`, tone: 'warn', text: `${shown} ${names.length === 1 ? 'is' : 'are'
    } served by ${g.name}, which has ${g.reason}.`, href: `/admin/ai/tasks?edit=${encodeURIComponent(g.rows[0]!)}` });
  }
  const dismissed = await all<{ key: string; dismissed_at: number; dismissed_by: string | null }>(env,
    'SELECT key, dismissed_at, dismissed_by FROM ai_dismissals').catch(() => []);
  for (const d of dismissed) {
    const p = out.find((x) => x.key === d.key);
    if (p) Object.assign(p, { dismissed: true, dismissed_at: d.dismissed_at, dismissed_by: d.dismissed_by });
  }
  const gone = dismissed.filter((d) => !out.some((x) => x.key === d.key));
  if (opts.purge !== false && gone.length) {
    await env.LICENSE_DB.batch(gone.map((d) => env.LICENSE_DB.prepare('DELETE FROM ai_dismissals WHERE key = ?1')
      .bind(d.key))).catch(() => undefined);
  }
  return out;
}

// -- providers -------------------------------------------------------------------------

export type ProviderState = 'connected' | 'key_refused' | 'unreachable' | 'unchecked' | 'not_connected' | 'off';

export interface ProviderStatusView {
  provider: Provider;
  label: string;
  wire: string;
  direct: boolean;
  /** A key is set (on the page or as a Worker secret). */
  configured: boolean;
  state: ProviderState;
  key: Pick<KeyView, 'source' | 'hint' | 'updated_at' | 'updated_by' | 'secret' | 'key_var'>;
  check: KeyView['check'];
  /** The last price-list read (pricing.refreshPricing). */
  listing: { at: number; ok: boolean; error: string | null; models_seen: number } | null;
  lists_prices: boolean;
  can_list: boolean;
  balance: Record<string, unknown> | null;
  rate_limit: Record<string, unknown> | null;
  forced: { reason: string | null; at: number } | null;
  /** Its catalogue routes, and the models they reach. */
  routes: number;
  models: number;
}

/** Every provider's connection, first match: switched off, no key, never checked, the check's answer. */
export async function providersView(env: Env, now: number, catalog?: Awaited<ReturnType<typeof catalogView>>):
  Promise<ProviderStatusView[]> {
  const view = catalog ?? await catalogView(env, now);
  const { keys } = await keysView(env, baseEnv(env));
  return PROVIDERS.map((provider) => {
    const p = view.providers.find((x) => x.provider === provider)!;
    const key = keys.find((k) => k.provider === provider)!;
    const check = key.check;
    const state: ProviderState = p.forced ? 'off' : key.source === 'none' ? 'not_connected' : !check ? 'unchecked'
      : check.ok ? 'connected' : check.status === 401 || check.status === 403 || /refused the key/.test(check.error ?? '')
        ? 'key_refused' : 'unreachable';
    return {
      provider, label: LABELS[provider], wire: p.wire, direct: p.direct, configured: p.configured, state,
      key: { source: key.source, hint: key.hint, updated_at: key.updated_at, updated_by: key.updated_by,
        secret: key.secret, key_var: key.key_var },
      check,
      listing: p.status ? { at: p.status.checked_at, ok: !!p.status.ok, error: p.status.error,
        models_seen: p.status.models_seen } : null,
      lists_prices: p.lists_prices, can_list: p.can_list,
      balance: parse(p.status?.balance_json ?? null), rate_limit: parse(p.status?.rate_limit_json ?? null),
      forced: p.forced, routes: p.routes,
      models: new Set(view.models.filter((m) => m.routes.some((r) => r.provider === provider)).map((m) => m.id)).size,
    };
  });
}

// -- summary ---------------------------------------------------------------------------

export interface Summary {
  providers: { connected: number; total: number; by_state: Partial<Record<ProviderState, number>> };
  models: { total: number; active: number; unused: number; unpriced: number; disabled: number };
  general: { model_id: string | null; model: string | null; chain: string[]; level: Level | null };
  fallback: { open_circuits: number; forced: string[]; routes_without_fallback: number };
  tasks: { total: number; own: number; issues: number };
  problems: { open: number; dismissed: number };
}

/** The four steps in one line each: what the step strip's dots and the Overview's strip read. */
export async function summary(env: Env, now: number, pre: {
  catalog?: Awaited<ReturnType<typeof catalogView>>; tasks?: Awaited<ReturnType<typeof tasksView>>;
  providers?: ProviderStatusView[]; problems?: Problem[];
} = {}): Promise<Summary> {
  const catalog = pre.catalog ?? await catalogView(env, now);
  const tasks = pre.tasks ?? await tasksView(env);
  const providers = pre.providers ?? await providersView(env, now, catalog);
  const found = pre.problems ?? await problems(env, now, catalog, tasks, { purge: false, capacity: null });
  const byState: Partial<Record<ProviderState, number>> = {};
  for (const p of providers) byState[p.state] = (byState[p.state] ?? 0) + 1;
  const models = catalog.models;
  const general = tasks.rows.find((r) => r.pattern === '*');
  const head = general?.effective.chain[0];
  const chains = chainsOf(tasks.rows);
  return {
    providers: { connected: providers.filter((p) => p.state === 'connected').length, total: providers.length,
      by_state: byState },
    models: { total: models.length,
      active: models.filter((m) => m.enabled && m.routes.some((r) => r.enabled && r.configured)).length,
      unused: models.filter((m) => m.enabled && !m.used_by.length).length,
      unpriced: models.filter((m) => m.routes.some((r) => r.unpriced)).length,
      disabled: models.filter((m) => !m.enabled).length },
    general: { model_id: head?.model_id ?? null, model: head?.name ?? null,
      chain: general?.effective.chain.map((l) => l.name) ?? [], level: general?.effective.level ?? null },
    fallback: { open_circuits: catalog.circuits.filter((x) => !x.forced && x.open_until > now).length,
      forced: catalog.providers.filter((p) => p.forced).map((p) => p.provider),
      routes_without_fallback: chains.filter((ch) => ch.routes.length < 2).length },
    tasks: { total: tasks.rows.length, own: tasks.rows.filter((r) => r.serve.length).length,
      issues: tasks.rows.filter((r) => r.mismatch.length ||
        r.effective.skipped.some((x) => x.pattern === r.pattern) ||
        (r.effective.source === r.pattern && r.effective.chain.some((l) => l.effort_unknown))).length },
    problems: { open: found.filter((p) => !p.dismissed).length, dismissed: found.filter((p) => p.dismissed).length },
  };
}
