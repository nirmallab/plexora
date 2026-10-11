"""QC of a z-stack as a stack: which planes, which cycles, which objects.

    run_qc_3d              a job: the stack checks (server/volume/qc3d.py) over
                           the channels -- focus by plane, fading with depth,
                           empty / repeated / outlier planes, tile seams -- a
                           cycle drifting in z against the reference cycle, and
                           the 3-D mask's objects that are not one whole cell
    get_qc_3d              what the last run found
    write_qc_3d_regions    its findings as QC regions: a finding on some planes
                           is a region on those planes (`extent`), so the cells
                           it leaves out are the ones on them

Every finding maps to one of QC's five categories through a class QC already
has (`CLASS_OF`): the stack checks find old problems on a new axis. Results
are kept in the project's own folder (`qc3d_v1.json`), replaced by each run.
A region limited to some planes needs a cell table that says which plane a
cell is on (a z column): without one, it would leave out every plane's cells,
so it is not written and the answer says why.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Literal

import numpy as np
from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.schemas import ProjectInput
from plexora.plugins.qc.server.region_write import (check_candidate, write_receipt,
                                                    write_regions)

DETECTOR = "stack_checks"
VERSION = "1"
RECORD = "qc3d_v1.json"
ALL_CHECKS = ("focus_profile", "axial_attenuation", "missing_slices", "stitching_seams",
              "z_registration_drift", "segmentation_3d")
#: Which QC class each finding is (and with it, which category).
CLASS_OF = {
    "focus_profile": "out_of_focus",
    "axial_attenuation": "illumination_or_shading",
    "missing_slices:empty": "empty_or_failed_channel",
    "missing_slices:duplicate": "other_technical",
    "missing_slices:outlier": "illumination_or_shading",
    "missing_slices:faint": "empty_or_failed_channel",
    "stitching_seams": "stitching_or_tile_seam",
    "z_registration_drift": "cross_cycle_registration_error",
}
#: Half the width of a seam's region, in full-resolution pixels.
SEAM_HALF_WIDTH = 6
#: The coarse 3-D read a cycle's nuclear volume is compared at: at most this
#: many voxels.
DRIFT_VOXELS = 16 * 2**20


class Qc3dInput(ProjectInput):
    channels: list[str] | None = Field(None, max_length=8, description="Channels for the "
                                       "plane checks (default: every channel, at most eight).")
    checks: list[Literal["focus_profile", "axial_attenuation", "missing_slices",
                         "stitching_seams", "z_registration_drift", "segmentation_3d"]] | None \
        = Field(None, description="Which checks (default: all).")
    t: int = Field(0, ge=0, description="Frame of a time series.")


class PlaneInput(ProjectInput):
    z: int | None = Field(None, ge=0, description="The plane QC's checks read on a z-stack; "
                          "omit to go back to the stack's default plane.")
    t: int = Field(0, ge=0, description="Frame of a time series.")


class Qc3dWriteInput(ProjectInput):
    findings: list[str] | None = Field(None, description="Ids of the findings to write "
                                       "(default: every one that can be a region).")


def _path(project):
    from plexora import paths

    return paths.project_state_dir(project) / RECORD


def _load(project):
    path = _path(project)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _drift_level(volume) -> int:
    """The finest level keeping every plane whose whole stack holds at most
    DRIFT_VOXELS voxels (a level pooling planes would blur the z shift)."""
    whole = [info for info in volume["levels"] if not info.get("depth")] or volume["levels"]
    for info in whole:
        if int(volume["depth"]) * int(info["height"]) * int(info["width"]) <= DRIFT_VOXELS:
            return int(info["level"])
    return int(whole[-1]["level"])


def _read_stack(call, project, volume, channel, t, level):
    from plexora.server.utils.source_image import SourceImage

    data = call.session.image_data(project)
    info = volume["levels"][level]
    box = (0, 0, info["width"], info["height"])
    prefix = f"t{t}" if t else ""
    planes = []
    for z in range(int(volume["depth"])):
        with SourceImage(data, view=f"{prefix}z{z}") as source:
            plane, _ = source.read(channel, level, box)
        planes.append(np.asarray(plane, dtype=np.float32))
    return np.stack(planes)


def run_qc_3d(call, inp):
    """The stack checks over a z-stack; findings kept for get_qc_3d and
    write_qc_3d_regions."""
    from plexora.agent.core import volume as core_volume
    from plexora.plugins.qc.server import cycles as qc_cycles
    from plexora.server.volume import qc3d

    record, _data, volume = core_volume._volume_of(call, inp.project)
    depth = int(volume["depth"])
    if depth < 2:
        raise AgentError("invalid_input", f"{inp.project} has one plane per frame; these "
                         "checks compare planes")
    if depth > core_volume.MAX_PLANES_PER_PROFILE:
        raise AgentError("too_large", f"{depth} planes is more than one check reads "
                         f"({core_volume.MAX_PLANES_PER_PROFILE})")
    checks = tuple(inp.checks or ALL_CHECKS)
    names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
    wanted = inp.channels or names[:8]
    plane_checks = tuple(c for c in checks if c in qc3d.CHECKS)
    findings, per_channel, level = [], {}, None
    seam_channel = None
    total = len(wanted) + 2
    for done, name in enumerate(wanted):
        call.check_cancelled()
        index, label = core_volume._channel_index(record, name)
        planes, level = core_volume._read_planes(call, inp.project, volume, index, inp.t)
        if plane_checks:
            found, stats = qc3d.run_checks(planes, channel=label, checks=plane_checks)
            findings += found
            per_channel[label] = {"best_z": int(np.argmax([r["focus"] for r in stats])),
                                  "focus": [round(r["focus"], 3) for r in stats]}
        if "stitching_seams" in checks and seam_channel is None:
            # Seams are the acquisition's, the same in every channel: one is enough.
            seam_channel = label
            findings += qc3d.stitching_seams(planes, channel=label, scale=2 ** level)
        call.progress(done + 1, total, f"checking {label}")
    cycle_info = None
    if "z_registration_drift" in checks:
        inferred = qc_cycles.infer(names)
        groups = [c for c in inferred.get("cycles") or [] if c.get("nuclear")]
        cycle_info = {"method": inferred.get("method"), "cycles": len(groups)}
        if len(groups) >= 2:
            drift_level = _drift_level(volume)
            stacks = []
            for group in groups:
                call.check_cancelled()
                index, _label = core_volume._channel_index(record, group["nuclear"])
                stacks.append((group["index"], group["channels"],
                               _read_stack(call, inp.project, volume, index, inp.t,
                                           drift_level)))
            reference = stacks[0]
            findings += qc3d.z_registration_drift(
                reference[2], [(c, ch, v) for c, ch, v in stacks[1:]],
                reference_cycle=reference[0])
    call.progress(total - 1, total, "checking objects")
    flagged = {}
    if "segmentation_3d" in checks:
        frame = _objects(record)
        if frame is not None:
            found, flagged = qc3d.segmentation_3d(frame, depth=depth)
            findings += found
    for number, finding in enumerate(findings, start=1):
        finding["id"] = f"f{number}"
        finding["class"] = _class_of(finding)
    out = {"project": record.name, "t": inp.t, "depth": depth, "level": level,
           "checks": list(checks), "findings": findings, "channels": per_channel,
           "cycles": cycle_info, "objects_flagged": len(flagged),
           "flagged": {str(k): v for k, v in flagged.items()},
           "params": dict(qc3d.PARAMS),
           "created": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()}
    path = _path(record.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out), encoding="utf-8")
    brief = {key: out[key] for key in out if key != "flagged"}
    brief["clean"] = not findings
    return brief


def _objects(record):
    """The measured 3-D object table (`objects_v1.parquet`), or None."""
    import polars as pl

    from plexora.server.volume import objects

    table, _manifest = objects.object_paths(record)
    return pl.read_parquet(table) if table.exists() else None


def _class_of(finding):
    if finding["check"] == "segmentation_3d":
        return "segmentation_error"
    if finding["check"] == "missing_slices":
        return CLASS_OF.get(f"missing_slices:{finding.get('kind')}", "other_technical")
    return CLASS_OF.get(finding["check"], "other_technical")


def get_qc_3d(call, inp):
    """What the last stack check found (without the per-object list)."""
    call.session.project(inp.project)
    found = _load(inp.project)
    if found is None:
        raise AgentError("precondition_missing", "the stack has not been checked yet: run "
                         "run_qc_3d first")
    flagged = found.pop("flagged", {})
    found["objects_flagged"] = len(flagged)
    found["flagged_examples"] = dict(list(flagged.items())[:20])
    return found


def _image_box(record):
    width, height = int(record.image.width or 0), int(record.image.height or 0)
    return width, height


def _rectangle(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _geometry(finding, width, height):
    """Where a finding is on the image: the whole plane for a plane finding,
    thin bands along a seam's line for a seam."""
    if finding["check"] != "stitching_seams":
        return _rectangle(0, 0, width, height)
    bands = []
    for position in finding["positions"]:
        low, high = position - SEAM_HALF_WIDTH, position + SEAM_HALF_WIDTH
        bands.append(_rectangle(max(0, low), 0, min(width, high), height)["coordinates"]
                     if finding["axis"] == "x"
                     else _rectangle(0, max(0, low), width, min(height, high))["coordinates"])
    return {"type": "MultiPolygon", "coordinates": bands}


