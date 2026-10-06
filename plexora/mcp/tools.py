"""One MCP tool per capability, with a signature built from its input model.

The SDK derives a tool's input schema from its function signature, so each
tool is a function whose signature IS the capability's pydantic model, field
for field -- descriptions included, which is what the agent reads. The body
does nothing but hand the arguments to `registry.invoke` on a worker thread
(reads are blocking file I/O) and serialize what comes back.
"""

from __future__ import annotations

import functools
import inspect
from typing import Annotated, Any

from pydantic import Field

from plexora.mcp import serialize

PERMISSION_NOTES = {
    "read": "Read-only.",
    "reversible_write": "Changes Plexora's own state (reversible); returns a receipt.",
    "source_file_write": ("Modifies the user's source file: needs the server's "
                          "--allow-source-writes and confirm=true, on the user's explicit "
                          "request only."),
    "destructive": ("Cannot be undone: needs the server's --allow-destructive and "
                    "confirm=true, on the user's explicit request only."),
}


def description_for(capability) -> str:
    parts = [capability.purpose, PERMISSION_NOTES[capability.permission]]
    if capability.viewer_required:
        parts.append("Needs an open Plexora viewer.")
    if capability.visual_output:
        parts.append("Returns an image plus a JSON manifest of exactly what was drawn.")
    # Static, whatever the licence: every tool is listed on every transport,
    # and the call is what decides (a `license_required` error on Free).
    if getattr(capability, "entitlement", None) not in (None, "free"):
        parts.append("Part of Plexora Paid; on Free it answers license_required, and over MCP "
                     "it also needs a licence with external MCP access.")
    return " ".join(parts)


def _parameters(model):
    params = []
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if info.description:
            annotation = Annotated[annotation, Field(description=info.description)]
        default = inspect.Parameter.empty if info.is_required() else info.get_default(
            call_default_factory=True)
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                                        default=default, annotation=annotation))
    return params


def split_images(result):
    """(result without images, [(bytes, format)]) -- images travel as image
    content. An entry is PNG bytes, or `{data, format}` for another format
    (WebP: the same pixels, a third of the bytes)."""
    if not isinstance(result, dict):
        return result, []
    images = result.pop("_images", None) or []
    out = []
    for image in images:
        if isinstance(image, (bytes, bytearray)):
            out.append((bytes(image), "png"))
        elif isinstance(image, dict) and isinstance(image.get("data"), (bytes, bytearray)):
            out.append((bytes(image["data"]), str(image.get("format") or "png")))
    return result, out


def _raise(outcome, bridge=False):
    """A refused call as the SDK's error result. `bridge`: the caller is
    another application (or the tool is one of the protocol's own), so the
    error is the protocol's shape and codes (`bridge_wire.problem`) -- a
    superset of Plexora's, which still carries Plexora's code in
    `detail.plexora_code`."""
    from mcp.server.mcpserver.exceptions import ToolError

    error = outcome["error"]
    if bridge:
        from plexora.agent.bridge_wire import problem

        error = problem(error)
    raise ToolError(serialize.bound({"error": error,
                                     "operation_id": outcome["operation_id"]}))


def _bridge_wire(capability, runtime) -> bool:
    from plexora.agent.registry import ORIGIN_BRIDGE

    return capability.owner == "bridge" or getattr(runtime, "origin", None) == ORIGIN_BRIDGE


#: How much one result may carry to another APPLICATION (a bridge-origin
#: connection): a program reading a capability catalogue or a QC result, not a
#: model's context, so the agent-sized bound would cut real data in half.
BRIDGE_RESULT_CHARS = 8_000_000


def _limit(runtime):
    from plexora.agent.limits import MAX_TOOL_CHARS
    from plexora.agent.registry import ORIGIN_BRIDGE

    return BRIDGE_RESULT_CHARS if getattr(runtime, "origin", None) == ORIGIN_BRIDGE \
        else MAX_TOOL_CHARS


def _answer(mcpserver, outcome, limit=None):
    result, images = split_images(outcome["result"])
    text = serialize.bound(result, limit) if limit else serialize.bound(result)
    if not images:
        return text
    return [mcpserver.Image(data=data, format=fmt) for data, fmt in images] + [text]


#: How long one wait slice blocks a worker thread before progress is reported.
PROGRESS_SLICE_S = 2.0


def _default_wait_s():
    from plexora.agent.core.jobs import WaitInput

    return float(WaitInput.model_fields["timeout_s"].default)


def tool_from_capability(capability, runtime):
    """An async tool function for one capability."""
    import anyio

    from plexora.mcp import require_mcp

    mcpserver = require_mcp()

    async def tool(**arguments) -> Any:
        # The request's token is a context variable: read it here, on the
        # request's own task, not on the worker thread.
        policy = runtime.request_policy()
        outcome = await anyio.to_thread.run_sync(
            functools.partial(runtime.invoke, capability.name, arguments, policy=policy))
        if not outcome["ok"]:
            _raise(outcome, _bridge_wire(capability, runtime))
        return _answer(mcpserver, outcome, _limit(runtime))

    async def streaming(ctx, **arguments) -> Any:
        # `job_wait`, in short slices on a worker thread, with a progress
        # notification whenever the job's progress moves. Every slice is its
        # own `invoke`, so nothing here reads the job except through it.
        policy = runtime.request_policy()
        timeout = float(arguments.get("timeout_s") or _default_wait_s())
        deadline = anyio.current_time() + timeout
        last = None
        while True:
            remaining = max(1.0, deadline - anyio.current_time())
            sliced = {**arguments, "timeout_s": min(PROGRESS_SLICE_S, remaining)}
            outcome = await anyio.to_thread.run_sync(
                functools.partial(runtime.invoke, capability.name, sliced, policy=policy))
            if not outcome["ok"]:
                _raise(outcome, _bridge_wire(capability, runtime))
            result = outcome["result"]
            progress = (result.get("job") or {}).get("progress") or {}
            if progress != last:
                last = progress
                try:
                    await ctx.report_progress(float(progress.get("done") or 0),
                                              progress.get("total"), progress.get("message"))
                except Exception:  # a client that asked for no progress token
                    pass
            if result.get("finished") or anyio.current_time() >= deadline:
                return _answer(mcpserver, outcome, _limit(runtime))

    params = _parameters(capability.input_model)
    fn = tool
    if capability.streams_progress:
        from mcp.server.mcpserver import Context

        fn = streaming
        # The SDK finds the context parameter through typing.get_type_hints
        # (so __annotations__) and builds the schema from the signature, which
        # it leaves the context out of: both have to name it.
        fn.__annotations__ = {"ctx": Context, "return": Any}
        params = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY,
                                    annotation=Context), *params]
    fn.__name__ = capability.tool_name
    fn.__doc__ = description_for(capability)
    fn.__signature__ = inspect.Signature(params, return_annotation=Any)
    return fn


def annotations_for(capability):
    from mcp.types import ToolAnnotations

    return ToolAnnotations(
        title=capability.name,
        read_only_hint=capability.permission == "read",
        destructive_hint=capability.permission in ("destructive", "source_file_write"),
        idempotent_hint=capability.permission == "read",
        open_world_hint=False,
    )
