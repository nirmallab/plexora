/**
 * Provider prices and model facts, read from the providers themselves.
 *
 * The aggregators publish their model lists with prices, unauthenticated:
 *
 *   openrouter   GET /api/v1/models   USD per token as strings (prompt, completion,
 *                                     input_cache_read, input_cache_write[_1h]);
 *                                     a 5.5% fee on credits that the prices leave out
 *   orcarouter   GET /v1/models       the same per-token fields, without cache writes (an
 *                                     Anthropic model's are charged at Anthropic's ratios)
 *   saygm        GET /v1/models       nano-dollars per 1M tokens (pricing.dimensions)
 *
 * Anthropic and OpenAI publish no price API: their routes carry list prices
 * from code (catalog.BUILTIN_PRICES) or an admin's own. A price an admin typed
 * (`manual`) is never overwritten; a listed price (`api`) is refreshed by the
 * nightly cron (the `pricing` step) or on demand, and every change is one
 * `ai.price.changed` event, which is the price history.
 *
 * The refresh also confirms availability (listed, and the last day's calls),
 * fills a model's context window where nobody set one, and records each
 * route's observed first-byte latency, which a task's latency preference reads.
 */
import { all } from '../db';
import { DAY, type Env, knob } from '../env';
import { eventStatement } from '../events';
import { ANTHROPIC_PRICING_URL, BUILTIN_PRICES, type UnitCosts } from './catalog';
import { configured, type Provider, providerFetch, PROVIDERS, SPECS } from './providers';
import type { CatalogRouteRow } from './routing';

/** One model as a provider lists it, normalised. Prices are micro-USD per 1M tokens. */
export interface Listing {
  id: string;
  name: string | null;
  prices: UnitCosts | null;
  fee_bps: number;
  context_window: number | null;
  max_output: number | null;
  vision: boolean | null;
  tools: boolean | null;
  structured: boolean | null;
  reasoning: boolean | null;
  available: boolean;
  /** Request and image fees, surcharges, tiered overrides: shown, never billed. */
  extra: Record<string, unknown>;
}

export interface ProviderListing {
  provider: Provider;
  url: string;
  models: Listing[];
  balance: Record<string, unknown> | null;
  rate_limit: Record<string, unknown> | null;
}

/** OpenRouter's platform fee on credits, which its per-token prices leave out. */
export const OPENROUTER_FEE_BPS = 550;

