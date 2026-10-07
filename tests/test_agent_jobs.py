"""Jobs: long work returns a job id at once, reports progress, persists, stops."""

import json
import os
import time

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.server import plugins as plugin_registry
from plexora.server.models.project import LayerSpec, Project
from tests.agent_fixtures import make_synthetic_project


class _EntryPoint:
    name = "future_modality"

    def load(self):
        from tests.fixtures.plugins.future_modality import PLUGIN

        return PLUGIN


@pytest.fixture
def session(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_registry, "_entry_points_by_name",
                        lambda: {"future_modality": _EntryPoint()})
    make_synthetic_project(tmp_path, "holo")
    record = Project.load("holo").with_layer(LayerSpec(
        id="holo_1", kind="image", label="Hologram", modality="holography"))
    config = json.loads((tmp_path / "config.json").read_text())
    config["holo"] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))
    registry.discover(["future_modality"])
    return AgentSession()


def _audit(tmp_path):
    path = tmp_path / ".agent" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _submit(session, **arguments):
    result = invoke(session, "holography.reconstruct", {"project": "holo", **arguments})
    assert result["ok"], result
    return result["result"]


def _wait(session, job_id, timeout_s=30):
    result = invoke(session, "job_wait", {"job_id": job_id, "timeout_s": timeout_s})
    assert result["ok"], result
    return result["result"]["job"]


def test_a_job_returns_its_id_at_once(session):
    started = time.monotonic()
    submitted = _submit(session, steps=20, step_s=0.05)
    assert time.monotonic() - started < 0.5
    assert submitted["job_id"].startswith("job_") and submitted["status"] == "queued"
    assert submitted["resource"] == f"plexora://job/{submitted['job_id']}"
    job = _wait(session, submitted["job_id"])
    assert job["status"] == "done"


def test_progress_and_result_are_persisted(session, tmp_path):
    submitted = _submit(session, steps=4)
    job = _wait(session, submitted["job_id"])
    assert job["progress"] == {"done": 4, "total": 4, "message": "done"}
    assert job["result"]["steps"] == 4 and job["result"]["receipt"]["changed"] is True
    on_disk = json.loads((tmp_path / ".agent" / "jobs" / f"{job['job_id']}.json").read_text())
    assert on_disk["status"] == "done" and on_disk["result"]["steps"] == 4
    assert not any(key.startswith("_") for key in on_disk)
    lines = _audit(tmp_path)
    assert [line["status"] for line in lines] == ["started", "ok"]
    assert lines[0]["job_id"] == job["job_id"]
    assert lines[1]["operation_id"] == submitted["operation_id"]


def test_cancelling_stops_the_job_and_is_audited(session, tmp_path):
    submitted = _submit(session, steps=500, step_s=0.01)
    cancelled = invoke(session, "job_cancel", {"job_id": submitted["job_id"]})
    assert cancelled["ok"], cancelled
    assert cancelled["result"]["receipt"]["capability"] == "job.cancel"
    job = _wait(session, submitted["job_id"])
    assert job["status"] == "cancelled"
    assert job["progress"]["done"] < 500
    statuses = [(line["capability"], line["status"]) for line in _audit(tmp_path)]
    assert ("holography.reconstruct", "cancelled") in statuses
    assert ("job.cancel", "ok") in statuses
    again = invoke(session, "job_cancel", {"job_id": submitted["job_id"]})
    assert again["error"]["code"] == "conflict"


def test_a_failed_job_leaves_a_failed_line(session, tmp_path):
    submitted = _submit(session, steps=5, fail_at=2)
    job = _wait(session, submitted["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "internal_error"
    assert _audit(tmp_path)[-1]["status"] == "failed"
    assert _audit(tmp_path)[-1]["job_id"] == job["job_id"]


def test_a_restarted_store_marks_a_dead_process_job_interrupted(session, tmp_path):
    folder = tmp_path / ".agent" / "jobs"
    folder.mkdir(parents=True)
    (folder / "job_deadbeef0000.json").write_text(json.dumps({
        "job_id": "job_deadbeef0000", "status": "running", "pid": os.getpid() + 100000,
        "submitted_at": "2026-01-01T00:00:00.000000Z", "progress": {"done": 3}}))
    jobs._reset_for_tests()
    got = invoke(session, "job_get", {"job_id": "job_deadbeef0000"})["result"]["job"]
    assert got["status"] == "interrupted" and got["error"]["retryable"] is True
    listed = invoke(session, "job_list", {})["result"]["jobs"]
    assert listed[0]["job_id"] == "job_deadbeef0000"


def test_an_unknown_job_is_invalid_input(session):
    assert invoke(session, "job_get", {"job_id": "job_nope"})["error"]["code"] == \
        "invalid_input"


def test_permissions_and_requirements_are_checked_before_the_job_starts(session, tmp_path):
    from plexora.agent import Policy

    make_synthetic_project(tmp_path, "plain")
    refused = invoke(session, "holography.reconstruct", {"project": "plain"})
    assert refused["error"]["code"] == "unsupported_modality"
    assert jobs.store().list() == []
    # A job-running write is still a write.
    from dataclasses import replace

    read_only = replace(Policy(), egress=frozenset())
    refused = invoke(session, "holography.reconstruct", {"project": "holo"}, policy=read_only)
    assert refused["error"]["code"] == "permission_required"


def test_threads_are_drained(session):
    submitted = _submit(session, steps=10, step_s=0.01)
    assert jobs.drain(10) == []
    assert jobs.store().get(submitted["job_id"])["status"] == "done"


def test_job_wait_streams_progress_over_mcp(session):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client

    from plexora.mcp.server import build_server

    server = build_server(session, names=["future_modality"])
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            started = json.loads((await client.call_tool(
                "holography_reconstruct",
                {"project": "holo", "steps": 30, "step_s": 0.1})).content[-1].text)
            waited = await client.call_tool("job_wait", {"job_id": started["job_id"],
                                                         "timeout_s": 30},
                                            progress_callback=on_progress)
            resource = await client.read_resource(f"plexora://job/{started['job_id']}")
            return tools, json.loads(waited.content[-1].text), resource

    tools, waited, resource = anyio.run(go)
    assert "ctx" not in tools["job_wait"].input_schema["properties"]
    assert set(tools["job_wait"].input_schema["properties"]) == {"job_id", "timeout_s"}
    assert waited["finished"] is True and waited["job"]["status"] == "done"
    assert len(seen) >= 2, seen
    assert seen[-1][1] == 30
    assert json.loads(resource.contents[0].text)["job"]["status"] == "done"


def test_a_finished_job_reports_done_equal_to_total(session, monkeypatch):
    """A handler's step count is an estimate: a job that ends early of it
    (the Artifact Detector's boxes, say) still reads all the way through."""
    original = jobs.JobStore.progress

    def overestimated(self, record, done=None, total=None, message=None):
        return original(self, record, done=done, total=None if total is None else 2 * total,
                        message=message)

    monkeypatch.setattr(jobs.JobStore, "progress", overestimated)
    submitted = _submit(session, steps=4)
    job = _wait(session, submitted["job_id"])
    assert job["status"] == "done"
    assert job["progress"] == {"done": 8, "total": 8, "message": "done"}
