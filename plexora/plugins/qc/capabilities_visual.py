"""The visual pass's two looks: `render_artifact_overview` and
`inspect_artifact_channels`.

An agent cannot find a fold by reading forty channels one at a time. The
overview (`server/overview.py`) is the whole tissue in four views on which
large artifacts stand out, every region already written outlined on it; the
agent names the places that worry it. The channel sheet then shows one such
place in the channels that differ most from their surroundings, so the agent
picks the two to four channels that show the artifact before it asks magic
select (`segment_qc_roi`) to outline it. Both are looks: they write nothing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.core.visual import with_image
from plexora.agent.schemas import ProjectInput
from plexora.plugins.qc.capabilities_segment import ArtifactBox, SegBox
from plexora.plugins.qc.server import schemas

VISUAL = schemas.VISUAL
#: Ranked channels sent back at least (the sheet's `top` when larger).
RANKED_SENT = 12


class OverviewInput(ProjectInput):
    region: SegBox | ArtifactBox | None = Field(
        None, description="Zoom the same views on one place: a box in image pixels, or "
                          "{artifact_id, px: [x0, y0, x1, y1]} drawn on a picture you were "
                          "shown (an earlier overview, say). Default: the whole tissue.")
    panels: list[Literal["dna_max", "dna_cycles", "pan", "agreement"]] | None = Field(
        None, max_length=4, description="Which views to draw (default all four): dna_max "
                                        "(the brightest DNA across cycles), dna_cycles "
                                        "(first cycle cyan, last red), pan (the mean of the "
                                        "channels), agreement (channels bright or dark "
                                        "together).")
    size: Literal["standard", "large"] = Field(
        "standard", description="The side of each tile: standard, or large for more detail.")
    grid: bool = Field(True, description="Draw a labelled grid (A1..) to name places by.")
    show_regions: bool = Field(True, description="Outline the QC regions already written "
                                                 "(solid, r1..) and the Artifact Detector's "
                                                 "pending objects (dashed, d1..).")


class ChannelsInput(ProjectInput):
    box: SegBox | ArtifactBox = Field(description="The place: a box in image pixels, or "
                                                  "{artifact_id, px: [x0, y0, x1, y1]} on a "
                                                  "picture you were shown.")
    top: int = Field(VISUAL["channel_sheet_top"], ge=1, le=12,
                     description="How many channels the sheet shows, strongest contrast first.")
    exclude_nuclear: bool = Field(False, description="Leave the DNA channels off the sheet "
                                                     "(the reference tile still shows them).")
    channels: list[str] = Field(default_factory=list, max_length=12,
                                description="Also show these channels, first, wherever they "
                                            "rank (an autofluorescence channel, say, on an "
                                            "image of many channels); the rest of the sheet is "
                                            "filled by rank.")


def _box(project, box):
    from plexora.plugins.qc.server import frames

    if isinstance(box, ArtifactBox):
        found, _manifest = frames.locate_box(project, box.artifact_id, box.px)
        return found
    return box.model_dump()


def _public(manifest, keys):
    return {k: manifest.get(k) for k in keys if manifest.get(k) is not None}


def _working_result(project):
    """The result of a QC session still open on the project -- its regions
    are the ones that count during its visual pass -- else None (the active
    result)."""
    from plexora.plugins.qc.server import results
    from plexora.plugins.qc.server.engine import store

    for record in store().list(limit=50):
        if project in (record.get("images") or []) and record.get("result_id") \
                and record.get("state") not in schemas.FINISHED_STATES:
            return results.get_result(project, results.load(project), record["result_id"])
    return None


def render_overview(call, inp):
    from plexora.plugins.qc.server import overview

    region = _box(inp.project, inp.region) if inp.region is not None else None
    rendered = overview.build_sheet(call.session, inp.project, region=region, panels=inp.panels,
                                    size=inp.size, grid=inp.grid, show_regions=inp.show_regions,
                                    result=_working_result(inp.project))
    manifest = rendered["manifest"]
    out = {"artifact": rendered["artifact"],
           "manifest": {**_public(manifest, ("kind", "bounds", "level", "zoomed", "panels",
                                             "frames", "tissue", "channels", "pixel_um",
                                             "how_to_point")),
                        **overview.for_agent(manifest)},
           "max_regions": VISUAL["max_regions"],
           "next": ("name the places that are clearly abnormal (a fold, a tear, debris, a "
                    "bubble, lifted tissue), or say nothing is; then inspect_artifact_channels "
                    "on one place, and segment_qc_roi with preview: true")}
    return with_image(out, rendered["image"])


def inspect_channels(call, inp):
    from plexora.plugins.qc.server import overview

    box = _box(inp.project, inp.box)
    ranking = overview.rank_channels(call.session, inp.project, box, top=inp.top,
                                     exclude_nuclear=inp.exclude_nuclear,
                                     include=inp.channels)
    rendered = overview.channel_sheet(call.session, inp.project, ranking)
    # A shortlist: the strongest few beyond the sheet, plus every DNA and
    # autofluorescence channel wherever it ranks (a fold shows in those, a
    # lifted piece as DNA missing) -- however many channels the image has.
    keep = max(RANKED_SENT, inp.top)
    ranked = ranking["ranked"]
    sent = ranked[:keep] + [r for r in ranked[keep:]
                            if r["nuclear"] or r.get("autofluorescence")]
    out = {"artifact": rendered["artifact"], "box": ranking["box"],
           "ranked": sent, "ranked_more": len(ranked) - len(sent),
           "shown": ranking["shown"],
           "ring_on_tissue_fraction": ranking["ring_on_tissue_fraction"],
           "frames": rendered["manifest"]["frames"],
           "next": "choose two to four channels that show the artifact, by name, from what "
                   "the tiles show; the ranking is a shortlist, not a verdict"}
    return with_image(out, rendered["image"])


def capabilities(paid):
    return [
        paid(name="qc.render_artifact_overview", tool_name="render_artifact_overview",
             purpose="The whole tissue in four views on which large artifacts stand out: the "
                     "brightest DNA across cycles, the first cycle's DNA against the last's, "
                     "the mean of the channels, and where channels are bright or dark "
                     "together. Cropped to the tissue; the QC regions already written and the "
                     "Artifact Detector's pending objects outlined on every tile; a labelled "
                     "grid. Point on it ({artifact_id, px}) to zoom (region) or to outline "
                     "(segment_qc_roi). Writes nothing.",
             permission="read", input_model=OverviewInput, handler=render_overview,
             visual_output=True, egress="rendered_pixels", reads=("image", "qc", "rois"),
             tags=("qc", "artifact", "overview", "look", "fold", "tear", "debris", "visual")),
        paid(name="qc.inspect_artifact_channels", tool_name="inspect_artifact_channels",
             purpose="One place in every channel, ranked by how much it differs from the "
                     "tissue round it, and a contact sheet of the strongest few beside a DNA "
                     "reference: how to choose the channels that show a suspected artifact "
                     "before outlining it. Writes nothing.",
             permission="read", input_model=ChannelsInput, handler=inspect_channels,
             visual_output=True, egress="rendered_pixels", reads=("image", "qc"),
             tags=("qc", "artifact", "channels", "look", "contrast", "visual")),
    ]
