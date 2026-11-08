import { evictDurableObject, runDurableObjectAlarm, runInDurableObject } from 'cloudflare:test';
import { afterEach, describe, expect, it } from 'vitest';

import { MAX_PAGES } from '../../src/room';
import { bytes, closeAll, newAuth, newRoom, node, open, page, PROTOCOL, refusal, roomStub } from './helpers';

afterEach(closeAll);

/** A room with one page and one node, both past their first presence frames. */
async function joined() {
  const room = newRoom();
  const auth = newAuth();
  const p = await page(room, auth);
  await p.presenceIs({ node: false, pages: 1 });
  const n = await node(room, auth);
  await n.presenceIs({ node: true, pages: 1 });
  await p.presenceIs({ node: true, pages: 1 });
  return { room, auth, p, n };
}

/** The pid a page's frames carry, learnt by sending one through. */
async function pidOf(p: Awaited<ReturnType<typeof page>>, n: Awaited<ReturnType<typeof node>>): Promise<Uint8Array> {
  p.send(bytes(0xee));
  const framed = await n.binary();
  expect(framed.length).toBe(9);
  expect(framed[8]).toBe(0xee);
  return framed.slice(0, 8);
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

describe('room creation and auth', () => {
  it('a page creates the room, answered with exactly plexora-relay.v1, and hears presence', async () => {
    const room = newRoom();
    const p = await page(room, newAuth(), { protocols: undefined });
    expect(p.protocol).toBe(PROTOCOL);
    expect(await p.presence()).toEqual({ t: 'presence', node: false, pages: 1 });
  });

  it('extra subprotocols are ignored and only ours is answered', async () => {
    const auth = newAuth();
    const p = await page(newRoom(), auth, { protocols: ['something.else', PROTOCOL, `a.${auth}`] });
    expect(p.protocol).toBe(PROTOCOL);
    expect(await p.presence()).toEqual({ t: 'presence', node: false, pages: 1 });
  });

  it('a node cannot create a room: 4404', async () => {
    const n = await node(newRoom(), newAuth());
    expect(n.protocol).toBe(PROTOCOL);
    expect(await refusal(n)).toEqual({ code: 4404, reason: 'no such room' });
  });

  it('a node joins with the same auth; both sides hear presence', async () => {
    const { p, n } = await joined();
    expect(p.protocol).toBe(PROTOCOL);
    expect(n.protocol).toBe(PROTOCOL);
  });

  it('a different auth gets 4401, for a node and for a page', async () => {
    const room = newRoom();
    const auth = newAuth();
    const first = await page(room, auth);
    await first.presence();
    expect(await refusal(await node(room, newAuth()))).toEqual({ code: 4401, reason: 'wrong room auth' });
    expect(await refusal(await page(room, newAuth()))).toEqual({ code: 4401, reason: 'wrong room auth' });
    // The room is unchanged: its own auth still works.
    const n = await node(room, auth);
    await n.presenceIs({ node: true, pages: 1 });
  });

  it('stores sha256(roomAuth), not the auth', async () => {
    const room = newRoom();
    const auth = newAuth();
    await (await page(room, auth)).presence();
    const stored = await runInDurableObject(roomStub(room), (_instance, state) => state.storage.kv.get<ArrayBuffer>('auth'));
    const expected = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(auth)));
    expect(new Uint8Array(stored as ArrayBuffer)).toEqual(expected);
  });

  it('refuses a bad request with 4400', async () => {
    const room = newRoom();
    const auth = newAuth();
    const cases: Array<[string, Parameters<typeof open>[1]]> = [
      [`/v1/rooms/${room}?role=page`, { protocols: [`a.${auth}`] }], // no plexora-relay.v1
      [`/v1/rooms/${room}?role=page`, { protocols: [PROTOCOL] }], // no auth
      [`/v1/rooms/${room}?role=page`, { protocols: [PROTOCOL, 'a.short'] }], // auth not 43 base64url
      [`/v1/rooms/${room}?role=page`, { protocols: [PROTOCOL, `a.${auth}`, `a.${newAuth()}`] }], // two auths
      [`/v1/rooms/${room}?role=watcher`, { protocols: [PROTOCOL, `a.${auth}`] }], // unknown role
      [`/v1/rooms/${room}`, { protocols: [PROTOCOL, `a.${auth}`] }], // no role
      [`/v1/rooms/tooshort?role=page`, { protocols: [PROTOCOL, `a.${auth}`] }], // room id under 22
      [`/v1/rooms/${'x'.repeat(65)}?role=page`, { protocols: [PROTOCOL, `a.${auth}`] }], // over 64
      [`/v1/rooms/${room.slice(0, 21)}!?role=page`, { protocols: [PROTOCOL, `a.${auth}`] }], // bad character
    ];
    for (const [path, options] of cases) {
      const closed = await refusal(await open(path, options));
      expect(closed.code, path).toBe(4400);
    }
  });

  it('a page needs an allowed Origin (4403); a node is not asked for one', async () => {
    const room = newRoom();
    const auth = newAuth();
    for (const origin of [null, 'https://evil.example', 'https://viewer.plexoraapp.com.evil.example', 'http://viewer.plexoraapp.com']) {
      const closed = await refusal(await page(room, auth, { origin }));
      expect(closed.code, String(origin)).toBe(4403);
    }
    const local = await page(room, auth, { origin: 'http://localhost:5173' });
    await local.presenceIs({ node: false, pages: 1 });
    const n = await node(room, auth, { origin: 'https://anything.example' });
    await n.presenceIs({ node: true, pages: 1 });
  });
});

