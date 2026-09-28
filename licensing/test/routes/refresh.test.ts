import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { type CertificatePayload, signCertificate } from '../../src/certs';
import { activate, admin, claims, count, environmentBody, fingerprint, issue, post, pubkey, travel } from './helpers';

const DAY = 86400;

async function registered(overrides: Record<string, unknown> = {}, kind = 'desktop') {
  const issued = await issue(overrides);
  const body = environmentBody(kind, kind === 'desktop' ? {} : { delegation_pubkey: pubkey() });
  const reply = await activate(issued.seat.key, body);
  expect(reply.status).toBe(200);
  return { ...issued, certificate: reply.json.certificate as string, binding: body.binding as string,
    environmentId: reply.json.environment.id as string };
}

describe('POST /v1/refresh', () => {
  it('answers ok and writes NOTHING inside the refresh interval', async () => {
    const { certificate, binding } = await registered();
    const before = await fingerprint();
    const reply = await post('/v1/refresh', { certificate, binding });
    expect(reply.status).toBe(200);
    expect(reply.json.status).toBe('ok');
    expect(reply.json.server_time).toBeTypeOf('number');
    expect(await fingerprint()).toBe(before);
  });

  it('records last_refresh_at at most once per interval', async () => {
    const { certificate, binding } = await registered();
    travel(8 * DAY);
    await post('/v1/refresh', { certificate, binding });
    expect(await count('events', "kind = 'environment.refreshed'")).toBe(1);
    const before = await fingerprint();
    await post('/v1/refresh', { certificate, binding });
    expect(await fingerprint()).toBe(before);
  });

  it('renews a certificate close to its end', async () => {
    const { certificate, binding } = await registered();
    travel(75 * DAY);
    const reply = await post('/v1/refresh', { certificate, binding });
    expect(reply.json.status).toBe('renewed');
    const fresh = claims(reply.json.certificate);
    expect(fresh.expires_at).toBeGreaterThan(claims(certificate).expires_at);
    expect(fresh.env_binding).toBe(binding);
  });

  it('renews when the licence says something different', async () => {
    const { certificate, binding, license } = await registered();
    await admin('PATCH', `/licenses/${license.id}`, { entitlements: ['ai:evidence'] });
    const reply = await post('/v1/refresh', { certificate, binding });
    expect(reply.json.status).toBe('renewed');
    expect(claims(reply.json.certificate).entitlements).toEqual(['ai:evidence']);
  });

  it('carries the licence end date, which is what a user is shown', async () => {
    const { certificate, license } = await registered();
    const payload = claims(certificate);
    expect(payload.license_expires_at).toBe(license.expires_at);
    expect(payload.expires_at).toBeLessThan(license.expires_at);
  });

  it('renews when the licence is extended, so the date shown moves with it', async () => {
    const { certificate, binding, license } = await registered();
    await admin('PATCH', `/licenses/${license.id}`, { extend_days: 30 });
    const reply = await post('/v1/refresh', { certificate, binding });
    expect(reply.json.status).toBe('renewed');
    expect(claims(reply.json.certificate).license_expires_at).toBe(license.expires_at + 30 * DAY);
  });

  it('renews a certificate issued before the licence end date was in it', async () => {
    const { certificate, binding, license } = await registered();
    const { license_expires_at: _dropped, ...older } = claims(certificate);
    const legacy = await signCertificate(env, older as unknown as CertificatePayload);
    const reply = await post('/v1/refresh', { certificate: legacy, binding });
    expect(reply.json.status).toBe('renewed');
    expect(claims(reply.json.certificate).license_expires_at).toBe(license.expires_at);
  });

  it('never renews past the licence', async () => {
    const soon = Math.floor(Date.now() / 1000) + 10 * DAY;
    const { certificate, binding } = await registered({ expires_at: soon, days: undefined });
    expect(claims(certificate).expires_at).toBe(soon);
    const reply = await post('/v1/refresh', { certificate, binding });
    expect(reply.json.status).toBe('ok');
  });

  it('says revoked when the licence, seat or environment is gone', async () => {
    const a = await registered();
    await admin('POST', `/licenses/${a.license.id}/revoke`, {});
    expect((await post('/v1/refresh', { certificate: a.certificate, binding: a.binding })).json)
      .toMatchObject({ status: 'revoked', reason: 'license_revoked' });

    const b = await registered();
    await admin('POST', `/environments/${b.environmentId}/revoke`);
    expect((await post('/v1/refresh', { certificate: b.certificate, binding: b.binding })).json.status).toBe('revoked');

    const c = await registered();
    await admin('POST', `/seats/${c.seat.id}/release`);
    expect((await post('/v1/refresh', { certificate: c.certificate, binding: c.binding })).json.status).toBe('revoked');

    const d = await registered();
    await admin('PATCH', `/licenses/${d.license.id}`, { status: 'suspended' });
    expect((await post('/v1/refresh', { certificate: d.certificate, binding: d.binding })).json)
      .toMatchObject({ status: 'revoked', reason: 'license_suspended' });
  });

  it('refuses a certificate presented from another environment', async () => {
    const { certificate } = await registered();
    const reply = await post('/v1/refresh', { certificate, binding: 'f'.repeat(64) });
    expect(reply.status).toBe(403);
    expect(reply.json.error.code).toBe('environment_mismatch');
  });

  it('refuses a forgery and records it for review', async () => {
    const { certificate, binding } = await registered();
    const [prefix, kid, body, sig] = certificate.split('.');
    const reply = await post('/v1/refresh', { certificate: `${prefix}.${kid}.${body}.${sig!.slice(0, -4)}AAAA`, binding });
    expect(reply.status).toBe(403);
    expect(reply.json.error.code).toBe('forged_certificate');
    expect(await count('events', "kind = 'certificate.forged'")).toBe(1);
  });

  it('a certificate it cannot check is not called forged', async () => {
    const { certificate, binding } = await registered();
    const [, , body, sig] = certificate.split('.');
    const reply = await post('/v1/refresh', { certificate: `PLEXORA1.zz9.${body}.${sig}`, binding });
    expect(reply.status).toBe(503);
    expect(reply.json.error.code).toBe('signing_unavailable');
  });

  it('a job certificate is not refreshed', async () => {
    const { certificate, binding } = await registered({}, 'cluster');
    const job = await post('/v1/delegate', { certificate, binding, ttl_hours: 2 });
    const reply = await post('/v1/refresh', { certificate: job.json.certificate, binding });
    expect(reply.status).toBe(400);
  });

  it('records a reported clock rollback', async () => {
    const { certificate, binding } = await registered();
    await post('/v1/refresh', { certificate, binding, flags: ['clock_rollback'] });
    expect(await count('events', "kind = 'client.clock_rollback'")).toBe(1);
  });
});

