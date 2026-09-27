"""undo_operation: replaying a receipt's undo hint, and refusing when it should."""

import json

import pytest

from plexora.agent import AgentSession, Policy, invoke, registry
from tests.agent_fixtures import make_synthetic_project

SQUARE = [[100, 100], [200, 100], [200, 200], [100, 200]]


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating", "roi"])
    return AgentSession()


def _audit(tmp_path):
    path = tmp_path / ".agent" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _ok(result):
    assert result["ok"], result
    return result["result"]


def _gate(session):
    return _ok(invoke(session, "get_gate", {"project": "synth", "marker": "CD8"}))["gate"]


def test_undoing_a_gate_restores_it_and_is_receipted(session, tmp_path):
    original = _gate(session)
    written = _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8",
                                               "low": 900}))["receipt"]
    undone = _ok(invoke(session, "undo_operation",
                        {"operation_id": written["operation_id"]}))
    assert _gate(session)["low"] == original["low"]
    receipt = undone["receipt"]
    assert receipt["capability"] == "operation.undo" and receipt["project"] == "synth"
    assert receipt["before"]["low"] == 900 and receipt["after"]["low"] == original["low"]
    assert receipt["undo_hint"]["tool"] == "gating.set"
    assert receipt["undo_hint"]["arguments"]["low"] == 900
    lines = _audit(tmp_path)
    by_op = {line["operation_id"]: line for line in lines}
    assert by_op[receipt["operation_id"]]["undo_of"] == written["operation_id"]
    replay = by_op[undone["replayed"]["operation_id"]]
    assert replay["capability"] == "gating.set" and replay["undo_of"] == written["operation_id"]


def test_the_redo_hint_puts_the_change_back(session):
    written = _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8",
                                               "low": 900}))["receipt"]
    undone = _ok(invoke(session, "undo_operation",
                        {"operation_id": written["operation_id"]}))["receipt"]
    _ok(invoke(session, "undo_operation", {"operation_id": undone["operation_id"]}))
    assert _gate(session)["low"] == 900


def test_a_gate_that_moved_on_is_not_undone(session):
    first = _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8",
                                             "low": 900}))["receipt"]
    _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 1200}))
    result = invoke(session, "undo_operation", {"operation_id": first["operation_id"]})
    assert result["error"]["code"] == "conflict"
    assert _gate(session)["low"] == 1200


def test_undoing_twice_is_a_conflict(session):
    written = _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8",
                                               "low": 900}))["receipt"]
    _ok(invoke(session, "undo_operation", {"operation_id": written["operation_id"]}))
    again = invoke(session, "undo_operation", {"operation_id": written["operation_id"]})
    assert again["error"]["code"] == "conflict"
    assert again["error"]["retryable"] is False


def test_undoing_a_drawn_region_deletes_it_without_the_destructive_flag(session):
    created = _ok(invoke(session, "create_roi", {"project": "synth", "category": "Tumour",
                                                 "points": SQUARE}))
    assert created["receipt"]["undo_hint"]["arguments"]["base_revision"] == \
        created["receipt"]["revision_after"]
    _ok(invoke(session, "undo_operation",
               {"operation_id": created["receipt"]["operation_id"]}, policy=Policy()))
    assert _ok(invoke(session, "list_rois", {"project": "synth"}))["count"] == 0


def test_a_bare_delete_still_needs_the_flag(session):
    created = _ok(invoke(session, "create_roi", {"project": "synth", "category": "Tumour",
                                                 "points": SQUARE}))
    hint = created["receipt"]["undo_hint"]
    result = invoke(session, hint["tool"], hint["arguments"], policy=Policy())
    assert result["error"]["code"] == "permission_required"


def test_an_irreversible_write_is_not_undone(session):
    created = _ok(invoke(session, "create_roi", {"project": "synth", "category": "Tumour",
                                                 "points": SQUARE}))
    deleted = _ok(invoke(session, "delete_roi",
                         {"project": "synth", "roi_id": created["roi"]["id"], "confirm": True},
                         policy=Policy(allow_destructive=True)))["receipt"]
    assert deleted["reversible"] is False
    assert deleted["undo_hint"]["arguments"]["base_revision"] == deleted["revision_after"]
    result = invoke(session, "undo_operation", {"operation_id": deleted["operation_id"]})
    assert result["error"]["code"] == "invalid_input"
    assert "not reversible" in result["error"]["message"]


def test_updating_a_region_can_be_undone_field_for_field(session):
    created = _ok(invoke(session, "create_roi", {"project": "synth", "category": "Tumour",
                                                 "points": SQUARE, "name": "a",
                                                 "notes": "first"}))
    roi_id = created["roi"]["id"]
    updated = _ok(invoke(session, "update_roi", {
        "project": "synth", "roi_id": roi_id, "name": "b", "notes": "second",
        "points": [[0, 0], [50, 0], [50, 50]]}))["receipt"]
    arguments = updated["undo_hint"]["arguments"]
    assert arguments["base_revision"] == updated["revision_after"]
    assert arguments["notes"] == "first" and arguments["geometry"] is not None
    _ok(invoke(session, "undo_operation", {"operation_id": updated["operation_id"]}))
    roi = _ok(invoke(session, "get_roi", {"project": "synth", "roi_id": roi_id}))["roi"]
    assert (roi["name"], roi["notes"]) == ("a", "first")
    assert roi["bounds"] == created["roi"]["bounds"]


def test_an_unknown_or_read_only_operation_is_invalid(session):
    assert invoke(session, "undo_operation", {"operation_id": "op_nope"})["error"]["code"] \
        == "invalid_input"
