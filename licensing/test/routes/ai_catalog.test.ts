import { env } from 'cloudflare:test';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { REASONING_UNCONFIRMED } from '../../src/ai/catalog_store';
import { clearListingCache, perM } from '../../src/ai/pricing';
import { withSettings } from '../../src/ai/settings';
import { setUpstreamFetch } from '../../src/ai/providers';
import { runMaintenance } from '../../src/cron';
import { ensureLateColumns, resetLateColumns } from '../../src/db';
import { nowSeconds } from '../../src/env';
import openrouter from './fixtures/openrouter_models.json';
import orcarouter from './fixtures/orcarouter_models.json';
import saygm from './fixtures/saygm_models.json';
import { admin, travel } from './helpers';

/**
 * Approved models and their provider routes: approving from a provider's own
 * model list (fixtures are trimmed live responses), the order routes are
 * tried in, the nightly price refresh, and what it records.
 */

const listings: Record<string, unknown> = {
  'openrouter.ai/api/v1/models': openrouter,
  'api.orcarouter.ai/v1/models': orcarouter,
  'api.saygm.com/v1/models': saygm,
};
let fetched: string[] = [];
let down = new Set<string>();

beforeEach(() => {
  clearListingCache();
  fetched = [];
  down = new Set();
  setUpstreamFetch(async (url) => {
    fetched.push(url);
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

  it('refuses to remove a model a task uses, unless forced', async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    // The default, on a direct provider, needs no bench result.
    expect((await admin('PUT', '/ai/tasks/*', { primary: 'claude-sonnet-5' })).status).toBe(200);
    const refused = await admin('DELETE', '/ai/catalog/claude-sonnet-5');
    expect(refused.status).toBe(409);
    expect(refused.json.error.details).toMatchObject({ reason: 'model_in_use', tasks: ['*'] });
    const forced = await admin('DELETE', '/ai/catalog/claude-sonnet-5?force=1');
    expect(forced.json.unassigned).toEqual(['*']);
    expect((await admin('GET', '/ai/catalog')).json.models.map((m: any) => m.id)).not.toContain('claude-sonnet-5');
  });

  it('takes prices by hand in dollars, and keeps them until set back to the provider list', async () => {
    await admin('PUT', '/ai/catalog/gpt-x', { name: 'GPT X' });
    const noPrice = await admin('POST', '/ai/catalog/gpt-x/routes', { provider: 'openai', provider_model: 'gpt-x' });
    expect(noPrice.status).toBe(400);
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
    expect(schema.json).toMatchObject({ schema_version: 4, ai_requests_has_task_columns: true, task_routing: false });
  });
});
