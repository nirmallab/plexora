"""Questions about a z-stack that need voxels, not a picture of them.

    colocalize_3d               how far two channels share voxels: Pearson's r and
                                Manders' coefficients over a range of planes, in a
                                region or a set of 3-D cells -- never on a projection
    find_regions_of_interest_3d where in a stack to look first: the brightest spots of
                                a marker, the densest cells, the cells a 3-D QC
                                calls suspicious, the planes out of focus

Colocalization is a voxel question. A projection puts two stains at different
depths on the same pixel, so "overlap in the MIP" is not colocalization -- a
projection view is refused (`inference_forbidden`). The voxels are read at the
finest level whose box (all the planes asked for) holds at most
`MAX_COLOC_VOXELS`; a coarser level pools pixels in x and y, which inflates
overlap, and the answer says so.

Region finding is deterministic: it ranks places by numbers the server
computes and hands back a centre and a size for each, with why; the agent
chooses which to look at (render_sections, render_object_views).
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import ProjectInput

TAGS = ("volume", "z-stack", "3d")
#: The most voxels one colocalization reads per channel.
MAX_COLOC_VOXELS = 30 * 2**20
#: The most places one search returns.
MAX_REGIONS = 24


# -- colocalize_3d ---------------------------------------------------------------


class ColocInput(ProjectInput):
    channels: list[str] = Field(..., min_length=2, max_length=2,
                                description="The two channels to compare.")
    planes: list[int] | None = Field(None, min_length=2, max_length=2, description="[z0, z1], "
                                     "inclusive (default: every plane).")
    view: str | None = Field(None, description="A plane ('z12') instead of `planes`. A "
                             "projection ('z4-19max') is refused: overlap in a projection is "
                             "not colocalization.")
    roi_id: str | None = Field(None, description="Only the voxels inside this region (on "
                               "the planes it covers).")
    object_ids: list[int] | None = Field(None, max_length=5000, description="Only the voxels "
                                         "of these cells of the 3-D mask.")
    threshold: Literal["otsu", "none"] = Field("otsu", description="Per-channel threshold "
                                               "for Manders' coefficients.")
    t: int = Field(0, ge=0)


def _planes_of(inp, depth):
    from plexora.api.representation import BadView, forbid, to_spec

    if inp.view:
        try:
            spec = to_spec(inp.view)
        except BadView as exc:
            raise AgentError("invalid_input", str(exc)) from None
        if spec.kind != "plane":
            raise forbid("colocalization_from_projection")
        return spec.z, spec.z
    if inp.planes:
        z0, z1 = sorted(int(v) for v in inp.planes)
    else:
        z0, z1 = 0, depth - 1
    if z0 < 0 or z1 >= depth:
        raise AgentError("invalid_input", f"planes {z0}..{z1} are outside the stack's {depth}")
    return z0, z1


def _region_mask(call, project, roi_id, level, box, z0, z1):
    """(Z, H, W) bool: the region on each plane it covers (its `extent`), False
    on the planes it leaves out. A region with an outline per plane is read by
    its footprint (the outlines' union): `get_roi` hands out the summary."""
    from shapely.geometry import shape

    summary = _roi(call, project, roi_id)
    x0, y0, x1, y1 = box
    scale = 2 ** level
    h, w = y1 - y0, x1 - x0
    out = np.zeros((z1 - z0 + 1, h, w), dtype=bool)
    extent = summary.get("extent") or {}
    if extent.get("kind") == "plane":
        lo = hi = int(extent["z"])
    elif extent.get("kind") == "slab":
        lo, hi = int(extent["z0"]), int(extent["z1"])
    else:
        lo, hi = z0, z1
    planes = (summary.get("solid") or {}).get("planes") or {}
    for z in range(max(z0, lo), min(z1, hi) + 1):
        geometry = planes.get(str(z)) or (summary.get("geometry") if not planes else None)
        if geometry is None:
            continue
        out[z - z0] = _rasterize(shape(geometry), x0, y0, scale, h, w)
    return out


def _rasterize(polygon, x0, y0, scale, h, w):
    """Pixel centres of a level-`log2(scale)` grid inside `polygon` (full-res px)."""
    import shapely

    ys, xs = np.mgrid[0:h, 0:w]
    px = (xs + x0 + 0.5) * scale
    py = (ys + y0 + 0.5) * scale
    return np.asarray(shapely.contains_xy(polygon, px.ravel(), py.ravel()),
                      dtype=bool).reshape(h, w)


def _roi(call, project, roi_id):
    from plexora.agent import registry

    answer = registry.invoke(call.session, "get_roi", {"project": project, "roi_id": roi_id})
    if not answer["ok"]:
        raise AgentError("invalid_input", f"no region {roi_id!r} in {project}")
    result = answer["result"]
    return result.get("roi", result)


def _objects(data):
    """The cell table with every column (a measured object table's boxes and
    centroids), or None -- `geometry()` holds only ids and positions."""
    if not data.table.available:
        return None
    try:
        return data.table.frame()
    except Exception:  # a table on a data node: only its ids and positions are here
        return data.table.geometry()


def _box_of(call, project, inp, record, level_info, factor):
    """The full-resolution box the question is about: the region's, the cells',
    or the whole image -- as (x0, y0, x1, y1) at the level read."""
    width, height = level_info["width"], level_info["height"]
    if inp.roi_id:
        from shapely.geometry import shape

        bounds = shape(_roi(call, project, inp.roi_id)["geometry"]).bounds
    elif inp.object_ids:
        frame = _objects(call.data)
        if frame is None or "bbox_x0" not in frame.columns:
            raise AgentError("precondition_missing", "cells by id need the measured 3-D "
                             "object table (measure_objects)")
        rows = frame.filter(frame["label"].is_in(list(inp.object_ids)))
        if not rows.height:
            raise AgentError("invalid_input", "none of those cells is in the object table")
        bounds = (rows["bbox_x0"].min(), rows["bbox_y0"].min(),
                  rows["bbox_x1"].max() + 1, rows["bbox_y1"].max() + 1)
    else:
        return 0, 0, width, height
    x0, y0, x1, y1 = (float(v) for v in bounds)
    return (max(0, int(x0 // factor)), max(0, int(y0 // factor)),
            min(width, int(math.ceil(x1 / factor))), min(height, int(math.ceil(y1 / factor))))


def colocalize_3d(call, inp):
    """Voxel colocalization of two channels over planes, in a region or cells."""
    from plexora.agent.core.volume import _channel_index, _volume_of
    from plexora.server.utils.source_image import open_project_volume

    record, _data, volume = _volume_of(call, inp.project)
    depth = int(volume["depth"])
    z0, z1 = _planes_of(inp, depth)
    if inp.roi_id and inp.object_ids:
        raise AgentError("invalid_input", "give a region or cells, not both")
    planes = z1 - z0 + 1
    levels = [info for info in volume["levels"] if not info.get("depth")] or volume["levels"]
    chosen = None
    for info in levels:
        factor = 2 ** int(info["level"])
        box = _box_of(call, inp.project, inp, record, info, factor)
        if planes * (box[2] - box[0]) * (box[3] - box[1]) <= MAX_COLOC_VOXELS:
            chosen = (info, factor, box)
            break
    if chosen is None:
        raise AgentError("too_large", "the region through those planes is too large to read "
                         "voxel by voxel even at the coarsest level; narrow the planes or the "
                         "region")
    info, factor, box = chosen
    level = int(info["level"])
    indices = [_channel_index(record, name) for name in inp.channels]
    source = open_project_volume(record)
    try:
        rows, cols = slice(box[1], box[3]), slice(box[0], box[2])
        a = np.asarray(source.read_block(level, inp.t, indices[0][0], z0, z1, rows, cols),
                       dtype=np.float64)
        b = np.asarray(source.read_block(level, inp.t, indices[1][0], z0, z1, rows, cols),
                       dtype=np.float64)
        spacing = source.volume.spacing
    finally:
        source.close()
    mask = np.ones(a.shape, dtype=bool)
    within = "every voxel of the planes"
    if inp.roi_id:
        mask = _region_mask(call, inp.project, inp.roi_id, level, box, z0, z1)
        within = f"region {inp.roi_id}"
    elif inp.object_ids:
        seg = call.data.segmentation.label_volume(inp.t)
        if seg is None:
            raise AgentError("precondition_missing", "cells by id need a 3-D label volume as "
                             "the mask")
        labels = seg.read(level if level < seg.levels else seg.levels - 1, z0, z1, rows, cols)
        if labels.shape != a.shape:
            raise AgentError("precondition_missing", "the mask's levels do not match the "
                             "image's at this resolution; give planes or a region instead")
        mask = np.isin(labels, np.asarray(inp.object_ids))
        within = f"{len(inp.object_ids)} cell(s)"
    va, vb = a[mask], b[mask]
    if va.size < 10:
        raise AgentError("invalid_input", "fewer than ten voxels in what was asked for")
    pearson = float(np.corrcoef(va, vb)[0, 1]) if va.std() and vb.std() else None
    if inp.threshold == "otsu":
        from skimage.filters import threshold_otsu

        ta = float(threshold_otsu(va)) if np.ptp(va) else float(va.max())
        tb = float(threshold_otsu(vb)) if np.ptp(vb) else float(vb.max())
    else:
        ta = tb = 0.0
    above_a, above_b = va > ta, vb > tb
    m1 = float(va[above_b].sum() / va.sum()) if va.sum() else None
    m2 = float(vb[above_a].sum() / vb.sum()) if vb.sum() else None
    both = int((above_a & above_b).sum())
    voxel_um3 = None
    if spacing.volumetric_ok():
        voxel_um3 = (spacing.x.microns() * factor) * (spacing.y.microns() * factor) \
            * spacing.z.microns()
    warnings = []
    if level:
        warnings.append(f"read at level {level} (pixels pooled {factor}x{factor} in x and y): "
                        "pooling raises apparent overlap; narrow the region for full resolution")
    return {
        "channels": [indices[0][1], indices[1][1]], "planes": [z0, z1], "t": inp.t,
        "within": within, "level": level, "voxels": int(va.size),
        "pearson_r": pearson, "manders_m1": m1, "manders_m2": m2,
        "thresholds": {indices[0][1]: ta, indices[1][1]: tb, "method": inp.threshold},
        "voxels_both_above": both,
        "volume_both_above_um3": round(both * voxel_um3, 2) if voxel_um3 else None,
        "m1_means": f"the share of {indices[0][1]}'s signal in voxels where "
                    f"{indices[1][1]} is above its threshold",
        "warnings": warnings,
    }


# -- find_regions_of_interest_3d -------------------------------------------------------


class FindRegionsInput(ProjectInput):
    criteria: list[Literal["bright", "dense", "seg_suspicious", "clipped",
                           "out_of_focus"]] = Field(
        default_factory=lambda: ["bright"], description="bright: a marker's brightest spots "
        "(needs `marker`); dense: the most crowded cells; seg_suspicious: 3-D cells that are "
        "not one whole cell; clipped: cells cut off by the stack's ends; out_of_focus: runs "
        "of blurred planes.")
    marker: str | None = Field(None, description="The channel for `bright`.")
    n: int = Field(6, ge=1, le=MAX_REGIONS)
    size_um: float = Field(50.0, gt=0, le=2000, description="The side of each place.")
    t: int = Field(0, ge=0)


def _spread_out(candidates, n, min_gap):
    """The best `n` of (score, x, y, z, why, extra) at least `min_gap` px apart in xy."""
    out = []
    for item in sorted(candidates, key=lambda c: -c[0]):
        if all(math.hypot(item[1] - o[1], item[2] - o[2]) >= min_gap for o in out):
            out.append(item)
        if len(out) >= n:
            break
    return out


def find_regions_of_interest_3d(call, inp):
    """Where in a z-stack to look first, by the criteria asked for."""
    from plexora.agent.core.volume import (_channel_index, _profile_level, _read_planes,
                                           _volume_of)
    from plexora.server.volume import qc3d

    record, _image, volume = _volume_of(call, inp.project)
    data = call.data
    spacing = volume.get("spacing") or {}
    px_um = ((spacing.get("x") or {}).get("value")) or None
    size_px = int(round(inp.size_um / px_um)) if px_um else int(round(inp.size_um * 2))
    per = max(1, inp.n // max(1, len(inp.criteria)))
    found, notes = [], []
    frame = _objects(data)
    has_objects = frame is not None and "bbox_z0" in frame.columns
    for criterion in inp.criteria:
        call.check_cancelled()
        if criterion == "bright":
            if not inp.marker:
                raise AgentError("invalid_input", "bright needs `marker`")
            index, label = _channel_index(record, inp.marker)
            planes, level = _read_planes(call, inp.project, volume, index, inp.t)
            scale = 2 ** level
            from scipy import ndimage

            candidates = []
            for z, plane in enumerate(planes):
                smooth = ndimage.gaussian_filter(np.asarray(plane, dtype=np.float32), 2)
                peaks = (smooth == ndimage.maximum_filter(smooth, size=9)) & (smooth > 0)
                for y, x in zip(*np.nonzero(peaks)):
                    candidates.append((float(smooth[y, x]), (x + 0.5) * scale,
                                       (y + 0.5) * scale, z, f"one of {label}'s brightest "
                                       f"spots, on plane {z}", None))
            found += [(c, "bright") for c in _spread_out(candidates, per, size_px)]
        elif criterion in ("dense", "seg_suspicious", "clipped"):
            if not has_objects:
                notes.append(f"{criterion}: needs the measured 3-D object table "
                             "(measure_objects)")
                continue
            if criterion == "dense":
                found += [(c, "dense") for c in _densest(frame, volume, size_px, per)]
            else:
                _findings, flagged = qc3d.segmentation_3d(frame, depth=int(volume["depth"]))
                wanted = ({"clipped_z"} if criterion == "clipped"
                          else {"flat", "merged_z", "split_z", "volume_outlier"})
                labels = frame["label"].to_numpy()
                xs, ys, zs = (frame[c].to_numpy() for c in ("X", "Y", "Z"))
                candidates = []
                for row, label_id in enumerate(labels):
                    reasons = set(flagged.get(int(label_id), [])) & wanted
                    if reasons:
                        candidates.append((float(len(reasons)), float(xs[row]), float(ys[row]),
                                           int(round(zs[row])),
                                           f"cell {int(label_id)}: {', '.join(sorted(reasons))}",
                                           int(label_id)))
                found += [(c, criterion) for c in _spread_out(candidates, per, size_px / 2)]
        elif criterion == "out_of_focus":
            index, label = _channel_index(record, inp.marker) if inp.marker \
                else _channel_index(record, None)
            planes, _level = _read_planes(call, inp.project, volume, index, inp.t)
            stats = qc3d.plane_stats(planes)
            info = volume["levels"][0]
            for finding in qc3d.focus_profile(stats, label)[:per]:
                middle = (finding["z0"] + finding["z1"]) // 2
                found.append(((1.0, info["width"] / 2.0, info["height"] / 2.0, middle,
                               finding["reason"], None), "out_of_focus"))
    regions = []
    for (score, x, y, z, why, object_id), criterion in found[:inp.n]:
        prefix = f"t{inp.t}" if inp.t else ""
        regions.append({
            "center": {"x": int(round(x)), "y": int(round(y)), "z": int(z)},
            "size_px": size_px, "size_um": inp.size_um if px_um else None,
            "criterion": criterion, "why": why,
            "suggested_view": f"{prefix}z{int(z)}",
            "next": ("render_object_views" if object_id is not None else "render_sections"),
            **({"object_id": object_id} if object_id is not None else {})})
    return {"project": record.name, "regions": regions, "notes": notes,
            "profile_level": _profile_level(volume)}


def _densest(frame, volume, size_px, n):
    """Cells with the most neighbours within half a place's side (3-D, µm
    where the table has them)."""
    from scipy.spatial import cKDTree

    if {"x_um", "y_um", "z_um"} <= set(frame.columns):
        points = np.column_stack([frame[c].to_numpy() for c in ("x_um", "y_um", "z_um")])
        spacing = volume.get("spacing") or {}
        px = ((spacing.get("x") or {}).get("value")) or 1.0
        radius = size_px * px / 2.0
    else:
        # Planes are not pixels: a plane counts as the z step over the pixel
        # size, so two cells two planes apart on a 4:1 stack are 8 px apart.
        anisotropy = float(volume.get("anisotropy") or 1.0)
        points = np.column_stack([frame["X"].to_numpy(), frame["Y"].to_numpy(),
                                  frame["Z"].to_numpy() * anisotropy])
        radius = size_px / 2.0
    if not len(points):
        return []
    counts = np.asarray(cKDTree(points).query_ball_point(points, radius, return_length=True))
    xs, ys, zs = (frame[c].to_numpy() for c in ("X", "Y", "Z"))
    candidates = [(float(counts[i]), float(xs[i]), float(ys[i]), int(round(zs[i])),
                   f"{int(counts[i])} cells within {size_px // 2} px", None)
                  for i in np.argsort(-counts)[:max(50, n * 10)]]
    return _spread_out(candidates, n, size_px)


def capabilities():
    return [
        Capability(
            name="volume.colocalize_3d", tool_name="colocalize_3d", owner="core",
            purpose="How far two channels share voxels in a z-stack: Pearson's r and Manders' "
                    "M1/M2 (Otsu thresholds) over a range of planes, in a region or a set of "
                    "3-D cells. Never on a projection (refused): overlap in a projection is "
                    "not colocalization.",
            permission="read", input_model=ColocInput, handler=colocalize_3d,
            execution="job", egress="aggregates", reads=("image", "rois", "table"),
            tags=TAGS + ("colocalization", "pearson", "manders", "overlap")),
        Capability(
            name="volume.find_regions_of_interest_3d", tool_name="find_regions_of_interest_3d",
            owner="core",
            purpose="Where in a z-stack to look first: a marker's brightest spots, the most "
                    "crowded cells, 3-D cells a check calls suspicious or cut off by the "
                    "stack's ends, runs of blurred planes -- each a centre (x, y, z), a size "
                    "and why, with the view to look at it in.",
            permission="read", input_model=FindRegionsInput,
            handler=find_regions_of_interest_3d, execution="job", egress="aggregates",
            reads=("image", "table"), tags=TAGS + ("regions", "sample", "where", "look")),
    ]
