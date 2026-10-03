/**
 * The v3 route table, kept for one release: it serves while no task has an
 * assignment (ai_task_routes), so a deploy changes nothing until an admin
 * migrates (POST /admin/api/ai/migrate-legacy). Read only; nothing writes
 * ai_routes or ai_models any more. Removed with them in 005.
 *
 * Resolution is by (feature, capability): rows for the call's feature win,
 * then rows for '*', then the built-in default (catalog.ROUTES).
 */
import type { Env } from '../env';
import { all, one } from '../db';
import { type Capability, COSTS, ROUTES, type Route } from './catalog';
import { admits, isProvider, type Provider } from './providers';
import { bucketOf, type ModelCost } from './routing';

interface ModelRow {
  provider: string; model: string; in_micro: number; cache_read_micro: number; cache_write_5m_micro: number;
  cache_write_1h_micro: number; out_micro: number; fee_bps: number; enabled: number;
  supports_structured: number; supports_tools: number; supports_vision: number;
}

/** A catalogued, enabled model's unit costs: `ai_models`, else Anthropic's built-in list prices. */
export async function legacyModelCost(env: Env, provider: Provider, model: string): Promise<ModelCost | null> {
  const row = await one<ModelRow>(env, 'SELECT * FROM ai_models WHERE provider = ?1 AND model = ?2', provider, model);
  if (row) {
    if (!row.enabled) return null;
    return { provider, model, in: row.in_micro, cache_read: row.cache_read_micro,
      cache_write_5m: row.cache_write_5m_micro, cache_write_1h: row.cache_write_1h_micro, out: row.out_micro,
      fee_bps: row.fee_bps, structured: !!row.supports_structured, tools: !!row.supports_tools,
      vision: !!row.supports_vision };
  }
  if (provider === 'anthropic' && COSTS[model]) {
    return { provider, model, ...COSTS[model]!, fee_bps: 0, structured: true, tools: true, vision: true };
  }
  return null;
}

export interface LegacyRouteRow {
  id: string; feature: string; capability: string; role: 'serve' | 'shadow'; rank: number; provider: string;
  model: string; effort: Route['effort']; max_tokens_cap: number; failover: Route['failover'];
  evaluation_id: number | null; shadow_pct: number; enabled: number; unbenched: number; note: string | null;
  updated_at: number;
  updated_by: string | null;
}

function routeOf(row: LegacyRouteRow): Route {
  return { id: row.id, provider: row.provider as Provider, model: row.model, model_id: row.model, level: 'legacy',
    effort: row.effort, max_tokens_cap: row.max_tokens_cap, failover: row.failover };
}

/** The serving routes for a call, best first, before circuits and stickiness. */
export async function legacyCandidates(env: Env, feature: string | null, capability: Capability): Promise<Route[]> {
  const rows = await all<LegacyRouteRow>(env,
    `SELECT * FROM ai_routes WHERE capability = ?1 AND role = 'serve' AND enabled = 1 AND feature IN (?2, '*')
     ORDER BY rank`, capability, feature ?? '*').catch(() => []);
  const specific = rows.filter((r) => r.feature !== '*');
  const chosen = specific.length ? specific : rows.filter((r) => r.feature === '*');
  const routes = chosen.filter((r) => isProvider(r.provider) && admits(r.provider as Provider, r.model)).map(routeOf);
  return routes.length ? routes : [ROUTES[capability]];
}

/** The shadow candidate for this session, if one is sampling it. */
export async function legacyShadowFor(env: Env, feature: string | null, capability: Capability,
  sessionKey: string | null): Promise<Route | null> {
  if (!sessionKey) return null;
  const rows = await all<LegacyRouteRow>(env,
    `SELECT * FROM ai_routes WHERE capability = ?1 AND role = 'shadow' AND enabled = 1 AND shadow_pct > 0
       AND feature IN (?2, '*')
     ORDER BY CASE WHEN feature = '*' THEN 1 ELSE 0 END, rank`, capability, feature ?? '*').catch(() => []);
  const bucket = bucketOf(sessionKey);
  const row = rows.find((r) => isProvider(r.provider) && admits(r.provider as Provider, r.model) &&
    bucket < r.shadow_pct);
  return row ? routeOf(row) : null;
}

/** How many legacy rows are left: the migration note shows while any are. */
export async function legacyRows(env: Env): Promise<{ routes: number; models: number }> {
  const count = (table: string) => one<{ n: number }>(env, `SELECT COUNT(*) AS n FROM ${table}`)
    .then((r) => r?.n ?? 0, () => 0);
  const [routes, models] = await Promise.all([count('ai_routes'), count('ai_models')]);
  return { routes, models };
}
