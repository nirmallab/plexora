"""One look at a check's scores: places sampled across the distribution.

The local checks do the finding; the agent's look says whether they found
artifacts or normal variation, and whether the bar sits right. Both the
session (`score_review` packets) and the free-standing sampler
(`sample_qc_examples`) build that look here, so the same field, bar and seed
give the same places and the same sheet:

    subject   `field_for` -- blur on a channel, a registration comparison,
              Segmentation QC's cluster map -- or `cells_for`, one
              segmentation reason's score per cell
    bar       the automatic threshold moved `offset_steps` (tighter +), or a
              value to preview
    regions   the flagged regions at that bar (grid subjects)
    strata    a few places from each part of the distribution
    sheet     `sheets.score_sheet`: a row per stratum, and the whole tissue

Nothing is stored but the sheet artifact; no pixel of the image is read but
the tiles drawn.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import schemas, score_fields

#: A tile's side, microns: enough tissue round the scored place to tell an
#: artifact from the tissue's own texture.
TILE_UM = {"blur": 80.0, "registration": 60.0, "segmentation": 60.0, "artifacts": 120.0}
#: A segmentation tile is at least this many nuclear diameters across.
SEG_TILE_NUCLEI = 4.0


def image_size(session, project):
    from plexora.server.utils import source_image

    with source_image.SHELF.reader(session.image_data(project)) as source:
        height, width = source.level_shape(0)
    return [int(width), int(height)]


def field_for(session, project, check, *, channel=None, comparison=None,
              seg_thresholds=None, category=None):
    """The ScoreField of a check as it was last run. Refused, with the tool
    that runs it, when it has not run."""
    if check == "artifacts":
        from plexora.plugins.qc.server import artifacts

        summary = artifacts.current(project)
        arrays = artifacts.load_arrays(project, summary["fingerprint"]) if summary else None
        if summary is None or arrays is None:
            raise AgentError("precondition_missing",
                             "the Artifact Detector has not run: run_artifact_check first",
                             detail={"hint": "run_artifact_check"})
        if category not in artifacts.CATEGORIES:
            raise AgentError("invalid_input", "give the artifact `category` to sample",
                             detail={"categories": list(artifacts.CATEGORIES)})
        return artifacts.score_field(summary, arrays, category)
    if check == "blur":
        from plexora.plugins.qc.server import blur

        label = blur.resolve(session, project, channel)
        summary = blur.current(project, label)
        if summary is None:
            raise AgentError("precondition_missing",
                             f"Blur QC has not run on {label}: run_blur_check first",
                             detail={"channel": label, "hint": "run_blur_check"})
        if summary.get("status") != "ok":
            raise AgentError("precondition_missing",
                             f"Blur QC could not score {label}: too few evaluable tiles",
                             detail={"channel": label, "status": summary.get("status")})
        arrays = blur.load_arrays(project, summary["fingerprint"])
        if arrays is None:
            raise AgentError("precondition_missing", "Blur QC's scores are gone: run it again",
                             detail={"hint": "run_blur_check"})
        return score_fields.from_blur(summary, arrays)
    if check == "registration":
        from plexora.plugins.qc.server import registration

        found = registration.cached_comparison(session, project, comparison)
        if found is None:
            raise AgentError("precondition_missing",
                             "this comparison has not been measured: "
                             "compute_registration_mismatch first",
                             detail={"comparison": comparison,
                                     "hint": "compute_registration_mismatch"})
        state = found["state"]
        return score_fields.from_registration(
            found["entry"], pixel_um=found["pixel_um"],
            fingerprint=found["field_fingerprint"], image_size=image_size(session, project),
            reference=state["reference"], comparison=state["comparison"],
            stats=found["stats"])
    if check == "segmentation":
        summary = _seg_summary(project)
        field = score_fields.from_segmentation(project, summary,
                                               image_size=image_size(session, project),
                                               thresholds=seg_thresholds)
        if field is None:
            raise AgentError("precondition_missing",
                             "this Segmentation QC result predates cell positions: run it "
                             "again", detail={"hint": "run_segmentation_qc"})
        return field
    raise AgentError("invalid_input", f"unknown check {check!r}",
                     detail={"allowed": list(schemas.CHECKS)})


def _seg_summary(project):
    from plexora.plugins.qc.server.segqc import run as segqc

    summary = segqc.current(project)
    if summary is None:
        raise AgentError("precondition_missing", "Segmentation QC has not run: "
                         "run_segmentation_qc first", detail={"hint": "run_segmentation_qc"})
    return summary


def cells_for(session, project, reason):
    """One segmentation reason's score per cell (`score_fields.CellScores`)."""
    if reason not in score_fields.SEG_CELL_SCORES:
        raise AgentError("invalid_input", f"unknown segmentation reason {reason!r}",
                         detail={"allowed": list(score_fields.SEG_CELL_SCORES)})
    summary = _seg_summary(project)
    scores = score_fields.cell_scores(project, reason, summary,
                                      image_size=image_size(session, project))
    if scores is None:
        raise AgentError("precondition_missing",
                         "this Segmentation QC result predates cell positions: run it again",
                         detail={"hint": "run_segmentation_qc"})
    return scores


