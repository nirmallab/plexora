import { env } from 'cloudflare:test';
import { afterEach, describe, expect, it } from 'vitest';

import { ACT, assess, chainsOf, gather, PER_CALL, PLAN, type Usage } from '../../src/ai/capacity';
import { tasksView } from '../../src/ai/views';
import { setUpstreamFetch } from '../../src/ai/providers';
import { admin, call } from './helpers';

/**
 * The capacity monitor: grading each plan, database and provider limit, the
 * D1 gathering it reads, and the Cloudflare analytics it prefers when a token
 * is set.
 */

const NOW = Date.UTC(2026, 9, 2, 18, 0, 0);
const day = (offset: number) => new Date(NOW - offset * 86_400_000).toISOString().slice(0, 10);

function usage(overrides: Partial<Usage> = {}): Usage {
  return { now_ms: NOW, calls_by_day: [], calls_30d: 0, peak_minute: null, calls_7d: 0, rate_limited_7d: 0,
    db_bytes_local: 500_000, free_serving: [], no_fallback: [], cloudflare: null, analytics_configured: false,
    paid: false, provider_rpm: 0, provider_rpd: 0, ...overrides };
}

const signal = (a: ReturnType<typeof assess>, key: string) => a.signals.find((s) => s.key === key)!;

afterEach(() => setUpstreamFetch(null));

describe('grading', () => {
  it('is within every limit when quiet', () => {
    const a = assess(usage({ calls_by_day: [{ day: day(1), value: 40 }], calls_7d: 40 }));
    expect(a.level).toBe('ok');
    expect(a.move_to_paid).toBe(false);
    expect(a.headline).toBe('Within every limit');
    expect(signal(a, 'cpu').level).toBe('unknown');
    expect(signal(a, 'worker_requests').source).toBe('estimated');
  });

  it('says to move to Workers Paid when a Free daily limit passes 80%', () => {
    // 6,000 calls a day estimates 108,000 rows written: past D1 Free's 100,000.
    const a = assess(usage({ calls_by_day: [{ day: day(2), value: 100 }, { day: day(1), value: 6_000 }] }));
    expect(signal(a, 'd1_rows_written').value).toBe(6_000 * PER_CALL.rows_written);
    expect(signal(a, 'd1_rows_written').level).toBe('act');
    expect(signal(a, 'd1_rows_written').detail).toContain(day(1));
    expect(a.move_to_paid).toBe(true);
    expect(a.headline).toBe('Time to move to Workers Paid');
  });

  it('only watches monthly inclusions on Paid: overage is billed, not refused', () => {
    const a = assess(usage({ paid: true, calls_30d: 3_000_000, calls_by_day: [{ day: day(1), value: 100_000 }] }));
    const written = signal(a, 'd1_rows_written');
    expect(written.limit).toBe(PLAN.paid.d1_rows_written_month);
    expect(written.value).toBe(3_000_000 * PER_CALL.rows_written);
    expect(written.level).toBe('watch');
    expect(a.move_to_paid).toBe(false);
  });

  it('grades the single database whatever the plan', () => {
    const calls = Math.ceil((ACT * 1_000 * 60) / PER_CALL.queries);
    for (const paid of [false, true]) {
      const a = assess(usage({ paid, peak_minute: { calls, at_ms: NOW } }));
      expect(signal(a, 'd1_throughput').level).toBe('act');
      expect(signal(a, 'd1_throughput').action).toContain('Durable Object');
    }
  });

  it('grades the provider: 429 share, written limits, free models and fallbacks', () => {
    const a = assess(usage({ calls_7d: 1_000, rate_limited_7d: 30, provider_rpm: 100,
      peak_minute: { calls: 60, at_ms: NOW }, free_serving: ['orcarouter/z-ai/glm-5.3-flash-free'],
      no_fallback: ['vision_judgement'] }));
    expect(signal(a, 'provider_429').level).toBe('act');
    expect(signal(a, 'provider_rpm').level).toBe('watch');
    expect(signal(a, 'provider_rpd').level).toBe('unknown');
    expect(signal(a, 'free_model').level).toBe('act');
    expect(signal(a, 'fallback').level).toBe('watch');
    expect(a.move_to_paid).toBe(false);
    expect(a.headline).toBe('Act now on 2 limits');
  });

  it('watches CPU over the limit, and acts only when Free stopped a request', () => {
    const over = { worker_requests: [{ day: day(1), value: 500 }], d1_rows_written: [], d1_rows_read: [],
      exceeded: 0, cpu_p99_ms: 249, cpu_p99_at: { script: 'plexora-licensing', day: day(1) }, db_bytes: null, errors: [] };
    const watched = assess(usage({ cloudflare: over, analytics_configured: true }));
    expect(signal(watched, 'cpu').level).toBe('watch');
    expect(signal(watched, 'cpu').detail).toContain('plexora-licensing');
    expect(watched.move_to_paid).toBe(false);
  });

  it('acts on any request Free cut off for CPU', () => {
    const cloudflare = { worker_requests: [{ day: day(1), value: 500 }], d1_rows_written: [], d1_rows_read: [],
      exceeded: 3, cpu_p99_ms: 4, db_bytes: null, errors: [] };
    const a = assess(usage({ cloudflare, analytics_configured: true }));
    expect(signal(a, 'cpu').level).toBe('act');
    expect(signal(a, 'worker_requests').source).toBe('measured');
    expect(signal(a, 'd1_rows_written').source).toBe('estimated');
    expect(a.move_to_paid).toBe(true);
  });
});

