"""session_report and `plexora ai audit`: what the agent did, from the audit log."""

import json
from pathlib import Path

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.agent.audit import AuditLog
from plexora.agent import report as reports
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating", "roi"])
    return AgentSession()


def _ok(result):
    assert result["ok"], result
    return result["result"]


def _session_of_work(session):
    rendered = _ok(invoke(session, "render_region", {"project": "synth",
                                                     "output": {"width": 64}}))
    first = _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8",
                                             "low": 900}))["receipt"]
    second = _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8",
                                              "low": 1200}))["receipt"]
    undone = _ok(invoke(session, "undo_operation",
                        {"operation_id": second["operation_id"]}))["receipt"]
    return rendered, first, second, undone


def test_every_operation_with_its_before_and_after_numbers(session):
    rendered, first, second, undone = _session_of_work(session)
    built = reports.build(AuditLog())
    ops = {entry["operation_id"]: entry for entry in built["operations"]}
    change = next(c for c in ops[first["operation_id"]]["changes"] if c["field"] == "low")
    assert change["after"] == 900
    assert ops[second["operation_id"]]["undone_by"] == undone["operation_id"]
    assert ops[undone["operation_id"]]["undo_of"] == second["operation_id"]
    # The render came before the first write, for the same project: nearby.
    art = rendered["artifact"]["id"]
    assert art in ops[first["operation_id"]]["evidence"]["nearby"]
    assert art not in ops[second["operation_id"]]["evidence"]["nearby"]


def test_markdown_links_artifacts_relatively(session, tmp_path):
    rendered, *_ = _session_of_work(session)
    result = _ok(invoke(session, "session_report", {}))
    path = Path(result["path"])
    assert path.parent == tmp_path / ".agent" / "reports"
    text = path.read_text()
    art = rendered["artifact"]["id"]
    assert f"](../artifacts/synth/{art}.png)" in text
    assert result["text"] == text and result["operations"] >= 4


def test_html_embeds_the_pictures(session):
    _session_of_work(session)
    result = _ok(invoke(session, "session_report", {"format": "html"}))
    text = Path(result["path"]).read_text()
    assert "data:image/png;base64," in text and "text" not in result


def test_filters_by_operation_and_time(session):
    rendered, first, second, undone = _session_of_work(session)
    only = reports.build(AuditLog(), operation_ids=[second["operation_id"]])
    names = [entry["operation_id"] for entry in only["operations"]]
    assert first["operation_id"] not in names
    assert second["operation_id"] in names and undone["operation_id"] in names
    later = reports.build(AuditLog(), since="2999-01-01")
    assert later["operations"] == []


def test_explicit_artifacts_are_cited(session):
    rendered, first, *_ = _session_of_work(session)
    art = rendered["artifact"]["id"]
    built = reports.build(AuditLog(), artifact_ids=[art])
    cited = [entry for entry in built["operations"] if art in entry["evidence"]["cited"]]
    assert cited and all(art not in entry["evidence"]["nearby"] for entry in cited)


def test_the_audit_command_prints_and_writes(session, tmp_path):
    from plexora.ai.audit import audit_command

    _session_of_work(session)
    printed = []
    assert audit_command(out=printed.append) == 0
    assert "gating.set" in "\n".join(printed) and "undo of" in "\n".join(printed)
    raw = []
    audit_command(as_json=True, limit=2, out=raw.append)
    assert len(raw) == 2 and json.loads(raw[-1])["capability"] == "operation.undo"
    target = tmp_path / "out" / "session.html"
    audit_command(report=str(target), out=printed.append)
    assert target.read_text().startswith("<!doctype html>")


def test_the_cli_parses_audit():
    from plexora import cli

    args = cli.build_parser("ai").parse_args(["audit", "--since", "2026-09-01",
                                              "--project", "synth", "--report", "r.md"])
    assert (args.ai_command, args.since, args.audit_project, args.report) == \
        ("audit", "2026-09-01", "synth", "r.md")
