"""A request from the page, answered by the Flask app, with no socket between.

On the website the app runs in a Web Worker. The page's fetch facade
(`services/backend.js`) hands a request it classed ``python`` to the worker
(`workers/pythonWorker.js`), which calls `handle` here: the request becomes a
WSGI environ, the app answers it exactly as Waitress would have it answer, and
the status, headers and body go back as plain values. Every hook the app
installs runs -- the browser guard, the licence, telemetry's timing, the
`_work` boundary that runs queued jobs between requests.

The request says it came from the page's own origin to the page's own host,
from loopback: the same request a browser makes to a desktop server it
started, which is what the guard is built to admit.

A body longer than `CHUNK` is not copied into one message: the first chunk
goes back with a `stream` id, and the worker pulls the rest with `read`
(a figure's PDF, a project bundle), so a large download never sits in memory
twice. Under CPython all of this is tested as it is (tests/test_bridge.py).
"""

from __future__ import annotations

import itertools
from urllib.parse import urlsplit

#: Bytes sent with an answer before the rest is left to `read`.
CHUNK = 8 * 2**20

#: Methods after which the worker writes its project store back to disk.
_READS = ("GET", "HEAD", "OPTIONS")

_streams: dict = {}
_ids = itertools.count(1)


def environ(request: dict, origin: str) -> dict:
    """The WSGI environ for `request`: `{method, path, query, headers, body}`,
    from `origin` (the page's, e.g. https://viewer.plexoraapp.com)."""
    from werkzeug.test import EnvironBuilder

    page = urlsplit(origin)
    headers = [(str(name), str(value)) for name, value in _pairs(request.get("headers"))]
    names = {name.lower() for name, _value in headers}
    if "origin" not in names and request.get("method", "GET").upper() not in _READS:
        # A browser sends Origin with every write; the facade's own calls
        # (a fetch made by the page) carry it already, a test's may not.
        headers.append(("Origin", origin))
    body = request.get("body") or b""
    if not isinstance(body, (bytes, str)):
        # From the worker a body arrives as a buffer (a memoryview, a
        # Uint8Array's proxy), which Werkzeug would read as form fields.
        body = bytes(body)
    builder = EnvironBuilder(
        path=request.get("path") or "/",
        base_url=f"{page.scheme}://{page.netloc}",
        query_string=request.get("query") or "",
        method=(request.get("method") or "GET").upper(),
        headers=headers,
        data=body,
        environ_base={"REMOTE_ADDR": "127.0.0.1", "SERVER_SOFTWARE": "plexora-browser"},
    )
    try:
        return builder.get_environ()
    finally:
        builder.close()


def handle(app, request: dict, origin: str) -> dict:
    """`{status, headers, body, stream, wrote}` for one request.

    `headers` is a list of [name, value] (a header may repeat); `body` is
    bytes, at most `CHUNK` of them; `stream` is None or the id `read` takes
    for the rest; `wrote` says the request may have changed files, so the
    worker writes its store back."""
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = int(status.split(" ", 1)[0])
        captured["headers"] = [[name, value] for name, value in headers]
        return lambda data: captured.setdefault("written", []).append(bytes(data))

    method = (request.get("method") or "GET").upper()
    iterable = app.wsgi_app(environ(request, origin), start_response)
    body, rest = _first(iterable, CHUNK)
    if captured.get("written"):
        body = b"".join(captured["written"]) + body
    stream = None
    if rest is not None:
        stream = next(_ids)
        _streams[stream] = rest
    return {"status": captured.get("status", 500), "headers": captured.get("headers", []),
            "body": body, "stream": stream, "wrote": method not in _READS}


def read(stream: int, size: int = CHUNK) -> dict:
    """`{body, done}`: the next part of a body `handle` left behind."""
    held = _streams.get(stream)
    if held is None:
        return {"body": b"", "done": True}
    body, rest = _first(held, size)
    if rest is None:
        _streams.pop(stream, None)
        return {"body": body, "done": True}
    _streams[stream] = rest
    return {"body": body, "done": False}


def close(stream: int) -> None:
    """The page stopped reading: let the app's iterable clean up."""
    held = _streams.pop(stream, None)
    if held is not None:
        held.close()


class _Rest:
    """What is left of a WSGI iterable: a carried-over piece, then the rest."""

    def __init__(self, iterator, source, carry=b""):
        self.iterator, self.source, self.carry = iterator, source, carry

    def close(self):
        closer = getattr(self.source, "close", None)
        if closer is not None:
            closer()


def _first(iterable, size):
    """(up to `size` bytes, a `_Rest` or None when that was all)."""
    if isinstance(iterable, _Rest):
        rest, parts, have = iterable, [iterable.carry] if iterable.carry else [], len(iterable.carry)
        iterator = rest.iterator
    else:
        rest, parts, have = None, [], 0
        iterator = iter(iterable)
    for piece in iterator:
        piece = bytes(piece)
        parts.append(piece)
        have += len(piece)
        if have > size:
            joined = b"".join(parts)
            source = rest.source if rest is not None else iterable
            return joined[:size], _Rest(iterator, source, joined[size:])
    source = rest.source if rest is not None else iterable
    closer = getattr(source, "close", None)
    if closer is not None:
        closer()
    return b"".join(parts), None


def _pairs(headers):
    if not headers:
        return []
    if isinstance(headers, dict):
        return list(headers.items())
    return [(name, value) for name, value in headers]
