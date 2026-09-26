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
    """(result without images, [png bytes]) -- images travel as image content."""
    if not isinstance(result, dict):
        return result, []
    images = result.pop("_images", None) or []
    return result, [image for image in images if isinstance(image, (bytes, bytearray))]


def tool_from_capability(capability, runtime):
    """An async tool function for one capability."""
    import anyio

    from plexora.mcp import require_mcp

    mcpserver = require_mcp()
    from mcp.server.mcpserver.exceptions import ToolError

    async def tool(**arguments) -> Any:
        outcome = await anyio.to_thread.run_sync(
            functools.partial(runtime.invoke, capability.name, arguments))
        if not outcome["ok"]:
            raise ToolError(serialize.bound({"error": outcome["error"],
                                             "operation_id": outcome["operation_id"]}))
        result, images = split_images(outcome["result"])
        text = serialize.bound(result)
        if not images:
            return text
        return [mcpserver.Image(data=bytes(png), format="png") for png in images] + [text]

    tool.__name__ = capability.tool_name
    tool.__doc__ = description_for(capability)
    tool.__signature__ = inspect.Signature(_parameters(capability.input_model),
                                           return_annotation=Any)
    return tool


def annotations_for(capability):
    from mcp.types import ToolAnnotations

    return ToolAnnotations(
        title=capability.name,
        read_only_hint=capability.permission == "read",
        destructive_hint=capability.permission in ("destructive", "source_file_write"),
        idempotent_hint=capability.permission == "read",
        open_world_hint=False,
    )
