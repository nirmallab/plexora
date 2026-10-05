"""Thresholding's client and routes for automatic gating.

The client checks run `tests/js/gating_agent_probe.mjs` (the real listeners and
controller methods); the route checks drive the Flask routes the panel calls:
a locked gate survives the sidebar's own save, provenance is readable, and a
mirrored session can be paused or taken over from the tab.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

#: These exercise Paid (AI) capabilities, so they run with a test licence
#: installed; what Free refuses is tests/test_licensing_enforcement.py's job.
pytestmark = pytest.mark.paid

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_agent_probe.mjs"

CHECKS = [
    "a preview moves the slider and draws, without a save event",
    "the previewed gate never enters the list the sidebar saves",
    "a user's own move ends the preview",
    "a preview on a marker the panel cannot gate is refused, not ignored",
    "a locked gate reverted by the server is re-read and said out loud",
    "provenance reads as a few words",
    "the pill offers Take over only; pause and resume live in the agent panel",
    "a restore puts the marker and the stored gate back",
    "a failed or empty marker reads as a gate at the maximum",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT,
                          timeout=120)


def test_the_agent_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert probe.stdout.count("PASS ") == len(CHECKS)


# -- routes ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    import plexora
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path, grid=8, size=384)
    return plexora.app.test_client()


def _save(client, gates):
    return client.post("/plugins/gating/save_gating_list", data=json.dumps({
        "datasource": "gsynth", "filter": {},
        "channels": {name: list(value) for name, value in gates.items()}}))


def _saved(name="gsynth"):
    from plexora.plugins.gating.server import model

    return {row["channel"]: row["gate_start"] for row in model.get_saved_gating_list(name)}


def test_a_locked_gate_survives_the_sidebars_save(client):
    assert _save(client, {"CD8": (120.0, 5000.0), "CD3": (80.0, 5000.0)}).status_code == 200
    locked = client.post("/plugins/gating/set_gate_status", data=json.dumps({
        "datasource": "gsynth", "marker": "CD8", "status": "locked"}))
    assert locked.get_json()["status"] == "locked"
    answer = _save(client, {"CD8": (40.0, 5000.0), "CD3": (90.0, 5000.0)}).get_json()
    assert answer["reverted"] == ["CD8"]
    assert _saved() == {"CD8": 120.0, "CD3": 90.0}
    provenance = client.get("/plugins/gating/get_gate_provenance?datasource=gsynth").get_json()
    assert provenance["provenance"]["CD8"]["status"] == "locked"
    unlocked = client.post("/plugins/gating/set_gate_status", data=json.dumps({
        "datasource": "gsynth", "marker": "CD8", "status": "unlocked"}))
    assert unlocked.get_json()["status"] != "locked"
    assert _save(client, {"CD8": (40.0, 5000.0)}).get_json()["reverted"] == []


def test_a_status_the_panel_does_not_offer_is_refused(client):
    refused = client.post("/plugins/gating/set_gate_status", data=json.dumps({
        "datasource": "gsynth", "marker": "CD8", "status": "deleted"}))
    assert refused.status_code == 400


def test_a_mirrored_session_is_paused_and_taken_over_from_the_tab(client):
    from plexora.agent import AgentSession, invoke, jobs, registry
    from plexora.plugins.gating.server.autogate import provenance

    registry.discover(["gating"])
    session = AgentSession()
    started = invoke(session, "gating_session_start", {"scope": "project",
                                                       "project": "gsynth"})
    assert started["ok"], started
    sid = started["result"]["session_id"]
    jobs.drain(60)
    paused = client.post(f"/plugins/gating/agent_session/{sid}/control",
                         data=json.dumps({"action": "pause"})).get_json()
    assert paused["control"]["paused"] is True
    answer = invoke(session, "gating_next", {"session_id": sid})
    assert answer["ok"] and answer["result"]["state"] == "paused"
    client.post(f"/plugins/gating/agent_session/{sid}/control",
                data=json.dumps({"action": "resume"}))
    taken = client.post(f"/plugins/gating/agent_session/{sid}/control", data=json.dumps({
        "action": "take_over", "marker": "CD8", "datasource": "gsynth"})).get_json()
    assert taken["control"]["paused"] is True and "CD8" in taken["control"]["locked_markers"]
    assert provenance.status_of("gsynth", "CD8") == "locked"
    missing = client.post("/plugins/gating/agent_session/gs_nope/control",
                          data=json.dumps({"action": "pause"}))
    assert missing.status_code == 404


def test_the_tab_detaches_and_reattaches_a_running_session(client, monkeypatch):
    """Continue in background / Watch in viewer: the session's control says
    whether it mirrors, the record is set to send again, and every `control`
    event the tabs are told carries the whole picture."""
    from plexora.agent import AgentSession, invoke, jobs, registry
    from plexora.agent.sessions import control as session_control
    from plexora.plugins.gating.server import routes
    from plexora.plugins.gating.server.autogate import engine

    registry.discover(["gating"])
    started = invoke(AgentSession(), "gating_session_start", {"scope": "project",
                                                              "project": "gsynth"})
    assert started["ok"], started
    sid = started["result"]["session_id"]
    jobs.drain(60)
    told = []
    monkeypatch.setattr(routes, "_tell_tabs",
                        lambda session_id, event, record=None, **payload: told.append(
                            (event, payload)))
    replays = []
    monkeypatch.setattr(session_control, "replayer",
                        lambda *a, **k: lambda *args: replays.append(args))

    def post(**body):
        return client.post(f"/plugins/gating/agent_session/{sid}/control",
                           data=json.dumps(body)).get_json()

    # No tab here, so the mirror is off: a pause must not tell the tabs the
    # session is attached just because nobody detached it.
    assert engine.store().load(sid)["mirror"]["status"] == "off"
    post(action="pause")
    assert told[-1][0] == "control" and told[-1][1]["viewer_attached"] is False
    post(action="resume")
    detached = post(action="detach_viewer")
    assert detached["control"]["viewer_detached"] is True
    event, payload = told[-1]
    assert event == "control" and payload["viewer_attached"] is False
    assert payload["paused"] is False and "view_id" in payload
    attached = post(action="attach_viewer", view_id="v_tab")
    assert attached["control"]["viewer_detached"] is False
    mirror = engine.store().load(sid)["mirror"]
    assert mirror["enabled"] is True and mirror["status"] == "pending"
    assert mirror["view_id"] == "v_tab"
    assert told[-1][1]["viewer_attached"] is True and told[-1][1]["view_id"] == "v_tab"
    assert len(replays) == 1 and replays[0][0] == sid
    taken = post(action="take_over")
    assert taken["control"]["paused"] is True and taken["control"]["viewer_detached"] is True
    event, payload = told[-1]
    assert event == "control" and payload["taken_over"] is True
    assert payload["viewer_attached"] is False and payload["paused_by"] == "viewer"
    resumed = post(action="resume")
    assert resumed["control"]["paused"] is False
    assert resumed["control"]["viewer_detached"] is True        # resume never re-attaches