async function request(id: string, startedMs: number, extra: { status?: number; billing?: string } = {}) {
  await env.LICENSE_DB.prepare(
    `INSERT INTO ai_requests (id, account_id, billing, capability, provider, model, status, http_status, usage_source,
       p_in, p_cache_read, p_cache_write_5m, p_cache_write_1h, p_out, markup_bps, started_at_ms, finished_at_ms)
     VALUES (?1, 'acc_cap', ?2, 'vision_judgement', 'orcarouter', 'm', 'ok', ?3, 'provider', 0, 0, 0, 0, 0, 20000,
       ?4, ?4)`,
  ).bind(id, extra.billing ?? 'credits', extra.status ?? 200, startedMs).run();
}

/** Approve a model with one route, and assign it to a scope (the admin API, as ai_routing.test.ts does). */
async function serve(pattern: string, id: string, provider: string, providerModel: string) {
  await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
  expect((await admin('PUT', `/ai/catalog/${id}`, { name: id })).status).toBeLessThan(300);
  expect((await admin('POST', `/ai/catalog/${id}/routes`, { provider, provider_model: providerModel,
    source_url: 'https://example.org/prices', in_usd: 0, out_usd: 0, cache_read_usd: 0, cache_write_5m_usd: 0,
    cache_write_1h_usd: 0 })).status).toBe(201);
  const assigned = await admin('PUT', `/ai/tasks/${encodeURIComponent(pattern)}`, { models: [id] });
  expect(assigned.status, JSON.stringify(assigned.json)).toBe(200);
}

const CHAINS = [
  { label: 'All tasks', routes: [{ provider: 'orcarouter', model: 'z-ai/glm-5.3-flash-free' }] },
  { label: 'Gating', routes: [{ provider: 'anthropic', model: 'claude-opus-5-5' },
    { provider: 'openrouter', model: 'google/gemma-4-31b-it:free' }] },
];

