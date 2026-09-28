"""The local telemetry routes: status, mode, notice, preview, and a tab's ingest."""

import json

import pytest

import plexora
from plexora.server.routes import telemetry_routes
from plexora.telemetry import config


@pytest.fixture(autouse=True)
def _forget_ingest_state():
    telemetry_routes._reset_for_tests()
    yield
    telemetry_routes._reset_for_tests()


def client():
    return plexora.app.test_client()


def ingest_body(viewer="ab" * 8, rows=None):
    return {"schema": 1, "viewer_id": viewer, "rows": rows if rows is not None else [
        {"e": "render.summary", "k": "tile_ms",
         "d": {"browser": "chrome", "os": "mac", "gpu": "apple", "label_renderer": "gpu",
               "path": "local", "kind": "channel"},
         "n": 4, "h": [1, 2, 1, 0, 0, 0, 0, 0, 0], "s": 80.0, "mx": 40.0},
        {"e": "tool.summary", "k": "open", "d": {"tool": "gating"}, "n": 2},
        {"e": "feature.summary", "k": "n", "d": {"feature": "gate.commit", "plugin": "gating"},
         "n": 3},
        {"e": "error.fingerprint", "k": "n",
         "d": {"where": "browser", "fp": "0a1b2c3d", "component": "tool_loader",
               "action": "load", "file": "toolLoader.js"}, "n": 1},
    ]}


def test_status_off_under_pytest():
    status = client().get("/telemetry/status").get_json()
    assert status["mode"] == "off" and status["source"] in ("testing", "env")
    assert status["notice_pending"] is False
    assert status["ingest_interval_s"] == 300


def test_ingest_is_204_and_ignored_when_off():
    response = client().post("/telemetry/ingest", json=ingest_body())
    assert response.status_code == 204


def test_notice_pending_then_seen(telemetry_enabled, monkeypatch):
    monkeypatch.delenv("PLEXORA_TELEMETRY")
    status = client().get("/telemetry/status").get_json()
    assert status["mode"] == "anonymous" and status["source"] == "default"
    assert status["notice_pending"] is True
    client().post("/telemetry/notice_seen")
    assert client().get("/telemetry/status").get_json()["notice_pending"] is False


def test_mode_writes_settings_and_flips_enabled(telemetry_enabled, monkeypatch):
    t, _fake = telemetry_enabled
    monkeypatch.delenv("PLEXORA_TELEMETRY")
    t.start(plexora.app, "terminal", upload=False)
    assert t.enabled
    answer = client().post("/telemetry/mode", json={"mode": "off"}).get_json()
    assert answer["mode"] == "off" and answer["source"] == "settings"
    assert config.read_prefs()["mode"] == "off"
    assert t.enabled is False
    answer = client().post("/telemetry/mode", json={"mode": "diagnostics"}).get_json()
    assert answer["mode"] == "diagnostics" and t.enabled is True
    assert client().post("/telemetry/mode", json={"mode": "loud"}).status_code == 400
    assert client().post("/telemetry/mode", data="mode=off").status_code == 415


def test_ingest_folds_rows(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    assert client().post("/telemetry/ingest", json=ingest_body()).status_code == 204
    t.sync()
    rows = {(r[1], r[2]): r for r in t.queue.snapshot()[0]}
    assert rows[("render.summary", "tile_ms")][5] == 4
    assert list(rows[("render.summary", "tile_ms")][6:15]) == [1, 2, 1, 0, 0, 0, 0, 0, 0]
    assert rows[("tool.summary", "open")][5] == 2
    assert rows[("feature.summary", "n")][5] == 3
    assert rows[("render.summary", "viewers")][5] == 1
    assert t._errors["browser"] == 1


@pytest.mark.parametrize("mutate", [
    lambda b: b["rows"][0]["d"].update(project="patient_1"),
    lambda b: b["rows"].append({"e": "server.summary", "k": "unavailable_503", "d": {}, "n": 1}),
    lambda b: b["rows"][3]["d"].update(where="server"),
    lambda b: b.update(extra=1),
    lambda b: b.update(viewer_id="not hex!"),
    lambda b: b["rows"][1]["d"].update(tool="my_private_plugin"),
    lambda b: b["rows"][3]["d"].update(file="/Users/alice/x.js"),
])
def test_ingest_rejects_anything_off_schema(telemetry_enabled, mutate):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    body = ingest_body()
    mutate(body)
    assert client().post("/telemetry/ingest", json=body).status_code == 400
    t.sync()
    assert not [r for r in t.queue.snapshot()[0] if r[1] in ("render.summary", "tool.summary")]


def test_ingest_too_large_and_too_soon(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    big = "x" * (70 * 1024)
    assert client().post("/telemetry/ingest", data=big,
                         content_type="application/json").status_code == 413
    assert client().post("/telemetry/ingest", json=ingest_body()).status_code == 204
    assert client().post("/telemetry/ingest", json=ingest_body()).status_code == 429
    assert client().post("/telemetry/ingest", json=ingest_body("cd" * 8)).status_code == 204


def test_preview_is_what_the_uploader_would_build(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    t.count("tool.summary", "open", tool="roi")
    t.sync()
    t.count("tool.summary", "open", tool="roi")  # still in memory
    preview = client().get("/telemetry/preview").get_json()
    body = preview["body"]
    assert body["client"]["mode"] == "diagnostics"
    counts = [row["n"] for event in body["events"] if event["type"] == "tool.summary"
              for row in event["rows"]]
    assert sum(counts) == 2
    assert "http" not in json.dumps(body)


def test_reset_mints_a_new_id_and_clears_the_queue(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    t.count("tool.summary", "open", tool="roi")
    t.sync()
    before = client().get("/telemetry/status").get_json()["install_id"]
    after = client().post("/telemetry/reset", json={}).get_json()
    assert after["install_id"] != before and len(after["install_id"]) == 32
    assert t.queue.snapshot() == ([], [])


def test_settings_page_has_the_section():
    page = client().get("/settings").get_data(as_text=True)
    assert 'id="settings_panel_telemetry"' in page
    assert 'name="settings_telemetry"' in page
