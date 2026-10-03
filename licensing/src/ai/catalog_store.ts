/**
 * Writes to the approved-model catalogue: models, their provider routes and
 * the order those are tried in. The admin API (routes/aiAdminCatalog.ts) and
 * the legacy migration share these, so a rank is never written two ways.
 */
import { all, one } from '../db';
import type { Env } from '../env';
import { eventStatement } from '../events';
import { BUILTIN_MODELS, COSTS, type UnitCosts } from './catalog';
import { builtinProfile, type StoredProfile } from './effort';
import type { Listing } from './pricing';
import { builtinPrice, builtinSource, hasPriceApi } from './pricing';
import type { Provider } from './providers';
import type { CatalogRouteRow, CatalogRow } from './routing';

export const MODEL_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
export const WIRE_MODEL = /^[A-Za-z0-9._:/-]{1,160}$/;
export const MAX_ROUTES = 3;

const squash = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

/**
 * The approved-model id a provider's model id suggests: its last path part,
 * as a slug (`google/gemma-4-31b-it:free` -> `gemma-4-31b-it-free`), or an
 * approved model it already is under another spelling (`anthropic/claude-opus-5.5`
 * -> `claude-opus-5-5`).
 */
export function suggestId(providerModel: string, existing: string[]): string {
  const tail = providerModel.split('/').pop() ?? providerModel;
  const slug = squash(tail).slice(0, 64) || 'model';
  return existing.find((id) => squash(id) === slug) ?? slug;
}

export async function modelById(env: Env, id: string): Promise<CatalogRow | null> {
  return one<CatalogRow>(env, 'SELECT * FROM ai_catalog WHERE id = ?1', id);
}

export async function routesOf(env: Env, id: string): Promise<CatalogRouteRow[]> {
  return all<CatalogRouteRow>(env, 'SELECT * FROM ai_catalog_routes WHERE model_id = ?1 ORDER BY rank', id);
}

export interface ModelFields {
  name: string; family: string | null; context_window: number | null; max_output: number | null;
  supports_vision: number; supports_tools: number; supports_structured: number; reasoning: number;
  status: CatalogRow['status']; enabled: number; note: string | null;
}

export function upsertModel(env: Env, id: string, f: ModelFields, who: string, now: number): D1PreparedStatement {
  return env.LICENSE_DB.prepare(
    `INSERT INTO ai_catalog (id, name, family, context_window, max_output, supports_vision, supports_tools,
       supports_structured, reasoning, status, enabled, note, updated_at, updated_by)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
     ON CONFLICT(id) DO UPDATE SET name = ?2, family = ?3, context_window = ?4, max_output = ?5,
       supports_vision = ?6, supports_tools = ?7, supports_structured = ?8, reasoning = ?9, status = ?10,
       enabled = ?11, note = ?12, updated_at = ?13, updated_by = ?14`,
  ).bind(id, f.name, f.family, f.context_window, f.max_output, f.supports_vision, f.supports_tools,
    f.supports_structured, f.reasoning, f.status, f.enabled, f.note, now, who);
}

/** Said in a model's note when its listing does not say whether it reasons, so "no" is never silent. */
export const REASONING_UNCONFIRMED = 'Reasoning unconfirmed: the provider\'s list does not say whether this model ' +
  'reasons (extended thinking). Tick “Reasons” if it does; the judging tasks need it.';

/** A model's fields from a provider listing, for a model not yet approved as `id`. A listing that does not
 * say whether the model reasons (OrcaRouter's never does) defers to the built-in model of that id, and failing
 * that records no -- with a note saying so. */
export function fieldsFromListing(found: Listing, providerModel: string, id: string): ModelFields {
  const name = (found.name ?? providerModel).replace(/^[^:]{1,40}:\s+/, '').trim();
  const vendor = providerModel.includes('/') ? providerModel.split('/')[0]! : null;
  const reasoning = found.reasoning ?? BUILTIN_MODELS.find((m) => m.id === id)?.reasoning ?? null;
  return { name: name || providerModel, family: vendor ? vendor.charAt(0).toUpperCase() + vendor.slice(1) : null,
    context_window: found.context_window, max_output: found.max_output,
    supports_vision: found.vision === false ? 0 : 1, supports_tools: found.tools === false ? 0 : 1,
    supports_structured: found.structured === false ? 0 : 1, reasoning: reasoning ? 1 : 0, status: 'active',
    enabled: 1, note: reasoning === null ? REASONING_UNCONFIRMED : null };
}

/** The effort profile a provider's list gives, to store on the model -- unless a built-in profile already
 * knows the model better (a list's levels are the aggregator's, not always the model's own wire). */
export function effortFromListing(found: Listing, providerModel: string, id: string): string | null {
  if (!found.efforts || builtinProfile(id, providerModel)) return null;
  const stored: StoredProfile = { ...found.efforts, wire: 'reasoning', source: 'listing' };
  return JSON.stringify(stored);
}

/** Store (or, with null, clear) a model's effort profile. */
export function setEffortStatement(env: Env, id: string, json: string | null, who: string,
  now: number): D1PreparedStatement {
  return env.LICENSE_DB.prepare('UPDATE ai_catalog SET effort_json = ?2, updated_at = ?3, updated_by = ?4 WHERE id = ?1')
    .bind(id, json, now, who);
}

export interface RouteFields {
  provider: Provider; provider_model: string; rank: number; enabled: number; failover: CatalogRouteRow['failover'];
  prices: UnitCosts; fee_bps: number; extra_json: string | null; price_source: CatalogRouteRow['price_source'];
  priced_at: number | null; source_url: string | null; note: string | null; availability?: CatalogRouteRow['availability'];
}

