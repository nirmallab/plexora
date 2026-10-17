"""The reference library as tools: sample cells, sort them, build sheets.

    library_sample  -> tiles of one marker from one image, into the library and
                       its Finder inbox, with a numbered contact sheet (a job)
    library_ingest  -> read the inbox's class folders: every tile sorted there
                       is labelled
    library_label   -> label tiles by id instead
    library_build   -> a reference sheet from the labelled tiles (one per
                       layout, to compare them)
    library_list    -> the tiles, sheets and inbox
    library_forget  -> take tiles or a sheet out
    library_path    -> where the library and its inbox are

A gating session shows a sheet once to each conversation answering its looks
(`SessionOptions.reference_sheet`). See `server/autogate/library.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.schemas import AgentModel
from plexora.plugins.gating.server.autogate import library, library_sheet, schemas

OWNER = "gating"
TAGS = ("gate", "gating", "reference", "library", "examples", "sheet", "few-shot",
        "faint", "threshold")
ENTITLEMENT = "ai:gating:analytics"


class SampleInput(AgentModel):
    project: str
    marker: str = Field(description="A marker of the project with an image channel.")
    n: int = Field(library.N_DEFAULT, ge=5, le=library.N_MAX,
                   description="Tiles to draw: an even share from each brightness band "
                               "(bright, moderate, dim, limit, background), each across "
                               "dense, sparse and typical areas.")
    seed: int = 0


class IngestInput(AgentModel):
    dry_run: bool = Field(False, description="Say what would be labelled; change nothing.")


class LabelEntry(AgentModel):
    tile_class: Literal[library.CLASSES + (library.DISCARD,)] = Field(alias="class")
    shape: Literal[library.SHAPES] | None = None
    note: str | None = Field(None, max_length=200)


class LabelInput(AgentModel):
    labels: dict[str, LabelEntry] = Field(max_length=500, description="{tile_id: {class, "
                                          "shape?, note?}}")


class BuildInput(AgentModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$", description="The sheet's name; "
                      "`default` is the one sessions take with reference_sheet=auto.")
    layout: Literal[library_sheet.LAYOUTS] = Field(
        "by_shape", description="by_shape: a row per stain shape, a column per class; "
                                "pairs: faint and limit positives each beside the closest "
                                "negative; by_marker: a row per marker.")
    shapes: list[Literal[library.SHAPES]] | None = None
    markers: list[str] | None = None
    per_class: int = Field(3, ge=1, le=3, description="pairs: pairs per row.")
    seed: int = 0


class ListInput(AgentModel):
    classes: list[Literal[library.CLASSES + (library.DISCARD,)]] | None = None
    shapes: list[Literal[library.SHAPES]] | None = None
    markers: list[str] | None = None
    projects: list[str] | None = None
    labelled: bool | None = Field(None, description="True: sorted tiles only; False: "
                                  "unsorted only.")
    limit: int = Field(200, ge=1, le=2000)


class ForgetInput(AgentModel):
    tile_ids: list[str] = Field(default_factory=list, max_length=500)
    sheet: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,40}$")


class PathInput(AgentModel):
    pass


def library_sample(call, inp):
    from plexora.agent import artifacts

    call.project_name = inp.project

    def progress(done, total):
        call.progress(done=done, total=total, message=f"{inp.marker}: {done} of {total} tiles")

    cancelled = getattr(call, "check_cancelled", None)
    out = library.sample(call.session, inp.project, inp.marker, n=inp.n, seed=inp.seed,
                         progress=progress, cancelled=cancelled)
    png = out.pop("contact_png")
    # A job's result keeps no images: the contact sheet is an artifact too.
    out["contact_artifact"] = artifacts.put(inp.project, png, {
        "kind": "plexora.gating_library_contact", "marker": inp.marker,
        "tiles": [t["tile_id"] for t in out["tiles"]]}, kind="library_contact")
    out["next"] = ("sort the inbox's tiles into its class folders (or library_label by id), "
                   "then library_ingest, then library_build")
    return out


def library_ingest(call, inp):
    out = library.ingest_inbox(dry_run=inp.dry_run)
    out["inbox"] = library.inbox_state()
    return out


def library_label(call, inp):
    return library.label({tid: {"class": entry.tile_class, "shape": entry.shape,
                                "note": entry.note}
                          for tid, entry in inp.labels.items()})


def library_build(call, inp):
    unknown = [m for m in inp.markers or () if not m.strip()]
    if unknown:
        raise AgentError("invalid_input", "a marker name is empty")
    built = library_sheet.build(inp.name, layout=inp.layout, shapes=inp.shapes,
                                markers=inp.markers, per_class=inp.per_class, seed=inp.seed)
    image = built.pop("image")
    fmt = built.pop("format")
    built["_images"] = [{"data": image, "format": fmt}]
    built["path"] = str(library.root() / "sheets" / f"{inp.name}.png")
    return built


def library_list(call, inp):
    found, warnings = library.tiles(classes=inp.classes, shapes=inp.shapes,
                                    markers=inp.markers, projects=inp.projects,
                                    labelled=inp.labelled)
    counts = {}
    for tile in found:
        key = tile.get("class") or "unsorted"
        counts[key] = counts.get(key, 0) + 1
    return {"library": str(library.root()), "n_tiles": len(found), "by_class": counts,
            "tiles": found[:inp.limit], "truncated": len(found) > inp.limit,
            "sheets": [{k: s.get(k) for k in ("name", "layout", "digest", "size", "built_at",
                                              "estimated_vision_tokens", "dropped")}
                       for s in library.sheets()],
            "inbox": library.inbox_state(), "warnings": warnings[:50],
            "classes": list(library.CLASSES), "shapes": list(library.SHAPES)}


def library_forget(call, inp):
    if not inp.tile_ids and not inp.sheet:
        raise AgentError("invalid_input", "name tile_ids or a sheet")
    return library.forget(inp.tile_ids, sheet=inp.sheet)


def library_path(call, inp):
    base = library.root()
    return {"library": str(base), "inbox": str(base / "inbox"),
            "sheets": str(base / "sheets"), "exists": base.is_dir(),
            "variable": library.ENV, "how_to_sort": library.HOW_TO_SORT}


def capabilities():
    from plexora.agent.registry import Capability
    from plexora.plugins.gating import PLUGIN

    def cap(**kwargs):
        kwargs.setdefault("tags", TAGS)
        kwargs.setdefault("entitlement", ENTITLEMENT)
        return Capability(owner=OWNER, version="1", **kwargs)

    return [
        cap(name="gating.library_sample", tool_name="library_sample",
            purpose="Draw single-cell tiles of one marker from one image into the gating "
                    "reference library: cells from every brightness band, in dense and "
                    "sparse areas, each drawn as a gating look draws it. They land in a "
                    "Finder inbox with a folder per class for a person to sort, and a "
                    "numbered contact sheet. A job.",
            permission="reversible_write", input_model=SampleInput, handler=library_sample,
            execution="job", requires=PLUGIN.requires, writes=("reference_library",),
            egress="rendered_pixels", reads=("table", "image", "mask"),
            tags=TAGS + ("sample", "cells", "thumbnails")),
        cap(name="gating.library_ingest", tool_name="library_ingest",
            purpose="Read how a person sorted the reference library's inbox: every tile "
                    "moved into a class folder (strong, faint, limit, negative, spill, "
                    "artifact, discard; a shape folder inside corrects its shape) is "
                    "labelled, and its inbox copy removed.",
            permission="reversible_write", input_model=IngestInput, handler=library_ingest,
            writes=("reference_library",), egress="metadata"),
        cap(name="gating.library_label", tool_name="library_label",
            purpose="Label reference-library tiles by id: a class, optionally a shape and "
                    "a note.",
            permission="reversible_write", input_model=LabelInput, handler=library_label,
            writes=("reference_library",), egress="metadata"),
        cap(name="gating.library_build", tool_name="library_build",
            purpose="Build a reference sheet from the labelled library tiles -- a row per "
                    "stain shape and a column per class, faint positives paired with the "
                    "closest negatives, or a row per marker -- for gating sessions to show "
                    "beside their looks (reference_sheet). Returns the sheet.",
            permission="reversible_write", input_model=BuildInput, handler=library_build,
            writes=("reference_library",), visual_output=True, egress="rendered_pixels"),
        cap(name="gating.library_list", tool_name="library_list",
            purpose="The reference library: its tiles (by class, shape, marker, image), "
                    "its sheets and what is waiting in its inbox.",
            permission="read", input_model=ListInput, handler=library_list,
            egress="metadata"),
        cap(name="gating.library_forget", tool_name="library_forget",
            purpose="Take tiles (with their labels) or a sheet out of the reference "
                    "library.",
            permission="destructive", input_model=ForgetInput, handler=library_forget,
            writes=("reference_library",), reversible=False, egress="metadata"),
        cap(name="gating.library_path", tool_name="library_path",
            purpose="Where the reference library, its inbox and its sheets are on disk, "
                    "and how to sort the inbox.",
            permission="read", input_model=PathInput, handler=library_path,
            egress="metadata"),
    ]


#: The vocabulary the tools' schemas name, for the skills' checks.
VOCABULARY = {"classes": library.CLASSES, "shapes": library.SHAPES,
              "layouts": library_sheet.LAYOUTS, "stain_shapes": schemas.STAIN_SHAPES}
