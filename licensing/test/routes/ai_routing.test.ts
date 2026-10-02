import { env, SELF } from 'cloudflare:test';
import { afterEach, describe, expect, it } from 'vitest';

import { setUpstreamFetch } from '../../src/ai/providers';
import { activate, admin, BASE, call, environmentBody, issue, post, travel } from './helpers';

/**
 * The route table: providers other than Anthropic (translated wires), the
 * publish gate, retries and failover, the kill switch, and shadow routes.
 */

interface Seen { url: string; body: Record<string, any>; headers: Headers }
let seen: Seen[] = [];

type Reply = (s: Seen) => Response;
const replies: Array<{ match: string; reply: Reply }> = [];

function on(match: string, reply: Reply) {
  replies.unshift({ match, reply });
}

setUpstreamFetch(null);

function install() {
  setUpstreamFetch(async (url, init) => {
    const s = { url, body: JSON.parse(String(init.body)), headers: new Headers(init.headers) };
    seen.push(s);
    const hit = replies.find((r) => url.includes(r.match));
    return hit ? hit.reply(s) : new Response('no fake for this url', { status: 500 });
  });
}

afterEach(() => {
  setUpstreamFetch(null);
  replies.length = 0;
  seen = [];
  travel(0);
});

const sseOf = (events: Array<[string | null, unknown]>, headers: Record<string, string> = {}) =>
  new Response(events.map(([name, data]) => `${name ? `event: ${name}\n` : ''}data: ${
    typeof data === 'string' ? data : JSON.stringify(data)}\n\n`).join(''),
  { headers: { 'content-type': 'text/event-stream', ...headers } });

function anthropicStream(text = '{"kind":"t2_confirm","direction":"about_right"}', headers: Record<string, string> = {}) {
  const events: Array<[string, Record<string, unknown>]> = [
    ['message_start', { type: 'message_start', message: { id: 'msg_a', model: 'claude-opus-5-5',
      usage: { input_tokens: 100, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, output_tokens: 1 } } }],
    ['content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }],
    ['content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text } }],
    ['content_block_stop', { type: 'content_block_stop', index: 0 }],
    ['message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 10 } }],
    ['message_stop', { type: 'message_stop' }],
  ];
  return sseOf(events, headers);
}

function responsesStream(text = '{"kind":"t2_confirm","direction":"about_right"}') {
  const half = Math.floor(text.length / 2);
  return sseOf([
    ['response.created', { type: 'response.created', response: { id: 'resp_1', model: 'gpt-test' } }],
    ['response.output_item.added', { type: 'response.output_item.added', output_index: 0,
      item: { type: 'message', id: 'item_1' } }],
    ['response.output_text.delta', { type: 'response.output_text.delta', item_id: 'item_1', content_index: 0,
      delta: text.slice(0, half) }],
    ['response.output_text.delta', { type: 'response.output_text.delta', item_id: 'item_1', content_index: 0,
      delta: text.slice(half) }],
    ['response.output_item.done', { type: 'response.output_item.done', item: { id: 'item_1' } }],
    ['response.completed', { type: 'response.completed', response: { id: 'resp_1', model: 'gpt-test',
      usage: { input_tokens: 1000, input_tokens_details: { cached_tokens: 800 }, output_tokens: 50 } } }],
  ]);
}

function chatToolStream() {
  return sseOf([
    [null, { id: 'gen-1', model: 'anthropic/claude-opus-5-5', choices: [{ index: 0, delta: { role: 'assistant',
      content: 'Looking.' } }] }],
    [null, { id: 'gen-1', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_1', type: 'function',
      function: { name: 'read_board', arguments: '' } }] } }] }],
    [null, { id: 'gen-1', choices: [{ index: 0, delta: { tool_calls: [{ index: 0,
      function: { arguments: '{"keys":["CD3"]}' } }] } }] }],
    [null, { id: 'gen-1', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }],
    [null, { id: 'gen-1', choices: [], usage: { prompt_tokens: 500, completion_tokens: 20,
      prompt_tokens_details: { cached_tokens: 400 }, cost: 0.0021 } }],
    [null, '[DONE]'],
  ]);
}

