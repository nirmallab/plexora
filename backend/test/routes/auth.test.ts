import { env, fetchMock, SELF } from 'cloudflare:test';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { resetAccessCache, verifyAccessJwt } from '../../src/admin/access';
import type { Env } from '../../src/env';
import { base64url } from '../../src/telemetry/tokens';
import { admin, ADMIN_TOKEN, BASE, count } from './helpers';

const TEAM = 'plexora-test.cloudflareaccess.com';
const AUD = 'aud-123';
const ISSUER = `https://${TEAM}`;

let keys: CryptoKeyPair;
let jwk: JsonWebKey & { kid?: string };
let published: unknown;

const encode = (value: unknown) => base64url(new TextEncoder().encode(JSON.stringify(value)));

async function jwt(claims: Record<string, unknown>, header: Record<string, unknown> = {}) {
  const head = encode({ alg: 'RS256', kid: 'k1', typ: 'JWT', ...header });
  const body = encode(claims);
  const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', keys.privateKey, new TextEncoder().encode(`${head}.${body}`));
  return `${head}.${body}.${base64url(new Uint8Array(sig))}`;
}

const claims = (over: Record<string, unknown> = {}) => ({
  iss: ISSUER,
  aud: [AUD],
  exp: Math.floor(Date.now() / 1000) + 600,
  email: 'owner@lab.example',
  ...over,
});
const accessEnv = { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD } as unknown as Env;
const req = (token: string) => new Request(`${BASE}/admin`, { headers: { 'Cf-Access-Jwt-Assertion': token } });

beforeAll(async () => {
  keys = (await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true,
    ['sign', 'verify'],
  )) as CryptoKeyPair;
  jwk = (await crypto.subtle.exportKey('jwk', keys.publicKey)) as JsonWebKey & { kid?: string };
  jwk.kid = 'k1';
  fetchMock.activate();
  fetchMock.disableNetConnect();
  fetchMock
    .get(ISSUER)
    .intercept({ path: '/cdn-cgi/access/certs' })
    .reply(200, () => JSON.stringify(published), { headers: { 'Content-Type': 'application/json' } })
    .persist();
});

afterAll(() => fetchMock.deactivate());

beforeEach(() => {
  resetAccessCache();
  published = { keys: [jwk] };
});

describe('Cloudflare Access JWT', () => {
  it('accepts a correctly signed token and yields the email', async () => {
    expect(await verifyAccessJwt(accessEnv, req(await jwt(claims())))).toBe('owner@lab.example');
  });

  it('refuses the wrong audience, issuer, an expired token, alg none and a foreign key', async () => {
    expect(await verifyAccessJwt(accessEnv, req(await jwt(claims({ aud: ['other'] }))))).toBeNull();
    expect(await verifyAccessJwt(accessEnv, req(await jwt(claims({ iss: 'https://evil.example' }))))).toBeNull();
    expect(await verifyAccessJwt(accessEnv, req(await jwt(claims({ exp: 1 }))))).toBeNull();
    const none = `${encode({ alg: 'none', kid: 'k1' })}.${encode(claims())}.`;
    expect(await verifyAccessJwt(accessEnv, req(none))).toBeNull();
    published = { keys: [] };
    resetAccessCache();
    expect(await verifyAccessJwt(accessEnv, req(await jwt(claims())))).toBeNull();
  });

  it('is ignored when Access is not configured', async () => {
    expect(await verifyAccessJwt({} as Env, req(await jwt(claims())))).toBeNull();
  });
});

