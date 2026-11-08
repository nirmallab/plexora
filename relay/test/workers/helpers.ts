/**
 * Socket helpers for the Workers tests: open a relay socket through the Worker's own fetch handler (with the test env,
 * or that env with some vars changed) and read what comes back in order, by kind.
 */
import { env as testEnv } from 'cloudflare:test';
import { expect } from 'vitest';

import type { Env } from '../../src/env';
import worker from '../../src/index';

export const SITE = 'https://viewer.plexoraapp.com';
export const PROTOCOL = 'plexora-relay.v1';
export const env = testEnv as unknown as Env;

export function b64url(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** A fresh roomAuth: 32 random bytes as 43 characters of base64url. */
export function newAuth(): string {
  return b64url(crypto.getRandomValues(new Uint8Array(32)));
}

/** A fresh room id (22 characters, the size of 16 random bytes). */
export function newRoom(): string {
  return b64url(crypto.getRandomValues(new Uint8Array(16)));
}

export function bytes(...values: number[]): Uint8Array {
  return Uint8Array.from(values);
}

type Message = string | ArrayBuffer;

/** One accepted client end, buffering every message until a test takes it. */
export class Client {
  readonly queue: Message[] = [];
  private waiters: Array<() => void> = [];
  readonly closed: Promise<{ code: number; reason: string }>;

  constructor(readonly ws: WebSocket, readonly protocol: string | null) {
    this.closed = new Promise((resolve) => {
      ws.addEventListener('close', (event) => {
        resolve({ code: event.code, reason: event.reason });
        this.wake();
      });
    });
    ws.addEventListener('message', (event) => {
      this.queue.push(event.data as Message);
      this.wake();
    });
    // workerd's default is "blob" at this compatibility date, as in a browser.
    ws.binaryType = 'arraybuffer';
    ws.accept();
  }

  private wake(): void {
    const waiters = this.waiters;
    this.waiters = [];
    for (const waiter of waiters) waiter();
  }

  /** The first buffered message that `match` accepts, removed from the queue; waits up to `ms` for one. */
  async take(match: (message: Message) => boolean, ms = 2000): Promise<Message> {
    const deadline = Date.now() + ms;
    for (;;) {
      const index = this.queue.findIndex(match);
      if (index >= 0) return this.queue.splice(index, 1)[0] as Message;
      const left = deadline - Date.now();
      if (left <= 0) throw new Error(`no matching message within ${ms} ms; queue: ${JSON.stringify(this.queue.map(describe))}`);
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, left);
        this.waiters.push(() => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
  }

  async binary(ms?: number): Promise<Uint8Array> {
    return new Uint8Array((await this.take((m) => typeof m !== 'string', ms)) as ArrayBuffer);
  }

  async text(value: string, ms?: number): Promise<string> {
    return (await this.take((m) => m === value, ms)) as string;
  }

  /** The next presence frame. */
  async presence(ms?: number): Promise<{ t: string; node: boolean; pages: number }> {
    const isPresence = (m: Message) => typeof m === 'string' && m.startsWith('{"t":"presence"');
    return JSON.parse((await this.take(isPresence, ms)) as string);
  }

  /** Wait for the presence frame equal to `expected`, skipping older ones. */
  async presenceIs(expected: { node: boolean; pages: number }, ms = 2000): Promise<void> {
    const deadline = Date.now() + ms;
    for (;;) {
      const seen = await this.presence(Math.max(1, deadline - Date.now()));
      if (seen.node === expected.node && seen.pages === expected.pages) return;
    }
  }

  /** Nothing binary arrives within `ms`. */
  async noBinary(ms = 150): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, ms));
    expect(this.queue.filter((m) => typeof m !== 'string').map(describe)).toEqual([]);
  }

  send(data: string | Uint8Array): void {
    this.ws.send(data);
  }

  close(code = 1000, reason = 'done'): void {
    try {
      this.ws.close(code, reason);
    } catch {
      // Already closed.
    }
  }
}

function describe(message: Message): string {
  return typeof message === 'string' ? message : `<${message.byteLength} bytes>`;
}

const opened: Client[] = [];

/** Close every socket a test opened (afterEach). */
export function closeAll(): void {
  for (const client of opened.splice(0)) client.close();
}

export interface OpenOptions {
  origin?: string | null;
  protocols?: string[] | null;
  /** Vars or bindings to change for this request (the Worker reads env per request). */
  vars?: Partial<Env>;
  /** Extra request headers (CF-Connecting-IP, say). */
  headers?: Record<string, string>;
}

/** Fetch `path` as a WebSocket upgrade through the Worker; the raw response. */
export async function upgrade(path: string, options: OpenOptions = {}): Promise<Response> {
  const headers = new Headers({ ...options.headers, Upgrade: 'websocket' });
  if (options.origin !== null) headers.set('Origin', options.origin ?? SITE);
  if (options.protocols) headers.set('Sec-WebSocket-Protocol', options.protocols.join(', '));
  return worker.fetch(new Request(`https://relay.test${path}`, { headers }), { ...env, ...options.vars });
}

/** Open `path`; every refusal is a 101 that closes at once, so this always gives a client. */
export async function open(path: string, options: OpenOptions = {}): Promise<Client> {
  const response = await upgrade(path, options);
  expect(response.status).toBe(101);
  expect(response.webSocket).toBeTruthy();
  const client = new Client(response.webSocket as WebSocket, response.headers.get('Sec-WebSocket-Protocol'));
  opened.push(client);
  return client;
}

/** A page socket into `room` with `auth` (and a licence certificate when given). */
export function page(room: string, auth: string, options: OpenOptions & { licence?: string } = {}): Promise<Client> {
  const protocols = options.protocols ?? [PROTOCOL, `a.${auth}`, ...(options.licence ? [`l.${options.licence}`] : [])];
  return open(`/v1/rooms/${room}?role=page`, { ...options, protocols });
}

/** A node socket into `room`: no Origin. */
export function node(room: string, auth: string, options: OpenOptions = {}): Promise<Client> {
  return open(`/v1/rooms/${room}?role=node`, { origin: null, protocols: [PROTOCOL, `a.${auth}`], ...options });
}

/** A socket refused at once: its close code and reason. */
export async function refusal(client: Client): Promise<{ code: number; reason: string }> {
  return client.closed;
}

export function roomStub(room: string) {
  return env.ROOMS.get(env.ROOMS.idFromName(room));
}
