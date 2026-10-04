import { env } from 'cloudflare:test';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { REASONING_UNCONFIRMED, UNPRICED_NOTE } from '../../src/ai/catalog_store';
import { clearListingCache, perM } from '../../src/ai/pricing';
import { withSettings } from '../../src/ai/settings';
import { setUpstreamFetch } from '../../src/ai/providers';
import { runMaintenance } from '../../src/cron';
import { ensureLateColumns, resetLateColumns } from '../../src/db';
import { nowSeconds } from '../../src/env';
import worker from '../../src/index';
import anthropic from './fixtures/anthropic_models.json';
import openai from './fixtures/openai_models.json';
import openrouter from './fixtures/openrouter_models.json';
import orcarouter from './fixtures/orcarouter_models.json';
import saygm from './fixtures/saygm_models.json';
import { ADMIN_HEADERS, admin, BASE, travel } from './helpers';
import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';

/**
 * Approved models and their provider routes: approving from a provider's own
 * model list (fixtures are trimmed live responses), the order routes are
 * tried in, the nightly price refresh, and what it records.
 */

const listings: Record<string, unknown> = {
  'openrouter.ai/api/v1/models': openrouter,
  'api.orcarouter.ai/v1/models': orcarouter,
  'api.saygm.com/v1/models': saygm,
  'api.anthropic.com/v1/models': anthropic,
  'api.openai.com/v1/models': openai,
};
let fetched: string[] = [];
let sent: Array<{ url: string; headers: Record<string, string> }> = [];
let down = new Set<string>();

beforeEach(() => {
  clearListingCache();
  fetched = [];
  sent = [];
  down = new Set();
  setUpstreamFetch(async (url, init) => {
    fetched.push(url);
    sent.push({ url, headers: { ...(init?.headers as Record<string, string> ?? {}) } });
    const host = Object.keys(listings).find((k) => url.includes(k));
    if (!host || [...down].some((d) => url.includes(d))) return new Response('down', { status: 503 });
    return new Response(JSON.stringify(listings[host]), { headers: { 'content-type': 'application/json' } });
  });
});

afterEach(() => {
  setUpstreamFetch(null);
  travel(0);
});

const routesOf = async (id: string) => (await admin('GET', '/ai/catalog')).json.models.find((m: any) => m.id === id)?.routes;

