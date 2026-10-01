/**
 * Translation between Plexora's Anthropic-shaped envelope and the two OpenAI
 * wire formats, in both directions.
 *
 * Requests: `toResponses` (OpenAI Responses API) and `toChat` (Chat
 * Completions, as OpenRouter serves it). Streams: `ResponsesAdapter` and
 * `ChatAdapter` read the provider's events and emit Anthropic Messages events
 * (`message_start`, `content_block_*`, `message_delta`, `message_stop`), so
 * the client parses one format whatever served the call. `AnthropicAdapter`
 * passes Anthropic-wire bytes through untouched.
 *
 * Every adapter also meters: usage, stop reason, the provider's message id,
 * the model that actually answered, and the provider's own cost when it
 * reports one. Text is kept only when `capture` is set (a shadow comparison),
 * in memory, bounded, and never written anywhere.
 */
import type { Route, Usage } from './catalog';
import { ZERO_USAGE } from './catalog';
import { type Envelope, type Metered, sse, UsageMeter } from './upstream';

const CAPTURE_LIMIT = 64 * 1024;

// -- requests ------------------------------------------------------------------------

type Block = Record<string, any>;

function blocksOf(content: unknown): Block[] {
  if (typeof content === 'string') return [{ type: 'text', text: content }];
  return Array.isArray(content) ? (content as Block[]) : [];
}

function systemText(system: unknown): string {
  return blocksOf(system).filter((b) => b.type === 'text').map((b) => String(b.text ?? '')).join('\n\n');
}

function dataUri(block: Block): string | null {
  const source = block.source ?? {};
  if (source.type === 'base64' && typeof source.data === 'string') {
    return `data:${source.media_type || 'image/png'};base64,${source.data}`;
  }
  if (source.type === 'url' && typeof source.url === 'string') return source.url;
  return null;
}

function resultText(block: Block): { text: string; images: string[] } {
  const parts = blocksOf(block.content);
  const images = parts.filter((p) => p.type === 'image').map(dataUri).filter((u): u is string => !!u);
  const text = parts.filter((p) => p.type === 'text').map((p) => String(p.text ?? '')).join('\n');
  return { text: block.is_error ? `ERROR: ${text}` : text, images };
}

/** Anthropic Messages envelope -> OpenAI Responses request. */
export function toResponses(route: Route, envelope: Envelope, user: string, cacheKey: string): Record<string, unknown> {
  const input: Block[] = [];
  for (const message of envelope.messages as Block[]) {
    const blocks = blocksOf(message.content);
    if (message.role === 'assistant') {
      const text = blocks.filter((b) => b.type === 'text').map((b) => String(b.text ?? '')).join('');
      if (text) input.push({ role: 'assistant', content: [{ type: 'output_text', text }] });
      for (const b of blocks.filter((x) => x.type === 'tool_use')) {
        input.push({ type: 'function_call', call_id: b.id, name: b.name, arguments: JSON.stringify(b.input ?? {}) });
      }
      continue;
    }
    const content: Block[] = [];
    const late: Block[] = [];
    for (const b of blocks) {
      if (b.type === 'text') content.push({ type: 'input_text', text: String(b.text ?? '') });
      else if (b.type === 'image') {
        const url = dataUri(b);
        if (url) content.push({ type: 'input_image', image_url: url });
      } else if (b.type === 'tool_result') {
        const { text, images } = resultText(b);
        input.push({ type: 'function_call_output', call_id: b.tool_use_id, output: text });
        for (const url of images) late.push({ type: 'input_image', image_url: url });
      }
    }
    content.push(...late);
    if (content.length) input.push({ role: 'user', content });
  }
  const body: Record<string, unknown> = {
    model: route.model,
    input,
    max_output_tokens: Math.min(envelope.max_tokens, route.max_tokens_cap),
    safety_identifier: user,
    prompt_cache_key: cacheKey,
    store: false,
    stream: true,
  };
  const instructions = systemText(envelope.system);
  if (instructions) body.instructions = instructions;
  if (envelope.tools?.length) {
    body.tools = (envelope.tools as Block[]).map((t) => ({ type: 'function', name: t.name,
      description: t.description ?? '', parameters: t.input_schema ?? { type: 'object' }, strict: false }));
  }
  if (envelope.output_schema) {
    body.text = { format: { type: 'json_schema', name: 'plexora_answer', schema: envelope.output_schema,
      strict: false } };
  }
  if (route.effort) body.reasoning = { effort: route.effort };
  return body;
}