def write_qc_3d_regions(call, inp):
    """The last run's findings as QC regions, by class; a finding on some planes
    limited to them. Object findings (segmentation_3d) are per cell, not
    regions, and are reported, not written."""
    record = call.session.project(inp.project)
    found = _load(inp.project)
    if found is None:
        raise AgentError("precondition_missing", "the stack has not been checked yet: run "
                         "run_qc_3d first")
    data = call.session.image_data(inp.project)
    has_z = bool(data.schema is not None and data.schema.get("z"))
    width, height = _image_box(record)
    chosen = [f for f in found["findings"]
              if (not inp.findings or f["id"] in inp.findings)
              and f["check"] != "segmentation_3d"]
    skipped, by_class = [], {}
    for finding in chosen:
        whole_stack = finding["z0"] == 0 and finding["z1"] == found["depth"] - 1
        if not whole_stack and not has_z:
            skipped.append({"id": finding["id"], "why": "the cell table has no z column, so "
                            "cells cannot be told apart by plane"})
            continue
        extent = None if whole_stack else {"kind": "slab", "z0": finding["z0"],
                                           "z1": finding["z1"], "t": found.get("t", 0)}
        channels = list(finding.get("channels") or ([finding["channel"]]
                                                    if finding.get("channel") else []))
        scope = ("cycle" if finding["check"] == "z_registration_drift"
                 else "channel" if channels else "all_channels")
        candidate = check_candidate(
            DETECTOR, VERSION, finding["class"], f"{found['created']}:{finding['id']}",
            {"geometry": _geometry(finding, width, height), "max": None},
            channels=channels, scope=scope,
            cycles=[finding["cycle"]] if finding.get("cycle") else (),
            threshold={"check": finding["check"], "reason": finding["reason"]},
            action="warn" if finding.get("severity") == "warn" else "exclude",
            extent=extent)
        by_class.setdefault(finding["class"], []).append(candidate)
    written, kept, removed, receipts = [], [], [], []
    revisions = (None, None)
    for klass, candidates in by_class.items():
        w, k, r, rec, _per_key, (before, after) = write_regions(
            call, inp.project, detector=DETECTOR, klass=klass, covered={klass},
            work=[(klass, candidates)], row_keys=lambda row: [row.get("class")])
        written += w
        kept += k
        removed += r
        receipts += rec
        # One receipt for the whole write: from before the first class's to
        # after the last's.
        revisions = (revisions[0] if revisions[0] is not None else before, after)
    if not by_class and not skipped:
        raise AgentError("precondition_missing", "the last stack check found nothing to "
                         "write as a region")
    return {"receipt": write_receipt(call, written, kept, removed, receipts, revisions),
            "written": written, "kept": kept, "removed": [r["roi_id"] for r in removed],
            "skipped": skipped,
            "objects": "per cell, not regions: see get_qc_3d (segmentation_3d)"}


