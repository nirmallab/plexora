/**
 * `Room`: one Durable Object per room id, forwarding sealed frames between the node and up to eight pages
 * (docs/internal/WEB_REMOTE_RELAY.md, "Room"). It never reads a frame: everything past the 8-byte pid is end-to-end
 * AES-GCM it holds no key for.
 *
 * Every socket is accepted with the Hibernation API (`ctx.acceptWebSocket`), never `ws.accept()`, so sockets stay
 * connected while the object is evicted (and, as the contract relies on, through code updates), and an idle room
 * costs no duration. Nothing is held in
 * memory between events: a socket's role and pid live in its tags and attachment, the room's auth hash in storage.
 *
 *   - The first page that presents `a.<roomAuth>` creates the room: sha256(roomAuth) is stored. A node only joins a
 *     room that exists (else 4404); any later socket whose hash differs gets 4401. Compared in constant time.
 *     Creating a room counts against ROOM_CREATE_LIMIT for the page's address (4429 past it, ratelimit.ts).
 *   - Pages are tagged ["page", "page:<pid>"] with a fresh 8-byte pid; the node ["node"]. A new node closes the old one
 *     with 4000 "replaced"; a ninth page gets 4429.
 *   - "ping" is answered "pong" by the runtime itself (setWebSocketAutoResponse), without waking the object.
 *   - After every accept and every close, `{"t":"presence","node":…,"pages":…}` goes to every socket.
 *   - Binary page -> node frames gain the page's pid in front; node -> page frames lose it and go to that page, or to
 *     every page for the all-zero pid. Text frames other than "ping" are ignored.
 *   - Every accept, and every close, sets an alarm ROOM_IDLE_DAYS ahead; when it fires with no socket open the room's
 *     storage is deleted. With a socket still open it is set again.
 *
 * A refusal is an HTTP response (protocol.ts `refusalResponse`) that the Worker turns into a closed socket, so the only
 * sockets this object ever accepts are hibernatable ones.
 */
import { DurableObject } from 'cloudflare:workers';

import { type Env, roomIdleMs } from './env';
import { CLOSE, PROTOCOL, parseProtocols, refusalResponse, TOO_MANY } from './protocol';
import { withinLimit } from './ratelimit';

export const MAX_PAGES = 8;
const AUTH_KEY = 'auth';

type Attachment = { role: 'page'; pid: string } | { role: 'node' };

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

