/**
 * The providers the gateway can route a capability to, and how each one is
 * called. Three wire formats cover all of them:
 *
 *   anthropic          Anthropic Messages: Anthropic itself, OrcaRouter and
 *                      SayGM (both serve a native /v1/messages). The envelope
 *                      is Anthropic-shaped, so this is a near passthrough.
 *   openai_responses   OpenAI's Responses API, translated (translate.ts).
 *   openai_chat        Chat Completions, translated: OpenRouter.
 *
 * Whatever the wire, the client always receives Anthropic-shaped events, so a
 * route change never touches the client.
 *
 * `direct` providers are Plexora's own accounts with the model's maker; only
 * they may serve a default (feature '*') route without a passing routing-bench
 * evaluation. Aggregators must earn each (feature, capability) they serve.
 */
import type { Env } from '../env';
import type { Route } from './catalog';
import { toChat, toResponses } from './translate';
import { anthropicBody, type Envelope } from './upstream';

export const PROVIDERS = ['anthropic', 'openai', 'openrouter', 'orcarouter', 'saygm'] as const;
export type Provider = (typeof PROVIDERS)[number];
export type Wire = 'anthropic' | 'openai_responses' | 'openai_chat';

interface ProviderSpec {
  wire: Wire;
  direct: boolean;
  base: string;
  baseVar: string;
  keyVar: string;
  path: string;
  /** A model this provider may serve; SayGM is admitted for its confidential (TEE) tier only. */
  admits?: (model: string) => boolean;
  headers: (key: string) => Record<string, string>;
}

export const ANTHROPIC_VERSION = '2023-06-01';

export const SPECS: Record<Provider, ProviderSpec> = {
  anthropic: {
    wire: 'anthropic', direct: true, base: 'https://api.anthropic.com', baseVar: 'ANTHROPIC_BASE_URL',
    keyVar: 'ANTHROPIC_API_KEY', path: '/v1/messages',
    headers: (key) => ({ 'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION }),
  },
  openai: {
    wire: 'openai_responses', direct: true, base: 'https://api.openai.com', baseVar: 'OPENAI_BASE_URL',
    keyVar: 'OPENAI_API_KEY', path: '/v1/responses',
    headers: (key) => ({ authorization: `Bearer ${key}` }),
  },
  openrouter: {
    wire: 'openai_chat', direct: false, base: 'https://openrouter.ai/api', baseVar: 'OPENROUTER_BASE_URL',
    keyVar: 'OPENROUTER_API_KEY', path: '/v1/chat/completions',
    headers: (key) => ({ authorization: `Bearer ${key}`, 'http-referer': 'https://plexoraapp.com',
      'x-title': 'Plexora' }),
  },
  orcarouter: {
    wire: 'anthropic', direct: false, base: 'https://api.orcarouter.ai', baseVar: 'ORCAROUTER_BASE_URL',
    keyVar: 'ORCAROUTER_API_KEY', path: '/v1/messages',
    headers: (key) => ({ 'x-api-key': key, authorization: `Bearer ${key}`, 'anthropic-version': ANTHROPIC_VERSION,
      'x-orcarouter-include-cost': 'true' }),
  },
  saygm: {
    wire: 'anthropic', direct: false, base: 'https://api.saygm.com', baseVar: 'SAYGM_BASE_URL',
    keyVar: 'SAYGM_API_KEY', path: '/v1/messages',
    // Frontier models reach SayGM's upstream on an anonymous operator's key,
    // which Plexora's provider obligations cannot allow; only its
    // confidential open-weight models (served inside an enclave) are routed.
    admits: (model) => /-tee$/i.test(model),
    headers: (key) => ({ 'x-api-key': key, authorization: `Bearer ${key}`, 'anthropic-version': ANTHROPIC_VERSION }),
  },
};

export function isProvider(value: unknown): value is Provider {
  return typeof value === 'string' && (PROVIDERS as readonly string[]).includes(value);
}

export function admits(provider: Provider, model: string): boolean {
  const rule = SPECS[provider].admits;
  return rule ? rule(model) : true;
}

/** Test seam: route tests stand in for every provider without a network. */
type Fetcher = (input: string, init: RequestInit) => Promise<Response>;
let fetcher: Fetcher | null = null;
export function setUpstreamFetch(fn: Fetcher | null): void {
  fetcher = fn;
}

export interface CallOptions {
  user: string;
  /** A stable, non-identifying key for provider-side cache affinity. */
  cacheKey: string;
  signal?: AbortSignal;
}

export function buildBody(route: Route, envelope: Envelope, options: CallOptions): Record<string, unknown> {
  switch (SPECS[route.provider].wire) {
    case 'anthropic':
      return anthropicBody(route, route.model, envelope, options.user);
    case 'openai_responses':
      return toResponses(route, envelope, options.user, options.cacheKey);
    case 'openai_chat':
      return toChat(route, envelope, options.user, options.cacheKey);
  }
}

export function configured(env: Env, provider: Provider): boolean {
  return typeof env[SPECS[provider].keyVar] === 'string' && String(env[SPECS[provider].keyVar]).length > 0;
}

export async function callProvider(env: Env, route: Route, body: Record<string, unknown>,
  signal?: AbortSignal): Promise<Response> {
  const spec = SPECS[route.provider];
  const base = String(env[spec.baseVar] || spec.base).replace(/\/$/, '');
  const key = String(env[spec.keyVar] ?? '');
  const init: RequestInit = {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'text/event-stream', ...spec.headers(key) },
    body: JSON.stringify(body),
    signal,
  };
  return (fetcher ?? ((input: string, i: RequestInit) => fetch(input, i)))(`${base}${spec.path}`, init);
}
