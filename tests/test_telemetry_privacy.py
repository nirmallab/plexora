"""The golden privacy test: a session full of personal strings, and an upload
that contains none of them.

Everything a real session does that could carry a name is done here with a
name worth catching -- a project called `patient_29384` under
`.../users/alice/...`, markers `CD45` and `TP53`, agent calls whose arguments
name all of those, a browser post that tries to smuggle them, an exception
whose message is a path, a node with a telling name -- and then the next
upload, built by the same code the uploader uses, is searched for every one.
"""

import getpass
import json
import socket

import pytest

import plexora
from plexora.agent import registry
from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel
from plexora.server.models import data_model
from plexora.telemetry import node_hooks, report, schema
from tests.brightfield_fixtures import write_planar_fluorescence

NAME = "patient_29384"
MARKERS = ("CD45", "TP53", "DAPI")


class Args(AgentModel):
    project: str | None = None
    note: str = ""


@pytest.fixture
def session(telemetry_enabled, tmp_path, monkeypatch):
    from plexora.datasource import register_image_datasource

    t, fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    home = tmp_path / "scratch" / "users" / "alice" / "data"
    home.mkdir(parents=True)
    path = write_planar_fluorescence(home / f"{NAME}.ome.tif", height=512, width=512,
                                     names=MARKERS)
    entry = register_image_datasource(NAME, path)
    key = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]
    return t, fake, home, key


def forbidden(tmp_path, home):
    strings = [NAME, "alice", *MARKERS[:2], str(tmp_path), str(home), "/", "\\", "http",
               "@", "secret.csv"]
    try:
        strings.append(getpass.getuser())
    except Exception:
        pass
    host = socket.gethostname()
    strings += [host, host.split(".")[0]]
    return [s for s in strings if s and len(s) >= 1]


def test_nothing_personal_reaches_the_upload(session, tmp_path, monkeypatch):
    t, fake, home, key = session
    client = plexora.app.test_client()

    # The viewer: config, a load, forty tiles.
    assert client.get("/config").status_code == 200
    data_model.load_datasource(NAME)
    for i in range(40):
        assert client.get(f"/generated/data/{NAME}/{key}/0/0_0.png?i={i}").status_code == 200

    # Agents, one call failing with a message full of names.
    registry._reset_for_tests()
    registry.register(Capability(name="project.inspect", owner="core", purpose="p",
                                 permission="read", input_model=Args,
                                 handler=lambda call, inp: {"ok": 1}))

    def fail(call, inp):
        raise AgentError("precondition_missing",
                         f"{NAME} at {home} has no mask for CD45 (alice@lab.org)")

    registry.register(Capability(name="roi.create", owner="roi", purpose="p",
                                 permission="read", input_model=Args, handler=fail))
    hostile = {"project": NAME, "note": f"{home}/secret.csv CD45 TP53 alice@lab.org"}
    registry.invoke(object(), "project.inspect", hostile)
    registry.invoke(object(), "roi.create", hostile)
    registry._reset_for_tests()

    # A browser tab: one honest post, one trying to smuggle names in.
    honest = {"schema": 1, "viewer_id": "ab" * 8, "rows": [
        {"e": "error.fingerprint", "k": "n",
         "d": {"where": "browser", "fp": "0a1b2c3d", "component": "viewer", "action": "render",
               "file": "imageViewer.js"}, "n": 1}]}
    assert client.post("/telemetry/ingest", json=honest).status_code == 204
    smuggled = {"schema": 1, "viewer_id": "cd" * 8, "rows": [
        {"e": "feature.summary", "k": "n", "d": {"feature": "gene.add", "plugin": "core",
                                                 "gene": "TP53"}, "n": 1}]}
    assert client.post("/telemetry/ingest", json=smuggled).status_code == 400

    # A server exception whose message is a path.
    def explode():
        raise FileNotFoundError(f"{home}/{NAME}.ome.tif")

    explode.__module__ = "plexora.server.routes.data_routes"
    monkeypatch.setitem(plexora.app.view_functions, "get_channel_names", explode)
    monkeypatch.setitem(plexora.app.config, "PROPAGATE_EXCEPTIONS", False)
    assert client.get("/get_channel_names").status_code == 500

    # A node with a telling name, sending a hostile block.
    node_hooks._reset_for_tests()
    node_hooks.fold(f"alice-{NAME}", {"telemetry": {"since": str(home), "tiles": {}}}, 10.0)

    t.sync()
    t.sync()
    preview = report.preview()
    text = json.dumps(preview["body"])
    for leak in forbidden(tmp_path, home):
        assert leak not in text, leak
    types = {e["type"] for e in preview["body"]["events"]}
    assert {"server.summary", "project.load", "dataset.opened", "capability.summary",
            "error.fingerprint"} <= types
    for event in preview["body"]["events"]:
        assert schema.validate_upload_event(event), event["type"]


def test_the_second_line_drops_what_the_schema_let_through(session, tmp_path, monkeypatch):
    """If a schema field were ever too loose, redaction still drops the event."""
    t, _fake, home, _key = session
    monkeypatch.setattr(schema, "validate_record", lambda *a, **k: True)
    monkeypatch.setattr(schema, "strip_record", lambda event, props, mode: props)
    t.queue.add_events("2026-01-01T10", [("project.load",
                                          json.dumps({"outcome": f"{home}/{NAME}"}), 3)])
    preview = report.preview()
    assert preview["dropped"] >= 1
    assert NAME not in json.dumps(preview["body"])