describe('admin auth', () => {
  it('401 JSON for scripts, 302 to sign-in for browsers', async () => {
    const json = await admin('/admin/usage', {}, '');
    expect(json.status).toBe(401);
    expect(await json.json()).toMatchObject({ error: expect.any(String) });
    const html = await admin('/admin/usage?from=2026-01-01', { headers: { Accept: 'text/html' } }, '');
    expect(html.status).toBe(302);
    expect(html.headers.get('Location')).toBe(`/admin/login?next=${encodeURIComponent('/admin/usage?from=2026-01-01')}`);
    expect((await admin('/admin/usage', {}, 'wrong')).status).toBe(401);
  });

  it('bearer ADMIN_TOKEN works', async () => {
    const response = await admin('/admin/usage');
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ dau: expect.any(Number) });
  });

  it('sign-in sets an HttpOnly, SameSite=Lax, /admin-scoped cookie that authenticates; a tampered one does not', async () => {
    const wrong = await SELF.fetch(`${BASE}/admin/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: 'nope' }),
    });
    expect(wrong.status).toBe(401);
    const login = await SELF.fetch(`${BASE}/admin/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Sec-Fetch-Site': 'same-origin' },
      body: JSON.stringify({ token: ADMIN_TOKEN }),
    });
    expect(login.status).toBe(200);
    const setCookie = login.headers.get('Set-Cookie')!;
    expect(setCookie).toMatch(/^plexora_admin=v1\./);
    expect(setCookie).toContain('HttpOnly');
    expect(setCookie).toContain('Path=/admin');
    expect(setCookie).toContain('SameSite=Lax');
    expect(setCookie).not.toContain('Secure'); // http in tests; https adds it
    const cookie = setCookie.split(';')[0]!;
    expect((await admin('/admin/usage', { headers: { Cookie: cookie } }, '')).status).toBe(200);
    const tampered = cookie.replace(/.$/, (ch) => (ch === 'a' ? 'b' : 'a'));
    expect((await admin('/admin/usage', { headers: { Cookie: tampered } }, '')).status).toBe(401);
  });

  it('refuses cross-site and non-JSON mutations, even with a valid token', async () => {
    const body = JSON.stringify({ version: '*', sample: 0.5 });
    const cross = await admin('/admin/api/client-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Sec-Fetch-Site': 'cross-site' },
      body,
    });
    expect(cross.status).toBe(403);
    const origin = await admin('/admin/api/client-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Origin: 'https://evil.example' },
      body,
    });
    expect(origin.status).toBe(403);
    const form = await admin('/admin/api/client-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'version=*',
    });
    expect(form.status).toBe(415);
    expect(await env.TELEMETRY_DB.prepare('SELECT COUNT(*) AS n FROM client_config').first('n')).toBe(0);
  });
});

describe('admin token sign-in limit', () => {
  const login = (token: string, ip: string) =>
    SELF.fetch(`${BASE}/admin/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'CF-Connecting-IP': ip },
      body: JSON.stringify({ token }),
    });

  it('allows 10 attempts an hour per address and 30 in all; past either even the right token is refused', async () => {
    await env.TELEMETRY_DB.prepare(`DELETE FROM register_quota WHERE ip_hash LIKE 'login:%'`).run();
    for (let i = 0; i < 10; i += 1) expect((await login('wrong', '198.51.100.20')).status).toBe(401);
    const blocked = await login(ADMIN_TOKEN, '198.51.100.20');
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get('Set-Cookie')).toBeNull();
    const wait = Number(blocked.headers.get('Retry-After'));
    expect(wait).toBeGreaterThan(0);
    expect(wait).toBeLessThanOrEqual(3600);

    // Another address still gets in.
    const other = await login(ADMIN_TOKEN, '198.51.100.21');
    expect(other.status).toBe(200);

    // 11 counted so far; 19 more from many addresses fill the global window.
    for (let i = 0; i < 19; i += 1) expect((await login('wrong', `203.0.113.${100 + i}`)).status).toBe(401);
    expect((await login(ADMIN_TOKEN, '192.0.2.1')).status).toBe(429);

    // A refused attempt moves no counter, and no address is stored.
    const global = await env.TELEMETRY_DB.prepare(`SELECT n, day FROM register_quota WHERE ip_hash = 'login:*'`).first<{ n: number; day: string }>();
    expect(global!.n).toBe(30);
    expect(global!.day).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}$/);
    expect(await count('register_quota', `ip_hash LIKE 'login:%' AND ip_hash LIKE '%198.51%'`)).toBe(0);
    expect(await count('register_quota', `ip_hash LIKE 'login:%' AND n > 10 AND ip_hash != 'login:*'`)).toBe(0);
  });

  it('production with no IP_HASH_KEY refuses the token sign-in', async () => {
    const { loginAttempt } = await import('../../src/admin/limit');
    const bindings = { ...env } as Record<string, unknown>;
    delete bindings.IP_HASH_KEY;
    const gate = await loginAttempt(bindings as unknown as Env, '198.51.100.30', Math.floor(Date.now() / 1000));
    expect(gate).toMatchObject({ ok: false, status: 503 });
  });
});
