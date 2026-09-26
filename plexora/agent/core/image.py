"""The reference image: its channels and what their intensities look like."""

from __future__ import annotations

from pydantic import Field

from plexora.agent.registry import Capability
from plexora.agent.schemas import ProjectInput


class ChannelsInput(ProjectInput):
    with_stats: bool = Field(False, description="Also read each channel's intensity "
                             "range and auto window (p1/p99.9) from the coarsest level.")


def list_channels(call, inp):
    from plexora.server.utils import source_image

    record = call.session.project(inp.project)
    channels = []
    for index, channel in enumerate(record.image.real_channels):
        channels.append({"index": index,
                         "name": channel.get("name"),
                         "fullname": channel.get("fullname") or channel.get("name"),
                         "key": source_image.channel_key(channel)})
    out = {"project": record.name, "kind": record.image.kind,
           "modality": record.image.modality, "channels": channels,
           "brightfield": None}
    if record.image.is_blank:
        out["note"] = "this sample has no image, only a reference frame"
        return out
    if inp.with_stats:
        data = call.session.image_data(inp.project)
        with source_image.SHELF.reader(data) as source:
            out["brightfield"] = source.is_brightfield
            if not source.is_brightfield:
                for entry in channels:
                    try:
                        entry["stats"] = source_image.channel_stats(source, entry["key"])
                    except source_image.RenderError as exc:
                        entry["stats"] = {"error": str(exc)}
    return out


def capabilities():
    return [
        Capability(
            name="image.channels", tool_name="list_channels", owner="core",
            purpose="The image channels of a project (names, stable keys, order), "
                    "optionally with each channel's intensity range and auto window.",
            permission="read", input_model=ChannelsInput, handler=list_channels,
            egress="aggregates", reads=("image",),
            tags=("channel", "channels", "image", "stain", "marker", "intensity"),
        ),
    ]
