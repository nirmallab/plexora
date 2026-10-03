import { env } from 'cloudflare:test';
import { afterEach, describe, expect, it } from 'vitest';

import { setUpstreamFetch } from '../../src/ai/providers';
import { activate, admin, BASE, call as callApi, count, environmentBody, issue, post, travel } from './helpers';
import { SELF } from 'cloudflare:test';

/** What the fake provider saw, per call. */
let seen: Array<{ url: string; body: Record<string, any>; headers: Headers }> = [];

const USAGE = { input_tokens: 100, cache_read_input_tokens: 8000, cache_creation_input_tokens: 2000,
  cache_creation: { ephemeral_5m_input_tokens: 2000, ephemeral_1h_input_tokens: 0 }, output_tokens: 1 };
// Opus 5.5: 100 x $4 + 8000 x $0.20 + 2000 x $5 + 400 x $20 per MTok = $0.02.
const OPUS_COST = 20_000;

function anthropicStream(usage: Record<string, unknown> = USAGE, output = 400, cut = false): Response {
  const events: Record<string, unknown>[] = [
    { type: 'message_start', message: { id: 'msg_test', type: 'message', role: 'assistant', content: [],
      model: 'x', usage } },
    { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
    { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: '{"kind":"t2","verdict":"ok"}' } },
    { type: 'content_block_stop', index: 0 },
    { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: output } },
    { type: 'message_stop' },
  ];
  const shown = cut ? events.slice(0, 3) : events;
  return new Response(shown.map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join(''),
    { headers: { 'content-type': 'text/event-stream' } });
}

function provider(reply: () => Response = () => anthropicStream()) {
  setUpstreamFetch(async (url, init) => {
    seen.push({ url, body: JSON.parse(String(init.body)), headers: new Headers(init.headers) });
    return reply();
  });
}

afterEach(() => {
  setUpstreamFetch(null);
  seen = [];
});

async function setup(overrides: Record<string, unknown> = {}) {
  const issued = await issue(overrides);
  const body = environmentBody();
  const activated = await activate(issued.seat.key, body);
  expect(activated.status).toBe(200);
  const certificate = activated.json.certificate as string;
  const binding = body.binding as string;
  const token = async () => {
    const reply = await post('/v1/ai/token', { certificate, binding, app_version: '0.0.27' });
    expect(reply.status, JSON.stringify(reply.json)).toBe(200);
    return reply.json.token as string;
  };
  return { ...issued, certificate, binding, token };
}

let keys = 0;
function request(extra: Record<string, unknown> = {}, ctx: Record<string, unknown> = {}) {
  return {
    capability: 'vision_judgement',
    context: { feature: 'gating', agent: 'gating_worker', session_id: 'gs_test', ...ctx },
    request: {
      system: [{ type: 'text', text: 'You are a Plexora worker.', cache_control: { type: 'ephemeral' } }],
      messages: [{ role: 'user', content: [{ type: 'text', text: '{"packet": 1}' }] }],
      max_tokens: 1024,
      output_schema: { type: 'object', properties: { kind: { type: 'string' } }, required: ['kind'] },
    },
    ...extra,
  };
}

interface Streamed {
  status: number;
  json: Record<string, any>;
  events: Array<{ event: string; data: any }>;
}

