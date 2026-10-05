import { env, SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { ensureLateColumns, resetLateColumns } from '../../src/db';
import { outbox, resetOutbox } from '../../src/email';
import { activate, admin, BASE, call, claims, count, environmentBody, fingerprint, issue, post, travel } from './helpers';

const HOUR = 3600;
const ADMIN_HTML = { Authorization: 'Bearer test-admin', Accept: 'text/html' };

async function registered(overrides: Record<string, unknown> = {}) {
  const issued = await issue(overrides);
  const body = environmentBody();
  const reply = await activate(issued.seat.key, body);
  expect(reply.status).toBe(200);
  return { ...issued, certificate: reply.json.certificate as string, binding: body.binding as string,
    environmentId: reply.json.environment.id as string };
}

async function lastMcp(environmentId: string): Promise<number | null> {
  return (await env.LICENSE_DB.prepare('SELECT last_mcp_at FROM environments WHERE id = ?1').bind(environmentId)
    .first<{ last_mcp_at: number | null }>())?.last_mcp_at ?? null;
}

async function signIn(email: string): Promise<string> {
  resetOutbox();
  await call('POST', '/portal/login', { email });
  const secret = /t=([\w-]+)/.exec(outbox.at(-1)!.text)![1]!;
  const response = await SELF.fetch(`${BASE}/portal/auth`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ t: secret }) });
  return response.headers.get('Set-Cookie')!.split(';')[0]!;
}

const page = async (path: string, headers: Record<string, string> = ADMIN_HTML) =>
  (await SELF.fetch(`${BASE}${path}`, { headers })).text();

describe('the MCP recheck (client: mcp on /v1/refresh)', () => {
  it('records when an environment was seen over MCP, an hour apart at most, an event a day', async () => {
    const { certificate, binding, environmentId } = await registered();
    const plain = await post('/v1/refresh', { certificate, binding });
    expect(plain.json.status).toBe('ok');
    expect(await lastMcp(environmentId)).toBeNull();

    const first = await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    expect(first.json.status).toBe('ok');
    const seen = await lastMcp(environmentId);
    expect(seen).toBeTypeOf('number');
    expect(await count('events', "kind = 'environment.mcp_seen'")).toBe(1);

    const before = await fingerprint();
    await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    expect(await fingerprint(), 'a recheck inside the hour writes nothing').toBe(before);

    travel(2 * HOUR);
    await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    expect(await lastMcp(environmentId)).toBeGreaterThan(seen!);
    expect(await count('events', "kind = 'environment.mcp_seen'")).toBe(1);

    travel(2 * HOUR + 25 * HOUR);
    await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    expect(await count('events', "kind = 'environment.mcp_seen'"), 'back after a day away').toBe(2);
  });

  it('writes nothing for a released environment', async () => {
    const { certificate, binding, seat, environmentId } = await registered();
    await admin('POST', `/seats/${seat.id}/release`);
    const reply = await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    expect(reply.json.status).toBe('revoked');
    expect(await lastMcp(environmentId)).toBeNull();
  });

  it('a seat override reaches the certificate, and inheriting puts it back', async () => {
    const { certificate, binding, seat } = await registered({ entitlements: ['ai', 'mcp'] });
    expect(claims(certificate).entitlements).toEqual(['ai', 'mcp']);
    expect((await admin('PATCH', `/seats/${seat.id}`, { entitlements_override: ['ai'] })).status).toBe(200);
    const narrowed = await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    expect(narrowed.json.status).toBe('renewed');
    expect(claims(narrowed.json.certificate).entitlements).toEqual(['ai']);
    await admin('PATCH', `/seats/${seat.id}`, { entitlements_override: null });
    const back = await post('/v1/refresh', { certificate: narrowed.json.certificate, binding });
    expect(back.json.status).toBe('renewed');
    expect(claims(back.json.certificate).entitlements).toEqual(['ai', 'mcp']);
  });
});

