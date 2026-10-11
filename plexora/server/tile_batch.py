"""Several tiles in one request, for a browser on a slow link.

A browser keeps six HTTP/1.1 connections to a server, and each tile is one
request: on a link with 100 ms between the browser and the tiles (a data node
reached through an SSH tunnel, a server across the internet) a plane change
of three dozen tiles waits out six round trips, though its bytes take a
fifth of one. HTTP/2 would carry them on one connection, but browsers speak
HTTP/2 only over TLS, and Plexora serves plain HTTP -- on localhost, where the
round trip is nothing, and through tunnels that already encrypt.

So the viewer's tile scheduler (client `views/tileFetch.js`) measures the
link -- each tile's wait for its first byte, less the server's own time from
`Server-Timing` -- and when it is slow, asks for its tiles in batches here
instead: a POST of tile paths, answered with every tile in one body. On
localhost it never batches, and nothing about a tile changes.

Each tile is answered by dispatching its own GET through the app (`serve`):
the same route, the same guard (a node's token rides in each tile's query),
the same bytes and headers as fetched alone, several at a time on a small
pool. Only tile routes are dispatched (`PRIMARY`, `NODE`), at most
`MAX_TILES` a request.

The body: a little-endian uint32 header length, the header (JSON: `{tiles:
[{status, type, etag, bytes}]}` in the order asked), then each tile's bytes.
"""

from __future__ import annotations

import json
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

MAX_TILES = 64
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="tile-batch")

#: Channel, section and layer tiles of this server.
PRIMARY = re.compile(r"^/generated/(data|layer)/[^?#]+/\d+/\d+_\d+\.png(\?[^#]*)?$")
#: A data node's image and mask tiles.
NODE = re.compile(r"^/node/v1/(image|seg)/[^/?#]+/tile/[^?#]+\.png(\?[^#]*)?$")


class BadBatch(ValueError):
    pass


def parse(body: bytes, pattern) -> list[str]:
    """The tile paths a batch asks for, each checked against `pattern`."""
    try:
        data = json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadBatch("the body is JSON: {\"tiles\": [path, ...]}") from None
    tiles = data.get("tiles") if isinstance(data, dict) else None
    if not isinstance(tiles, list) or not tiles:
        raise BadBatch("no tiles asked for")
    if len(tiles) > MAX_TILES:
        raise BadBatch(f"at most {MAX_TILES} tiles a batch")
    for path in tiles:
        if not isinstance(path, str) or ".." in path or not pattern.match(path):
            raise BadBatch(f"{str(path)[:120]!r} is not a tile of this server")
    return tiles


def serve(app, paths, headers=None) -> bytes:
    """Every tile of `paths`, each dispatched as its own GET, in one body."""
    headers = dict(headers or {})

    def one(path):
        try:
            with app.test_request_context(path, method="GET", headers=headers):
                response = app.full_dispatch_request()
                # A tile is sent as a file (direct passthrough): read it whole.
                response.direct_passthrough = False
                body = response.get_data()
                response.close()
                return ({"status": response.status_code, "type": response.mimetype,
                         "etag": response.headers.get("ETag"), "bytes": len(body)}, body)
        except Exception as exc:
            # One tile that fails (or a test app that re-raises) fails alone.
            return ({"status": 500, "type": "text/plain", "etag": None, "bytes": 0,
                     "error": str(exc)[:200]}, b"")

    results = list(_POOL.map(one, paths))
    header = json.dumps({"tiles": [meta for meta, _body in results]}).encode("utf-8")
    return struct.pack("<I", len(header)) + header + b"".join(body for _meta, body in results)


def respond(app, request, pattern, headers=None):
    """The whole route: parse, serve, and say how long the server took
    (`Server-Timing`), so the client can tell the link's wait from ours."""
    from flask import jsonify

    started = perf_counter()
    try:
        paths = parse(request.get_data(), pattern)
    except BadBatch as exc:
        return jsonify(error=str(exc)), 400
    body = serve(app, paths, headers)
    response = app.response_class(body, mimetype="application/octet-stream")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Server-Timing"] = f"total;dur={(perf_counter() - started) * 1000:.1f}"
    return response
