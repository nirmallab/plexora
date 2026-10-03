import { describe, expect, it } from 'vitest';

import {
  builtinProfile, clamp, describeResolved, effortFields, profileFor, resolveEffort,
} from '../../src/ai/effort';
import { clearListingCache } from '../../src/ai/pricing';
import { setUpstreamFetch } from '../../src/ai/providers';
import openrouter from './fixtures/openrouter_models.json';
import {
  anthropicStream, message, on, request, responsesStream, row, seen, setup, sseOf, textOf, usageOf,
} from './ai_fakes';
import { admin, count } from './helpers';

/**
 * Effort: one scale for the admin, fitted to each model. The built-in
 * profiles, the level each model is sent (or not), the shape on each wire,
 * the retry when a model refuses it, and reasoning kept from the client.
 */

const PRICES = { in_micro: 1_000_000, cache_read_micro: 100_000, cache_write_5m_micro: 1_250_000,
  cache_write_1h_micro: 2_000_000, out_micro: 5_000_000, source_url: 'https://example.org/prices' };

/** A model on a direct provider, assigned to every task with an effort. */
async function serving(id: string, provider: string, providerModel: string, effort: string,
  extra: Record<string, unknown> = {}) {
  const made = await admin('PUT', `/ai/catalog/${id}`, { name: id, reasoning: true, ...extra });
  expect(made.status, JSON.stringify(made.json)).toBeLessThan(300);
  const route = await admin('POST', `/ai/catalog/${id}/routes`, { provider, provider_model: providerModel, ...PRICES });
  expect(route.status, JSON.stringify(route.json)).toBe(201);
  const assigned = await admin('PUT', '/ai/tasks/*', { models: [id], effort });
  expect(assigned.status, JSON.stringify(assigned.json)).toBe(200);
}

const ask = (task: string, maxTokens = 16000) => ({ ...request('vision_judgement', {}, { max_tokens: maxTokens }), task });

describe('effort profiles', () => {
  it('knows the major models, whatever the spelling', () => {
    expect(builtinProfile('claude-opus-5-5')?.wire).toBe('effort');
    expect(builtinProfile('anthropic/claude-opus-5.5')?.default).toBe('medium');
    expect(builtinProfile('claude-sonnet-5-5')?.default).toBe('high');
    expect(builtinProfile('claude-haiku-4-5-20251001')?.wire).toBe('budget');
    expect(builtinProfile('claude-opus-4-7')?.wire).toBe('adaptive');
    expect(builtinProfile('openai/gpt-5.2')?.levels).toContain('xhigh');
    expect(builtinProfile('z-ai/glm-5.3-flash:free')?.wire).toBe('none');
    expect(builtinProfile('acme-thinker')).toBeNull();
  });

  it('fits a level to the nearest one a model takes, ties going lower', () => {
    expect(clamp('max', ['low', 'medium', 'high'])).toBe('high');
    expect(clamp('xhigh', ['low', 'high'])).toBe('high');
    expect(clamp('medium', ['low', 'high'])).toBe('low');
    expect(clamp('none', ['low', 'medium'])).toBe('low');
    expect(clamp('high', [])).toBeNull();
  });

  it('auto is the task level; default sends nothing; a model without effort gets none', () => {
    const opus = profileFor({ id: 'claude-opus-5-5' }).profile;
    expect(resolveEffort('auto', 'high', opus)).toEqual({ level: 'high', wire: 'effort', asked: 'high' });
    expect(resolveEffort('default', 'high', opus).level).toBeNull();
    expect(resolveEffort(null, 'high', opus).level).toBeNull();
    const glm = profileFor({ id: 'glm-5-3-flash-free' }).profile;
    expect(describeResolved(resolveEffort('high', 'low', glm))).toBe('not sent');
    const gpt = profileFor({ id: 'gpt-5-1' }).profile;
    expect(describeResolved(resolveEffort('max', 'low', gpt))).toBe('max → high');
    const haiku = profileFor({ id: 'claude-haiku-4-5' }).profile;
    expect(describeResolved(resolveEffort('low', 'low', haiku), haiku.budgets)).toBe('low, no thinking');
  });

  it('a stored profile wins over the built-in one; an unknown reasoning model gets the generic three', () => {
    const stored = JSON.stringify({ levels: ['low', 'high'], default: 'high', wire: 'reasoning', source: 'admin' });
    expect(profileFor({ id: 'claude-opus-5-5', effort_json: stored })).toMatchObject({ source: 'admin',
      profile: { levels: ['low', 'high'] } });
    expect(profileFor({ id: 'acme-thinker', reasoning: 1 })).toMatchObject({ source: 'generic',
      profile: { levels: ['low', 'medium', 'high'], wire: 'reasoning' } });
    expect(profileFor({ id: 'acme-thinker', reasoning: 0 }).source).toBe('unknown');
  });

  it('says each wire its own way', () => {
    expect(effortFields('anthropic', 'high', 'effort', undefined, 16000)).toEqual({ output_config: { effort: 'high' } });
    expect(effortFields('anthropic', 'high', 'adaptive', undefined, 16000)).toEqual({ thinking: { type: 'adaptive' },
      output_config: { effort: 'high' } });
    expect(effortFields('anthropic', 'high', 'budget', undefined, 16000)).toEqual({ thinking: { type: 'enabled',
      budget_tokens: 10000 } });
    // The budget leaves the answer room, and a cap too small for both sends no thinking at all.
    expect(effortFields('anthropic', 'max', 'budget', undefined, 8000)).toEqual({ thinking: { type: 'enabled',
      budget_tokens: 6976 } });
    expect(effortFields('anthropic', 'high', 'budget', undefined, 1024)).toEqual({});
    expect(effortFields('anthropic', 'high', 'reasoning', undefined, 16000)).toEqual({});
    expect(effortFields('openai_chat', 'high', 'effort', undefined, 0)).toEqual({ reasoning: { effort: 'high' } });
    expect(effortFields('openai_chat', 'low', 'budget', undefined, 0)).toEqual({});
    expect(effortFields('openai_chat', 'high', 'none', undefined, 0)).toEqual({});
  });
});

