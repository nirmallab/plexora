"""The node's relay link (plexora/server/node/relay_link.py), end to end.

Three stand-ins and the real link between them:

- `FakeRelay`: an aiohttp server doing what the relay's Durable Object does
  for a room (docs/internal/WEB_REMOTE_RELAY.md): the subprotocol and auth
  checks, page and node roles, pid prefixing, presence frames, ping -> pong.
- `FakePage`: the website's client, written with relay_wire, including the
  retry rule -- pending requests are cleared and sent again with the same
  stream id once both ends are back.
- `Origin`: a small real HTTP server standing in for the node's own, which
  counts what it executes.
"""

import asyncio
import hashlib
import hmac
import json
import os
import random
import select
import signal
import socket
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web

from plexora import relay_wire as wire
from plexora.server.node import relay_link

TOKEN = "node-token-0123456789"
PATTERN = bytes(range(251))


# -- the node's own server -----------------------------------------------------


class Origin:
    def __init__(self):
        self.counts = Counter()
        self.seen = []
        self.entered = {}
        self.gates = {}
        self.aborted = threading.Event()
        self.hang_entered = threading.Event()
        origin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, status, body, content_type="application/octet-stream"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                origin.counts[parts.path] += 1
                origin.seen.append({"method": self.command, "path": self.path,
                                    "headers": {k.lower(): v for k, v in self.headers.items()},
                                    "body": body})
                if parts.path == "/big":
                    n = int(parse_qs(parts.query)["n"][0])
                    data = (PATTERN * (n // len(PATTERN) + 1))[:n]
                    return self._reply(200, data)
                if parts.path == "/echo":
                    answer = {"len": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                              "token": self.headers.get("X-Plexora-Node-Token"),
                              "count": origin.counts[parts.path]}
                    return self._reply(200, json.dumps(answer).encode(), "application/json")
                if parts.path.startswith("/gate/"):
                    name = parts.path[len("/gate/"):]
                    count = origin.counts[parts.path]
                    origin.entered.setdefault(name, threading.Event()).set()
                    origin.gates.setdefault(name, threading.Event()).wait(20)
                    return self._reply(200, f"done:{name}:{count}".encode(), "text/plain")
                if parts.path == "/hang":
                    origin.hang_entered.set()
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        ready, _, _ = select.select([self.connection], [], [], 0.05)
                        if ready and self.connection.recv(1, socket.MSG_PEEK) == b"":
                            origin.aborted.set()
                            self.close_connection = True
                            return None
                    return self._reply(200, b"never cancelled")
                return self._reply(404, b"no such path", "text/plain")

            do_GET = do_POST = do_HEAD = do_PUT = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def gate(self, name):
        return self.gates.setdefault(name, threading.Event())

    def entered_event(self, name):
        return self.entered.setdefault(name, threading.Event())

    def close(self):
        for gate in list(self.gates.values()):
            gate.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def origin():
    server = Origin()
    yield server
    server.close()


# -- the relay ---------------------------------------------------------------


class Room:
    def __init__(self, digest):
        self.digest = digest
        self.node = None
        self.pages = {}


class FakeRelay:
    def __init__(self):
        self.rooms = {}
        self.swallow_pings = False
        self.node_connects = 0
        self.node_offers = []
        self.url = None
        self._runner = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/v1/rooms/{room}", self.handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def close(self):
        for room in self.rooms.values():
            for ws in [room.node, *room.pages.values()]:
                if ws is not None:
                    await ws.close()
        await self._runner.cleanup()

    async def handler(self, request):
        room_id = request.match_info["room"]
        role = request.query.get("role")
        if not wire.ROOM_RE.match(room_id) or role not in ("page", "node"):
            return web.Response(status=400)
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
                   if p.strip()]
        auths = [p[2:] for p in offered if p.startswith("a.")]
        if wire.PROTOCOL not in offered or len(auths) != 1:
            return web.Response(status=400)
        if role == "node":
            self.node_offers.append(offered)
        ws = web.WebSocketResponse(protocols=(wire.PROTOCOL,), max_msg_size=33 << 20)
        await ws.prepare(request)
        digest = hashlib.sha256(auths[0].encode()).hexdigest()
        room = self.rooms.get(room_id)
        if room is None:
            if role == "node":
                await ws.close(code=4404, message=b"no such room")
                return ws
            room = self.rooms[room_id] = Room(digest)
        if not hmac.compare_digest(room.digest, digest):
            await ws.close(code=4401, message=b"wrong auth")
            return ws
        pid = None
        if role == "node":
            previous, room.node = room.node, ws
            self.node_connects += 1
            if previous is not None:
                await previous.close(code=4000, message=b"replaced")
        else:
            if len(room.pages) >= 8:
                await ws.close(code=4429, message=b"full")
                return ws
            pid = os.urandom(8)
            room.pages[pid] = ws
        await self.presence(room)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if msg.data == "ping" and not self.swallow_pings:
                        await ws.send_str("pong")
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    if role == "page":
                        if room.node is not None:
                            await _quiet_send(room.node, pid + msg.data)
                    elif ws is room.node:
                        target, payload = wire.split_pid(msg.data)
                        targets = (list(room.pages.values()) if target == wire.ALL
                                   else [room.pages[target]] if target in room.pages else [])
                        for page in targets:
                            await _quiet_send(page, payload)
        finally:
            if role == "node" and room.node is ws:
                room.node = None
            elif role == "page":
                room.pages.pop(pid, None)
            await self.presence(room)
        return ws

    async def presence(self, room):
        text = json.dumps({"t": "presence", "node": room.node is not None,
                           "pages": len(room.pages)})
        for ws in [room.node, *room.pages.values()]:
            if ws is not None and not ws.closed:
                try:
                    await ws.send_str(text)
                except Exception:
                    pass

    async def drop_node(self, room_id, code=1012):
        await self.rooms[room_id].node.close(code=code, message=b"restart")


async def _quiet_send(ws, data):
    try:
        await ws.send_bytes(data)
    except Exception:
        pass


# -- the page ------------------------------------------------------------------


class FakePage:
    def __init__(self, relay, room, auth, key, client="client-1"):
        self.url = f"{relay.url}/v1/rooms/{room}?role=page"
        self.room, self.auth, self.key, self.client = room, auth, key, client
        self.pending = {}
        self.next_stream = random.randint(1, 1 << 30)
        self.node_hellos = []
        self.presence = None
        self.anomalies = []
        self.strays = []
        self.session = None
        self.ws = None
        self.reader = None

    async def connect(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url, protocols=wire.subprotocols(self.auth))
        assert self.ws.protocol == wire.PROTOCOL
        self.reader = asyncio.ensure_future(self._read(self.ws))
        await self._send(wire.HELLO, 0, {"client": self.client, "v": 1})

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self.reader is not None:
            self.reader.cancel()
        if self.session is not None:
            await self.session.close()

    async def drop(self):
        """The page's socket goes; what it had received is forgotten."""
        await self.ws.close()
        self.reader.cancel()
        self.ws = None
        self._clear_all()

    async def reconnect(self):
        if self.ws is not None:
            await self.drop()
        await self.connect()
        await self._resend()

    def _clear_all(self):
        for entry in self.pending.values():
            entry.update(head=None, chunks=[], stale=True)

    async def _resend(self):
        for stream, entry in list(self.pending.items()):
            if entry["stale"]:
                entry["stale"] = False
                await self._send_request(stream)

    async def _send(self, kind, stream, header, body=b""):
        await self.ws.send_bytes(wire.seal_frame(self.key, self.room, wire.P2N,
                                                 kind, stream, header, body))

    async def _read(self, ws):
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                presence = wire.parse_presence(msg.data)
                if presence is not None:
                    self.presence = presence
                    if not presence["node"]:
                        self._clear_all()
                continue
            if msg.type != aiohttp.WSMsgType.BINARY:
                continue
            frame = wire.open_frame(self.key, self.room, wire.N2P, msg.data)
            if frame.kind == wire.HELLO:
                self.node_hellos.append(frame.header)
                await self._resend()
                continue
            entry = self.pending.get(frame.stream)
            if entry is None or entry["stale"]:
                self.strays.append(frame)
                continue
            if frame.kind == wire.RESPONSE_HEAD:
                if entry["head"] is not None:
                    self.anomalies.append(("second head", frame.stream))
                entry["head"] = frame.header
            elif frame.kind == wire.RESPONSE_BODY:
                if entry["head"] is None:
                    self.anomalies.append(("body before head", frame.stream))
                entry["chunks"].append(frame.body)
                if frame.header.get("end"):
                    del self.pending[frame.stream]
                    entry["future"].set_result({
                        "status": entry["head"]["s"] if entry["head"] else None,
                        "headers": {k.lower(): v for k, v in (entry["head"] or {}).get("h", [])},
                        "body": b"".join(entry["chunks"]),
                        "chunks": [len(c) for c in entry["chunks"]],
                        "error": frame.header.get("error"),
                    })

    def start(self, method, path, headers=(), body=b"", chunk=wire.MAX_CHUNK - 4096):
        stream = self.next_stream
        self.next_stream += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[stream] = {"request": (method, path, [list(h) for h in headers], body, chunk),
                                "future": future, "head": None, "chunks": [], "stale": False}
        return stream, future

    async def send(self, stream):
        await self._send_request(stream)

    async def repeat(self, stream, method, path, headers=(), body=b"", timeout=15):
        """Send a request again under a stream id it was answered on."""
        future = asyncio.get_running_loop().create_future()
        self.pending[stream] = {"request": (method, path, [list(h) for h in headers], body,
                                            wire.MAX_CHUNK - 4096),
                                "future": future, "head": None, "chunks": [], "stale": False}
        await self._send_request(stream)
        return await asyncio.wait_for(future, timeout)

    async def _send_request(self, stream):
        method, path, headers, body, chunk = self.pending[stream]["request"]
        first, rest = body[:chunk], body[chunk:]
        await self._send(wire.REQUEST, stream, {"m": method, "p": path, "h": headers,
                                                "client": self.client, "end": not rest}, first)
        while rest:
            piece, rest = rest[:chunk], rest[chunk:]
            await self._send(wire.REQUEST_BODY, stream, {"end": not rest}, piece)

    async def request(self, method, path, headers=(), body=b"", timeout=15, **kw):
        stream, future = self.start(method, path, headers, body, **kw)
        await self.send(stream)
        return await asyncio.wait_for(future, timeout)

    async def cancel(self, stream):
        self.pending.pop(stream, None)
        await self._send(wire.CANCEL, stream, {})

    async def stop_node(self):
        await self._send(wire.STOP, 0, {})


# -- helpers -------------------------------------------------------------------


async def until(condition, timeout=8.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def wait_event(event, timeout=8.0):
    ok = await asyncio.to_thread(event.wait, timeout)
    assert ok, "timed out waiting for the origin"


class Rig:
    """A relay, a room, a link to `origin` and the record of what it said."""

    def __init__(self, origin, idle_exit=0, **link_options):
        self.origin = origin
        self.relay = FakeRelay()
        self.room, self.auth, self.key = wire.new_room(), wire.new_auth(), wire.new_key()
        self.idle_exit = idle_exit
        self.link_options = link_options
        self.lines = []
        self.shutdowns = []
        self.link = None
        self.pages = []

    async def __aenter__(self):
        await self.relay.start()
        return self

    async def __aexit__(self, *exc):
        if self.link is not None:
            await asyncio.to_thread(self.link.stop)
        for page in self.pages:
            await page.close()
        await self.relay.close()

    def start_link(self):
        config = relay_link.RelayConfig(relay=self.relay.url, room=self.room, auth=self.auth,
                                        key=self.key, idle_exit=self.idle_exit)
        options = dict(ping_interval=0.3, pong_timeout=1.2, backoff_base=0.05,
                       backoff_max=0.2, tick=0.05, join_grace=0.5)
        options.update(self.link_options)
        self.link = relay_link.RelayLink(
            config, port=self.origin.port, token=TOKEN,
            hello={"node_id": "relay-test", "api_version": 1, "token": TOKEN,
                   "version": "test", "hostname": "here", "platform": "linux"},
            shutdown=self.shutdowns.append, announce=self.lines.append,
            log=self.lines.append, **options)
        self.link.start()
        return self.link

    async def page(self, client="client-1"):
        page = FakePage(self.relay, self.room, self.auth, self.key, client=client)
        self.pages.append(page)
        await page.connect()
        return page

    async def joined_page(self, client="client-1"):
        page = await self.page(client)
        if self.link is None:
            self.start_link()
        await until(lambda: page.node_hellos, what="the node's HELLO")
        return page

    def joined_lines(self):
        return [line for line in self.lines if line.startswith(relay_link.JOINED_PREFIX)]


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 60))


