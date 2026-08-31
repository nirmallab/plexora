"""The primary's HTTP client for talking to data nodes.

One pooled urllib3 manager for the whole process, because the traffic this
carries is not a handful of API calls: a proxied pan pulls a tile per channel
per viewport step, and opening a TCP connection for each one would add a
handshake to every tile on a link that may be a hop across a cluster.

Four properties the rest of the package depends on:

**Failures arrive typed.** A node that is asleep, a DNS name that does not
resolve and a connection that times out all become `ResourceUnavailable`,
because they are one situation from the caller's point of view -- the user's
laptop shut its lid -- and one the app degrades around rather than fails on. A
node that answers with a 500 is a different thing and stays a `ResourceError`.

**Nothing retries a write.** Retries are enabled for idempotent methods only.
A `POST /write/roi_columns` that times out may or may not have rewritten the
user's obs, and repeating it is how a "column already exists" refusal turns
into two columns.

**A disconnected address is refused, not attempted.** Taking a tunnel down
does not reach into the providers, background threads and in-flight requests
that are still holding the address it served on. The registry records what it
was told to forget and this client checks it before opening a socket, so work
that outlived the connection fails at once and says which node and what to do
-- rather than spending two connection attempts and a backoff each
rediscovering it, and logging a warning per attempt about a connection the
user closed on purpose. Probes are exempt: asking whether a machine is up is
how it stops being disconnected.

**The token never appears in a URL from here.** The primary sends it as a
header; only the browser uses the query-parameter form, and only because that
is what keeps a tile request free of a CORS preflight.
"""

from __future__ import annotations

import contextlib
import json
import threading
from contextlib import contextmanager
from typing import Any, Mapping

from plexora.server.models import nodes as node_registry
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
#: The name of the file a `/read_file` answer is carrying. A header rather than
#: a JSON envelope because the body IS the file: the bytes stream through the
#: primary untouched, and the one thing that has to travel beside them is what
#: to call the File the browser builds out of them.
FILE_NAME_HEADER = "X-Plexora-File-Name"
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


_speculation = threading.local()


@contextmanager
def speculative():
    """Inside this, a node call is work nobody is waiting for -- so it does not
    retry.

    Same reasoning as `hello`'s `retries=False`, applied to the other traffic
    nothing is blocked on. The background cache warm-up walks every channel of
    a project on a thread of its own, and the moment its tunnel goes away --
    which is what disconnecting IS -- the pool's default policy spends two
    connection attempts and a backoff per call to arrive at the answer the
    first refusal already gave, logging a urllib3 warning on the way about a
    connection the user closed on purpose.

    The cost is that one blip on a flaky link ends the warm-up instead of
    riding over it. That is the right trade for work whose only purpose is to
    be early: everything it precomputes is computed on demand later anyway, by
    callers that do retry because somebody is waiting for them.
    """
    previous = getattr(_speculation, "on", False)
    _speculation.on = True
    try:
        yield
    finally:
        _speculation.on = previous


