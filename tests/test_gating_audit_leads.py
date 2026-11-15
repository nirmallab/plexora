"""Regression tests for the gating leads of the Nov 2026 audit
(docs/internal/AUDIT_2026-11-14.md §7), one per lead id."""

from __future__ import annotations

import json
import threading
import types

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


# -- flow-user-state-7: classifier_annotate's undo restores prior labels -----------------


def test_annotate_undo_hint_puts_back_the_labels_the_cells_had(tmp_path):
    from plexora import api
    from plexora.plugins.gating.server.classifier import store
    from tests.agent_fixtures import make_synthetic_project

    made = make_synthetic_project(tmp_path)
    session = AgentSession()
    pos, neg = made["positives"][:2], made["negatives"][:2]
    first = invoke(session, "classifier_annotate",
                   {"project": "synth", "marker": "CD8", "positive": pos, "negative": neg})
    assert first["ok"], first
    # Flip one positive to negative and remove one negative.
    second = invoke(session, "classifier_annotate",
                    {"project": "synth", "marker": "CD8", "negative": [pos[0]],
                     "remove": [neg[0]]})
    assert second["ok"], second
    hint = second["result"]["receipt"]["undo_hint"]
    assert hint["tool"] == "classifier_annotate"
    undone = invoke(session, hint["tool"], hint["arguments"])
    assert undone["ok"], undone
    ds = api.project_data("synth")
    back = store.examples(ds.name, "CD8")
    assert sorted(back[0]) == sorted(pos) and sorted(back[1]) == sorted(neg)


# -- pg-gating-rest-3: apply_gate_to_dataset records provenance --------------------------


def test_a_cohort_gate_is_recorded_as_the_agents(tmp_path):
    from plexora import api
    from plexora.plugins.gating.server import model, provenance
    from plexora.server.models import datasets as dataset_registry
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path, "a", seed=0)
    make_synthetic_project(tmp_path, "b", seed=1)
    dataset_registry.create("cohort", projects=["a", "b"])
    session = AgentSession()
    submitted = invoke(session, "apply_gate_to_dataset",
                       {"dataset": "cohort", "marker": "CD8", "low": 1500})
    assert submitted["ok"], submitted
    job = invoke(session, "job_wait", {"job_id": submitted["result"]["job_id"],
                                       "timeout_s": 30})["result"]["job"]
    assert job["status"] == "done", job
    for name in ("a", "b"):
        gate = model.get_gate(api.project_data(name), "CD8")
        row = provenance.read(name).get("CD8")
        assert row and row.get("written_low") == pytest.approx(1500), row
        assert model._gated_by(row, gate["low"], gate["high"]) == model.GATED_BY_AI


# -- pg-gating-rest-5: the sidebar save's locked-row restore runs under the lock ---------


def test_the_locked_row_restore_runs_under_the_gates_lock(tmp_path, monkeypatch):
    import plexora
    from plexora.plugins.gating.server import model, provenance
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path)
    held = []
    real = provenance.merge_locked

    def probe(name, rows):
        free = []
        lock = model._lock_for(name)

        def other():
            got = lock.acquire(blocking=False)
            free.append(got)
            if got:
                lock.release()

        t = threading.Thread(target=other)
        t.start()
        t.join()
        held.append(not free[0])
        return real(name, rows)

    monkeypatch.setattr(provenance, "merge_locked", probe)
    from plexora import api

    ds = api.project_data("gsynth")
    channels = {ch: [lo, hi] for ch, (lo, hi) in model.default_ranges(ds).items()}
    client = plexora.app.test_client()
    saved = client.post("/plugins/gating/save_gating_list",
                        json={"datasource": "gsynth", "filter": {}, "channels": channels})
    assert saved.status_code == 200
    assert held == [True]


# -- pg-gating-rest-6: the classifier's state write waits out a Windows refusal ----------


