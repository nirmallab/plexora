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
 * The direct providers list their models only to a key holder, and without prices:
 *
 *   anthropic    GET /v1/models   id, display name, context, output cap and
 *                                 capabilities (vision, tools, structured
 *                                 output, thinking, effort levels)
 *   openai       GET /v1/models   ids only
 *
 * so a direct model's price is, in order: Anthropic's list price from code
 * (catalog.ANTHROPIC_LIST_PRICES, `builtin`), else the same model on
 * OpenRouter's list at fee 0 (OpenRouter passes list prices through; its fee is
 * on credits), recorded as a listed price (`api`) with the reference in
 * `extra_json`, else none: the route is added unpriced and switched off
 * (catalog_store.unpricedPricing). A price an admin typed (`manual`) is never
 * overwritten; a listed price (`api`) is refreshed by the nightly cron (the
 * `pricing` step) or on demand, and every change is one `ai.price.changed`
 * event, which is the price history.
 *
 * The refresh also confirms availability (listed, and the last day's calls),
 * fills a model's context window where nobody set one, and records each
 * route's observed first-byte latency, which a task's latency preference reads.
 */
import { all } from '../db';
import { DAY, type Env, knob } from '../env';
import { eventStatement } from '../events';
import { ANTHROPIC_PRICING_URL, BUILTIN_PRICES, type UnitCosts } from './catalog';
import {
  builtinProfile, canonicalModelId, type EffortLevel, type EffortWire, isLevel, LEVELS, sortLevels,
} from './effort';
import { configured, LABELS, type Provider, providerFetch, PROVIDERS, SPECS } from './providers';
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
  /** The effort levels the list says the model takes (OpenRouter's `reasoning.supported_efforts`, Anthropic's
   * `capabilities.effort`), when it says, and how it takes them when the list knows. */
  efforts: { levels: EffortLevel[]; default: EffortLevel | null; wire?: EffortWire } | null;
  available: boolean;
  /** Where `prices` came from: the provider's own list, a built-in list price, or another provider's list
   * (OpenRouter's, for a direct provider whose list has no prices); null when unpriced. */
  price_from: 'listing' | 'builtin' | 'reference' | null;
  /** Where that price can be read. */
  price_url: string | null;
  /** The model family, when the list says (a direct provider's own models). */
  family?: string;
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
    efforts: effortsOf(m.reasoning), available: true, extra, price_from: prices ? 'listing' : null, price_url: null,
  };
}

/** OpenRouter's `reasoning: {supported_efforts, default_effort}`, as canonical levels. */
function effortsOf(raw: unknown): Listing['efforts'] {
  const r = raw && typeof raw === 'object' ? raw as Record<string, unknown> : null;
  if (!r || !Array.isArray(r.supported_efforts)) return null;
  const levels = sortLevels(r.supported_efforts.filter(isLevel));
  return levels.length ? { levels, default: isLevel(r.default_effort) ? r.default_effort : null } : null;
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
    efforts: null,
    available: m.available !== false && m.coming_soon !== true,
    extra: { ...(surcharges ? { surcharges } : {}), ...(m.pricing?.basis ? { basis: m.pricing.basis } : {}) },
    price_from: input === null && output === null ? null : 'listing', price_url: null,
  };
}

/** A direct provider's list needs its key; without one there is nothing to read. */
export class NotConnected extends Error {
  constructor(readonly provider: Provider) {
    super(`${LABELS[provider]} is not connected: set its API key on the Providers page to read its model list.`);
  }
}

/** OpenRouter's list, as the price reference for a direct provider's models; null when it cannot be read. */
async function reference(env: Env): Promise<ProviderListing | null> {
  try {
    return await listing(env, 'openrouter');
  } catch {
    return null;
  }
}

/** A direct model on OpenRouter's list: `vendor/id` exactly, else the same canonical id among the vendor's priced
 * models (`claude-opus-4-1-20250805` -> `anthropic/claude-opus-4.1`). A variant (`:thinking`, `:free`) never
 * matches, so a free tier is never taken for the paid price. */
export function referenced(ref: ProviderListing | null, vendor: string, id: string): Listing | null {
  if (!ref) return null;
  const exact = ref.models.find((m) => m.id === `${vendor}/${id}`);
  if (exact?.prices) return exact;
  const want = canonicalModelId(id);
  return ref.models.find((m) => m.id.startsWith(`${vendor}/`) && !m.id.includes(':') && m.prices &&
    canonicalModelId(m.id) === want) ?? null;
}

function priceFromReference(ref: ProviderListing, hit: Listing): Pick<Listing, 'prices' | 'fee_bps' | 'price_from' |
  'price_url' | 'extra'> {
  return { prices: hit.prices, fee_bps: 0, price_from: 'reference', price_url: ref.url,
    extra: { reference: { provider: 'openrouter', id: hit.id } } };
}

const supported = (caps: Record<string, any>, name: string): boolean | null =>
  typeof caps?.[name]?.supported === 'boolean' ? caps[name].supported : null;

