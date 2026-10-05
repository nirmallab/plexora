"""The Flask side: request timings, tile phases, and error fingerprints.

`install(app)` runs once, at the end of `create_app`. What it adds per request:

- `before_request`: arms the tile phase recorder for tile endpoints (always --
  the `Server-Timing` header does not depend on telemetry), and notes the
  start time when telemetry is on.
- `after_request`: writes `Server-Timing` on tiles, then -- only when on --
  one route histogram, one family histogram, and for tiles the phase
  histograms. `/health` (polled every five seconds by every tab), the
  client's static files and plugin static files are never counted.
- `got_request_exception`: one error fingerprint. A signal rather than an
  `errorhandler(Exception)`, which would change what a 500 looks like.

Budget: O(1) in-memory work, no I/O, never a datasource load. Measured by
tests/test_telemetry_hooks_cost.py.
"""

from __future__ import annotations

import hashlib
import re
from time import perf_counter

from plexora.telemetry import identity, performance, schema
from plexora.telemetry.client import telemetry

#: Endpoints whose phases are recorded, and the tile kind each one serves.
TILE_ENDPOINTS = frozenset({"generate_png", "generate_layer_tile"})

#: Never counted: polled constantly, or files rather than work.
SKIP = frozenset({"health", "serveClient", "favicon", "static"})

_FAMILIES = (
    ("tile", frozenset({"generate_png", "generate_layer_tile", "generate_blank_tile",
                        "generate_rgb_image", "generate_overview", "project_thumbnail"})),
    ("points", frozenset({"get_centroid_tiles", "get_centroid_manifest", "transcripts.points",
                          "visium_hd.square", "visium_hd.spot_values"})),
    ("page", frozenset({"image_viewer", "my_index", "settings_page", "open_project_page",
                        "edit_project_page", "figure_builder.figure_page",
                        "figure_builder.library_page", "figure_builder.captures_page"})),
    ("config", frozenset({"serve_config", "get_database_description", "get_channel_names",
                          "get_load_status", "project_manifest", "resource_status",
                          "resource_routing"})),
    ("tool", frozenset({"open_tool", "tool_panel", "tool_requirements"})),
)

_FIRST_PARTY_BLUEPRINTS = frozenset(schema.FIRST_PARTY) | {"agent_v1", "ai_v1", "ai_chat_v1", "segment_v1", "telemetry", "license"}
_INDEXED = re.compile(r".*_(\d+)$")
_routes: dict = {}


def route_name(app, endpoint) -> str:
    """What telemetry calls an endpoint: ours verbatim, anyone else's hashed."""
    cached = _routes.get(endpoint)
    if cached is not None:
        return cached
    name = _route_name(app, endpoint)
    if len(_routes) < 4096:
        _routes[endpoint] = name
    return name


def _route_name(app, endpoint):
    if not endpoint:
        return "unmatched"
    if "." in endpoint:
        blueprint, _, _function = endpoint.partition(".")
        name = endpoint if blueprint in _FIRST_PARTY_BLUEPRINTS else identity.owner_label(blueprint)
    else:
        view = app.view_functions.get(endpoint)
        module = getattr(view, "__module__", "") or ""
        name = endpoint if module.startswith("plexora.") or module == "plexora" \
            else identity.owner_label(endpoint)
    return name if schema.ROUTE.check(name) else "unmatched"


def family(endpoint) -> str:
    for name, members in _FAMILIES:
        if endpoint in members:
            return name
    if endpoint.startswith("agent_v1."):
        return "agent"
    if endpoint.startswith("import") or endpoint in ("detect_image_type", "inspect_data",
                                                     "upload_data_file"):
        return "import"
    if endpoint.startswith("settings_") or endpoint.startswith("update_"):
        return "settings"
    if "." in endpoint:
        return "plugin"
    return "data"


def tile_kind(endpoint, view_args, args) -> str:
    if endpoint == "generate_layer_tile":
        return "channel"
    channel = (view_args or {}).get("channel") or ""
    if channel == "rgb":
        return "rgb"
    if not _INDEXED.match(channel):
        return "label"
    return "hd" if args.get("q") == "hd" else "channel"


