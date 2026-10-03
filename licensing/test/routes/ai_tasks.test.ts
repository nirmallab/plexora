import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { CAPABILITIES } from '../../src/ai/catalog';
import { MODULES, TASKS, WIRE_TASK } from '../../src/ai/tasks';
import {
  anthropicStream, chatToolStream, message, on, request, responsesStream, row, seen, setup, usageOf,
} from './ai_fakes';
import { admin } from './helpers';

/**
 * Task routing: which approved model serves a call, from the task it names.
 * Assignments are made to a task, a module (`gating.*`) or every task (`*`),
 * and the most specific one wins. The hosts a call reaches prove which.
 */

const OPENAI = { in_usd: 1.25, out_usd: 10, source_url: 'https://example.org/prices' };
const ANTHROPIC = { in_usd: 4, out_usd: 20, source_url: 'https://example.org/prices' };

async function approve(id: string, provider: string, wire: string, prices: Record<string, unknown> = OPENAI,
  abilities: Record<string, unknown> = {}) {
  expect((await admin('PUT', `/ai/catalog/${id}`, { name: id, reasoning: true, ...abilities })).status).toBeLessThan(300);
  const r = await admin('POST', `/ai/catalog/${id}/routes`, { provider, provider_model: wire, ...prices });
  expect(r.status, JSON.stringify(r.json)).toBe(201);
}

async function assign(pattern: string, body: Record<string, unknown>) {
  const r = await admin('PUT', `/ai/tasks/${encodeURIComponent(pattern)}`, body);
  expect(r.status, JSON.stringify(r.json)).toBe(200);
  return r.json;
}

const hosts = () => seen.map((s) => new URL(s.url).host);
const call = (token: string, task: string | null, ctx: Record<string, unknown> = {}, capability = 'vision_judgement',
  extra: Record<string, unknown> = {}) => message(token, { ...request(capability, ctx, extra),
  ...(task ? { task } : {}) });

/** Every provider answers; the host tells which model served. */
function everyoneAnswers() {
  on('api.openai.com', () => responsesStream());
  on('api.anthropic.com', () => anthropicStream());
  on('api.orcarouter.ai', () => anthropicStream());
  on('openrouter.ai', () => chatToolStream());
}

describe('the task registry', () => {
  it('lists every module with tasks that name what they need, a known capability and their packet kinds', () => {
    expect(MODULES.map((m) => m.id)).toEqual(['gating', 'qc', 'chat']);
    expect(Object.keys(TASKS)).toContain('gating.threshold_evaluation');
    expect(Object.keys(TASKS)).toContain('qc.final_review');
    for (const task of Object.values(TASKS)) {
      expect(task.id).toMatch(WIRE_TASK);
      expect(CAPABILITIES).toContain(task.capability);
      expect(task.kinds.length).toBeGreaterThan(0);
      expect(typeof task.requires.vision).toBe('boolean');
    }
  });
});