const failing = (status = 503) => () => new Response('{"error":"down"}', { status });

async function setup(overrides: Record<string, unknown> = {}) {
  const issued = await issue(overrides);
  const body = environmentBody();
  const activated = await activate(issued.seat.key, body);
  expect(activated.status).toBe(200);
  await admin('POST', `/ai/accounts/${issued.account_id}/credit`, { credits: 1000 });
  const reissue = async () => {
    const reply = await post('/v1/ai/token', { certificate: activated.json.certificate, binding: body.binding,
      app_version: '0.0.27' });
    expect(reply.status, JSON.stringify(reply.json)).toBe(200);
    return reply.json.token as string;
  };
  const token = await reissue();
  install();
  return { account_id: issued.account_id, token, reissue };
}

let keys = 0;
function request(capability = 'vision_judgement', ctx: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) {
  return {
    capability,
    context: { feature: 'gating', agent: 'gating_worker', session_id: 'gs_route', ...ctx },
    request: {
      system: [{ type: 'text', text: 'You are a Plexora worker.', cache_control: { type: 'ephemeral' } }],
      messages: [{ role: 'user', content: [{ type: 'text', text: '{"packet": 1}' }] }],
      max_tokens: 1024,
      output_schema: { type: 'object', properties: { kind: { type: 'string' } }, required: ['kind'] },
      ...extra,
    },
  };
}

interface Streamed { status: number; json: Record<string, any>; events: Array<{ event: string; data: any }> }