# -- joining -------------------------------------------------------------------


def test_the_node_waits_for_its_room_then_joins_and_says_hello(origin):
    async def scenario():
        async with Rig(origin) as rig:
            link = rig.start_link()
            # No page has opened the room yet: the relay says 4404, the node
            # keeps trying.
            await asyncio.sleep(0.4)
            assert not link.joined.is_set()
            page = await rig.page()
            await until(lambda: len(page.node_hellos) >= 1, what="the node's HELLO")
            hello = page.node_hellos[-1]
            assert hello == {"v": 1, "node_id": "relay-test", "api_version": 1,
                             "token": TOKEN, "version": "test", "hostname": "here",
                             "platform": "linux"}
            assert rig.joined_lines() == [f"[plexora-relay] joined {rig.room}"]
            assert link.joins == 1
            assert rig.relay.node_offers[-1] == ["plexora-relay.v1", f"a.{rig.auth}"]
            assert page.presence == {"node": True, "pages": 1}
            # A second page gets a HELLO of its own.
            other = await rig.page("client-2")
            await until(lambda: other.node_hellos, what="the second page's HELLO")
            assert rig.shutdowns == []
            # A clean stop closes the socket, and the relay says so.
            started = time.monotonic()
            await asyncio.to_thread(link.stop)
            assert link.stopped.is_set() and time.monotonic() - started < 3
            await until(lambda: page.presence == {"node": False, "pages": 2},
                        what="the relay to see the node go")
            assert rig.shutdowns == []

    run(scenario())