describe('approving models from provider lists', () => {
  it('normalises each provider list: per-token strings, nano-dollars per million, fees and abilities', () => {
    expect(perM('0.000004')).toBe(4_000_000);
    expect(perM('0.00000037999999960000003')).toBe(380_000);
    expect(perM('')).toBeNull();
    expect(perM('free')).toBeNull();
  });

  it('approves a model from OpenRouter at its published prices, with its fee and abilities', async () => {
    const reply = await admin('POST', '/ai/catalog/import', { provider: 'openrouter',
      provider_model: 'anthropic/claude-opus-5.5' });
    expect(reply.status, JSON.stringify(reply.json)).toBe(201);
    expect(fetched[0]).toBe('https://openrouter.ai/api/v1/models');
    expect(reply.json.model).toMatchObject({ id: 'claude-opus-5-5', name: 'Claude Opus 5.5', supports_vision: 1 });
    expect(reply.json.routes[0]).toMatchObject({ provider: 'openrouter', provider_model: 'anthropic/claude-opus-5.5',
      rank: 0, in_micro: 4_000_000, out_micro: 20_000_000, cache_read_micro: 200_000, cache_write_5m_micro: 5_000_000,
      cache_write_1h_micro: 8_000_000, fee_bps: 550, price_source: 'api', availability: 'ok' });
    // A free model costs nothing; an unlisted one is refused.
    const free = await admin('POST', '/ai/catalog/import', { provider: 'openrouter',
      provider_model: 'google/gemma-4-26b-a4b-it:free' });
    expect(free.json.model.id).toBe('gemma-4-26b-a4b-it-free');
    expect(free.json.routes[0]).toMatchObject({ in_micro: 0, out_micro: 0 });
    expect((await admin('POST', '/ai/catalog/import', { provider: 'openrouter', provider_model: 'vendor/nope' })).status)
      .toBe(404);
  });

  it('adds a second provider to the SAME model, and SayGM in nano-dollars, TEE models only', async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    const orca = await admin('POST', '/ai/catalog/import', { provider: 'orcarouter',
      provider_model: 'anthropic/claude-opus-5.5' });
    // `anthropic/claude-opus-5.5` is the built-in Claude Opus 5.5 by another spelling: it becomes its fallback.
    expect(orca.json).toMatchObject({ created: false, model: { id: 'claude-opus-5-5', reasoning: 1 } });
    expect(orca.json.routes.map((r: any) => [r.provider, r.rank, r.price_source])).toEqual([
      ['anthropic', 0, 'builtin'], ['orcarouter', 1, 'api']]);
    const tee = await admin('POST', '/ai/catalog/import', { provider: 'saygm', provider_model: 'gemma-4-31b-turbo-tee' });
    expect(tee.json.routes[0]).toMatchObject({ in_micro: 60_000, out_micro: 185_000, cache_read_micro: 6_000 });
    expect(tee.json.model.supports_vision).toBe(1);
    const frontier = await admin('POST', '/ai/catalog/import', { provider: 'saygm', provider_model: 'claude-fable-5' });
    expect(frontier.status).toBe(400);
    const found = await admin('GET', '/ai/catalog/discover?provider=saygm');
    expect(found.json.models.map((m: any) => m.id)).toEqual(['deepseek-v3.2-tee', 'gemma-4-31b-turbo-tee']);
    expect(found.json.models[1].catalogued_as).toBe('gemma-4-31b-turbo-tee');
    const search = await admin('GET', '/ai/catalog/discover?provider=orcarouter&q=opus');
    expect(search.json.models).toHaveLength(1);
    expect(search.json.models[0]).toMatchObject({ suggested_id: 'claude-opus-5-5', catalogued_as: 'claude-opus-5-5' });
  });

  it('takes reasoning from the built-in model when a list does not say, and otherwise asks', async () => {
    // OrcaRouter lists no supported parameters, so it never says whether a model reasons.
    const opus = await admin('POST', '/ai/catalog/import', { provider: 'orcarouter',
      provider_model: 'anthropic/claude-opus-5.5' });
    expect(opus.json.model).toMatchObject({ id: 'claude-opus-5-5', reasoning: 1, note: null });
    const gemma = await admin('POST', '/ai/catalog/import', { provider: 'orcarouter',
      provider_model: 'google/gemma-4-31b-it' });
    expect(gemma.json.model).toMatchObject({ reasoning: 0, note: REASONING_UNCONFIRMED });
    // Answering it -- either way -- clears the note; a note typed by hand stays.
    const id = gemma.json.model.id;
    const ticked = await admin('PUT', `/ai/catalog/${id}`, { reasoning: true, note: REASONING_UNCONFIRMED });
    expect(ticked.json.model).toMatchObject({ reasoning: 1, note: null });
    await admin('PUT', `/ai/catalog/${id}`, { note: 'checked by hand' });
    const kept = await admin('PUT', `/ai/catalog/${id}`, { reasoning: false });
    expect(kept.json.model).toMatchObject({ reasoning: 0, note: 'checked by hand' });
  });

  it('holds at most three routes, one per provider, and keeps their order through a reorder', async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    for (const provider of ['openrouter', 'orcarouter']) {
      expect((await admin('POST', '/ai/catalog/import', { provider, provider_model: 'anthropic/claude-opus-5.5' })).status)
        .toBe(201);
    }
    const full = await admin('POST', '/ai/catalog/claude-opus-5-5/routes', { provider: 'openai', provider_model: 'x',
      in_usd: 1, out_usd: 2, source_url: 'https://example.org' });
    expect(full.status).toBe(409);
    expect(full.json.error.details.reason).toBe('routes_full');
    const twice = await admin('POST', '/ai/catalog/gemma/routes', {});
    expect(twice.status).toBe(404);
    const moved = await admin('POST', '/ai/catalog/claude-opus-5-5/routes/orcarouter/primary');
    expect(moved.json.routes.map((r: any) => r.provider)).toEqual(['orcarouter', 'anthropic', 'openrouter']);
    // Prices and sources travel with their route.
    expect(moved.json.routes[1]).toMatchObject({ price_source: 'builtin', in_micro: 4_000_000 });
    const removed = await admin('DELETE', '/ai/catalog/claude-opus-5-5/routes/anthropic');
    expect(removed.json.routes.map((r: any) => [r.provider, r.rank])).toEqual([['orcarouter', 0], ['openrouter', 1]]);
    const events = await env.LICENSE_DB.prepare("SELECT COUNT(*) AS n FROM events WHERE kind = 'ai.routes_reordered'")
      .first<{ n: number }>();
    expect(events!.n).toBe(1);
  });

  it('takes prices by hand in dollars, and keeps them until set back to the provider list', async () => {
    await admin('PUT', '/ai/catalog/gpt-x', { name: 'GPT X' });
    // Nobody prices it: the route is added, off, and waits for a price.
    const noPrice = await admin('POST', '/ai/catalog/gpt-x/routes', { provider: 'openai', provider_model: 'gpt-x' });
    expect(noPrice.status).toBe(201);
    expect(noPrice.json.unpriced).toBe(true);
    expect(noPrice.json.routes[0]).toMatchObject({ enabled: 0, price_source: 'manual', priced_at: null, in_micro: 0,
      note: UNPRICED_NOTE });
    const priced = await admin('PATCH', '/ai/catalog/gpt-x/routes/openai', { in_usd: 1, out_usd: 4,
      source_url: 'https://openai.com/api/pricing' });
    expect(priced.json.routes[0]).toMatchObject({ enabled: 1, in_micro: 1_000_000, note: null });
    expect(priced.json.note).toContain('now on');
    await admin('DELETE', '/ai/catalog/gpt-x/routes/openai');
    const manual = await admin('POST', '/ai/catalog/gpt-x/routes', { provider: 'openai', provider_model: 'gpt-x',
      in_usd: 1.25, out_usd: '10', source_url: 'https://openai.com/api/pricing' });
    expect(manual.json.routes[0]).toMatchObject({ in_micro: 1_250_000, cache_read_micro: 1_250_000, out_micro: 10_000_000,
      price_source: 'manual' });
    await admin('POST', '/ai/catalog/import', { provider: 'openrouter', provider_model: 'anthropic/claude-opus-5.5' });
    const typed = await admin('PATCH', '/ai/catalog/claude-opus-5-5/routes/openrouter', { in_usd: 3, out_usd: 15,
      source_url: 'https://example.org/deal' });
    expect(typed.json.routes[0]).toMatchObject({ in_micro: 3_000_000, price_source: 'manual' });
    await runMaintenance(env, nowSeconds(), 'pricing');
    expect((await routesOf('claude-opus-5-5'))[0]).toMatchObject({ in_micro: 3_000_000, price_source: 'manual' });
    const back = await admin('PATCH', '/ai/catalog/claude-opus-5-5/routes/openrouter', { price_source: 'api' });
    expect(back.json.routes[0]).toMatchObject({ in_micro: 4_000_000, price_source: 'api' });
  });
});

