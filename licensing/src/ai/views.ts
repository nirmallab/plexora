/**
 * What the admin sees of Plexora AI, computed once for both the JSON API
 * (routes/aiAdminCatalog.ts) and the pages (routes/adminAi/): the catalogue
 * with each route's live state, every task's effective chain (from the same
 * `resolve()` a call uses, never a re-implementation), pricing status, usage,
 * and the problems worth an admin's attention.
 */
import { all, one } from '../db';
import { DAY, type Env, knob } from '../env';
import { BENCH, type Capability, type Level } from './catalog';
import { describeResolved, type EffortProfile, type EffortSpec, profileFor, type ProfileSource,
  resolveEffort } from './effort';
import { isStale, hasPriceApi } from './pricing';
import { admits, configured, isProvider, type Provider, PROVIDERS, SPECS } from './providers';
import {
  type CatalogRouteRow, type CatalogRow, hasTaskRouting, lacks, resolve, specOf, type TaskRouteRow, taskEffort,
} from './routing';
import { legacyRows } from './routing_legacy';
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
  price_api: boolean;
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
      extra: parse(r.extra_json),
    })),
    used_by: assigned.filter((a) => a.model_id === m.id).map((a) => a.task),
    effort: effortView(m, routes.filter((r) => r.model_id === m.id).map((r) => r.provider_model)),
  }));
  const providers: ProviderView[] = PROVIDERS.map((p) => {
    const forced = open.find((x) => x.route_key === p && x.forced);
    return { provider: p, wire: SPECS[p].wire, direct: SPECS[p].direct, configured: configured(env, p),
      price_api: hasPriceApi(p), forced: forced ? { reason: forced.reason, at: forced.updated_at } : null,
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
  tone: 'bad' | 'warn';
  text: string;
  href?: string;
  /** An action the problem can be fixed by from where it is shown. */
  fix?: { label: string; action: string; body?: unknown };
}

export async function problems(env: Env, now: number, catalog?: Awaited<ReturnType<typeof catalogView>>,
  tasks?: Awaited<ReturnType<typeof tasksView>>): Promise<Problem[]> {
  const view = catalog ?? await catalogView(env, now);
  const out: Problem[] = [];
  if (view.legacy.serving) {
    out.push({ tone: 'warn', text: `The previous route table still serves every call (${view.legacy.routes} routes, ${
      view.legacy.models} catalogued models). Migrate it to approved models and task routing.`,
    href: '/admin/ai/settings', fix: { label: 'Migrate', action: '/admin/api/ai/migrate-legacy' } });
  }
  for (const p of view.providers) {
    if (p.forced) {
      out.push({ tone: 'bad', text: `${p.provider} is switched off${p.forced.reason ? ` (${p.forced.reason})` : ''}: ${
        p.routes} route${p.routes === 1 ? '' : 's'} skipped.`, href: '/admin/ai/providers' });
    }
    if (p.status && !p.status.ok) {
      out.push({ tone: 'warn', text: `${p.provider}'s model list could not be read: ${p.status.error ?? 'error'}.`,
        href: '/admin/ai/providers' });
    }
  }
  const usedModels = new Set(view.models.filter((m) => m.used_by.length).map((m) => m.id));
  for (const m of view.models) {
    for (const r of m.routes) {
      if (!r.enabled) continue;
      if (r.availability === 'down' && usedModels.has(m.id)) {
        out.push({ tone: 'bad', text: `${m.name} via ${r.provider} is down.`, href: `/admin/ai/models/${m.id}` });
      }
      if (r.circuit === 'open') {
        out.push({ tone: 'warn', text: `${m.name} via ${r.provider}: circuit open, calls go to the next route.`,
          href: `/admin/ai/models/${m.id}` });
      }
      if (r.stale && usedModels.has(m.id)) {
        out.push({ tone: 'warn', text: `${m.name} via ${r.provider}: price not confirmed for over ${view.stale_after_h} h.`,
          href: `/admin/ai/models/${m.id}`, fix: { label: 'Refresh', action: '/admin/api/ai/pricing/refresh',
            body: { model_id: m.id } } });
      }
      if (r.extra?.unlisted && usedModels.has(m.id)) {
        out.push({ tone: 'warn', text: `${r.provider} no longer lists ${r.provider_model} (${m.name}).`,
          href: `/admin/ai/models/${m.id}` });
      }
    }
    if (usedModels.has(m.id) && m.routes.length && !m.routes.some((r) => r.enabled && r.configured)) {
      out.push({ tone: 'bad', text: `${m.name} is assigned, but none of its providers has a key set.`,
        href: `/admin/ai/models/${m.id}` });
    }
  }
  const t = tasks ?? await tasksView(env);
  for (const row of t.rows) {
    for (const s of row.effective.skipped.filter((x) => x.pattern === row.pattern)) {
      out.push({ tone: 'warn', text: `${patternLabel(row.pattern)}: ${s.model_id} is passed over (${s.reason}).`,
        href: `/admin/ai/routing?edit=${encodeURIComponent(row.pattern)}` });
    }
  }
  for (const row of t.rows) {
    if (row.effective.source !== row.pattern) continue;
    for (const link of row.effective.chain.filter((l) => l.effort_unknown)) {
      out.push({ tone: 'warn', text: `${patternLabel(row.pattern)}: no effort is sent to ${link.name}, since nothing ` +
        'says how it takes effort. Set its levels on its page.', href: `/admin/ai/models/${link.model_id}` });
    }
  }
  // One line per model and reason, naming the rows it affects.
  const groups = new Map<string, { name: string; reason: string; rows: string[] }>();
  for (const row of t.rows) {
    for (const m of row.mismatch) {
      const key = `${m.model_id}|${m.reason}`;
      const g = groups.get(key) ?? { name: m.name, reason: m.reason, rows: [] };
      g.rows.push(row.pattern);
      groups.set(key, g);
    }
  }
  for (const g of groups.values()) {
    const names = g.rows.map(patternLabel);
    const shown = names.length > 2 ? `${names.slice(0, 2).join(', ')} and ${names.length - 2} more` : names.join(' and ');
    out.push({ tone: 'warn', text: `${shown} ${names.length === 1 ? 'is' : 'are'} served by ${g.name}, which has ${
      g.reason}.`, href: `/admin/ai/routing?edit=${encodeURIComponent(g.rows[0]!)}` });
  }
  return out;
}