# -- requests -----------------------------------------------------------------


def test_a_get_streams_a_body_larger_than_a_frame(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            # A frame that does not open is dropped, and nothing else happens.
            await page.ws.send_bytes(b"\x00" * 64)
            n = 3 * wire.MAX_CHUNK + 12345
            answer = await page.request("GET", f"/big?n={n}")
            assert answer["status"] == 200 and answer["error"] is None
            assert answer["body"] == (PATTERN * (n // len(PATTERN) + 1))[:n]
            assert len(answer["chunks"]) >= 4
            assert max(answer["chunks"]) <= wire.MAX_CHUNK
            assert answer["headers"]["content-length"] == str(n)
            assert "transfer-encoding" not in answer["headers"]
            # HEAD: a head and an empty end.
            answer = await page.request("HEAD", f"/big?n={n}")
            assert answer["status"] == 200 and answer["body"] == b""
            assert origin.counts["/big"] == 2
            assert page.anomalies == [] and page.strays == []
            assert any("did not open" in line for line in rig.lines)

    run(scenario())


def test_a_post_body_in_chunks_and_the_token_put_in(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            body = os.urandom(2 * wire.MAX_CHUNK + 777)
            answer = await page.request(
                "POST", "/echo",
                headers=[("Content-Type", "application/octet-stream"),
                         ("Accept-Encoding", "gzip, br"), ("Connection", "keep-alive"),
                         ("Host", "evil.example"), ("Content-Length", "1"),
                         ("X-Custom", "yes")],
                body=body)
            echoed = json.loads(answer["body"])
            assert echoed == {"len": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                              "token": TOKEN, "count": 1}
            seen = origin.seen[-1]["headers"]
            assert "accept-encoding" not in seen
            assert seen["host"] == f"127.0.0.1:{origin.port}"
            assert seen["content-length"] == str(len(body))
            assert seen["x-custom"] == "yes"
            # A token the page sends itself is left alone.
            answer = await page.request("POST", "/echo",
                                        headers=[("X-Plexora-Node-Token", "page-token")],
                                        body=b"x")
            assert json.loads(answer["body"])["token"] == "page-token"
            # A path that is not one is refused without reaching the server.
            answer = await page.request("GET", "//elsewhere/x")
            assert answer["status"] == 400 and answer["error"]
            assert origin.counts["/echo"] == 2

    run(scenario())


def test_cancel_aborts_the_request(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            stream, _future = page.start("GET", "/hang")
            await page.send(stream)
            await wait_event(origin.hang_entered)
            await page.cancel(stream)
            await wait_event(origin.aborted)
            await until(lambda: not rig.link._exchanges, what="the exchange to go")
            await asyncio.sleep(0.2)
            assert page.strays == []
            # And the link carries on.
            answer = await page.request("GET", "/big?n=10")
            assert answer["status"] == 200

    run(scenario())


# -- reconnecting ---------------------------------------------------------------


def test_the_node_reconnects_and_a_pending_get_is_sent_again(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            stream, future = page.start("GET", "/gate/a")
            await page.send(stream)
            await wait_event(origin.entered_event("a"))
            # The relay restarts under the node.
            await rig.relay.drop_node(rig.room)
            await until(lambda: rig.relay.node_connects >= 2, what="the node to come back")
            await until(lambda: origin.counts["/gate/a"] >= 2, what="the GET to be sent again")
            origin.gate("a").set()
            answer = await asyncio.wait_for(future, 10)
            assert answer["status"] == 200 and answer["body"] == b"done:a:2"
            assert origin.counts["/gate/a"] == 2
            assert page.anomalies == []
            assert len(rig.joined_lines()) == 2 and rig.link.joins == 2
            assert rig.shutdowns == []

    run(scenario())


def test_a_socket_without_pongs_is_given_up_and_replaced(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            rig.relay.swallow_pings = True
            await until(lambda: rig.relay.node_connects >= 2, timeout=10,
                        what="the node to give up on a silent socket")
            rig.relay.swallow_pings = False
            assert any("no pong" in line for line in rig.lines)
            await until(lambda: rig.link.connected, what="the new socket")
            answer = await page.request("GET", "/big?n=100")
            assert answer["status"] == 200

    run(scenario())


def test_a_post_answered_while_the_page_was_away_comes_from_the_copy(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            first_pid_count = len(rig.relay.rooms[rig.room].pages)
            stream, future = page.start("POST", "/gate/p", body=b"payload")
            await page.send(stream)
            await wait_event(origin.entered_event("p"))
            await page.drop()
            origin.gate("p").set()
            key = (page.client, stream)
            await until(lambda: key in rig.link._exchanges and rig.link._exchanges[key].done,
                        what="the node to finish the POST")
            # Back with a new pid; the same stream again.
            await page.reconnect()
            answer = await asyncio.wait_for(future, 10)
            assert answer["body"] == b"done:p:1"
            assert origin.counts["/gate/p"] == 1
            assert page.anomalies == []
            assert first_pid_count == 1

    run(scenario())


def test_a_post_still_running_when_the_page_returns_is_not_run_twice(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            stream, future = page.start("POST", "/gate/q", body=b"payload")
            await page.send(stream)
            await wait_event(origin.entered_event("q"))
            await page.reconnect()
            await asyncio.sleep(0.3)
            assert origin.counts["/gate/q"] == 1
            origin.gate("q").set()
            answer = await asyncio.wait_for(future, 10)
            assert answer["body"] == b"done:q:1"
            assert origin.counts["/gate/q"] == 1
            assert page.anomalies == []

    run(scenario())


def test_a_get_is_simply_run_again_after_a_page_reconnect(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            stream, future = page.start("GET", "/gate/g")
            await page.send(stream)
            await wait_event(origin.entered_event("g"))
            await page.reconnect()
            await until(lambda: origin.counts["/gate/g"] >= 2, what="the GET to run again")
            origin.gate("g").set()
            answer = await asyncio.wait_for(future, 10)
            assert answer["body"] == b"done:g:2"
            assert page.anomalies == []

    run(scenario())


@pytest.mark.parametrize("options, expected", [
    ({}, "copy"),
    ({"cache_bytes": 8}, "refused"),
    ({"cache_ttl": 0.2}, "run again"),
])
def test_a_repeat_of_an_answered_post(origin, options, expected):
    """Kept for two minutes and answered from the copy; one too large to keep
    is refused rather than run twice; one past its time is a new request."""
    async def scenario():
        async with Rig(origin, **options) as rig:
            page = await rig.joined_page()
            stream, future = page.start("POST", "/echo", body=b"once")
            await page.send(stream)
            first = await asyncio.wait_for(future, 10)
            assert json.loads(first["body"])["count"] == 1
            if expected == "run again":
                await until(lambda: not rig.link._exchanges, what="the copy to expire")
            again = await page.repeat(stream, "POST", "/echo", body=b"once")
            if expected == "copy":
                assert again["body"] == first["body"] and again["status"] == 200
                assert origin.counts["/echo"] == 1
            elif expected == "refused":
                assert again["status"] == 502 and "too large" in again["error"]
                assert origin.counts["/echo"] == 1
            else:
                assert json.loads(again["body"])["count"] == 2
            assert page.anomalies == []

    run(scenario())


# -- ending -------------------------------------------------------------------------


def test_idle_exit_stops_the_node_once_no_page_is_left(origin):
    async def scenario():
        async with Rig(origin, idle_exit=0.6) as rig:
            page = await rig.joined_page()
            await asyncio.sleep(1.0)
            assert rig.shutdowns == []
            await page.drop()
            await until(lambda: rig.shutdowns, timeout=5, what="idle exit")
            assert "no page" in rig.shutdowns[0]
            await asyncio.to_thread(rig.link.stopped.wait, 5)
            assert rig.link.stopped.is_set()

    run(scenario())


def test_stop_from_the_page_stops_the_node(origin):
    async def scenario():
        async with Rig(origin) as rig:
            page = await rig.joined_page()
            await page.stop_node()
            await until(lambda: rig.shutdowns, what="the shutdown")
            assert rig.shutdowns == ["the page asked this node to stop"]
            await asyncio.to_thread(rig.link.stopped.wait, 5)
            assert rig.link.stopped.is_set()
            await until(lambda: page.presence == {"node": False, "pages": 1},
                        what="the relay to see the node go")

    run(scenario())


def test_another_node_in_the_room_ends_this_one(origin):
    async def scenario():
        async with Rig(origin) as rig:
            await rig.joined_page()
            await rig.relay.drop_node(rig.room, code=4000)
            await until(lambda: rig.shutdowns, what="the shutdown")
            assert "another node" in rig.shutdowns[0]

    run(scenario())


# -- the relay file --------------------------------------------------------------


def _relay_json(**changes):
    data = {"v": 1, "relay": "wss://relay.plexoraapp.com/", "room": wire.new_room(),
            "auth": wire.new_auth(), "key": wire.key_to_b64(wire.new_key()),
            "idle_exit": 1800}
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


def test_a_good_relay_file_is_read_then_deleted(tmp_path):
    data = _relay_json()
    path = tmp_path / "room.json"
    path.write_text(json.dumps(data))
    config = relay_link.read_relay_file(path)
    assert not path.exists()
    assert config.relay == "wss://relay.plexoraapp.com"
    assert config.room == data["room"] and config.auth == data["auth"]
    assert config.key == wire.key_from_b64(data["key"])
    assert config.idle_exit == 1800
    assert config.room_url == f"wss://relay.plexoraapp.com/v1/rooms/{data['room']}?role=node"
    # Never the secrets in a repr.
    assert data["auth"] not in repr(config) and data["key"] not in repr(config)
    # idle_exit may be left out: never.
    assert relay_link.parse_relay_config(_relay_json(idle_exit=None)).idle_exit == 0


@pytest.mark.parametrize("text, words", [
    ("not json", "not JSON"),
    ("[1]", "JSON object"),
    (json.dumps(_relay_json(v=2)), "version"),
    (json.dumps(_relay_json(relay="https://relay.plexoraapp.com")), "wss://"),
    (json.dumps(_relay_json(room="short")), '"room"'),
    (json.dumps(_relay_json(auth="x" * 10)), '"auth"'),
    (json.dumps(_relay_json(key=wire.b64url_encode(bytes(16)))), '"key"'),
    (json.dumps(_relay_json(idle_exit=-1)), '"idle_exit"'),
    (json.dumps(_relay_json(idle_exit=True)), '"idle_exit"'),
])
def test_a_bad_relay_file_is_refused_and_left_in_place(tmp_path, text, words):
    path = tmp_path / "room.json"
    path.write_text(text)
    with pytest.raises(relay_link.RelayConfigError) as caught:
        relay_link.read_relay_file(path)
    assert words in str(caught.value) and str(path) in str(caught.value)
    assert path.exists()


def test_a_missing_relay_file_is_refused(tmp_path):
    with pytest.raises(relay_link.RelayConfigError, match="no relay file"):
        relay_link.read_relay_file(tmp_path / "absent.json")


# -- `plexora node serve --relay-file` --------------------------------------------


def test_the_flag_is_on_node_serve():
    from plexora import cli

    args = cli._build_node_parser().parse_args(["serve", "--relay-file", "/tmp/r.json"])
    assert args.relay_file == "/tmp/r.json"
    assert cli._build_node_parser().parse_args(["serve"]).relay_file is None


def test_serve_node_refuses_a_bad_relay_file_before_anything_else(tmp_path, monkeypatch):
    from plexora.server.node import app as node_app

    def never(*args, **kwargs):
        raise AssertionError("got past the relay file")

    monkeypatch.setattr(node_app, "create_node_app", never)
    path = tmp_path / "room.json"
    path.write_text("{}")
    with pytest.raises(node_app.NodeStartupError, match="--relay-file"):
        node_app.serve_node([], token=TOKEN, relay_file=path)
    path.write_text(json.dumps(_relay_json()))
    with pytest.raises(node_app.NodeStartupError, match="exit-on-stdin-close"):
        node_app.serve_node([], token=TOKEN, relay_file=path, exit_on_stdin_close=True)


def test_serve_node_starts_the_link_once_bound_and_stops_it_after(tmp_path, monkeypatch):
    import waitress

    from plexora.server.models import data_model
    from plexora.server.node import app as node_app

    events = []
    made = {}

    class Server:
        effective_port = 54321

        def print_listen(self, fmt):
            events.append("listen")

        def run(self):
            events.append("run")

    def create_server(app, **options):
        events.append("bound")
        made["options"] = options
        return Server()

    class Link:
        def __init__(self, config, **kwargs):
            made["config"], made["link"] = config, kwargs
            events.append("link")

        def start(self):
            events.append("link-start")
            return self

        def stop(self):
            events.append("link-stop")

    monkeypatch.setattr(data_model, "prime_hot_code", lambda log=None: None)
    monkeypatch.setattr(node_app, "warm_resources", lambda *a, **k: None)
    monkeypatch.setattr(waitress, "create_server", create_server)
    monkeypatch.setattr(relay_link, "RelayLink", Link)
    monkeypatch.delenv("PLEXORA_NODE_ORIGINS", raising=False)

    data = _relay_json()
    path = tmp_path / "room.json"
    path.write_text(json.dumps(data))
    term_before = signal.getsignal(signal.SIGTERM)
    lines = []
    node_app.serve_node([], token=TOKEN, port=0, node_id="relay-test", dynamic=True,
                        manifest=tmp_path / "m.json", relay_file=path,
                        log=lambda line="", **kw: lines.append(str(line)))

    assert not path.exists()
    assert events == ["bound", "listen", "link", "link-start", "run", "link-stop"]
    assert made["options"]["port"] == 0
    assert made["link"]["port"] == 54321 and made["link"]["token"] == TOKEN
    assert made["link"]["host"] == "127.0.0.1"
    assert made["config"].room == data["room"]
    hello = made["link"]["hello"]
    assert hello["node_id"] == "relay-test" and hello["token"] == TOKEN
    assert set(hello) == {"node_id", "api_version", "token", "version", "hostname", "platform"}
    # The announce is what it always was.
    announce = [line for line in lines if line.startswith("[plexora-node]")]
    assert len(announce) == 1
    assert announce[0].startswith(
        f"[plexora-node] host=127.0.0.1 port=0 node_id=relay-test token={TOKEN} hostname=")
    assert signal.getsignal(signal.SIGTERM) == term_before
