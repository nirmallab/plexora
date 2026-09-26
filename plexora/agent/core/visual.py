"""Visual evidence as capabilities: render a region, fetch an artifact."""

from __future__ import annotations

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_INLINE_BYTES
from plexora.agent.registry import Capability
from plexora.agent.render_spec import RenderInput
from plexora.agent.schemas import AgentModel


def with_image(result, png):
    """A result carrying its PNG inline, unless it is too big to."""
    if len(png) <= MAX_INLINE_BYTES:
        result["_images"] = [png]
        result["image_inline"] = True
    else:
        result["image_inline"] = False
        result["note"] = ("the image is larger than can be sent inline; read it from "
                          "the artifact uri")
    return result


def render_region(call, inp):
    from plexora.agent import render

    rendered = render.render_region(call.session, inp)
    return with_image({"manifest": rendered["manifest"], "artifact": rendered["artifact"]},
                      rendered["png"])


class ArtifactInput(AgentModel):
    artifact_id: str = Field(description="An artifact id (art_...) from a render or capture.")
    include_image: bool = True


def get_artifact(call, inp):
    from plexora.agent import artifacts

    try:
        png, sidecar = artifacts.get(inp.artifact_id)
    except KeyError as exc:
        raise AgentError("invalid_input", str(exc.args[0])) from None
    result = {"artifact": {k: v for k, v in sidecar.items() if k != "manifest"},
              "manifest": sidecar.get("manifest")}
    return with_image(result, png) if inp.include_image else result


class ListArtifactsInput(AgentModel):
    project: str | None = None
    limit: int = Field(20, ge=1, le=200)


def list_artifacts(call, inp):
    from plexora.agent import artifacts

    return {"artifacts": artifacts.list_artifacts(inp.project, inp.limit)}


def capabilities():
    return [
        Capability(
            name="image.render_region", tool_name="render_region", owner="core",
            purpose="Render a region of a project's image as the viewer draws it -- "
                    "chosen channels and windows, the cell mask as outlines or fill, "
                    "cells a gate calls positive highlighted, cell ids, a scale bar -- "
                    "deterministically, from the source pyramid, without touching the "
                    "viewer. Regions by pixel box, by centre and size in µm, or by ROI.",
            permission="read", input_model=RenderInput, handler=render_region,
            visual_output=True, egress="rendered_pixels", reads=("image", "mask", "table"),
            tags=("render", "look", "see", "show", "image", "picture", "view", "visual",
                  "inspect", "region", "field", "overlay", "segmentation")),
        Capability(
            name="artifact.get", tool_name="get_artifact", owner="core",
            purpose="A stored render or capture by id: the image and its manifest.",
            permission="read", input_model=ArtifactInput, handler=get_artifact,
            visual_output=True, egress="rendered_pixels",
            tags=("artifact", "evidence", "image")),
        Capability(
            name="artifact.list", tool_name="list_artifacts", owner="core",
            purpose="Recent stored renders and captures, newest first.",
            permission="read", input_model=ListArtifactsInput, handler=list_artifacts,
            tags=("artifact", "evidence", "history")),
    ]
