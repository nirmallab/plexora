/**
 * What the gateway sells and what it costs us: capability classes, the
 * built-in models and routes, provider unit costs, and the flat feature prices.
 *
 * The client names a TASK (and, for older gateways, a capability class), never
 * a model: which model serves it is Plexora's decision. Administrators approve
 * models (ai_catalog), give each up to three provider routes, and assign
 * models to tasks (routing.ts). With nothing assigned, a call is served by the
 * built-in ROUTES below. An assignment to an aggregator-served model must carry
 * a passing routing-bench evaluation (BENCH) unless the Worker allows
 * unbenched routes. Only the dev route (internal testing) may name a model,
 * and only a catalogued one.
 *
 * Money is micro-USD; unit costs are micro-USD per 1M tokens (so $4/MTok is
 * 4_000_000). Every ai_requests row copies the unit costs it was charged at,
 * so history survives a price change.
 */

import { builtinProfile, type EffortLevel, type EffortProfile, type EffortWire } from './effort';
import type { Provider } from './providers';

export const CAPABILITIES = ['vision_judgement', 'vision_routine', 'text_routine', 'text_reasoning'] as const;
export type Capability = (typeof CAPABILITIES)[number];

export interface UnitCosts {
  in: number;
  cache_read: number;
  cache_write_5m: number;
  cache_write_1h: number;
  out: number;
}

/** USD per 1M tokens as UnitCosts: cache writes at Anthropic's ratios (5 minutes 1.25x, 1 hour 2x input). */
const usd = (input: number, cacheRead: number, output: number): UnitCosts => ({
  in: Math.round(input * 1e6), cache_read: Math.round(cacheRead * 1e6), cache_write_5m: Math.round(input * 1.25e6),
  cache_write_1h: Math.round(input * 2e6), out: Math.round(output * 1e6) });

/**
 * Anthropic list prices by canonical model id (no date suffix; effort.ts
 * `canonicalModelId`). Anthropic's model list carries no prices, so these are
 * the 'builtin' prices of an anthropic route, and of a Claude model found on
 * Anthropic's list. Update them when Anthropic's pricing page changes.
 */
export const ANTHROPIC_PRICES_AS_OF = '2026-10-03';
export const ANTHROPIC_LIST_PRICES: Record<string, UnitCosts> = {
  'claude-fable-5-1': usd(10, 0.25, 50),
  'claude-fable-5': usd(10, 1, 50),
  'claude-opus-5-5': usd(4, 0.2, 20),
  'claude-opus-5': usd(5, 0.5, 25),
  'claude-opus-4-8': usd(5, 0.5, 25),
  'claude-opus-4-7': usd(5, 0.5, 25),
  'claude-opus-4-6': usd(5, 0.5, 25),
  'claude-sonnet-5-5': usd(2, 0.2, 10),
  'claude-sonnet-5': usd(2, 0.2, 10),
  'claude-sonnet-4-6': usd(3, 0.3, 15),
  'claude-haiku-4-5': usd(1, 0.1, 5),
};

/** The built-in models' unit costs by their exact wire id (the built-in routes and the legacy table read
 * these by key). */
export const COSTS: Record<string, UnitCosts> = {
  'claude-opus-5-5': ANTHROPIC_LIST_PRICES['claude-opus-5-5']!,
  'claude-sonnet-5': ANTHROPIC_LIST_PRICES['claude-sonnet-5']!,
  'claude-haiku-4-5-20251001': ANTHROPIC_LIST_PRICES['claude-haiku-4-5']!,
};

/** Where a call's routes came from: an assignment at that level, the built-in default, the legacy route
 * table (until migrated), or the dev route's named model. */
export type Level = 'task' | 'module' | 'global' | 'builtin' | 'legacy' | 'dev';

export interface Route {
  /** `<model_id>@<provider>` for a catalogue route, `builtin:<capability>` for the defaults below, a legacy
   * `ai_routes` id, or `dev:<provider>/<model>`. */
  id: string;
  provider: Provider;
  /** The model id on the provider's wire. */
  model: string;
  /** The approved model this route reaches: one model, whichever provider serves it. */
  model_id: string;
  level: Level;
  /** The effort level this route sends, already fitted to its model (effort.ts), or null for none. Fixed per
   * task and model, so a worker's cache never changes under it. */
  effort: EffortLevel | null;
  /** How the model takes it (effort.ts `EffortWire`); absent: looked up from the built-in profiles. */
  effort_wire?: EffortWire;
  effort_budgets?: EffortProfile['budgets'];
  max_tokens_cap: number;
  /** When a failure moves the call to the next route: only on an open circuit
   * (default: a transient error does not throw away a warm prompt cache), on
   * any retryable error, or never. */
  failover: 'outage' | 'error' | 'never';
}