describe('effort on the wire', () => {
  it('Opus 5.5: the effort alone, any level up to max, recorded on the request', async () => {
    const { token } = await setup();
    await serving('claude-opus-5-5', 'anthropic', 'claude-opus-5-5', 'max');
    on('api.anthropic.com/v1/messages', () => anthropicStream());
    const reply = await message(token, ask('gating.threshold_evaluation'));
    expect(reply.status).toBe(200);
    expect(seen[0]!.body.output_config).toMatchObject({ effort: 'max' });
    expect(seen[0]!.body.thinking).toBeUndefined();
    expect((await row(usageOf(reply).gateway_request_id))!.effort).toBe('max');
  });

  it("auto: each task's own level, and Haiku 4.5 a thinking budget, never an effort", async () => {
    const { token } = await setup();
    await serving('claude-haiku-4-5', 'anthropic', 'claude-haiku-4-5-20251001', 'auto');
    on('api.anthropic.com/v1/messages', () => anthropicStream());
    expect((await message(token, ask('gating.threshold_evaluation'))).status).toBe(200);
    expect(seen[0]!.body.thinking).toEqual({ type: 'enabled', budget_tokens: 10000 });
    expect(seen[0]!.body.output_config?.effort).toBeUndefined();
    // Biological context is a low task: on Haiku, no thinking at all.
    expect((await message(token, ask('gating.biological_context'))).status).toBe(200);
    expect(seen[1]!.body.thinking).toBeUndefined();
    expect(seen[1]!.body.output_config?.effort).toBeUndefined();
  });

  it('Opus 4.7 thinks only when asked: adaptive thinking with the effort', async () => {
    const { token } = await setup();
    await serving('claude-opus-4-7', 'anthropic', 'claude-opus-4-7', 'xhigh');
    on('api.anthropic.com/v1/messages', () => anthropicStream());
    expect((await message(token, ask('gating.final_validation'))).status).toBe(200);
    expect(seen[0]!.body.thinking).toEqual({ type: 'adaptive' });
    expect(seen[0]!.body.output_config).toMatchObject({ effort: 'xhigh' });
  });

  it('OpenAI: reasoning.effort, fitted to the levels the model has', async () => {
    const { token } = await setup();
    await serving('gpt-5-1', 'openai', 'gpt-5.1', 'max');
    on('api.openai.com/v1/responses', () => responsesStream());
    expect((await message(token, ask('gating.final_validation'))).status).toBe(200);
    expect(seen[0]!.body.reasoning).toEqual({ effort: 'high' });
  });

  it('a model that refuses its effort is asked once more without it, and the refusal is recorded', async () => {
    const { token } = await setup();
    await serving('claude-opus-5-5', 'anthropic', 'claude-opus-5-5', 'high');
    let calls = 0;
    on('api.anthropic.com/v1/messages', () => (++calls === 1
      ? new Response('{"type":"error","error":{"type":"invalid_request_error","message":"output_config.effort: not supported"}}',
        { status: 400 })
      : anthropicStream()));
    const reply = await message(token, ask('gating.threshold_evaluation'));
    expect(reply.status).toBe(200);
    expect(seen).toHaveLength(2);
    expect(seen[0]!.body.output_config.effort).toBe('high');
    expect(seen[1]!.body.output_config?.effort).toBeUndefined();
    expect((await row(usageOf(reply).gateway_request_id))!.effort).toBeNull();
    expect(await count('events', "kind = 'ai.effort_rejected'")).toBe(1);
  });

  it("never passes a model's thinking to the client", async () => {
    const { token } = await setup();
    await serving('claude-opus-5-5', 'anthropic', 'claude-opus-5-5', 'auto');
    on('api.anthropic.com/v1/messages', () => sseOf([
      ['message_start', { type: 'message_start', message: { id: 'msg_t', model: 'claude-opus-5-5',
        usage: { input_tokens: 10, output_tokens: 1 } } }],
      ['content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } }],
      ['content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta',
        thinking: 'The private reasoning.' } }],
      ['content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'signature_delta', signature: 'sig' } }],
      ['content_block_stop', { type: 'content_block_stop', index: 0 }],
      ['content_block_start', { type: 'content_block_start', index: 1, content_block: { type: 'text', text: '' } }],
      ['content_block_delta', { type: 'content_block_delta', index: 1, delta: { type: 'text_delta', text: '{"kind":"x"}' } }],
      ['content_block_stop', { type: 'content_block_stop', index: 1 }],
      ['message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 30 } }],
      ['message_stop', { type: 'message_stop' }],
    ]));
    const reply = await message(token, ask('gating.threshold_evaluation'));
    expect(reply.status).toBe(200);
    expect(textOf(reply)).toBe('{"kind":"x"}');
    expect(JSON.stringify(reply.events)).not.toContain('private reasoning');
    expect(reply.events.filter((e) => e.event === 'content_block_start').map((e) => e.data.index)).toEqual([1]);
    expect(usageOf(reply).usage).toMatchObject({ output_tokens: 30 });
  });
});

describe('effort in the admin', () => {
  it('takes the levels a provider lists for a model nothing else describes', async () => {
    clearListingCache();
    const gpt = (openrouter as { data: Array<Record<string, any>> }).data.find((m) => m.id === 'openai/gpt-6.1-sol-pro')!;
    const listed = { data: [{ ...gpt, id: 'acme/thinker-1', name: 'Acme: Thinker 1' }] };
    setUpstreamFetch(async () => new Response(JSON.stringify(listed), { headers: { 'content-type': 'application/json' } }));
    const reply = await admin('POST', '/ai/catalog/import', { provider: 'openrouter', provider_model: 'acme/thinker-1' });
    expect(reply.status, JSON.stringify(reply.json)).toBe(201);
    const model = (await admin('GET', '/ai/catalog')).json.models.find((m: any) => m.id === 'thinker-1');
    expect(model.effort).toMatchObject({ source: 'listing', levels: ['low', 'medium', 'high', 'xhigh', 'max'],
      default: 'medium', wire: 'reasoning' });
    clearListingCache();
  });

  it('an admin sets and clears a profile; a bad level or scale is refused', async () => {
    await admin('PUT', '/ai/catalog/claude-opus-5-5', { name: 'Claude Opus 5.5', reasoning: true });
    const set = await admin('PUT', '/ai/catalog/claude-opus-5-5', { effort_wire: 'effort', effort_levels: 'low, high',
      effort_default: 'high' });
    expect(set.status, JSON.stringify(set.json)).toBe(200);
    let model = (await admin('GET', '/ai/catalog')).json.models.find((m: any) => m.id === 'claude-opus-5-5');
    expect(model.effort).toMatchObject({ source: 'admin', levels: ['low', 'high'], default: 'high' });
    expect((await admin('PUT', '/ai/catalog/claude-opus-5-5', { effort_wire: 'effort', effort_levels: 'low, huge' }))
      .status).toBe(400);
    expect((await admin('PUT', '/ai/catalog/claude-opus-5-5', { effort_wire: 'loud' })).status).toBe(400);
    expect((await admin('PUT', '/ai/catalog/claude-opus-5-5', { effort_wire: '' })).status).toBe(200);
    model = (await admin('GET', '/ai/catalog')).json.models.find((m: any) => m.id === 'claude-opus-5-5');
    expect(model.effort).toMatchObject({ source: 'builtin', default: 'medium' });
  });

  it('a task takes auto or any level of the scale, nothing else', async () => {
    await admin('PUT', '/ai/catalog/claude-opus-5-5', { name: 'Claude Opus 5.5', reasoning: true });
    await admin('POST', '/ai/catalog/claude-opus-5-5/routes', { provider: 'anthropic', provider_model: 'claude-opus-5-5',
      ...PRICES });
    expect((await admin('PUT', '/ai/tasks/*', { models: ['claude-opus-5-5'], effort: 'xhigh' })).status).toBe(200);
    expect((await admin('PUT', '/ai/tasks/*', { models: ['claude-opus-5-5'], effort: 'auto' })).status).toBe(200);
    const bad = await admin('PUT', '/ai/tasks/*', { models: ['claude-opus-5-5'], effort: 'extreme' });
    expect(bad.status).toBe(400);
    const tasks = (await admin('GET', '/ai/tasks')).json;
    const all = tasks.rows.find((r: any) => r.pattern === '*');
    expect(all.serve[0].effort_spec).toBe('auto');
  });
});