describe('gathering', () => {
  it('reads calls, the busiest minute and 429s from D1, and grades the chains it is given', async () => {
    const minute = NOW - 3_600_000;
    for (let i = 0; i < 5; i++) await request(`req_m${i}`, minute + i * 1000, i === 0 ? { status: 429 } : {});
    await request('req_old', NOW - 2 * 86_400_000);
    await request('req_shadow', minute, { billing: 'shadow' });
    await request('req_ancient', NOW - 40 * 86_400_000);
    const u = await gather(env, NOW, CHAINS);
    expect(u.calls_by_day).toEqual([{ day: day(2), value: 1 }, { day: day(0), value: 6 }]);
    expect(u.calls_30d).toBe(7);
    expect(u.peak_minute?.calls).toBe(6);
    expect(u.calls_7d).toBe(6);              // shadow calls cost us, but the provider never refused a user
    expect(u.rate_limited_7d).toBe(1);
    expect(u.free_serving).toEqual(['All tasks: orcarouter/z-ai/glm-5.3-flash-free']);   // a free fallback is not serving
    expect(u.no_fallback).toEqual(['All tasks']);
    expect(u.analytics_configured).toBe(false);
    expect(u.cloudflare).toBeNull();
  });

  it('prefers Cloudflare analytics, and keeps the datasets that answered', async () => {
    const seen: Array<{ url: string; auth: string | null; query: string }> = [];
    setUpstreamFetch(async (url, init) => {
      const body = JSON.parse(String(init.body));
      seen.push({ url, auth: new Headers(init.headers).get('authorization'), query: body.query });
      expect(body.variables.account).toBe('acct_test');
      if (body.query.includes('workersInvocationsAdaptive')) {
        return Response.json({ data: { viewer: { accounts: [{ workersInvocationsAdaptive: [
          { sum: { requests: 70_000 }, quantiles: { cpuTimeP99: 2_500 }, dimensions: { date: day(1), status: 'success' } },
          { sum: { requests: 15_000 }, quantiles: { cpuTimeP99: 9_100 }, dimensions: { date: day(1), status: 'exceededResources', scriptName: 'plexora-licensing' } },
          { sum: { requests: 1_000 }, quantiles: { cpuTimeP99: 1_000 }, dimensions: { date: day(2), status: 'success' } },
        ] }] } } });
      }
      if (body.query.includes('d1AnalyticsAdaptiveGroups')) {
        return Response.json({ errors: [{ message: 'not authorized for that dataset' }], data: null });
      }
      return Response.json({ data: { viewer: { accounts: [{ d1StorageAdaptiveGroups: [
        { max: { databaseSizeBytes: 450_000_000 }, dimensions: { date: day(0), databaseId: 'db1' } },
        { max: { databaseSizeBytes: 1_000 }, dimensions: { date: day(0), databaseId: 'db2' } },
      ] }] } } });
    });
    const u = await gather({ ...env, CF_ANALYTICS_TOKEN: 'cf_test', CF_ACCOUNT_ID: 'acct_test' }, NOW, []);
    expect(seen).toHaveLength(3);
    expect(seen.every((s) => s.url === 'https://api.cloudflare.com/client/v4/graphql' && s.auth === 'Bearer cf_test')).toBe(true);
    expect(u.cloudflare?.worker_requests).toEqual([{ day: day(2), value: 1_000 }, { day: day(1), value: 85_000 }]);
    expect(u.cloudflare?.exceeded).toBe(15_000);
    expect(u.cloudflare?.cpu_p99_ms).toBe(9.1);
    expect(u.cloudflare?.cpu_p99_at).toEqual({ script: 'plexora-licensing', day: day(1) });
    expect(u.cloudflare?.db_bytes).toBe(450_000_000);
    expect(u.cloudflare?.errors).toEqual(['D1 analytics: not authorized for that dataset']);

    const a = assess(u);
    expect(signal(a, 'worker_requests')).toMatchObject({ value: 85_000, source: 'measured', level: 'act' });
    expect(signal(a, 'd1_rows_written').source).toBe('estimated');
    expect(signal(a, 'd1_storage')).toMatchObject({ source: 'measured', level: 'act' });
    expect(signal(a, 'cpu').level).toBe('act');
    expect(a.move_to_paid).toBe(true);
  });

  it('survives an unreachable analytics API', async () => {
    setUpstreamFetch(async () => { throw new Error('network down'); });
    const u = await gather({ ...env, CF_ANALYTICS_TOKEN: 'cf_test', CF_ACCOUNT_ID: 'acct_test' }, NOW, []);
    expect(u.cloudflare?.errors).toHaveLength(3);
    expect(signal(assess(u), 'worker_requests').source).toBe('estimated');
  });
});