/** USD per token (a string or number) as micro-USD per 1M tokens; null when absent or not a price. */
export function perM(value: unknown): number | null {
  if (value === undefined || value === null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.round(n * 1e12) : null;
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null);

function baseOf(env: Env, provider: Provider): string {
  return String(env[SPECS[provider].baseVar] || SPECS[provider].base).replace(/\/$/, '');
}

async function getJson(url: string, headers: Record<string, string> = {}): Promise<any> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await providerFetch(url, { headers: { accept: 'application/json', 'user-agent': 'plexora-licensing',
      ...headers }, signal: controller.signal });
    if (!response.ok) throw new Error(`${url} answered ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The OpenRouter-shaped listing both OpenRouter and OrcaRouter serve. A cache
 * write the list leaves out is priced at the input price, or, where the
 * provider is known to pass Anthropic's own structure through (`anthropicWrites`),
 * at Anthropic's 1.25x (5 minutes) and 2x (1 hour) of input.
 */
function perTokenListing(m: Record<string, any>, fee: number, anthropicWrites = false): Listing {
  const p = (m.pricing ?? {}) as Record<string, unknown>;
  const input = perM(p.prompt);
  const output = perM(p.completion);
  const claude = anthropicWrites && String(m.id).startsWith('anthropic/') && input !== null;
  const write5 = perM(p.input_cache_write) ?? (claude ? Math.round(input! * 1.25) : input);
  const write1h = perM(p.input_cache_write_1h) ?? (claude ? input! * 2 : write5);
  const prices = input === null && output === null ? null : {
    in: input ?? 0, out: output ?? 0, cache_read: perM(p.input_cache_read) ?? input ?? 0,
    cache_write_5m: write5 ?? 0, cache_write_1h: write1h ?? 0 };
  const extra: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(p)) {
    if (!['prompt', 'completion', 'input_cache_read', 'input_cache_write', 'input_cache_write_1h',
      'prompt_per_million', 'completion_per_million'].includes(key) && value !== '0' && value !== 0) extra[key] = value;
  }
  const params = Array.isArray(m.supported_parameters) ? new Set<string>(m.supported_parameters) : null;
  const modalities = m.architecture?.input_modalities;
  return {
    id: String(m.id), name: typeof m.name === 'string' ? m.name : null, prices, fee_bps: fee,
    context_window: num(m.context_length) ?? num(m.top_provider?.context_length),
    max_output: num(m.top_provider?.max_completion_tokens) ?? num(m.max_completion_tokens),
    vision: Array.isArray(modalities) ? modalities.includes('image') : null,
    tools: params ? params.has('tools') : null,
    structured: params ? params.has('response_format') || params.has('structured_outputs') : null,
    reasoning: params ? params.has('reasoning') || params.has('include_reasoning') : null,
    available: true, extra,
  };
}

/** SayGM: nano-dollars per 1M tokens; only its confidential (-TEE) models may be routed. */
function saygmListing(m: Record<string, any>): Listing {
  const dims = (m.pricing?.unit === 'ndollars_per_mtok' ? m.pricing.dimensions : null) ?? {};
  const nano = (key: string) => (typeof dims[key] === 'number' ? Math.round(dims[key] / 1000) : null);
  const input = nano('input_per_mtok_ndollars');
  const output = nano('output_per_mtok_ndollars');
  const write5 = nano('cache_write_5m_per_mtok_ndollars') ?? input;
  const caps = (m.capabilities ?? {}) as Record<string, unknown>;
  const surcharges = m.pricing?.surcharges && Object.keys(m.pricing.surcharges).length ? m.pricing.surcharges : null;
  return {
    id: String(m.id), name: typeof m.display_name === 'string' ? m.display_name : null,
    prices: input === null && output === null ? null : { in: input ?? 0, out: output ?? 0,
      cache_read: nano('cache_read_per_mtok_ndollars') ?? input ?? 0, cache_write_5m: write5 ?? 0,
      cache_write_1h: nano('cache_write_1h_per_mtok_ndollars') ?? write5 ?? 0 },
    fee_bps: 0, context_window: null, max_output: null,
    vision: typeof caps.vision === 'boolean' ? caps.vision : null,
    tools: typeof caps.tools === 'boolean' ? caps.tools : null, structured: null, reasoning: null,
    available: m.available !== false && m.coming_soon !== true,
    extra: { ...(surcharges ? { surcharges } : {}), ...(m.pricing?.basis ? { basis: m.pricing.basis } : {}) },
  };
}

type Adapter = (env: Env) => Promise<ProviderListing>;

export const ADAPTERS: Partial<Record<Provider, Adapter>> = {
  openrouter: async (env) => {
    const base = baseOf(env, 'openrouter');
    const url = `${base}/v1/models`;
    const body = await getJson(url);
    let balance: Record<string, unknown> | null = null;
    let rateLimit: Record<string, unknown> | null = null;
    if (configured(env, 'openrouter')) {
      // The key's own limits; a failure here never stops the prices.
      try {
        const key = (await getJson(`${base}/v1/key`, { authorization: `Bearer ${env.OPENROUTER_API_KEY}` }))?.data ?? {};
        balance = { limit: key.limit ?? null, usage: key.usage ?? null, remaining: key.limit_remaining ?? null,
          free_tier: key.is_free_tier ?? null };
        rateLimit = key.rate_limit && typeof key.rate_limit === 'object' ? key.rate_limit : null;
      } catch {
        // as above
      }
    }
    return { provider: 'openrouter', url, balance, rate_limit: rateLimit,
      models: (Array.isArray(body?.data) ? body.data : []).map((m: any) => perTokenListing(m, OPENROUTER_FEE_BPS)) };
  },
  orcarouter: async (env) => {
    const url = `${baseOf(env, 'orcarouter')}/v1/models`;
    const body = await getJson(url);
    return { provider: 'orcarouter', url, balance: null, rate_limit: null,
      models: (Array.isArray(body?.data) ? body.data : []).map((m: any) => perTokenListing(m, 0, true)) };
  },
  saygm: async (env) => {
    const url = `${baseOf(env, 'saygm')}/v1/models`;
    const body = await getJson(url);
    return { provider: 'saygm', url, balance: null, rate_limit: null,
      models: (Array.isArray(body?.data) ? body.data : []).map(saygmListing) };
  },
};

/** Whether a provider's prices come from its own listing. */
export const hasPriceApi = (provider: Provider): boolean => ADAPTERS[provider] !== undefined;

const cache = new Map<Provider, { at: number; listing: ProviderListing }>();

/** A provider's listing, reused for ten minutes per isolate unless `fresh`. */
export async function listing(env: Env, provider: Provider, fresh = false): Promise<ProviderListing | null> {
  const adapter = ADAPTERS[provider];
  if (!adapter) return null;
  const hit = cache.get(provider);
  if (!fresh && hit && Date.now() - hit.at < 10 * 60 * 1000) return hit.listing;
  const got = await adapter(env);
  cache.set(provider, { at: Date.now(), listing: got });
  return got;
}

export function clearListingCache(): void {
  cache.clear();
}

/** A built-in list price, for a provider without a price API. */
export function builtinPrice(provider: Provider, providerModel: string): UnitCosts | null {
  return BUILTIN_PRICES[provider]?.[providerModel] ?? null;
}

export function builtinSource(provider: Provider): string | null {
  return provider === 'anthropic' ? ANTHROPIC_PRICING_URL : null;
}

const PRICE_FIELDS = [
  ['in_micro', 'in'], ['cache_read_micro', 'cache_read'], ['cache_write_5m_micro', 'cache_write_5m'],
  ['cache_write_1h_micro', 'cache_write_1h'], ['out_micro', 'out'],
] as const;

export interface Change { field: string; old: number; new: number }

function diff(route: CatalogRouteRow, prices: UnitCosts, fee: number): Change[] {
  const changes: Change[] = [];
  for (const [column, key] of PRICE_FIELDS) {
    if (route[column] !== prices[key]) changes.push({ field: column, old: route[column], new: prices[key] });
  }
  if (route.fee_bps !== fee) changes.push({ field: 'fee_bps', old: route.fee_bps, new: fee });
  return changes;
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return Math.round(sorted.length % 2 ? sorted[mid]! : (sorted[mid - 1]! + sorted[mid]!) / 2);
}

export interface ProviderReport {
  ok: boolean;
  error: string | null;
  models_seen: number;
  routes_updated: number;
  changes: number;
}

/**
 * Refresh every catalogued route's price and facts from its provider, one
 * provider at a time (a failure is that provider's alone), or only one
 * provider's or one model's routes.
 */
export async function refreshPricing(env: Env, now: number, only: { provider?: Provider; model_id?: string } = {},
  actor = 'cron:pricing'): Promise<Record<string, ProviderReport>> {
  const routes = await all<CatalogRouteRow & { m_context: number | null; m_output: number | null }>(env,
    `SELECT r.*, m.context_window AS m_context, m.max_output AS m_output FROM ai_catalog_routes r
     JOIN ai_catalog m ON m.id = r.model_id
     WHERE (?1 IS NULL OR r.provider = ?1) AND (?2 IS NULL OR r.model_id = ?2)`,
    only.provider ?? null, only.model_id ?? null);
  const providers = PROVIDERS.filter((p) => routes.some((r) => r.provider === p) || only.provider === p);
  const sinceMs = (now - 7 * DAY) * 1000;
  const report: Record<string, ProviderReport> = {};
  for (const provider of providers) {
    const mine = routes.filter((r) => r.provider === provider);
    const entry: ProviderReport = { ok: true, error: null, models_seen: 0, routes_updated: 0, changes: 0 };
    let listed: ProviderListing | null = null;
    try {
      listed = await listing(env, provider, true);
      entry.models_seen = listed?.models.length ?? 0;
    } catch (error) {
      entry.ok = false;
      entry.error = String((error as Error)?.message ?? error).slice(0, 300);
    }
    const statements: D1PreparedStatement[] = [];
    for (const route of mine) {
      const found = listed?.models.find((m) => m.id === route.provider_model) ?? null;
      const recent = await all<{ ok: number; status: string; first_byte_ms: number | null; started_at_ms: number }>(env,
        `SELECT status = 'ok' AS ok, status, first_byte_ms, started_at_ms FROM ai_requests
         WHERE provider = ?1 AND model = ?2 AND started_at_ms >= ?3 AND billing != 'shadow'
         ORDER BY started_at_ms DESC LIMIT 2000`, route.provider, route.provider_model, sinceMs);
      const lastDay = recent.filter((r) => r.started_at_ms >= (now - DAY) * 1000);
      const latency = median(recent.filter((r) => r.ok && r.first_byte_ms !== null)
        .map((r) => r.first_byte_ms! - r.started_at_ms));
      // Listed-but-unavailable is down; otherwise the last day's calls say how it went; else listed is ok.
      const availability = found && !found.available ? 'down'
        : lastDay.length ? (lastDay.some((r) => r.ok) ? (lastDay.every((r) => r.ok) ? 'ok' : 'degraded') : 'down')
          : found ? 'ok' : 'unknown';
      let prices: UnitCosts | null = null;
      let fee = route.fee_bps;
      if (route.price_source === 'api' && found?.prices) {
        prices = found.prices;
        fee = found.fee_bps;
      } else if (route.price_source === 'builtin') {
        prices = builtinPrice(provider, route.provider_model);
        fee = 0;
      }
      const changes = prices ? diff(route, prices, fee) : [];
      const extra = found ? JSON.stringify({ ...found.extra, ...(found.name ? { listed_name: found.name } : {}) })
        : route.price_source === 'api' && listed ? JSON.stringify({ unlisted: true }) : route.extra_json;
      const confirmed = prices !== null && (route.price_source === 'builtin' || found !== null);
      statements.push(env.LICENSE_DB.prepare(
        `UPDATE ai_catalog_routes SET in_micro = ?3, cache_read_micro = ?4, cache_write_5m_micro = ?5,
           cache_write_1h_micro = ?6, out_micro = ?7, fee_bps = ?8, availability = ?9, latency_p50_ms = ?10,
           extra_json = ?11, priced_at = CASE WHEN ?12 THEN ?13 ELSE priced_at END
         WHERE model_id = ?1 AND provider = ?2`,
      ).bind(route.model_id, route.provider, prices?.in ?? route.in_micro, prices?.cache_read ?? route.cache_read_micro,
        prices?.cache_write_5m ?? route.cache_write_5m_micro, prices?.cache_write_1h ?? route.cache_write_1h_micro,
        prices?.out ?? route.out_micro, fee, availability, latency, extra, confirmed ? 1 : 0, now));
      if (found && (route.m_context === null || route.m_output === null)) {
        statements.push(env.LICENSE_DB.prepare(
          `UPDATE ai_catalog SET context_window = COALESCE(context_window, ?2), max_output = COALESCE(max_output, ?3)
           WHERE id = ?1`).bind(route.model_id, found.context_window, found.max_output));
      }
      if (changes.length) {
        entry.changes += 1;
        statements.push(eventStatement(env, now, { actor, kind: 'ai.price.changed', payload: {
          model_id: route.model_id, provider, provider_model: route.provider_model, source: route.price_source,
          changes } }));
      }
      entry.routes_updated += 1;
    }
    statements.push(env.LICENSE_DB.prepare(
      `INSERT INTO ai_provider_status (provider, checked_at, ok, error, balance_json, rate_limit_json, models_seen,
         routes_updated, changes) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
       ON CONFLICT(provider) DO UPDATE SET checked_at = ?2, ok = ?3, error = ?4, balance_json = ?5,
         rate_limit_json = ?6, models_seen = ?7, routes_updated = ?8, changes = ?9`,
    ).bind(provider, now, entry.ok ? 1 : 0, entry.error, listed?.balance ? JSON.stringify(listed.balance) : null,
      listed?.rate_limit ? JSON.stringify(listed.rate_limit) : null, entry.models_seen, entry.routes_updated,
      entry.changes));
    await env.LICENSE_DB.batch(statements);
    report[provider] = entry;
  }
  return report;
}

/** The cron step: off when AI_PRICE_REFRESH is 0. */
export async function pricingStep(env: Env, now: number): Promise<unknown> {
  if (knob(env, 'AI_PRICE_REFRESH') !== 1) return 'off';
  return refreshPricing(env, now);
}

/** Whether a route's price is overdue a refresh: only listed prices go stale. */
export function isStale(env: Env, route: Pick<CatalogRouteRow, 'price_source' | 'priced_at'>, now: number): boolean {
  if (route.price_source !== 'api') return false;
  return !route.priced_at || now - route.priced_at > knob(env, 'AI_PRICE_STALE_HOURS') * 3600;
}

