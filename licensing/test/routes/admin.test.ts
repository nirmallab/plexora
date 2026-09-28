import { SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { outbox } from '../../src/email';
import { activate, admin, BASE, call, count, issue, post } from './helpers';

describe('admin authentication', () => {
  it('refuses without credentials, and with the wrong token', async () => {
    expect((await call('GET', '/admin/api/licenses')).status).toBe(401);
    expect((await call('GET', '/admin/api/licenses', undefined, { Authorization: 'Bearer nope' })).status).toBe(401);
    const page = await SELF.fetch(`${BASE}/admin`, { headers: { Accept: 'text/html' }, redirect: 'manual' });
    expect(page.status).toBe(302);
    expect(page.headers.get('Location')).toBe('/admin/login');
  });

  it('signs in with the admin token and a cookie', async () => {
    expect((await post('/admin/login', { token: 'wrong' })).status).toBe(401);
    const login = await SELF.fetch(`${BASE}/admin/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: 'test-admin' }),
    });
    expect(login.status).toBe(200);
    const cookie = login.headers.get('Set-Cookie')!;
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/SameSite=Strict/i);
    const page = await SELF.fetch(`${BASE}/admin`, { headers: { Cookie: cookie.split(';')[0]!, Accept: 'text/html' } });
    expect(page.status).toBe(200);
    expect(page.headers.get('Content-Security-Policy')).toContain("default-src 'none'");
  });

  it('refuses a cross-site mutation', async () => {
    const reply = await call('POST', '/admin/api/licenses', { owner_email: 'x@y.org' },
      { Authorization: 'Bearer test-admin', 'Sec-Fetch-Site': 'cross-site' });
    expect(reply.status).toBe(403);
  });
});

describe('manual issuance and management', () => {
  it('issues a licence, emails the owner their seat key, and shows it once', async () => {
    const reply = await admin('POST', '/licenses', { owner_email: 'pi@lab.example.org', account_name: 'Nirmal Lab',
      account_kind: 'organization', use_class: 'academic', seats: 3, days: 365 });
    expect(reply.status).toBe(201);
    expect(reply.json.seat.key).toMatch(/^PLEX-/);
    expect(outbox[0]!.to).toBe('pi@lab.example.org');
    expect(outbox[0]!.text).toContain(reply.json.seat.key);
    const detail = await admin('GET', `/licenses/${reply.json.license.id}`);
    expect(JSON.stringify(detail.json)).not.toContain(reply.json.seat.key);
    expect(JSON.stringify(detail.json)).not.toMatch(/seat_key_hash|env_binding_hash|token_hash/);
    expect(detail.json.seats[0].key_hint).toMatch(/^PLEX-\*{4}-\*{4}-\*{4}-/);
  });

  it('validates what it is given', async () => {
    expect((await admin('POST', '/licenses', { owner_email: 'nope' })).status).toBe(400);
    expect((await admin('POST', '/licenses', { owner_email: 'a@b.org', use_class: 'hobby', days: 1 })).status).toBe(400);
    expect((await admin('POST', '/licenses', { owner_email: 'a@b.org', seats: 0, days: 1 })).status).toBe(400);
    expect((await admin('POST', '/licenses', { owner_email: 'a@b.org', seats: 1 })).status).toBe(400);
    expect((await admin('POST', '/licenses', { owner_email: 'a@b.org', days: 1, entitlements: ['AI'] })).status).toBe(400);
  });

  it('enforces the seat count', async () => {
    const { license } = await issue({ seats: 2 });
    expect((await admin('POST', `/licenses/${license.id}/seats`, { email: 'second@lab.example.org' })).status).toBe(201);
    const third = await admin('POST', `/licenses/${license.id}/seats`, {});
    expect(third.status).toBe(409);
    expect(third.json.error.code).toBe('seat_limit');
  });

  it('extends, suspends, revokes -- and revocation is final', async () => {
    const { license } = await issue({ days: 30 });
    const extended = await admin('PATCH', `/licenses/${license.id}`, { extend_days: 365 });
    expect(extended.json.license.expires_at).toBeGreaterThan(license.expires_at + 364 * 86400);
    expect((await admin('PATCH', `/licenses/${license.id}`, { status: 'suspended' })).json.license.status).toBe('suspended');
    await admin('POST', `/licenses/${license.id}/revoke`, { reason: 'test' });
    expect((await admin('PATCH', `/licenses/${license.id}`, { status: 'active' })).status).toBe(409);
    expect(await count('events', "kind = 'license.revoked'")).toBe(1);
  });

  it('searches by email and account', async () => {
    await issue({ owner_email: 'findme@lab.example.org', account_name: 'Findable Lab' });
    expect((await admin('GET', '/licenses?q=findme')).json.licenses).toHaveLength(1);
    expect((await admin('GET', '/licenses?q=findable')).json.licenses).toHaveLength(1);
    expect((await admin('GET', '/licenses?q=nobody')).json.licenses).toHaveLength(0);
  });

  it('rotating a key retires the old one', async () => {
    const { seat } = await issue();
    const rotated = await admin('POST', `/seats/${seat.id}/rotate-key`);
    expect((await activate(seat.key)).json.error.code).toBe('invalid_credential');
    expect((await activate(rotated.json.key)).status).toBe(200);
  });

  it('issues an offline licence against a fingerprint report', async () => {
    const { seat } = await issue();
    const reply = await admin('POST', `/seats/${seat.id}/offline`, { days: 30, report: {
      product: 'plexora', kind: 'desktop', display_name: 'Air-gapped scope PC', binding: 'c'.repeat(64) } });
    expect(reply.status).toBe(201);
    expect(reply.json.file).toMatch(/^# Plexora offline licence/);
    expect(reply.json.file).toMatch(/cannot be recalled/);
    const cert = reply.json.file.split('\n').find((l: string) => l.startsWith('PLEXORA1.'));
    const { claims } = await import('./helpers');
    const payload = claims(cert);
    expect(payload.offline_until).toBe(payload.expires_at);
    expect(payload.env_binding).toBe('c'.repeat(64));
    expect(await count('offline_grants')).toBe(1);
    expect(await count('environments', "registration_kind = 'offline'")).toBe(1);
  });

  it('refuses offline licences a licence does not include', async () => {
    const { seat } = await issue({ offline_allowed: false });
    const reply = await admin('POST', `/seats/${seat.id}/offline`, { report: { product: 'plexora', binding: 'd'.repeat(64) } });
    expect(reply.json.error.code).toBe('offline_not_allowed');
  });

  it('reports stats and runs maintenance on request', async () => {
    await issue();
    expect((await admin('GET', '/stats')).json.licenses.length).toBeGreaterThan(0);
    const ran = await admin('POST', '/cron', { task: 'expire' });
    expect(ran.status).toBe(200);
    expect(ran.json.expire).toBe(0);
  });
});
