"""The Flask hooks: what a request leaves behind, and what it never does."""

import json
from types import SimpleNamespace

import pytest

import plexora
from plexora.telemetry import flask_hooks
from tests.brightfield_fixtures import write_planar_fluorescence


def rows(t, event=None):
    t.sync()
    found = []
    for row in t.queue.snapshot(limit=100000)[0]:
        if event is None or row[1] == event:
            found.append({"event": row[1], "key": row[2], "dims": json.loads(row[3]),
                          "n": row[5], "bins": list(row[6:15])})
    return found


@pytest.fixture
def panel(tmp_path):
    from plexora.datasource import register_image_datasource

    path = write_planar_fluorescence(tmp_path / "panel.ome.tif", height=256, width=256)
    entry = register_image_datasource("panel", path)
    return entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]


def test_tiles_carry_server_timing_even_when_off(panel):
    client = plexora.app.test_client()
    response = client.get(f"/generated/data/panel/{panel}/0/0_0.png")
    assert response.status_code == 200
    timing = response.headers["Server-Timing"]
    names = [part.strip().split(";")[0] for part in timing.split(",")]
    assert names[:3] == ["read", "lut", "enc"]
    assert "total" in names and "cache;desc=miss" in timing
    again = client.get(f"/generated/data/panel/{panel}/0/0_0.png")
    assert "cache;desc=hit" in again.headers["Server-Timing"]
    assert "read;" not in again.headers["Server-Timing"]


def test_non_tile_requests_get_no_server_timing():
    response = plexora.app.test_client().get("/health")
    assert "Server-Timing" not in response.headers


def test_tile_requests_fold_into_phases(telemetry_enabled, panel):
    t, fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    client = plexora.app.test_client()
    for _ in range(3):
        assert client.get(f"/generated/data/panel/{panel}/0/0_0.png").status_code == 200
    found = rows(t, "server.summary")
    cache = {r["dims"]["result"]: r["n"] for r in found if r["key"] == "tile_cache"}
    assert cache == {"miss": 1, "hit": 2}
    keys = {r["key"] for r in found}
    assert {"route_ms", "family_ms", "tile_ms", "tile_read_ms", "tile_lut_ms",
            "tile_encode_ms"} <= keys
    route = [r for r in found if r["key"] == "route_ms"
             and r["dims"] == {"route": "generate_png", "status": "2xx"}]
    assert route and sum(route[0]["bins"]) == 3
    assert fake.requests == []
    assert "panel" not in json.dumps(found)


def test_health_and_client_files_are_never_counted(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    client = plexora.app.test_client()
    for _ in range(5):
        client.get("/health")
    client.get("/client/src/js/main.js")
    routes = {r["dims"].get("route") for r in rows(t, "server.summary") if r["key"] == "route_ms"}
    assert "health" not in routes and "serveClient" not in routes


def test_a_500_is_one_fingerprint_with_no_message(telemetry_enabled, monkeypatch):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)

    def explode():
        raise KeyError("/Users/alice/patient_29384/secret.csv")

    # Standing in for the real view, so named as it: a view from any other
    # module is somebody else's code and would be hashed.
    explode.__module__ = "plexora.server.routes.data_routes"
    flask_hooks._routes.clear()
    monkeypatch.setitem(plexora.app.view_functions, "get_channel_names", explode)
    monkeypatch.setitem(plexora.app.config, "PROPAGATE_EXCEPTIONS", False)
    response = plexora.app.test_client().get("/get_channel_names?datasource=x")
    assert response.status_code == 500
    errors = rows(t, "error.fingerprint")
    assert len(errors) == 1
    dims = errors[0]["dims"]
    assert dims["where"] == "server" and dims["exc_type"] == "KeyError"
    assert dims["route"] == "get_channel_names" and len(dims["fp"]) == 16
    assert "alice" not in json.dumps(errors) and "patient" not in json.dumps(errors)
    status = [r for r in rows(t, "server.summary") if r["key"] == "route_ms"
              and r["dims"]["route"] == "get_channel_names"]
    assert status and status[0]["dims"]["status"] == "5xx"


def test_fingerprint_is_stable_across_lines():
    def fail_here():
        raise ValueError("one")

    def fail_again():
        raise ValueError("two, with a different message")

    first = second = None
    for function, slot in ((fail_here, "first"), (fail_here, "second")):
        try:
            function()
        except ValueError as exc:
            if slot == "first":
                first = flask_hooks.fingerprint(exc, "generate_png")
            else:
                second = flask_hooks.fingerprint(exc, "generate_png")
    assert first == second
    try:
        fail_again()
    except ValueError as exc:
        third = flask_hooks.fingerprint(exc, "generate_png")
    assert third[1] == "ValueError"


def test_foreign_exception_class_is_ext():
    class AcmeError(Exception):
        pass

    AcmeError.__module__ = "acme_plugin.errors"
    try:
        raise AcmeError("x")
    except AcmeError as exc:
        assert flask_hooks.fingerprint(exc, "r")[1] == "ext"


def test_third_party_routes_are_hashed():
    view = SimpleNamespace(__module__="acme_plugin.routes")
    ours = SimpleNamespace(__module__="plexora.server.routes.data_routes")
    app = SimpleNamespace(view_functions={"acme_secret": view, "generate_png": ours})
    flask_hooks._routes.clear()
    assert flask_hooks.route_name(app, "generate_png") == "generate_png"
    label = flask_hooks.route_name(app, "acme_secret")
    assert label.startswith("ext:") and "acme" not in label
    blueprint = flask_hooks.route_name(app, "acmebp.do_things")
    assert blueprint.startswith("ext:") and "acme" not in blueprint
    assert flask_hooks.route_name(app, "gating.static") == "gating.static"
    flask_hooks._routes.clear()


def test_unavailable_resource_is_counted(telemetry_enabled, monkeypatch):
    from plexora.server.providers.base import ResourceUnavailable

    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)

    def unavailable():
        raise ResourceUnavailable("node asleep", node="hpc")

    monkeypatch.setitem(plexora.app.view_functions, "get_channel_names", unavailable)
    response = plexora.app.test_client().get("/get_channel_names")
    assert response.status_code == 503
    assert [r["n"] for r in rows(t, "server.summary") if r["key"] == "unavailable_503"] == [1]


def test_every_first_party_endpoint_is_a_valid_route_name():
    flask_hooks._routes.clear()
    for endpoint in plexora.app.view_functions:
        name = flask_hooks.route_name(plexora.app, endpoint)
        assert name != "unmatched", endpoint
        assert not name.startswith("ext:"), endpoint
    flask_hooks._routes.clear()