async function message(token: string, body: unknown, { key, path = '/v1/ai/messages' }:
  { key?: string; path?: string } = {}): Promise<Streamed> {
  const response = await SELF.fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`,
      'Idempotency-Key': key ?? `key-${++keys}-abcdef`, 'CF-Connecting-IP': '203.0.113.7' },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.headers.get('content-type')?.includes('text/event-stream')) {
    return { status: response.status, json: text ? JSON.parse(text) : {}, events: [] };
  }
  const events = text.split('\n\n').filter(Boolean).map((chunk) => {
    const event = /^event: (.*)$/m.exec(chunk)?.[1] ?? '';
    const data = /^data: (.*)$/m.exec(chunk)?.[1] ?? 'null';
    return { event, data: JSON.parse(data) };
  });
  return { status: response.status, json: {}, events };
}

const usageOf = (s: Streamed) => s.events.find((e) => e.event === 'plexora.usage')?.data;

async function ledgerTotal(accountId: string): Promise<number> {
  const row = await env.LICENSE_DB.prepare('SELECT COALESCE(SUM(amount_micro), 0) AS n FROM ai_ledger WHERE account_id = ?1')
    .bind(accountId).first<{ n: number }>();
  return row?.n ?? 0;
}

async function balanceRow(accountId: string) {
  return env.LICENSE_DB.prepare('SELECT * FROM ai_balances WHERE account_id = ?1').bind(accountId)
    .first<{ prepaid_micro: number; allowance_micro: number } & Record<string, number>>();
}

describe('POST /v1/ai/token', () => {
  it('issues a short PLXAI1 token for a registered environment', async () => {
    const { token } = await setup();
    const t = await token();
    expect(t.startsWith('PLXAI1.pxt.')).toBe(true);
    const reply = await post('/v1/ai/token', { certificate: 'nope', binding: 'x' });
    expect(reply.status).toBe(403);
  });

  it('refuses a certificate presented from another environment', async () => {
    const { certificate } = await setup();
    const reply = await post('/v1/ai/token', { certificate, binding: 'f'.repeat(64) });
    expect(reply.status).toBe(403);
    expect(reply.json.error.code).toBe('environment_mismatch');
  });

  it('refuses a licence without AI, and an account an admin turned off', async () => {
    const plain = await setup({ entitlements: ['viewer:export'] });
    const reply = await post('/v1/ai/token', { certificate: plain.certificate, binding: plain.binding });
    expect(reply.json.error.code).toBe('ai_not_entitled');

    const off = await setup();
    await admin('PATCH', `/ai/accounts/${off.account_id}`, { mode: 'disabled' });
    const refused = await post('/v1/ai/token', { certificate: off.certificate, binding: off.binding });
    expect(refused.json.error.code).toBe('ai_disabled');
  });
});

describe('POST /v1/ai/messages', () => {
  it('refuses a call it cannot pay for, before any provider call, and frees the key', async () => {
    provider();
    const { token } = await setup();
    const t = await token();
    const first = await message(t, request(), { key: 'retry-me-123' });
    expect(first.status).toBe(402);
    expect(first.json.error.code).toBe('insufficient_credits');
    expect(seen).toHaveLength(0);
    const again = await message(t, request(), { key: 'retry-me-123' });
    expect(again.json.error.code).toBe('insufficient_credits');
  });

  it('streams the provider events between its own, and bills the provider usage x markup', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 1000, note: 'test grant' });
    const t = await token();
    const reply = await message(t, request());
    expect(reply.status).toBe(200);
    const names = reply.events.map((e) => e.event);
    expect(names[0]).toBe('plexora.accepted');
    expect(names.at(-1)).toBe('plexora.usage');
    expect(names).toContain('content_block_delta');

    const usage = usageOf(reply);
    expect(usage.status).toBe('ok');
    expect(usage.usage).toEqual({ input_uncached: 100, cache_read: 8000, cache_write_5m: 2000, cache_write_1h: 0,
      output_tokens: 400 });
    expect(usage.price_micro).toBe(OPUS_COST * 2);
    expect(usage.charged_micro).toBe(OPUS_COST * 2);
    expect(usage.cost_micro).toBeUndefined();          // the upstream cost is not shown on the paid route

    // Money is consistent: nothing still held, and Σ ledger = balance.
    const bal = await balanceRow(account_id);
    expect(bal!.held_micro).toBe(0);
    expect(bal!.prepaid_micro).toBe(10_000_000 - OPUS_COST * 2);
    expect(await ledgerTotal(account_id)).toBe(bal!.prepaid_micro + bal!.allowance_micro);

    // Tracking: one metadata row, with the provider's numbers.
    const row = await env.LICENSE_DB.prepare('SELECT * FROM ai_requests WHERE account_id = ?1').bind(account_id)
      .first<Record<string, any>>();
    expect(row).toMatchObject({ billing: 'credits', feature: 'gating', capability: 'vision_judgement',
      model: 'claude-opus-5-5', cache_read: 8000, output_tokens: 400, cost_micro: OPUS_COST,
      price_micro: OPUS_COST * 2, markup_bps: 20000, status: 'ok', provider_request_id: 'msg_test' });
  });

  it('builds the provider request: route model, effort, structured output, hashed user, no client extras', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    await message(await token(), request());
    expect(seen).toHaveLength(1);
    const sent = seen[0]!;
    expect(sent.url).toMatch(/\/v1\/messages$/);
    expect(sent.body.model).toBe('claude-opus-5-5');
    expect(sent.body.stream).toBe(true);
    expect(sent.body.output_config).toEqual({ effort: 'medium',
      format: { type: 'json_schema', schema: request().request.output_schema } });
    expect(sent.body.metadata.user_id).toMatch(/^[0-9a-f]{32}$/);
    expect(JSON.stringify(sent.body)).not.toContain(account_id);
    expect(sent.body.system[0].cache_control).toEqual({ type: 'ephemeral' });
    expect(sent.body.output_schema).toBeUndefined();
  });

  it('refuses a model, thinking or any field outside the allowlist on the paid route', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const t = await token();
    expect((await message(t, request({ model: 'claude-sonnet-5' }))).json.error.code).toBe('invalid_request');
    const thinking = request();
    (thinking.request as Record<string, unknown>).thinking = { type: 'adaptive' };
    expect((await message(t, thinking)).json.error.code).toBe('invalid_request');
    expect((await message(t, request({ capability: 'anything' }))).json.error.code).toBe('invalid_request');
    expect(seen).toHaveLength(0);
  });

  it('never sends one idempotency key upstream twice', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const t = await token();
    expect((await message(t, request(), { key: 'once-only-1' })).status).toBe(200);
    const again = await message(t, request(), { key: 'once-only-1' });
    expect(again.status).toBe(409);
    expect(again.json.error.code).toBe('idempotency_conflict');
    expect(again.json.error.details.price_micro).toBe(OPUS_COST * 2);
    expect(seen).toHaveLength(1);
  });

  it('gives the hold back when the provider is down, records the failure, and frees the key', async () => {
    provider(() => new Response('overloaded', { status: 529 }));
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const t = await token();
    const reply = await message(t, request(), { key: 'flaky-call-1' });
    expect(reply.status).toBe(503);
    expect(reply.json.error.code).toBe('provider_unavailable');
    const bal = await balanceRow(account_id);
    expect(bal!.held_micro).toBe(0);
    expect(bal!.prepaid_micro).toBe(1_000_000);
    expect(await count('ai_requests', "account_id = ?1 AND status = 'error'", account_id)).toBe(1);
    provider();
    expect((await message(t, request(), { key: 'flaky-call-1' })).status).toBe(200);
  });

  it('settles a cut stream on what it saw', async () => {
    provider(() => anthropicStream(USAGE, 400, true));
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const usage = usageOf(await message(await token(), request()));
    expect(usage.status).toBe('incomplete');
    expect(usage.usage_source).toBe('partial');
    // Input was seen in message_start; one output token by then.
    expect(usage.usage.cache_read).toBe(8000);
    expect((await balanceRow(account_id))!.held_micro).toBe(0);
  });

  it('refuses a capability the licence does not carry', async () => {
    provider();
    const { token, account_id } = await setup({ entitlements: ['ai:chat'] });
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const reply = await message(await token(), request());
    expect(reply.json.error.code).toBe('capability_not_allowed');
  });
});

describe('the dev route (internal testing, no markup)', () => {
  it('is closed to an account not in dev mode', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const reply = await message(await token(), request(), { path: '/v1/ai/dev/messages' });
    expect(reply.status).toBe(403);
    expect(reply.json.error.code).toBe('dev_not_allowed');
  });

  it('bills a dev account at provider cost, may name a model, and is tracked as dev', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('PATCH', `/ai/accounts/${account_id}`, { mode: 'dev', notes: 'internal testing' });
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const t = await token();
    const reply = await message(t, request(), { path: '/v1/ai/dev/messages' });
    const usage = usageOf(reply);
    expect(usage.billing).toBe('dev');
    expect(usage.price_micro).toBe(OPUS_COST);         // no markup
    expect(usage.cost_micro).toBe(OPUS_COST);
    expect(usage.charged_micro).toBe(OPUS_COST);

    const sonnet = await message(t, request({ model: 'claude-sonnet-5' }), { path: '/v1/ai/dev/messages' });
    expect(seen.at(-1)!.body.model).toBe('claude-sonnet-5');
    // Sonnet 5: 100 x $2 + 8000 x $0.20 + 2000 x $2.50 + 400 x $10 = $0.0108.
    expect(usageOf(sonnet).cost_micro).toBe(10_800);

    const rows = await env.LICENSE_DB.prepare(
      'SELECT billing, markup_bps, cost_micro, price_micro FROM ai_requests WHERE account_id = ?1 ORDER BY started_at_ms')
      .bind(account_id).all<Record<string, any>>();
    expect(rows.results).toEqual([
      { billing: 'dev', markup_bps: 10000, cost_micro: OPUS_COST, price_micro: OPUS_COST },
      { billing: 'dev', markup_bps: 10000, cost_micro: 10_800, price_micro: 10_800 },
    ]);
    // The paid route still marks up, even for a dev account.
    expect(usageOf(await message(t, request())).price_micro).toBe(OPUS_COST * 2);
  });

  it('rejects a model the catalogue does not price', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('PATCH', `/ai/accounts/${account_id}`, { mode: 'dev' });
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const reply = await message(await token(), request({ model: 'gpt-anything' }), { path: '/v1/ai/dev/messages' });
    expect(reply.json.error.code).toBe('invalid_request');
  });
});

describe('runs: quoted, capped, metered', () => {
  it('holds the quote, charges min(accrued, quote), and returns the rest at finish', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 1000 });
    const t = await token();
    const auth = { Authorization: `Bearer ${t}` };
    const started = await post('/v1/ai/runs', { feature: 'gating', units: 2, session_id: 'gs_run' }, auth);
    expect(started.status).toBe(201);
    expect(started.json.quote_credits).toBe(50);       // 2 markers x 25 credits
    expect((await balanceRow(account_id))!.held_micro).toBe(500_000);
    const runId = started.json.run_id as string;

    for (let i = 0; i < 3; i += 1) {
      expect(usageOf(await message(t, request({}, { run_id: runId }))).charged_micro).toBe(OPUS_COST * 2);
    }
    const mid = await call(`/v1/ai/runs/${runId}`, t);
    expect(mid.accrued_micro).toBe(OPUS_COST * 6);
    expect(mid.charged_micro).toBe(OPUS_COST * 6);

    const finished = await post(`/v1/ai/runs/${runId}/finish`, {}, auth);
    expect(finished.json.status).toBe('finished');
    const bal = await balanceRow(account_id);
    expect(bal!.held_micro).toBe(0);
    expect(bal!.prepaid_micro).toBe(10_000_000 - OPUS_COST * 6);
    expect(await ledgerTotal(account_id)).toBe(bal!.prepaid_micro);
  });

  it('never charges past the quote, and refuses calls past the envelope', async () => {
    // An expensive call: 20,000 output tokens = $0.40 each at Opus, $0.80 marked up.
    provider(() => anthropicStream(USAGE, 20_000));
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 1000 });
    const t = await token();
    const auth = { Authorization: `Bearer ${t}` };
    const run = await post('/v1/ai/runs', { feature: 'look', units: 1 }, auth);   // 10 credits, 3 calls
    const runId = run.json.run_id as string;
    const charges: number[] = [];
    for (let i = 0; i < 3; i += 1) charges.push(usageOf(await message(t, request({}, { run_id: runId }))).charged_micro);
    expect(charges).toEqual([100_000, 0, 0]);
    const over = await message(t, request({}, { run_id: runId }));
    expect(over.status).toBe(402);
    expect(over.json.error.code).toBe('run_envelope_exceeded');
    await post(`/v1/ai/runs/${runId}/finish`, {}, auth);
    const bal = await balanceRow(account_id);
    expect(bal!.prepaid_micro).toBe(10_000_000 - 100_000);
    expect(bal!.held_micro).toBe(0);
  });

  it('prices a dev run at cost', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('PATCH', `/ai/accounts/${account_id}`, { mode: 'dev' });
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 1000 });
    const t = await token();
    const run = await post('/v1/ai/dev/runs', { feature: 'gating', units: 1 }, { Authorization: `Bearer ${t}` });
    expect(run.json.billing).toBe('dev');
    const usage = usageOf(await message(t, request({}, { run_id: run.json.run_id }), { path: '/v1/ai/dev/messages' }));
    expect(usage.charged_micro).toBe(OPUS_COST);
    // A dev run cannot be spent on the paid route, or the other way round.
    const crossed = await message(t, request({}, { run_id: run.json.run_id }));
    expect(crossed.json.error.code).toBe('invalid_request');
  });
});

async function call(path: string, token: string) {
  const response = await SELF.fetch(`${BASE}${path}`, { headers: { Authorization: `Bearer ${token}` } });
  return response.json() as Promise<Record<string, any>>;
}

describe('tracking', () => {
  it('shows the account its balance and usage, and an admin everything with margin', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100, kind: 'purchase', journal_id: 'pad_1' });
    const repeat = await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100, kind: 'purchase',
      journal_id: 'pad_1' });
    expect(repeat.json.posted).toBe(false);             // a retried purchase posts once
    const t = await token();
    await message(t, request());
    await message(t, request({}, { feature: 'qc' }));

    const balance = await call('/v1/ai/balance', t);
    expect(balance.prepaid_micro).toBe(1_000_000 - OPUS_COST * 4);
    const usage = await call('/v1/ai/usage?days=7', t);
    expect(usage.rows.map((r: any) => r.feature).sort()).toEqual(['gating', 'qc']);
    expect(usage.rows[0].dev_cost_micro).toBe(0);

    const all = await admin('GET', '/ai/usage?days=7');
    const mine = all.json.accounts.find((a: any) => a.account_id === account_id);
    expect(mine).toMatchObject({ calls: 2, cost_micro: OPUS_COST * 2, charged_micro: OPUS_COST * 4 });
    const detail = await admin('GET', `/ai/accounts/${account_id}`);
    expect(detail.json.balance.prepaid_micro).toBe(1_000_000 - OPUS_COST * 4);
    expect(detail.json.recent).toHaveLength(2);
    expect(detail.json.ledger.length).toBeGreaterThanOrEqual(3);
    expect(await count('events', "kind = 'ai.credit' AND account_id = ?1", account_id)).toBe(1);
  });

  it('grants the monthly allowance once and draws it before prepaid credit', async () => {
    provider();
    const { token, account_id } = await setup();
    await admin('PATCH', `/ai/accounts/${account_id}`, { allowance_micro: 30_000 });
    await admin('POST', `/ai/accounts/${account_id}/credit`, { credits: 100 });
    const t = await token();
    await message(t, request());                        // 40,000: 30,000 allowance + 10,000 prepaid
    const bal = await balanceRow(account_id);
    expect(bal!.allowance_micro).toBe(0);
    expect(bal!.prepaid_micro).toBe(1_000_000 - 10_000);
    expect(await count('ai_ledger', "account_id = ?1 AND kind = 'allowance_grant'", account_id)).toBe(1);
    expect(await ledgerTotal(account_id)).toBe(bal!.prepaid_micro + bal!.allowance_micro);
  });
});

describe('admin settings and usage limits', () => {
  afterEach(async () => {
    travel(0);
    await env.LICENSE_DB.prepare('DELETE FROM ai_settings').run();
  });

  async function funded() {
    const s = await setup();
    expect((await admin('POST', `/ai/accounts/${s.account_id}/credit`, { credits: 1000 })).status).toBe(201);
    return s;
  }

  it('sets, reports and resets a setting in displayed units, and refuses what it may not change', async () => {
    const set = await admin('PUT', '/ai/settings', { AI_ALLOWANCE_PER_SEAT_MICRO: 1500, AI_MAX_BODY_BYTES: '2.5' });
    expect(set.status).toBe(200);
    expect(set.json.changed).toEqual({ AI_ALLOWANCE_PER_SEAT_MICRO: 15_000_000, AI_MAX_BODY_BYTES: 2_500_000 });
    let rows = (await admin('GET', '/ai/settings')).json.settings as any[];
    const allowance = rows.find((r) => r.name === 'AI_ALLOWANCE_PER_SEAT_MICRO');
    expect(allowance).toMatchObject({ source: 'admin', value: 15_000_000, shown: 1500, fallback: 0 });
    expect((await admin('PUT', '/ai/settings', { AI_ALLOWANCE_PER_SEAT_MICRO: null })).status).toBe(200);
    rows = (await admin('GET', '/ai/settings')).json.settings as any[];
    expect(rows.find((r) => r.name === 'AI_ALLOWANCE_PER_SEAT_MICRO')).toMatchObject({ source: 'wrangler', value: 0 });
    expect((await admin('PUT', '/ai/settings', { SIGNING_KEY_PX1: 'x' })).status).toBe(400);
    expect((await admin('PUT', '/ai/settings', { AI_MARKUP_BPS: 0.5 })).status).toBe(400);
    expect(await count('events', "kind = 'ai.settings_changed'")).toBe(2);
  });

  it('switches AI off for every token, call and run, and back on', async () => {
    provider();
    const s = await funded();
    const token = await s.token();
    expect((await admin('PUT', '/ai/settings', { AI_ENABLED: false })).status).toBe(200);
    const refused = await post('/v1/ai/token', { certificate: s.certificate, binding: s.binding, app_version: '0.0.27' });
    expect(refused.status).toBe(503);
    expect(refused.json.error.code).toBe('ai_disabled');
    const call = await message(token, request());
    expect(call.status).toBe(503);
    expect(call.json.error.code).toBe('ai_disabled');
    expect(seen).toHaveLength(0);
    await admin('PUT', '/ai/settings', { AI_ENABLED: 'default' });
    expect((await message(token, request())).status).toBe(200);
  });

  it('caps calls per person per day, frees the refused key, and resets the next UTC day', async () => {
    provider();
    const s = await funded();
    await admin('PUT', '/ai/settings', { AI_CALLS_PER_SEAT_PER_DAY: 2 });
    const token = await s.token();
    expect((await message(token, request())).status).toBe(200);
    expect((await message(token, request())).status).toBe(200);
    const third = await message(token, request(), { key: 'over-the-limit-1' });
    expect(third.status).toBe(429);
    expect(third.json.error.code).toBe('usage_limit_reached');
    expect(third.json.error.details).toMatchObject({ scope: 'person', limit: 2 });
    expect(seen).toHaveLength(2);
    travel(86_400);
    // The refused key was never spent: tomorrow the same key is a fresh call.
    expect((await message(await s.token(), request(), { key: 'over-the-limit-1' })).status).toBe(200);
  });

  it("an account's own daily limit beats the global one, and empty goes back to it", async () => {
    provider();
    const s = await funded();
    await admin('PUT', '/ai/settings', { AI_CALLS_PER_ACCOUNT_PER_DAY: 100 });
    const own = await admin('PATCH', `/ai/accounts/${s.account_id}/limits`, { calls_per_day: 1 });
    expect(own.status).toBe(200);
    expect(own.json.effective).toEqual({ account: 1, seat: 0 });
    const token = await s.token();
    expect((await message(token, request())).status).toBe(200);
    const second = await message(token, request());
    expect(second.json.error).toMatchObject({ code: 'usage_limit_reached', details: { scope: 'account', limit: 1 } });
    const back = await admin('PATCH', `/ai/accounts/${s.account_id}/limits`, { calls_per_day: '' });
    expect(back.json.effective).toEqual({ account: 100, seat: 0 });
    expect((await message(token, request())).status).toBe(200);
  });

  it('shows the switch, the settings and the account limits on the admin pages', async () => {
    const s = await setup();
    const headers = { Authorization: 'Bearer test-admin' };
    const page = await callApi('GET', '/admin/ai', undefined, headers);
    expect(page.json.text).toContain('Switch off');
    const settings = await callApi('GET', '/admin/ai/settings', undefined, headers);
    for (const text of ['Limits', 'Calls per person per day', 'Refresh prices nightly']) {
      expect(settings.json.text).toContain(text);
    }
    // The switch has one home: the page header, never a settings form.
    expect(settings.json.text).not.toContain('name="AI_ENABLED"');
    await admin('PUT', '/ai/settings', { AI_ENABLED: 0 });
    for (const path of ['/admin/ai', '/admin/ai/models', '/admin/ai/tasks']) {
      expect((await callApi('GET', path, undefined, headers)).json.text).toContain('Switch AI on');
    }
    const licence = await callApi('GET', `/admin/licenses/${s.license.id}`, undefined, headers);
    expect(licence.json.text).toContain('Daily limits for this account');
    expect(licence.json.text).toContain('Calls today (UTC)');
  });
});
