import { describe, expect, it } from 'vitest';

import { outbox } from '../../src/email';
import { activate, claims, count, post } from './helpers';

const fp = (n: number) => n.toString(16).padStart(64, '0');

describe('POST /v1/trial/start', () => {
  it('issues a 30-day, no-grace trial and emails the key', async () => {
    const reply = await post('/v1/trial/start', { email: 'Ada@Lab.example.org', fingerprint: fp(1) });
    expect(reply.status).toBe(202);
    expect(reply.json.status).toBe('sent');
    expect(JSON.stringify(reply.json)).not.toMatch(/PLEX-/);
    expect(outbox).toHaveLength(1);
    const key = /PLEX-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}/.exec(outbox[0]!.text)![0];
    const activated = await activate(key);
    expect(activated.status).toBe(200);
    const payload = claims(activated.json.certificate);
    expect(payload.trial).toBe(true);
    expect(payload.grace_days).toBe(0);
    expect(payload.expires_at - payload.issued_at).toBeLessThanOrEqual(30 * 86400);
  });

  it('one per mailbox, however it is spelled', async () => {
    expect((await post('/v1/trial/start', { email: 'someone@gmail.com', fingerprint: fp(2) })).status).toBe(202);
    const again = await post('/v1/trial/start', { email: 'Some.One+plexora@googlemail.com', fingerprint: fp(3) });
    expect(again.status).toBe(409);
    expect(again.json.error.code).toBe('trial_already_issued');
  });

  it('one per mailbox under a race', async () => {
    const replies = await Promise.all(Array.from({ length: 5 }, (_, i) =>
      post('/v1/trial/start', { email: 'racer@lab.example.org', fingerprint: fp(10 + i) })));
    expect(replies.filter((r) => r.status === 202)).toHaveLength(1);
    expect(await count('trials')).toBe(1);
    expect(await count('licenses', 'is_trial = 1')).toBe(1);
  });

  it('at most two per machine', async () => {
    expect((await post('/v1/trial/start', { email: 'a1@lab.example.org', fingerprint: fp(20) })).status).toBe(202);
    expect((await post('/v1/trial/start', { email: 'a2@lab.example.org', fingerprint: fp(20) })).status).toBe(202);
    const third = await post('/v1/trial/start', { email: 'a3@lab.example.org', fingerprint: fp(20) });
    expect(third.json.error.code).toBe('trial_machine_limit');
    expect(await count('events', "kind = 'trial.refused_machine'")).toBe(1);
  });

  it('needs the fingerprint Plexora sends, and a real address', async () => {
    expect((await post('/v1/trial/start', { email: 'x@lab.example.org' })).status).toBe(400);
    expect((await post('/v1/trial/start', { email: 'nope', fingerprint: fp(30) })).status).toBe(400);
    const disposable = await post('/v1/trial/start', { email: 'x@mailinator.com', fingerprint: fp(31) });
    expect(disposable.json.error.code).toBe('trial_not_available');
  });

  it('is rate-limited per address', async () => {
    const replies = [];
    for (let i = 0; i < 7; i += 1) {
      replies.push(await post('/v1/trial/start', { email: `burst${i}@lab.example.org`, fingerprint: fp(100 + i) },
        { 'CF-Connecting-IP': '198.51.100.9' }));
    }
    expect(replies.filter((r) => r.status === 429).length).toBeGreaterThan(0);
    expect(replies.find((r) => r.status === 429)!.headers.get('Retry-After')).toBeTruthy();
  });
});