def test_the_state_write_retries_past_a_sharing_violation(tmp_path, monkeypatch):
    from plexora.plugins.gating.server.classifier import store

    target = tmp_path / "state.json"
    real = store.os.replace
    refused = []

    def flaky(src, dst):
        if len(refused) < 3:
            refused.append(dst)
            raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(store.os, "replace", flaky)
    store.write_atomic(target, b"{}")
    assert target.read_bytes() == b"{}" and len(refused) == 3


# -- pg-gating-rest-4: the panel's Save Gates to AnnData rewrites the not-called cells ---


def test_the_panels_source_extras_carry_the_not_called_cells(tmp_path):
    from plexora import api
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import report
    from tests.autogate_fixtures import make_gating_project

    make_gating_project(tmp_path)
    ds = api.project_data("gsynth")
    full = model.get_gate(ds, "CD8")
    model.set_gate(ds, "CD8", (full["default_low"] + full["default_high"]) / 2,
                   full["default_high"])
    extras = report.source_extras(ds)
    # Every gated marker has its (here empty) list, so a column an earlier
    # write left is replaced, and the row count the write checks against.
    assert extras["not_called"] == {"CD8": []}
    assert extras["n_rows"] == int(ds.table.geometry().height)


# -- pg-autogate-a-4: rebuilding the panel context keeps its settled review --------------


def _fake_ds(markers):
    image = types.SimpleNamespace(real_channels=[{"name": m} for m in markers])
    return types.SimpleNamespace(project=types.SimpleNamespace(image=image, has_table=True),
                                 table=types.SimpleNamespace(markers=list(markers)))


def test_a_panel_context_rebuilt_for_another_order_keeps_the_review(tmp_path, monkeypatch):
    from plexora import paths
    from plexora.plugins.gating.server.autogate import context

    monkeypatch.setattr(paths, "agent_root", lambda: tmp_path / ".agent")
    first = context.for_project(_fake_ds(["CD3", "CD8", "CD20"]))
    first["reviewed"] = {"unspecified": {"done": True, "version": context.REVIEW_VERSION,
                                         "markers": ["CD3", "CD8"]}}
    first["proposed"] = {"unspecified": {"CD8": [{"marker": "CD3", "relation": "subset"}]}}
    context.save(first)
    again = context.for_project(_fake_ds(["CD8", "CD3", "CD20"]))
    assert again["reviewed"] == first["reviewed"]
    assert again["proposed"] == first["proposed"]
    stored = context.load(first["panel_hash"])
    assert stored["reviewed"] == first["reviewed"]


# -- the session leads ---------------------------------------------------------------------


def _session_file(session_id):
    from plexora import paths

    return paths.data_root() / ".agent" / "sessions" / "gating" / session_id / "session.json"


def _session(session_id):
    return json.loads(_session_file(session_id).read_text())


def _write_session(session_id, record):
    _session_file(session_id).write_text(json.dumps(record))


# -- pg-autogate-a-5: one marker's failure closes that marker, not the pass --------------


def test_a_marker_that_fails_to_profile_is_closed_and_the_pass_finishes(tmp_path, monkeypatch):
    from plexora.plugins.gating.server.autogate import bulk
    from tests.autogate_fixtures import make_gating_project
    from tests.test_gating_session import start

    make_gating_project(tmp_path)
    real = bulk.prepare_unit

    def broken(call, session_id, project, marker, panel, options):
        if marker == "CD8":
            raise ValueError("a column the profile cannot read")
        return real(call, session_id, project, marker, panel, options)

    monkeypatch.setattr(bulk, "prepare_unit", broken)
    session = AgentSession()
    started = start(session)
    record = _session(started["session_id"])
    assert record["state"] != "bulk_running", record["state"]
    unit = record["units"]["gsynth::CD8"]
    assert unit["state"] == "manual_review_recommended"
    assert "could not be profiled" in json.dumps(unit)


# -- pg-autogate-a-3: a look refused for its `within` leaves the unit as it was ----------


