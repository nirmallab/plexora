import { env, SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import {
  anthropicStream, chatToolStream, failing, message, on, request, responsesStream, row, seen, setup, textOf, usageOf,
} from './ai_fakes';
import { admin, BASE, travel } from './helpers';

/**
 * The route table: providers other than Anthropic (translated wires), the
 * publish gate, retries and failover, the kill switch, and shadow routes.
 */

const GPT = { in_micro: 1_250_000, cache_read_micro: 125_000, cache_write_5m_micro: 1_562_500,
  cache_write_1h_micro: 1_562_500, out_micro: 10_000_000, source_url: 'https://openai.com/api/pricing' };
const OPUS = { in_micro: 4_000_000, cache_read_micro: 200_000, cache_write_5m_micro: 5_000_000,
  cache_write_1h_micro: 8_000_000, out_micro: 20_000_000, source_url: 'https://example.org/prices' };
const PASSING = { code_agreement: 0.99, marker_f1: 0.97, cache_hit_ratio: 0.9, invalid_answer_rate: 0.002,
  failure_rate: 0.001 };

/** Approve a model and give it one provider route at the given (manual) prices. */
async function approve(id: string, provider: string, providerModel: string,
  costs: Record<string, unknown> = GPT, abilities: Record<string, unknown> = {}) {
  const made = await admin('PUT', `/ai/catalog/${id}`, { name: id, reasoning: true, ...abilities });
  expect(made.status, JSON.stringify(made.json)).toBeLessThan(300);
  const { fee_bps, failover, ...prices } = costs as Record<string, unknown>;
  const route = await admin('POST', `/ai/catalog/${id}/routes`, { provider, provider_model: providerModel,
    source_url: 'https://example.org/prices', ...prices, ...(fee_bps !== undefined ? { fee_bps } : {}),
    ...(failover !== undefined ? { failover } : {}) });
  expect(route.status, JSON.stringify(route.json)).toBe(201);
  return route.json;
}

/** Assign models (in order) to a task, a module (gating.*) or every task (*). */
async function assign(pattern: string, models: string[], extra: Record<string, unknown> = {}) {
  const reply = await admin('PUT', `/ai/tasks/${encodeURIComponent(pattern)}`, { models, ...extra });
  expect(reply.status, JSON.stringify(reply.json)).toBe(200);
  return reply.json;
}

async function evaluate(subject: Record<string, unknown>, metrics = PASSING) {
  const reply = await admin('POST', '/ai/evaluations', { metrics, dataset_ids: ['synthetic:all'], ...subject });
  expect(reply.status).toBe(201);
  return reply.json;
}

/** An aggregator-served model on the gating module: approved, benched, assigned. */
async function gatingVia(id: string, provider: string, providerModel: string, costs: Record<string, unknown> = OPUS,
  abilities: Record<string, unknown> = {}) {
  await approve(id, provider, providerModel, costs, abilities);
  await evaluate({ feature: 'gating', model_id: id });
  await assign('gating.*', [id]);
}

describe('OpenAI (Responses API, translated)', () => {
  it('serves an assigned model, translating the request and the stream, and bills its catalogued cost', async () => {
    const { token } = await setup();
    await approve('gpt-test', 'openai', 'gpt-test');
    await assign('*', ['gpt-test'], { effort: 'high' });
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
    // The client learns which approved model served it (never its cost).
    expect(usage).toMatchObject({ model: 'gpt-test', provider: 'openai' });
    expect(usage.cost_micro).toBeUndefined();
    const r = await row(usage.gateway_request_id);
    expect(r).toMatchObject({ provider: 'openai', model: 'gpt-test', model_id: 'gpt-test', cost_micro: 850,
      resolved_model: 'gpt-test', status: 'ok', failover: 0, route_id: 'gpt-test@openai', task: null });
  });

  it('carries tools, tool calls and tool results across the translation', async () => {
    const { token } = await setup();
    await approve('gpt-test', 'openai', 'gpt-test');
    await assign('*', ['gpt-test']);
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
  it('refuses an aggregator-served model without a passing evaluation on the current bench', async () => {
    await setup();
    await approve('opus-or', 'openrouter', 'anthropic/claude-opus-5-5', { ...OPUS, fee_bps: 550 });
    const bare = await admin('PUT', '/ai/tasks/gating.*', { primary: 'opus-or' });
    expect(bare.status).toBe(409);
    expect(bare.json.error.code).toBe('route_not_publishable');

    const weak = await evaluate({ feature: 'gating', model_id: 'opus-or' }, { ...PASSING, code_agreement: 0.9 });
    expect(weak.passed).toBe(false);
    expect(weak.misses).toEqual(['code_agreement < 0.98']);
    expect((await admin('PUT', '/ai/tasks/gating.*', { primary: 'opus-or', evaluation_id: weak.id })).status).toBe(409);
    const old = await admin('POST', '/ai/evaluations', { feature: 'gating', model_id: 'opus-or', metrics: PASSING,
      bench_version: 'gating-0' });
    expect(old.json.passed).toBe(false);
    expect((await admin('PUT', '/ai/tasks/gating.*', { primary: 'opus-or', evaluation_id: old.json.id })).status).toBe(409);
    // An evaluation for another model, or another module, does not admit this one.
    const other = await evaluate({ feature: 'gating', model_id: 'gpt-x' });
    expect((await admin('PUT', '/ai/tasks/gating.*', { primary: 'opus-or', evaluation_id: other.id })).status).toBe(409);
    await evaluate({ feature: 'qc', model_id: 'opus-or' }, { artifact_precision: 0.95, artifact_recall: 0.95,
      cache_hit_ratio: 0.9, invalid_answer_rate: 0, failure_rate: 0 } as never);
    expect((await admin('PUT', '/ai/tasks/gating.*', { primary: 'opus-or' })).status).toBe(409);

    // The older shape (provider + its wire id) is recorded against the approved model, and admits it.
    const passing = await evaluate({ feature: 'gating', capability: 'vision_judgement', provider: 'openrouter',
      model: 'anthropic/claude-opus-5-5' });
    expect(passing).toMatchObject({ passed: true, model_id: 'opus-or' });
    const ok = await admin('PUT', '/ai/tasks/gating.*', { primary: 'opus-or' });
    expect(ok.status).toBe(200);
    expect(ok.json.assignments[0]).toMatchObject({ model_id: 'opus-or', evaluation_id: passing.id, unbenched: 0 });
    // A model nobody approved cannot be assigned at all, even a direct provider's.
    const unknown = await admin('PUT', '/ai/tasks/*', { primary: 'gpt-none' });
    expect(unknown.status).toBe(409);
  });

  it('OpenRouter: chat-completions translation, tool calls back as tool_use, its fee and reported cost', async () => {
    const { token } = await setup();
    await gatingVia('opus-or', 'openrouter', 'anthropic/claude-opus-5-5', { ...OPUS, fee_bps: 550 });
    on('openrouter.ai/api/v1/chat/completions', () => chatToolStream());
    const reply = await message(token, request('vision_judgement', {}, {
      tools: [{ name: 'read_board', description: 'Read facts.', input_schema: { type: 'object' } }],
    }));
    expect(reply.status).toBe(200);
    const sent = seen[0]!;
    expect(sent.headers.get('authorization')).toBe('Bearer test-openrouter');
    expect(sent.headers.get('x-orcarouter-session-id')).toBeNull();     // OrcaRouter's header only
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
    await gatingVia('opus-orca', 'orcarouter', 'claude-opus-5-5');
    on('api.orcarouter.ai/v1/messages', () => anthropicStream(undefined, { 'x-orca-resolved-model': 'claude-opus-5-5' }));
    const reply = await message(token, request());
    expect(reply.status).toBe(200);
    expect(seen[0]!.headers.get('x-orcarouter-include-cost')).toBe('true');
    expect(seen[0]!.body).toMatchObject({ model: 'claude-opus-5-5', metadata: { user_id: expect.any(String) } });
    expect((await row(usageOf(reply).gateway_request_id))!.resolved_model).toBe('claude-opus-5-5');
  });

  it('OrcaRouter: a session sends one stable session header, and message breakpoints pass through', async () => {
    const { token } = await setup();
    await gatingVia('opus-orca', 'orcarouter', 'claude-opus-5-5');
    on('api.orcarouter.ai/v1/messages', () => anthropicStream());
    const marked = [{ role: 'user', content: [{ type: 'text', text: '{"packet": 1}', cache_control: { type: 'ephemeral' } }] }];
    expect((await message(token, request('vision_judgement', {}, { messages: marked }))).status).toBe(200);
    expect((await message(token, request())).status).toBe(200);
    expect((await message(token, request('vision_judgement', { session_id: 'gs_other' }))).status).toBe(200);
    const sessions = seen.map((s) => s.headers.get('x-orcarouter-session-id'));
    // Not the session id itself: the gateway's hashed cache key.
    expect(sessions[0]).toMatch(/^[0-9a-f]{32}$/);
    expect(sessions[1]).toBe(sessions[0]);
    expect(sessions[2]).not.toBe(sessions[0]);
    expect((seen[0]!.body as { messages: { content: { cache_control?: unknown }[] }[] }).messages[0]!.content[0]!
      .cache_control).toEqual({ type: 'ephemeral' });
  });

  it('SayGM: only its confidential (TEE) models may be routed', async () => {
    await setup();
    await admin('PUT', '/ai/catalog/any', { name: 'Any' });
    const frontier = await admin('POST', '/ai/catalog/any/routes', { provider: 'saygm', provider_model: 'claude', ...GPT });
    expect(frontier.status).toBe(400);
    await approve('llama', 'saygm', 'llama-4-70b-TEE');
  });

  it('SayGM: chat-completions with Bearer auth, no OpenRouter fields, and the usage chunk asked for', async () => {
    const { token } = await setup();
    await gatingVia('gemma-tee', 'saygm', 'gemma-4-31b-tee');
    on('api.saygm.com/v1/chat/completions', () => chatToolStream());
    const reply = await message(token, request());
    expect(reply.status).toBe(200);
    const sent = seen[0]!;
    expect(sent.headers.get('authorization')).toBe('Bearer test-saygm');
    expect(sent.headers.get('x-api-key')).toBeNull();
    expect(sent.body).toMatchObject({ model: 'gemma-4-31b-tee', stream: true, stream_options: { include_usage: true } });
    for (const key of ['provider', 'usage', 'session_id']) expect(sent.body).not.toHaveProperty(key);
    const usage = usageOf(reply);
    expect(usage.usage).toMatchObject({ input_uncached: 100, cache_read: 400, output_tokens: 20 });
    expect(await row(usage.gateway_request_id)).toMatchObject({ provider: 'saygm', reported_cost_micro: 2100 });
  });
});

describe('retries, failover and the kill switch', () => {
  /** Two models for every task: Claude Opus on Anthropic, then gpt-test on OpenAI. */
  async function twoModels(failover = 'outage') {
    await approve('claude-opus-5-5', 'anthropic', 'claude-opus-5-5', { ...OPUS, failover });
    await approve('gpt-test', 'openai', 'gpt-test');
    await assign('*', ['claude-opus-5-5', 'gpt-test']);
  }

  it('retries the same route, and moves to the next only once its circuit opens', async () => {
    const { token } = await setup();
    await twoModels();
    on('api.anthropic.com', failing(503));
    on('api.openai.com', () => responsesStream());

    // Three tries on Anthropic, circuit still closed (5 failures open it): refused, key freed.
    const first = await message(token, request(), { key: 'same-key-001' });
    expect(first.status).toBe(503);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com', 'api.anthropic.com', 'api.anthropic.com']);

    // The retry (same key) opens the circuit on its second try and fails over to the next MODEL.
    seen.length = 0;
    const second = await message(token, request(), { key: 'same-key-001' });
    expect(second.status).toBe(200);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com', 'api.anthropic.com', 'api.openai.com']);
    expect(second.events[0]!.data).toMatchObject({ failover: 'model', model: 'gpt-test', provider: 'openai' });
    const r = await row(usageOf(second).gateway_request_id);
    expect(r).toMatchObject({ provider: 'openai', failover: 1, attempts: 3 });

    // While the circuit is open nobody waits on Anthropic.
    seen.length = 0;
    const third = await message(token, request('vision_judgement', { session_id: 'gs_other' }));
    expect(third.status).toBe(200);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.openai.com']);

    // Half-open after AI_CIRCUIT_OPEN_S: a healthy Anthropic serves a new session again...
    travel(25);
    on('api.anthropic.com', () => anthropicStream());
    seen.length = 0;
    const fresh = await message(token, request('vision_judgement', { session_id: 'gs_new' }));
    expect(new URL(seen[0]!.url).host).toBe('api.anthropic.com');
    expect(fresh.status).toBe(200);
    // ...while the session that failed over stays on its route (its cache is warm there).
    seen.length = 0;
    await message(token, request());
    expect(new URL(seen[0]!.url).host).toBe('api.openai.com');
  });

  it('says when a refusal is an open circuit, and for how long', async () => {
    const { token } = await setup();
    await approve('claude-opus-5-5', 'anthropic', 'claude-opus-5-5', OPUS);
    await assign('*', ['claude-opus-5-5']);
    on('api.anthropic.com', failing(503));

    // Before the circuit opens: a plain outage, no circuit in the details.
    const first = await message(token, request(), { key: 'circ-key-001' });
    expect(first.status).toBe(503);
    expect(first.json.error).toMatchObject({ code: 'provider_unavailable',
      details: { failure: expect.any(String), model: 'claude-opus-5-5', provider: 'anthropic' } });
    expect(first.json.error.details.circuit_open_s).toBeUndefined();

    // The call that opens it says how long it is rested (AI_CIRCUIT_OPEN_S), still as provider_unavailable.
    const second = await message(token, request(), { key: 'circ-key-001' });
    expect(second.status).toBe(503);
    expect(second.json.error.code).toBe('provider_unavailable');
    expect(second.json.error.details.circuit_open_s).toBe(20);
    expect(second.json.error.retry_after).toBeGreaterThanOrEqual(20);

    // While open: nothing reaches the provider, and retry_after is the time left.
    seen.length = 0;
    travel(5);
    const third = await message(token, request(), { key: 'circ-key-002' });
    expect(seen).toEqual([]);
    expect(third.json.error.details).toMatchObject({ failure: 'circuit_open' });
    expect(third.json.error.retry_after).toBeGreaterThan(0);
    expect(third.json.error.retry_after).toBeLessThanOrEqual(15);
  });

  it("fails over on any exhausted retry when the route says 'error', and never on the request's own fault", async () => {
    const { token } = await setup();
    await twoModels('error');
    on('api.anthropic.com', failing(529));
    on('api.openai.com', () => responsesStream());
    const reply = await message(token, request('vision_judgement', { session_id: 'gs_err' }));
    expect(reply.status).toBe(200);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com', 'api.anthropic.com', 'api.anthropic.com',
      'api.openai.com']);

    on('api.anthropic.com', failing(400));
    seen.length = 0;
    const rejected = await message(token, request('vision_judgement', { session_id: 'gs_bad' }));
    expect(rejected.status).toBe(400);
    expect(rejected.json.error.code).toBe('provider_rejected');
    expect(seen).toHaveLength(1);
  });

  it('tries every provider of the primary model before the fallback model, and a reorder switches provider', async () => {
    const { token } = await setup();
    await approve('claude-opus-5-5', 'anthropic', 'claude-opus-5-5', OPUS);
    // Approving from a listing at its published price is covered in ai_catalog.test.ts; by hand here.
    expect((await admin('POST', '/ai/catalog/claude-opus-5-5/routes', { provider: 'orcarouter',
      provider_model: 'anthropic/claude-opus-5.5', ...OPUS })).status).toBe(201);
    await approve('gpt-test', 'openai', 'gpt-test');
    await evaluate({ feature: '*', model_id: 'claude-opus-5-5' });
    await assign('*', ['claude-opus-5-5', 'gpt-test']);
    on('api.anthropic.com', failing(503));
    on('api.orcarouter.ai', () => anthropicStream());
    on('api.openai.com', () => responsesStream());
    const reply = await message(token, request('vision_judgement', { session_id: 'gs_prov' }));
    expect(reply.status).toBe(200);
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com', 'api.anthropic.com', 'api.anthropic.com',
      'api.orcarouter.ai']);
    expect(reply.events[0]!.data).toMatchObject({ failover: 'provider', model: 'claude-opus-5-5', provider: 'orcarouter' });
    expect(seen[3]!.body.model).toBe('anthropic/claude-opus-5.5');

    // Make OrcaRouter primary: no assignment changes, the model's first provider does.
    const moved = await admin('POST', '/ai/catalog/claude-opus-5-5/routes/orcarouter/primary');
    expect(moved.json.routes.map((r: any) => [r.provider, r.rank])).toEqual([['orcarouter', 0], ['anthropic', 1]]);
    on('api.anthropic.com', () => anthropicStream());
    seen.length = 0;
    const after = await message(token, request('vision_judgement', { session_id: 'gs_prov2' }));
    expect(after.events[0]!.data.failover).toBeUndefined();
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.orcarouter.ai']);
    // And back with ↓, which is the same reorder from the other side.
    await admin('POST', '/ai/catalog/claude-opus-5-5/routes/orcarouter/move', { to: 1 });
    seen.length = 0;
    await message(token, request('vision_judgement', { session_id: 'gs_prov3' }));
    expect(new URL(seen[0]!.url).host).toBe('api.anthropic.com');
    // Switching a route off takes it out of serving at once.
    await admin('PATCH', '/ai/catalog/claude-opus-5-5/routes/anthropic', { enabled: false });
    seen.length = 0;
    await message(token, request('vision_judgement', { session_id: 'gs_prov4' }));
    expect(new URL(seen[0]!.url).host).toBe('api.orcarouter.ai');
  });

  it('the kill switch takes a provider out of every route at once, and back', async () => {
    const { token } = await setup();
    await twoModels();
    on('api.anthropic.com', () => anthropicStream());
    on('api.openai.com', () => responsesStream());
    expect((await admin('POST', '/ai/providers/anthropic/disable', { reason: 'incident' })).json.forced).toBe('open');
    await message(token, request('vision_judgement', { session_id: 'gs_k1' }));
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.openai.com']);
    await admin('POST', '/ai/providers/anthropic/enable');
    seen.length = 0;
    await message(token, request('vision_judgement', { session_id: 'gs_k2' }));
    expect(seen.map((s) => new URL(s.url).host)).toEqual(['api.anthropic.com']);
    const providers = await admin('GET', '/ai/providers');
    expect(providers.json.providers.find((p: any) => p.provider === 'openai').configured).toBe(true);
  });

  it('the dev route may name any catalogued provider/model, or an approved model, at cost', async () => {
    const s = await setup();
    await admin('PATCH', `/ai/accounts/${s.account_id}`, { mode: 'dev' });
    const token = await s.reissue();
    await approve('gpt-test', 'openai', 'gpt-test');
    on('api.openai.com', () => responsesStream());
    const dev = { path: '/v1/ai/dev/messages' };
    const reply = await message(token, { ...request(), model: 'openai/gpt-test' }, dev);
    expect(reply.status).toBe(200);
    expect(usageOf(reply)).toMatchObject({ billing: 'dev', price_micro: 850, cost_micro: 850, provider: 'openai',
      model: 'gpt-test', provider_model: 'gpt-test' });
    const byId = await message(token, { ...request(), model: 'gpt-test' }, dev);
    expect(byId.status).toBe(200);
    const unknown = await message(token, { ...request(), model: 'openai/gpt-none' }, dev);
    expect(unknown.status).toBe(400);
    const frontier = await message(token, { ...request(), model: 'saygm/claude' }, dev);
    expect(frontier.status).toBe(400);
  });
});