describe('resolution by task', () => {
  it('serves the most specific assignment: the task, then its module, then every task', async () => {
    const { token } = await setup();
    await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
    await approve('gpt-test', 'openai', 'gpt-test');
    await approve('opus', 'anthropic', 'claude-opus-5-5', ANTHROPIC);
    await approve('opus-orca', 'orcarouter', 'claude-opus-5-5', ANTHROPIC);
    await assign('*', { primary: 'gpt-test' });
    await assign('gating.*', { primary: 'opus' });
    const own = await assign('gating.threshold_evaluation', { primary: 'opus-orca', fallback_1: 'opus' });
    // Allowed here only because the Worker allows unbenched routes: flagged so.
    expect(own.assignments[0]).toMatchObject({ unbenched: 1, evaluation_id: null });
    everyoneAnswers();

    const cases: Array<[string | null, string | null, string]> = [
      ['gating.threshold_evaluation', 'gating', 'api.orcarouter.ai'],
      ['gating.image_inspection', 'gating', 'api.anthropic.com'],
      ['qc.blur', 'qc', 'api.openai.com'],
      [null, 'gating', 'api.anthropic.com'],         // an older client: its feature names the module
      [null, 'chat', 'api.openai.com'],
      ['future.thing', 'future', 'api.openai.com'],  // a task this Worker does not know yet: the default
    ];
    for (const [task, feature, host] of cases) {
      seen.length = 0;
      const reply = await call(token, task, { feature, session_id: `gs_${task ?? feature}` });
      expect(reply.status, `${task} ${JSON.stringify(reply.json)}`).toBe(200);
      expect(hosts(), `${task ?? feature}`).toEqual([host]);
      const r = await row(usageOf(reply).gateway_request_id);
      expect(r).toMatchObject({ task, model_id: host === 'api.openai.com' ? 'gpt-test'
        : host === 'api.orcarouter.ai' ? 'opus-orca' : 'opus' });
    }

    const view = (await admin('GET', '/ai/tasks')).json;
    const effective = (pattern: string) => view.rows.find((r: any) => r.pattern === pattern).effective;
    expect(effective('gating.threshold_evaluation')).toMatchObject({ level: 'task', source: 'gating.threshold_evaluation' });
    expect(effective('gating.threshold_evaluation').chain.map((l: any) => l.model_id)).toEqual(['opus-orca', 'opus']);
    expect(effective('gating.image_inspection')).toMatchObject({ level: 'module', source: 'gating.*' });
    expect(effective('qc.blur')).toMatchObject({ level: 'global', source: '*' });

    // Reset: the task inherits its module's model again.
    await admin('DELETE', '/ai/tasks/gating.threshold_evaluation');
    seen.length = 0;
    await call(token, 'gating.threshold_evaluation', { session_id: 'gs_reset' });
    expect(hosts()).toEqual(['api.anthropic.com']);
  });

  it("falls back to the built-in model for the task's own class when nothing is assigned", async () => {
    const { token } = await setup();
    on('api.anthropic.com', () => anthropicStream());
    await call(token, 'gating.biological_context', { session_id: 'gs_b1' }, 'text_routine');
    await call(token, 'gating.image_inspection', { session_id: 'gs_b2' });
    expect(seen.map((s) => s.body.model)).toEqual(['claude-haiku-4-5-20251001', 'claude-opus-5-5']);
    // With any assignment in place, a task none covers still gets its built-in model.
    await approve('gpt-test', 'openai', 'gpt-test');
    await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
    await assign('qc.*', { primary: 'gpt-test' });
    seen.length = 0;
    await call(token, 'gating.biological_context', { session_id: 'gs_b3' }, 'text_routine');
    expect(seen.map((s) => s.body.model)).toEqual(['claude-haiku-4-5-20251001']);
    const view = (await admin('GET', '/ai/tasks')).json;
    expect(view.rows.find((r: any) => r.pattern === 'gating.biological_context').effective.level).toBe('builtin');
  });

  it('refuses a malformed task, and entitles a call that names its task by module', async () => {
    const { token } = await setup();
    on('api.anthropic.com', () => anthropicStream());
    expect((await call(token, 'Gating.X')).status).toBe(400);
    expect((await call(token, 'gating')).status).toBe(400);
    const chat = await setup({ entitlements: ['ai:chat'] });
    const denied = await call(chat.token, 'gating.image_inspection');
    expect(denied.status).toBe(403);
    expect(denied.json.error.code).toBe('capability_not_allowed');
    // A chat turn with images is a chat task, whatever capability class the client names for it.
    const turn = await call(chat.token, 'chat.turn', { feature: 'chat', session_id: 'c1' }, 'vision_routine');
    expect(turn.status).toBe(200);
    // Without a task, the capability class still decides, as before.
    expect((await call(chat.token, null, { feature: 'chat' }, 'vision_routine')).status).toBe(403);
  });
});

describe('what an assignment may name', () => {
  it("refuses a model that lacks what the task needs, unless the task's needs are overridden", async () => {
    const { token } = await setup();
    await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
    await approve('text-only', 'openai', 'text-only', OPENAI, { supports_vision: false });
    const refused = await admin('PUT', '/ai/tasks/gating.image_inspection', { primary: 'text-only' });
    expect(refused.status).toBe(409);
    expect(refused.json.error.details).toMatchObject({ reason: 'assignment_invalid', why: 'no vision' });
    // A module default must suit at least one task it covers: gating has a text task, so this one does.
    await assign('gating.*', { primary: 'text-only' });
    await assign('gating.image_inspection', { primary: 'text-only', requires_vision: '0' });
    on('api.openai.com', () => responsesStream());
    const image = { type: 'image', source: { type: 'base64', media_type: 'image/webp', data: 'AAAA' } };
    const withImage = await call(token, 'gating.image_inspection', {}, 'vision_judgement',
      { messages: [{ role: 'user', content: [image, { type: 'text', text: 'look' }] }] });
    expect(withImage.status).toBe(400);
    expect(withImage.json.error.code).toBe('route_unsupported');
    // The admin page says which rows are served by a model that cannot do what they need.
    const problems = (await admin('GET', '/ai/tasks')).json.rows.filter((r: any) => r.mismatch.length)
      .map((r: any) => r.pattern);
    expect(problems).toContain('gating.threshold_evaluation');
    expect(problems).not.toContain('gating.image_inspection');
  });

  it('validates the task, the number of models and repeats', async () => {
    await setup();
    await approve('gpt-test', 'openai', 'gpt-test');
    expect((await admin('PUT', '/ai/tasks/gating.nope', { primary: 'gpt-test' })).status).toBe(400);
    expect((await admin('PUT', '/ai/tasks/nope.*', { primary: 'gpt-test' })).status).toBe(400);
    expect((await admin('PUT', '/ai/tasks/*', { models: ['gpt-test', 'gpt-test'] })).status).toBe(400);
    expect((await admin('PUT', '/ai/tasks/*', { models: ['a', 'b', 'c', 'd'] })).status).toBe(400);
    // Every model empty is "inherit": nothing stays assigned.
    await assign('*', { primary: 'gpt-test' });
    await assign('*', { primary: '', fallback_1: '', fallback_2: '' });
    expect((await admin('GET', '/ai/schema')).json.task_routing).toBe(false);
  });

  it("passes over a model whose estimate is above the task's cost cap", async () => {
    const { token } = await setup();
    await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
    await approve('dear', 'anthropic', 'claude-opus-5-5', { in_usd: 400, out_usd: 2000, source_url: 'https://x.org' });
    await approve('cheap', 'openai', 'gpt-test');
    await assign('gating.*', { models: ['dear', 'cheap'], max_cost_usd: 0.5 });
    everyoneAnswers();
    const reply = await call(token, 'gating.image_inspection', { session_id: 'gs_cap' });
    expect(reply.status).toBe(200);
    expect(hosts()).toEqual(['api.openai.com']);
    await assign('gating.*', { models: ['dear'], max_cost_usd: 0.5 });
    const none = await call(token, 'gating.image_inspection', { session_id: 'gs_cap2' });
    expect(none.status).toBe(400);
    expect(none.json.error.code).toBe('route_unsupported');
  });
});

