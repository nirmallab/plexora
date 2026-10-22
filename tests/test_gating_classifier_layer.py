"""The Single Cell Classifier beside a gating session's gates
(`autogate/cellcalls.py`): a layer at each written gate that never decides
the marker until it is adopted, read-ahead of its features, the push, and
what commit, rollback and an old record do."""

import json

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.plugins.gating.server.classifier import calls, store
from tests.agent_fixtures import local_handles
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import HARD, Oracle, drive, ok, start, units

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _record(session_id):
    from plexora import paths

    return json.loads((paths.data_root() / ".agent" / "sessions" / "gating" / session_id
                       / "session.json").read_text())


def _events(session_id, kind):
    from plexora import paths

    path = paths.data_root() / ".agent" / "sessions" / "gating" / session_id / "events.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == kind]


def test_each_written_gate_gets_a_layer_that_does_not_decide_the_marker(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    packets = drive(session, sid, Oracle(info))
    jobs.drain(120)
    # Nothing classifier-derived is in any packet: the looks are the same
    # with the layer on or off.
    assert not any("classifier" in json.dumps(p) for p in packets)
    record = _record(sid)
    assert record["options"]["classifier"] == "layer"
    ds = local_handles("gsynth")
    final = units(session, sid)
    for marker in ("CD3", "CD8", "CD20"):
        unit = record["units"][f"gsynth::{marker}"]
        assert unit["state"] in ("accepted", "accepted_low_confidence")
        layer = unit["classifier"]
        assert layer["status"] == "ready" and layer["stored"] == "project", layer
        assert layer["gate_used"][0] == unit["final"] == final[marker]["final"]
        assert layer["call_rule"] == "gate_unless_confident"
        saved = store.session_run_record(ds.name, marker)
        assert saved["session_id"] == sid and saved["origin"] == "session"
        # The threshold decides; Thresholding's export is the gate.
        assert store.active(ds.name, marker) == "threshold"
        assert calls.marker_calls(ds, marker)[1] == "threshold"
    # The features were read while the looks were answered.
    assert set(record["classifier"]["ready"]) >= {"gsynth::CD3", "gsynth::CD8"}


def test_with_the_classifier_off_there_is_no_layer(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session, classifier="off")
    drive(session, started["session_id"], Oracle(info))
    jobs.drain(60)
    record = _record(started["session_id"])
    assert not any(u.get("classifier") for u in record["units"].values())
    ds = local_handles("gsynth")
    assert all(store.session_run_record(ds.name, m) is None for m in ("CD3", "CD8", "CD20"))


def test_a_propose_session_stores_its_layers_only_when_committed(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session, mode="propose")
    sid = started["session_id"]
    drive(session, sid, Oracle(info))
    jobs.drain(120)
    ds = local_handles("gsynth")
    record = _record(sid)
    assert record["units"]["gsynth::CD8"]["classifier"]["status"] == "ready"
    assert record["units"]["gsynth::CD8"]["classifier"].get("stored") is None
    assert store.session_run_record(ds.name, "CD8") is None
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "commit"}))
    jobs.drain(120)
    saved = store.session_run_record(ds.name, "CD8")
    assert saved is not None and saved["session_id"] == sid
    assert _record(sid)["units"]["gsynth::CD8"]["classifier"]["stored"] == "project"


