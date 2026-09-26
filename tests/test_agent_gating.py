"""Gating through the agent pipeline: receipts, audit lines, refusals."""

import json

import pytest

from plexora.agent import AgentSession, Policy, invoke, registry
from plexora.agent.audit import AuditLog
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating", "roi"])
    return AgentSession()


def _audit(tmp_path):
    path = tmp_path / ".agent" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_set_gate_returns_a_receipt_and_logs_it(session, tmp_path):
    result = invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900})
    assert result["ok"], result
    receipt = result["result"]["receipt"]
    assert receipt["changed"] is True
    assert receipt["before"]["thresholded"] is False
    assert receipt["after"]["low"] == 900
    assert receipt["persistent_state"] == "plugin_store:gating"
    assert receipt["source_file_modified"] is False
    assert receipt["undo_hint"]["tool"] == "set_gate"
    lines = _audit(tmp_path)
    assert lines[-1]["status"] == "ok" and lines[-1]["operation_id"] == receipt["operation_id"]
    # The same gate, read back.
    got = invoke(session, "get_gate", {"project": "synth", "marker": "CD8"})["result"]
    assert got["gate"]["low"] == 900 and got["revision"] == receipt["revision_after"]


def test_a_stale_revision_is_a_conflict_and_is_logged(session, tmp_path):
    invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900})
    result = invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 950,
                                          "expected_revision": "0"})
    assert result["error"]["code"] == "conflict"
    assert _audit(tmp_path)[-1]["status"] == "conflict"


def test_writing_the_source_file_is_refused_by_default(session, tmp_path):
    result = invoke(session, "write_gates_to_source", {"project": "synth", "confirm": True})
    assert result["error"]["code"] == "permission_required"
    assert _audit(tmp_path)[-1]["status"] == "refused"


def test_writing_a_csv_projects_source_is_a_missing_precondition(session):
    policy = Policy.from_flags(allow_source_writes=True)
    result = invoke(session, "write_gates_to_source", {"project": "synth", "confirm": True},
                    policy=policy)
    assert result["error"]["code"] == "precondition_missing"


def test_confirm_must_be_literally_true(session):
    policy = Policy.from_flags(allow_source_writes=True)
    result = invoke(session, "write_gates_to_source", {"project": "synth", "confirm": False},
                    policy=policy)
    assert result["error"]["code"] == "invalid_input"


def test_adjust_moves_by_the_rule(session):
    auto = invoke(session, "suggest_auto_gate", {"project": "synth", "marker": "CD8"})["result"]
    invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": auto["auto_gate"]})
    result = invoke(session, "adjust_gate", {"project": "synth", "marker": "CD8",
                                             "direction": "up", "magnitude": "small"})["result"]
    assert result["receipt"]["after"]["low"] > auto["auto_gate"]
    assert result["delta_positive"] <= 0
    assert "moved up small" in result["reason"]


def test_a_project_without_a_table_is_told_what_it_lacks(tmp_path):
    make_synthetic_project(tmp_path, "bare", table=False)
    registry.discover(["gating"])
    result = invoke(AgentSession(), "get_gate", {"project": "bare", "marker": "CD8"})
    assert result["error"]["code"] == "precondition_missing"
    assert any(item["key"] == "table" for item in result["error"]["detail"]["missing"])