def request(node, method, path, *, body=None, fields=None, headers=None,
            raw_body=None, content_type=None, timeout=None, stream=False,
            expected_api=None, retries=None, allow_disconnected=False,
            allow_status=()):
    """One call to a node, with its failures already sorted into kinds.

    Returns the raw urllib3 response so callers can read bytes, headers or a
    stream as they need. `stream=True` leaves the body unread -- the caller
    must consume and release it.

    `body` is encoded as JSON; `raw_body` is sent as-is and may be a file-like
    object, which is what lets a 300 MB export travel to a node without the
    primary holding it -- urllib3 reads from it while writing the socket. The
    two are mutually exclusive, and a raw body goes through here rather than
    around it so a write still gets the disconnect check, the typed failures
    and the never-retry-a-POST policy that the rest of this module promises.

    `allow_status` names statuses that are ANSWERS rather than failures, handed
    back for the caller to read. One caller needs it: a write refused because
    something is already there replies 409 with `exists`, and that flag is the
    difference between a dead end and a Replace? question -- raising it as a
    `ResourceError` would leave the caller matching on a substring of a message
    to get it back.
    """
    import urllib3

    # Before the socket, because the answer is already known. A provider holds
    # the node it resolved for its whole life, so after a disconnect there is
    # work still carrying an address whose tunnel has gone -- see
    # `models/nodes.is_disconnected`. Probes pass `allow_disconnected`: asking
    # whether a machine is up is exactly what may legitimately be done to one
    # that was taken down, and it is how registering it again begins.
    if not allow_disconnected and node_registry.is_disconnected(node):
        raise ResourceUnavailable(
            f"data node {node.name!r} was disconnected from this server. "
            f"Connect it again to read from it.", node=node.name)

    # An explicit `retries` always wins; this only fills in for a caller that
    # said nothing while running under speculative() -- see that context
    # manager for why work nobody is waiting for does not retry.
    if retries is None and getattr(_speculation, "on", False):
        retries = False

    url = node.url(path)
    sent = {TOKEN_HEADER: node.token or ""}
    if headers:
        sent.update(headers)

    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        sent["Content-Type"] = "application/json"
    elif raw_body is not None:
        # Not encoded, not counted: a stream has no length until it is read,
        # and the caller passes the size it already knows in `headers` when it
        # has one. Without a Content-Length urllib3 chunks it, which the node's
        # `request.stream` reads either way.
        payload = raw_body
        sent["Content-Type"] = content_type or "application/octet-stream"

    budget = urllib3.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT if timeout is None else timeout,
    )
    try:
        response = pool().request(
            method, url, body=payload, fields=fields, headers=sent,
            timeout=budget, preload_content=not stream,
            # `None` keeps the pool's policy (see pool()); a caller that is
            # only asking "is this up?" passes False -- see hello().
            **({} if retries is None else {"retries": retries}),
            # Redirects are never legitimate here: a node answers on the path
            # it was asked about, and following one is how a misconfigured
            # reverse proxy turns a data request into a login page.
            redirect=False,
        )
    except urllib3.exceptions.MaxRetryError as exc:
        raise ResourceUnavailable(
            f"cannot reach data node {node.name!r} at {node.endpoint}: {exc.reason}",
            node=node.name) from exc
    except urllib3.exceptions.NewConnectionError as exc:
        # BEFORE the timeout branch, which it would otherwise fall into:
        # NewConnectionError subclasses ConnectTimeoutError. A refused
        # connection is not a slow one -- the machine answered at once, and
        # the answer was "no" -- so "did not answer in time" would send
        # somebody hunting a network problem that is not there. This is the
        # ordinary shape of a tunnel that has gone away, and naming it is
        # what turns the panel's second line into something actionable.
        # Only reachable when the caller disabled retries (see hello): with
        # them on, MaxRetryError wraps this first.
        raise ResourceUnavailable(
            f"cannot reach data node {node.name!r} at {node.endpoint}: {exc}",
            node=node.name) from exc
    except urllib3.exceptions.TimeoutError as exc:
        raise ResourceUnavailable(
            f"data node {node.name!r} did not answer in time", node=node.name) from exc
    except urllib3.exceptions.HTTPError as exc:
        raise ResourceUnavailable(
            f"cannot reach data node {node.name!r}: {exc}", node=node.name) from exc

    _check(node, response, expected_api, stream=stream,
           allow_status=allow_status, path=path)
    return response


def json_request(node, method, path, *, body=None, timeout=None,
                 expected_api=None, retries=None, allow_disconnected=False):
    """A call whose answer is one JSON document."""
    response = request(node, method, path, body=body, timeout=timeout,
                       expected_api=expected_api, retries=retries,
                       allow_disconnected=allow_disconnected)
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