/** Anthropic Messages envelope -> Chat Completions request (OpenRouter). */
export function toChat(route: Route, envelope: Envelope, user: string, cacheKey: string): Record<string, unknown> {
  const messages: Block[] = [];
  // System blocks keep their cache_control: OpenRouter forwards it to models that cache explicitly.
  const system = blocksOf(envelope.system).filter((b) => b.type === 'text');
  if (system.length) {
    messages.push({ role: 'system', content: system.map((b) => ({ type: 'text', text: String(b.text ?? ''),
      ...(b.cache_control ? { cache_control: b.cache_control } : {}) })) });
  }
  for (const message of envelope.messages as Block[]) {
    const blocks = blocksOf(message.content);
    if (message.role === 'assistant') {
      const text = blocks.filter((b) => b.type === 'text').map((b) => String(b.text ?? '')).join('');
      const calls = blocks.filter((b) => b.type === 'tool_use').map((b) => ({ id: b.id, type: 'function',
        function: { name: b.name, arguments: JSON.stringify(b.input ?? {}) } }));
      messages.push({ role: 'assistant', content: text || null, ...(calls.length ? { tool_calls: calls } : {}) });
      continue;
    }
    const parts: Block[] = [];
    const late: Block[] = [];
    for (const b of blocks) {
      if (b.type === 'text') {
        parts.push({ type: 'text', text: String(b.text ?? ''), ...(b.cache_control ? { cache_control: b.cache_control } : {}) });
      } else if (b.type === 'image') {
        const url = dataUri(b);
        if (url) parts.push({ type: 'image_url', image_url: { url } });
      } else if (b.type === 'tool_result') {
        const { text, images } = resultText(b);
        messages.push({ role: 'tool', tool_call_id: b.tool_use_id, content: text });
        for (const url of images) late.push({ type: 'image_url', image_url: { url } });
      }
    }
    parts.push(...late);
    if (parts.length) messages.push({ role: 'user', content: parts });
  }
  const body: Record<string, unknown> = {
    model: route.model,
    messages,
    max_tokens: Math.min(envelope.max_tokens, route.max_tokens_cap),
    user,
    session_id: cacheKey,
    usage: { include: true },
    provider: { data_collection: 'deny' },
    stream: true,
  };
  if (envelope.stop_sequences?.length) body.stop = envelope.stop_sequences;
  if (envelope.tools?.length) {
    body.tools = (envelope.tools as Block[]).map((t) => ({ type: 'function', function: { name: t.name,
      description: t.description ?? '', parameters: t.input_schema ?? { type: 'object' } } }));
  }
  if (envelope.output_schema) {
    body.response_format = { type: 'json_schema', json_schema: { name: 'plexora_answer',
      schema: envelope.output_schema, strict: false } };
  }
  if (route.effort) body.reasoning = { effort: route.effort };
  return body;
}

// -- streams -------------------------------------------------------------------------

export interface StreamAdapter {
  /** Bytes for the client (Anthropic-shaped SSE), or null when there are none yet. */
  push(chunk: Uint8Array): Uint8Array | null;
  /** Called once the provider's stream has ended. */
  end(): Uint8Array | null;
  result(): Metered & { model: string | null; reported_cost_micro: number | null };
  /** The answer text, when the adapter was asked to capture it. */
  text(): string;
}

const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.round(v) : 0);

class Lines {
  private buffer = '';
  private readonly decoder = new TextDecoder();
  /** `data:` payloads in the chunk. */
  feed(chunk: Uint8Array): string[] {
    this.buffer += this.decoder.decode(chunk, { stream: true });
    const out: string[] = [];
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index).replace(/\r$/, '');
      this.buffer = this.buffer.slice(index + 1);
      if (line.startsWith('data:')) out.push(line.slice(5).trim());
      index = this.buffer.indexOf('\n');
    }
    return out;
  }
}

