import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { activate, admin, claims, count, environmentBody, issue, pubkey, travel } from './helpers';

async function tokenFor(scope: string, extra: Record<string, unknown> = {}) {
  const issued = await issue();
  const reply = await admin('POST', `/seats/${issued.seat.id}/tokens`, { scope, label: `${scope} test`, ...extra });
  expect(reply.status).toBe(201);
  return { ...issued, token: reply.json.token as string, tokenId: reply.json.info.id as string };
}

describe('licence tokens', () => {
  it('are shown once and stored only as a hash', async () => {
    const { token } = await tokenFor('interactive');
    expect(token).toMatch(/^PLXT1_[A-Za-z0-9_-]{43}$/);
    const rows = await env.LICENSE_DB.prepare('SELECT * FROM license_tokens').all();
    expect(JSON.stringify(rows.results)).not.toContain(token);
  });

  it('an hpc token registers a cluster, and records its use', async () => {
    const { token, tokenId } = await tokenFor('hpc');
    const reply = await activate(token, environmentBody('cluster', { delegation_pubkey: pubkey() }));
    expect(reply.status).toBe(200);
    const row = await env.LICENSE_DB.prepare('SELECT use_count, last_used_at FROM license_tokens WHERE id = ?1')
      .bind(tokenId).first<{ use_count: number; last_used_at: number }>();
    expect(row!.use_count).toBe(1);
    expect(row!.last_used_at).toBeGreaterThan(0);
  });

  it('scopes limit what may be registered', async () => {
    const { token } = await tokenFor('hpc');
    const reply = await activate(token, environmentBody('desktop'));
    expect(reply.status).toBe(403);
    expect(reply.json.error.code).toBe('scope_not_allowed');
  });

  it('a ci token never registers anything', async () => {
    const { token } = await tokenFor('ci');
    for (let i = 0; i < 3; i += 1) {
      const reply = await activate(token, environmentBody());
      expect(reply.status).toBe(200);
      const payload = claims(reply.json.certificate);
      expect(payload.environment_type).toBe('job');
      expect(payload.env_binding).toBeNull();
      expect(payload.expires_at - payload.issued_at).toBeLessThanOrEqual(24 * 3600);
    }
    expect(await count('environments')).toBe(0);
    expect(await count('license_tokens', 'use_count = 3')).toBe(1);
  });

  it('a revoked token is refused, and its use recorded', async () => {
    const { token, tokenId } = await tokenFor('automation');
    await admin('POST', `/tokens/${tokenId}/revoke`);
    const reply = await activate(token);
    expect(reply.json.error.code).toBe('credential_revoked');
    expect(await count('events', "kind = 'token.revoked_use'")).toBe(1);
  });

  it('an expired token is refused', async () => {
    const { token } = await tokenFor('automation', { ttl_days: 1 });
    travel(2 * 86400);
    expect((await activate(token)).json.error.code).toBe('credential_expired');
  });

  it('never outlives its licence, nor TOKEN_MAX_TTL_DAYS', async () => {
    const issued = await issue({ days: 30 });
    const reply = await admin('POST', `/seats/${issued.seat.id}/tokens`, { scope: 'ci', label: 'x', ttl_days: 9999 });
    expect(reply.json.info.expires_at).toBeLessThanOrEqual(issued.license.expires_at);
  });

  it('releasing a seat revokes its tokens', async () => {
    const { token, seat } = await tokenFor('automation');
    await admin('POST', `/seats/${seat.id}/release`);
    expect((await activate(token)).status).toBe(403);
  });
});
