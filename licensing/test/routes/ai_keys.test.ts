import { createExecutionContext, env, SELF, waitOnExecutionContext } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { setUpstreamFetch } from '../../src/ai/providers';
import worker from '../../src/index';
import { anthropicStream, install, message, on, request, seen, setup } from './ai_fakes';
import { admin, ADMIN_HEADERS, BASE, call, count } from './helpers';

/**
 * Provider keys set on /admin/ai/providers: checked with the provider, sealed in
 * D1, used ahead of the Worker secret, never shown again.
 */

const KEY = 'sk-ant-test-key-from-the-page-0001';

/** A provider that accepts KEY (an empty request is a 400) and refuses anything else (401). */
function provider() {
  setUpstreamFetch(async (url, init) => {
    const headers = new Headers(init.headers);
    const key = headers.get('x-api-key') ?? headers.get('authorization')?.replace(/^Bearer /, '');
    if (init.body === '{}') {
      return key === KEY || key?.startsWith('sk-or-ok')
        ? new Response('{"error":{"message":"messages: field required"}}', { status: 400 })
        : new Response(`{"error":{"message":"invalid x-api-key ${key}"}}`, { status: 401 });
    }
    return new Response('unexpected', { status: 500 });
  });
}

const keysOf = async () => (await admin('GET', '/ai/keys')).json.keys as Array<Record<string, any>>;

describe('provider keys on the API page', () => {
  it('lists every provider, with the Worker secrets the deploy carries', async () => {
    const keys = await keysOf();
    expect(keys.map((k) => k.provider)).toEqual(['anthropic', 'openai', 'openrouter', 'orcarouter', 'saygm']);
    expect(keys.every((k) => k.source === 'secret' && k.hint === null)).toBe(true);
  });

  it('checks a key, seals it, and never sends it back', async () => {
    provider();
    const saved = await admin('PUT', '/ai/keys/anthropic', { key: KEY });
    expect(saved.status, JSON.stringify(saved.json)).toBe(200);
    expect(saved.json.key).toMatchObject({ provider: 'anthropic', source: 'page', hint: '0001', secret: true,
      check: { ok: true } });
    expect(JSON.stringify(saved.json)).not.toContain(KEY);
    expect(JSON.stringify((await admin('GET', '/ai/keys')).json)).not.toContain(KEY);
    const stored = await env.LICENSE_DB.prepare('SELECT * FROM ai_provider_keys WHERE provider = ?1').bind('anthropic')
      .first<Record<string, any>>();
    expect(stored!.vault).toMatch(/^v1\./);
    expect(JSON.stringify(stored)).not.toContain(KEY);
    const events = await env.LICENSE_DB.prepare("SELECT payload FROM events WHERE kind LIKE 'ai.key_%'").all();
    expect(JSON.stringify(events.results)).not.toContain(KEY);
    expect(await count('events', "kind = 'ai.key_set'")).toBe(1);
  });

  it('refuses a key the provider refuses, and a malformed one, saving neither', async () => {
    provider();
    const refused = await admin('PUT', '/ai/keys/anthropic', { key: 'sk-ant-wrong-key-000000000' });
    expect(refused.status).toBe(400);
    expect(refused.json.error.message).toContain('refused the key');
    expect(refused.json.error.message).not.toContain('sk-ant-wrong-key-000000000');
    expect((await admin('PUT', '/ai/keys/anthropic', { key: 'short' })).status).toBe(400);
    expect((await admin('PUT', '/ai/keys/anthropic', { key: 'has a space in it, not a key' })).status).toBe(400);
    expect((await admin('PUT', '/ai/keys/nobody', { key: KEY })).status).toBe(400);
    expect(await count('ai_provider_keys')).toBe(0);
  });

  it('a key set here serves calls ahead of the Worker secret, and the secret again once it is removed', async () => {
    const { token } = await setup();
    provider();
    expect((await admin('PUT', '/ai/keys/anthropic', { key: KEY })).status).toBe(200);
    install();   // back to the fake provider for calls
    on('api.anthropic.com/v1/messages', () => anthropicStream());
    expect((await message(token, request())).status).toBe(200);
    expect(seen.at(-1)!.headers.get('x-api-key')).toBe(KEY);
    expect((await admin('DELETE', '/ai/keys/anthropic')).status).toBe(200);
    expect((await message(token, request())).status).toBe(200);
    expect(seen.at(-1)!.headers.get('x-api-key')).toBe('test-anthropic');
    expect((await admin('DELETE', '/ai/keys/anthropic')).status).toBe(404);
    expect(await count('events', "kind = 'ai.key_removed'")).toBe(1);
  });

  it('tests the key in use and records the result', async () => {
    provider();
    const tested = await admin('POST', '/ai/keys/openrouter/test');
    expect(tested.status).toBe(200);
    expect(tested.json.check).toMatchObject({ ok: false, status: 401 });
    const row = (await keysOf()).find((k) => k.provider === 'openrouter')!;
    expect(row).toMatchObject({ source: 'secret', check: { ok: false } });
    expect(await count('events', "kind = 'ai.key_checked'")).toBe(1);
  });

  it('without KEY_VAULT_KEY stores nothing, and says how to fix it', async () => {
    provider();
    const ctx = createExecutionContext();
    const response = await worker.fetch(new Request(`${BASE}/admin/api/ai/keys/anthropic`, { method: 'PUT',
      headers: { ...ADMIN_HEADERS, Origin: BASE, 'Sec-Fetch-Site': 'same-origin' }, body: JSON.stringify({ key: KEY }) }),
    { ...env, KEY_VAULT_KEY: undefined }, ctx);
    await waitOnExecutionContext(ctx);
    expect(response.status).toBe(409);
    expect(((await response.json()) as any).error.message).toContain('KEY_VAULT_KEY');
    expect(await count('ai_provider_keys')).toBe(0);
  });

  it('is for admins only', async () => {
    expect((await call('GET', '/admin/api/ai/keys')).status).toBe(401);
    expect((await call('PUT', '/admin/api/ai/keys/anthropic', { key: KEY })).status).toBe(401);
    const page = await SELF.fetch(`${BASE}/admin/ai/providers`, { headers: { Accept: 'text/html' }, redirect: 'manual' });
    expect(page.status).toBe(302);
    const moved = await SELF.fetch(`${BASE}/admin/ai/api`, { headers: { Accept: 'text/html',
      Authorization: 'Bearer test-admin' }, redirect: 'manual' });
    expect(moved.status).toBe(301);
    expect(moved.headers.get('Location')).toBe('/admin/ai/providers');
  });
});