describe('the price refresh', () => {
  it('updates listed prices nightly, records each change once, and leaves typed and list prices alone', async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    await admin('POST', '/ai/catalog/import', { provider: 'orcarouter', provider_model: 'anthropic/claude-opus-5.5' });
    await admin('POST', '/ai/catalog/import', { provider: 'saygm', provider_model: 'gemma-4-31b-turbo-tee' });
    // The provider cuts its price.
    const cut = structuredClone(orcarouter) as any;
    cut.data.find((m: any) => m.id === 'anthropic/claude-opus-5.5').pricing.prompt = '0.0000035';
    listings['api.orcarouter.ai/v1/models'] = cut;
    try {
      const report = (await runMaintenance(env, nowSeconds(), 'pricing')).pricing as Record<string, any>;
      expect(report.orcarouter).toMatchObject({ ok: true, routes_updated: 1, changes: 1 });
      expect(report.anthropic).toMatchObject({ ok: true, changes: 0 });
      const routes = await routesOf('claude-opus-5-5');
      expect(routes.find((r: any) => r.provider === 'orcarouter')).toMatchObject({ in_micro: 3_500_000, stale: false });
      expect(routes.find((r: any) => r.provider === 'anthropic')).toMatchObject({ in_micro: 4_000_000 });
      const history = await admin('GET', '/ai/pricing/history?model_id=claude-opus-5-5');
      expect(history.json.changes, JSON.stringify(history.json.changes)).toHaveLength(1);
      // OrcaRouter lists no cache-write price; a Claude model's follows input at Anthropic's ratios.
      expect(history.json.changes[0]).toMatchObject({ provider: 'orcarouter', actor: 'cron:pricing',
        changes: [{ field: 'in_micro', old: 4_000_000, new: 3_500_000 },
          { field: 'cache_write_5m_micro', old: 5_000_000, new: 4_375_000 },
          { field: 'cache_write_1h_micro', old: 8_000_000, new: 7_000_000 }] });
      // Nothing changed the second time: no second event.
      await runMaintenance(env, nowSeconds(), 'pricing');
      expect((await admin('GET', '/ai/pricing/history')).json.changes).toHaveLength(1);
    } finally {
      listings['api.orcarouter.ai/v1/models'] = orcarouter;
    }
  });

  it('flags a listed price not confirmed within the stale window, and is off when switched off', async () => {
    await admin('POST', '/ai/catalog/import', { provider: 'openrouter', provider_model: 'anthropic/claude-opus-5.5' });
    expect((await admin('GET', '/ai/pricing/status')).json.stale).toEqual([]);
    travel(48 * 3600);
    const status = await admin('GET', '/ai/pricing/status');
    expect(status.json.stale).toMatchObject([{ model_id: 'claude-opus-5-5', provider: 'openrouter' }]);
    await admin('POST', '/ai/pricing/refresh', {});
    expect((await admin('GET', '/ai/pricing/status')).json.stale).toEqual([]);
    await admin('PUT', '/ai/settings', { AI_PRICE_REFRESH: 0 });
    expect((await runMaintenance(await withSettings(env), nowSeconds(), 'pricing')).pricing).toBe('off');
  });

  it('records a provider whose list fails, and carries on with the others', async () => {
    await admin('POST', '/ai/catalog/import', { provider: 'openrouter', provider_model: 'anthropic/claude-opus-5.5' });
    await admin('POST', '/ai/catalog/import', { provider: 'saygm', provider_model: 'gemma-4-31b-turbo-tee' });
    down.add('openrouter.ai');
    const report = (await runMaintenance(env, nowSeconds(), 'pricing')).pricing as Record<string, any>;
    expect(report.openrouter.ok).toBe(false);
    expect(report.saygm.ok).toBe(true);
    const status = (await admin('GET', '/ai/pricing/status')).json.providers;
    expect(status.find((p: any) => p.provider === 'openrouter').status).toMatchObject({ ok: 0 });
    expect(status.find((p: any) => p.provider === 'saygm').status).toMatchObject({ ok: 1, models_seen: 3 });
  });
});

