import { SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { outbox, resetOutbox } from '../../src/email';
import { activate, admin, BASE, call, count, environmentBody, issue } from './helpers';

/** Sign in through the real magic-link flow; returns the session cookie. */
async function signIn(email: string): Promise<string> {
  resetOutbox();
  expect((await call('POST', '/portal/login', { email })).status).toBe(200);
  const link = /http:\/\/localhost\/portal\/auth\?t=([\w-]+)/.exec(outbox.at(-1)?.text ?? '');
  expect(link, 'a sign-in link was emailed').toBeTruthy();
  const response = await SELF.fetch(`${BASE}/portal/auth`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ t: link![1] }),
  });
  expect(response.status).toBe(200);
  return response.headers.get('Set-Cookie')!.split(';')[0]!;
}

const as = (cookie: string) => ({ Cookie: cookie });

describe('portal sign-in', () => {
  it('answers the same for unknown addresses, and sends nothing', async () => {
    const reply = await call('POST', '/portal/login', { email: 'stranger@nowhere.example.org' });
    expect(reply.status).toBe(200);
    expect(outbox).toHaveLength(0);
  });

  it('a link works once, and the GET spends nothing', async () => {
    await issue({ owner_email: 'owner@lab.example.org' });
    resetOutbox();
    await call('POST', '/portal/login', { email: 'owner@lab.example.org' });
    const secret = /t=([\w-]+)/.exec(outbox[0]!.text)![1]!;
    expect((await SELF.fetch(`${BASE}/portal/auth?t=${secret}`)).status).toBe(200);
    expect((await call('POST', '/portal/auth', { t: secret })).status).toBe(200);
    expect((await call('POST', '/portal/auth', { t: secret })).status).toBe(401);
  });

  it('the cookie is HttpOnly and scoped to the portal', async () => {
    await issue({ owner_email: 'cookie@lab.example.org' });
    await call('POST', '/portal/login', { email: 'cookie@lab.example.org' });
    const secret = /t=([\w-]+)/.exec(outbox.at(-1)!.text)![1]!;
    const response = await SELF.fetch(`${BASE}/portal/auth`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ t: secret }) });
    const cookie = response.headers.get('Set-Cookie')!;
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/Path=\/portal/);
  });

  it('pages redirect to sign-in, the API refuses', async () => {
    const page = await SELF.fetch(`${BASE}/portal/environments`, { redirect: 'manual' });
    expect(page.status).toBe(302);
    expect((await call('POST', '/portal/api/tokens', {})).status).toBe(401);
  });
});