describe('migrating the v3 route table', () => {
  async function legacy() {
    const now = Math.floor(Date.now() / 1000);
    const model = (provider: string, name: string, source: string) => env.LICENSE_DB.prepare(
      `INSERT INTO ai_models (provider, model, in_micro, cache_read_micro, cache_write_5m_micro, cache_write_1h_micro,
         out_micro, fee_bps, supports_vision, enabled, source_url, updated_at)
       VALUES (?1, ?2, 0, 0, 0, 0, 0, 550, 1, 1, ?3, ?4)`).bind(provider, name, source, now);
    const route = (id: string, feature: string, capability: string, rank: number, provider: string, name: string) =>
      env.LICENSE_DB.prepare(
        `INSERT INTO ai_routes (id, feature, capability, role, rank, provider, model, effort, max_tokens_cap, failover,
           shadow_pct, enabled, unbenched, updated_at)
         VALUES (?1, ?2, ?3, 'serve', ?4, ?5, ?6, NULL, 4096, 'outage', 0, 1, 1, ?7)`)
        .bind(id, feature, capability, rank, provider, name, now);
    const statements = [model('openrouter', 'vendor/model-a:free', 'https://openrouter.ai/api/v1/models (imported)')];
    for (const cap of CAPABILITIES) {
      statements.push(route(`rt_${cap}_0`, '*', cap, 0, 'openrouter', 'vendor/model-a:free'));
      statements.push(route(`rt_${cap}_1`, '*', cap, 1, 'anthropic', 'claude-opus-5-5'));
    }
    statements.push(route('rt_gating', 'gating', 'vision_judgement', 0, 'anthropic', 'claude-sonnet-5'));
    await env.LICENSE_DB.batch(statements);
  }

  it('carries every chain over unchanged, then serves from task routing; once', async () => {
    const { token } = await setup();
    await legacy();
    everyoneAnswers();
    // Before: the v3 table serves, and the admin is told so.
    const before = await call(token, null, { feature: 'chat', session_id: 'm0' });
    expect(hosts()).toEqual(['openrouter.ai']);
    expect((await row(usageOf(before).gateway_request_id))!.route_id).toBe('rt_vision_judgement_0');
    expect((await admin('GET', '/ai/schema')).json.legacy).toMatchObject({ routes: 9, models: 1, serving: true });

    const migrated = await admin('POST', '/ai/migrate-legacy', {});
    expect(migrated.status, JSON.stringify(migrated.json)).toBe(200);
    expect(migrated.json.assigned['*']).toEqual(['model-a-free', 'claude-opus-5-5']);
    // The gating feature had a chain of its own for its looks: every gating task of that class keeps it.
    expect(migrated.json.assigned['gating.image_inspection']).toEqual(['claude-sonnet-5']);
    expect(migrated.json.assigned['gating.biological_context']).toBeUndefined();
    const route = (await admin('GET', '/ai/catalog')).json.models.find((m: any) => m.id === 'model-a-free').routes[0];
    expect(route).toMatchObject({ provider: 'openrouter', provider_model: 'vendor/model-a:free', fee_bps: 550 });

    seen.length = 0;
    await call(token, 'gating.image_inspection', { session_id: 'm1' });
    await call(token, null, { feature: 'chat', session_id: 'm2' });
    expect(hosts()).toEqual(['api.anthropic.com', 'openrouter.ai']);
    expect(seen[0]!.body.model).toBe('claude-sonnet-5');
    expect((await admin('GET', '/ai/schema')).json.legacy.serving).toBe(false);
    const again = await admin('POST', '/ai/migrate-legacy', {});
    expect(again.status).toBe(409);
    expect(again.json.error.details.reason).toBe('already_migrated');
  });
});