function concat(parts: Uint8Array[]): Uint8Array | null {
  if (!parts.length) return null;
  if (parts.length === 1) return parts[0]!;
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

export class AnthropicAdapter implements StreamAdapter {
  private readonly meter: UsageMeter;
  constructor(capture = false) {
    this.meter = new UsageMeter(capture ? CAPTURE_LIMIT : 0);
  }
  push(chunk: Uint8Array) {
    this.meter.push(chunk);
    return chunk;
  }
  end() {
    return null;
  }
  result() {
    return { ...this.meter.result(), model: this.meter.model(), reported_cost_micro: this.meter.reportedCost() };
  }
  text() {
    return this.meter.text();
  }
}

/** Shared bookkeeping for the two translated wires. */
abstract class Translated implements StreamAdapter {
  protected readonly lines = new Lines();
  protected usage: Usage = { ...ZERO_USAGE };
  protected started = false;
  protected finished = false;
  protected stopReason: string | null = null;
  protected messageId: string | null = null;
  protected answeredBy: string | null = null;
  protected reported: number | null = null;
  protected nextIndex = 0;
  protected readonly open = new Map<string, number>();
  protected sawTool = false;
  private captured = '';
  protected out: Uint8Array[] = [];

  constructor(private readonly capture = false) {}

  protected emit(type: string, data: Record<string, unknown>) {
    this.out.push(sse(type, { type, ...data }));
  }

  protected start(id: unknown, model: unknown) {
    if (this.started) return;
    this.started = true;
    this.messageId = typeof id === 'string' ? id : null;
    if (typeof model === 'string') this.answeredBy = model;
    this.emit('message_start', { message: { id: this.messageId, type: 'message', role: 'assistant', content: [],
      model: this.answeredBy, usage: { input_tokens: 0, output_tokens: 0 } } });
  }

  protected block(key: string, contentBlock: Record<string, unknown>): number {
    const existing = this.open.get(key);
    if (existing !== undefined) return existing;
    const index = this.nextIndex++;
    this.open.set(key, index);
    this.emit('content_block_start', { index, content_block: contentBlock });
    return index;
  }

  protected textDelta(key: string, text: string) {
    if (!text) return;
    const index = this.block(key, { type: 'text', text: '' });
    this.emit('content_block_delta', { index, delta: { type: 'text_delta', text } });
    if (this.capture && this.captured.length < CAPTURE_LIMIT) this.captured += text;
  }

  protected close(key?: string) {
    for (const [k, index] of [...this.open]) {
      if (key !== undefined && !k.startsWith(key)) continue;
      this.emit('content_block_stop', { index });
      this.open.delete(k);
    }
  }

  protected finish(stop: string) {
    if (this.finished) return;
    this.start(null, null);
    this.close();
    this.finished = true;
    this.stopReason = stop;
    this.emit('message_delta', { delta: { stop_reason: stop, stop_sequence: null },
      usage: { output_tokens: this.usage.output_tokens } });
    this.emit('message_stop', {});
  }

  protected fail(message: string) {
    this.emit('error', { error: { type: 'api_error', message } });
  }

  protected abstract data(event: Record<string, any>): void;
  protected done(): void {}

  push(chunk: Uint8Array) {
    for (const text of this.lines.feed(chunk)) {
      if (!text) continue;
      if (text === '[DONE]') {
        this.done();
        continue;
      }
      try {
        this.data(JSON.parse(text));
      } catch {
        // A line that is not JSON carries nothing we bill or show.
      }
    }
    const bytes = concat(this.out);
    this.out = [];
    return bytes;
  }

  end() {
    this.done();
    const bytes = concat(this.out);
    this.out = [];
    return bytes;
  }

  result() {
    return {
      usage: { ...this.usage },
      source: (this.started ? (this.finished ? 'provider' : 'partial') : 'none') as Metered['source'],
      stop_reason: this.stopReason,
      provider_request_id: this.messageId,
      complete: this.finished,
      model: this.answeredBy,
      reported_cost_micro: this.reported,
    };
  }

  text() {
    return this.captured;
  }
}

/** OpenAI Responses API events -> Anthropic events. */
export class ResponsesAdapter extends Translated {
  protected data(event: Record<string, any>) {
    const type = event.type;
    if (type === 'response.created' || type === 'response.in_progress') {
      this.start(event.response?.id, event.response?.model);
    } else if (type === 'response.output_item.added') {
      this.start(null, null);
      const item = event.item ?? {};
      if (item.type === 'function_call') {
        this.sawTool = true;
        this.block(String(item.id), { type: 'tool_use', id: item.call_id, name: item.name, input: {} });
      }
    } else if (type === 'response.output_text.delta') {
      this.start(null, null);
      this.textDelta(`${event.item_id}:${event.content_index ?? 0}`, String(event.delta ?? ''));
    } else if (type === 'response.function_call_arguments.delta') {
      const index = this.open.get(String(event.item_id));
      if (index !== undefined) {
        this.emit('content_block_delta', { index, delta: { type: 'input_json_delta',
          partial_json: String(event.delta ?? '') } });
      }
    } else if (type === 'response.output_item.done') {
      this.close(String(event.item?.id ?? ''));
    } else if (type === 'response.completed' || type === 'response.incomplete') {
      const response = event.response ?? {};
      this.start(response.id, response.model);
      if (typeof response.model === 'string') this.answeredBy = response.model;
      this.absorb(response.usage);
      const truncated = type === 'response.incomplete' && response.incomplete_details?.reason === 'max_output_tokens';
      this.finish(truncated ? 'max_tokens' : this.sawTool ? 'tool_use' : 'end_turn');
    } else if (type === 'response.failed' || type === 'error') {
      this.absorb(event.response?.usage);
      this.fail('The model provider failed mid-answer.');
    }
  }

  private absorb(raw: unknown) {
    if (!raw || typeof raw !== 'object') return;
    const u = raw as Record<string, any>;
    const details = u.input_tokens_details ?? {};
    const read = num(details.cached_tokens);
    const write = num(details.cache_write_tokens ?? u.cache_write_tokens);
    this.usage = { input_uncached: Math.max(0, num(u.input_tokens) - read - write), cache_read: read,
      cache_write_5m: write, cache_write_1h: 0, output_tokens: num(u.output_tokens) };
  }
}

/** Chat Completions chunks (OpenRouter) -> Anthropic events. */
export class ChatAdapter extends Translated {
  private finishReason: string | null = null;
  private usageSeen = false;

  protected data(event: Record<string, any>) {
    if (event.error) {
      this.fail('The model provider failed mid-answer.');
      return;
    }
    this.start(event.id, event.model);
    if (typeof event.model === 'string') this.answeredBy = event.model;
    const choice = Array.isArray(event.choices) ? event.choices[0] : null;
    const delta = choice?.delta ?? {};
    if (typeof delta.content === 'string') this.textDelta('text', delta.content);
    if (Array.isArray(delta.tool_calls)) {
      for (const call of delta.tool_calls) {
        const key = `tool${call.index ?? 0}`;
        let index = this.open.get(key);
        if (index === undefined) {
          this.sawTool = true;
          index = this.block(key, { type: 'tool_use', id: call.id, name: call.function?.name, input: {} });
        }
        const args = call.function?.arguments;
        if (typeof args === 'string' && args) {
          this.emit('content_block_delta', { index, delta: { type: 'input_json_delta', partial_json: args } });
        }
      }
    }
    if (choice && typeof choice.finish_reason === 'string') this.finishReason = choice.finish_reason;
    if (event.usage && typeof event.usage === 'object') {
      const u = event.usage as Record<string, any>;
      const details = u.prompt_tokens_details ?? {};
      const read = num(details.cached_tokens);
      const write = num(details.cache_write_tokens);
      this.usage = { input_uncached: Math.max(0, num(u.prompt_tokens) - read - write), cache_read: read,
        cache_write_5m: write, cache_write_1h: 0, output_tokens: num(u.completion_tokens) };
      if (typeof u.cost === 'number' && Number.isFinite(u.cost)) this.reported = Math.round(u.cost * 1_000_000);
      this.usageSeen = true;
    }
  }

  protected done() {
    if (this.finished || !this.finishReason) return;
    const stop = { tool_calls: 'tool_use', length: 'max_tokens', content_filter: 'refusal' }[this.finishReason];
    this.finish(stop ?? (this.sawTool ? 'tool_use' : 'end_turn'));
    if (!this.usageSeen) this.finished = false;   // a finish with no usage is billed as partial
  }
}

export function adapterFor(wire: 'anthropic' | 'openai_responses' | 'openai_chat', capture = false): StreamAdapter {
  if (wire === 'openai_responses') return new ResponsesAdapter(capture);
  if (wire === 'openai_chat') return new ChatAdapter(capture);
  return new AnthropicAdapter(capture);
}