describe('serving chains', () => {
  it('reads the global default and own assignments from task routing, not inherited scopes', async () => {
    await serve('*', 'glm-free', 'orcarouter', 'z-ai/glm-5.3-flash-free');
    let chains = chainsOf((await tasksView(env)).rows);
    expect(chains).toHaveLength(1);
    expect(chains[0]!.routes).toEqual([{ provider: 'orcarouter', model: 'z-ai/glm-5.3-flash-free' }]);

    await serve('gating.*', 'gemma-free', 'openrouter', 'google/gemma-4-31b-it:free');
    chains = chainsOf((await tasksView(env)).rows);
    expect(chains.map((c) => c.routes[0]!.model)).toEqual(['z-ai/glm-5.3-flash-free', 'google/gemma-4-31b-it:free']);
    const u = await gather(env, Date.now(), chains);
    expect(u.free_serving).toHaveLength(2);
    expect(u.no_fallback).toHaveLength(2);
  });
});

describe('admin', () => {
  it('serves the assessment as JSON and on the AI page, for an admin only', async () => {
    await request('req_page', Date.now() - 60_000);
    await serve('*', 'glm-free', 'orcarouter', 'z-ai/glm-5.3-flash-free');
    const json = await admin('GET', '/ai/capacity');
    expect(json.status).toBe(200);
    expect(json.json.plan).toBe('free');
    expect(json.json.usage.calls_7d).toBe(1);
    expect(json.json.usage.free_serving).toEqual(['All tasks: orcarouter/z-ai/glm-5.3-flash-free']);
    expect(json.json.signals.map((s: any) => s.key)).toEqual(expect.arrayContaining(['worker_requests',
      'd1_rows_written', 'd1_rows_read', 'cpu', 'd1_storage', 'd1_throughput', 'provider_429', 'provider_rpm',
      'provider_rpd', 'free_model', 'fallback']));
    expect((await call('GET', '/admin/api/ai/capacity')).status).not.toBe(200);

    const page = await call('GET', '/admin/ai', undefined, { Authorization: 'Bearer test-admin' });
    expect(page.status).toBe(200);
    for (const text of ['id="capacity"', 'Act now on', 'Capacity: act now on', 'href="#capacity"',
      'Serving on a free model', 'meter class="capacity"', 'CF_ANALYTICS_TOKEN']) {
      expect(page.json.text).toContain(text);
    }
    // No inline style attribute: the CSP allows hashed stylesheets only.
    expect(page.json.text).not.toMatch(/<[^>]+ style="/);
    const settings = await call('GET', '/admin/ai/settings', undefined, { Authorization: 'Bearer test-admin' });
    for (const text of ['Capacity', 'Cloudflare account is on Workers Paid', 'Provider limit per minute']) {
      expect(settings.json.text).toContain(text);
    }
  });

  it('grades against Paid once the admin says so', async () => {
    expect((await admin('PUT', '/ai/settings', { AI_CLOUDFLARE_PAID: 1, AI_PROVIDER_RPM: 600 })).status).toBe(200);
    const json = await admin('GET', '/ai/capacity');
    expect(json.json.plan).toBe('paid');
    expect(json.json.signals.find((s: any) => s.key === 'provider_rpm').limit).toBe(600);
    expect(json.json.signals.find((s: any) => s.key === 'd1_rows_written').title).toContain('30 days');
  });
});