describe('schema', () => {
  it('adds the task columns to an ai_requests table that predates them, once', async () => {
    await env.LICENSE_DB.prepare('ALTER TABLE ai_requests DROP COLUMN model_id').run();
    resetLateColumns();
    await ensureLateColumns(env);
    await ensureLateColumns(env);
    resetLateColumns();
    await ensureLateColumns(env);
    const schema = await admin('GET', '/ai/schema');
    expect(schema.json).toMatchObject({ schema_version: 5, ai_requests_has_task_columns: true, task_routing: false,
      dismissals_table: true });
  });
});

describe('direct providers', () => {
  it("reads Anthropic's list with its key, priced from the list-price table or OpenRouter's listing", async () => {
    const found = await admin('GET', '/ai/catalog/discover?provider=anthropic');
    expect(found.status, JSON.stringify(found.json)).toBe(200);
    const call = sent.find((x) => x.url.startsWith('https://api.anthropic.com/v1/models'))!;
    expect(call.headers).toMatchObject({ 'x-api-key': 'test-anthropic', 'anthropic-version': '2023-06-01' });
    const byId = (id: string) => found.json.models.find((m: any) => m.id === id);
    expect(byId('claude-opus-5-5')).toMatchObject({ price_from: 'builtin', vision: true, reasoning: true,
      context_window: 1_000_000, efforts: { levels: ['low', 'medium', 'high', 'xhigh', 'max'] } });
    expect(byId('claude-opus-5-5').prices.in).toBe(4_000_000);
    expect(byId('claude-haiku-4-5-20251001').prices.in).toBe(1_000_000);
    expect(byId('claude-opus-4-1-20250805')).toMatchObject({ price_from: 'reference', fee_bps: 0, structured: false,
      extra: { reference: { provider: 'openrouter', id: 'anthropic/claude-opus-4.1' } } });
    expect(byId('claude-opus-4-1-20250805').prices.in).toBe(15_000_000);
    expect(byId('claude-mythos-6')).toMatchObject({ prices: null, price_from: null });
    expect(fetched.filter((u) => u === 'https://openrouter.ai/api/v1/models').length).toBe(1);
  });

  it('approves a Claude model from Anthropic at its reference price, and maps a known one to its built-in id', async () => {
    const opus41 = await admin('POST', '/ai/catalog/import', { provider: 'anthropic',
      provider_model: 'claude-opus-4-1-20250805' });
    expect(opus41.status, JSON.stringify(opus41.json)).toBe(201);
    expect(opus41.json.model).toMatchObject({ name: 'Claude Opus 4.1', family: 'Claude', context_window: 200_000,
      supports_structured: 0, reasoning: 1 });
    expect(opus41.json.routes[0]).toMatchObject({ provider: 'anthropic', price_source: 'api', fee_bps: 0, enabled: 1,
      in_micro: 15_000_000, out_micro: 75_000_000, source_url: 'https://openrouter.ai/api/v1/models' });
    await admin('POST', '/ai/catalog/seed-builtin');
    const again = await admin('POST', '/ai/catalog/import', { provider: 'anthropic', provider_model: 'claude-opus-5-5' });
    expect(again.status).toBe(409);
    expect(again.json.error.details.reason).toBe('route_exists');
  });

  it("reads OpenAI's chat models only, aliases over snapshots, priced from OpenRouter's listing", async () => {
    const found = await admin('GET', '/ai/catalog/discover?provider=openai');
    expect(found.json.models.map((m: any) => m.id)).toEqual(['gpt-6.1-sol-pro', 'gpt-5', 'o3']);
    const sol = await admin('POST', '/ai/catalog/import', { provider: 'openai', provider_model: 'gpt-6.1-sol-pro' });
    expect(sol.status, JSON.stringify(sol.json)).toBe(201);
    expect(sol.json.model).toMatchObject({ id: 'gpt-6-1-sol-pro', reasoning: 1, family: 'OpenAI' });
    expect(sol.json.routes[0]).toMatchObject({ in_micro: 2_000_000, out_micro: 10_000_000, cache_read_micro: 100_000,
      cache_write_5m_micro: 2_500_000, fee_bps: 0, price_source: 'api' });
    const catalog = await admin('GET', '/ai/catalog');
    expect(catalog.json.models.find((m: any) => m.id === 'gpt-6-1-sol-pro').effort.source).toBe('builtin');
  });

  it('says a direct provider is not connected rather than failing, when it has no key', async () => {
    const ctx = createExecutionContext();
    const response = await worker.fetch(new Request(`${BASE}/admin/api/ai/catalog/discover?provider=anthropic`,
      { headers: ADMIN_HEADERS }), { ...env, ANTHROPIC_API_KEY: undefined }, ctx);
    await waitOnExecutionContext(ctx);
    expect(response.status).toBe(409);
    const body = await response.json() as any;
    expect(body.error.details).toMatchObject({ reason: 'provider_not_connected', provider: 'anthropic' });
    expect(body.error.message).toContain('Anthropic is not connected');
  });

  it('adds a model nobody prices switched off, refuses to assign or switch it on, and turns it on once priced', async () => {
    const mythos = await admin('POST', '/ai/catalog/import', { provider: 'anthropic', provider_model: 'claude-mythos-6' });
    expect(mythos.status).toBe(201);
    expect(mythos.json).toMatchObject({ unpriced: true, created: true });
    expect(mythos.json.note).toContain('no price could be found');
    expect(mythos.json.routes[0]).toMatchObject({ enabled: 0, price_source: 'manual', priced_at: null });
    const assign = await admin('PUT', '/ai/tasks/*', { primary: 'claude-mythos-6' });
    expect(assign.status).toBe(409);
    expect(assign.json.error.details.why).toBe('unpriced');
    expect((await admin('GET', '/ai/problems')).json.problems.map((p: any) => p.key))
      .toContain('route:claude-mythos-6:anthropic:unpriced');
    expect((await admin('PATCH', '/ai/catalog/claude-mythos-6/routes/anthropic', { enabled: true })).status).toBe(400);
    const priced = await admin('PATCH', '/ai/catalog/claude-mythos-6/routes/anthropic', { in_usd: 12, out_usd: 60,
      source_url: 'https://www.anthropic.com/pricing' });
    expect(priced.json.routes[0]).toMatchObject({ enabled: 1, in_micro: 12_000_000 });
    expect((await admin('GET', '/ai/problems')).json.problems.map((p: any) => p.key))
      .not.toContain('route:claude-mythos-6:anthropic:unpriced');
    expect((await admin('PUT', '/ai/tasks/*', { primary: 'claude-mythos-6' })).status).toBe(200);
  });

  it("re-prices a reference route nightly, and fills a built-in model's context from Anthropic's list", async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    await admin('POST', '/ai/catalog/import', { provider: 'anthropic', provider_model: 'claude-opus-4-1-20250805' });
    const cut = structuredClone(openrouter) as any;
    cut.data.find((m: any) => m.id === 'anthropic/claude-opus-4.1').pricing.prompt = '0.000012';
    listings['openrouter.ai/api/v1/models'] = cut;
    try {
      const report = (await runMaintenance(env, nowSeconds(), 'pricing')).pricing as Record<string, any>;
      expect(report.anthropic).toMatchObject({ ok: true, listed: true });
      const routes = await routesOf('claude-opus-4-1-20250805');
      expect(routes[0]).toMatchObject({ in_micro: 12_000_000, price_source: 'api' });
      const history = (await admin('GET', '/ai/pricing/history?provider=anthropic')).json.changes;
      expect(history).toHaveLength(1);
      expect(history[0]).toMatchObject({ provider: 'anthropic', source: 'api' });
      const opus = (await admin('GET', '/ai/catalog')).json.models.find((m: any) => m.id === 'claude-opus-5-5');
      expect(opus.context_window).toBe(1_000_000);
    } finally {
      listings['openrouter.ai/api/v1/models'] = openrouter;
    }
  });
});

