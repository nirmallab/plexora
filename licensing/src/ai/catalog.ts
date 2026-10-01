/**
 * What the gateway sells and what it costs us: capability classes, the route
 * each one takes, provider unit costs, and the flat feature prices.
 *
 * The client names a CAPABILITY, never a model: which model serves a class is
 * Plexora's decision and lives here. Only the dev route (internal testing) may
 * name a model, and only one in COSTS.
 *
 * Money is micro-USD; unit costs are micro-USD per 1M tokens (so $4/MTok is
 * 4_000_000). Effective-dated price tables in D1 are the later step; until
 * then a price change is a deploy, and every ai_requests row copies the unit
 * costs it was charged at.
 */

export const CAPABILITIES = ['vision_judgement', 'vision_routine', 'text_routine', 'text_reasoning'] as const;
export type Capability = (typeof CAPABILITIES)[number];

export interface UnitCosts {
  in: number;
  cache_read: number;
  cache_write_5m: number;
  cache_write_1h: number;
  out: number;
}

/** Anthropic list prices (Claude API, 2026-10-01). */
export const COSTS: Record<string, UnitCosts> = {
  'claude-opus-5-5': { in: 4_000_000, cache_read: 200_000, cache_write_5m: 5_000_000, cache_write_1h: 8_000_000,
    out: 20_000_000 },
  'claude-sonnet-5': { in: 2_000_000, cache_read: 200_000, cache_write_5m: 2_500_000, cache_write_1h: 4_000_000,
    out: 10_000_000 },
  'claude-haiku-4-5-20251001': { in: 1_000_000, cache_read: 100_000, cache_write_5m: 1_250_000,
    cache_write_1h: 2_000_000, out: 5_000_000 },
};

export interface Route {
  provider: 'anthropic';
  model: string;
  /** `output_config.effort`, fixed per class so a worker's cache never changes under it. */
  effort: 'low' | 'medium' | 'high' | null;
  max_tokens_cap: number;
}

export const ROUTES: Record<Capability, Route> = {
  vision_judgement: { provider: 'anthropic', model: 'claude-opus-5-5', effort: 'medium', max_tokens_cap: 16000 },
  vision_routine: { provider: 'anthropic', model: 'claude-sonnet-5', effort: 'medium', max_tokens_cap: 16000 },
  text_routine: { provider: 'anthropic', model: 'claude-haiku-4-5-20251001', effort: null, max_tokens_cap: 8000 },
  text_reasoning: { provider: 'anthropic', model: 'claude-sonnet-5', effort: 'medium', max_tokens_cap: 32000 },
};

/** The capability classes an entitlement list unlocks. */
export function capabilitiesFor(entitlements: string[]): Capability[] {
  if (entitlements.includes('ai')) return [...CAPABILITIES];
  const caps = new Set<Capability>();
  for (const e of entitlements) {
    if (e === 'ai:gating' || e.startsWith('ai:gating:') || e === 'ai:qc' || e.startsWith('ai:qc:')) {
      caps.add('vision_judgement');
      caps.add('vision_routine');
      caps.add('text_routine');
    }
    if (e === 'ai:chat') {
      caps.add('text_routine');
      caps.add('text_reasoning');
    }
  }
  return CAPABILITIES.filter((c) => caps.has(c));
}

export interface Usage {
  input_uncached: number;
  cache_read: number;
  cache_write_5m: number;
  cache_write_1h: number;
  output_tokens: number;
}

export const ZERO_USAGE: Usage = { input_uncached: 0, cache_read: 0, cache_write_5m: 0, cache_write_1h: 0,
  output_tokens: 0 };

export function costMicro(usage: Usage, unit: UnitCosts): number {
  const total = usage.input_uncached * unit.in + usage.cache_read * unit.cache_read +
    usage.cache_write_5m * unit.cache_write_5m + usage.cache_write_1h * unit.cache_write_1h +
    usage.output_tokens * unit.out;
  return Math.ceil(total / 1_000_000);
}

export function withMarkup(micro: number, markupBps: number): number {
  return Math.ceil((micro * markupBps) / 10_000);
}

/**
 * Flat prices for declared runs. `expected_cost_micro` is the measured
 * Premium-class cost per unit (bounded workers, 5-minute cache) and sizes the
 * dev-route cap; `calls_per_unit` + `base_calls` is the envelope a run's
 * declared units pay for.
 */
export interface FeaturePrice {
  unit: string;
  price_micro: number;
  expected_cost_micro: number;
  calls_per_unit: number;
  base_calls: number;
}

export const FEATURES: Record<string, FeaturePrice> = {
  gating: { unit: 'marker', price_micro: 250_000, expected_cost_micro: 124_000, calls_per_unit: 6, base_calls: 25 },
  transfer: { unit: 'marker_image', price_micro: 40_000, expected_cost_micro: 16_000, calls_per_unit: 2,
    base_calls: 5 },
  qc: { unit: 'channel', price_micro: 120_000, expected_cost_micro: 58_000, calls_per_unit: 3, base_calls: 10 },
  look: { unit: 'look', price_micro: 100_000, expected_cost_micro: 45_000, calls_per_unit: 2, base_calls: 1 },
};

/** Chat and other open-ended calls: the per-call hold before usage is known. */
export const CHARS_PER_TOKEN = 3.3;
export const TOKENS_PER_IMAGE = 1600;

/** A deliberately high estimate of one call's metered price, for its hold. */
export function estimateMicro(textChars: number, images: number, maxTokens: number, unit: UnitCosts,
  markupBps: number): number {
  const input = Math.ceil(textChars / CHARS_PER_TOKEN) + images * TOKENS_PER_IMAGE;
  return withMarkup(Math.ceil((input * unit.cache_write_5m + maxTokens * unit.out) / 1_000_000), markupBps);
}
