"""What a live AutoQC run found too big or too wrong to use, trimmed and
pinned: a 40-channel panel's own name repeated on every region and every
neighbour, a pause that read back the whole session, a bulk pass that never
stopped saying its last stage, a fold that flagged forty clean channels, a
final review that lost its own recommendation, an answer schema that did not
say how long a string could be, and an auto mirror that gave up with two tabs
open on the one project it was asked about.
"""

import json
import types

import pytest

from plexora.agent import invoke
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, _qc, drive, ok, start  # noqa: F401

pytestmark = pytest.mark.paid


def _record(session_id):
    from plexora.plugins.qc.server.engine import store

    return store().load(session_id)


# -- 1. payload trim: channel lists collapse, neighbours shrink to counts ----------------


def _fake_scan(n=40, per_cycle=20):
    names = [f"M{i:02d}" for i in range(n)]
    cycles = [{"index": i // per_cycle + 1, "channels": names[i:i + per_cycle]}
             for i in range(0, n, per_cycle)]
    scan = types.SimpleNamespace(channels=[{"name": nm} for nm in names],
                                 meta={"cycles": {"cycles": cycles}})
    return scan, names, cycles


def test_a_channel_list_equal_to_all_or_to_a_cycle_collapses_to_a_token():
    from plexora.plugins.qc.server import packets

    scan, names, cycles = _fake_scan()
    assert packets._channel_token(list(names), scan) == "all_channels"
    assert packets._channel_token(list(cycles[0]["channels"]), scan) == "cycle 1"
    assert packets._channel_token(list(cycles[1]["channels"]), scan) == "cycle 2"
    # A genuine subset (not the whole panel, not a whole cycle) is untouched.
    subset = [names[0], names[3], names[21]]
    assert packets._channel_token(subset, scan) == subset
    assert packets._channel_token([], scan) == []
    assert packets._channel_token(None, scan) is None


def test_final_review_regions_shrink_with_a_forty_channel_panel():
    """Before: 56 regions each repeating a 40-channel (or 20-channel cycle)
    list and a three-field refinement dict. After: the list is a token, the
    refinement a status word."""
    from plexora.plugins.qc.server import packets

    scan, names, cycles = _fake_scan()
    cycle1 = cycles[0]["channels"]

    def before(i, channels):
        return {"label": f"r{i}", "candidate": f"c{i}", "class": "tissue_fold",
                "action": "exclude", "geometry": {"type": "Polygon", "coordinates": []},
                "channels": channels, "tissue_fraction": 0.012, "refined_fraction": 0.0108,
                "refinement": {"status": "refined", "method": "aperture",
                              "kept_fraction": 0.9}}

    regions = [before(i, cycle1 if i % 2 else names) for i in range(56)]
    before_json = json.dumps({"regions": regions})
    after_json = json.dumps({"regions": [packets._compact_region(r, scan) for r in regions]})
    print(f"final_qc_review regions: before={len(before_json)} chars, "
         f"after={len(after_json)} chars")
    assert len(after_json) < len(before_json) * 0.35
    compacted = [packets._compact_region(r, scan) for r in regions]
    assert all(r["channels"] in ("all_channels", "cycle 1") for r in compacted)
    assert all(r["refinement"] == "refined" for r in compacted)
    assert all("geometry" not in r and "refined_fraction" not in r for r in compacted)


def test_artifact_confirm_candidate_and_neighbours_shrink():
    """Before: the candidate's own channels and every neighbour's channels
    spelled out in full (a crowded field can have several). After: the
    candidate's collapse like a region's; a neighbour is a count."""
    from plexora.plugins.qc.server import packets

    scan, names, cycles = _fake_scan()

    class FakeEngine:
        def __init__(self, units):
            self._units = units

        def mask_of(self, unit):
            return unit["mask"]

        def units_of(self, kind, project=None):
            return [u for u in self._units if u["type"] == kind]

    import numpy as np

    mask = np.zeros((10, 10), dtype=bool)
    mask[2:6, 2:6] = True
    unit = {"type": "candidate", "id": "c0", "project": "p", "mask": mask,
           "channels": list(names)}
    neighbours_units = []
    for i in range(6):
        nmask = np.zeros((10, 10), dtype=bool)
        nmask[3:5, 3:5] = True
        neighbours_units.append({"type": "candidate", "id": f"n{i}", "state": "confirmed_warn",
                                 "mask": nmask, "channels": list(cycles[i % 2]["channels"])})
    engine = FakeEngine([unit, *neighbours_units])

    before_candidate = {"id": unit["id"], "channels": list(names)}
    before_neighbours = [{"candidate": u["id"], "channels": u["channels"], "iou": 0.16,
                          "state": u["state"]} for u in neighbours_units]
    before_json = json.dumps({"candidate": before_candidate, "neighbours": before_neighbours})

    after_candidate = packets._brief(unit, None, scan)
    after_neighbours = packets._neighbours(engine, unit, "p")
    after_json = json.dumps({"candidate": {"id": after_candidate["id"],
                                          "channels": after_candidate["channels"]},
                             "neighbours": after_neighbours})
    print(f"artifact_confirm candidate+neighbours: before={len(before_json)} chars, "
         f"after={len(after_json)} chars")
    assert len(after_json) < len(before_json) * 0.5
    assert after_candidate["channels"] == "all_channels"
    assert all("channels" not in n and n["n_channels"] == 20 for n in after_neighbours)


# -- 2. status/finish: a short pause ack, a capped default, detail=full for the rest -----


def test_pause_and_resume_return_a_short_ack(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    from plexora.agent import AgentSession

    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    paused = ok(invoke(session, "qc_session_status", {"session_id": sid, "pause": True}))
    assert set(paused) == {"session_id", "paused", "state", "progress"}
    assert paused["paused"] is True
    # Four keys, `progress` the engine's own counts (by_state, by_type) --
    # nowhere near the 30k of every unit, the residual and the vocabulary.
    assert len(json.dumps(paused)) < 2000
    assert _record(sid)["state"] != "cancelled"
    resumed = ok(invoke(session, "qc_session_status", {"session_id": sid, "pause": False}))
    assert resumed["paused"] is False
    ok(invoke(session, "qc_session_finish", {"session_id": sid, "action": "rollback"}))


def test_default_status_is_brief_and_full_asks_for_the_rest(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    from plexora.agent import AgentSession

    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    brief = ok(invoke(session, "qc_session_status", {"session_id": sid}))
    assert "strictness" not in brief and "vocabulary" not in brief
    assert "residual_totals" in brief
    assert len(brief["residual"]) <= 20
    assert "units_omitted" in brief
    full = ok(invoke(session, "qc_session_status", {"session_id": sid, "detail": "full"}))
    assert full["strictness"] and full["vocabulary"]["terminal_states"]
    ok(invoke(session, "qc_session_finish", {"session_id": sid, "action": "rollback"}))


def test_many_open_units_are_capped_with_a_count_of_the_rest():
    """A bulk pass in progress has every unit still `open` -- the 129-unit,
    30k-character status. `listed_units` + the status/finish cap do not need
    a real session to prove: a synthetic record of many open units is
    capped, with the rest only counted. `units="all"` is an explicit ask for
    everything (a benchmark reading back every channel, say) and is not cut
    down to the tight default -- only to the older, looser list cap."""
    from plexora.plugins.qc import capabilities_session as cs
    from plexora.agent.sessions import tools as session_tools

    record = {"units": {f"k{i}": {"type": "channel", "id": f"CH{i}", "state": "pending"}
                        for i in range(129)}}
    units = [cs.TOOLS.unit_row(u) for u in cs.TOOLS.listed_units(record, "open")]
    assert len(units) == 129
    shown, omitted = session_tools._capped(units, "open")
    assert len(shown) == session_tools.MAX_STATUS_UNITS
    assert omitted == 129 - session_tools.MAX_STATUS_UNITS
    shown_all, omitted_all = session_tools._capped(units, "all")
    assert len(shown_all) == 129 and omitted_all == 0


# -- 3. the bulk pass clears its own last stage, success or failure ----------------------


def test_bulk_progress_is_cleared_when_the_pass_raises(monkeypatch):
    from plexora.plugins.qc.server import bulk

    class _FakeEngine:
        def __init__(self):
            self.record = {"bulk_progress": {"stage": "cells", "message": "channel_outlier:X",
                                             "done": 43, "total": 44}}

    class _FakeContext:
        def __init__(self, engine):
            self.engine = engine

        def __enter__(self):
            return self.engine

        def __exit__(self, *exc):
            return False

    engine = _FakeEngine()
    monkeypatch.setattr(bulk, "engine_for", lambda call, session_id: _FakeContext(engine))

    def boom(*a, **k):
        raise RuntimeError("the scan blew up")

    monkeypatch.setattr(bulk, "_run_bulk", boom)
    inp = types.SimpleNamespace(session_id="qs_boom")
    with pytest.raises(RuntimeError):
        bulk.run(object(), inp)
    assert engine.record["bulk_progress"] is None


def test_bulk_progress_is_cleared_once_a_real_pass_finishes(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    from plexora.agent import AgentSession

    session = AgentSession()
    started = start(session)       # drains the bulk job
    record = _record(started["session_id"])
    assert not record.get("bulk_progress")
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"],
                                             "action": "rollback"}))


# -- 4. a whole-tissue region reaches channels, it does not flag them --------------------


def test_a_whole_scope_region_reaches_channels_without_flagging_them():
    """The live run's `channels {flagged: 22, clean: 18}` though the audit
    flagged two channels: one fold confirmed with scope `all_channels`
    reached every channel it was drawn on and flagged every one of them.
    Only the channel it was actually audited and confirmed on should flag;
    the rest are `reached_by`, still clean. A channel-scoped artifact still
    flags its one channel."""
    from plexora.plugins.qc.server import finalize

    class FakeEngine:
        def __init__(self, units):
            self._units = units

        def units_of(self, kind):
            return [u for u in self._units if u["type"] == kind]

    all_channels = [f"CH{i}" for i in range(22)]
    clean_extra = [f"CL{i}" for i in range(18)]
    units = [
        {"type": "candidate", "id": "c1", "state": "confirmed_exclude",
         "channels": all_channels, "audit_channel": "CH0", "scope_hint": "all_channels",
         "class": "tissue_fold"},
        {"type": "candidate", "id": "c2", "state": "confirmed_exclude",
         "channels": ["CH1"], "audit_channel": "CH1", "scope_hint": "channel",
         "class": "antibody_aggregate"},
    ]
    for name in all_channels + clean_extra:
        units.append({"type": "channel", "id": name, "state": "clean"})
    engine = FakeEngine(units)

    finalize._settle_channel_statuses(engine)

    channels = {u["id"]: u for u in engine.units_of("channel")}
    flagged = {name for name, u in channels.items() if u["state"] == "flagged"}
    assert flagged == {"CH0", "CH1"}, flagged
    for name in all_channels:
        if name in flagged:
            continue
        assert channels[name]["state"] == "clean"
        assert channels[name]["reached_by"] == ["c1"]
    for name in clean_extra:
        assert channels[name]["state"] == "clean"
        assert "reached_by" not in channels[name]


# -- 5. a consistent-but-flagged final review stays `reviewed`, with a real reason -------


def test_recommend_manual_review_with_a_consistent_verdict_stays_reviewed(tmp_path):
    """verdict=consistent + recommend_manual_review=true closed
    `manual_review_recommended` / "the final review could not say" -- wrong
    on both counts, since the review *did* say (consistent) and *did* give a
    reason (ask a person to check)."""
    class _RecommendsReview(QCOracle):
        def answer(self, packet, session_id):
            if packet["kind"] == "final_qc_review":
                return {"kind": "final_qc_review", "verdict": "consistent",
                        "recommend_manual_review": True}
            return super().answer(packet, session_id)

    info = make_qc_project(tmp_path, artifacts=("saturation",))
    from plexora.agent import AgentSession

    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    drive(session, sid, _RecommendsReview(info))
    final = next(u for u in _record(sid)["units"].values() if u["type"] == "final")
    assert final["state"] == "reviewed", final
    assert "agent recommends a person check" in final["reason"], final["reason"]
    ok(invoke(session, "qc_session_finish", {"session_id": sid}))


# -- 6. a length-limited answer field says its limit in the schema the agent reads ------


def test_the_answer_schema_surfaces_length_limits():
    from plexora.plugins.qc.server import answers

    schema = answers.schema_for("final_qc_review")
    assert schema["properties"]["notes"]["maxLength"] == 300
    concerns = schema["properties"]["concerns"]
    assert concerns["maxItems"] == 8
    assert concerns["items"]["properties"]["target"]["maxLength"] == 40
    grid = answers.schema_for("artifact_grid")
    assert grid["properties"]["cells"]["maxItems"] == 256


# -- 8. auto mirror picks the visible, most recently seen tab of the project ------------


class _FakeControl:
    def __init__(self, sessions):
        self._sessions = sessions

    def list_sessions(self, project=None):
        return list(self._sessions)


def test_auto_mirror_prefers_the_visible_most_recent_tab_of_the_project(monkeypatch):
    from plexora.agent import viewer
    from plexora.agent.sessions import tools as session_tools

    control = _FakeControl([
        {"view_id": "a", "status": "live", "visible": False, "seconds_since_seen": 1.0},
        {"view_id": "b", "status": "live", "visible": True, "seconds_since_seen": 5.0},
        {"view_id": "c", "status": "live", "visible": True, "seconds_since_seen": 0.5},
    ])
    monkeypatch.setattr(viewer, "connect", lambda link=None: control)
    monkeypatch.setattr(viewer, "require", lambda link=None: control)

    call = types.SimpleNamespace(link=object())
    mirror = session_tools.mirror_at_start(call, None, None, "qc.session_status",
                                           project="qcsynth")
    assert mirror["status"] == "pending"
    assert mirror["view_id"] == "c"


def test_auto_mirror_still_refuses_across_different_projects(monkeypatch):
    """Two tabs on two different images is still a real ambiguity: no
    project to prefer within, so it is still refused rather than guessed."""
    from plexora.agent import viewer
    from plexora.agent.sessions import tools as session_tools

    control = _FakeControl([
        {"view_id": "a", "status": "live", "visible": True, "seconds_since_seen": 1.0},
        {"view_id": "b", "status": "live", "visible": True, "seconds_since_seen": 2.0},
    ])
    monkeypatch.setattr(viewer, "connect", lambda link=None: control)
    monkeypatch.setattr(viewer, "require", lambda link=None: control)

    call = types.SimpleNamespace(link=object())
    mirror = session_tools.mirror_at_start(call, None, None, "qc.session_status", project=None)
    assert mirror["status"] == "off"
    assert "pass mirror=true and view_id" in mirror["reason"]


# -- 3. run-3 trims: audit numbers as columns, shared metrics once, short progress ------


def test_audit_rows_carry_their_numbers_as_columns_named_once():
    from plexora.plugins.qc.server import packets

    summary = {"saturation_fraction": 0.0, "tissue_ratio": 227.0,
               "dynamic_range_decades": 0.6951, "zero_fraction": 0.0006,
               "focus_rel_p10": 0.4559, "bright_compact_fraction": 0.0064}
    rows = [{"number": n, "channel": f"M{n}", "cycle": 1, "overview": dict(summary),
             "illumination_r2": 0.01, "candidates": []} for n in range(16)]
    out = packets._audit_evidence(rows)
    assert out["overview_columns"][-1] == "illumination_r2"
    row = out["rows"][3]
    assert "illumination_r2" not in row
    assert dict(zip(out["overview_columns"], row["overview"])) == {
        **summary, "illumination_r2": 0.01}
    # Every number is still there; the names are not repeated per row.
    assert json.dumps(out).count("dynamic_range_decades") == 1


def test_a_confirm_sheet_states_what_its_candidates_share_once():
    from plexora.plugins.qc.server import packets

    common = {"check": "blur", "threshold": 0.6372, "step": 0.113,
              "fingerprint": "9798827141254bce", "score_cell_um": 40.3}
    briefs = [{"label": f"c{i}", "metrics": {**common, "score_cells": i, "mean": 0.7 + i / 100}}
              for i in range(4)]
    shared = packets._shared_metrics(briefs)
    assert shared == common
    assert briefs[2]["metrics"] == {"score_cells": 2, "mean": 0.72}
    # One candidate, or one without metrics: nothing is taken out.
    lone = [{"metrics": dict(common)}]
    assert packets._shared_metrics(lone) == {} and lone[0]["metrics"] == common
    assert packets._shared_metrics([{"metrics": None}, {"metrics": dict(common)}]) == {}


def test_an_answer_followed_by_a_packet_carries_only_the_counts(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    from plexora.agent import AgentSession

    session = AgentSession()
    sid = start(session)["session_id"]
    agent = QCOracle(info)
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    followed = 0
    for _ in range(40):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        answered = ok(invoke(session, "qc_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet, sid)}))
        if answered["next"]["state"] == "decision":
            assert set(answered["progress"]) == {"units_done", "units_total"}
            followed += 1
        else:
            assert "by_state" in answered["progress"]
        result = answered["next"]
    assert followed
    ok(invoke(session, "qc_session_finish", {"session_id": sid, "action": "rollback"}))