def tile_side(subject) -> float:
    """A tile's side in full-resolution pixels."""
    pixel_um = subject.pixel_um
    if isinstance(subject, score_fields.CellScores) or subject.check == "segmentation":
        d_px = float(getattr(subject, "d_nucleus_px", None)
                     or (getattr(subject, "stats", {}) or {}).get("d_nucleus_px") or 16.0)
        by_nuclei = SEG_TILE_NUCLEI * d_px
        if pixel_um:
            return max(TILE_UM["segmentation"] / pixel_um, by_nuclei)
        return max(by_nuclei, 64.0)
    if pixel_um:
        return TILE_UM[subject.check] / pixel_um
    return max(2.0 * float(subject.grid["step"]), 64.0)


def _channels(session, project, subject):
    from plexora.agent.evidence import calibration as display
    from plexora.plugins.qc.server import sheets

    reference = getattr(subject, "reference", None)
    shown = None if subject.channel else getattr(subject, "display_channels", None)
    names = list(shown) if shown else [n for n in (reference, subject.channel) if n]
    try:
        record = display.current(session, project, names) if names else None
    except AgentError:
        record = None
    if shown:
        # A field of no single channel (the Artifact Detector's) is drawn in
        # the channels it names: nuclear first, then its lead channel.
        return sheets.score_channels(subject.check, record, channel=None, display=shown)
    if not subject.channel:
        return []
    return sheets.score_channels(subject.check if not isinstance(
        subject, score_fields.CellScores) else "segmentation", record,
        channel=subject.channel, reference=reference)


def _public_regions(found, n=5):
    if not found:
        return None
    return {"flagged_pct": found["flagged_pct"], "denominator": found["denominator"],
            "n_regions": found["n_regions"], "residual": found["residual"],
            "largest": [{k: r.get(k) for k in ("id", "cells", "area_um2", "mean", "max",
                                               "bbox", "peak")}
                        for r in found["regions"][:n]]}