describe('provider connection state', () => {
  const stateOf = async (p: string) => (await admin('GET', '/ai/providers')).json.providers
    .find((x: any) => x.provider === p);

  it('reads unchecked, refused, connected, off and not connected', async () => {
    const all = (await admin('GET', '/ai/providers')).json.providers;
    expect(all.map((p: any) => p.state)).toEqual(['unchecked', 'unchecked', 'unchecked', 'unchecked', 'unchecked']);
    expect(all[0]).toMatchObject({ provider: 'anthropic', label: 'Anthropic', configured: true,
      key: { source: 'secret' } });
    provider();
    await admin('POST', '/ai/keys/openrouter/test');
    expect(await stateOf('openrouter')).toMatchObject({ state: 'key_refused', check: { ok: false, status: 401 } });
    await admin('PUT', '/ai/keys/anthropic', { key: KEY });
    expect(await stateOf('anthropic')).toMatchObject({ state: 'connected', key: { source: 'page', hint: '0001' } });
    await admin('POST', '/ai/providers/saygm/disable', { reason: 'test' });
    expect((await stateOf('saygm')).state).toBe('off');
    const ctx = createExecutionContext();
    const response = await worker.fetch(new Request(`${BASE}/admin/api/ai/providers`, { headers: ADMIN_HEADERS }),
      { ...env, OPENAI_API_KEY: undefined }, ctx);
    await waitOnExecutionContext(ctx);
    const body = await response.json() as any;
    expect(body.providers.find((p: any) => p.provider === 'openai').state).toBe('not_connected');
  });

  it("counts a provider's routes and models", async () => {
    await admin('POST', '/ai/catalog/seed-builtin');
    expect(await stateOf('anthropic')).toMatchObject({ routes: 3, models: 3 });
  });
});
