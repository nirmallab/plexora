"""What crosses to another application: origins and error shapes, nothing more.

The shared protocol (spatialbridge, an optional extra) has its own short list
of error codes, because the caller on the other side -- SCIMAP Pro, a plugin
button, an agent holding only SCIMAP Pro's server -- has to decide what to do
next without knowing Plexora's longer list. Every failure that leaves Plexora
on the bridge path is translated here, once, into that list:

    {"code", "message", "hint", "detail", "provider", "retryable"}

A superset of Plexora's own `Problem` (`code, message, detail, retryable`), so
a reader that only knows Plexora's shape still reads it. Plexora's own code
travels in `detail.plexora_code`, so nothing is lost in the translation.

Pure Python and import-light on purpose: the HTTP capability route and the MCP
adapter use it on every refused call, and neither may pay for importing the
protocol library to say "no".
"""

from __future__ import annotations

import hmac
import os

PROVIDER = "plexora"

#: The protocol's codes (`spatialbridge.errors.CODES`), spelled here so this
#: module needs no import of it. A test checks the two lists agree.
BRIDGE_CODES = ("peer_unavailable", "capability_unavailable", "workspace_mismatch",
                "stale_revision", "conflict", "lease_held", "license_required",
                "invalid_input", "precondition_missing", "job_failed", "protocol_mismatch",
                "transport_error")

#: Plexora's code -> the protocol's, as the contract fixes it. Anything not
#: named is `job_failed`: it ran, or tried to, and failed.
TO_BRIDGE = {
    "license_required": "license_required",
    # A write against an old revision is the protocol's stale case, not its
    # `conflict` -- which means two applications changed the same key.
    "conflict": "stale_revision",
    "precondition_missing": "precondition_missing",
    "unsupported_modality": "precondition_missing",
    "invalid_input": "invalid_input",
    "unknown_capability": "capability_unavailable",
    "capability_unavailable": "capability_unavailable",
}

#: The protocol's code -> the nearest of Plexora's, for a `BridgeError` raised
#: inside a capability (the bridge tools call the protocol library). The
#: original problem rides along in `detail.bridge_error` and wins on the way
#: back out, so the round trip is exact.
FROM_BRIDGE = {
    "peer_unavailable": "resource_unavailable",
    "transport_error": "resource_unavailable",
    "lease_held": "resource_unavailable",
    "capability_unavailable": "capability_unavailable",
    "protocol_mismatch": "capability_unavailable",
    "workspace_mismatch": "conflict",
    "stale_revision": "conflict",
    "conflict": "conflict",
    "license_required": "license_required",
    "invalid_input": "invalid_input",
    "precondition_missing": "precondition_missing",
    "job_failed": "internal_error",
}

#: HTTP status for a refused call on `/agent/v1/capabilities/<tool>`, by
#: Plexora's code. The contract allows 400, 403, 404 and 409.
HTTP_STATUS = {
    "permission_required": 403, "license_required": 403,
    "unknown_capability": 404, "unknown_project": 404, "capability_unavailable": 404,
    "conflict": 409, "precondition_missing": 409, "unsupported_modality": 409,
}

#: The environment variable a spawning peer sets to the nonce it also passes as
#: `--bridge-nonce` (spatialbridge `transports/mcp.py`).
NONCE_ENV = "SPATIALBRIDGE_NONCE"


def nonce_matches(nonce: str | None, environ=None) -> bool:
    """Whether `--bridge-nonce` equals `$SPATIALBRIDGE_NONCE`, both non-empty.

    What it proves is narrow, and it is meant to be: that whoever started this
    process set both on purpose -- the bridge client does, a client config that
    copied `--origin bridge` from somewhere does not. It is a statement of
    intent fixed at launch, not an authentication of the caller.
    """
    expected = (environ if environ is not None else os.environ).get(NONCE_ENV) or ""
    nonce = nonce or ""
    if not nonce or not expected:
        return False
    return hmac.compare_digest(nonce.encode("utf-8"), expected.encode("utf-8"))