describe('forwarding', () => {
  it('page -> node gains the page pid; node -> page with that pid loses it', async () => {
    const { p, n } = await joined();
    p.send(bytes(1, 2, 3));
    const framed = await n.binary();
    expect(framed.length).toBe(11);
    const pid = framed.slice(0, 8);
    expect(Array.from(framed.slice(8))).toEqual([1, 2, 3]);
    expect(pid.some((b) => b !== 0)).toBe(true);

    n.send(concat(pid, bytes(9, 8, 7)));
    expect(Array.from(await p.binary())).toEqual([9, 8, 7]);
  });

  it('each page has its own pid, and a node frame reaches only that page', async () => {
    const { room, auth, p, n } = await joined();
    const q = await page(room, auth);
    await n.presenceIs({ node: true, pages: 2 });
    const pidP = await pidOf(p, n);
    const pidQ = await pidOf(q, n);
    expect(pidP).not.toEqual(pidQ);

    n.send(concat(pidQ, bytes(42)));
    expect(Array.from(await q.binary())).toEqual([42]);
    await p.noBinary();
  });

  it('an all-zero pid goes to every page', async () => {
    const { room, auth, p, n } = await joined();
    const q = await page(room, auth);
    await n.presenceIs({ node: true, pages: 2 });
    n.send(concat(new Uint8Array(8), bytes(5, 5)));
    expect(Array.from(await p.binary())).toEqual([5, 5]);
    expect(Array.from(await q.binary())).toEqual([5, 5]);
  });

  it('a frame for a pid that is not there, or shorter than a pid, is dropped', async () => {
    const { p, n } = await joined();
    n.send(concat(bytes(1, 2, 3, 4, 5, 6, 7, 8), bytes(1)));
    n.send(bytes(1, 2, 3));
    await p.noBinary();
    const pid = await pidOf(p, n);
    n.send(concat(pid, bytes(77)));
    expect(Array.from(await p.binary())).toEqual([77]);
  });

  it('text frames are not forwarded; ping is answered pong', async () => {
    const { p, n } = await joined();
    p.send('hello');
    n.send('hello');
    p.send('ping');
    n.send('ping');
    expect(await p.text('pong')).toBe('pong');
    expect(await n.text('pong')).toBe('pong');
    await n.noBinary();
    await p.noBinary();
    expect(n.queue.filter((m) => m === 'hello')).toEqual([]);
    expect(p.queue.filter((m) => m === 'hello')).toEqual([]);
  });

  it('a page frame with no node attached is dropped', async () => {
    const room = newRoom();
    const auth = newAuth();
    const p = await page(room, auth);
    await p.presence();
    p.send(bytes(1));
    const n = await node(room, auth);
    await n.presenceIs({ node: true, pages: 1 });
    await n.noBinary();
  });

  it('carries a 1 MiB frame whole, both ways', async () => {
    const { p, n } = await joined();
    const big = crypto.getRandomValues(new Uint8Array(65536));
    const payload = new Uint8Array(1024 * 1024 + 64);
    for (let at = 0; at < payload.length; at += big.length) payload.set(big.subarray(0, Math.min(big.length, payload.length - at)), at);
    p.send(payload);
    const framed = await n.binary(5000);
    expect(framed.length).toBe(payload.length + 8);
    expect(framed.slice(8)).toEqual(payload);
    n.send(framed);
    expect(await p.binary(5000)).toEqual(payload);
  });
});

