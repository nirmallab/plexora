import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { runMaintenance } from '../../src/cron';
import { outbox } from '../../src/email';
import { activate, admin, count, environmentBody, issue, post } from './helpers';

const DAY = 86400;
const now = () => Math.floor(Date.now() / 1000);

describe('daily maintenance', () => {
  it('marks a licence expired only after its grace', async () => {
    const { license } = await issue({ expires_at: now() + DAY, days: undefined, grace_days: 14 });
    expect((await runMaintenance(env, now() + 10 * DAY, 'expire')).expire).toBe(0);
    expect((await runMaintenance(env, now() + 16 * DAY, 'expire')).expire).toBe(1);
    expect((await admin('GET', `/licenses/${license.id}`)).json.license.status).toBe('expired');
  });

  it('sends each reminder once, the most urgent that applies', async () => {
    await issue({ owner_email: 'remind@lab.example.org', expires_at: now() + 6 * DAY + 3600, days: undefined });
    outbox.length = 0;
    await runMaintenance(env, now(), 'reminders');
    await runMaintenance(env, now() + 3600, 'reminders');
    const mails = outbox.filter((m) => m.to === 'remind@lab.example.org');
    expect(mails).toHaveLength(1);
    expect(mails[0]!.text).toContain('stays exactly as it is');
    expect(await count('notices', "kind = 'expiry_7'")).toBe(1);
  });

  it('releases online environments unseen for 180 days, never offline ones', async () => {
    const { seat } = await issue();
    await activate(seat.key);
    await admin('POST', `/seats/${seat.id}/offline`, { report: { product: 'plexora', binding: '7'.repeat(64) } });
    expect((await runMaintenance(env, now() + 181 * DAY, 'idle')).idle).toBe(1);
    expect(await count('environments', "status = 'active' AND registration_kind = 'offline'")).toBe(1);
  });

  it('prunes sessions, links and rate-limit windows, and old audit payloads', async () => {
    await post('/portal/login', { email: 'x@lab.example.org' });
    await env.LICENSE_DB.prepare(
      `INSERT INTO events (at, actor, kind, payload) VALUES (?1, 'test', 'old', '{"a":1}')`).bind(now() - 800 * DAY).run();
    const report = await runMaintenance(env, now() + 2 * DAY, 'prune');
    expect((report.prune as Record<string, number>).rate_limits).toBeGreaterThan(0);
    expect(await count('events', "kind = 'old' AND payload IS NULL")).toBe(1);
  });

  it('backs up gzipped JSON to R2, without sessions', async () => {
    await issue();
    const report = (await runMaintenance(env, now(), 'backup')).backup as Record<string, unknown>;
    const object = await env.BACKUPS.get(String(report.key));
    expect(object).not.toBeNull();
    const text = await new Response(object!.body.pipeThrough(new DecompressionStream('gzip'))).text();
    const snapshot = JSON.parse(text);
    expect(snapshot.tables.licenses).toHaveLength(1);
    expect(snapshot.tables.sessions).toBeUndefined();
    expect(snapshot.tables.rate_limits).toBeUndefined();
  });

  it('prunes dailies past their retention', async () => {
    await env.BACKUPS.put('backups/daily/2000-01-01.json.gz', 'old');
    const report = (await runMaintenance(env, now(), 'backup')).backup as Record<string, unknown>;
    expect(report.pruned).toBe(1);
  });

  it('raises signals for review, once', async () => {
    const { seat } = await issue({ envs_per_seat: 1 });
    // Churn: register and release repeatedly (as an owner would, without cooldown).
    for (let i = 0; i < 4; i += 1) {
      const reply = await activate(seat.key, environmentBody());
      await admin('POST', `/environments/${reply.json.environment.id}/release`);
    }
    const first = await runMaintenance(env, now(), 'signals');
    expect(first.signals).toBeGreaterThan(0);
    expect(await count('signals', "kind = 'env_overuse'")).toBe(1);
    const again = await runMaintenance(env, now(), 'signals');
    expect(again.signals).toBe(0);
  });

  it('one step failing does not stop the others', async () => {
    const broken = { ...env, BACKUPS: { put: () => { throw new Error('r2 down'); } } } as unknown as typeof env;
    const report = await runMaintenance(broken, now());
    expect((report.backup as { error: string }).error).toContain('r2 down');
    expect(report.expire).toBeTypeOf('number');
  });
});