describe('POST /v1/deactivate and the cooldown', () => {
  it('releases, and the first release is free but starts a cooldown', async () => {
    const issued = await issue();
    const one = environmentBody();
    const two = environmentBody();
    const a = await activate(issued.seat.key, one);
    const b = await activate(issued.seat.key, two);

    const first = await post('/v1/deactivate', { certificate: a.json.certificate, binding: one.binding });
    expect(first.status).toBe(200);
    expect(first.json.status).toBe('released');
    expect(first.json.next_allowed_at).toBeGreaterThan(Math.floor(Date.now() / 1000) + 47 * 3600);

    const second = await post('/v1/deactivate', { certificate: b.json.certificate, binding: two.binding });
    expect(second.status).toBe(429);
    expect(second.json.error.code).toBe('cooldown_active');
    expect(second.json.error.next_allowed_at).toBe(first.json.next_allowed_at);

    travel(49 * 3600);
    expect((await post('/v1/deactivate', { certificate: b.json.certificate, binding: two.binding })).status).toBe(200);
  });

  it('is idempotent', async () => {
    const issued = await issue();
    const body = environmentBody();
    const a = await activate(issued.seat.key, body);
    await post('/v1/deactivate', { certificate: a.json.certificate, binding: body.binding });
    const again = await post('/v1/deactivate', { certificate: a.json.certificate, binding: body.binding });
    expect(again.status).toBe(200);
    expect(again.json.status).toBe('released');
  });

  it('an environment unseen for 30 days can be released during a cooldown', async () => {
    const issued = await issue({ envs_per_seat: 3 });
    const one = environmentBody();
    const two = environmentBody();
    const three = environmentBody();
    const a = await activate(issued.seat.key, one);
    const stale = await activate(issued.seat.key, two);
    travel(31 * DAY);
    const b = await activate(issued.seat.key, three);
    expect((await post('/v1/deactivate', { certificate: b.json.certificate, binding: three.binding })).status).toBe(200);
    // Inside b's cooldown, but `stale` has not been seen for 31 days.
    const reply = await post('/v1/deactivate', { certificate: stale.json.certificate, binding: two.binding });
    expect(reply.status).toBe(200);
    expect(a.status).toBe(200);
  });

  it('refuses a release from another environment', async () => {
    const issued = await issue();
    const a = await activate(issued.seat.key);
    const reply = await post('/v1/deactivate', { certificate: a.json.certificate, binding: 'e'.repeat(64) });
    expect(reply.json.error.code).toBe('environment_mismatch');
  });

  it('a freed slot can be registered again', async () => {
    const issued = await issue({ envs_per_seat: 1 });
    const one = environmentBody();
    const a = await activate(issued.seat.key, one);
    expect((await activate(issued.seat.key)).status).toBe(409);
    await post('/v1/deactivate', { certificate: a.json.certificate, binding: one.binding });
    expect((await activate(issued.seat.key)).status).toBe(200);
    const rows = await env.LICENSE_DB.prepare('SELECT status FROM environments ORDER BY created_at').all();
    expect(rows.results.map((r) => r.status)).toEqual(['released', 'active']);
  });
});

describe('POST /v1/delegate', () => {
  it('gives a registered cluster a short, unbound job certificate', async () => {
    const { certificate, binding } = await registered({}, 'cluster');
    const reply = await post('/v1/delegate', { certificate, binding, ttl_hours: 12 });
    expect(reply.status).toBe(200);
    const job = claims(reply.json.certificate);
    expect(job).toMatchObject({ environment_type: 'job', env_binding: null, grace_days: 0, delegation_pubkey: null });
    expect(job.expires_at - job.issued_at).toBe(12 * 3600);
    expect(await count('environments')).toBe(1);
  });

  it('clamps the lifetime to seven days', async () => {
    const { certificate, binding } = await registered({}, 'cluster');
    const job = claims((await post('/v1/delegate', { certificate, binding, ttl_hours: 10000 })).json.certificate);
    expect(job.expires_at - job.issued_at).toBeLessThanOrEqual(7 * DAY);
  });

  it('refuses a desktop', async () => {
    const { certificate, binding } = await registered();
    expect((await post('/v1/delegate', { certificate, binding })).json.error.code).toBe('not_delegating');
  });
});