describe('shadow assignments', () => {
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
    await approve('gpt-test', 'openai', 'gpt-test');
    expect((await admin('PUT', '/ai/tasks/gating.*', { shadow_model: 'gpt-test', shadow_pct: 100 })).status).toBe(200);
    on('api.anthropic.com', () => anthropicStream('{"kind":"t2_confirm","direction":"about_right","notes":"a"}'));
    on('api.openai.com', () => responsesStream('{"kind":"t2_confirm","direction":"about_right","notes":"b"}'));
    const agreed = await message(token, request('vision_judgement', { session_id: 'gs_sh1' }));
    const servedId = usageOf(agreed).gateway_request_id;
    const shadow = await shadowRow(servedId);
    expect(shadow).toMatchObject({ billing: 'shadow', provider: 'openai', model_id: 'gpt-test', charged_micro: 0,
      price_micro: 0, shadow_agree: 1, cost_micro: 850 });
    // The served call carried the shadow text capture; the client saw only the served answer.
    expect(textOf(agreed)).toContain('"notes":"a"');

    on('api.openai.com', () => responsesStream('{"kind":"t2_confirm","direction":"too_low"}'));
    const disagreed = await message(token, request('vision_judgement', { session_id: 'gs_sh2' }));
    expect((await shadowRow(usageOf(disagreed).gateway_request_id))!.shadow_agree).toBe(0);

    const report = await admin('GET', '/ai/shadow');
    expect(report.json.candidates[0]).toMatchObject({ provider: 'openai', model_id: 'gpt-test', calls: 2, compared: 2,
      agreed: 1, agreement: 0.5 });
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
    await approve('claude-opus-5-5', 'anthropic', 'claude-opus-5-5', OPUS);
    await admin('PUT', '/ai/tasks/gating.*', { shadow_model: 'claude-opus-5-5', shadow_pct: 100 });
    on('api.anthropic.com', () => anthropicStream());
    await message(token, request('vision_judgement', { session_id: 'gs_same' }));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(seen).toHaveLength(1);
  });
});

