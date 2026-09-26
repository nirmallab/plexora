"""apply_gate_to_dataset: one gate across a cohort, as a job, receipted per image."""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def session(tmp_path):
    from plexora.server.models import datasets as dataset_registry

    make_synthetic_project(tmp_path, "a", seed=0)
    make_synthetic_project(tmp_path, "b", seed=1)
    make_synthetic_project(tmp_path, "empty", table=False)
    dataset_registry.create("cohort", projects=["a", "b", "empty"])
    registry.discover(["gating"])
    return AgentSession()


def _audit(tmp_path):
    path = tmp_path / ".agent" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _run(session, notify=None, **arguments):
    submitted = invoke(session, "apply_gate_to_dataset",
                       {"dataset": "cohort", "marker": "CD8", "low": 1500, **arguments},
                       notify=notify)
    assert submitted["ok"], submitted
    job = invoke(session, "job_wait", {"job_id": submitted["result"]["job_id"],
                                       "timeout_s": 30})["result"]["job"]
    assert job["status"] == "done", job
    return submitted, job["result"]


def test_every_image_gets_the_gate_and_its_own_receipt(session, tmp_path):
    told = []
    submitted, result = _run(session, notify=lambda *args: told.append(args) or True)
    assert [row["project"] for row in result["projects"]] == ["a", "b"]
    assert result["skipped"] == [{"project": "empty", "reason": "no cell table to gate"}]
    assert result["experimental_unit"] == "image" and result["n_images"] == 2
    for row in result["projects"]:
        gate = invoke(session, "get_gate", {"project": row["project"], "marker": "CD8"})
        assert gate["result"]["gate"]["low"] == 1500
        assert 0 < row["n_positive"] < row["n_cells"]
    op = submitted["operation_id"]
    lines = [line for line in _audit(tmp_path) if line["status"] == "ok"]
    children = [line for line in lines if line.get("parent_operation_id") == op]
    assert [line["project"] for line in children] == ["a", "b"]
    assert [line["operation_id"] for line in children] == [f"{op}.001", f"{op}.002"]
    parent = next(line for line in lines if line["operation_id"] == op)
    assert parent["receipt"]["after"]["projects"] == [f"{op}.001", f"{op}.002"]
    # Each image's viewer was told; the cohort line told nobody.
    assert sorted(args[0] for args in told) == ["a", "b"]
    spread = result["fraction_positive_per_image"]
    assert spread["min"] <= spread["median"] <= spread["max"]


def test_each_image_is_undone_on_its_own(session):
    submitted, result = _run(session)
    first = result["projects"][0]
    undone = invoke(session, "undo_operation", {"operation_id": first["operation_id"]})
    assert undone["ok"], undone
    a = invoke(session, "get_gate", {"project": "a", "marker": "CD8"})["result"]["gate"]
    b = invoke(session, "get_gate", {"project": "b", "marker": "CD8"})["result"]["gate"]
    assert a["low"] != 1500 and b["low"] == 1500


def test_an_unknown_dataset_fails_the_job_with_a_reason(session, tmp_path):
    submitted = invoke(session, "apply_gate_to_dataset",
                       {"dataset": "nope", "marker": "CD8", "low": 1})
    job = invoke(session, "job_wait", {"job_id": submitted["result"]["job_id"],
                                       "timeout_s": 30})["result"]["job"]
    assert job["status"] == "failed" and job["error"]["code"] == "invalid_input"
    assert _audit(tmp_path)[-1]["status"] == "failed"
