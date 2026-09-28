"""Telemetry for data nodes, which never upload anything themselves.

A node is often a compute job on a cluster with no route to the internet,
started by somebody else's server, and it has no install identity of its own
to report under. So a node only *counts*, in memory -- no queue, no settings
file, no identity, no thread -- and hands its cumulative counts to whoever
asks `/node/v1/hello` (the primary, over the connection it already has). The
primary folds the difference since it last asked into its own queue as
`node.summary` (`fold` below, called from `providers.http.hello`).

Cumulative since process start, not reset on read: two primaries polling one
node, or a lost response, must not lose data. `since` identifies the node
process, so a restart resets the primary's baseline rather than producing a
negative delta.

`Server-Timing` is emitted on node tiles whatever the mode, and
`Timing-Allow-Origin` for exactly the origins the node already lets read its
responses, so a browser reaching the node directly can time the tiles too.
"""

from __future__ import annotations

import threading
import time
import uuid
from time import perf_counter

from plexora.telemetry import config, performance, schema

_BINS = schema.HIST_BINS
_TILE_ENDPOINTS = {"plexora_node.image_tile": "channel", "plexora_node.seg_tile": "label"}


class NodeStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.since = uuid.uuid4().hex[:16]
        self.started = time.time()
        self.requests = 0
        self.errors = 0
        self.by_family = {"tile": 0, "data": 0, "other": 0}
        self.request_ms = [0] * _BINS
        self.tiles = 0
        self.cache_hit = 0
        self.cache_miss = 0
        self.tile_total_ms = [0] * _BINS
        self.tile_read_ms = [0] * _BINS
        self.tile_encode_ms = [0] * _BINS

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "since": self.since,
                "uptime_s": int(time.time() - self.started),
                "tiles": {"count": self.tiles, "cache_hit": self.cache_hit,
                          "cache_miss": self.cache_miss,
                          "total_ms": list(self.tile_total_ms),
                          "read_ms": list(self.tile_read_ms),
                          "encode_ms": list(self.tile_encode_ms)},
                "requests": {"count": self.requests, "errors": self.errors,
                             "by_family": dict(self.by_family), "ms": list(self.request_ms)},
            }


#: None when telemetry is off on this machine: no counting, no block in hello.
stats: NodeStats | None = None


def enabled_here() -> bool:
    return config.resolve(prefs={}).mode != config.OFF


def install(app, *, force=None):
    """Hooks for a node app. `force` overrides the mode check (tests)."""
    global stats
    from flask import request

    on = enabled_here() if force is None else force
    stats = NodeStats() if on else None

    @app.before_request
    def _node_telemetry_begin():
        kind = _TILE_ENDPOINTS.get(request.endpoint or "")
        if kind is not None:
            performance.begin(kind)
        request.environ["plexora.telemetry.t0"] = perf_counter()

    @app.after_request
    def _node_telemetry_end(response):
        phases = performance.end() if performance.active() else None
        try:
            if phases is not None:
                response.headers["Server-Timing"] = performance.server_timing_header(phases)
                allowed = app.config.get("PLEXORA_NODE_ORIGINS") or []
                origin = request.headers.get("Origin")
                if origin and origin in allowed:
                    response.headers["Timing-Allow-Origin"] = origin
            if stats is not None:
                _fold(request, response, phases)
        except Exception:
            pass
        return response

    @app.teardown_request
    def _node_telemetry_teardown(_exc=None):
        if performance.active():
            performance.end()

    return app


def _fold(request, response, phases):
    endpoint = request.endpoint or ""
    if endpoint.endswith(".hello") or endpoint.endswith(".health"):
        return
    t0 = request.environ.get("plexora.telemetry.t0")
    ms = (perf_counter() - t0) * 1000.0 if t0 else 0.0
    family = "tile" if endpoint in _TILE_ENDPOINTS else (
        "data" if endpoint.startswith("plexora_node.") else "other")
    with stats.lock:
        stats.requests += 1
        stats.by_family[family] += 1
        stats.request_ms[schema.ms_bin(ms)] += 1
        if response.status_code >= 500:
            stats.errors += 1
        if phases is None or response.status_code >= 400:
            return
        stats.tiles += 1
        cache = phases.notes.get("cache")
        if cache == "hit":
            stats.cache_hit += 1
        elif cache == "miss":
            stats.cache_miss += 1
        stats.tile_total_ms[schema.ms_bin(ms)] += 1
        read = phases.ms("read")
        if read is not None:
            stats.tile_read_ms[schema.ms_bin(read)] += 1
        enc = phases.ms("enc")
        if enc is not None:
            stats.tile_encode_ms[schema.ms_bin(enc)] += 1