def test_a_look_refused_for_its_within_keeps_nothing_of_the_answer(tmp_path):
    from tests.autogate_fixtures import make_gating_project
    from tests.test_gating_session import HARD, Oracle, ok, start

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session)["session_id"]
    oracle = Oracle(info)
    result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
    for _ in range(30):
        assert result["state"] == "decision", result
        packet = result["packet"]
        if packet["kind"] in ("t2_confirm", "t3_biological"):
            break
        result = ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": oracle.answer(packet), "include_next": True}))["next"]
    else:
        raise AssertionError("no look was issued")
    key = f"{packet['units'][0]['project']}::{packet['units'][0]['marker']}"
    before = _session(sid)["units"][key]
    answer = {**oracle.answer(packet), "within": "NOT_A_PARTNER", "notes": "refused note"}
    refused = invoke(session, "gating_answer", {"session_id": sid,
                                                "packet_id": packet["packet_id"],
                                                "answer": answer})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input", refused
    after = _session(sid)["units"][key]
    for field in ("notes", "judged_rows", "last_answer", "ai_confidence", "own_seen",
                  "path", "look"):
        assert after.get(field) == before.get(field), field


# -- pg-autogate-b-1: a layer re-queued while the job runs is made, at its new gate ------


def test_a_layer_requeued_while_the_job_ran_is_made_at_the_new_gate(tmp_path, monkeypatch):
    from plexora.plugins.gating.server.classifier import runs, store
    from tests.autogate_fixtures import make_gating_project
    from tests.test_gating_session import Oracle, drive, start

    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    sid = start(session)["session_id"]
    drive(session, sid, Oracle(info))
    jobs.drain(120)
    key = "gsynth::CD8"
    record = _session(sid)
    layer = record["units"][key]["classifier"]
    assert layer["status"] == "ready", layer
    low1 = float(layer["gate_used"][0])
    low2 = low1 * 1.15
    record["units"][key]["classifier"] = {"status": "pending", "gate_used": [low1, None],
                                          "gate_source": "session", "store": True}
    record.setdefault("classifier", {})["pending"] = [key]
    record["classifier"].pop("job_id", None)
    _write_session(sid, record)
    real = runs.session_run
    calls = []

    def moved_meanwhile(ds, marker, **kw):
        calls.append(kw["low"])
        if len(calls) == 1:
            # The gate moves (and its layer is queued again) while this runs.
            now = _session(sid)
            now["units"][key]["classifier"] = {"status": "pending", "gate_used": [low2, None],
                                               "gate_source": "session", "store": True}
            _write_session(sid, now)
        return real(ds, marker, **kw)

    monkeypatch.setattr(runs, "session_run", moved_meanwhile)
    submitted = invoke(session, "gating_session_classifier", {"session_id": sid})
    assert submitted["ok"], submitted
    jobs.drain(120)
    assert calls[:2] == [low1, low2]
    final = _session(sid)["units"][key]["classifier"]
    assert final["status"] == "ready" and final["gate_used"][0] == pytest.approx(low2), final
    saved = store.session_run_record("gsynth", "CD8")
    assert saved["gate_used"][0] == pytest.approx(low2)


# -- pg-autogate-a-2: a lane's context sheets are neither trimmed nor counted as shown ---


def test_a_lanes_sheets_are_not_counted_as_the_default_readers(tmp_path):
    from plexora.agent.sessions.engine import DEFAULT_READER
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.autogate_fixtures import make_gating_project
    from tests.test_gating_session import HARD, Oracle, ok, start

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session)["session_id"]
    oracle = Oracle(info)
    lane = "lane-b"
    assert lane != DEFAULT_READER
    result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20,
                                                 "reader": lane}))
    issued = []
    for _ in range(60):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        issued.append(packet["packet_id"])
        result = ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"], "reader": lane,
            "answer": oracle.answer(packet), "include_next": True}))["next"]
    assert issued
    record = _session(sid)
    seen = (record.get("briefed") or {}).get("seen") or {}
    assert not [k for k in seen if k.startswith("sheet.")], seen
    st = engines.store()
    for packet_id in issued:
        stored, _images = st.read_packet(sid, packet_id)
        assert not stored.get("_sheet_left_out"), packet_id
