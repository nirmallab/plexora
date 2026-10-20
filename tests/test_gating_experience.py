"""The experience record (`plugins/gating/server/experience.py`): what each
finished session decided and what people did afterwards, for a person to
learn from -- names, counts, gates and versions only, read by `plexora ai
bench experience`, never into a gate."""

import json

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.plugins.gating.server import experience, model
from tests.agent_fixtures import local_handles
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import Oracle, drive, ok, start

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _session(tmp_path, **options):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session, **options)
    drive(session, started["session_id"], Oracle(info))
    jobs.drain(120)
    return session, started["session_id"]


def test_a_closed_session_leaves_one_decision_row_a_marker_and_no_cell(tmp_path):
    session, sid = _session(tmp_path)
    ok(invoke(session, "gating_session_finish", {"session_id": sid}))
    rows = experience.read("gsynth")
    decisions = [r for r in rows if r["row"] == "decision"]
    assert {r["marker"] for r in decisions} == {"CD3", "CD8", "CD20"}
    for row in decisions:
        assert row["session_id"] == sid and row["action"] == "close"
        assert row["final"] is not None and row["versions"]["gating"]
        assert row["versions"]["classifier"]["rules"]
        assert row["classifier"]["status"] == "ready"
    text = json.dumps(rows)
    assert "cell_id" not in text and "positive_ids" not in text
    # Every list in a row is short: names, gates, flags -- never one per cell.
    for row in rows:
        for value in row.values():
            assert not isinstance(value, list) or len(value) < 50


def test_later_moves_and_verdicts_are_corrections(tmp_path):
    from plexora.plugins.gating.server.classifier import runs

    session, sid = _session(tmp_path)
    ok(invoke(session, "gating_session_finish", {"session_id": sid}))
    ds = local_handles("gsynth")
    gate = model.get_gate(ds, "CD8")
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD8",
                                    "low": gate["low"] * 1.1}))
    runs.adopt_session(ds, "CD3")
    runs.revert(ds, "CD3")
    runs.drop_session_run(ds, "CD20")
    corrections = [r for r in experience.read("gsynth") if r["row"] == "correction"]
    what = {(r["marker"], r["what"]) for r in corrections}
    assert ("CD8", "gate_moved") in what
    assert {("CD3", "classifier_adopted"), ("CD3", "classifier_reverted"),
            ("CD20", "classifier_discarded")} <= what
    moved = next(r for r in corrections if r["what"] == "gate_moved")
    assert moved["session_id"] == sid and moved["by"] == "agent"
    assert moved["after"][0] == pytest.approx(gate["low"] * 1.1, rel=1e-3)


def test_a_rollback_is_recorded_for_each_marker(tmp_path):
    session, sid = _session(tmp_path)
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "rollback"}))
    rows = experience.read("gsynth")
    assert {r["marker"] for r in rows if r.get("what") == "rolled_back"} == \
        {"CD3", "CD8", "CD20"}


def test_the_reader_finds_a_gate_moved_by_hand_and_changes_nothing(tmp_path):
    from plexora.ai import bench_experience

    session, sid = _session(tmp_path)
    ok(invoke(session, "gating_session_finish", {"session_id": sid}))
    ds = local_handles("gsynth")
    gate = model.get_gate(ds, "CD20")
    # The sidebar's own save: the gate changes, the session's provenance row stays.
    model.set_gate(ds, "CD20", gate["low"] * 0.8, gate["high"])
    before = model.all_gates(ds)
    summary = bench_experience.summarise(bench_experience.collect(["gsynth"]))
    moved = summary["by_marker"]["CD20"][0]
    assert moved["final"] == pytest.approx(gate["low"])
    assert moved["delta"] < 0
    assert "CD8" not in summary["by_marker"]
    assert summary["layers"]
    text = bench_experience.to_markdown(summary)
    assert "Gates the user moved" in text and "CD20" in text
    assert model.all_gates(ds) == before


def test_the_record_can_live_in_a_shared_folder(tmp_path, monkeypatch):
    shared = tmp_path / "lab"
    monkeypatch.setenv(experience.ENV_PATH, str(shared))
    experience.correction("p1", "CD3", "committed", session_id="gs_1")
    assert (shared / "p1.jsonl").is_file()
    assert experience.read("p1")[0]["what"] == "committed"
    # A line cut off by a sync is skipped, not fatal.
    with (shared / "p1.jsonl").open("a") as handle:
        handle.write('{"row": "correc')
    assert len(experience.read("p1")) == 1
    with pytest.raises(ValueError):
        experience.correction("p1", "CD3", "liked")