const builtin = (capability: Capability, model: string, effort: Route['effort'], cap: number): Route =>
  ({ id: `builtin:${capability}`, provider: 'anthropic', model, model_id: model, level: 'builtin', effort,
    effort_wire: builtinProfile(model)?.wire, max_tokens_cap: cap, failover: 'outage' });

export const ROUTES: Record<Capability, Route> = {
  vision_judgement: builtin('vision_judgement', 'claude-opus-5-5', 'medium', 16000),
  vision_routine: builtin('vision_routine', 'claude-sonnet-5', 'medium', 16000),
  text_routine: builtin('text_routine', 'claude-haiku-4-5-20251001', null, 8000),
  text_reasoning: builtin('text_reasoning', 'claude-sonnet-5', 'medium', 32000),
};

/** The models the built-in routes name, as approved models (`POST /catalog/seed-builtin` writes them). */
export interface BuiltinModel {
  id: string;
  name: string;
  family: string;
  vision: boolean;
  tools: boolean;
  structured: boolean;
  reasoning: boolean;
}

export const BUILTIN_MODELS: BuiltinModel[] = [
  { id: 'claude-opus-5-5', name: 'Claude Opus 5.5', family: 'Claude', vision: true, tools: true, structured: true,
    reasoning: true },
  { id: 'claude-sonnet-5', name: 'Claude Sonnet 5', family: 'Claude', vision: true, tools: true, structured: true,
    reasoning: true },
  { id: 'claude-haiku-4-5-20251001', name: 'Claude Haiku 4.5', family: 'Claude', vision: true, tools: true,
    structured: true, reasoning: false },
];

/** List prices for providers without a price API, by provider and canonical model id (pricing.builtinPrice). */
export const BUILTIN_PRICES: Partial<Record<Provider, Record<string, UnitCosts>>> = {
  anthropic: ANTHROPIC_LIST_PRICES,
};

export const ANTHROPIC_PRICING_URL = 'https://www.anthropic.com/pricing#api';

/**
 * The routing bench's bar per module. A route other than a direct provider's
 * default serves a feature only with an evaluation, on the CURRENT bench
 * version, that meets every threshold here; the gateway computes `passed`
 * from the metrics, it never takes the submitter's word. `min` metrics must
 * be at least the value, `max` at most.
 */
export interface Bench {
  version: string;
  min: Record<string, number>;
  max: Record<string, number>;
}

export const BENCH: Record<string, Bench> = {
  gating: { version: 'gating-1', min: { code_agreement: 0.98, marker_f1: 0.95, cache_hit_ratio: 0.8 },
    max: { invalid_answer_rate: 0.01, failure_rate: 0.005 } },
  qc: { version: 'qc-1', min: { artifact_precision: 0.9, artifact_recall: 0.9, cache_hit_ratio: 0.8 },
    max: { invalid_answer_rate: 0.01, failure_rate: 0.005 } },
  transfer: { version: 'transfer-1', min: { code_agreement: 0.98 },
    max: { invalid_answer_rate: 0.01, failure_rate: 0.005 } },
  look: { version: 'look-1', min: { agreement: 0.9 }, max: { failure_rate: 0.005 } },
  chat: { version: 'chat-1', min: { task_success: 0.9 }, max: { failure_rate: 0.01 } },
  '*': { version: 'default-1', min: { agreement: 0.95 }, max: { invalid_answer_rate: 0.01, failure_rate: 0.005 } },
};

export function benchPasses(bench: Bench, metrics: Record<string, unknown>): { passed: boolean; misses: string[] } {
  const misses: string[] = [];
  for (const [name, bar] of Object.entries(bench.min)) {
    const value = metrics[name];
    if (typeof value !== 'number' || !(value >= bar)) misses.push(`${name} < ${bar}`);
  }
  for (const [name, bar] of Object.entries(bench.max)) {
    const value = metrics[name];
    if (typeof value !== 'number' || !(value <= bar)) misses.push(`${name} > ${bar}`);
  }
  return { passed: misses.length === 0, misses };
}

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

/** What the provider charges us; `fee_bps` is an aggregator's fee on top of list price (OpenRouter 550). */
export function costMicro(usage: Usage, unit: UnitCosts & { fee_bps?: number }): number {
  const total = usage.input_uncached * unit.in + usage.cache_read * unit.cache_read +
    usage.cache_write_5m * unit.cache_write_5m + usage.cache_write_1h * unit.cache_write_1h +
    usage.output_tokens * unit.out;
  return Math.ceil((total / 1_000_000) * (1 + (unit.fee_bps ?? 0) / 10_000));
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
