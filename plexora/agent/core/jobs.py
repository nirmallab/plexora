"""Asking after long work: a job's status and result, waiting on it, stopping it."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent import jobs
from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel

TAGS = ("job", "jobs", "progress", "status", "long", "background", "cancel")


class JobInput(AgentModel):
    job_id: str = Field(description="The job_id a job-running capability returned.")


class ListInput(AgentModel):
    status: Literal[jobs.STATUSES] | None = Field(None, description="Only jobs in this state.")
    limit: int = Field(20, ge=1, le=200)


class WaitInput(JobInput):
    timeout_s: float = Field(30, ge=1, le=60, description="How long to wait at most; call "
                                                        "again if it is still running.")


def _require(job_id):
    record = jobs.store().get(job_id)
    if record is None:
        raise AgentError("invalid_input", f"no job {job_id!r}",
                         detail={"hint": "job_list shows the jobs this data root has"})
    return record


def job_get(call, inp):
    return {"job": _require(inp.job_id)}


def job_list(call, inp):
    records = jobs.store().list(status=inp.status, limit=inp.limit)
    return {"jobs": [{k: v for k, v in record.items() if k != "result"} for record in records],
            "count": len(records)}


def job_wait(call, inp):
    _require(inp.job_id)
    record = jobs.store().wait(inp.job_id, inp.timeout_s)
    return {"job": record, "finished": record["status"] in jobs.TERMINAL}


def job_cancel(call, inp):
    from plexora.agent.receipts import make_receipt

    record = _require(inp.job_id)
    before, after = jobs.store().cancel(inp.job_id)
    if before in jobs.TERMINAL:
        raise AgentError("conflict", f"{inp.job_id} has already finished ({before})",
                         detail={"status": before}, retryable=False)
    if after == before:
        raise AgentError("resource_unavailable",
                         f"{inp.job_id} is {before} in another process (pid "
                         f"{record.get('pid')}), which this one cannot stop",
                         detail={"pid": record.get("pid")})
    call.project_name = record.get("project")
    receipt = make_receipt(call, changed=True, before={"status": before},
                           after={"status": after}, persistent_state="none",
                           reversible=False, extra={"job_id": inp.job_id})
    return {"receipt": receipt.model_dump(mode="json"), "job_id": inp.job_id,
            "status": after,
            "note": "the job stops at its next checkpoint; job_wait reports when it has"}


def capabilities():
    def cap(**kwargs):
        return Capability(owner="core", tags=TAGS, **kwargs)

    return [
        cap(name="job.get", tool_name="job_get",
            purpose="One job's status, progress and, once done, result.",
            permission="read", input_model=JobInput, handler=job_get),
        cap(name="job.list", tool_name="job_list",
            purpose="Recent jobs, newest first, optionally only those in one state.",
            permission="read", input_model=ListInput, handler=job_list),
        cap(name="job.wait", tool_name="job_wait",
            purpose="Wait for a job to finish (up to timeout_s), reporting its progress as it "
                    "goes; returns the job, finished or not.",
            permission="read", input_model=WaitInput, handler=job_wait,
            streams_progress=True),
        cap(name="job.cancel", tool_name="job_cancel",
            purpose="Ask a running job to stop. Work it already receipted stays done.",
            permission="reversible_write", input_model=JobInput, handler=job_cancel,
            reversible=False),
    ]
