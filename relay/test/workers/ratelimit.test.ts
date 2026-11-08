/**
 * The per-address limits with stubbed bindings: the Worker's `/v1/ssh` gets its stub through the env it is handed; a
 * room gets one put on its own instance's env before the page arrives (runInDurableObject).
 */
import { runInDurableObject } from 'cloudflare:test';
import { afterEach, describe, expect, inject, it } from 'vitest';

import type { Env } from '../../src/env';
import { TOO_MANY } from '../../src/protocol';
import { clientKey } from '../../src/ratelimit';
import { closeAll, newAuth, newRoom, node, open, page, PROTOCOL, roomStub } from './helpers';

const fixtures = inject('fixtures');
const ssh = `/v1/ssh?host=127.0.0.1&port=${fixtures.echoPort}`;

afterEach(closeAll);

/** A Rate Limiting binding allowing `limit` calls per key, recording the keys it was asked about. */
function stubLimiter(limit: number) {
  const counts = new Map<string, number>();
  const keys: string[] = [];
  const limiter = {
    async limit({ key }: { key: string }) {
      keys.push(key);
      const seen = (counts.get(key) ?? 0) + 1;
      counts.set(key, seen);
      return { success: seen <= limit };
    },
  } as RateLimit;
  return { limiter, keys };
}

const from = (address: string) => ({ 'CF-Connecting-IP': address });

/** Give the room's live instance a different env (its own copy of the bindings). */
async function roomEnv(room: string, vars: Partial<Env>): Promise<void> {
  await runInDurableObject(roomStub(room), (instance) => {
    const holder = instance as unknown as { env: Env };
    holder.env = { ...holder.env, ...vars };
  });
}

describe('client key', () => {
  it('is the address, an IPv6 one by its /64', () => {
    const request = (address?: string) => new Request('https://relay.test/', { headers: address ? from(address) : {} });
    expect(clientKey(request('203.0.113.9'))).toBe('203.0.113.9');
    expect(clientKey(request('2001:db8:1:2:aaaa::1'))).toBe('2001:db8:1:2::/64');
    expect(clientKey(request('2001:DB8:1:2:ffff:ffff:ffff:ffff'))).toBe('2001:db8:1:2::/64');
    expect(clientKey(request())).toBe('unknown');
  });
});

describe('/v1/ssh rate limit', () => {
  it('refuses past the limit with 4429 and the plain reason, per address', async () => {
    const { limiter, keys } = stubLimiter(2);
    const vars = { SSH_LIMIT: limiter };
    for (let i = 0; i < 2; i += 1) {
      const client = await open(ssh, { vars, protocols: [PROTOCOL], headers: from('203.0.113.7') });
      client.send(Uint8Array.from([i]));
      expect(Array.from(await client.binary())).toEqual([i]);
      client.close();
    }
    const third = await open(ssh, { vars, protocols: [PROTOCOL], headers: from('203.0.113.7') });
    expect(third.protocol).toBe(PROTOCOL);
    expect(await third.closed).toEqual({ code: 4429, reason: TOO_MANY });
    // Another address has its own count.
    const other = await open(ssh, { vars, headers: from('198.51.100.1') });
    other.send(Uint8Array.from([9]));
    expect(Array.from(await other.binary())).toEqual([9]);
    expect(keys).toEqual(['203.0.113.7', '203.0.113.7', '203.0.113.7', '198.51.100.1']);
  });

  it('does not count a request refused before it could open anything', async () => {
    const { limiter, keys } = stubLimiter(1);
    const vars = { SSH_LIMIT: limiter };
    expect((await (await open('/v1/ssh?host=127.1&port=22', { vars })).closed).code).toBe(4400);
    expect((await (await open(`/v1/ssh?host=example.org&port=2222`, { vars })).closed).code).toBe(4403);
    expect((await (await open(ssh, { vars, origin: 'https://evil.example' })).closed).code).toBe(4403);
    expect(keys).toEqual([]);
  });

  it('no binding, or a binding that fails, is no limit', async () => {
    const failing = { limit: async () => { throw new Error('limiter down'); } } as unknown as RateLimit;
    for (const vars of [{ SSH_LIMIT: undefined }, { SSH_LIMIT: failing }]) {
      for (let i = 0; i < 3; i += 1) {
        const client = await open(ssh, { vars });
        client.send(Uint8Array.from([i]));
        expect(Array.from(await client.binary())).toEqual([i]);
        client.close();
      }
    }
  });
});

describe('room creation rate limit', () => {
  it('counts a page creating a room, refuses past the limit with 4429, and never counts joining', async () => {
    const { limiter, keys } = stubLimiter(1);
    const first = newRoom();
    const second = newRoom();
    const auth = newAuth();
    await roomEnv(first, { ROOM_CREATE_LIMIT: limiter });
    await roomEnv(second, { ROOM_CREATE_LIMIT: limiter });

    const creator = await page(first, auth, { headers: from('203.0.113.7') });
    await creator.presenceIs({ node: false, pages: 1 });
    // Joining the room it made: a second page and the node are not counted.
    const joiner = await page(first, auth, { headers: from('203.0.113.7') });
    await joiner.presenceIs({ node: false, pages: 2 });
    const n = await node(first, auth, { headers: from('203.0.113.7') });
    await n.presenceIs({ node: true, pages: 2 });
    expect(keys).toEqual(['203.0.113.7']);

    const refused = await page(second, auth, { headers: from('203.0.113.7') });
    expect(refused.protocol).toBe(PROTOCOL);
    expect(await refused.closed).toEqual({ code: 4429, reason: TOO_MANY });
    // Refused, so not created: a node still finds no room.
    expect((await (await node(second, auth)).closed).code).toBe(4404);
    // Another address may create it.
    const elsewhere = await page(second, auth, { headers: from('198.51.100.1') });
    await elsewhere.presenceIs({ node: false, pages: 1 });
  });

  it('no binding, or a binding that fails, is no limit', async () => {
    const failing = { limit: async () => { throw new Error('limiter down'); } } as unknown as RateLimit;
    for (const vars of [{ ROOM_CREATE_LIMIT: undefined }, { ROOM_CREATE_LIMIT: failing }]) {
      const room = newRoom();
      await roomEnv(room, vars);
      const p = await page(room, newAuth());
      await p.presenceIs({ node: false, pages: 1 });
    }
  });
});