def review(session, project, subject, *, offset_steps=0, threshold=None, strata=None,
           per_row=None, seed=0, fmt="webp", store=True, scan=None, source="auto",
           title=None) -> dict:
    """The look at `subject` (a ScoreField or CellScores) at a bar: the
    sheet, and what it shows in numbers (see the module docstring)."""
    from plexora.plugins.qc.server import sheets
    from plexora.server.utils import pixel_scale

    cells = isinstance(subject, score_fields.CellScores)
    per_row = min(int(per_row or schemas.ENGINE["score_per_stratum"]), sheets.SCORE_COLUMNS)
    if cells:
        bar = score_fields.cell_bar(subject, offset_steps, value=threshold)
        dist = score_fields.cell_distribution(subject)
        found = None
        rows = score_fields.sample_cells(subject, bar["value"], bar["step"], per_row=per_row,
                                         strata=strata, seed=seed)
        flagged = int((subject.values >= bar["value"]).sum())
        at = {"flagged": flagged, "flagged_pct": round(100.0 * flagged / subject.values.size, 2)
              if subject.values.size else 0.0, "denominator": "cells",
              "n_cells": int(subject.values.size)}
        global_row = {"possible": False, "why": None}
    else:
        bar = score_fields.bar(subject, offset_steps, value=threshold)
        dist = score_fields.distribution(subject)
        found = score_fields.regions(subject, bar["value"])
        rows = score_fields.sample_strata(subject, bar["value"], bar["step"], found,
                                          per_row=per_row, strata=strata, seed=seed)
        at = _public_regions(found)
        global_row = score_fields.global_possible(subject)
    record = session.project(project)
    pixel = pixel_scale.pixel_size(record)
    words = schemas.CHECK_WORDS.get(subject.check, subject.check)
    what = subject.reason if cells else words
    on = subject.channel or "the image"
    if not cells and subject.check == "registration" and subject.reference:
        on = f"{subject.channel} against {subject.reference}"
    title = title or (f"{project} - {what} in {on}: bar {bar['value']:.3g} "
                      f"({source}) - rows from clearly fine to far above")
    channels = _channels(session, project, subject)
    sheet = sheets.score_sheet(
        session, project, scan, subject if not cells else _as_field(subject),
        list(rows.items()), threshold=bar["value"], tile_px_side=tile_side(subject),
        channels=channels, fmt=fmt, pixel=pixel,
        regions=found, segmentation="outlines" if (cells or subject.check == "segmentation")
        else "none", title=title, overview=True,
        cell_marks=(float(subject.d_nucleus_px or 16.0) if cells else None), store=store)
    evidence = {
        "check": subject.check, "reason": subject.reason if cells else None,
        "channel": subject.channel, "reference": getattr(subject, "reference", None),
        "shown_channels": [c.name for c in channels],
        "score": {"name": subject.score_name, "range": list(subject.range),
                  "cell_um": None if cells else subject.cell_um},
        "distribution": {k: v for k, v in dist.items() if k != "histogram"},
        "histogram": (dist.get("histogram") or {}).get("counts"),
        "threshold": {"value": bar["value"], "auto": bar["auto"],
                      "offset_steps": bar["offset_steps"], "step": bar["step"],
                      "source": source},
        "at_threshold": at,
        "strata": {k: [p["score"] for p in v] for k, v in rows.items()},
        "global": global_row}
    return {"sheet": sheet, "bar": bar, "distribution": dist, "found": found,
            "strata": rows, "global": global_row, "evidence": evidence, "at": at}


class _PointsField:
    """What `score_sheet` needs of a subject whose places are cells."""

    def __init__(self, scores):
        import numpy as np

        self.check = "segmentation"
        self.channel = scores.channel
        self.reference = None
        self.fingerprint = scores.fingerprint
        self.score_name = scores.score_name
        self.values = np.zeros((1, 1))
        self.valid = np.zeros((1, 1), dtype=bool)
        self.grid = {"x0": 0.0, "y0": 0.0, "step": float(scores.d_nucleus_px or 16.0),
                     "nx": 1, "ny": 1, "image_size": list(scores.image_size)}


def _as_field(scores):
    return _PointsField(scores)


def manifest_of(result) -> list:
    """[{label, stratum, score, position, channel}] -- where each tile of a
    review is, so a caller can look again at one (`render_region`)."""
    out = []
    manifest = result["sheet"]["manifest"]
    for row in manifest.get("rows") or []:
        for index, tile in enumerate(row.get("tiles") or []):
            if tile.get("kind"):
                out.append({"label": f"{row['stratum']}:{tile['kind']}",
                            "stratum": row["stratum"], "kind": tile["kind"],
                            "bounds": tile.get("bounds")})
                continue
            position = dict(tile["position"])
            if tile.get("cell_id") is not None:
                position["cell_id"] = tile["cell_id"]
            out.append({"label": f"{row['stratum']}:{index + 1}", "stratum": row["stratum"],
                        "score": tile["score"], "position": position,
                        "channel": manifest.get("channel")})
    return out