/** One row of Anthropic's `GET /v1/models`, priced from the list-price table or the reference. */
function anthropicListing(m: Record<string, any>, ref: ProviderListing | null): Listing {
  const id = String(m.id);
  const caps = (m.capabilities ?? {}) as Record<string, any>;
  const levels = caps.effort?.supported ? sortLevels(LEVELS.filter((l) => caps.effort?.[l]?.supported === true)) : [];
  const builtin = builtinPrice('anthropic', id);
  const hit = builtin ? null : referenced(ref, 'anthropic', id);
  const price = builtin
    ? { prices: builtin, fee_bps: 0, price_from: 'builtin' as const, price_url: ANTHROPIC_PRICING_URL, extra: {} }
    : hit && ref ? priceFromReference(ref, hit)
      : { prices: null, fee_bps: 0, price_from: null, price_url: null, extra: {} };
  return {
    id, name: typeof m.display_name === 'string' ? m.display_name : null, ...price,
    context_window: num(m.max_input_tokens), max_output: num(m.max_tokens),
    vision: supported(caps, 'image_input'), tools: supported(caps, 'tool_use'),
    structured: supported(caps, 'structured_outputs'), reasoning: supported(caps, 'thinking'),
    efforts: levels.length ? { levels, default: null, wire: 'effort' } : null,
    available: true, family: 'Claude',
  };
}

/** A chat model on OpenAI's list: not audio, speech, images, embeddings, moderation or the legacy completions. */
export function isOpenAiChatModel(id: string): boolean {
  return /^(gpt-|o[134](-|$)|chatgpt-)/.test(id) &&
    !/(audio|realtime|tts|transcribe|embedding|image|dall-e|whisper|moderation|instruct|search-preview|computer-use)/
      .test(id);
}

/** One row of OpenAI's `GET /v1/models` (ids only): abilities and price from OpenRouter's listing of it. */
function openaiListing(id: string, ref: ProviderListing | null): Listing {
  const hit = referenced(ref, 'openai', id);
  const reasoning = hit?.reasoning ?? (builtinProfile(id) ? true : null);
  return {
    id, name: hit?.name ? hit.name.replace(/^[^:]{1,40}:\s+/, '') : null,
    ...(hit && ref ? priceFromReference(ref, hit)
      : { prices: null, fee_bps: 0, price_from: null, price_url: null, extra: {} }),
    context_window: hit?.context_window ?? null, max_output: hit?.max_output ?? null,
    vision: hit?.vision ?? null, tools: hit?.tools ?? null, structured: hit?.structured ?? null, reasoning,
    efforts: null, available: true, family: 'OpenAI',
  };
}

type Adapter = (env: Env) => Promise<ProviderListing>;

export const ADAPTERS: Partial<Record<Provider, Adapter>> = {
  anthropic: async (env) => {
    if (!configured(env, 'anthropic')) throw new NotConnected('anthropic');
    const base = baseOf(env, 'anthropic');
    const url = `${base}/v1/models`;
    const headers = SPECS.anthropic.headers(String(env.ANTHROPIC_API_KEY));
    const rows: Array<Record<string, any>> = [];
    let after: string | null = null;
    for (let page = 0; page < 5; page += 1) {
      const body = await getJson(`${url}?limit=1000${after ? `&after_id=${encodeURIComponent(after)}` : ''}`, headers);
      rows.push(...(Array.isArray(body?.data) ? body.data : []));
      if (!body?.has_more || !body?.last_id) break;
      after = String(body.last_id);
    }
    const ref = rows.some((m) => !builtinPrice('anthropic', String(m.id))) ? await reference(env) : null;
    return { provider: 'anthropic', url, balance: null, rate_limit: null,
      models: rows.map((m) => anthropicListing(m, ref)) };
  },
  openai: async (env) => {
    if (!configured(env, 'openai')) throw new NotConnected('openai');
    const url = `${baseOf(env, 'openai')}/v1/models`;
    const body = await getJson(url, SPECS.openai.headers(String(env.OPENAI_API_KEY)));
    const ids: string[] = (Array.isArray(body?.data) ? body.data : []).map((m: any) => String(m?.id ?? ''))
      .filter(isOpenAiChatModel);
    // A dated snapshot is hidden when its alias is listed: the alias is what one approves.
    const shown = ids.filter((id) => canonicalModelId(id) === id.replace(/\./g, '-') || !ids.some((other) =>
      other !== id && other.replace(/\./g, '-') === canonicalModelId(id)));
    const ref = await reference(env);
    return { provider: 'openai', url, balance: null, rate_limit: null,
      models: shown.map((id) => openaiListing(id, ref)) };
  },
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
      models: (Array.isArray(body?.data) ? body.data : []).map((m: any) =>
        ({ ...perTokenListing(m, OPENROUTER_FEE_BPS), price_url: url })) };
  },
  orcarouter: async (env) => {
    const url = `${baseOf(env, 'orcarouter')}/v1/models`;
    const body = await getJson(url);
    return { provider: 'orcarouter', url, balance: null, rate_limit: null,
      models: (Array.isArray(body?.data) ? body.data : []).map((m: any) => ({ ...perTokenListing(m, 0, true),
        price_url: url })) };
  },
  saygm: async (env) => {
    const url = `${baseOf(env, 'saygm')}/v1/models`;
    const body = await getJson(url);
    return { provider: 'saygm', url, balance: null, rate_limit: null,
      models: (Array.isArray(body?.data) ? body.data : []).map((m: any) => ({ ...saygmListing(m), price_url: url })) };
  },
};

