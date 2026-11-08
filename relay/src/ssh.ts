/**
 * `GET /v1/ssh?host=&port=`: one WebSocket piped to one TCP connection, for the few seconds the page needs to sign in
 * over SSH and start a node (docs/internal/WEB_REMOTE_RELAY.md, "SSH start"). Binary messages are TCP bytes both ways;
 * text messages are ignored. The plain Worker holds it, so a Cloudflare runtime update may cut it; nothing long-lived
 * rides on it.
 *
 * Refusals, in the order they are checked: an Origin not in ALLOWED_ORIGINS (4403), a missing or unreadable host or
 * port (4400), a port not in SSH_PORTS (4403), a host that is not a DNS name or public address (4400), more than
 * SSH_LIMIT from this address (4429, ratelimit.ts), and with the website switch on "licensed" a missing or failing
 * `l.` certificate (4403). A connect that fails or does not finish
 * in CONNECT_TIMEOUT_MS closes 4502 with the error as the reason. The SSH server closing its end closes the socket
 * 1000; a TCP error after that closes it 1011.
 */
import { connect } from 'cloudflare:sockets';

import { admittedOrigin, allowPrivateHosts, type Env, sshPorts } from './env';
import { checkHost } from './hosts';
import { licenceRefusal } from './licence';
import { CLOSE, closeReason, handshakeHeaders, isWebSocketUpgrade, parseProtocols, refuseSocket, TOO_MANY } from './protocol';
import { withinLimit } from './ratelimit';

export const CONNECT_TIMEOUT_MS = 15_000;

export async function handleSsh(request: Request, env: Env): Promise<Response> {
  if (!isWebSocketUpgrade(request)) {
    return new Response('Expected a WebSocket upgrade.\n', { status: 426, headers: { Upgrade: 'websocket' } });
  }
  const offered = parseProtocols(request.headers.get('Sec-WebSocket-Protocol'));
  if (!admittedOrigin(request.headers.get('Origin'), env)) {
    return refuseSocket(request, CLOSE.refused, 'This page may not use the relay (origin not allowed).');
  }
  const url = new URL(request.url);
  const portText = url.searchParams.get('port') ?? '';
  if (!/^[1-9][0-9]{0,4}$/.test(portText) || Number(portText) > 65535) {
    return refuseSocket(request, CLOSE.badRequest, 'port must be a TCP port number');
  }
  const port = Number(portText);
  if (!sshPorts(env).includes(port)) {
    return refuseSocket(request, CLOSE.refused, `port ${port} is not one the relay opens`);
  }
  const host = checkHost(url.searchParams.get('host'), allowPrivateHosts(env));
  if (!host.ok) return refuseSocket(request, CLOSE.badRequest, host.reason);
  // Counted only once the request could open something: a malformed one costs nothing.
  if (!(await withinLimit(env.SSH_LIMIT, request))) return refuseSocket(request, CLOSE.tooMany, TOO_MANY);
  const refusal = await licenceRefusal(env.FEATURES_URL, offered.licence);
  if (refusal) return refuseSocket(request, CLOSE.refused, refusal);

  const pair = new WebSocketPair();
  const client = pair[0];
  const server = pair[1];
  // The runtime's default is "blob", as in a browser; the pipe wants bytes it can write at once, in order.
  server.binaryType = 'arraybuffer';
  server.accept();
  pipe(server, host.hostname, port);
  return new Response(null, { status: 101, webSocket: client, headers: handshakeHeaders(offered.protocol) });
}

function errorText(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  return text || 'connection failed';
}

/** Pump `ws` and a new TCP connection to `hostname:port` into each other until either ends. */
function pipe(ws: WebSocket, hostname: string, port: number): void {
  let finished = false;
  let socket: Socket;
  try {
    socket = connect({ hostname, port }, { secureTransport: 'off', allowHalfOpen: false });
  } catch (error) {
    ws.close(CLOSE.tcpFailed, closeReason(errorText(error)));
    return;
  }

  const finish = (code: number, reason: string) => {
    if (finished) return;
    finished = true;
    try {
      ws.close(code, closeReason(reason));
    } catch {
      // Already closed from the page's side.
    }
    socket.close().catch(() => undefined);
  };

  // Failures are handled where they happen; the socket's own `closed` would otherwise reject unobserved.
  socket.closed.catch(() => undefined);
  let opened = false;
  const timer = setTimeout(() => {
    if (!opened) finish(CLOSE.tcpFailed, `timed out connecting to ${hostname}:${port}`);
  }, CONNECT_TIMEOUT_MS);

  // Page -> TCP. Writes queue behind the connect, so bytes the page sends early are not lost.
  const writer = socket.writable.getWriter();
  let writes: Promise<unknown> = socket.opened.catch(() => undefined);
  ws.addEventListener('message', (event) => {
    if (typeof event.data === 'string' || finished) return;
    const bytes = new Uint8Array(event.data as ArrayBuffer);
    writes = writes
      .then(() => (finished ? undefined : writer.write(bytes)))
      .catch((error) => finish(1011, `tcp write failed: ${errorText(error)}`));
  });
  const pageGone = () => {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    try {
      ws.close(1000, 'closed');
    } catch {
      // The close handshake is already answered.
    }
    socket.close().catch(() => undefined);
  };
  ws.addEventListener('close', pageGone);
  ws.addEventListener('error', pageGone);

  // TCP -> page.
  socket.opened.then(
    async () => {
      opened = true;
      clearTimeout(timer);
      if (finished) return;
      const reader = socket.readable.getReader();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done || finished) break;
          ws.send(value);
        }
        finish(1000, 'the server closed the connection');
      } catch (error) {
        finish(1011, `tcp read failed: ${errorText(error)}`);
      }
    },
    (error) => {
      clearTimeout(timer);
      finish(CLOSE.tcpFailed, errorText(error));
    },
  );
}