describe('admin: grants per seat and per organisation', () => {
  it('PATCH /admin/api/seats/:id validates, records and shows the override', async () => {
    const { seat, license } = await registered();
    expect((await admin('PATCH', `/seats/${seat.id}`, {})).status).toBe(400);
    expect((await admin('PATCH', `/seats/${seat.id}`, { entitlements_override: ['AI'] })).status).toBe(400);
    const set = await admin('PATCH', `/seats/${seat.id}`, { entitlements_override: ['mcp'] });
    expect(set.status).toBe(200);
    expect(set.json.seat.entitlements_override).toEqual(['mcp']);
    expect(await count('events', "kind = 'seat.updated'")).toBe(1);
    const detail = await admin('GET', `/licenses/${license.id}`);
    expect(detail.json.seats[0].entitlements_override).toEqual(['mcp']);
    expect(detail.json.environments[0].mcp_last_seen).toBeNull();
    await admin('POST', `/seats/${seat.id}/release`);
    expect((await admin('PATCH', `/seats/${seat.id}`, { entitlements_override: null })).status).toBe(409);
  });

  it('shows when an environment was last seen over MCP', async () => {
    const { certificate, binding, license } = await registered();
    await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    const detail = await admin('GET', `/licenses/${license.id}`);
    expect(detail.json.environments[0].mcp_last_seen).toBeTypeOf('number');
    expect(await page(`/admin/licenses/${license.id}`)).toContain('MCP seen');
  });

  it('apply_to_account sets the grants on every active licence of the organisation', async () => {
    const first = await issue({ owner_email: 'org@lab.example.org', account_kind: 'organization' });
    const second = await issue({ account_id: first.account_id });
    const third = await issue({ account_id: first.account_id });
    expect(second.account_id).toBe(first.account_id);
    await admin('POST', `/licenses/${third.license.id}/revoke`, { reason: 'test' });
    expect((await admin('PATCH', `/licenses/${first.license.id}`, { apply_to_account: true })).status).toBe(400);
    const reply = await admin('PATCH', `/licenses/${first.license.id}`, { entitlements: ['ai', 'mcp'],
      apply_to_account: true });
    expect(reply.status).toBe(200);
    expect(reply.json.applied_to).toEqual([second.license.id]);
    const grants = async (id: string) => (await admin('GET', `/licenses/${id}`)).json.license.entitlements;
    expect(await grants(second.license.id)).toEqual(['ai', 'mcp']);
    expect(await grants(third.license.id)).toEqual(['ai']);
  });
});

describe('portal: an owner narrows a seat', () => {
  it('narrows, never widens, and only an owner or admin may', async () => {
    const issued = await issue({ owner_email: 'grants@lab.example.org', seats: 2, entitlements: ['ai', 'mcp'] });
    const member = await admin('POST', `/licenses/${issued.license.id}/seats`, { email: 'member@lab.example.org' });
    const body = environmentBody();
    const activated = await activate(member.json.key, body);
    const owner = await signIn('grants@lab.example.org');
    const path = `/portal/api/seats/${member.json.seat.id}/entitlements`;

    const narrowed = await call('POST', path, { entitlements_override: ['ai'] }, { Cookie: owner });
    expect(narrowed.status).toBe(200);
    const renewed = await post('/v1/refresh', { certificate: activated.json.certificate, binding: body.binding });
    expect(claims(renewed.json.certificate).entitlements).toEqual(['ai']);

    expect((await call('POST', path, { entitlements_override: ['ai', 'plugin:x'] }, { Cookie: owner })).status).toBe(400);
    const seatsPage = await page('/portal/seats', { Cookie: owner, Accept: 'text/html' });
    expect(seatsPage).toContain('External MCP access');
    expect(seatsPage).toContain('as the licence');

    const memberCookie = await signIn('member@lab.example.org');
    expect((await call('POST', path, { entitlements_override: ['ai'] }, { Cookie: memberCookie })).status).toBe(403);
    expect((await call('POST', `/portal/api/seats/${issued.seat.id}/entitlements`, { entitlements_override: [] },
      { Cookie: memberCookie })).status).toBe(404);

    expect((await call('POST', path, { entitlements_override: null }, { Cookie: owner })).status).toBe(200);
    const seat = (await admin('GET', `/licenses/${issued.license.id}`)).json.seats
      .find((s: any) => s.id === member.json.seat.id);
    expect(seat.entitlements_override).toBeNull();
  });
});

describe('admin pages', () => {
  it('filters licences by what they unlock and counts MCP on the dashboard', async () => {
    const mcp = await issue({ owner_email: 'mcp-filter@lab.example.org', account_name: 'MCP Filter Lab',
      entitlements: ['mcp'] });
    await issue({ owner_email: 'ai-filter@lab.example.org', account_name: 'AI Filter Lab' });
    await issue({ owner_email: 'app-filter@lab.example.org', account_name: 'App Filter Lab', entitlements: [] });
    const onlyMcp = await page('/admin/licenses?unlocks=mcp');
    expect(onlyMcp).toContain('MCP Filter Lab');
    expect(onlyMcp).not.toContain('AI Filter Lab');
    const none = await page('/admin/licenses?unlocks=none');
    expect(none).toContain('App Filter Lab');
    expect(none).not.toContain('MCP Filter Lab');
    expect(await page('/admin')).toContain('External MCP');
    expect(await page('/admin/issue')).toContain('AI harness + MCP');
    expect(mcp.license.entitlements).toEqual(['mcp']);
  });
});

describe('schema v6', () => {
  it('adds last_mcp_at to an environments table that predates it', async () => {
    const { certificate, binding, environmentId } = await registered();
    await env.LICENSE_DB.prepare('ALTER TABLE environments DROP COLUMN last_mcp_at').run();
    resetLateColumns();
    await ensureLateColumns(env);
    const reply = await post('/v1/refresh', { certificate, binding, client: 'mcp' });
    expect(reply.json.status).toBe('ok');
    expect(await lastMcp(environmentId)).toBeTypeOf('number');
  });
});