async function message(token: string, body: unknown, { key, path = '/v1/ai/messages' }:
  { key?: string; path?: string } = {}): Promise<Streamed> {
  const response = await SELF.fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`,
      'Idempotency-Key': key ?? `rkey-${++keys}-abcdef`, 'CF-Connecting-IP': '203.0.113.9' },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.headers.get('content-type')?.includes('text/event-stream')) {
    return { status: response.status, json: text ? JSON.parse(text) : {}, events: [] };
  }
  const events = text.split('\n\n').filter(Boolean).map((chunk) => ({
    event: /^event: (.*)$/m.exec(chunk)?.[1] ?? '',
    data: JSON.parse(/^data: (.*)$/m.exec(chunk)?.[1] ?? 'null'),
  }));
  return { status: response.status, json: {}, events };
}

const usageOf = (s: Streamed) => s.events.find((e) => e.event === 'plexora.usage')?.data;
const textOf = (s: Streamed) => s.events.filter((e) => e.event === 'content_block_delta' &&
  e.data.delta?.type === 'text_delta').map((e) => e.data.delta.text).join('');

const row = (id: string) => env.LICENSE_DB.prepare('SELECT * FROM ai_requests WHERE id = ?1').bind(id)
  .first<Record<string, any>>();

const GPT = { in_micro: 1_250_000, cache_read_micro: 125_000, cache_write_5m_micro: 1_562_500,
  cache_write_1h_micro: 1_562_500, out_micro: 10_000_000, source_url: 'https://openai.com/api/pricing' };
const OPUS = { in_micro: 4_000_000, cache_read_micro: 200_000, cache_write_5m_micro: 5_000_000,
  cache_write_1h_micro: 8_000_000, out_micro: 20_000_000 };
const PASSING = { code_agreement: 0.99, marker_f1: 0.97, cache_hit_ratio: 0.9, invalid_answer_rate: 0.002,
  failure_rate: 0.001 };

async function catalogue(provider: string, model: string, costs: Record<string, unknown> = GPT) {
  const reply = await admin('PUT', `/ai/models/${provider}/${model}`, { source_url: 'https://example.org/prices', ...costs });
  expect(reply.status, JSON.stringify(reply.json)).toBe(200);
}

async function publish(route: Record<string, unknown>) {
  const reply = await admin('POST', '/ai/routes', route);
  expect(reply.status, JSON.stringify(reply.json)).toBe(201);
  return reply.json;
}

async function evaluate(route: Record<string, unknown>, metrics = PASSING) {
  const reply = await admin('POST', '/ai/evaluations', { metrics, dataset_ids: ['synthetic:all'], ...route });
  expect(reply.status).toBe(201);
  return reply.json;
}

describe('OpenAI (Responses API, translated)', () => {
  it('serves a published route, translating the request and the stream, and bills its catalogued cost', async () => {
    const { token } = await setup();
    await catalogue('openai', 'gpt-test');
    await publish({ capability: 'text_reasoning', provider: 'openai', model: 'gpt-test', effort: 'high' });
    on('api.openai.com/v1/responses', () => responsesStream());
    const reply = await message(token, request('text_reasoning', { feature: 'chat' }));
    expect(reply.status).toBe(200);
    const sent = seen[0]!;
    expect(sent.headers.get('authorization')).toBe('Bearer test-openai');
    expect(sent.body).toMatchObject({ model: 'gpt-test', instructions: 'You are a Plexora worker.', store: false,
      stream: true, max_output_tokens: 1024, reasoning: { effort: 'high' },
      text: { format: { type: 'json_schema', name: 'plexora_answer' } } });
    expect(sent.body.input).toEqual([{ role: 'user', content: [{ type: 'input_text', text: '{"packet": 1}' }] }]);
    expect(sent.body.safety_identifier).toMatch(/^[0-9a-f]{32}$/);
    expect(sent.body.metadata).toBeUndefined();
    // The client sees Anthropic events, whatever served the call.
    const names = reply.events.map((e) => e.event);
    expect(names).toEqual(['plexora.accepted', 'message_start', 'content_block_start', 'content_block_delta',
      'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop', 'plexora.usage']);
    expect(textOf(reply)).toBe('{"kind":"t2_confirm","direction":"about_right"}');
    const usage = usageOf(reply);
    expect(usage.usage).toEqual({ input_uncached: 200, cache_read: 800, cache_write_5m: 0, cache_write_1h: 0,
      output_tokens: 50 });
    // 200 x $1.25 + 800 x $0.125 + 50 x $10 per MTok = 850 micro, x 2.0.
    expect(usage.price_micro).toBe(1700);
    const r = await row(usage.gateway_request_id);
    expect(r).toMatchObject({ provider: 'openai', model: 'gpt-test', cost_micro: 850, resolved_model: 'gpt-test',
      status: 'ok', failover: 0 });
    expect(r!.route_id).toMatch(/^rt_/);
  });

  it('carries tools, tool calls and tool results across the translation', async () => {
    const { token } = await setup();
    await catalogue('openai', 'gpt-test');
    await publish({ capability: 'text_reasoning', provider: 'openai', model: 'gpt-test' });
    on('api.openai.com', () => responsesStream('done'));
    const body = request('text_reasoning', { feature: 'chat' }, {
      tools: [{ name: 'read_board', description: 'Read facts.', input_schema: { type: 'object' } }],
      messages: [
        { role: 'user', content: 'What is on the board?' },
        { role: 'assistant', content: [{ type: 'tool_use', id: 'tu_1', name: 'read_board', input: { keys: ['a'] } }] },
        { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tu_1', content: [{ type: 'text', text: 'a=1' }] },
          { type: 'image', source: { type: 'base64', media_type: 'image/webp', data: 'AAAA' } }] },
      ],
    });
    expect((await message(token, body)).status).toBe(200);
    const sent = seen[0]!.body;
    expect(sent.tools).toEqual([{ type: 'function', name: 'read_board', description: 'Read facts.',
      parameters: { type: 'object' }, strict: false }]);
    expect(sent.input).toEqual([
      { role: 'user', content: [{ type: 'input_text', text: 'What is on the board?' }] },
      { type: 'function_call', call_id: 'tu_1', name: 'read_board', arguments: '{"keys":["a"]}' },
      { type: 'function_call_output', call_id: 'tu_1', output: 'a=1' },
      { role: 'user', content: [{ type: 'input_image', image_url: 'data:image/webp;base64,AAAA' }] },
    ]);
  });
});

describe('aggregators and the publish gate', () => {
  it('refuses an aggregator route without a passing evaluation on the current bench', async () => {
    await setup();
    await catalogue('openrouter', 'anthropic/claude-opus-5-5', { ...OPUS, fee_bps: 550 });
    const route = { feature: 'gating', capability: 'vision_judgement', provider: 'openrouter',
      model: 'anthropic/claude-opus-5-5' };
    const bare = await admin('POST', '/ai/routes', route);
    expect(bare.status).toBe(409);
    expect(bare.json.error.code).toBe('route_not_publishable');

    const weak = await evaluate(route, { ...PASSING, code_agreement: 0.9 });
    expect(weak.passed).toBe(false);
    expect(weak.misses).toEqual(['code_agreement < 0.98']);
    expect((await admin('POST', '/ai/routes', { ...route, evaluation_id: weak.id })).status).toBe(409);

    const stale = await evaluate(route, PASSING);
    const old = await admin('POST', '/ai/evaluations', { ...route, metrics: PASSING, bench_version: 'gating-0' });
    expect(old.json.passed).toBe(false);
    expect((await admin('POST', '/ai/routes', { ...route, evaluation_id: old.json.id })).status).toBe(409);
    // An evaluation for another model does not admit this one.
    const other = await evaluate({ ...route, model: 'openai/gpt-x' });
    expect((await admin('POST', '/ai/routes', { ...route, evaluation_id: other.id })).status).toBe(409);

    expect(stale.passed).toBe(true);
    await publish({ ...route, evaluation_id: stale.id });
    // Uncatalogued models cannot be published at all, even by a direct provider.
    const unknown = await admin('POST', '/ai/routes', { capability: 'text_routine', provider: 'openai', model: 'gpt-none' });
    expect(unknown.status).toBe(409);
  });

  it('OpenRouter: chat-completions translation, tool calls back as tool_use, its fee and reported cost', async () => {
    const { token } = await setup();
    const route = { feature: 'gating', capability: 'vision_judgement', provider: 'openrouter',
      model: 'anthropic/claude-opus-5-5' };
    await catalogue('openrouter', 'anthropic/claude-opus-5-5', { ...OPUS, fee_bps: 550 });
    await publish({ ...route, evaluation_id: (await evaluate(route)).id });
    on('openrouter.ai/api/v1/chat/completions', () => chatToolStream());
    const reply = await message(token, request('vision_judgement', {}, {
      tools: [{ name: 'read_board', description: 'Read facts.', input_schema: { type: 'object' } }],
    }));
    expect(reply.status).toBe(200);
    const sent = seen[0]!;
    expect(sent.headers.get('authorization')).toBe('Bearer test-openrouter');
    expect(sent.body.messages[0]).toEqual({ role: 'system', content: [{ type: 'text', text: 'You are a Plexora worker.',
      cache_control: { type: 'ephemeral' } }] });
    expect(sent.body).toMatchObject({ model: 'anthropic/claude-opus-5-5', usage: { include: true },
      provider: { data_collection: 'deny' }, response_format: { type: 'json_schema' } });
    expect(sent.body.tools[0]).toEqual({ type: 'function', function: { name: 'read_board', description: 'Read facts.',
      parameters: { type: 'object' } } });
    const start = reply.events.find((e) => e.event === 'content_block_start' && e.data.content_block.type === 'tool_use');
    expect(start!.data.content_block).toMatchObject({ id: 'call_1', name: 'read_board' });
    const args = reply.events.filter((e) => e.data?.delta?.type === 'input_json_delta').map((e) => e.data.delta.partial_json);
    expect(args.join('')).toBe('{"keys":["CD3"]}');
    expect(reply.events.find((e) => e.event === 'message_delta')!.data.delta.stop_reason).toBe('tool_use');
    const usage = usageOf(reply);
    expect(usage.usage).toMatchObject({ input_uncached: 100, cache_read: 400, output_tokens: 20 });
    // (100 x $4 + 400 x $0.20 + 20 x $20) per MTok = 880 micro, + 5.5% fee.
    const r = await row(usage.gateway_request_id);
    expect(r).toMatchObject({ provider: 'openrouter', cost_micro: Math.ceil(880 * 1.055), reported_cost_micro: 2100 });
  });

  it('OrcaRouter: native Anthropic passthrough, and the model it says answered is recorded', async () => {
    const { token } = await setup();
    const route = { feature: 'gating', capability: 'vision_judgement', provider: 'orcarouter', model: 'claude-opus-5-5' };
    await catalogue('orcarouter', 'claude-opus-5-5', OPUS);
    await publish({ ...route, evaluation_id: (await evaluate(route)).id });
    on('api.orcarouter.ai/v1/messages', () => anthropicStream(undefined, { 'x-orca-resolved-model': 'claude-opus-5-5' }));
    const reply = await message(token, request());
    expect(reply.status).toBe(200);
    expect(seen[0]!.headers.get('x-orcarouter-include-cost')).toBe('true');
    expect(seen[0]!.body).toMatchObject({ model: 'claude-opus-5-5', metadata: { user_id: expect.any(String) } });
    expect((await row(usageOf(reply).gateway_request_id))!.resolved_model).toBe('claude-opus-5-5');
  });

  it('SayGM: only its confidential (TEE) models may be catalogued', async () => {
    await setup();
    const frontier = await admin('PUT', '/ai/models/saygm/claude', { ...GPT });
    expect(frontier.status).toBe(400);
    await catalogue('saygm', 'llama-4-70b-TEE');
  });
});

describe('retries, failover and the kill switch', () => {
  async function twoRoutes(failover = 'outage') {
    await catalogue('openai', 'gpt-test');
    await publish({ capability: 'vision_judgement', provider: 'anthropic', model: 'claude-opus-5-5', rank: 0, failover });
    await publish({ capability: 'vision_judgement', provider: 'openai', model: 'gpt-test', rank: 1 });
  }

  it('retries the same route, and moves to the next only once its circuit opens', async () => {
    const { token } = await setup();
    await twoRoutes();
    on('api.anthropic.com', failing(503));
    on('api.openai.com', () => responsesStream());

    // Three tries on Anthropic, circuit still closed (5 failures open it): refused, key freed.
    const first = await message(token, request(), { key: 'same-key-001' });
    expect(first.status).toBe(503);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com', 'api.anthropic.com', 'api.anthropic.com']);

    // The retry (same key) opens the circuit on its second try and fails over.
    seen = [];
    const second = await message(token, request(), { key: 'same-key-001' });
    expect(second.status).toBe(200);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com', 'api.anthropic.com', 'api.openai.com']);
    expect(second.events[0]!.data.failover).toBe(true);
    const r = await row(usageOf(second).gateway_request_id);
    expect(r).toMatchObject({ provider: 'openai', failover: 1, attempts: 3 });

    // While the circuit is open nobody waits on Anthropic.
    seen = [];
    const third = await message(token, request('vision_judgement', { session_id: 'gs_other' }));
    expect(third.status).toBe(200);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.openai.com']);

    // Half-open after AI_CIRCUIT_OPEN_S: a healthy Anthropic serves a new session again...
    travel(25);
    on('api.anthropic.com', () => anthropicStream());
    seen = [];
    const fresh = await message(token, request('vision_judgement', { session_id: 'gs_new' }));
    expect(new URL(seen[0]!.url).host).toBe('api.anthropic.com');
    expect(fresh.status).toBe(200);
    // ...while the session that failed over stays on its route (its cache is warm there).
    seen = [];
    await message(token, request());
    expect(new URL(seen[0]!.url).host).toBe('api.openai.com');
  });

  it("fails over on any exhausted retry when the route says 'error', and never on the request's own fault", async () => {
    const { token } = await setup();
    await twoRoutes('error');
    on('api.anthropic.com', failing(529));
    on('api.openai.com', () => responsesStream());
    const reply = await message(token, request('vision_judgement', { session_id: 'gs_err' }));
    expect(reply.status).toBe(200);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com', 'api.anthropic.com', 'api.anthropic.com',
      'api.openai.com']);

    on('api.anthropic.com', failing(400));
    seen = [];
    const rejected = await message(token, request('vision_judgement', { session_id: 'gs_bad' }));
    expect(rejected.status).toBe(400);
    expect(rejected.json.error.code).toBe('provider_rejected');
    expect(seen).toHaveLength(1);
  });

  it('the kill switch takes a provider out of every route at once, and back', async () => {
    const { token } = await setup();
    await twoRoutes();
    on('api.anthropic.com', () => anthropicStream());
    on('api.openai.com', () => responsesStream());
    expect((await admin('POST', '/ai/providers/anthropic/disable', { reason: 'incident' })).json.forced).toBe('open');
    await message(token, request('vision_judgement', { session_id: 'gs_k1' }));
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.openai.com']);
    await admin('POST', '/ai/providers/anthropic/enable');
    seen = [];
    await message(token, request('vision_judgement', { session_id: 'gs_k2' }));
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com']);
    const providers = await admin('GET', '/ai/providers');
    expect(providers.json.providers.find((p: any) => p.provider === 'openai').configured).toBe(true);
  });

  it('the dev route may name any catalogued provider/model, at cost', async () => {
    const s = await setup();
    await admin('PATCH', `/ai/accounts/${s.account_id}`, { mode: 'dev' });
    const token = await s.reissue();
    await catalogue('openai', 'gpt-test');
    on('api.openai.com', () => responsesStream());
    const dev = { path: '/v1/ai/dev/messages' };
    const reply = await message(token, { ...request(), model: 'openai/gpt-test' }, dev);
    expect(reply.status).toBe(200);
    expect(usageOf(reply)).toMatchObject({ billing: 'dev', price_micro: 850, cost_micro: 850, provider: 'openai',
      model: 'gpt-test' });
    const unknown = await message(token, { ...request(), model: 'openai/gpt-none' }, dev);
    expect(unknown.status).toBe(400);
    const frontier = await message(token, { ...request(), model: 'saygm/claude' }, dev);
    expect(frontier.status).toBe(400);
  });
});

describe('shadow routes', () => {
  async function shadowRow(servedId: string) {
    for (let i = 0; i < 50; i++) {
      const r = await env.LICENSE_DB.prepare('SELECT * FROM ai_requests WHERE shadow_of = ?1').bind(servedId)
        .first<Record<string, any>>();
      if (r) return r;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    return null;
  }

  it('duplicates sampled sessions to a candidate at Plexora cost, records agreement, and bills nothing for it', async () => {
    const { token, account_id } = await setup();
    await catalogue('openai', 'gpt-test');
    await publish({ feature: 'gating', capability: 'vision_judgement', role: 'shadow', provider: 'openai',
      model: 'gpt-test', shadow_pct: 100 });
    on('api.anthropic.com', () => anthropicStream('{"kind":"t2_confirm","direction":"about_right","notes":"a"}'));
    on('api.openai.com', () => responsesStream('{"kind":"t2_confirm","direction":"about_right","notes":"b"}'));
    const agreed = await message(token, request('vision_judgement', { session_id: 'gs_sh1' }));
    const servedId = usageOf(agreed).gateway_request_id;
    const shadow = await shadowRow(servedId);
    expect(shadow).toMatchObject({ billing: 'shadow', provider: 'openai', charged_micro: 0, price_micro: 0,
      shadow_agree: 1, cost_micro: 850 });
    // The served call carried the shadow text capture; the client saw only the served answer.
    expect(textOf(agreed)).toContain('"notes":"a"');

    on('api.openai.com', () => responsesStream('{"kind":"t2_confirm","direction":"too_low"}'));
    const disagreed = await message(token, request('vision_judgement', { session_id: 'gs_sh2' }));
    expect((await shadowRow(usageOf(disagreed).gateway_request_id))!.shadow_agree).toBe(0);

    const report = await admin('GET', '/ai/shadow');
    expect(report.json.candidates[0]).toMatchObject({ provider: 'openai', calls: 2, compared: 2, agreed: 1,
      agreement: 0.5 });
    // Shadow calls are Plexora's: invisible to the account, absent from its balance.
    const usage = await SELF.fetch(`${BASE}/v1/ai/usage`, { headers: { Authorization: `Bearer ${token}` } });
    const rows = ((await usage.json()) as any).rows;
    expect(rows.every((r: any) => r.billing !== 'shadow')).toBe(true);
    expect(rows.reduce((n: number, r: any) => n + r.calls, 0)).toBe(2);
    const ledger = await env.LICENSE_DB.prepare(
      "SELECT COUNT(*) AS n FROM ai_ledger WHERE account_id = ?1 AND kind = 'settle'").bind(account_id).first<{ n: number }>();
    expect(ledger!.n).toBe(2);
  });

  it('does not shadow a call to the very route that served it', async () => {
    const { token } = await setup();
    await publish({ feature: 'gating', capability: 'vision_judgement', role: 'shadow', provider: 'anthropic',
      model: 'claude-opus-5-5', shadow_pct: 100 });
    on('api.anthropic.com', () => anthropicStream());
    await message(token, request('vision_judgement', { session_id: 'gs_same' }));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(seen).toHaveLength(1);
  });
});

describe('what a model can do', () => {
  it('puts the schema in the prompt for a model without structured output, after every cached byte', async () => {
    const { token } = await setup();
    await catalogue('openrouter', 'free/model:free', { ...GPT, supports_structured: false });
    await publish({ feature: 'gating', capability: 'vision_judgement', provider: 'openrouter',
      model: 'free/model:free', evaluation_id: (await evaluate({ feature: 'gating', capability: 'vision_judgement',
        provider: 'openrouter', model: 'free/model:free' })).id });
    on('openrouter.ai', () => chatToolStream());
    expect((await message(token, request())).status).toBe(200);
    const sent = seen[0]!.body;
    expect(sent.response_format).toBeUndefined();
    expect(sent.messages[0].content[0].text).toBe('You are a Plexora worker.');
    const last = sent.messages[sent.messages.length - 1].content;
    expect(last[0]).toEqual({ type: 'text', text: '{"packet": 1}' });
    expect(last[1].text).toMatch(/^Reply with ONLY one JSON object.*"required":\["kind"\]/);
  });

  it('skips a model that cannot take the request, and says so when none can', async () => {
    const { token } = await setup();
    await catalogue('openai', 'text-only', { ...GPT, supports_vision: false });
    await catalogue('openai', 'gpt-test');
    await publish({ capability: 'vision_judgement', provider: 'openai', model: 'text-only', rank: 0 });
    await publish({ capability: 'vision_judgement', provider: 'openai', model: 'gpt-test', rank: 1 });
    on('api.openai.com', () => responsesStream());
    const image = { type: 'image', source: { type: 'base64', media_type: 'image/webp', data: 'AAAA' } };
    const withImage = request('vision_judgement', { session_id: 'gs_img' }, {
      messages: [{ role: 'user', content: [image, { type: 'text', text: 'look' }] }] });
    expect((await message(token, withImage)).status).toBe(200);
    expect(seen.map((s) => s.body.model)).toEqual(['gpt-test']);
    // Text-only calls still use rank 0.
    seen = [];
    await message(token, request('vision_judgement', { session_id: 'gs_txt' }));
    expect(seen.map((s) => s.body.model)).toEqual(['text-only']);

    await admin('POST', '/ai/providers/openai:gpt-test/disable');
    await catalogue('openai', 'gpt-test', { ...GPT, supports_vision: false });
    const none = await message(token, withImage);
    expect(none.status).toBe(400);
    expect(none.json.error.code).toBe('route_unsupported');
  });
});

describe('admin AI page and its API', () => {
  const listing = { data: [
    { id: 'vendor/model-a:free', pricing: { prompt: '0', completion: '0' },
      supported_parameters: ['tools', 'response_format'], architecture: { input_modalities: ['text', 'image'] } },
    { id: 'vendor/model-b', pricing: { prompt: '0.0000003', completion: '0.0000012', input_cache_read: '0.00000003' },
      supported_parameters: ['tools'], architecture: { input_modalities: ['text'] } },
  ] };

  it('imports an OpenRouter model at its published prices and capabilities', async () => {
    const urls: string[] = [];
    setUpstreamFetch(async (url) => {
      urls.push(url);
      return new Response(JSON.stringify(listing), { headers: { 'content-type': 'application/json' } });
    });
    const free = await admin('POST', '/ai/models/openrouter/import', { model: 'vendor/model-a:free' });
    expect(free.status).toBe(200);
    expect(urls[0]).toBe('https://openrouter.ai/api/v1/models');
    expect(free.json).toMatchObject({ provider: 'openrouter', model: 'vendor/model-a:free', in_micro: 0, out_micro: 0,
      fee_bps: 550, supports_tools: 1, supports_structured: 1, supports_vision: 1 });
    const paid = await admin('POST', '/ai/models/openrouter/import', { model: 'vendor/model-b' });
    // $0.30 / $1.20 / $0.03 per 1M; cache writes fall back to the input price.
    expect(paid.json).toMatchObject({ in_micro: 300_000, out_micro: 1_200_000, cache_read_micro: 30_000,
      cache_write_5m_micro: 300_000, supports_structured: 0, supports_vision: 0 });
    const missing = await admin('POST', '/ai/models/openrouter/import', { model: 'vendor/nope' });
    expect(missing.status).toBe(404);
  });

  it('catalogues a model from dollar prices, with its id in the path encoded', async () => {
    const r = await admin('PUT', `/ai/models/openrouter/${encodeURIComponent('vendor/model-c:free')}`, {
      in_usd: 0.5, cache_read_usd: 0.05, cache_write_5m_usd: 0.625, cache_write_1h_usd: 1, out_usd: 2,
      source_url: 'https://example.org/pricing', supports_vision: false });
    expect(r.status).toBe(200);
    expect(r.json).toMatchObject({ model: 'vendor/model-c:free', in_micro: 500_000, cache_read_micro: 50_000,
      cache_write_5m_micro: 625_000, out_micro: 2_000_000, supports_vision: 0, supports_tools: 1 });
  });

  it('points every capability at one model, and refuses an uncatalogued one', async () => {
    const r = await admin('POST', '/ai/routes/all', { provider: 'anthropic', model: 'claude-sonnet-5', rank: 0 });
    expect(r.status).toBe(201);
    expect(r.json.routes.map((x: any) => x.capability).sort())
      .toEqual(['text_reasoning', 'text_routine', 'vision_judgement', 'vision_routine']);
    const table = await admin('GET', '/ai/routes');
    for (const cap of ['text_reasoning', 'text_routine', 'vision_judgement', 'vision_routine']) {
      expect(table.json.default_serving[cap][0].model).toBe('claude-sonnet-5');
    }
    const narrow = await admin('POST', '/ai/routes/all', { provider: 'anthropic', model: 'claude-sonnet-5', rank: 1,
      capabilities: ['text_routine'] });
    expect(narrow.json.routes).toHaveLength(1);
    const unknown = await admin('POST', '/ai/routes/all', { provider: 'openrouter', model: 'vendor/never-catalogued' });
    expect(unknown.status).toBe(409);
    expect(unknown.json.error.code).toBe('route_not_publishable');
  });

  it('renders the AI page and the licence page AI card for an admin only', async () => {
    const page = await call('GET', '/admin/ai', undefined, { Authorization: 'Bearer test-admin' });
    expect(page.status).toBe(200);
    for (const text of ['Serving now', 'Use one model for everything', 'Import a model from OpenRouter', 'Providers']) {
      expect(page.json.text).toContain(text);
    }
    const anonymous = await call('GET', '/admin/ai');
    expect(anonymous.status).not.toBe(200);
    const issued = await issue();
    const licence = await call('GET', `/admin/licenses/${issued.license.id}`, undefined,
      { Authorization: 'Bearer test-admin' });
    expect(licence.status).toBe(200);
    expect(licence.json.text).toContain('Plexora AI');
    expect(licence.json.text).toContain('Grant credits');
  });
});
