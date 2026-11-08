/**
 * plexora-relay: how the website reaches a remote server with nothing to download (docs/internal/WEB_REMOTE_RELAY.md).
 *
 *   GET /v1/hello            what this relay offers, with CORS for ALLOWED_ORIGINS
 *   GET /v1/ssh              WebSocket <-> TCP, for starting a node over SSH (ssh.ts)
 *   GET /v1/rooms/<room>     WebSocket into the room's Durable Object (room.ts)
 *
 * Everything else is 404.
 */
import { admittedOrigin, type Env, sshPorts } from './env';
import { licenceRefusal, websiteSwitch } from './licence';
import { CLOSE, isWebSocketUpgrade, parseProtocols, REFUSAL_HEADER, refuseSocket } from './protocol';
import { handleSsh } from './ssh';

export { Room } from './room';

const ROOM_PATH = /^\/v1\/rooms\/([^/]*)$/;
const ROOM_ID = /^[A-Za-z0-9_-]{22,64}$/;
/** How long a browser may cache a preflight answer, in seconds: two hours is Chrome's cap. */
const CORS_MAX_AGE = 7200;

function notFound(): Response {
  return new Response('Not found.\n', { status: 404, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
}

/** `/v1/hello`: GET (and HEAD) with the switch as it stands, OPTIONS for a preflight. */
async function hello(request: Request, env: Env): Promise<Response> {
  const origin = admittedOrigin(request.headers.get('Origin'), env);
  const headers = new Headers({ Vary: 'Origin' });
  if (origin) headers.set('Access-Control-Allow-Origin', origin);
  if (request.method === 'OPTIONS') {
    if (origin) {
      headers.set('Access-Control-Allow-Methods', 'GET, OPTIONS');
      const asked = request.headers.get('Access-Control-Request-Headers');
      if (asked) headers.set('Access-Control-Allow-Headers', asked);
      headers.set('Access-Control-Max-Age', String(CORS_MAX_AGE));
    }
    return new Response(null, { status: 204, headers });
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    headers.set('Allow', 'GET, HEAD, OPTIONS');
    return new Response('Method not allowed.\n', { status: 405, headers });
  }
  const body = { relay: 1, ssh_ports: sshPorts(env), website: await websiteSwitch(env.FEATURES_URL) };
  headers.set('Content-Type', 'application/json');
  headers.set('Cache-Control', 'no-store');
  return new Response(request.method === 'HEAD' ? null : JSON.stringify(body), { status: 200, headers });
}

/** `/v1/rooms/<room>?role=page|node`: the checks that need no room, then the room itself. */
async function room(request: Request, env: Env, id: string): Promise<Response> {
  if (!isWebSocketUpgrade(request)) {
    return new Response('Expected a WebSocket upgrade.\n', { status: 426, headers: { Upgrade: 'websocket' } });
  }
  if (!ROOM_ID.test(id)) return refuseSocket(request, CLOSE.badRequest, 'room must be 22 to 64 of A-Z a-z 0-9 _ -');
  const role = new URL(request.url).searchParams.get('role');
  if (role !== 'page' && role !== 'node') return refuseSocket(request, CLOSE.badRequest, 'role must be page or node');
  if (role === 'page') {
    // Pages are browsers: an Origin is required and must be ours. Nodes send none and are not asked for one.
    if (!admittedOrigin(request.headers.get('Origin'), env)) {
      return refuseSocket(request, CLOSE.refused, 'This page may not use the relay (origin not allowed).');
    }
    const offered = parseProtocols(request.headers.get('Sec-WebSocket-Protocol'));
    const refusal = await licenceRefusal(env.FEATURES_URL, offered.licence);
    if (refusal) return refuseSocket(request, CLOSE.refused, refusal);
  }
  const stub = env.ROOMS.get(env.ROOMS.idFromName(id));
  const response = await stub.fetch(request);
  const code = response.headers.get(REFUSAL_HEADER);
  if (response.status === 101 || code === null) return response;
  return refuseSocket(request, Number(code), await response.text());
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const { pathname } = new URL(request.url);
    if (pathname === '/v1/hello') return hello(request, env);
    if (pathname === '/v1/ssh') return handleSsh(request, env);
    const match = ROOM_PATH.exec(pathname);
    if (match) return room(request, env, match[1] ?? '');
    return notFound();
  },
} satisfies ExportedHandler<Env>;
