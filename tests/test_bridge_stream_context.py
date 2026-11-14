"""A long answer the bridge leaves to `read` keeps its Flask contexts to itself.

On the website one Python thread answers every request (plexora/browser/
bridge.py). A body longer than `CHUNK` is handed back in parts, and the page's
reads of it wait in the worker's queue behind whatever else arrived, so other
requests are answered between them. A route that streams with
`stream_with_context` (`/fetch_file`) pushes its app and request context each
time its generator runs; those must not stay pushed for the requests answered
in between, and two such downloads at once must both finish.

Each test runs in a contextvars.Context of its own, the way the worker's one
thread would run every call, so nothing it leaves pushed reaches other tests.
"""

import contextvars

import flask
import pytest
from flask import Flask, Response, g, stream_with_context

import plexora
from plexora.browser import bridge
from plexora.server.routes import transfer_routes

ORIGIN = "https://viewer.plexoraapp.com"


@pytest.fixture
def app(monkeypatch):
    app = plexora.app
    monkeypatch.setitem(app.config, "PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setitem(app.config, "PLEXORA_TRUSTED_HOSTS", None)
    monkeypatch.setitem(app.config, "PLEXORA_ALLOWED_ORIGINS", [ORIGIN])
    monkeypatch.setattr(bridge, "CHUNK", 1000)
    monkeypatch.setattr(transfer_routes, "CHUNK", 100)
    return app


def _isolated(scenario):
    """Run `scenario` in a fresh Context; close whatever streams it left."""
    before = set(bridge._streams)

    def run():
        try:
            return scenario()
        finally:
            for stream in sorted(set(bridge._streams) - before, reverse=True):
                try:
                    bridge.close(stream)
                except Exception:
                    bridge._streams.pop(stream, None)

    return contextvars.Context().run(run)


def _fetch(app, path):
    import json

    return bridge.handle(app, {"method": "POST", "path": "/fetch_file",
                               "headers": {"Content-Type": "application/json"},
                               "body": json.dumps({"path": str(path)}).encode()}, ORIGIN)


def _drain(answer):
    parts, done = [answer["body"]], answer["stream"] is None
    while not done:
        more = bridge.read(answer["stream"], 1000)
        parts.append(more["body"])
        done = more["done"]
    return b"".join(parts)


def test_no_request_context_stays_pushed_while_a_long_download_waits_for_its_reads(app, tmp_path):
    whole = bytes(range(256)) * 20
    path = tmp_path / "table.csv"
    path.write_bytes(whole)

    def scenario():
        answer = _fetch(app, path)
        assert answer["status"] == 200 and answer["stream"] is not None
        # Between the answer and the page's next read the thread answers
        # other requests: none of them may find /fetch_file's contexts there.
        left = (flask.has_request_context(), flask.has_app_context())
        body = _drain(answer)
        return left, body

    left, body = _isolated(scenario)
    assert body == whole
    assert left == (False, False), (
        "after handle() returned a streamed /fetch_file, the thread still has "
        f"Flask's request/app context pushed: {left}")


def test_a_request_answered_between_reads_does_not_share_the_streams_g():
    """flask.g of a request served while another's body is still being read
    is its own, not the streaming request's."""
    toy = Flask("bridge_stream_toy")

    @toy.route("/big/<tag>")
    def big(tag):
        g.tag = tag

        def chunks():
            for _ in range(30):
                yield b"x" * 100

        return Response(stream_with_context(chunks()))

    @toy.route("/small")
    def small():
        seen = g.get("tag", "")
        g.tag = "small"
        return seen

    def scenario():
        answer = bridge.handle(toy, {"method": "GET", "path": "/big/A"}, ORIGIN)
        assert answer["stream"] is not None
        seen_first = bridge.handle(toy, {"method": "GET", "path": "/small"}, ORIGIN)["body"]
        seen_second = bridge.handle(toy, {"method": "GET", "path": "/small"}, ORIGIN)["body"]
        _drain(answer)
        return seen_first, seen_second

    old = bridge.CHUNK
    bridge.CHUNK = 1000
    try:
        seen_first, seen_second = _isolated(scenario)
    finally:
        bridge.CHUNK = old
    assert seen_first == b"", f"/small saw the streaming request's g.tag: {seen_first!r}"
    assert seen_second == b"", f"/small saw the previous /small's g.tag: {seen_second!r}"


@pytest.mark.parametrize("order", ["first_started_first", "last_started_first"])
def test_two_long_downloads_at_once_both_finish(app, tmp_path, order):
    one = bytes(range(256)) * 20
    two = bytes(reversed(range(256))) * 24
    (tmp_path / "one.bin").write_bytes(one)
    (tmp_path / "two.bin").write_bytes(two)

    def scenario():
        a = _fetch(app, tmp_path / "one.bin")
        b = _fetch(app, tmp_path / "two.bin")
        assert a["stream"] is not None and b["stream"] is not None
        if order == "first_started_first":
            return _drain(a), _drain(b)
        got_b = _drain(b)
        return _drain(a), got_b

    got_one, got_two = _isolated(scenario)
    assert got_one == one
    assert got_two == two
