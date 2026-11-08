/**
 * What every relay socket shares (docs/internal/WEB_REMOTE_RELAY.md): the subprotocol list, the close codes, and how a
 * refusal reaches a browser.
 *
 * A browser's WebSocket sees no HTTP status: a handshake answered 4xx is just "closed, 1006". So a refusal is an
 * accepted handshake that is closed at once with the code and a plain reason, which the page can show. The handshake
 * must echo `plexora-relay.v1` whenever the client offered it, or Chrome fails the connection itself ("Sent non-empty
 * 'Sec-WebSocket-Protocol' header but no response was received") and the code is lost again.
 */

export const PROTOCOL = 'plexora-relay.v1';

/** Close codes. 4000 is the room's; 4429 is a full room or a rate limit (ratelimit.ts); the rest are shared. */
export const CLOSE = Object.freeze({
  replaced: 4000,
  badRequest: 4400,
  wrongAuth: 4401,
  refused: 4403,
  noRoom: 4404,
  full: 4429,
  tooMany: 4429,
  tcpFailed: 4502,
});

export const LICENCE_REQUIRED = 'A Plexora licence is required to connect to servers from the website.';
export const TOO_MANY = 'Too many connections from this address; try again in a minute.';

/** `roomAuth`: 43 characters of base64url, the encoding of 32 random bytes. */
const AUTH = /^[A-Za-z0-9_-]{43}$/;

export interface Offered {
  /** Whether the client offered `plexora-relay.v1`. */
  protocol: boolean;
  /** The `a.` value, when exactly one well-formed one was offered. */
  auth: string | null;
  /** The `l.` value (a licence certificate), when exactly one was offered; two are no licence. */
  licence: string | null;
  /** Why the room auth cannot be used, or null (also null when none was offered). */
  problem: string | null;
}

/**
 * The `Sec-WebSocket-Protocol` list a client offered. Entries other than ours are ignored, so a later client may offer
 * more; two `a.` entries are a bad request rather than a choice. `l.` is only read where the licence switch asks for it
 * (pages, `/v1/ssh`), so two of those simply present no licence.
 */
export function parseProtocols(header: string | null): Offered {
  const entries = (header ?? '').split(',').map((entry) => entry.trim()).filter(Boolean);
  const auths = entries.filter((entry) => entry.startsWith('a.')).map((entry) => entry.slice(2));
  const licences = entries.filter((entry) => entry.startsWith('l.')).map((entry) => entry.slice(2));
  const offered: Offered = { protocol: entries.includes(PROTOCOL), auth: null, licence: null, problem: null };
  if (licences.length === 1 && licences[0]) offered.licence = licences[0];
  if (auths.length === 1 && AUTH.test(auths[0] ?? '')) offered.auth = auths[0] ?? null;
  else if (auths.length > 1) offered.problem = 'more than one room auth was offered';
  else if (auths.length === 1) offered.problem = 'the room auth is not 43 characters of base64url';
  return offered;
}

export function isWebSocketUpgrade(request: Request): boolean {
  return request.method === 'GET' && (request.headers.get('Upgrade') ?? '').toLowerCase() === 'websocket';
}

/** A close reason is at most 123 bytes of UTF-8. */
export function closeReason(text: string): string {
  const bytes = new TextEncoder().encode(text);
  if (bytes.length <= 123) return text;
  let cut = 120;
  // Back off to a character boundary (never inside a multi-byte sequence).
  while (cut > 0 && ((bytes[cut] ?? 0) & 0xc0) === 0x80) cut -= 1;
  return `${new TextDecoder().decode(bytes.subarray(0, cut))}...`;
}

/** The handshake headers: ours echoed when it was offered, nothing otherwise. */
export function handshakeHeaders(offeredProtocol: boolean): Headers {
  const headers = new Headers();
  if (offeredProtocol) headers.set('Sec-WebSocket-Protocol', PROTOCOL);
  return headers;
}

/**
 * Accept the handshake and close it straight away with `code` and `reason`. Only ever in the plain Worker: the room's
 * Durable Object answers a refusal as an HTTP response (`refusalResponse`) and the Worker turns it into this, so every
 * socket the room accepts is a hibernatable one.
 */
export function refuseSocket(request: Request, code: number, reason: string): Response {
  const pair = new WebSocketPair();
  const client = pair[0];
  const server = pair[1];
  server.accept();
  server.close(code, closeReason(reason));
  return new Response(null, {
    status: 101,
    webSocket: client,
    headers: handshakeHeaders(parseProtocols(request.headers.get('Sec-WebSocket-Protocol')).protocol),
  });
}

/** Header carrying a refusal's close code from the room back to the Worker. */
export const REFUSAL_HEADER = 'X-Relay-Close';

/** The room's refusal: a plain response the Worker converts with `refuseSocket` (never sent to a client as it is). */
export function refusalResponse(code: number, reason: string): Response {
  return new Response(reason, { status: 400, headers: { [REFUSAL_HEADER]: String(code) } });
}