function fromHex(text: string): Uint8Array {
  const out = new Uint8Array(text.length / 2);
  for (let i = 0; i < out.length; i += 1) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

async function sha256(text: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
}

/** Constant-time equality of two digests (timingSafeEqual needs equal lengths, so that is checked first). */
function sameDigest(a: Uint8Array, b: Uint8Array): boolean {
  if (a.byteLength !== b.byteLength) return false;
  return crypto.subtle.timingSafeEqual(a, b);
}

function attachmentOf(ws: WebSocket): Attachment | null {
  try {
    return ws.deserializeAttachment() as Attachment | null;
  } catch {
    return null;
  }
}

function isOpen(ws: WebSocket): boolean {
  return ws.readyState === WebSocket.OPEN;
}

export class Room extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('ping', 'pong'));
  }

  /** The Worker has checked the path, the role, the Origin and the licence; this checks the room. */
  async fetch(request: Request): Promise<Response> {
    const role = new URL(request.url).searchParams.get('role');
    if (role !== 'page' && role !== 'node') return refusalResponse(CLOSE.badRequest, 'role must be page or node');
    const offered = parseProtocols(request.headers.get('Sec-WebSocket-Protocol'));
    if (!offered.protocol) return refusalResponse(CLOSE.badRequest, `the ${PROTOCOL} subprotocol is required`);
    if (offered.problem) return refusalResponse(CLOSE.badRequest, offered.problem);
    if (!offered.auth) return refusalResponse(CLOSE.badRequest, 'a room auth (a.<43 base64url>) is required');

    // Synchronous storage: nothing can interleave between reading the hash and storing it.
    const digest = await sha256(offered.auth);
    let stored = this.ctx.storage.kv.get<ArrayBuffer>(AUTH_KEY);
    if (stored === undefined && role === 'page') {
      // Creating a room is limited per address; joining one is not.
      if (!(await withinLimit(this.env.ROOM_CREATE_LIMIT, request))) return refusalResponse(CLOSE.tooMany, TOO_MANY);
      // Another page may have created it while the limit was asked: read again, then decide with no await between.
      stored = this.ctx.storage.kv.get<ArrayBuffer>(AUTH_KEY);
    }
    if (stored === undefined) {
      if (role === 'node') return refusalResponse(CLOSE.noRoom, 'no such room');
      this.ctx.storage.kv.put(AUTH_KEY, digest.buffer);
    } else if (!sameDigest(new Uint8Array(stored), digest)) {
      return refusalResponse(CLOSE.wrongAuth, 'wrong room auth');
    }

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    if (role === 'page') {
      if (this.open('page').length >= MAX_PAGES) return refusalResponse(CLOSE.full, `the room already has ${MAX_PAGES} pages`);
      const pid = hex(crypto.getRandomValues(new Uint8Array(8)));
      this.ctx.acceptWebSocket(server, ['page', `page:${pid}`]);
      server.serializeAttachment({ role: 'page', pid } satisfies Attachment);
    } else {
      for (const old of this.ctx.getWebSockets('node')) {
        try {
          old.close(CLOSE.replaced, 'replaced');
        } catch {
          // Already closing.
        }
      }
      this.ctx.acceptWebSocket(server, ['node']);
      server.serializeAttachment({ role: 'node' } satisfies Attachment);
    }
    await this.armAlarm();
    this.sendPresence();
    return new Response(null, { status: 101, webSocket: client, headers: { 'Sec-WebSocket-Protocol': PROTOCOL } });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (typeof message === 'string') return;
    const from = attachmentOf(ws);
    if (!from) return;
    if (from.role === 'page') {
      const node = this.open('node')[0];
      if (!node) return;
      const out = new Uint8Array(8 + message.byteLength);
      out.set(fromHex(from.pid), 0);
      out.set(new Uint8Array(message), 8);
      node.send(out);
      return;
    }
    if (message.byteLength < 8) return;
    const bytes = new Uint8Array(message);
    const pid = bytes.subarray(0, 8);
    const payload = bytes.slice(8);
    const pages = pid.every((b) => b === 0) ? this.open('page') : this.open(`page:${hex(pid)}`);
    for (const page of pages) {
      try {
        page.send(payload);
      } catch {
        // Closing under us; its close event sends presence.
      }
    }
  }

  async webSocketClose(ws: WebSocket, _code: number, _reason: string, _wasClean: boolean): Promise<void> {
    await this.gone(ws);
  }

  async webSocketError(ws: WebSocket, _error: unknown): Promise<void> {
    await this.gone(ws);
  }

  async alarm(): Promise<void> {
    if (this.open().length > 0) {
      await this.armAlarm();
      return;
    }
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
  }

  /** A socket closed or failed: finish its close, tell everyone else, and restart the idle clock. */
  private async gone(ws: WebSocket): Promise<void> {
    try {
      ws.close(1000, 'closed');
    } catch {
      // The runtime already answered the close.
    }
    this.sendPresence(ws);
    await this.armAlarm();
  }

  /** Open sockets, optionally only those with `tag`. A socket being closed (a replaced node) is not open. */
  private open(tag?: string): WebSocket[] {
    return this.ctx.getWebSockets(tag).filter(isOpen);
  }

  private sendPresence(exclude?: WebSocket): void {
    const sockets = this.open().filter((ws) => ws !== exclude);
    let node = false;
    let pages = 0;
    for (const ws of sockets) {
      const role = attachmentOf(ws)?.role;
      if (role === 'node') node = true;
      else if (role === 'page') pages += 1;
    }
    const text = JSON.stringify({ t: 'presence', node, pages });
    for (const ws of sockets) {
      try {
        ws.send(text);
      } catch {
        // Closing under us.
      }
    }
  }

  private async armAlarm(): Promise<void> {
    await this.ctx.storage.setAlarm(Date.now() + roomIdleMs(this.env));
  }
}