/** Whether a provider's own list carries prices: the aggregators'. A direct provider's price is a list price or a
 * reference. */
export const listsPrices = (provider: Provider): boolean => !SPECS[provider].direct;

/** Whether a provider's model list can be read now: an aggregator's always (it is public), a direct provider's
 * once its key is set. */
export const canList = (env: Env, provider: Provider): boolean =>
  ADAPTERS[provider] !== undefined && (!SPECS[provider].direct || configured(env, provider));

const cache = new Map<Provider, { at: number; listing: ProviderListing }>();
/** A list that just failed, so a page of searches does not ask a down provider again for a minute. */
const failed = new Map<Provider, { at: number; error: unknown }>();

/** A provider's listing, reused for ten minutes per isolate unless `fresh`. */
export async function listing(env: Env, provider: Provider, fresh = false): Promise<ProviderListing | null> {
  const adapter = ADAPTERS[provider];
  if (!adapter) return null;
  const hit = cache.get(provider);
  if (!fresh && hit && Date.now() - hit.at < 10 * 60 * 1000) return hit.listing;
  const miss = failed.get(provider);
  if (!fresh && miss && Date.now() - miss.at < 60 * 1000) throw miss.error;
  try {
    const got = await adapter(env);
    cache.set(provider, { at: Date.now(), listing: got });
    failed.delete(provider);
    return got;
  } catch (error) {
    if (!(error instanceof NotConnected)) failed.set(provider, { at: Date.now(), error });
    throw error;
  }
}

export function clearListingCache(): void {
  cache.clear();
  failed.clear();
}

/** A built-in list price, for a provider without a price API: by the wire id, else its canonical id (a dated
 * snapshot has its alias's price). */
export function builtinPrice(provider: Provider, providerModel: string): UnitCosts | null {
  const table = BUILTIN_PRICES[provider];
  return table?.[providerModel] ?? table?.[canonicalModelId(providerModel)] ?? null;
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
  /** Whether its list was read: a direct provider's is not without its key (and its routes are still checked). */
  listed: boolean;
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
  // The aggregators first: a direct provider's list reads OpenRouter's as its price reference, fresh by then.
  const providers = PROVIDERS.filter((p) => routes.some((r) => r.provider === p) || only.provider === p)
    .sort((a, b) => Number(SPECS[a].direct) - Number(SPECS[b].direct));
  const sinceMs = (now - 7 * DAY) * 1000;
  const report: Record<string, ProviderReport> = {};
  let referenceFresh = false;
  for (const provider of providers) {
    const mine = routes.filter((r) => r.provider === provider);
    const attempted = canList(env, provider);
    const entry: ProviderReport = { ok: true, listed: attempted, error: null, models_seen: 0, routes_updated: 0,
      changes: 0 };
    let listed: ProviderListing | null = null;
    if (attempted) {
      try {
        // A direct provider's reference prices are OpenRouter's: read that list fresh first, once per refresh.
        if (SPECS[provider].direct && !referenceFresh) await listing(env, 'openrouter', true).catch(() => null);
        if (provider === 'openrouter' || SPECS[provider].direct) referenceFresh = true;
        listed = await listing(env, provider, true);
        entry.models_seen = listed?.models.length ?? 0;
      } catch (error) {
        entry.ok = false;
        entry.error = String((error as Error)?.message ?? error).slice(0, 300);
      }
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
    // A direct provider without a key has no list to have failed: no status row, so no "could not be read".
    if (attempted) statements.push(env.LICENSE_DB.prepare(
      `INSERT INTO ai_provider_status (provider, checked_at, ok, error, balance_json, rate_limit_json, models_seen,
         routes_updated, changes) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
       ON CONFLICT(provider) DO UPDATE SET checked_at = ?2, ok = ?3, error = ?4, balance_json = ?5,
         rate_limit_json = ?6, models_seen = ?7, routes_updated = ?8, changes = ?9`,
    ).bind(provider, now, entry.ok ? 1 : 0, entry.error, listed?.balance ? JSON.stringify(listed.balance) : null,
      listed?.rate_limit ? JSON.stringify(listed.rate_limit) : null, entry.models_seen, entry.routes_updated,
      entry.changes));
    if (statements.length) await env.LICENSE_DB.batch(statements);
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

