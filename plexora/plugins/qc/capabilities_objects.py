"""Reviewing a 3-D mask's suspicious cells, one picture at a time.

    object_review_start    a queue of the cells a 3-D check calls suspicious
                           (one plane only, cut off by the stack's ends, taller
                           than wide, split between planes, odd volume) -- or
                           the cells named -- with a `delegate` block that hands
                           the looking to worker conversations
    object_review_next     the next cell: its XY, XZ and YZ views through its
                           centre (render_object_views), its measured row, and
                           why it is in the queue
    object_review_answer   a verdict: ok, merged, split, clipped, debris, unsure
    object_review_status   how far the review is, and its verdicts so far
    object_review_finish   the tally, and (by default) the cells judged not one
                           whole cell as one QC segmentation region holding
                           exactly them -- the region QC's cell calls, and
                           gating's exclusions, read

Plexora picks the cells (`server/volume/qc3d.py` `segmentation_3d`) and draws
them; the model only judges what the three views show. The queue lives in the
project's folder (`object_review_v1.json`); a new review replaces it.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.schemas import ProjectInput

RECORD = "object_review_v1.json"
#: Cells one worker conversation reviews before handing back.
OBJECT_REVIEW = {"objects_per_worker": 12, "max_objects": 96}
VERDICTS = ("ok", "merged", "split", "clipped", "debris", "unsure")
#: The verdicts that leave a cell out: it is not one whole cell.
NOT_ONE_CELL = ("merged", "split", "clipped", "debris")
REASONS = ("flat", "clipped_z", "merged_z", "split_z", "volume_outlier")


class StartInput(ProjectInput):
    reasons: list[Literal["flat", "clipped_z", "merged_z", "split_z", "volume_outlier"]] | None \
        = Field(None, description="Which suspicions to review (default: all).")
    object_ids: list[int] | None = Field(None, max_length=OBJECT_REVIEW["max_objects"],
                                         description="Review these cells instead.")
    max_objects: int = Field(24, ge=1, le=OBJECT_REVIEW["max_objects"])


class ReviewInput(ProjectInput):
    review_id: str


class AnswerInput(ReviewInput):
    object_id: int
    verdict: Literal["ok", "merged", "split", "clipped", "debris", "unsure"]
    note: str = Field("", max_length=400)
    model: str | None = Field(None, description="The model that judged, as the client names it.")


class FinishInput(ReviewInput):
    write_region: bool = Field(True, description="Write the cells judged not one whole cell "
                               "as a QC segmentation region holding exactly them.")


def _path(project):
    from plexora import paths

    return paths.project_state_dir(project) / RECORD


def _load(project, review_id=None):
    path = _path(project)
    if not path.exists():
        raise AgentError("precondition_missing", "no object review: object_review_start first")
    review = json.loads(path.read_text(encoding="utf-8"))
    if review_id and review.get("review_id") != review_id:
        raise AgentError("invalid_input", f"review {review_id!r} is not this project's current "
                         f"one ({review.get('review_id')})")
    return review


def _save(project, review):
    path = _path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review), encoding="utf-8")


def _status(review):
    answered = review["answers"]
    tally = {}
    for answer in answered.values():
        tally[answer["verdict"]] = tally.get(answer["verdict"], 0) + 1
    remaining = sum(1 for item in review["queue"] if str(item["object_id"]) not in answered)
    return {"review_id": review["review_id"], "objects": len(review["queue"]),
            "answered": len(answered), "remaining": remaining, "verdicts": tally,
            "state": "finished" if review.get("finished") else
                     ("decided" if not remaining else "open")}


def object_review_start(call, inp):
    """A queue of suspicious 3-D cells to judge from their three views."""
    import polars as pl

    from plexora.ai import delegation
    from plexora.server.volume import objects, qc3d

    record = call.session.project(inp.project)
    table, _manifest = objects.object_paths(record)
    if not table.exists():
        raise AgentError("precondition_missing", "measure the 3-D mask's cells first "
                         "(measure_objects): the review needs their measured rows")
    frame = pl.read_parquet(table)
    depth = record.image.depth
    _findings, flagged = qc3d.segmentation_3d(frame, depth=depth)
    if inp.object_ids:
        known = set(frame["label"].to_list())
        queue = [{"object_id": int(i), "reasons": flagged.get(int(i), [])}
                 for i in inp.object_ids if int(i) in known]
    else:
        wanted = set(inp.reasons or REASONS)
        queue = [{"object_id": int(label), "reasons": sorted(set(reasons) & wanted)}
                 for label, reasons in flagged.items() if set(reasons) & wanted]
        # The most suspicious first: cells with more reasons, then by id.
        queue.sort(key=lambda item: (-len(item["reasons"]), item["object_id"]))
    queue = queue[:inp.max_objects]
    if not queue:
        raise AgentError("invalid_input", "no cell to review: none is suspicious by those "
                         "reasons, or none of the ids is in the object table")
    review = {"review_id": "objrev_" + uuid.uuid4().hex[:10], "project": record.name,
              "created": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
              "queue": queue, "answers": {}, "finished": False}
    _save(record.name, review)
    out = _status(review)
    out["delegate"] = delegation.block("object_review_worker", project=record.name,
                                       review_id=review["review_id"])
    return out


def object_review_next(call, inp):
    """The next unanswered cell: three views, its row, and why it is here."""
    from plexora.agent.core.volume import ObjectViewsInput, render_object_views

    review = _load(inp.project, inp.review_id)
    if review.get("finished"):
        return {**_status(review), "object": None}
    item = next((q for q in review["queue"] if str(q["object_id"]) not in review["answers"]),
                None)
    if item is None:
        return {**_status(review), "object": None}
    rendered = render_object_views(call, ObjectViewsInput(project=inp.project,
                                                          object_id=item["object_id"]))
    manifest = rendered.get("manifest") or {}
    payload = {**_status(review), "object": {"object_id": item["object_id"],
                                             "reasons": item["reasons"],
                                             "row": (manifest.get("object") or {})},
               "artifact": rendered.get("artifact"),
               "answer_with": "object_review_answer {review_id, object_id, verdict, note}",
               "verdicts": {"ok": "one whole cell", "merged": "two or more cells as one",
                            "split": "part of a cell; another object holds the rest",
                            "clipped": "a cell cut off by the stack's first or last plane",
                            "debris": "not a cell", "unsure": "the views do not say"}}
    # The views' image rides along as render_object_views sent it.
    for key in ("_images", "image_inline", "note"):
        if key in rendered:
            payload[key] = rendered[key]
    return payload


def object_review_answer(call, inp):
    review = _load(inp.project, inp.review_id)
    if not any(q["object_id"] == inp.object_id for q in review["queue"]):
        raise AgentError("invalid_input", f"cell {inp.object_id} is not in this review")
    review["answers"][str(inp.object_id)] = {
        "verdict": inp.verdict, "note": inp.note, "model": inp.model,
        "at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()}
    _save(inp.project, review)
    return _status(review)


def object_review_status(call, inp):
    review = _load(inp.project, inp.review_id)
    out = _status(review)
    out["answers"] = review["answers"]
    return out


def object_review_finish(call, inp):
    """The tally, and the cells judged not one whole cell as a QC region."""
    review = _load(inp.project, inp.review_id)
    bad = sorted(int(k) for k, v in review["answers"].items() if v["verdict"] in NOT_ONE_CELL)
    out = _status(review)
    if inp.write_region and bad:
        out["region"] = _write_region(call, inp.project, bad, review)
    review["finished"] = True
    _save(inp.project, review)
    out["state"] = "finished"
    out["not_one_cell"] = bad
    return out


def _write_region(call, project, ids, review):
    """The cells as one region in QC's segmentation category, holding exactly
    them (a `labels` region); QC adopts it as its own (the `qc-class` token)."""
    from plexora.plugins.qc.server import roi_link, schemas
    from plexora.plugins.roi.server import service
    from plexora.plugins.roi.server.repository import ROIRepository

    data = call.data
    klass = "segmentation_error"
    roi_link.ensure_categories(data, [klass])
    solid = service.labels_solid(data, ids)
    category_id = schemas.roi_category_id(klass)
    label = next((c.get("label") for c in ROIRepository(project).load().get("categories") or []
                  if c.get("id") == category_id), category_id)
    _before, revision, roi = service.create_roi(
        data, category=label, geometry=service.footprint_of(solid["planes"]),
        name=f"{len(ids)} cell{'s' if len(ids) != 1 else ''} judged not one whole cell",
        notes=f"qc-class:{klass} -- object review {review['review_id']}", solid=solid)
    return {"roi_id": roi["id"], "revision": revision, "cells": len(ids)}


def capabilities(free):
    tags = ("qc", "segmentation", "3d", "objects", "cells", "review", "z-stack")
    return [
        free(name="qc.object_review_start", tool_name="object_review_start",
             purpose="Start reviewing a 3-D mask's suspicious cells (one plane only, cut off "
                     "by the stack's ends, taller than wide, split between planes, odd volume) "
                     "or named cells, one picture each; answers with a `delegate` block for "
                     "worker conversations.",
             permission="reversible_write", input_model=StartInput,
             handler=object_review_start, writes=("qc",), reads=("table", "image"),
             persistent=True, tags=tags),
        free(name="qc.object_review_next", tool_name="object_review_next",
             purpose="The next cell of an object review: its XY, XZ and YZ views through its "
                     "centre, its measured row, and why it is in the queue.",
             permission="read", input_model=ReviewInput, handler=object_review_next,
             visual_output=True, egress="rendered_pixels", reads=("image", "table"),
             tags=tags),
        free(name="qc.object_review_answer", tool_name="object_review_answer",
             purpose="Record a verdict on one cell of an object review: ok, merged, split, "
                     "clipped, debris or unsure.",
             permission="reversible_write", input_model=AnswerInput,
             handler=object_review_answer, writes=("qc",), persistent=True, tags=tags),
        free(name="qc.object_review_status", tool_name="object_review_status",
             purpose="How far an object review is, and its verdicts so far.",
             permission="read", input_model=ReviewInput, handler=object_review_status,
             egress="aggregates", tags=tags),
        free(name="qc.object_review_finish", tool_name="object_review_finish",
             purpose="Finish an object review: the tally, and the cells judged not one whole "
                     "cell written as one QC segmentation region holding exactly them.",
             permission="reversible_write", input_model=FinishInput,
             handler=object_review_finish, writes=("qc", "rois"), persistent=True,
             reads=("qc", "rois"), tags=tags),
    ]