def set_qc_plane(call, inp):
    """Which plane of a z-stack QC's checks read; each check runs again on it."""
    from plexora.agent.receipts import make_receipt
    from plexora.plugins.qc.server import plane

    record = call.session.project(inp.project)
    if not record.image.is_volumetric:
        raise AgentError("invalid_input", f"{inp.project} is not a z-stack: there is one "
                         "plane to check")
    before = plane.view_of(inp.project)
    after = plane.set_view(inp.project, inp.z, inp.t)
    receipt = make_receipt(call, changed=before != after, before={"plane": before or None},
                           after={"plane": after or None}, persistent_state="plugin_store:qc",
                           reversible=True,
                           undo_hint={"tool": "set_qc_plane", "arguments": {
                               "project": inp.project,
                               **({"z": int(before.split("z")[-1])} if before else {})}})
    return {"plane": after or None, "receipt": receipt.model_dump(mode="json"),
            "note": "every QC check reads this plane now; results made on another plane "
                    "are not reused"}


def capabilities(free):
    tags = ("qc", "z-stack", "volume", "planes", "focus", "attenuation", "seams", "drift",
            "segmentation", "3d")
    return [
        free(name="qc.run_qc_3d", tool_name="run_qc_3d",
             purpose="Check a z-stack as a stack: planes out of focus, signal fading with "
                     "depth, empty or repeated planes, tile seams, a cycle drifting in z "
                     "against the reference cycle, and 3-D objects that are not one whole "
                     "cell. Plane ranges and reasons, per channel; kept for write_qc_3d_regions.",
             permission="read", input_model=Qc3dInput, handler=run_qc_3d,
             execution="job", egress="aggregates", reads=("image", "table"), tags=tags),
        free(name="qc.get_qc_3d", tool_name="get_qc_3d",
             purpose="What the last stack check (run_qc_3d) found.",
             permission="read", input_model=ProjectInput, handler=get_qc_3d,
             egress="aggregates", reads=("qc",), tags=tags),
        free(name="qc.set_qc_plane", tool_name="set_qc_plane",
             purpose="Choose which plane of a z-stack QC's checks (blur, artifacts, "
                     "registration, segmentation, DNA retention) read; omitted, the stack's "
                     "default plane.",
             permission="reversible_write", input_model=PlaneInput, handler=set_qc_plane,
             writes=("qc",), persistent=True, reads=("qc",), tags=tags),
        free(name="qc.write_qc_3d_regions", tool_name="write_qc_3d_regions",
             purpose="Write the last stack check's findings as QC regions: one on some planes "
                     "is limited to them, so the cells it leaves out are the ones there "
                     "(needs a z column in the cell table).",
             permission="reversible_write", input_model=Qc3dWriteInput,
             handler=write_qc_3d_regions, writes=("qc", "rois"), persistent=True,
             reads=("qc", "rois"), tags=tags),
    ]