def mcp_origin(requested: str | None, nonce: str | None, environ=None) -> str:
    """The origin a `plexora mcp serve` process gives its calls.

    `bridge` only when it was asked for AND the nonce matches; anything else
    -- no flag, a wrong or missing nonce -- is an outside agent, `mcp`.
    """
    from plexora.agent.registry import ORIGIN_BRIDGE, ORIGIN_MCP

    if requested == ORIGIN_BRIDGE and nonce_matches(nonce, environ):
        return ORIGIN_BRIDGE
    return ORIGIN_MCP


def problem(error: dict | None, *, provider: str = PROVIDER) -> dict:
    """Plexora's `Problem` dict as the protocol's error object."""
    error = dict(error or {})
    detail = error.get("detail")
    carried = detail.get("bridge_error") if isinstance(detail, dict) else None
    if isinstance(carried, dict) and carried.get("code") in BRIDGE_CODES:
        out = {"code": carried["code"], "message": carried.get("message") or error.get("message"),
               "hint": carried.get("hint"), "detail": carried.get("detail"),
               "provider": carried.get("provider") or provider,
               "retryable": bool(carried.get("retryable", error.get("retryable")))}
        return out
    code = str(error.get("code") or "internal_error")
    hint = None
    if isinstance(detail, dict):
        hint = detail.get("hint")
        if hint is None and isinstance(detail.get("license"), dict):
            hint = detail["license"].get("hint")
    wire_detail = dict(detail) if isinstance(detail, dict) else (
        {"detail": detail} if detail is not None else {})
    wire_detail["plexora_code"] = code
    return {"code": TO_BRIDGE.get(code, "job_failed"),
            "message": str(error.get("message") or code), "hint": hint,
            "detail": wire_detail, "provider": provider,
            "retryable": bool(error.get("retryable"))}


def http_status(error: dict | None) -> int:
    code = str((error or {}).get("code") or "")
    detail = (error or {}).get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("bridge_error"), dict):
        bridged = detail["bridge_error"].get("code")
        if bridged in ("stale_revision", "conflict", "workspace_mismatch", "lease_held"):
            return 409
        if bridged in ("capability_unavailable",):
            return 404
        if bridged == "license_required":
            return 403
    return HTTP_STATUS.get(code, 400)


def agent_error(exc):
    """A `spatialbridge.errors.BridgeError` as the AgentError a capability
    raises, carrying the original problem so `problem()` restores it."""
    from plexora.agent.errors import AgentError

    carried = exc.to_problem()
    detail = {"bridge_error": carried}
    if exc.hint:
        detail["hint"] = exc.hint
    return AgentError(FROM_BRIDGE.get(exc.code, "internal_error"), exc.message,
                      detail=detail, retryable=bool(exc.retryable))


#: Plexora's job states -> the protocol's three.
JOB_STATUS = {"queued": "running", "running": "running", "done": "done",
              "failed": "failed", "cancelled": "failed", "interrupted": "failed"}


def job_state(record: dict) -> dict:
    """A job record (`jobs.public`) as the protocol reads one:
    `{job_id, status: running|done|failed, progress, result?, error?}`, with
    Plexora's own status kept as `plexora_status`."""
    status = JOB_STATUS.get(record.get("status"), "running")
    out = {"job_id": record.get("job_id"), "status": status,
           "progress": record.get("progress"), "operation_id": record.get("operation_id"),
           "capability": record.get("capability"), "plexora_status": record.get("status")}
    if status == "done":
        result = record.get("result")
        out["result"] = dict(result) if isinstance(result, dict) else {"result": result}
        out["result"].setdefault("operation_id", record.get("operation_id"))
    elif status == "failed":
        error = record.get("error") or {"code": "internal_error",
                                        "message": f"the job was {record.get('status')}"}
        out["error"] = problem(error)
    return out