describe('removing a model', () => {
  const chainOf = async (task: string) => (await env.LICENSE_DB.prepare(
    `SELECT model_id, rank, effort_spec, role FROM ai_task_routes WHERE task = ?1 ORDER BY role, rank`).bind(task).all()).results;

  it('closes up every chain that names it, keeps what each row asked for, and drops its shadows', async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
    await admin('PUT', '/ai/tasks/*', { primary: 'claude-sonnet-5', fallback_1: 'claude-opus-5-5',
      fallback_2: 'claude-haiku-4-5-20251001', effort: 'high' });
    await admin('PUT', '/ai/tasks/qc.*', { primary: 'claude-opus-5-5', shadow_model: 'claude-sonnet-5' });
    const removed = await admin('DELETE', '/ai/catalog/claude-sonnet-5');
    expect(removed.status, JSON.stringify(removed.json)).toBe(200);
    expect(removed.json.tasks).toEqual(expect.arrayContaining([
      expect.objectContaining({ task: '*', outcome: 'promoted', now: 'Claude Opus 5.5' }),
      expect.objectContaining({ task: 'qc.*', role: 'shadow', outcome: 'shadow_removed' })]));
    expect(removed.json.note).toContain('All tasks now uses Claude Opus 5.5');
    expect(await chainOf('*')).toEqual([
      { model_id: 'claude-opus-5-5', rank: 0, effort_spec: 'high', role: 'serve' },
      { model_id: 'claude-haiku-4-5-20251001', rank: 1, effort_spec: 'high', role: 'serve' }]);
    // qc.* named no effort when it was made, so it asks `auto` (a new row's default), and keeps it.
    expect(await chainOf('qc.*')).toEqual([{ model_id: 'claude-opus-5-5', rank: 0, effort_spec: 'auto', role: 'serve' }]);
    const events = await env.LICENSE_DB.prepare("SELECT COUNT(*) AS n FROM events WHERE kind = 'ai.model_removed'")
      .first<{ n: number }>();
    expect(events!.n).toBe(1);
  });

  it('lets a chain it was alone in inherit, down to the built-in default', async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
    await admin('PUT', '/ai/tasks/*', { primary: 'claude-sonnet-5' });
    await admin('PUT', '/ai/tasks/gating.threshold_evaluation', { primary: 'claude-opus-5-5' });
    const first = await admin('DELETE', '/ai/catalog/claude-opus-5-5');
    expect(first.json.tasks).toEqual([expect.objectContaining({ task: 'gating.threshold_evaluation',
      outcome: 'inherits', now: 'Claude Sonnet 5', level: 'global' })]);
    const tasks = (await admin('GET', '/ai/tasks')).json.rows;
    expect(tasks.find((r: any) => r.pattern === 'gating.threshold_evaluation').effective.source).toBe('*');
    const last = await admin('DELETE', '/ai/catalog/claude-sonnet-5?force=1');
    expect(last.json.tasks).toEqual([expect.objectContaining({ task: '*', outcome: 'inherits',
      now: 'built-in default', level: 'builtin' })]);
    expect((await admin('GET', '/ai/schema')).json.task_routing).toBe(false);
  });

  it('removes an unused model with nothing to say about tasks', async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    const removed = await admin('DELETE', '/ai/catalog/claude-haiku-4-5-20251001');
    expect(removed.json).toMatchObject({ deleted: 'claude-haiku-4-5-20251001', tasks: [] });
    expect(removed.json.note).toBe('Removed Claude Haiku 4.5.');
  });
});
