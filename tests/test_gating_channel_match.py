"""Automatic gating judges every gate on the pixels, so a marker the image has
no channel for is never gated -- the lesson of a run (lsp35766_p193,
2026-10-15) whose channels still carried placeholder names: it gated all 37
markers from their histograms alone, wrote every gate, and never looked.

What these pin: a session with no marker matching a channel never starts; a
partial match refuses without `force` and gates only the matched markers with
it; a channel lost after the start closes its marker unwritten; there is no
numbers-only tier; the bulk pass reads the panel context per marker and says
what it decided; and the panel's Clear puts markers back to never gated.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.autogate_fixtures import make_gating_project

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _rename_channels(data_root, rename, name="gsynth"):
    """Rename the image's channels in the registry, as an import that named
    them by their file keys left them."""
    path = data_root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    for channel in config[name]["imageData"]:
        if channel.get("fullname") == "Area":
            continue
        new = rename(channel.get("fullname") or channel.get("name"))
        channel["name"] = channel["fullname"] = new
    path.write_text(json.dumps(config), encoding="utf-8")


def _start(session, **options):
    return invoke(session, "gating_session_start",
                  {"scope": "project", "project": "gsynth", **options})


def _units(session, session_id):
    result = invoke(session, "gating_session_status", {"session_id": session_id})
    assert result["ok"], result.get("error")
    return {u["marker"]: u for u in result["result"]["units"]}


def _placeholder_keys():
    """`lsp35766-p193-ome-e861978e_<n>`: what the failed run's channels were
    called, one number per channel in order."""
    count = iter(range(1000))
    return lambda _name: f"lsp35766-p193-ome-e861978e_{next(count)}"


def test_no_marker_with_a_channel_never_starts(tmp_path):
    make_gating_project(tmp_path)
    _rename_channels(tmp_path, _placeholder_keys())
    session = AgentSession()
    for force in (False, True):
        refused = _start(session, force=force)
        assert not refused["ok"]
        error = refused["error"]
        assert error["code"] == "precondition_missing"
        assert "none of the 3 markers" in error["message"]
        assert sorted(error["detail"]["unmatched"]) == ["CD20", "CD3", "CD8"]
    # Nothing was started, so nothing was written.
    gates = invoke(session, "get_all_gates", {"project": "gsynth"})["result"]
    assert not gates["thresholded"]


def test_a_partial_match_refuses_without_force(tmp_path):
    make_gating_project(tmp_path)
    _rename_channels(tmp_path, lambda n: "channel_7" if n == "CD20" else n)
    refused = _start(AgentSession())
    assert not refused["ok"]
    error = refused["error"]
    assert error["code"] == "precondition_missing"
    assert error["detail"]["unmatched"] == ["CD20"]
    assert "force" in error["message"]


def test_force_gates_only_the_markers_with_a_channel(tmp_path):
    make_gating_project(tmp_path)
    _rename_channels(tmp_path, lambda n: "channel_7" if n == "CD20" else n)
    session = AgentSession()
    started = _start(session, force=True)
    assert started["ok"], started.get("error")
    out = started["result"]
    assert out["channels"] == {"state": "partial", "n_markers": 3, "n_matched": 2,
                               "unmatched": ["CD20"]}
    jobs.drain(120)
    units = _units(session, out["session_id"])
    assert units["CD20"]["state"] == "skipped_no_channel"
    assert units["CD3"]["state"] != "skipped_no_channel"
    gate = invoke(session, "get_gate", {"project": "gsynth", "marker": "CD20"})["result"]
    assert not gate["gate"]["thresholded"]
    from plexora.plugins.gating.server import provenance

    assert "CD20" not in provenance.read("gsynth")


def test_a_channel_lost_after_the_start_closes_its_marker_unwritten(tmp_path, monkeypatch):
    """The start saw every channel; by the bulk pass one is gone (renamed in
    the viewer meanwhile). The marker is skipped, never profiled or written --
    there is no numbers-only gate to fall back on."""
    from plexora.plugins.gating import capabilities_session as cs
    from plexora.plugins.gating.server.autogate import views

    make_gating_project(tmp_path)
    real = views.image_channel
    monkeypatch.setattr(views, "image_channel",
                        lambda ds, marker: None if marker == "CD8" else real(ds, marker))
    original = cs.channel_match

    def as_at_start(call, images, markers):
        # What the start saw: every channel still there.
        return {**original(call, images, markers), "state": "full",
                "n_matched": len(markers), "matched": {p: list(markers) for p in images},
                "unmatched": {p: [] for p in images}}

    monkeypatch.setattr(cs, "channel_match", as_at_start)
    session = AgentSession()
    started = _start(session)
    assert started["ok"], started.get("error")
    jobs.drain(120)
    units = _units(session, started["result"]["session_id"])
    assert units["CD8"]["state"] == "skipped_no_channel"
    gate = invoke(session, "get_gate", {"project": "gsynth", "marker": "CD8"})["result"]
    assert not gate["gate"]["thresholded"]


def test_there_is_no_numbers_only_tier(tmp_path):
    make_gating_project(tmp_path)
    refused = _start(AgentSession(), max_tier=1)
    assert not refused["ok"]
    assert refused["error"]["code"] == "invalid_input"


def test_the_bulk_pass_says_what_it_decided_marker_by_marker(tmp_path, monkeypatch):
    """Each settled marker goes to the tabs as a `phase` event whose progress
    carries the bulk stage (core's agent panel shows it), and the stage is
    cleared once the pass is over."""
    from plexora.plugins.gating import capabilities_session as cs

    make_gating_project(tmp_path)
    heard = []
    real = cs._announce

    def listen(call, record, session_id, event, /, **payload):
        heard.append((event, payload))
        return real(call, record, session_id, event, **payload)

    monkeypatch.setattr(cs, "_announce", listen)
    session = AgentSession()
    started = _start(session)
    assert started["ok"], started.get("error")
    jobs.drain(120)
    stages = [p["progress"]["bulk"] for e, p in heard
              if e == "phase" and (p.get("progress") or {}).get("bulk", {}).get("stage")]
    assert stages, heard
    assert all(s["stage"] == "profiling" for s in stages)
    assert stages[-1]["done"] == stages[-1]["total"] == 3
    assert any(s["message"].split(":")[0] in ("CD3", "CD8", "CD20") for s in stages)
    from plexora.plugins.gating.server.autogate.engine import store

    record = store().load(started["result"]["session_id"])
    assert "bulk_progress" not in record


def test_the_bulk_pass_reads_the_panel_context_for_each_marker(tmp_path, monkeypatch):
    """Not once before its loop: the panel's context packet is answered while
    the pass runs, and a snapshot taken before the answer overwrote it on
    every marker settled after it."""
    from plexora.plugins.gating.server.autogate import bulk

    make_gating_project(tmp_path)
    panels = []
    real = bulk.prepare_unit

    def seen(call, session_id, project, marker, panel, options):
        panels.append(panel)       # held, so no two can share an id
        return real(call, session_id, project, marker, panel, options)

    monkeypatch.setattr(bulk, "prepare_unit", seen)
    session = AgentSession()
    started = _start(session)
    assert started["ok"], started.get("error")
    jobs.drain(120)
    assert len(panels) == 3
    assert len({id(panel) for panel in panels}) == 3


def test_a_small_floor_spike_on_a_large_image_is_still_left_out():
    """83-155 unmeasured cells of 820,618 were a mixture component of their
    own (gate ~0.03, 99.98% positive): under 0.1% of the cells, but counted
    now, since FLOOR_MIN_CELLS caps how many a spike must have."""
    from plexora.plugins.gating.server import model

    rng = np.random.default_rng(0)
    body = rng.normal(5.2, 0.2, 200_000)
    for spike in (60, 150):
        fit = np.sort(np.concatenate([np.zeros(spike), body]))
        assert spike < model.FLOOR_MIN_FRACTION * fit.size
        assert model.floor_spike(fit) == spike
    # A handful is not a spike: a few stragglers at the minimum stay in.
    fit = np.sort(np.concatenate([np.zeros(5), body]))
    assert model.floor_spike(fit) == 0


def test_clear_puts_markers_back_to_never_gated_and_keeps_a_locked_one(tmp_path):
    import plexora
    from plexora.plugins.gating.server import model, provenance

    make_gating_project(tmp_path)
    session = AgentSession()
    for marker, low in (("CD3", 6.0), ("CD8", 5.0), ("CD20", 4.0)):
        assert invoke(session, "set_gate", {"project": "gsynth", "marker": marker,
                                            "low": low})["ok"]
    assert invoke(session, "set_gate_status", {"project": "gsynth", "marker": "CD20",
                                               "status": "locked"})["ok"]
    client = plexora.app.test_client()

    one = client.post("/plugins/gating/clear_gates",
                      json={"datasource": "gsynth", "markers": ["CD3"]})
    assert one.status_code == 200
    assert one.get_json() == {"cleared": ["CD3"], "kept": {}}
    gates = {g["marker"]: g for g in model.all_gates(_ds("gsynth"))}
    assert not gates["CD3"]["thresholded"] and gates["CD8"]["thresholded"]
    assert "CD3" not in provenance.read("gsynth")

    every = client.post("/plugins/gating/clear_gates", json={"datasource": "gsynth"})
    assert every.status_code == 200
    body = every.get_json()
    assert body["kept"] == {"CD20": "locked"} and "CD8" in body["cleared"]
    gates = {g["marker"]: g for g in model.all_gates(_ds("gsynth"))}
    assert not gates["CD8"]["thresholded"]
    assert gates["CD20"]["thresholded"] and gates["CD20"]["low"] == 4.0
    assert provenance.read("gsynth")["CD20"]["status"] == "locked"

    unknown = client.post("/plugins/gating/clear_gates",
                          json={"datasource": "gsynth", "markers": ["NOPE"]})
    assert unknown.status_code == 400


def _ds(name):
    from plexora import api

    return api.project_data(name)


def test_a_skipped_marker_is_not_quoted():
    """The harness declares a run at the units the session will gate: a
    marker skipped for having no image channel is not one of them."""
    from plexora.ai.harness.decision import GatingOptions, GatingWorkflow

    options = GatingOptions(project="p")
    flow = GatingWorkflow()
    assert flow.units_started({"n_units": 39, "channels": {"unmatched": ["A", "B"]}},
                              options) == 37
    assert flow.units_started({"n_units": 3, "channels": {"unmatched": []}}, options) == 3
    assert flow.units_started({"n_units": 3}, options) == 3


def test_the_launcher_is_told_before_a_partial_run_starts():
    """`ai.balance`'s gating estimate: none matching blocks the run with the
    reason; some matching asks first, and a yes carries `force`."""
    from plexora.ai.harness.capabilities import _gating_estimate

    order = ["CD3", "CD8", "CD20"]
    none = {"matched": {"p": []}, "unmatched": {"p": order}, "n_markers": 3,
            "n_matched": 0, "state": "none"}
    units, why, words = _gating_estimate(order, none)
    assert units == 0 and why is None and "None of the 3 markers" in words["blocked"]
    partial = {"matched": {"p": ["CD3", "CD8"]}, "unmatched": {"p": ["CD20"]},
               "n_markers": 3, "n_matched": 2, "state": "partial"}
    units, _why, words = _gating_estimate(order, partial)
    assert units == 2
    assert words["confirm"]["start_options"] == {"force": True}
    assert "CD20" in words["confirm"]["body"]
    full = {"matched": {"p": order}, "unmatched": {"p": []}, "n_markers": 3,
            "n_matched": 3, "state": "full"}
    units, _why, words = _gating_estimate(order, full)
    assert units == 3 and "confirm" not in words and "blocked" not in words
