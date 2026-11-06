"""The website's job runner (plexora/browser/cooperative.py), under CPython.

What the browser's worker does between requests, done here by calling `step`:
a job queued by a call runs after it, its record reaches the page as it is
written, and a cancel the page flags while the job holds the worker stops it
at the job's next check.
"""

import json

import pytest

from plexora import _work
from plexora.agent import AgentSession, invoke, registry
from plexora.browser import cooperative
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
    monkeypatch.setenv("PLEXORA_WORK", "threads")  # restored after; install() changes it
    monkeypatch.setattr(plugin_registry, "_entry_points_by_name",
                        lambda: {"future_modality": _EntryPoint()})
    make_synthetic_project(tmp_path, "holo")
    record = Project.load("holo").with_layer(LayerSpec(
        id="holo_1", kind="image", label="Hologram", modality="holography"))
    config = json.loads((tmp_path / "config.json").read_text())
    config["holo"] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))
    registry.discover(["future_modality"])
    yield AgentSession()
    _work._reset_for_tests()


def test_a_job_runs_after_the_call_that_queued_it(session):
    posted = []
    cooperative.install(post=posted.append)
    started = invoke(session, "holography.reconstruct", {"project": "holo", "steps": 3, "step_s": 0})
    assert started["ok"], started
    job_id = started["result"]["job_id"]
    assert _work.pending() == 1  # answered first; nothing ran inside the call
    stepped = cooperative.step(budget_ms=10_000)
    assert stepped["ran"] == 1 and stepped["pending"] == 0
    assert stepped["names"] == [f"agent-job-{job_id}"]
    mine = [message["job"] for message in posted if message["job"]["job_id"] == job_id]
    assert mine[0]["status"] == "queued" and mine[-1]["status"] == "done"
    assert all(message["kind"] == "job" for message in posted)
    assert not any(key.startswith("_") for record in mine for key in record)


def test_a_cancel_flagged_by_the_page_stops_the_job_at_its_next_check(session):
    flagged = set()
    cooperative.install(cancelled=lambda job_id: job_id in flagged)
    started = invoke(session, "holography.reconstruct",
                     {"project": "holo", "steps": 50, "step_s": 0})
    job_id = started["result"]["job_id"]
    flagged.add(job_id)  # what the page does while the worker is busy
    cooperative.step(budget_ms=10_000)
    job = invoke(session, "job_get", {"job_id": job_id})["result"]["job"]
    assert job["status"] == "cancelled"
    assert job["progress"]["done"] < 50


def test_the_budget_leaves_the_rest_for_later(session):
    cooperative.install()
    for _ in range(3):
        invoke(session, "holography.reconstruct", {"project": "holo", "steps": 2, "step_s": 0.02})
    first = cooperative.step(budget_ms=1)
    assert first["ran"] == 1 and first["pending"] == 2
    rest = cooperative.step(budget_ms=10_000)
    assert rest["ran"] == 2 and rest["pending"] == 0


def test_a_poll_waiting_on_the_user_sleeps_through_the_page(session):
    slept = []
    cooperative.install(idle=slept.append)
    _work.wait(0.5)
    assert slept == [0.5]
