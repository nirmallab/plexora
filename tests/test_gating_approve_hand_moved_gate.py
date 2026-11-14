"""Approving or locking a gate the user moved by hand keeps it the user's.

An agent writes CD8; the user then drags it in the sidebar (whose save never
touches provenance, so the only sign of the edit is the stored gate no longer
equalling what the agent wrote). Approve or Lock under the gate is the user
signing off THEIR value: the gates file and the gate versions must still say
the user set it, and once the approval or lock is lifted, the next automatic
run must keep the gate as hand-set (overwrite_manual off) instead of treating
it as the agent's own and replacing it.
"""

import types

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.autogate_fixtures import make_gating_project

pytestmark = pytest.mark.pro

SIGN_OFFS = [("approved", "unapproved"), ("locked", "unlocked")]


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _ds(name):
    from plexora import api

    return api.project_data(name)


def _agent_gate_moved_by_hand(tmp_path):
    """An agent's CD8 gate (low 5.0), then the sidebar's own save with it
    moved to 5.5. Returns the app's test client."""
    import plexora
    from plexora.plugins.gating.server import model, provenance

    make_gating_project(tmp_path)
    agent = invoke(AgentSession(), "set_gate",
                   {"project": "gsynth", "marker": "CD8", "low": 5.0})
    assert agent["ok"], agent.get("error")
    client = plexora.app.test_client()
    ds = _ds("gsynth")
    _, revision = model.saved_gating_list_at("gsynth")
    channels = {ch: [lo, hi] for ch, (lo, hi) in model.default_ranges(ds).items()}
    gates = {ch: [lo, hi] for ch, (lo, hi) in model.active_gates(ds).items()}
    gates["CD8"] = [5.5, gates["CD8"][1]]
    saved = client.post("/plugins/gating/save_gating_list",
                        json={"datasource": "gsynth", "filter": gates,
                              "channels": channels, "base": revision})
    assert saved.status_code == 200
    gate = model.get_gate(_ds("gsynth"), "CD8")
    assert gate["low"] == 5.5 and gate["thresholded"]
    row = provenance.read("gsynth")["CD8"]
    # Before any sign-off the edit is seen, and the gate is the user's.
    assert provenance.edited_since(row, gate)
    assert model._gated_by(row, gate["low"], gate["high"]) == model.GATED_BY_USER
    return client


def _set_status(client, status):
    response = client.post("/plugins/gating/set_gate_status",
                           json={"datasource": "gsynth", "marker": "CD8", "status": status})
    assert response.status_code == 200 and response.get_json()["status"] in (
        status, "accepted", "proposed")


@pytest.mark.parametrize("status, undo", SIGN_OFFS)
def test_signing_off_a_hand_moved_agent_gate_still_credits_the_user(tmp_path, status, undo):
    """The gates file's gated_by and a new gate version's `by` say User, not
    Plexora AI, for an agent's gate the user moved and then approved/locked."""
    from plexora.plugins.gating.server import model, provenance

    client = _agent_gate_moved_by_hand(tmp_path)
    _set_status(client, status)

    ds = _ds("gsynth")
    gate = model.get_gate(ds, "CD8")
    assert gate["low"] == 5.5
    row = provenance.read("gsynth")["CD8"]
    assert model._gated_by(row, gate["low"], gate["high"]) == model.GATED_BY_USER, row
    channels = {ch: (lo, hi) for ch, (lo, hi) in model.default_ranges(ds).items()}
    sheet = model.download_gates("gsynth", model.active_gates(ds), channels)
    by = dict(zip(sheet["channel"].to_list(), sheet["gated_by"].to_list()))
    assert by["CD8"] == model.GATED_BY_USER
    version = model.save_version(ds)
    assert version["by"] == model.GATED_BY_USER


@pytest.mark.parametrize("status, undo", SIGN_OFFS)
def test_a_lifted_sign_off_leaves_the_hand_moved_gate_to_the_user(tmp_path, monkeypatch,
                                                                   status, undo):
    """After Approve/Lock and then Unapprove/Unlock, an automatic run with
    overwrite_manual off closes the marker skipped_manual instead of
    re-gating it as the agent's own."""
    from plexora.plugins.gating.server import provenance
    from plexora.plugins.gating.server.autogate import bulk, views

    client = _agent_gate_moved_by_hand(tmp_path)
    _set_status(client, status)
    _set_status(client, undo)
    assert provenance.read("gsynth")["CD8"]["status"] not in ("approved", "locked")

    # Pixels are not what this is about: a unit that would be re-gated stops
    # at a stand-in profile instead of reading the image.
    monkeypatch.setattr(views, "image_channel", lambda ds, marker: marker)
    monkeypatch.setattr(views, "full_profile", lambda *a, **k: {"stub": True})
    call = types.SimpleNamespace(session=AgentSession())
    prepared = bulk.prepare_unit(call, "s-audit", "gsynth", "CD8", {"entries": {}},
                                 {"overwrite_manual": False, "seed": 0})
    assert prepared.get("close", (None,))[0] == "skipped_manual", \
        (provenance.read("gsynth")["CD8"], prepared)
