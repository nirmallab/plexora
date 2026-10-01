/**
 * The provider side: turn a validated Plexora envelope into the provider's
 * request, and read the provider's usage back out of its event stream.
 *
 * The envelope is Anthropic-Messages-shaped, so the Anthropic wire is a near
 * passthrough: the model, effort and structured-output format come from the
 * route, the end-user id is HMAC'd in, and nothing the client sent outside the
 * allowlist survives validation (routes/ai.ts). Other wires are translated in
 * translate.ts; providers.ts says which provider speaks which.
 */
import { hmacHex } from '../crypto';
import type { Env } from '../env';
import { type Route, type Usage, ZERO_USAGE } from './catalog';

export interface Envelope {
  system?: unknown;
  messages: unknown[];
  tools?: unknown[];
  max_tokens: number;
  stop_sequences?: string[];
  output_schema?: Record<string, unknown>;
}

export async function userHash(env: Env, accountId: string, userId: string | null): Promise<string> {
  const pepper = env.AI_USER_PEPPER || env.IP_HASH_KEY || 'plexora-ai';
  return (await hmacHex(pepper, `${accountId}:${userId ?? '-'}`)).slice(0, 32);
}

export function anthropicBody(route: Route, model: string, envelope: Envelope, user: string): Record<string, unknown> {
  const outputConfig: Record<string, unknown> = {};
  if (route.effort) outputConfig.effort = route.effort;
  if (envelope.output_schema) outputConfig.format = { type: 'json_schema', schema: envelope.output_schema };
  return {
    model,
    max_tokens: Math.min(envelope.max_tokens, route.max_tokens_cap),
    ...(envelope.system !== undefined ? { system: envelope.system } : {}),
    messages: envelope.messages,
    ...(envelope.tools?.length ? { tools: envelope.tools } : {}),
    ...(envelope.stop_sequences?.length ? { stop_sequences: envelope.stop_sequences } : {}),
    ...(Object.keys(outputConfig).length ? { output_config: outputConfig } : {}),
    metadata: { user_id: user },
    stream: true,
  };
}

/** What a provider failure means for the caller. */
export function classify(status: number): { code: 'provider_rate_limited' | 'provider_unavailable' |
  'provider_rejected'; http: 429 | 503 | 400; failure: string; retryable: boolean } {
  if (status === 429) return { code: 'provider_rate_limited', http: 429, failure: 'provider_429', retryable: true };
  if (status >= 500 || status === 408 || status === 529 || status === 0) {
    return { code: 'provider_unavailable', http: 503, failure: 'provider_5xx', retryable: true };
  }
  // 401/403 from a provider is OUR key or account, not the caller's request.
  if (status === 401 || status === 403) {
    return { code: 'provider_unavailable', http: 503, failure: 'provider_auth', retryable: false };
  }
  return { code: 'provider_rejected', http: 400, failure: 'provider_4xx', retryable: false };
}

export interface Metered {
  usage: Usage;
  source: 'provider' | 'partial' | 'none';
  stop_reason: string | null;
  provider_request_id: string | null;
  complete: boolean;
}

/**
 * An incremental reader of an Anthropic SSE stream that keeps only the usage
 * numbers, the stop reason and the message id. It never keeps text.
 */
export class UsageMeter {
  private buffer = '';
  private captured = '';
  private answeredBy: string | null = null;
  private reported: number | null = null;
  private usage: Usage = { ...ZERO_USAGE };
  private started = false;
  private stopped = false;
  private stopReason: string | null = null;
  private messageId: string | null = null;
  private readonly decoder = new TextDecoder();

  /** `capture` > 0 keeps up to that many characters of answer text (shadow comparison only). */
  constructor(private readonly capture = 0) {}

  push(chunk: Uint8Array): void {
    this.buffer += this.decoder.decode(chunk, { stream: true });
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index).replace(/\r$/, '');
      this.buffer = this.buffer.slice(index + 1);
      if (line.startsWith('data:')) this.data(line.slice(5).trim());
      index = this.buffer.indexOf('\n');
    }
  }

  private data(text: string): void {
    if (!text || text === '[DONE]') return;
    let event: Record<string, any>;
    try {
      event = JSON.parse(text);
    } catch {
      return;
    }
    if (event.type === 'message_start' && event.message) {
      this.started = true;
      this.messageId = typeof event.message.id === 'string' ? event.message.id : null;
      if (typeof event.message.model === 'string') this.answeredBy = event.message.model;
      this.absorb(event.message.usage);
    } else if (event.type === 'content_block_delta') {
      const text = event.delta?.type === 'text_delta' ? event.delta.text : null;
      if (this.capture && typeof text === 'string' && this.captured.length < this.capture) this.captured += text;
    } else if (event.type === 'message_delta') {
      if (event.delta && typeof event.delta.stop_reason === 'string') this.stopReason = event.delta.stop_reason;
      this.absorb(event.usage);
    } else if (event.type === 'message_stop') {
      this.stopped = true;
    }
  }

  private absorb(raw: unknown): void {
    if (!raw || typeof raw !== 'object') return;
    const u = raw as Record<string, any>;
    const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.round(v) : null);
    const input = num(u.input_tokens);
    if (input !== null) this.usage.input_uncached = input;
    const read = num(u.cache_read_input_tokens);
    if (read !== null) this.usage.cache_read = read;
    const split = u.cache_creation && typeof u.cache_creation === 'object' ? u.cache_creation : null;
    const w5 = split ? num(split.ephemeral_5m_input_tokens) : null;
    const w1 = split ? num(split.ephemeral_1h_input_tokens) : null;
    if (w5 !== null || w1 !== null) {
      this.usage.cache_write_5m = w5 ?? 0;
      this.usage.cache_write_1h = w1 ?? 0;
    } else {
      const created = num(u.cache_creation_input_tokens);
      if (created !== null) {
        this.usage.cache_write_5m = created;
        this.usage.cache_write_1h = 0;
      }
    }
    const out = num(u.output_tokens);
    if (out !== null) this.usage.output_tokens = Math.max(this.usage.output_tokens, out);
    // Aggregators that bill differently from list price report their own cost (OrcaRouter: cost_usd).
    const cost = typeof u.cost_usd === 'number' ? u.cost_usd : typeof u.cost === 'number' ? u.cost : null;
    if (cost !== null && Number.isFinite(cost) && cost >= 0) this.reported = Math.round(cost * 1_000_000);
  }

  text(): string {
    return this.captured;
  }

  model(): string | null {
    return this.answeredBy;
  }

  reportedCost(): number | null {
    return this.reported;
  }

  result(): Metered {
    return {
      usage: { ...this.usage },
      source: this.started ? (this.stopped ? 'provider' : 'partial') : 'none',
      stop_reason: this.stopReason,
      provider_request_id: this.messageId,
      complete: this.stopped,
    };
  }
}

export function sse(event: string, data: unknown): Uint8Array {
  return new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}