def snapshot():
    """The block `/node/v1/hello` carries, or None when off here."""
    return stats.snapshot() if stats is not None else None


# -- the primary's side ---------------------------------------------------------

_baselines: dict = {}
_baseline_lock = threading.Lock()


def _delta(now, before):
    if isinstance(now, list):
        before = before if isinstance(before, list) and len(before) == len(now) else [0] * len(now)
        return [max(0, int(a) - int(b)) for a, b in zip(now, before)]
    if isinstance(now, dict):
        before = before if isinstance(before, dict) else {}
        return {k: _delta(v, before.get(k)) for k, v in now.items()}
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        prior = before if isinstance(before, (int, float)) and not isinstance(before, bool) else 0
        return max(0, int(now) - int(prior))
    return now


def _valid_block(block) -> bool:
    try:
        tiles, requests = block["tiles"], block["requests"]
        hists = [tiles["total_ms"], tiles["read_ms"], tiles["encode_ms"], requests["ms"]]
        return (isinstance(block["since"], str) and len(block["since"]) <= 64
                and all(isinstance(h, list) and len(h) == _BINS
                        and all(isinstance(v, int) and v >= 0 for v in h) for h in hists)
                and all(isinstance(tiles[k], int) for k in ("count", "cache_hit", "cache_miss"))
                and all(isinstance(requests[k], int) for k in ("count", "errors")))
    except (KeyError, TypeError):
        return False


def fold(node_key, answer, rtt_ms):
    """Fold one successful hello into this primary's `node.summary`.

    `node_key` identifies the node in memory only -- it is never counted,
    stored or sent."""
    from plexora.telemetry.client import telemetry

    if not telemetry.enabled:
        return
    try:
        telemetry.observe("node.summary", "hello_rtt_ms", rtt_ms)
        block = (answer or {}).get("telemetry") if isinstance(answer, dict) else None
        if not isinstance(block, dict) or not _valid_block(block):
            return
        with _baseline_lock:
            previous = _baselines.get(node_key)
            _baselines[node_key] = block
            if len(_baselines) > 256:
                _baselines.pop(next(iter(_baselines)))
        if previous is None or previous.get("since") != block["since"]:
            if previous is None:
                telemetry.count("node.summary", "nodes")
            previous = {}
        delta = _delta({"tiles": block["tiles"], "requests": block["requests"]},
                       {"tiles": previous.get("tiles"), "requests": previous.get("requests")})
        tiles, requests = delta["tiles"], delta["requests"]
        if tiles["cache_hit"]:
            telemetry.count("node.summary", "tiles", tiles["cache_hit"], cache="hit")
        if tiles["cache_miss"]:
            telemetry.count("node.summary", "tiles", tiles["cache_miss"], cache="miss")
        for key, name in (("total_ms", "tile_total_ms"), ("read_ms", "tile_read_ms"),
                          ("encode_ms", "tile_encode_ms")):
            if sum(tiles[key]):
                telemetry.add_hist("node.summary", name, tiles[key])
        if requests["count"]:
            telemetry.count("node.summary", "requests", requests["count"])
        if requests["errors"]:
            telemetry.count("node.summary", "request_errors", requests["errors"])
            with telemetry._lock:
                telemetry._errors["node"] += requests["errors"]
    except Exception:
        telemetry._fail()


def hello_failed(exc):
    """One failed probe, classified by the exception's type alone."""
    from plexora.telemetry.client import telemetry

    if not telemetry.enabled:
        return
    name = type(exc).__name__.lower()
    if "timeout" in name:
        reason = "timeout"
    elif "refused" in name or "connection" in name or "unreachable" in name:
        reason = "refused"
    elif "refus" in name or "auth" in name or "token" in name:
        reason = "auth"
    elif "version" in name:
        reason = "version"
    else:
        reason = "other"
    telemetry.count("node.summary", "hello_failed", reason=reason)


def _reset_for_tests():
    global stats
    stats = None
    with _baseline_lock:
        _baselines.clear()