describe('what a seat holder and an owner can do', () => {
  it('an owner sees every environment; a member only their own', async () => {
    const issued = await issue({ owner_email: 'boss@lab.example.org', seats: 3 });
    const member = await admin('POST', `/licenses/${issued.license.id}/seats`, { email: 'postdoc@lab.example.org' });
    await activate(issued.seat.key, environmentBody('desktop', { display_name: 'Boss laptop' }));
    await activate(member.json.key, environmentBody('desktop', { display_name: 'Postdoc laptop' }));

    const owner = await signIn('boss@lab.example.org');
    const ownerPage = await (await SELF.fetch(`${BASE}/portal/environments`, { headers: as(owner) })).text();
    expect(ownerPage).toContain('Boss laptop');
    expect(ownerPage).toContain('Postdoc laptop');

    const postdoc = await signIn('postdoc@lab.example.org');
    const memberPage = await (await SELF.fetch(`${BASE}/portal/environments`, { headers: as(postdoc) })).text();
    expect(memberPage).toContain('Postdoc laptop');
    expect(memberPage).not.toContain('Boss laptop');

    // A member cannot touch the owner's seat.
    expect((await call('POST', `/portal/api/seats/${issued.seat.id}/rotate-key`, {}, as(postdoc))).status).toBe(404);
    expect((await call('POST', `/portal/api/seats/${issued.seat.id}/release`, {}, as(postdoc))).status).toBe(404);
  });

  it('renames and removes an environment; an owner skips the cooldown', async () => {
    const issued = await issue({ owner_email: 'renamer@lab.example.org', envs_per_seat: 3 });
    const a = await activate(issued.seat.key);
    const b = await activate(issued.seat.key);
    const cookie = await signIn('renamer@lab.example.org');
    const renamed = await call('POST', `/portal/api/environments/${a.json.environment.id}/rename`,
      { name: '<b>Bench</b> PC' }, as(cookie));
    expect(renamed.json.name).toBe('bBench/b PC');
    expect((await call('POST', `/portal/api/environments/${a.json.environment.id}/remove`, {}, as(cookie))).status).toBe(200);
    expect((await call('POST', `/portal/api/environments/${b.json.environment.id}/remove`, {}, as(cookie))).status).toBe(200);
    expect(await count('environments', "status = 'active'")).toBe(0);
  });

  it('a seat holder mints a token (shown once) and revokes it', async () => {
    const issued = await issue({ owner_email: 'tokens@lab.example.org' });
    const cookie = await signIn('tokens@lab.example.org');
    const minted = await call('POST', '/portal/api/tokens', { seat_id: issued.seat.id, scope: 'hpc', label: 'O2 jobs' },
      as(cookie));
    expect(minted.status).toBe(201);
    expect(minted.json.token).toMatch(/^PLXT1_/);
    const page = await (await SELF.fetch(`${BASE}/portal/tokens`, { headers: as(cookie) })).text();
    expect(page).toContain('O2 jobs');
    expect(page).not.toContain(minted.json.token);
    expect((await call('POST', `/portal/api/tokens/${minted.json.info.id}/revoke`, {}, as(cookie))).json.revoked).toBe(true);
  });

  it('reveals the holder\'s own seat key from the vault', async () => {
    const issued = await issue({ owner_email: 'vault@lab.example.org' });
    const cookie = await signIn('vault@lab.example.org');
    const reply = await call('POST', `/portal/api/seats/${issued.seat.id}/reveal-key`, {}, as(cookie));
    expect(reply.json.key).toBe(issued.seat.key);
  });

  it('downloads an offline licence for an uploaded report', async () => {
    const issued = await issue({ owner_email: 'offline@lab.example.org' });
    const cookie = await signIn('offline@lab.example.org');
    const reply = await call('POST', '/portal/api/offline', { seat_id: issued.seat.id, days: 60,
      report: { product: 'plexora', kind: 'cluster', display_name: 'Dark cluster', binding: '9'.repeat(64) } }, as(cookie));
    expect(reply.status).toBe(201);
    expect(reply.json.filename).toMatch(/\.plexora$/);
    const page = await (await SELF.fetch(`${BASE}/portal/offline`, { headers: as(cookie) })).text();
    expect(page).toContain('Dark cluster');
    expect(page).toContain('cannot be recalled');
  });

  it('invites a person who then gets a seat by accepting', async () => {
    const issued = await issue({ owner_email: 'inviter@lab.example.org', seats: 2 });
    const cookie = await signIn('inviter@lab.example.org');
    resetOutbox();
    const sent = await call('POST', `/portal/api/licenses/${issued.license.id}/invite`, { email: 'new@lab.example.org' },
      as(cookie));
    expect(sent.status).toBe(201);
    const secret = /invite\?t=([\w-]+)/.exec(outbox[0]!.text)![1]!;
    resetOutbox();
    const accepted = await call('POST', '/portal/invite', { t: secret });
    expect(accepted.status).toBe(200);
    expect(outbox.some((m) => /PLEX-/.test(m.text))).toBe(true);
    expect(await count('seat_assignments', "status = 'active'")).toBe(2);
    expect((await call('POST', '/portal/invite', { t: secret })).status).toBe(401);
  });

  it('the trial page needs to be opened from Plexora', async () => {
    const plain = await (await SELF.fetch(`${BASE}/portal/trial`)).text();
    expect(plain).toContain('Open this page from Plexora');
    const withFp = await (await SELF.fetch(`${BASE}/portal/trial?fp=${'a'.repeat(64)}`)).text();
    expect(withFp).toContain('name="fingerprint"');
  });
});