def fingerprint(exc, route) -> tuple[str, str]:
    """`(fp, exc_type)` for an exception, without ever reading its message.

    The fingerprint is the class, the route, and the innermost frame inside
    Plexora as `module:function` -- no line number, so a fix elsewhere in
    the file does not make the same bug look new.
    """
    cls = type(exc)
    module = getattr(cls, "__module__", "") or ""
    if module == "builtins" or module.startswith("plexora"):
        exc_type = cls.__name__ if schema.EXC_TYPE.check(cls.__name__) else "ext"
    else:
        exc_type = "ext"
    frame = "none"
    tb = exc.__traceback__
    while tb is not None:
        name = tb.tb_frame.f_globals.get("__name__", "") or ""
        if name.startswith("plexora"):
            frame = f"{name}:{tb.tb_frame.f_code.co_name}"
        tb = tb.tb_next
    digest = hashlib.sha256(f"{cls.__module__}.{cls.__qualname__}|{route}|{frame}".encode(
        "utf-8", "replace")).hexdigest()[:16]
    return digest, exc_type


def install(app):
    from flask import g, got_request_exception, request

    @app.before_request
    def _telemetry_begin():
        endpoint = request.endpoint
        if endpoint in TILE_ENDPOINTS:
            performance.begin(tile_kind(endpoint, request.view_args, request.args))
        if telemetry.enabled:
            g._plexora_t0 = perf_counter()

    @app.after_request
    def _telemetry_end(response):
        phases = performance.end() if performance.active() else None
        if phases is not None:
            try:
                response.headers["Server-Timing"] = performance.server_timing_header(phases)
            except Exception:
                pass
        if not telemetry.enabled:
            return response
        try:
            _record(app, request, response, phases, g.pop("_plexora_t0", None))
        except Exception:
            telemetry._fail()
        return response

    @app.teardown_request
    def _telemetry_teardown(_exc=None):
        # A request that died before after_request must not leave its
        # recorder armed for the next request on this thread.
        if performance.active():
            performance.end()

    def _on_exception(sender, exception, **_extra):
        if not telemetry.enabled:
            return
        try:
            route = route_name(app, request.endpoint)
            fp, exc_type = fingerprint(exception, route)
            telemetry.error("server", fp, exc_type=exc_type, route=route)
        except Exception:
            telemetry._fail()

    got_request_exception.connect(_on_exception, app, weak=False)
    return app


def _record(app, request, response, phases, t0):
    endpoint = request.endpoint
    if t0 is None or not endpoint or endpoint in SKIP or endpoint.endswith(".static"):
        return
    ms = (perf_counter() - t0) * 1000.0
    status = f"{min(5, max(2, response.status_code // 100))}xx"
    telemetry.observe("server.summary", "route_ms", ms, route=route_name(app, endpoint),
                      status=status)
    telemetry.observe("server.summary", "family_ms", ms, family=family(endpoint))
    if endpoint not in TILE_ENDPOINTS:
        return
    if response.status_code == 503:
        telemetry.count("server.summary", "node_forward", result="unavailable")
        return
    if phases is None or response.status_code >= 400:
        return
    kind = phases.kind
    cache = phases.notes.get("cache")
    node = phases.ms("node")
    source = "node" if node is not None else "local"
    if cache in ("hit", "miss"):
        telemetry.count("server.summary", "tile_cache", result=cache)
        telemetry.observe("server.summary", "tile_ms", ms, source=source, kind=kind, cache=cache)
    if node is not None:
        telemetry.count("server.summary", "node_forward", result="ok")
    read = node if node is not None else phases.ms("read")
    if read is not None:
        telemetry.observe("server.summary", "tile_read_ms", read, source=source, kind=kind)
    lut = phases.ms("lut")
    if lut is not None:
        telemetry.observe("server.summary", "tile_lut_ms", lut, kind=kind)
    enc = phases.ms("enc")
    if enc is not None:
        telemetry.observe("server.summary", "tile_encode_ms", enc, kind=kind)
