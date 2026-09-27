"""A read-only policy (a `read` token's): reads and renders, never writes."""

import json

import pytest

from plexora.agent import AgentSession, Policy, invoke, registry
from plexora.agent.policy import classify_scope
from tests.agent_fixtures import make_synthetic_project

READ_ONLY = Policy().narrowed_by_scope("read", principal="token:abc")


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating", "roi"])
    return AgentSession()


def test_a_write_is_refused_and_the_refusal_audited(session, tmp_path):
    result = invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900},
                    policy=READ_ONLY)
    assert result["error"]["code"] == "permission_required"
    assert result["error"]["detail"]["scope"] == "read"
    line = json.loads((tmp_path / ".agent" / "audit.jsonl").read_text().splitlines()[-1])
    assert line["status"] == "refused" and line["principal"] == "token:abc"


def test_reads_and_renders_still_work(session):
    assert invoke(session, "get_gate", {"project": "synth", "marker": "CD8"},
                  policy=READ_ONLY)["ok"]
    assert invoke(session, "render_region", {"project": "synth", "output": {"width": 64}},
                  policy=READ_ONLY)["ok"]


def test_validate_scope_lists_writes_as_not_permitted(session):
    answer = classify_scope(session, "gate CD8", project="synth", policy=READ_ONLY)
    assert answer["state"] == "can_recommend"
    assert "set_gate" in answer["not_permitted"]


def test_a_write_token_leaves_a_principal_on_the_receipt_line(session, tmp_path):
    policy = Policy().narrowed_by_scope("write", principal="token:w")
    assert invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900},
                  policy=policy)["ok"]
    line = json.loads((tmp_path / ".agent" / "audit.jsonl").read_text().splitlines()[-1])
    assert line["status"] == "ok" and line["principal"] == "token:w"


def test_undo_is_a_write(session):
    written = invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900})
    refused = invoke(session, "undo_operation",
                     {"operation_id": written["operation_id"]}, policy=READ_ONLY)
    assert refused["error"]["code"] == "permission_required"
