/**
 * End to end through `wrangler dev --local` (no Cloudflare account, nothing remote), driven with Node's own WebSocket
 * client the way a browser drives it: a real HTTP upgrade with an Origin, the subprotocol answer, close codes on the
 * wire, and the runtime's ping -> pong. The node side is a WebSocket with no Origin, as aiohttp's is.
 *
 * Every var is given with --var, so a developer's own .dev.vars does not change the run.
 */
import { type ChildProcess, spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { createServer, type AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { type Fixtures, startFixtures } from '../fixtures';

const RELAY = fileURLToPath(new URL('../..', import.meta.url));
const ORIGIN = 'http://localhost:5173';
const PROTOCOL = 'plexora-relay.v1';

let fixtures: Fixtures;
let wrangler: ChildProcess;
let base: string;
let log = '';
let state: string;

function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const server = createServer();
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as AddressInfo;
      server.close(() => resolve(port));
    });
  });
}

beforeAll(async () => {
  fixtures = await startFixtures();
  const port = await freePort();
  base = `127.0.0.1:${port}`;
  state = mkdtempSync(join(tmpdir(), 'plexora-relay-dev-'));
  const vars = {
    ALLOWED_ORIGINS: 'http://localhost:*',
    SSH_PORTS: `22,${fixtures.echoPort}`,
    ALLOW_PRIVATE_HOSTS: '1',
    FEATURES_URL: `http://127.0.0.1:${fixtures.featuresPort}/free`,
    ROOM_IDLE_DAYS: '7',
  };
  const args = ['dev', '--local', '--ip', '127.0.0.1', '--port', String(port), '--inspector-port', String(await freePort()),
    '--persist-to', state, '--show-interactive-dev-session=false', '--log-level', 'warn'];
  for (const [key, value] of Object.entries(vars)) args.push('--var', `${key}:${value}`);
  wrangler = spawn(join(RELAY, 'node_modules', '.bin', 'wrangler'), args, {
    cwd: RELAY,
    env: { ...process.env, WRANGLER_SEND_METRICS: 'false', CI: '1', NO_COLOR: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  wrangler.stdout?.on('data', (chunk) => (log += chunk));
  wrangler.stderr?.on('data', (chunk) => (log += chunk));
  const deadline = Date.now() + 60_000;
  for (;;) {
    try {
      const response = await fetch(`http://${base}/v1/hello`);
      if (response.ok) break;
    } catch {
      // Not up yet.
    }
    if (Date.now() > deadline || wrangler.exitCode !== null) throw new Error(`wrangler dev did not start:\n${log}`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
});

afterAll(async () => {
  wrangler?.kill('SIGTERM');
  await fixtures?.close();
  if (state) rmSync(state, { recursive: true, force: true });
});

type Received = string | Uint8Array;

/** Node's WebSocket, buffering what arrives. */
function connect(path: string, protocols: string[], origin: string | null) {
  const headers: Record<string, string> = origin ? { Origin: origin } : {};
  // Node's WebSocket takes headers beside protocols (undici's WebSocketInit); a browser sets Origin itself.
  const ws = new WebSocket(`ws://${base}${path}`, { protocols, headers } as unknown as string[]);
  ws.binaryType = 'arraybuffer';
  const queue: Received[] = [];
  let wake: (() => void) | null = null;
  ws.addEventListener('message', (event) => {
    queue.push(typeof event.data === 'string' ? event.data : new Uint8Array(event.data as ArrayBuffer));
    wake?.();
  });
  const opened = new Promise<void>((resolve, reject) => {
    ws.addEventListener('open', () => resolve());
    ws.addEventListener('error', () => reject(new Error(`could not open ${path}`)));
  });
  const closed = new Promise<{ code: number; reason: string }>((resolve) => {
    ws.addEventListener('close', (event) => resolve({ code: event.code, reason: event.reason }));
  });
  async function take(match: (m: Received) => boolean, ms = 5000): Promise<Received> {
    const deadline = Date.now() + ms;
    for (;;) {
      const at = queue.findIndex(match);
      if (at >= 0) return queue.splice(at, 1)[0] as Received;
      if (Date.now() > deadline) throw new Error(`nothing matching on ${path}; have ${JSON.stringify(queue)}`);
      await new Promise<void>((resolve) => {
        wake = resolve;
        setTimeout(resolve, 50);
      });
    }
  }
  const presence = async (node: boolean, pages: number) => {
    const want = JSON.stringify({ t: 'presence', node, pages });
    await take((m) => m === want);
  };
  return { ws, opened, closed, take, presence };
}

const b64 = (bytes: Uint8Array) => Buffer.from(bytes).toString('base64url');

describe('wrangler dev', () => {
  it('/v1/hello with CORS for a localhost page', async () => {
    const response = await fetch(`http://${base}/v1/hello`, { headers: { Origin: ORIGIN } });
    expect(response.headers.get('access-control-allow-origin')).toBe(ORIGIN);
    expect(await response.json()).toEqual({ relay: 1, ssh_ports: [22, fixtures.echoPort], website: 'free' });
  });

  it('/v1/ssh: the handshake answers our subprotocol, and bytes go round', async () => {
    const ssh = connect(`/v1/ssh?host=127.0.0.1&port=${fixtures.echoPort}`, [PROTOCOL], ORIGIN);
    await ssh.opened;
    expect(ssh.ws.protocol).toBe(PROTOCOL);
    ssh.ws.send(Uint8Array.from([1, 2, 3]));
    expect(Array.from((await ssh.take((m) => typeof m !== 'string')) as Uint8Array)).toEqual([1, 2, 3]);
    ssh.ws.close();
  });

  it('/v1/ssh: refusals arrive as close codes a page can read', async () => {
    const origin = connect(`/v1/ssh?host=127.0.0.1&port=${fixtures.echoPort}`, [PROTOCOL], 'https://evil.example');
    expect((await origin.closed).code).toBe(4403);
    const port = connect('/v1/ssh?host=127.0.0.1&port=2222', [PROTOCOL], ORIGIN);
    expect((await port.closed).code).toBe(4403);
    const host = connect('/v1/ssh?host=127.1&port=22', [PROTOCOL], ORIGIN);
    expect((await host.closed).code).toBe(4400);
  });

  it('/v1/ssh: wrangler.toml\'s SSH_LIMIT (20 a minute per address) closes the next one 4429', async () => {
    // A fixed one-minute window may roll over once mid-run, so up to two windows' worth.
    let refused: { code: number; reason: string } | null = null;
    for (let i = 0; i < 42 && !refused; i += 1) {
      const ssh = connect(`/v1/ssh?host=127.0.0.1&port=${fixtures.echoPort}`, [PROTOCOL], ORIGIN);
      const outcome = await Promise.race([ssh.opened.then(() => 'open'), ssh.closed]);
      const closed = await Promise.race([ssh.closed, new Promise<null>((resolve) => setTimeout(() => resolve(null), 100))]);
      if (closed && closed.code === 4429) refused = closed;
      else if (outcome === 'open') ssh.ws.close();
    }
    expect(refused).toEqual({ code: 4429, reason: 'Too many connections from this address; try again in a minute.' });
  });

  it('a room: page creates, node joins, frames cross with the pid, ping is answered', async () => {
    const room = b64(crypto.getRandomValues(new Uint8Array(16)));
    const auth = b64(crypto.getRandomValues(new Uint8Array(32)));
    const page = connect(`/v1/rooms/${room}?role=page`, [PROTOCOL, `a.${auth}`], ORIGIN);
    await page.opened;
    expect(page.ws.protocol).toBe(PROTOCOL);
    await page.presence(false, 1);

    const stranger = connect(`/v1/rooms/${b64(crypto.getRandomValues(new Uint8Array(16)))}?role=node`, [PROTOCOL, `a.${auth}`], null);
    expect(await stranger.closed).toEqual({ code: 4404, reason: 'no such room' });
    const wrong = connect(`/v1/rooms/${room}?role=node`, [PROTOCOL, `a.${b64(new Uint8Array(32))}`], null);
    expect(await wrong.closed).toEqual({ code: 4401, reason: 'wrong room auth' });

    const node = connect(`/v1/rooms/${room}?role=node`, [PROTOCOL, `a.${auth}`], null);
    await node.opened;
    await node.presence(true, 1);
    await page.presence(true, 1);

    page.ws.send(Uint8Array.from([10, 20]));
    const framed = (await node.take((m) => typeof m !== 'string')) as Uint8Array;
    expect(framed.length).toBe(10);
    const reply = new Uint8Array(11);
    reply.set(framed.subarray(0, 8));
    reply.set([1, 2, 3], 8);
    node.ws.send(reply);
    expect(Array.from((await page.take((m) => typeof m !== 'string')) as Uint8Array)).toEqual([1, 2, 3]);

    page.ws.send('ping');
    expect(await page.take((m) => m === 'pong')).toBe('pong');
    node.ws.send('ping');
    expect(await node.take((m) => m === 'pong')).toBe('pong');

    const second = connect(`/v1/rooms/${room}?role=node`, [PROTOCOL, `a.${auth}`], null);
    await second.opened;
    expect(await node.closed).toEqual({ code: 4000, reason: 'replaced' });

    page.ws.close();
    await second.presence(true, 0);
    second.ws.close();
  });
});
