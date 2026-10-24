"""The MCP wrapper for a capability that streams progress.

`job_wait` is called in short slices, each with its own `timeout_s`. A
capability that STARTS a job (`execution="job"`, `export_video`) must be called
once, never sliced: its input has no `timeout_s` (the call was refused), and
were it accepted, every slice would start another export.
"""

import json

import anyio
import pytest

pytest.importorskip("mcp")

from plexora.agent import registry  # noqa: E402
from plexora.mcp import tools  # noqa: E402


class FakeContext:
    def __init__(self):
        self.reported = []

    async def report_progress(self, done, total=None, message=None):
        self.reported.append((done, total, message))


class FakeRuntime:
    """Validates every call against the input model of the capability named,
    as the real registry does, and answers a job and its waits."""

    origin = None

    def __init__(self, waits_before_done=1):
        self.calls = []
        self.waits_before_done = waits_before_done

    def request_policy(self):
        return None

    def invoke(self, name, arguments, *, policy=None):
        registry.get(name).input_model.model_validate(arguments)
        self.calls.append((name, dict(arguments)))
        if name != "job.wait":
            return {"ok": True, "operation_id": "op_1",
                    "result": {"job_id": "job_1", "status": "queued"}}
        waits = sum(1 for called, _ in self.calls if called == "job.wait")
        finished = waits > self.waits_before_done
        return {"ok": True, "operation_id": "op_2",
                "result": {"finished": finished,
                           "job": {"job_id": "job_1",
                                   "status": "succeeded" if finished else "running",
                                   "progress": {"done": waits, "total": 3,
                                                "message": "rendering"}}}}


@pytest.fixture
def discovered():
    registry._reset_for_tests()
    registry.discover()
    yield
    registry._reset_for_tests()


def _run(capability, runtime, **arguments):
    fn = tools.tool_from_capability(capability, runtime)
    ctx = FakeContext()

    async def go():
        return await fn(ctx=ctx, **arguments)

    return anyio.run(go), ctx


def test_a_job_capability_is_started_once_then_followed_with_job_wait(discovered):
    export = registry.get("video.export")
    assert export.execution == "job" and export.streams_progress
    runtime = FakeRuntime(waits_before_done=2)
    answer, ctx = _run(export, runtime, video_id="vid_0123456789ab", output={},
                       view_id=None)
    names = [name for name, _ in runtime.calls]
    assert names == ["video.export", "job.wait", "job.wait", "job.wait"]
    assert "timeout_s" not in runtime.calls[0][1]
    assert all(args["job_id"] == "job_1" and args["timeout_s"] <= tools.PROGRESS_SLICE_S
               for name, args in runtime.calls[1:])
    assert [done for done, _, _ in ctx.reported] == [1, 2, 3]
    assert json.loads(answer)["finished"] is True


def test_no_streaming_tool_is_sent_an_argument_its_input_lacks(discovered):
    """Every capability with streams_progress, through the wrapper, against
    its own input model (which forbids extra fields)."""
    streaming = [cap for cap in registry.all_capabilities() if cap.streams_progress]
    assert {cap.name for cap in streaming} >= {"job.wait", "video.export"}
    for capability in streaming:
        runtime = FakeRuntime(waits_before_done=0)
        required = {name: ("job_1" if name == "job_id" else "vid_0123456789ab")
                    for name, info in capability.input_model.model_fields.items()
                    if info.is_required()}
        _run(capability, runtime, **required)
        starts = [args for name, args in runtime.calls if name == capability.name]
        if capability.execution == "job":
            assert len(starts) == 1, capability.name
