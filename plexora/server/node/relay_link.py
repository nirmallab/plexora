"""A data node's link to the website's relay: requests in, responses out.

`plexora node serve --relay-file F` starts one of these once its HTTP server
is bound (docs/internal/WEB_REMOTE_RELAY.md is the contract). The node dials
*out* to `wss://<relay>/v1/rooms/<room>?role=node`, a page on the website dials
the same room, and the relay forwards sealed frames between them
(plexora/relay_wire.py). Each REQUEST a page sends is made against this
node's own server on loopback, exactly as a viewer's request would be, and the
answer goes back in RESPONSE_HEAD and RESPONSE_BODY frames.

The node's life is never tied to this socket. It reconnects for as long as the
node runs, and requests a page had in flight are sent again by the page with
the same stream id once both ends are back, so this side:

- executes a repeated GET or HEAD again (a read costs nothing twice);
- never executes anything else twice: a repeat of a request it is still
  answering is answered by that execution, from its first byte, and a repeat
  of one it has answered in the last two minutes is answered from a copy;
- addresses a client's answers to the pid that client last spoke from, since
  a page that reconnected has a new one.

One daemon thread with its own asyncio loop and aiohttp sessions; `stop()`
ends it. The node stops itself (through `shutdown`, `_lifetime.request_shutdown`
by default) on STOP, after `idle_exit` seconds with no page, and when the room
turns out not to be its own.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import random
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from plexora import relay_wire as wire

_log = logging.getLogger(__name__)

#: Printed to stdout, flushed, each time the node joins its room. The start
#: script's `tail` waits for the first one before the page closes SSH.
JOINED_PREFIX = "[plexora-relay] joined"

TOKEN_HEADER = "X-Plexora-Node-Token"

#: Never forwarded to the node's server: they describe the hop, not the
#: request, and `accept-encoding` would have the server compress what the page
#: wants as served.
DROP_REQUEST_HEADERS = frozenset({
    "connection", "keep-alive", "transfer-encoding", "upgrade", "host",
    "content-length", "accept-encoding",
})
#: Never forwarded back: the page is handed the body bytes themselves.
DROP_RESPONSE_HEADERS = frozenset({
    "connection", "keep-alive", "transfer-encoding", "upgrade",
})

READ_METHODS = frozenset({"GET", "HEAD"})
_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
_CLIENT_MAX = 128

#: Close codes the relay uses that mean "stop trying": another node took the
#: room, or this node's auth is not the room's.
CLOSE_REPLACED = 4000
CLOSE_UNAUTHORISED = 4401
CLOSE_NO_ROOM = 4404


class RelayConfigError(ValueError):
    """A relay file this node cannot use, with a sentence worth printing."""


@dataclass(frozen=True)
class RelayConfig:
    relay: str
    room: str
    auth: str
    key: bytes
    idle_exit: float = 0.0

    @property
    def room_url(self):
        return f"{self.relay}/v1/rooms/{self.room}?role=node"

    def __repr__(self):  # never the key or the auth, in a log or a traceback
        return f"RelayConfig(relay={self.relay!r}, room={self.room!r}, idle_exit={self.idle_exit})"


def parse_relay_config(data):
    """A RelayConfig from the relay file's JSON object, or RelayConfigError.

    Fields this version does not know are ignored, so a newer page can add
    some without breaking an older node.
    """
    if not isinstance(data, dict):
        raise RelayConfigError("the relay file must hold a JSON object")
    version = data.get("v")
    if version != wire.VERSION or isinstance(version, bool):
        raise RelayConfigError(
            f"the relay file is version {version!r}; this Plexora reads version "
            f"{wire.VERSION} (update Plexora on this machine, or the page)")
    relay = data.get("relay")
    if not isinstance(relay, str):
        raise RelayConfigError('the relay file has no "relay" address')
    relay = relay.strip().rstrip("/")
    parts = urllib.parse.urlsplit(relay)
    if parts.scheme not in ("wss", "ws") or not parts.hostname or parts.query or parts.fragment:
        raise RelayConfigError(
            f'"relay" must be a wss:// address such as wss://relay.plexoraapp.com, '
            f"not {relay!r}")
    try:
        room = wire.check_room(data.get("room"))
    except wire.RelayWireError as exc:
        raise RelayConfigError(f'"room": {exc}') from None
    try:
        auth = wire.check_auth(data.get("auth"))
    except wire.RelayWireError as exc:
        raise RelayConfigError(f'"auth": {exc}') from None
    try:
        key = wire.key_from_b64(data.get("key"))
    except wire.RelayWireError as exc:
        raise RelayConfigError(f'"key": {exc} (32 bytes of base64url)') from None
    idle_exit = data.get("idle_exit", 0)
    if (isinstance(idle_exit, bool) or not isinstance(idle_exit, (int, float))
            or idle_exit != idle_exit or idle_exit < 0):
        raise RelayConfigError('"idle_exit" must be a number of seconds, 0 or more '
                               "(0 = never)")
    return RelayConfig(relay=relay, room=room, auth=auth, key=key,
                       idle_exit=float(idle_exit))


def read_relay_file(path, *, delete=True):
    """Read and check a relay file, then delete it.

    Deleted only once it has been read and found good: it holds the frame key
    and the room's auth, and nothing needs it again. A file refused is left
    where it is (the start script writes it 0600) so that whoever reads the
    error can look at it -- and so a path typed by mistake never costs
    somebody an unrelated file.
    """
    path = Path(path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RelayConfigError(f"no relay file at {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise RelayConfigError(f"could not read the relay file {path}: {exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RelayConfigError(f"the relay file {path} is not JSON ({exc})") from None
    try:
        config = parse_relay_config(data)
    except RelayConfigError as exc:
        raise RelayConfigError(f"the relay file {path}: {exc}") from None
    if delete:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return config


def _print_stdout(line):
    print(line, flush=True)


def _print_stderr(line):
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass


def _default_shutdown(reason):
    from plexora._lifetime import request_shutdown

    request_shutdown(reason, log=_print_stderr)


class _NotConnected(ConnectionError):
    pass


class _Exchange:
    """One request a page sent, and everything this node answered to it.

    `frames` holds the answer as produced; `cursor` is how many of them have
    gone to `target`, the pid delivery is addressed to (None while delivery
    waits for the page to ask again). An answer to anything but a read is kept
    whole, so it can be sent again from its first frame; past the cache's size
    it stops being kept (`base` counts the frames let go), and a repeat then
    gets an error instead of a second execution.
    """

    def __init__(self, link, client, stream, method, path, headers, target):
        self.client = client
        self.stream = stream
        self.method = method
        self.path = path
        self.headers = headers
        self.target = target
        self.reads = method in READ_METHODS
        self.upload = bytearray()
        self.upload_done = False
        self.frames = collections.deque()
        self.base = 0
        self.cursor = 0
        # Bumped whenever delivery starts over or stops, so a send that was
        # under way at that moment does not move the new cursor.
        self.epoch = 0
        self.size = 0
        self.keep = not self.reads
        self.head_sent = False
        self.done = False
        self.finished_at = None
        self.paused_at = None
        self.producer = None
        self.deliverer = None
        self.wake = asyncio.Event()
        self.space = asyncio.Event()
        self.space.set()
        self._link = link

    @property
    def key(self):
        return (self.client, self.stream)

    @property
    def total(self):
        return self.base + len(self.frames)

    @property
    def replayable(self):
        return self.base == 0

    @property
    def delivered(self):
        return self.done and self.cursor >= self.total

    def push(self, kind, header, body=b""):
        self.frames.append((kind, header, bytes(body)))
        self.size += len(body)
        if self.keep and self.size > self._link.cache_bytes:
            # Too big to keep for a resend: from here it streams like a read.
            self.keep = False
        self._trim()
        self.wake.set()

    def pending_bytes(self):
        return sum(len(body) for _k, _h, body in
                   list(self.frames)[max(0, self.cursor - self.base):])

    def _trim(self):
        if self.keep:
            return
        while self.frames and self.base < self.cursor:
            _kind, _header, body = self.frames.popleft()
            self.base += 1
        if self.pending_bytes() <= self._link.window_bytes:
            self.space.set()
        else:
            self.space.clear()

    def sent_one(self):
        self.cursor += 1
        self._trim()

    def pause(self):
        """Stop delivering until the page asks again; start over then."""
        self.target = None
        self.cursor = self.base if not self.replayable else 0
        self.epoch += 1
        self.paused_at = time.monotonic()

    def cancel(self):
        for task in (self.producer, self.deliverer):
            if task is not None and not task.done():
                task.cancel()
        self.space.set()
        self.wake.set()


class RelayLink:
    """The node's end of a relay room. `start()`, then `stop()` on the way out.

    `hello` is the node's HELLO header without `v` (node_id, api_version,
    token, version, hostname, platform). `port` and `token` are the node's
    own server, which every request is made against at `host` (loopback).
    `shutdown(reason)` is called when the node should end; `announce(line)`
    prints the joined line (stdout, flushed); `log(line)` everything else.
    """

    def __init__(self, config, *, port, token, hello=None, host="127.0.0.1",
                 shutdown=None, announce=None, log=None,
                 ping_interval=30.0, pong_timeout=75.0,
                 backoff_base=1.0, backoff_max=30.0,
                 cache_ttl=120.0, cache_bytes=64 << 20, window_bytes=8 << 20,
                 max_request_body=1 << 30, tick=1.0, join_grace=3.0):
        self.config = config
        self.port = int(port)
        self.token = token
        self.host = host
        self.hello = {"v": wire.VERSION, **(hello or {})}
        self.shutdown = shutdown or _default_shutdown
        self.announce = announce or _print_stdout
        self.log = log or _print_stderr
        self.ping_interval = float(ping_interval)
        self.pong_timeout = float(pong_timeout)
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.cache_ttl = float(cache_ttl)
        self.cache_bytes = int(cache_bytes)
        self.window_bytes = int(window_bytes)
        self.max_request_body = int(max_request_body)
        self.tick = float(tick)
        self.join_grace = float(join_grace)

        self.joins = 0
        self.joined = threading.Event()
        self.stopped = threading.Event()
        self.end_reason = None

        self._thread = None
        self._loop = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._stop_evt = None
        self._ws = None
        self._send_lock = None
        self._joined_now = False
        self._connected_at = 0.0
        self._last_pong = 0.0
        self._pages = 0
        self._last_page = time.monotonic()
        self._clients = {}     # client id -> pid it last spoke from
        self._pid_client = {}  # pid -> client id
        self._exchanges = {}   # (client, stream) -> _Exchange
        self._tasks = set()
        self._bad_frames = 0
        self._last_bad_log = 0.0
        self._http = None

    # -- the thread ---------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="plexora-relay", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        return self

    def stop(self, timeout=5.0):
        """Close the socket and end the thread. Safe to call more than once,
        from any thread, before or after the link stopped itself."""
        self._stop_requested.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake_stop)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def wait_joined(self, timeout=None):
        return self.joined.wait(timeout)

    @property
    def connected(self):
        return self._ws is not None and not self._ws.closed

    def _wake_stop(self):
        if self._stop_evt is not None:
            self._stop_evt.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            self._spawn(self._close(ws))

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            self._stop_evt = asyncio.Event()
            self._send_lock = asyncio.Lock()
            if self._stop_requested.is_set():
                self._stop_evt.set()
            self._ready.set()
            loop.run_until_complete(self._main())
        except Exception as exc:  # pragma: no cover - a bug, said once
            _log.exception("relay link failed")
            self.log(f"[plexora-relay] stopped: {exc}")
        finally:
            self._ready.set()
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self.stopped.set()

    # -- connecting ---------------------------------------------------------

    def _ssl_context(self):
        context = ssl.create_default_context()
        try:
            import certifi

            context.load_verify_locations(certifi.where())
        except Exception:
            pass
        return context

    def _proxy(self):
        """An http:// proxy for the relay from the environment, if any:
        WSS_PROXY/WS_PROXY, else HTTPS_PROXY/HTTP_PROXY (a cluster's compute
        nodes often reach the internet only through one), unless NO_PROXY
        names the relay."""
        parts = urllib.parse.urlsplit(self.config.relay)
        if parts.hostname in ("127.0.0.1", "localhost", "::1"):
            return None
        try:
            if parts.hostname and urllib.request.proxy_bypass(parts.hostname):
                return None
        except Exception:
            pass
        proxies = urllib.request.getproxies()
        order = ("wss", "https") if parts.scheme == "wss" else ("ws", "http")
        for name in order:
            proxy = proxies.get(name)
            if proxy and urllib.parse.urlsplit(proxy).scheme == "http":
                return proxy
        return None

    def _backoff(self, attempt):
        base = min(self.backoff_max, self.backoff_base * (2 ** min(attempt, 16)))
        return base * random.uniform(0.75, 1.0)

    async def _main(self):
        import aiohttp

        ws_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30),
            cookie_jar=aiohttp.DummyCookieJar())
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
            connector=aiohttp.TCPConnector(limit=32),
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False)
        self._spawn(self._idle_watch())
        self._spawn(self._janitor())
        context = self._ssl_context() if self.config.relay.startswith("wss:") else None
        proxy = self._proxy()
        attempt = 0
        try:
            while not self._stop_evt.is_set():
                joined = await self._connect_once(ws_session, context, proxy)
                if self._stop_evt.is_set():
                    break
                if joined:
                    attempt = 0
                delay = self._backoff(attempt)
                attempt += 1
                self.log(f"[plexora-relay] reconnecting in {delay:.1f} s")
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            for exchange in list(self._exchanges.values()):
                exchange.cancel()
            self._exchanges.clear()
            ws = self._ws
            if ws is not None and not ws.closed:
                try:
                    await asyncio.wait_for(ws.close(code=1001, message=b"node stopping"), 3)
                except Exception:
                    pass
            await ws_session.close()
            await self._http.close()

    async def _connect_once(self, session, context, proxy):
        """One socket, from dialling to its close. True when it joined."""
        import aiohttp

        try:
            ws = await asyncio.wait_for(session.ws_connect(
                self.config.room_url,
                protocols=wire.subprotocols(self.config.auth),
                heartbeat=None, autoping=True,
                timeout=aiohttp.ClientWSTimeout(ws_close=3.0),
                max_msg_size=33 << 20,
                ssl=context if context is not None else True,
                proxy=proxy), 45)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"[plexora-relay] could not reach the relay: {_describe(exc)}")
            return False
        if ws.protocol != wire.PROTOCOL:
            self.log(f"[plexora-relay] the relay answered with protocol {ws.protocol!r}, "
                     f"not {wire.PROTOCOL}")
            await ws.close()
            return False
        self._ws = ws
        self._joined_now = False
        self._connected_at = self._last_pong = time.monotonic()
        pinger = asyncio.ensure_future(self._pinger(ws))
        try:
            self._spawn(self._send_quiet(wire.ALL, wire.HELLO, 0, self.hello))
            while True:
                msg = await ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._on_text(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    self._mark_joined()
                    self._on_binary(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            pinger.cancel()
            self._ws = None
            self._pages = 0
            self._on_node_disconnected()
            if not ws.closed:
                try:
                    await asyncio.wait_for(ws.close(), 3)
                except Exception:
                    pass
        code = ws.close_code
        joined = self._joined_now
        if self._stop_evt.is_set():
            return joined
        if code == CLOSE_REPLACED:
            self._end("another node joined this room")
        elif code == CLOSE_UNAUTHORISED:
            self._end("the relay refused this node's room auth")
        elif code == CLOSE_NO_ROOM:
            self.log("[plexora-relay] the room does not exist yet (no page has opened it)")
        else:
            self.log(f"[plexora-relay] lost the relay (close code {code})")
        return joined

    def _mark_joined(self):
        if self._joined_now:
            return
        self._joined_now = True
        self.joins += 1
        self.joined.set()
        try:
            self.announce(f"{JOINED_PREFIX} {self.config.room}")
        except Exception:
            pass

    async def _pinger(self, ws):
        next_ping = time.monotonic() + self.ping_interval
        step = max(0.05, min(self.tick, self.ping_interval / 2))
        while not ws.closed:
            await asyncio.sleep(step)
            now = time.monotonic()
            if not self._joined_now and now - self._connected_at >= self.join_grace:
                self._mark_joined()
            if now - self._last_pong > self.pong_timeout:
                self.log(f"[plexora-relay] no pong for {self.pong_timeout:.0f} s; "
                         "treating the socket as dead")
                try:
                    await asyncio.wait_for(ws.close(code=1001, message=b"no pong"), 3)
                except BaseException:
                    pass
                return
            if now >= next_ping:
                next_ping = now + self.ping_interval
                self._spawn(self._ping(ws))

    async def _ping(self, ws):
        try:
            await ws.send_str("ping")
        except Exception:
            pass

    # -- incoming -----------------------------------------------------------

    def _on_text(self, text):
        if text == "pong":
            self._last_pong = time.monotonic()
            return
        presence = wire.parse_presence(text)
        if presence is None:
            return
        self._mark_joined()
        self._pages = presence["pages"]
        if self._pages > 0:
            self._last_page = time.monotonic()

    def _on_binary(self, data):
        try:
            pid, payload = wire.split_pid(data)
            frame = wire.open_frame(self.config.key, self.config.room, wire.P2N, payload)
        except wire.RelayWireError as exc:
            self._bad_frame(exc)
            return
        self._last_page = time.monotonic()
        kind, stream, header = frame.kind, frame.stream, frame.header
        if kind == wire.HELLO:
            client = header.get("client")
            if _good_client(client):
                self._client_at(client, pid)
            self._spawn(self._send_quiet(pid, wire.HELLO, 0, self.hello))
        elif kind == wire.REQUEST:
            self._on_request(pid, stream, header, frame.body)
        elif kind == wire.REQUEST_BODY:
            self._on_request_body(pid, stream, header, frame.body)
        elif kind == wire.CANCEL:
            client = self._pid_client.get(pid)
            exchange = self._exchanges.get((client, stream)) if client else None
            if exchange is not None:
                exchange.cancel()
                self._exchanges.pop(exchange.key, None)
        elif kind == wire.STOP:
            self._end("the page asked this node to stop")
        # Anything else is a kind from a newer page; ignored.

    def _bad_frame(self, exc):
        self._bad_frames += 1
        now = time.monotonic()
        if now - self._last_bad_log > 60:
            self._last_bad_log = now
            self.log(f"[plexora-relay] dropped a frame that did not open ({exc}); "
                     f"{self._bad_frames} so far")

    def _client_at(self, client, pid):
        """Record that `client` now speaks from `pid`; what it had in flight
        from an older pid waits for the page to ask again."""
        previous = self._clients.get(client)
        self._clients[client] = pid
        self._pid_client[pid] = client
        if previous is None or previous == pid:
            return
        if self._pid_client.get(previous) == client:
            del self._pid_client[previous]
        for exchange in list(self._exchanges.values()):
            if exchange.client == client and exchange.target not in (None, pid):
                self._interrupt(exchange)

    def _interrupt(self, exchange):
        """The way to the page that asked is gone. A read, or anything not yet
        sent whole, is dropped (the page sends it again); anything else keeps
        running and waits for the page to ask again."""
        if exchange.reads or not exchange.upload_done:
            exchange.cancel()
            self._exchanges.pop(exchange.key, None)
        else:
            exchange.pause()

    def _on_node_disconnected(self):
        for exchange in list(self._exchanges.values()):
            if exchange.target is not None or not exchange.upload_done:
                self._interrupt(exchange)

    def _on_request(self, pid, stream, header, body):
        client = header.get("client")
        if not _good_client(client) or stream == 0:
            return
        self._client_at(client, pid)
        key = (client, stream)
        method = header.get("m")
        path = header.get("p")
        existing = self._exchanges.get(key)
        if existing is not None:
            if existing.reads or not existing.upload_done:
                # A read runs again; an upload that never finished never ran.
                existing.cancel()
                del self._exchanges[key]
            elif existing.replayable:
                # Already running or answered: never twice. Send it from the
                # start to where the page is now.
                existing.target = pid
                existing.cursor = 0
                existing.epoch += 1
                existing.paused_at = None
                self._ensure_delivery(existing)
                return
            else:
                # Remembered as answered until the copy would have expired,
                # so every further repeat is refused the same way.
                existing.cancel()
                existing.base, existing.size, existing.target = existing.total, 0, None
                existing.frames.clear()
                if not existing.done:
                    existing.done = True
                    existing.finished_at = time.monotonic()
                self._spawn(self._send_error(
                    pid, stream, "this request's answer was too large to keep for "
                                 "a resend, and it is not run twice"))
                return
        problem = _request_problem(method, path, header)
        if problem:
            self._spawn(self._send_error(pid, stream, problem, status=400))
            return
        headers = []
        for item in header.get("h") or []:
            name, value = item
            if name.lower() not in DROP_REQUEST_HEADERS:
                headers.append((name, value))
        if not any(name.lower() == TOKEN_HEADER.lower() for name, _ in headers):
            headers.append((TOKEN_HEADER, self.token))
        exchange = _Exchange(self, client, stream, method, path, headers, pid)
        self._exchanges[key] = exchange
        self._add_upload(exchange, body, header.get("end") is not False)
        self._ensure_delivery(exchange)

    def _on_request_body(self, pid, stream, header, body):
        client = self._pid_client.get(pid)
        exchange = self._exchanges.get((client, stream)) if client else None
        if exchange is None or exchange.upload_done:
            # The tail of a request answered from a copy, or of one cancelled.
            return
        self._add_upload(exchange, body, header.get("end") is True)

    def _add_upload(self, exchange, body, end):
        exchange.upload += body
        if len(exchange.upload) > self.max_request_body:
            exchange.upload_done = True
            exchange.upload = bytearray()
            exchange.push(wire.RESPONSE_HEAD, {"s": 413, "h": [["Content-Type", "text/plain; charset=utf-8"]]})
            exchange.push(wire.RESPONSE_BODY, {"end": True, "error": "the request body is too large"})
            self._finish(exchange)
            return
        if end:
            exchange.upload_done = True
            exchange.producer = self._spawn(self._execute(exchange))

    # -- executing ----------------------------------------------------------

    async def _execute(self, exchange):
        import aiohttp
        from multidict import CIMultiDict

        url = f"http://{_url_host(self.host)}:{self.port}{exchange.path}"
        body = bytes(exchange.upload)
        exchange.upload = bytearray()
        try:
            from yarl import URL

            async with self._http.request(
                    exchange.method, URL(url, encoded=True),
                    headers=CIMultiDict(exchange.headers),
                    data=body if (body or not exchange.reads) else None,
                    allow_redirects=False,
                    skip_auto_headers=("Accept-Encoding",)) as response:
                head = [[_latin1(name), _latin1(value)] for name, value in response.raw_headers
                        if _latin1(name).lower() not in DROP_RESPONSE_HEADERS]
                exchange.push(wire.RESPONSE_HEAD, {"s": response.status, "h": head})
                exchange.head_sent = True
                content = response.content
                while True:
                    chunk = await content.read(wire.MAX_CHUNK)
                    if not chunk:
                        exchange.push(wire.RESPONSE_BODY, {"end": True})
                        break
                    while len(chunk) < wire.MAX_CHUNK:
                        more = content.read_nowait(wire.MAX_CHUNK - len(chunk))
                        if not more:
                            break
                        chunk += more
                    end = content.at_eof()
                    exchange.push(wire.RESPONSE_BODY, {"end": end}, chunk)
                    if end:
                        break
                    await exchange.space.wait()
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError) as exc:
            if not exchange.head_sent:
                exchange.push(wire.RESPONSE_HEAD, {
                    "s": 502, "h": [["Content-Type", "text/plain; charset=utf-8"]]})
            exchange.push(wire.RESPONSE_BODY, {
                "end": True, "error": f"the node's server: {_describe(exc)}"})
        self._finish(exchange)

    def _finish(self, exchange):
        exchange.done = True
        exchange.finished_at = time.monotonic()
        exchange.wake.set()
        if not exchange.reads:
            self._fit_cache()

    def _ensure_delivery(self, exchange):
        exchange.wake.set()
        if exchange.deliverer is None or exchange.deliverer.done():
            exchange.deliverer = self._spawn(self._deliver(exchange))

    async def _deliver(self, exchange):
        while True:
            if exchange.target is None or self._ws is None:
                exchange.wake.clear()
                await exchange.wake.wait()
                continue
            if exchange.cursor < exchange.total:
                if exchange.cursor < exchange.base:
                    # Frames already let go of: cannot start over.
                    exchange.pause()
                    continue
                kind, header, body = exchange.frames[exchange.cursor - exchange.base]
                epoch = exchange.epoch
                try:
                    await self._send(exchange.target, kind, exchange.stream, header, body)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The socket went: the reconnect pauses or drops this.
                    exchange.wake.clear()
                    if exchange.epoch == epoch:
                        await exchange.wake.wait()
                    continue
                if exchange.epoch == epoch:
                    exchange.sent_one()
                continue
            if exchange.done:
                if exchange.reads and self._exchanges.get(exchange.key) is exchange:
                    del self._exchanges[exchange.key]
                return
            exchange.wake.clear()
            await exchange.wake.wait()

    # -- outgoing -----------------------------------------------------------

    async def _send(self, pid, kind, stream, header, body=b""):
        ws = self._ws
        if ws is None or ws.closed:
            raise _NotConnected("not connected to the relay")
        sealed = wire.seal_frame(self.config.key, self.config.room, wire.N2P,
                                 kind, stream, header, body)
        async with self._send_lock:
            if ws is not self._ws or ws.closed:
                raise _NotConnected("not connected to the relay")
            await ws.send_bytes(wire.with_pid(pid, sealed))

    async def _send_quiet(self, pid, kind, stream, header, body=b""):
        try:
            await self._send(pid, kind, stream, header, body)
        except Exception:
            pass

    async def _send_error(self, pid, stream, message, status=502):
        await self._send_quiet(pid, wire.RESPONSE_HEAD, stream, {
            "s": status, "h": [["Content-Type", "text/plain; charset=utf-8"]]})
        await self._send_quiet(pid, wire.RESPONSE_BODY, stream,
                               {"end": True, "error": message})

    # -- housekeeping -------------------------------------------------------

    def _fit_cache(self):
        """Answers kept for a resend stay under `cache_bytes`, oldest let go
        first. One let go is remembered as answered, so a resend is refused
        rather than run a second time."""
        kept = [e for e in self._exchanges.values()
                if not e.reads and e.done and e.replayable and e.size]
        total = sum(e.size for e in kept)
        for exchange in sorted(kept, key=lambda e: e.finished_at):
            if total <= self.cache_bytes:
                break
            if exchange.target is not None and not exchange.delivered:
                continue
            total -= exchange.size
            exchange.base = exchange.total
            exchange.frames.clear()
            exchange.size = 0

    async def _janitor(self):
        while True:
            await asyncio.sleep(self.tick)
            now = time.monotonic()
            for exchange in list(self._exchanges.values()):
                expired = (exchange.done and not exchange.reads
                           and now - exchange.finished_at > self.cache_ttl
                           and (exchange.target is None or exchange.delivered))
                abandoned = (exchange.target is None and exchange.paused_at is not None
                             and now - exchange.paused_at > self.cache_ttl)
                if expired or abandoned:
                    exchange.cancel()
                    self._exchanges.pop(exchange.key, None)

    async def _idle_watch(self):
        if self.config.idle_exit <= 0:
            return
        while True:
            await asyncio.sleep(min(self.tick, self.config.idle_exit))
            now = time.monotonic()
            if self._ws is not None and self._pages > 0:
                self._last_page = now
                continue
            if now - self._last_page >= self.config.idle_exit:
                self._end(f"no page for {self.config.idle_exit:g} s")
                return

    def _end(self, reason):
        """Stop the node, then this link."""
        if self.end_reason is not None:
            return
        self.end_reason = reason
        self.log(f"[plexora-relay] {reason}; stopping the node.")
        try:
            self.shutdown(reason)
        except Exception:
            _log.exception("relay shutdown hook failed")
        self._stop_requested.set()
        if self._stop_evt is not None:
            self._stop_evt.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            self._spawn(self._close(ws))

    async def _close(self, ws):
        try:
            await asyncio.wait_for(ws.close(code=1000, message=b"node stopping"), 3)
        except Exception:
            pass

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _good_client(client):
    return isinstance(client, str) and 0 < len(client) <= _CLIENT_MAX


def _request_problem(method, path, header):
    if not isinstance(method, str) or not _METHOD_RE.match(method):
        return f"not a method: {method!r}"
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return "the path must start with a single /"
    if any(ch in path for ch in "\r\n\t ") or any(ord(ch) > 0x7E or ord(ch) < 0x21 for ch in path):
        return "the path must be URL-encoded ASCII"
    headers = header.get("h") or []
    if not isinstance(headers, list):
        return "h must be a list of [name, value] pairs"
    for item in headers:
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(part, str) for part in item)
                or not item[0] or any(ch in item[0] + item[1] for ch in "\r\n\0")):
            return "h must be a list of [name, value] pairs"
    return None


def _latin1(value):
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return value


def _url_host(host):
    return f"[{host}]" if ":" in host else host


def _describe(exc):
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def relay_target_host(bound_host):
    """Where the link reaches the node's own server: loopback, unless the node
    bound one particular other address."""
    if bound_host in ("", "0.0.0.0", "127.0.0.1", "localhost"):
        return "127.0.0.1"
    if bound_host == "::":
        return "::1"
    return bound_host