export function insertRoute(env: Env, modelId: string, r: RouteFields, who: string, now: number): D1PreparedStatement {
  return env.LICENSE_DB.prepare(
    `INSERT INTO ai_catalog_routes (model_id, provider, provider_model, rank, enabled, failover, in_micro,
       cache_read_micro, cache_write_5m_micro, cache_write_1h_micro, out_micro, fee_bps, extra_json, availability,
       price_source, priced_at, source_url, note, updated_at, updated_by)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20)`,
  ).bind(modelId, r.provider, r.provider_model, r.rank, r.enabled, r.failover, r.prices.in, r.prices.cache_read,
    r.prices.cache_write_5m, r.prices.cache_write_1h, r.prices.out, r.fee_bps, r.extra_json,
    r.availability ?? 'unknown', r.price_source, r.priced_at, r.source_url, r.note, now, who);
}

/** A route row exactly as it is, at another rank: how a reorder rewrites a model's routes. */
function copyRoute(env: Env, row: CatalogRouteRow, rank: number, who: string, now: number): D1PreparedStatement {
  return env.LICENSE_DB.prepare(
    `INSERT INTO ai_catalog_routes (model_id, provider, provider_model, rank, enabled, failover, in_micro,
       cache_read_micro, cache_write_5m_micro, cache_write_1h_micro, out_micro, fee_bps, extra_json, availability,
       rate_limit_json, latency_p50_ms, price_source, priced_at, source_url, note, updated_at, updated_by)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22)`,
  ).bind(row.model_id, row.provider, row.provider_model, rank, row.enabled, row.failover, row.in_micro,
    row.cache_read_micro, row.cache_write_5m_micro, row.cache_write_1h_micro, row.out_micro, row.fee_bps,
    row.extra_json, row.availability, row.rate_limit_json, row.latency_p50_ms, row.price_source, row.priced_at,
    row.source_url, row.note, rank === row.rank ? row.updated_at : now, rank === row.rank ? row.updated_by : who);
}

/**
 * Rewrite a model's routes in the given order (ranks 0..n-1), in one batch:
 * the UNIQUE (model_id, rank) index never sees two routes at one rank.
 */
export function reorderStatements(env: Env, modelId: string, ordered: CatalogRouteRow[], who: string,
  now: number): D1PreparedStatement[] {
  return [
    env.LICENSE_DB.prepare('DELETE FROM ai_catalog_routes WHERE model_id = ?1').bind(modelId),
    ...ordered.map((row, rank) => copyRoute(env, row, rank, who, now)),
  ];
}

/** A route's price when none is given: the provider's listing, else its built-in list price, else nothing. */
export function defaultPricing(provider: Provider, providerModel: string, found: Listing | null, now: number):
  Pick<RouteFields, 'prices' | 'fee_bps' | 'price_source' | 'priced_at' | 'source_url' | 'extra_json'> | null {
  if (found?.prices && hasPriceApi(provider)) {
    return { prices: found.prices, fee_bps: found.fee_bps, price_source: 'api', priced_at: now, source_url: null,
      extra_json: Object.keys(found.extra).length ? JSON.stringify(found.extra) : null };
  }
  const builtin = builtinPrice(provider, providerModel);
  if (builtin) {
    return { prices: builtin, fee_bps: 0, price_source: 'builtin', priced_at: now, source_url: builtinSource(provider),
      extra_json: null };
  }
  return null;
}

/** The built-in models and their direct Anthropic routes, where they are missing. Idempotent. */
export async function seedBuiltinStatements(env: Env, who: string, now: number): Promise<D1PreparedStatement[]> {
  const statements: D1PreparedStatement[] = [];
  const have = new Set((await all<{ id: string }>(env, 'SELECT id FROM ai_catalog')).map((r) => r.id));
  const routes = await all<{ model_id: string; provider: string; rank: number }>(env,
    'SELECT model_id, provider, rank FROM ai_catalog_routes');
  for (const m of BUILTIN_MODELS) {
    if (!have.has(m.id)) {
      statements.push(upsertModel(env, m.id, { name: m.name, family: m.family, context_window: null, max_output: null,
        supports_vision: m.vision ? 1 : 0, supports_tools: m.tools ? 1 : 0, supports_structured: m.structured ? 1 : 0,
        reasoning: m.reasoning ? 1 : 0, status: 'active', enabled: 1, note: null }, who, now));
    }
    const mine = routes.filter((r) => r.model_id === m.id);
    if (mine.some((r) => r.provider === 'anthropic') || mine.length >= MAX_ROUTES) continue;
    const rank = [0, 1, 2].find((n) => !mine.some((r) => r.rank === n))!;
    statements.push(insertRoute(env, m.id, { provider: 'anthropic', provider_model: m.id, rank, enabled: 1,
      failover: 'error', prices: COSTS[m.id]!, fee_bps: 0, extra_json: null, price_source: 'builtin', priced_at: now,
      source_url: builtinSource('anthropic'), note: null }, who, now));
  }
  if (statements.length) {
    statements.push(eventStatement(env, now, { actor: who, kind: 'ai.catalog_seeded',
      payload: { models: BUILTIN_MODELS.map((m) => m.id) } }));
  }
  return statements;
}

/** The task patterns that name a model (serve or shadow), for "used by" and the delete guard. */
export async function usedBy(env: Env, modelId: string): Promise<string[]> {
  const rows = await all<{ task: string }>(env, 'SELECT DISTINCT task FROM ai_task_routes WHERE model_id = ?1 ORDER BY task',
    modelId);
  return rows.map((r) => r.task);
}