def _sentence(response, stream):
    """What the node SAID about a failure, or nothing at all.

    Read even when the caller asked for a stream. A failed response's body is
    never the payload that was asked for, and nobody is going to consume it
    now, since the caller raises -- skipping it left `/fetch_file` reporting
    "the node disagrees about the request:" with the half that says which file
    was missing cut off.

    A node writes its own failures as JSON, and those sentences are written to
    be read: which file is missing, which name is taken. What comes back from
    Flask's own handlers is an HTML document, and a document put in front of
    somebody in place of a sentence is worse than saying nothing -- it is long,
    it is not about this application, and it buries the only thing that
    mattered, which is the status line. So markup is dropped and the caller's
    own sentence stands alone.
    """
    try:
        raw = response.read(512) if stream else (response.data or b"")[:512]
    except Exception:  # pragma: no cover - a body that will not be read
        return ""
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text or text.startswith("<"):
        return ""
    if not text.startswith("{"):
        return text
    try:
        said = json.loads(text)
    except ValueError:
        # A document cut off at 512 bytes is not a sentence either, and half a
        # JSON object read aloud is the worst of both.
        return ""
    if not isinstance(said, dict):
        return ""
    return str(said.get("error") or said.get("message") or "").strip()


def _check(node, response, expected_api, stream=False, allow_status=(), path=""):
    """Turn a node's status line into the right kind of failure.

    A 304 is a success -- the proxy forwards conditional requests verbatim, so
    "not modified" has to survive the trip rather than being read as an error.
    """
    offered = response.headers.get(API_HEADER)
    if expected_api is not None and offered is not None and str(offered) != str(expected_api):
        raise NodeVersionMismatch(expected_api, offered, node=node.name)

    status = response.status
    if status < 400 or status in allow_status:
        return
    detail = _sentence(response, stream)
    if stream:
        with contextlib.suppress(Exception):
            response.release_conn()
    if status in (401, 403):
        raise ResourceError(
            f"data node {node.name!r} refused this server's token. Re-register "
            f"the node with the token it was started with.")
    if status == 404:
        # Two different 404s, told apart by whether there is a sentence behind
        # it. The node answers its own "no such resource" as JSON and means it.
        # A 404 with nothing to say never reached a route at all: the URL is
        # not registered, and on a machine that answers and accepts the token
        # that means the Plexora over there is older than this endpoint.
        #
        # Nothing negotiates that away. `api_version` marks incompatible wire
        # SHAPES and is deliberately not bumped when an endpoint is added, so
        # the 404 itself is the only signal there is -- and it used to arrive
        # as Flask's own "<!doctype html>… 404 Not Found" pasted into whatever
        # dialog had asked, which named neither the machine's version nor the
        # one thing to do about it.
        if detail:
            raise ResourceError(
                f"data node {node.name!r} does not serve that resource: {detail}")
        from plexora import cli

        # Said the way connect.py says the same thing about a remote that
        # cannot understand a flag (`_old_remote_hint`): name the machine that
        # is behind, and the one command that fixes it. Both versions, because
        # the two sides are separately installed and matching numbers are not
        # proof -- which is itself the thing worth knowing when they match.
        raise ResourceError(
            f"the Plexora installed on data node {node.name!r} is too old for "
            f"this: it does not serve {path or 'that endpoint'}. Upgrade it "
            f"there and connect again: pip install --upgrade plexora. "
            f"(That machine reports "
            f"{node.plexora_version or 'no version'}; this one reports "
            f"{cli.version_string()}.)")
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

    **And a probe does not retry.** The pool retries idempotent requests twice
    with a backoff, which is right for a tile fetch across a flaky tunnel and
    wrong for every caller here: they are all asking whether a node is up, and
    they all catch the "no" and carry on. Against a tunnel that is simply gone
    -- the ordinary state of a saved connection the morning after -- a refusal
    is definitive on the first attempt, so retrying spends 0.6s of backoff to
    arrive at the same answer and logs two urllib3 warnings per probe on the
    way. Four best-effort probes on one page load made eight lines of stack
    trace about a machine nobody had asked about yet.

    **And a probe may ask a disconnected node.** Every other call is refused
    for one that was taken down; this one is how `register_node` checks an
    address before recording it, which is how a machine stops being
    disconnected. Refusing it here would make reconnecting on the port the
    last session used impossible.
    """
    return json_request(node, "GET", "/node/v1/hello", timeout=timeout,
                        retries=False, allow_disconnected=True)


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