def test_a_rollback_drops_the_sessions_layers(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    drive(session, sid, Oracle(info))
    jobs.drain(120)
    ds = local_handles("gsynth")
    assert store.session_run_record(ds.name, "CD8") is not None
    finished = ok(invoke(session, "gating_session_finish", {"session_id": sid,
                                                            "action": "rollback"}))
    assert "CD8" in finished["classifier_discarded"]
    assert all(store.session_run_record(ds.name, m) is None for m in ("CD3", "CD8", "CD20"))


class _CheckReader(Oracle):
    """The oracle, but a classifier check's pairs of rows are answered by
    `halves(k, flagged_is)` -> (word for the flagged half, word for the other)."""

    def __init__(self, info, session_id, halves):
        super().__init__(info)
        self.session_id, self.halves, self.seen, self.tasks = session_id, halves, [], []

    def answer(self, packet):
        pairs = (packet.get("evidence") or {}).get("pairs")
        if packet["kind"] == "t4_candidates":
            self.tasks.append((bool(pairs), packet.get("task")))
        if packet["kind"] != "t4_candidates" or not pairs:
            return super().answer(packet)
        unit = _record(self.session_id)["units"][f"gsynth::{packet['units'][0]['marker']}"]
        check = unit["classifier_check"]
        self.seen.append((unit["marker"], check["direction"], packet["allowed"]["intervals"]))
        words = {}
        for k, row in enumerate(check["rows"][:pairs]):
            flagged, other = self.halves(k, row["flagged_is"])
            flagged = len(row["flagged"]) if flagged == "all" else flagged
            words[f"i{k + 1}{row['flagged_is']}"] = flagged
            words[f"i{k + 1}{'b' if row['flagged_is'] == 'a' else 'a'}"] = other
        field = "counts" if packet["evidence"].get("reading") == "count" else "intervals"
        return {"kind": "t4_candidates", field: words, "confidence": "fairly_sure"}


def _force_check(monkeypatch, direction="down"):
    from plexora.plugins.gating.server.autogate import cellcalls

    def disagrees(ds, unit, result, lo, hi, side):
        return {"direction": side, "from": lo, "to": hi, "n_cells": 100, "n_flagged": 60,
                "share": 0.6, "tiles_hit": 9, "pass": side == direction}

    monkeypatch.setattr(cellcalls, "disagreement", disagrees)
    monkeypatch.setattr(cellcalls, "_contradicted", lambda unit, side, lo, hi: False)
    # Every point a row (the synthetic lattice is sparse beside its half-band;
    # the width rule has its own test).
    monkeypatch.setattr(cellcalls, "check_points", lambda ds, unit, candidate, chain: chain)


def test_a_check_moves_the_gate_past_the_rows_a_look_confirms(tmp_path, monkeypatch):
    """The flagged half reads positive and the other half does not in the
    nearest row only: the gate goes down to that row's candidate, once."""
    info = make_gating_project(tmp_path, grid=24, size=1024, markers=HARD)
    _force_check(monkeypatch, "down")
    session = AgentSession()
    started = start(session, classifier="check", check_reading="word")
    sid = started["session_id"]
    reader = _CheckReader(info, sid, lambda k, _f: ("own_stain", "unstained") if k == 0
                          else ("mixed", "mixed"))
    drive(session, sid, reader)
    record = _record(sid)
    checked = {u["marker"]: u for u in record["units"].values() if u.get("classifier_check")}
    assert checked and reader.seen
    for marker, unit in checked.items():
        check = unit["classifier_check"]
        assert unit["code_moves"].count("down:classifier_check") == 1
        assert check["asked"] and check["confirmed"] == 1
        assert check["answers"][0] == {"flagged": "own_stain", "control": "unstained",
                                       "confirmed": True, "alike": False}
        # Moved past the confirmed row, not beyond.
        if unit["state"].startswith("accepted"):
            assert unit["final"] <= check["rows"][0]["point"] + 1e-6
            assert unit["final"] < check["at"]
        # Never shown which half is which.
        assert all(r[-1] in "ab" for _m, _d, rows in reader.seen for r in rows)
    # A check is its own AI task, so its model is set apart (`tasks.yaml`).
    assert {task for is_check, task in reader.tasks if is_check} == {"gating.classifier_check"}
    assert all(task == "gating.threshold_evaluation" for is_check, task in reader.tasks
               if not is_check)


def test_both_halves_stained_is_a_row_of_positives_and_the_gate_goes_down(tmp_path, monkeypatch):
    """p193 CD3E: the flagged cells and the ones beside them all read
    `own_stain` -- the row is positive, so the gate passes it; the halves
    reading alike is kept for the report, never a reason to stay."""
    info = make_gating_project(tmp_path, grid=24, size=1024, markers=HARD)
    _force_check(monkeypatch, "down")
    session = AgentSession()
    # Every row passed, the walk goes on with plain looks; a marker that runs
    # out of rounds is flagged rather than asked about (nobody to ask here).
    started = start(session, classifier="check", check_reading="word", on_limit="stop")
    sid = started["session_id"]
    drive(session, sid, _CheckReader(info, sid, lambda k, _f: ("own_stain", "own_stain")))
    checked = [u for u in _record(sid)["units"].values() if u.get("classifier_check")]
    assert checked
    for unit in checked:
        check = unit["classifier_check"]
        assert check["confirmed"] == len(check["answers"]) >= 1
        assert all(a["alike"] for a in check["answers"])
        if unit["state"].startswith("accepted"):
            assert unit["final"] < check["at"]


def test_a_session_stored_before_the_layer_resumes_without_one(tmp_path):
    from types import SimpleNamespace

    from plexora.agent.sessions.store import SessionStore
    from plexora.plugins.gating.server.autogate.engine import Engine

    st = SessionStore("gating", root=tmp_path)
    st.create({"session_id": "s_old", "options": {}, "units": {}, "outstanding": {}})
    engine = Engine(SimpleNamespace(), "s_old", st=st)
    assert engine.options["classifier"] == "off"
    assert engine.record["classifier"] == {"schema": 0}
    st.create({"session_id": "s_new", "options": {"classifier": "layer"}, "units": {},
               "outstanding": {}, "classifier": {"schema": 1}})
    assert Engine(SimpleNamespace(), "s_new", st=st).options["classifier"] == "layer"


def test_get_gate_status_qc_and_the_report_show_the_layer(tmp_path):
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    drive(session, sid, Oracle(info))
    jobs.drain(120)
    gate = ok(invoke(session, "get_gate", {"project": "gsynth", "marker": "CD8"}))
    assert gate["source"] == "threshold"
    layer = gate["session_run"]
    assert layer["session_id"] == sid and layer["origin"] == "session"
    assert layer["stale"] is None and layer["compare"]["gate_used"][0] == gate["gate"]["low"]
    every = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert every["classifier_runs"]["CD8"]["session_id"] == sid
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["classifier"]["mode"] == "layer"
    assert status["classifier"]["layers"].get("ready", 0) >= 3
    row = next(u for u in status["units"] if u["marker"] == "CD8")
    assert row["classifier"]["status"] == "ready"
    report = ok(invoke(session, "gating_report", {"session_id": sid}))
    html = open(report["paths"]["html"], encoding="utf-8").read()
    assert "Single Cell Classifier" in html and "Classifier at gate" in html
    # The gate moved by hand: the layer says so, and does not flag the gate.
    ds = local_handles("gsynth")
    current = model.get_gate(ds, "CD8")
    model.set_gate(ds, "CD8", current["low"] * 1.2, current["high"])
    qc = ok(invoke(session, "gating_qc", {"project": "gsynth"}))
    stale = [s for s in qc["stale_classifier"] if s["marker"] == "CD8"]
    assert stale and stale[0]["reason"] == "the gate moved since this run"
    assert not stale[0]["decides"]


def test_the_check_counts_the_cells_the_classifier_calls_the_other_way(tmp_path):
    import numpy as np

    from plexora.plugins.gating.server.autogate import cellcalls
    from plexora.plugins.gating.server.autogate.cells import cells, values

    make_gating_project(tmp_path, grid=24, size=1024)
    ds = local_handles("gsynth")
    v = values(ds, "CD8")
    gate = float(np.nanquantile(v, 0.6))
    lo = float(np.nanquantile(v, 0.4))
    gate_calls = np.where(np.isfinite(v), (v > gate).astype(np.int8), -1).astype(np.int8)
    below = (v > lo) & (v <= gate)
    raw = gate_calls.copy()
    raw[below] = 1                     # it calls every cell just below the gate positive
    result = {"arrays": {"gate_calls": gate_calls, "raw_calls": raw}}
    down = cellcalls.disagreement(ds, {"marker": "CD8"}, result, lo, gate, "down")
    assert down["n_flagged"] == down["n_cells"] == int(below.sum()) and down["share"] == 1.0
    assert down["pass"]
    up = cellcalls.disagreement(ds, {"marker": "CD8"}, result, gate, float(np.nanmax(v)), "up")
    assert up["n_flagged"] == 0 and not up["pass"]
    # The look's rows: a flagged half and a half it agrees on, spread.
    raw[below & (np.arange(v.size) % 2 == 0)] = 0
    rows = cellcalls.check_rows(ds, {"marker": "CD8"}, {"arrays": {"gate_calls": gate_calls,
                                                                    "raw_calls": raw}},
                                gate, [{"low": lo}], "down")
    assert len(rows) == 1 and rows[0]["flagged"] and rows[0]["control"]
    assert set(rows[0]["flagged"]).isdisjoint(rows[0]["control"])
    assert np.all(raw[rows[0]["flagged"]] == 1) and np.all(raw[rows[0]["control"]] == 0)
    del cells


def test_read_mixed_a_half_stained_flagged_row_moves_the_gate(tmp_path, monkeypatch):
    info = make_gating_project(tmp_path, grid=24, size=1024, markers=HARD)
    _force_check(monkeypatch, "down")
    session = AgentSession()
    sid = start(session, classifier="check", check_reading="mixed")["session_id"]
    drive(session, sid, _CheckReader(info, sid, lambda k, _f: ("mixed", "mixed") if k == 0
                                     else ("unstained", "unstained")))
    checked = [u for u in _record(sid)["units"].values() if u.get("classifier_check")]
    assert checked
    for unit in checked:
        check = unit["classifier_check"]
        assert check["reading"] == "mixed" and check["confirmed"] == 1


def test_read_by_counts_a_row_moves_the_gate_when_its_flagged_cells_win(tmp_path, monkeypatch):
    info = make_gating_project(tmp_path, grid=24, size=1024, markers=HARD)
    _force_check(monkeypatch, "down")
    session = AgentSession()
    sid = start(session, classifier="check", check_reading="count")["session_id"]
    # The nearest pair: every flagged cell stained, no other; the next, none.
    reader = _CheckReader(info, sid, lambda k, _f: ("all", 0) if k == 0 else (0, 0))
    drive(session, sid, reader)
    checked = [u for u in _record(sid)["units"].values() if u.get("classifier_check")]
    assert checked and reader.seen
    for unit in checked:
        check = unit["classifier_check"]
        assert check["reading"] == "count" and check["confirmed"] == 1
        first = check["answers"][0]
        assert first["flagged"] == len(check["rows"][0]["flagged"]) and first["confirmed"]


def test_a_count_above_the_rows_cells_is_refused():
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import transitions

    check = {"rows": [{"flagged_is": "a", "flagged": [1, 2], "control": [3, 4]}]}
    transitions._check_counts(check, {"i1a": 2, "i1b": "cannot_tell"}, 1)
    with pytest.raises(AgentError):
        transitions._check_counts(check, {"i1a": 3, "i1b": 0}, 1)


def test_counts_confirm_on_the_flagged_half():
    from plexora.plugins.gating.server.autogate import cellcalls

    assert cellcalls.confirms_count("down", 4, 6, 1, 6)
    assert cellcalls.confirms_count("down", 3, 6, 1, 6)
    assert cellcalls.confirms_count("down", 3, 6, 3, 6)          # alike: still half stained
    assert not cellcalls.confirms_count("down", 2, 6, 0, 6)      # under half
    assert not cellcalls.confirms_count("down", None, 6, 0, 6)   # cannot_tell
    assert cellcalls.confirms_count("up", 1, 6, 4, 6)
    assert not cellcalls.confirms_count("up", 4, 6, 6, 6)
    assert cellcalls.confirms("down", "mixed", "mixed", "mixed")
    assert cellcalls.confirms("down", "mixed", "own_stain", "mixed")
    assert not cellcalls.confirms("up", "mixed", "own_stain", "mixed")


def test_a_check_skips_points_closer_than_a_half_band(monkeypatch):
    """p193 CD11c: the gate at 5.617 and the next candidate at 5.61 -- a row
    of a hundredth, which a confirmed look moves the gate by nothing."""
    from plexora.plugins.gating.server.autogate import cellcalls
    from plexora.plugins.gating.server.classifier import signal

    monkeypatch.setattr(signal, "halves_apart", lambda ds, m, a, b: abs(a - b) / 0.1)
    chain = [{"low": v} for v in (5.61, 5.59, 5.5, 5.45, 5.3)]
    got = cellcalls.check_points(None, {"marker": "CD11c"}, 5.617, chain)
    assert [p["low"] for p in got] == [5.5, 5.3]
    monkeypatch.setattr(signal, "halves_apart", lambda ds, m, a, b: None)
    assert cellcalls.check_points(None, {"marker": "CD11c"}, 5.617, chain) == chain


def test_a_check_reads_past_a_looks_nearest_points():
    """p193 Ecadherin: a look's four nearest points (5.16-5.22) all sat
    inside one half-band of the gate at 5.15, and the check had no row."""
    from plexora.plugins.gating.server.autogate import cellcalls, lattice

    lat = {"points": [{"id": f"p{k}", "low": 5.15 + 0.01 * k, "sources": ["grid"]}
                      for k in range(1, 30)]}
    assert len(lattice.chain(lat, 5.15, "up")) == lattice.MAX_CHAIN
    reach = cellcalls._reach(lat, 5.15, "up")
    assert len(reach) > lattice.MAX_CHAIN and reach[-1]["low"] > 5.4


def test_a_row_the_classifier_flags_whole_is_read_on_its_flagged_half():
    """p193 CD8a: every cell of the row flagged, the other half empty and
    read `cannot_tell` -- the flagged cells' own stain still counts."""
    from plexora.plugins.gating.server.autogate import cellcalls

    assert cellcalls.confirms("down", "own_stain", None)
    assert not cellcalls.confirms("down", "mixed", None)
    assert cellcalls.confirms("down", "mixed", None, "mixed")
    assert cellcalls.confirms("up", "unstained", None)
    assert cellcalls.confirms_count("down", 4, 6, None, 0)
    assert not cellcalls.confirms_count("down", 2, 6, None, 0)


def test_auto_reads_a_fibrous_marker_strictly_and_the_rest_by_mixed():
    from plexora.plugins.gating.server.autogate import cellcalls

    assert cellcalls.reading_for("auto", "fibrous") == "word"
    assert cellcalls.reading_for("auto", "cell") == "mixed"
    assert cellcalls.reading_for("auto", None) == "mixed"
    assert cellcalls.reading_for("count", "fibrous") == "count"
    # p193 aSMA's second pair: a half-stained row moves a cell marker's gate,
    # not a fibrous one's.
    assert cellcalls.confirms("down", "mixed", "mixed", cellcalls.reading_for("auto", "cell"))
    assert not cellcalls.confirms("down", "mixed", "mixed",
                                  cellcalls.reading_for("auto", "fibrous"))


def test_a_pair_confirms_on_its_flagged_half_whatever_the_other_reads():
    from plexora.plugins.gating.server.autogate import cellcalls

    for other in ("unstained", "mixed", "own_stain", "cannot_tell", None):
        assert cellcalls.confirms("down", "own_stain", other)
        assert not cellcalls.confirms("down", "neighbour_only", other)
        assert cellcalls.confirms("up", "unstained", other)
        assert cellcalls.confirms("up", "neighbour_only", other)
        assert not cellcalls.confirms("up", "own_stain", other)
    assert not cellcalls.confirms("down", "mixed", "unstained")


def test_a_row_is_walked_by_its_cells_not_its_flagged_half_alone():
    """Walking a row turns every cell in it (lsp11385, 2026-10-20: the rows
    of the real run, by their cells)."""
    from plexora.plugins.gating.server.autogate import cellcalls

    # Ki67's second row: 230 stained beside 28,217 unstained is not walked.
    assert not cellcalls.confirms("down", "own_stain", "unstained", "mixed", (230, 28217))
    assert not cellcalls.confirms_count("down", 5, 6, 0, 6, (230, 28217))
    assert cellcalls.confirms_count("down", 5, 6, 4, 6, (230, 28217))
    # SOX9: half the row stained is walked.
    assert cellcalls.confirms("down", "own_stain", "unstained", "mixed", (7452, 7523))
    # NGFR: the cells beside the flagged ones count too, by the same reading.
    assert cellcalls.confirms("down", "neighbour_only", "mixed", "mixed", (262, 5357))
    assert not cellcalls.confirms("down", "neighbour_only", "mixed", "word", (262, 5357))
    # CD20L's second row: 22 % of it stained stops the walk.
    assert not cellcalls.confirms("down", "mixed", "neighbour_only", "mixed", (320, 1142))
    # Going up: b2m and HLA-ABC.
    assert cellcalls.confirms("up", "unstained", "unstained", "mixed", (561, 22258))
    assert cellcalls.confirms("up", "unstained", "mixed", "mixed", (5389, 13306))
    assert not cellcalls.confirms("up", "mixed", "mixed", "mixed", (314, 79446))
    assert not cellcalls.confirms("down", "cannot_tell", "cannot_tell", "mixed", (0, 0))


def test_a_row_a_look_judged_overrules_the_check():
    from plexora.plugins.gating.server.autogate import cellcalls

    assert cellcalls._contradicted({"judged": [[1.2, 1.8, "unstained"]]}, "down", 1.0, 2.0)
    assert not cellcalls._contradicted({"judged": [[1.2, 1.8, "mixed"]]}, "down", 1.0, 2.0)
    assert cellcalls._contradicted({"judged": [[2.2, 2.6, "own_stain"]]}, "up", 2.0, 3.0)
    assert not cellcalls._contradicted({"judged": [[3.5, 4.0, "own_stain"]]}, "up", 2.0, 3.0)
