"""The primary's HTTP client for talking to data nodes.

One pooled urllib3 manager for the whole process, because the traffic this
carries is not a handful of API calls: a proxied pan pulls a tile per channel
per viewport step, and opening a TCP connection for each one would add a
handshake to every tile on a link that may be a hop across a cluster.

Three properties the rest of the package depends on:

**Failures arrive typed.** A node that is asleep, a DNS name that does not
resolve and a connection that times out all become `ResourceUnavailable`,
because they are one situation from the caller's point of view -- the user's
laptop shut its lid -- and one the app degrades around rather than fails on. A
node that answers with a 500 is a different thing and stays a `ResourceError`.

**Nothing retries a write.** Retries are enabled for idempotent methods only.
A `POST /write/roi_columns` that times out may or may not have rewritten the
user's obs, and repeating it is how a "column already exists" refusal turns
into two columns.

**The token never appears in a URL from here.** The primary sends it as a
header; only the browser uses the query-parameter form, and only because that
is what keeps a tile request free of a CORS preflight.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Mapping

from plexora.server.providers.base import (
    NodeVersionMismatch,
    ResourceError,
    ResourceUnavailable,
)

#: Header the primary authenticates with. A header rather than `?t=` so a token
#: cannot end up in a node's access log, and because the primary has no CORS
#: preflight to avoid.
TOKEN_HEADER = "X-Plexora-Node-Token"
#: What the node stamps on every response.
API_HEADER = "X-Plexora-Node-Api"
GENERATION_HEADER = "X-Plexora-Generation"
RESOURCE_HEADER = "X-Plexora-Resource"
#: Set by the primary's proxy routes when a node call failed, so the client can
#: attribute the failure to the node rather than to the app.
NODE_ERROR_HEADER = "X-Plexora-Node-Error"

#: Seconds. Generous on read because a cold GMM fit on a node is ~1 s and a
#: pyramid conversion is polled rather than waited on; tight on connect because
#: an unreachable node has to be found out about quickly -- the reachability
#: probe is what decides whether the browser talks to it directly.
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 120.0
#: The probe's own budget. Deliberately short: it runs at project open, and a
#: sleeping laptop must not hold that up.
PROBE_TIMEOUT = 1.5

#: Refused rather than read. A node answering a table request with a gigabyte
#: is either broken or not a node, and the primary buffering it would take the
#: viewer down with it. Streamed endpoints (the CSV export) bypass this by
#: construction -- they are never buffered.
MAX_BUFFERED_BYTES = 512 * 1024 * 1024

_pool = None
_pool_lock = threading.Lock()


def pool():
    """The process-wide connection pool, made on first use.

    Lazily, so importing this module costs nothing in a build that never talks
    to a node -- which is every single-server install.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                import urllib3

                _pool = urllib3.PoolManager(
                    num_pools=8,
                    maxsize=16,
                    # Idempotent methods only. See the module docstring: a
                    # retried write is a duplicated write.
                    retries=urllib3.Retry(
                        total=2, connect=2, read=1, status=0,
                        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
                        backoff_factor=0.2,
                    ),
                    headers={"Accept-Encoding": "gzip"},
                )
    return _pool


def request(node, method, path, *, body=None, fields=None, headers=None,
            timeout=None, stream=False, expected_api=None):
    """One call to a node, with its failures already sorted into kinds.

    Returns the raw urllib3 response so callers can read bytes, headers or a
    stream as they need. `stream=True` leaves the body unread -- the caller
    must consume and release it.
    """
    import urllib3

    url = node.url(path)
    sent = {TOKEN_HEADER: node.token or ""}
    if headers:
        sent.update(headers)

    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        sent["Content-Type"] = "application/json"

    budget = urllib3.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT if timeout is None else timeout,
    )
    try:
        response = pool().request(
            method, url, body=payload, fields=fields, headers=sent,
            timeout=budget, preload_content=not stream,
            # Redirects are never legitimate here: a node answers on the path
            # it was asked about, and following one is how a misconfigured
            # reverse proxy turns a data request into a login page.
            redirect=False,
        )
    except urllib3.exceptions.MaxRetryError as exc:
        raise ResourceUnavailable(
            f"cannot reach data node {node.name!r} at {node.endpoint}: {exc.reason}",
            node=node.name) from exc
    except urllib3.exceptions.TimeoutError as exc:
        raise ResourceUnavailable(
            f"data node {node.name!r} did not answer in time", node=node.name) from exc
    except urllib3.exceptions.HTTPError as exc:
        raise ResourceUnavailable(
            f"cannot reach data node {node.name!r}: {exc}", node=node.name) from exc

    _check(node, response, expected_api, stream=stream)
    return response


