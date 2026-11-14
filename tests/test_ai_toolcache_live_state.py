"""The tool-result cache never answers a call whose result is live state.

A conversation's reads are answered from the tool-result cache while the
project revision is unchanged (plexora/ai/harness/toolcache.py). That holds for
a read that is a function of the project's saved state. It does not hold for a
job's status, a session's status or an object review's next cell: those move
on without a receipt, so the revision stays where it was and the first answer
would come back for up to 15 minutes. What is pinned: asking about a job
again, after it failed, reports that it failed; and the session and review
reads that report (or change) live state are not cacheable.
"""

from __future__ import annotations

import json
import threading

import pytest
from pydantic import Field

from plexora.agent import jobs, registry
from plexora.agent.errors import AgentError
from plexora.agent.policy import Policy
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel
from plexora.ai.harness.conversations import ToolAdapter
from plexora.ai.harness.toolcache import ToolResultCache, cacheable

RELEASE = threading.Event()


class _Input(AgentModel):
    key: str = Field("x", description="Anything.")


def _fails_when_released(call, inp):
    RELEASE.wait(10)
    raise AgentError("resource_unavailable", "the work could not finish")


@pytest.fixture
def job_cap():
    registry.discover([])
    before = dict(registry._REGISTRY)
    RELEASE.clear()
    registry.register(Capability(owner="testlive", entitlement="free", name="testlive.compare",
                                 tool_name="tl_compare", purpose="A read that runs as a job.",
                                 permission="read", input_model=_Input,
                                 handler=_fails_when_released, execution="job"))
    yield
    RELEASE.set()
    jobs.drain(10)
    registry._REGISTRY.clear()
    registry._REGISTRY.update(before)


def _adapter(tmp_path):
    from plexora.agent import AgentSession

    return ToolAdapter(AgentSession(), policy=Policy(), cache=ToolResultCache(tmp_path / "toolcache"))


def _job_id(adapter):
    started = adapter.execute("t0", "tl_compare", {"key": "a"})
    assert not started.is_error
    return json.loads(started.content[0]["text"])["job_id"]


def _finish_with_failure(job_id):
    RELEASE.set()
    jobs.drain(10)
    assert jobs.store().get(job_id)["status"] == "failed"


def test_job_get_asked_again_after_the_job_failed_reports_failed(job_cap, tmp_path):
    adapter = _adapter(tmp_path)
    job_id = _job_id(adapter)
    first = adapter.execute("t1", "job_get", {"job_id": job_id})
    assert json.loads(first.content[0]["text"])["job"]["status"] in ("queued", "running")
    _finish_with_failure(job_id)
    second = adapter.execute("t2", "job_get", {"job_id": job_id})
    assert json.loads(second.content[0]["text"])["job"]["status"] == "failed", \
        f"job_get answered from the {second.source}: the first snapshot, not the failed job"


def test_job_wait_asked_again_after_the_job_failed_reports_it_finished(job_cap, tmp_path):
    adapter = _adapter(tmp_path)
    job_id = _job_id(adapter)
    first = adapter.execute("t1", "job_wait", {"job_id": job_id, "timeout_s": 1})
    assert json.loads(first.content[0]["text"])["finished"] is False
    _finish_with_failure(job_id)
    second = adapter.execute("t2", "job_wait", {"job_id": job_id, "timeout_s": 1})
    body = json.loads(second.content[0]["text"])
    assert body["finished"] is True and body["job"]["status"] == "failed", \
        f"job_wait answered from the {second.source}: the first 'still running' snapshot"


LIVE_STATE = ("job.get", "job.wait", "job.list", "gating.session_status", "qc.session_status",
              "qc.object_review_next", "qc.object_review_status")


def test_job_session_and_object_review_state_is_never_cached():
    before = dict(registry._REGISTRY)
    try:
        registry.discover(["gating", "qc"])
        cached = [name for name in LIVE_STATE if cacheable(registry.get(name))]
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(before)
    assert cached == [], f"live state answered from the tool-result cache: {cached}"