describe('presence, capacity and replacement', () => {
  it('a page leaving is told to everyone else', async () => {
    const { room, auth, p, n } = await joined();
    const q = await page(room, auth);
    await n.presenceIs({ node: true, pages: 2 });
    q.close();
    await n.presenceIs({ node: true, pages: 1 });
    await p.presenceIs({ node: true, pages: 1 });
  });

  it('the node leaving is told to the pages', async () => {
    const { p, n } = await joined();
    n.close();
    await p.presenceIs({ node: false, pages: 1 });
  });

  it('a second node replaces the first with 4000 "replaced"', async () => {
    const { room, auth, p, n } = await joined();
    const n2 = await node(room, auth);
    expect(await n.closed).toEqual({ code: 4000, reason: 'replaced' });
    await n2.presenceIs({ node: true, pages: 1 });
    await p.presenceIs({ node: true, pages: 1 });
    p.send(bytes(3));
    const framed = await n2.binary();
    expect(framed[8]).toBe(3);
  });

  it(`at most ${MAX_PAGES} pages: the next gets 4429, and a place freed is taken again`, async () => {
    const room = newRoom();
    const auth = newAuth();
    const pages = [];
    for (let i = 0; i < MAX_PAGES; i += 1) pages.push(await page(room, auth));
    await pages[0]?.presenceIs({ node: false, pages: MAX_PAGES });
    const ninth = await page(room, auth);
    expect((await refusal(ninth)).code).toBe(4429);
    pages[3]?.close();
    await pages[0]?.presenceIs({ node: false, pages: MAX_PAGES - 1 });
    const again = await page(room, auth);
    await again.presenceIs({ node: false, pages: MAX_PAGES });
  });
});

describe('hibernation and expiry', () => {
  it('sockets, pids and the auth survive the object being evicted', async () => {
    const { room, auth, p, n } = await joined();
    const pid = await pidOf(p, n);
    await evictDurableObject(roomStub(room));
    // ping is answered by the runtime, without the object.
    p.send('ping');
    expect(await p.text('pong')).toBe('pong');
    // Forwarding wakes it, with the pid read back from the socket's attachment.
    p.send(bytes(4, 4));
    expect(await n.binary()).toEqual(concat(pid, bytes(4, 4)));
    n.send(concat(pid, bytes(6)));
    expect(Array.from(await p.binary())).toEqual([6]);
    // And the stored auth still decides.
    expect((await refusal(await node(room, newAuth()))).code).toBe(4401);
  });

  it('every accept sets the idle alarm; it keeps a room with sockets and deletes an empty one', async () => {
    const { room, auth, p, n } = await joined();
    const stub = roomStub(room);
    const alarm = await runInDurableObject(stub, (_instance, state) => state.storage.getAlarm());
    expect(alarm).not.toBeNull();
    expect((alarm as number) - Date.now()).toBeGreaterThan(6.9 * 86_400_000);

    // Sockets open: the room stays and the alarm is set again.
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await runInDurableObject(stub, (_instance, state) => state.storage.kv.get('auth'))).toBeDefined();
    expect(await runInDurableObject(stub, (_instance, state) => state.storage.getAlarm())).not.toBeNull();

    p.close();
    n.close();
    await n.closed;
    await p.closed;
    // Wait until the room has seen both closes (it re-arms the alarm on each).
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await runInDurableObject(stub, (_instance, state) => state.storage.kv.get('auth'))).toBeUndefined();
    expect((await refusal(await node(room, auth))).code).toBe(4404);
  });
});