def json_request(node, method, path, *, body=None, timeout=None, expected_api=None):
    """A call whose answer is one JSON document."""
    response = request(node, method, path, body=body, timeout=timeout,
                       expected_api=expected_api)
    data = response.data
    if len(data) > MAX_BUFFERED_BYTES:
        raise ResourceError(
            f"data node {node.name!r} answered {path} with "
            f"{len(data)} bytes, past this server's limit")
    if not data:
        return {}
    try:
        return json.loads(data)
    except ValueError as exc:
        raise ResourceError(
            f"data node {node.name!r} answered {path} with something that is "
            f"not JSON") from exc


def bytes_request(node, method, path, *, body=None, headers=None, timeout=None,
                  expected_api=None):
    """(bytes, response) for an answer that is not JSON -- a tile, a buffer."""
    response = request(node, method, path, body=body, headers=headers,
                       timeout=timeout, expected_api=expected_api)
    data = response.data
    if len(data) > MAX_BUFFERED_BYTES:
        raise ResourceError(
            f"data node {node.name!r} answered {path} with "
            f"{len(data)} bytes, past this server's limit")
    return data, response


def stream_request(node, method, path, *, body=None, timeout=None, chunk=1 << 16):
    """Yield an answer in chunks, never holding it whole.

    For the CSV export, which is the whole table by construction. The response
    is released when the generator is exhausted or closed, which Flask's
    `stream_with_context` guarantees even when the client disconnects.
    """
    response = request(node, method, path, body=body, timeout=timeout, stream=True)
    try:
        for piece in response.stream(chunk, decode_content=True):
            yield piece
    finally:
        response.release_conn()


def _check(node, response, expected_api, stream=False):
    """Turn a node's status line into the right kind of failure.

    A 304 is a success -- the proxy forwards conditional requests verbatim, so
    "not modified" has to survive the trip rather than being read as an error.
    """
    offered = response.headers.get(API_HEADER)
    if expected_api is not None and offered is not None and str(offered) != str(expected_api):
        raise NodeVersionMismatch(expected_api, offered, node=node.name)

    status = response.status
    if status < 400:
        return
    detail = ""
    if not stream:
        try:
            detail = (response.data or b"")[:512].decode("utf-8", "replace").strip()
        except Exception:  # pragma: no cover - a body that will not decode
            detail = ""
    if status in (401, 403):
        raise ResourceError(
            f"data node {node.name!r} refused this server's token. Re-register "
            f"the node with the token it was started with.")
    if status == 404:
        raise ResourceError(
            f"data node {node.name!r} does not serve that resource: {detail}")
    if status == 409:
        raise ResourceError(
            f"data node {node.name!r} disagrees about the request: {detail}")
    if status in (502, 503, 504):
        raise ResourceUnavailable(
            f"data node {node.name!r} is not ready: {detail}", node=node.name)
    raise ResourceError(f"data node {node.name!r} failed: {status} {detail}")


def hello(node, timeout=PROBE_TIMEOUT) -> dict:
    """The node's own description of itself, and the handshake.

    Also the reachability probe: it is one small GET, it is authenticated, and
    it answers the two questions a probe has to answer at once -- can this be
    reached, and does it speak a version we understand.
    """
    return json_request(node, "GET", "/node/v1/hello", timeout=timeout)


def reachable(node, timeout=PROBE_TIMEOUT) -> bool:
    """Whether this server can reach the node right now.

    Never raises: the whole point is to answer a question the caller is asking
    in order to decide what to do about a "no".
    """
    try:
        json_request(node, "GET", "/node/v1/health", timeout=timeout)
        return True
    except Exception:
        return False


def encode_ids(ids) -> str:
    """A list of cell ids as one query-string value."""
    return ",".join(str(int(value)) for value in ids)


def as_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def parse_generation(response) -> int | None:
    raw = response.headers.get(GENERATION_HEADER)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def conditional_headers(etag: str | None) -> Mapping[str, str]:
    return {"If-None-Match": etag} if etag else {}
