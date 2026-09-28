import { describe, expect, it } from 'vitest';

import { activate, admin, binding, claims, count, environmentBody, issue, post, pubkey } from './helpers';

describe('POST /v1/activate', () => {
  it('turns a seat key into a bound certificate for this environment', async () => {
    const { seat, license } = await issue();
    const env = environmentBody('desktop', { display_name: 'My laptop' });
    const reply = await activate(seat.key, env);
    expect(reply.status).toBe(200);
    const payload = claims(reply.json.certificate);
    expect(payload).toMatchObject({
      v: 1, aud: 'plexora', kid: 'pxt', plan: 'paid', trial: false, use_class: 'academic',
      entitlements: ['ai'], environment_type: 'desktop', env_binding: env.binding, delegation_pubkey: null,
      license_id: license.id, seat_id: seat.id, grace_days: 14, offline_until: null,
    });
    expect(payload.expires_at - payload.issued_at).toBeLessThanOrEqual(90 * 86400);
    expect(reply.json.environment).toMatchObject({ name: 'My laptop', type: 'desktop' });
    expect(await count('environments', "status = 'active'")).toBe(1);
  });

  it('accepts a seat key typed loosely', async () => {
    const { seat } = await issue();
    const loose = seat.key.toLowerCase().replace(/-/g, ' ');
    expect((await activate(loose)).status).toBe(200);
  });

  it('re-activating the same environment returns the same registration', async () => {
    const { seat } = await issue();
    const env = environmentBody();
    const first = await activate(seat.key, env);
    const second = await activate(seat.key, env);
    expect(second.status).toBe(200);
    expect(second.json.environment.id).toBe(first.json.environment.id);
    expect(await count('environments')).toBe(1);
  });

  it('caps environments per seat, and says which are in use', async () => {
    const { seat } = await issue();
    expect((await activate(seat.key)).status).toBe(200);
    expect((await activate(seat.key, environmentBody('cluster'))).status).toBe(200);
    const third = await activate(seat.key);
    expect(third.status).toBe(409);
    expect(third.json.error.code).toBe('seat_env_limit');
    expect(third.json.error.details.allowed).toBe(2);
    expect(third.json.error.details.active).toHaveLength(2);
  });

  it('the cap holds under a race for the last slot', async () => {
    const { seat } = await issue({ envs_per_seat: 1 });
    const replies = await Promise.all(Array.from({ length: 6 }, () => activate(seat.key)));
    expect(replies.filter((r) => r.status === 200)).toHaveLength(1);
    expect(replies.filter((r) => r.status === 409)).toHaveLength(5);
    expect(await count('environments', "status = 'active'")).toBe(1);
  });

  it('many nodes of one cluster activating at once are one environment', async () => {
    const { seat } = await issue({ envs_per_seat: 1 });
    const env = environmentBody('cluster', { delegation_pubkey: pubkey() });
    const replies = await Promise.all(Array.from({ length: 6 }, () => activate(seat.key, env)));
    expect(replies.every((r) => r.status === 200)).toBe(true);
    expect(new Set(replies.map((r) => r.json.environment.id)).size).toBe(1);
    expect(await count('environments')).toBe(1);
  });

  it('a cluster certificate carries its delegation key; a desktop one never does', async () => {
    const { seat } = await issue();
    const cluster = await activate(seat.key, environmentBody('cluster', { delegation_pubkey: pubkey(3) }));
    expect(claims(cluster.json.certificate).delegation_pubkey).toBe(pubkey(3));
    const desktop = await activate(seat.key, environmentBody('desktop', { delegation_pubkey: pubkey(4) }));
    expect(claims(desktop.json.certificate).delegation_pubkey).toBeNull();
  });

  it('refuses what it should, with the right codes', async () => {
    const { seat, license } = await issue();
    expect((await activate('PLEX-AAAA-BBBB-CCCC-DDDD')).json.error.code).toBe('invalid_credential');
    expect((await activate('hello')).json.error.code).toBe('invalid_credential');
    expect((await post('/v1/activate', { credential: seat.key })).json.error.code).toBe('invalid_request');
    expect((await activate(seat.key, { kind: 'toaster', binding: binding() })).status).toBe(400);
    expect((await activate(seat.key, { kind: 'desktop', binding: 'nothex' })).status).toBe(400);
    expect((await post('/v1/activate', 'not json')).status).toBe(400);

    await admin('PATCH', `/licenses/${license.id}`, { status: 'suspended' });
    expect((await activate(seat.key)).json.error.code).toBe('license_suspended');
    await admin('PATCH', `/licenses/${license.id}`, { status: 'active' });
    await admin('POST', `/licenses/${license.id}/revoke`, { reason: 'test' });
    expect((await activate(seat.key)).json.error.code).toBe('license_revoked');
  });

  it('a released seat\'s key stops working', async () => {
    const { seat } = await issue();
    await admin('POST', `/seats/${seat.id}/release`);
    const reply = await activate(seat.key);
    expect(reply.status).toBe(403);
    expect(reply.json.error.code).toBe('credential_revoked');
  });

  it('an expired licence cannot activate', async () => {
    const { seat, license } = await issue();
    await admin('PATCH', `/licenses/${license.id}`, { expires_at: Math.floor(Date.now() / 1000) - 10 });
    expect((await activate(seat.key)).json.error.code).toBe('license_expired');
  });

  it('entitlements come from the licence, or the seat override', async () => {
    const { seat, license } = await issue({ entitlements: ['ai:evidence'] });
    expect(claims((await activate(seat.key)).json.certificate).entitlements).toEqual(['ai:evidence']);
    await admin('PATCH', `/licenses/${license.id}`, { entitlements: [] });
    expect(claims((await activate(seat.key)).json.certificate).entitlements).toEqual([]);
  });

  it('never stores the binding itself', async () => {
    const { seat } = await issue();
    const env = environmentBody();
    await activate(seat.key, env);
    const { env: bindings } = await import('cloudflare:test');
    const row = await bindings.LICENSE_DB.prepare('SELECT env_binding_hash FROM environments').first<{ env_binding_hash: string }>();
    expect(row!.env_binding_hash).not.toBe(env.binding);
    expect(row!.env_binding_hash).toMatch(/^[0-9a-f]{64}$/);
  });
});