describe('what a model can do', () => {
  it('puts the schema in the prompt for a model without structured output, after every cached byte', async () => {
    const { token } = await setup();
    await gatingVia('free-model', 'openrouter', 'free/model:free', GPT, { supports_structured: false });
    on('openrouter.ai', () => chatToolStream());
    expect((await message(token, request())).status).toBe(200);
    const sent = seen[0]!.body;
    expect(sent.response_format).toBeUndefined();
    expect(sent.messages[0].content[0].text).toBe('You are a Plexora worker.');
    const last = sent.messages[sent.messages.length - 1].content;
    expect(last[0]).toEqual({ type: 'text', text: '{"packet": 1}' });
    expect(last[1].text).toMatch(/^Reply with ONLY one JSON object.*"required":\["kind"\]/);
  });

  it('pins an OpenRouter session to the backend that served it, so a free model keeps its cache', async () => {
    const { token, reissue } = await setup();
    await gatingVia('free-model', 'openrouter', 'free/model:free', { ...GPT, in_micro: 0, cache_read_micro: 0,
      cache_write_5m_micro: 0, cache_write_1h_micro: 0, out_micro: 0 });
    on('openrouter.ai', () => chatToolStream('Chutes'));
    expect((await message(token, request('vision_judgement', { session_id: 'gs_pin' }))).status).toBe(200);
    expect(seen[0]!.body.provider).toEqual({ data_collection: 'deny' });
    expect((await message(token, request('vision_judgement', { session_id: 'gs_pin' }))).status).toBe(200);
    expect(seen[1]!.body.provider).toEqual({ data_collection: 'deny', order: ['Chutes'], allow_fallbacks: true });
    // Another session is not pinned by this one, and a stale pin lapses.
    await message(token, request('vision_judgement', { session_id: 'gs_other_pin' }));
    expect(seen[2]!.body.provider).toEqual({ data_collection: 'deny' });
    travel(31 * 60);
    await message(await reissue(), request('vision_judgement', { session_id: 'gs_pin' }));
    expect(seen[3]!.body.provider).toEqual({ data_collection: 'deny' });
  });

  it('skips a model that cannot take the request, and says so when none can', async () => {
    const { token } = await setup();
    await approve('text-only', 'openai', 'text-only', GPT, { supports_vision: false });
    await approve('gpt-test', 'openai', 'gpt-test');
    await assign('*', ['text-only', 'gpt-test']);
    on('api.openai.com', () => responsesStream());
    const image = { type: 'image', source: { type: 'base64', media_type: 'image/webp', data: 'AAAA' } };
    const withImage = request('vision_judgement', { session_id: 'gs_img' }, {
      messages: [{ role: 'user', content: [image, { type: 'text', text: 'look' }] }] });
    expect((await message(token, withImage)).status).toBe(200);
    expect(seen.map((s) => s.body.model)).toEqual(['gpt-test']);
    // Text-only calls still use the primary model.
    seen.length = 0;
    await message(token, request('vision_judgement', { session_id: 'gs_txt' }));
    expect(seen.map((s) => s.body.model)).toEqual(['text-only']);

    await admin('PUT', '/ai/catalog/gpt-test', { supports_vision: false });
    const none = await message(token, withImage);
    expect(none.status).toBe(400);
    expect(none.json.error.code).toBe('route_unsupported');
  });
});
