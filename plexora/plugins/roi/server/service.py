"""ROI annotations for a caller that is not the ROI panel.

The panel edits the document through `routes.py`, one batch of operations at a
time against the revision it last read. An agent does the same thing -- through
the same `ROIRepository.apply`, so the revision check, the lock and the
validation are the panel's own -- but it asks in terms of one region at a
time, and it names categories by label rather than by id. That translation is
all this module adds.

Everything takes the `ProjectData` it is handed and reads the table through
it, so a caller holding provider-backed handles (plexora/agent/session.py)
never loads the project the viewer has open. Nothing here writes columns onto
the cells: `cells_in_roi` answers "which cells", and writing the answer into
the table stays `roi.map_to_cells`, an explicit act.
"""

from __future__ import annotations

import uuid

from plexora.plugins.roi.server import geometry as geometry_rules
from plexora.plugins.roi.server import mapping, schema
from plexora.plugins.roi.server.repository import ConflictError, ROIRepository

__all__ = ["ConflictError", "cells_in_roi", "create_roi", "delete_roi", "get_roi",
           "list_rois", "update_roi", "summarize"]


def new_id(prefix="roi"):
    """An id the schema accepts, and that nothing else is likely to have."""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _state(ds):
    return ROIRepository(ds.name).load()


def _features(state, image=schema.DEFAULT_IMAGE):
    return schema.image_entry(state, image)["features"]


def _category_by_id(state):
    return {category["id"]: category for category in state["categories"]}


def summarize(feature, categories, *, max_vertices=None):
    """One region as an agent sees it: what it is, where it is, how big."""
    bounds = geometry_rules.geometry_bounds(feature["geometry"])
    category = categories.get(feature["category_id"]) or {}
    out = {
        "id": feature["id"],
        "name": feature.get("name") or "",
        "category_id": feature["category_id"],
        "category": category.get("label") or "",
        "color": category.get("color") or schema.DEFAULT_COLOR,
        "locked": bool(feature.get("locked")),
        "visible": bool(feature.get("visible", True)),
        "bounds": list(bounds) if bounds else None,
        "vertex_count": geometry_rules.vertex_count(feature["geometry"]),
        "geometry_type": feature["geometry"].get("type"),
        "notes": feature.get("notes") or "",
        "updated_at": feature.get("updated_at"),
        # Which tool drew it (schema.FEATURE_METHODS); None for shapes drawn
        # before that was recorded.
        "method": (feature.get("flags") or {}).get("method"),
        # Which planes of a z-stack it covers; "all" on a flat image.
        "extent": dict(feature.get("extent") or {"kind": "all"}),
        # Its shape through those planes, in brief (the outlines themselves
        # are in the document, and in get_roi's `solid_planes` when asked).
        **({"solid": solid_summary(feature["solid"])} if feature.get("solid") else {}),
    }
    if max_vertices is not None:
        geometry = feature["geometry"]
        if out["vertex_count"] <= max_vertices:
            out["geometry"] = geometry
            out["geometry_truncated"] = False
        else:
            out["geometry"] = None
            out["geometry_truncated"] = True
    return out


def solid_summary(solid):
    """What a region's shape through the planes is, without its outlines."""
    if solid["kind"] == "contours":
        zs = sorted(int(z) for z in solid["planes"])
        return {"kind": "contours", "planes": len(zs), "z0": zs[0], "z1": zs[-1],
                "keys": list(solid.get("keys") or []), "t": solid.get("t", 0)}
    return {"kind": "ellipsoid", "z": solid["z"], "rz": solid["rz"], "t": solid.get("t", 0)}


def footprint_of(planes):
    """The outline covering every plane's outline: what a 3-D region covers
    seen from above, stored as its 2-D geometry (plexora/vision/propagate.py)."""
    from plexora.vision.propagate import footprint

    return geometry_rules.validate_geometry(footprint(planes))


def list_rois(ds):
    """(revision, [summary]) for every region on the project's image."""
    state = _state(ds)
    categories = _category_by_id(state)
    return state["revision"], [summarize(feature, categories) for feature in _features(state)]


def get_roi(ds, roi_id, *, max_vertices=2_000):
    """(revision, summary with geometry) for one region; KeyError if absent."""
    state = _state(ds)
    _, feature = schema.find(_features(state), roi_id)
    if feature is None:
        raise KeyError(f"no ROI {roi_id!r} in {ds.name!r}")
    return state["revision"], summarize(feature, _category_by_id(state),
                                        max_vertices=max_vertices)


def _resolve_category(state, label, color=None):
    """(category_id, ops) for a category named `label`, creating it if needed.

    Matched case-insensitively, the rule `_require_unique_label` enforces: two
    categories whose names differ only in case are two the user cannot tell
    apart, so they are the same one here.
    """
    label = schema.clean_text(label)
    if not label:
        raise ValueError("a region needs a category")
    folded = label.casefold()
    for category in state["categories"]:
        if category["label"].casefold() == folded:
            return category["id"], []
    category_id = new_id("cat")
    return category_id, [{"op": "category.create", "category": {
        "id": category_id, "label": label,
        "color": color or schema.DEFAULT_COLOR}}]


def _polygon(points):
    return {"type": geometry_rules.POLYGON,
            "coordinates": [geometry_rules.close_ring([list(map(float, p)) for p in points])]}


def create_roi(ds, *, category, geometry=None, points=None, name="", notes="",
               color=None, base_revision=None, method=None, extent=None, solid=None):
    """Add one region. Returns (before_revision, after_revision, summary).

    `geometry` is GeoJSON (Polygon/MultiPolygon) in full-resolution image
    pixels, or `points` a list of [x, y] vertices for one ring. `method` is
    the tool that drew it (schema.FEATURE_METHODS), kept as `flags.method`.
    """
    repo = ROIRepository(ds.name)
    state = repo.load()
    base = state["revision"] if base_revision is None else base_revision
    if geometry is None:
        if not points:
            raise ValueError("a region needs geometry or points")
        geometry = _polygon(points)
    geometry = geometry_rules.validate_geometry(geometry)
    category_id, ops = _resolve_category(state, category, color)
    roi_id = new_id()
    feature = {"id": roi_id, "category_id": category_id,
               "name": schema.clean_text(name), "geometry": geometry}
    if notes:
        feature["notes"] = notes
    if method:
        feature["flags"] = schema.normalize_flags({"method": method})
    if extent is not None:
        feature["extent"] = extent
    if solid is not None:
        # schema.normalize_solid; the extent follows from it.
        feature["solid"] = solid
        if solid.get("kind") == "contours":
            feature["geometry"] = footprint_of(schema.normalize_solid(solid)["planes"])
    ops.append({"op": "roi.create", "feature": feature})
    after = repo.apply(base, ops)
    _, created = get_roi(ds, roi_id)
    return base, after, created


def update_roi(ds, roi_id, *, name=None, notes=None, category=None, geometry=None,
               points=None, visible=None, locked=None, base_revision=None, method=None,
               extent=None, solid=None, flatten=False):
    """Change one region. Returns (before_revision, after_revision, before, after)."""
    repo = ROIRepository(ds.name)
    state = repo.load()
    base = state["revision"] if base_revision is None else base_revision
    _, before = get_roi(ds, roi_id)
    ops = []
    changes = {}
    if name is not None:
        changes["name"] = name
    if notes is not None:
        changes["notes"] = notes
    if visible is not None:
        changes["visible"] = bool(visible)
    if locked is not None:
        changes["locked"] = bool(locked)
    if extent is not None:
        changes["extent"] = extent
    if category is not None:
        category_id, category_ops = _resolve_category(state, category)
        ops.extend(category_ops)
        changes["category_id"] = category_id
    if changes:
        ops.append({"op": "roi.update_properties", "id": roi_id, "changes": changes})
    if geometry is None and points:
        geometry = _polygon(points)
    if solid is not None or flatten:
        # The shape through the planes, its footprint and its planes together.
        op = {"op": "roi.set_solid", "id": roi_id, "solid": None if flatten else solid}
        if solid is not None and solid.get("kind") == "contours":
            op["geometry"] = footprint_of(schema.normalize_solid(solid)["planes"])
        elif geometry is not None:
            op["geometry"] = geometry
        if flatten and extent is not None:
            op["extent"] = extent
        ops.append(op)
        geometry = None
    if geometry is not None:
        op = {"op": "roi.update_geometry", "id": roi_id, "geometry": geometry}
        if method:
            flags = dict((before or {}).get("flags") or {})
            flags["method"] = method
            op["flags"] = flags
        ops.append(op)
    if not ops:
        return base, base, before, before
    after_revision = repo.apply(base, ops)
    _, after = get_roi(ds, roi_id)
    return base, after_revision, before, after


def section_at(feature, plane):
    """A region's outline on one plane: a solid's section there (None where it
    does not reach), else its geometry."""
    solid = feature.get("solid")
    if not solid:
        return feature["geometry"]
    for z, geometry in mapping.solid_sections(feature, solid):
        if z == int(plane):
            return geometry
    return None


def carry_through_planes(ds, session, roi_id, *, z=None, view=None, z_range=None,
                         engine="auto", channels=None, base_revision=None):
    """Carry a region's outline on plane `z` up and down the stack and keep the
    outlines as its shape through the planes (`solid: contours`), in one step.

    `z` defaults to the middle of the planes it covers (its own plane for a
    region drawn on one). `view` (full-resolution {x, y, width, height})
    defaults to the outline's box with as much again around it, which is the
    context the model needs to find an edge. Returns (before_revision,
    after_revision, before, after, result) -- `result` is
    plexora/vision/propagate.py's answer without the outlines.
    """
    from plexora.vision import propagate

    repo = ROIRepository(ds.name)
    state = repo.load()
    base = state["revision"] if base_revision is None else base_revision
    _, feature = schema.find(_features(state), roi_id)
    if feature is None:
        raise KeyError(roi_id)
    if z is None:
        bounds = schema.extent_planes(feature.get("extent"))
        if bounds is None:
            raise ValueError("say which plane to carry the outline from (z): "
                             "this region is on every plane")
        z = (bounds[0] + bounds[1]) // 2
    outline = section_at(feature, z)
    if outline is None:
        raise ValueError(f"the region has no outline on plane {z}")
    if view is None:
        from shapely.geometry import shape

        x0, y0, x1, y1 = shape(outline).bounds
        pad = max(32.0, x1 - x0, y1 - y0)
        view = {"x": x0 - pad, "y": y0 - pad, "width": x1 - x0 + 2 * pad,
                "height": y1 - y0 + 2 * pad}
    result = propagate.carry(session, ds.name, z=int(z), view=view, geometry=outline,
                             z_range=z_range, engine=engine, channels=channels)
    planes = result.pop("planes")
    solid = {"kind": "contours", "planes": planes, "keys": [int(z)],
             "t": (feature.get("extent") or {}).get("t", 0)}
    before_rev, after_rev, before, after = update_roi(ds, roi_id, solid=solid,
                                                      base_revision=base)
    return before_rev, after_rev, before, after, result


def delete_roi(ds, roi_id, *, base_revision=None):
    """Remove one region. Returns (before_revision, after_revision, deleted)."""
    repo = ROIRepository(ds.name)
    state = repo.load()
    base = state["revision"] if base_revision is None else base_revision
    _, before = get_roi(ds, roi_id, max_vertices=geometry_rules.MAX_VERTICES)
    after = repo.apply(base, [{"op": "roi.delete", "id": roi_id}])
    return base, after, before


def cells_in_roi(ds, roi_id, *, max_ids=1_000):
    """Which cells' centroids fall inside one region -- counted, not written.

    Returns {roi_id, n_cells, n_total, fraction, cell_ids (bounded),
    truncated}. The containment rule is `mapping.assign`'s: holes are outside,
    and the boundary is strict.
    """
    state = _state(ds)
    _, feature = schema.find(_features(state), roi_id)
    if feature is None:
        raise KeyError(f"no ROI {roi_id!r} in {ds.name!r}")
    if ds.schema is None or not ds.table.available:
        raise LookupError("this project has no cell table")
    x_column, y_column = ds.schema.x, ds.schema.y
    frame = ds.table.geometry()
    missing = [c for c in (x_column, y_column) if not c or c not in frame.columns]
    if missing:
        raise LookupError(f"coordinate column {missing[0]!r} is not in this project's table")
    import numpy as np
    import shapely
    from shapely import geometry as sgeom

    # The predicate `mapping.assign` uses (strict containment, holes outside),
    # asked of one polygon directly rather than through the labelling it
    # produces -- a region's category label is not a membership test.
    polygon = mapping._shapely(feature["geometry"], sgeom)
    inside = np.asarray(shapely.contains_xy(
        polygon, frame[x_column].to_numpy().astype("float64"),
        frame[y_column].to_numpy().astype("float64")), dtype=bool)
    # A region limited to some planes of a z-stack holds only the cells on
    # them -- when the table says where each cell is in z. One with a shape
    # through the planes holds the cells inside its outline on their plane.
    extent = feature.get("extent")
    z_column = ds.schema.get("z")
    z_applied = bool(extent and z_column and z_column in frame.columns)
    if z_applied and feature.get("solid"):
        xs = frame[x_column].to_numpy().astype("float64")
        ys = frame[y_column].to_numpy().astype("float64")
        with np.errstate(invalid="ignore"):
            planes = np.floor(frame[z_column].to_numpy().astype("float64") + 0.5)
        inside = np.zeros(frame.height, dtype=bool)
        for plane, geometry in mapping.solid_sections(feature, feature["solid"]):
            on = planes == plane
            if on.any():
                inside[on] |= np.asarray(shapely.contains_xy(
                    mapping._shapely(geometry, sgeom), xs[on], ys[on]), dtype=bool)
    elif z_applied:
        inside &= mapping.planes_mask(
            [feature], np.zeros(frame.height, dtype=int),
            frame[z_column].to_numpy().astype("float64"))
    # A time series' table: a region drawn on one frame holds that frame's rows.
    t_column = ds.schema.get("t")
    if extent and t_column and t_column in frame.columns:
        inside &= mapping.frames_mask([feature], np.zeros(frame.height, dtype=int),
                                      frame[t_column].to_numpy())
    id_column = ds.schema.cell_id if ds.schema.cell_id in frame.columns else "id"
    ids = frame[id_column].to_numpy()[inside] if id_column in frame.columns else np.empty(0)
    total = int(frame.height)
    count = int(inside.sum())
    return {
        "roi_id": roi_id,
        "n_cells": count,
        "n_total": total,
        "fraction": (count / total) if total else None,
        "cell_ids": [v.item() if hasattr(v, "item") else v for v in ids[:max_ids]],
        "truncated": count > max_ids,
        "id_column": id_column,
        "extent": dict(extent or {"kind": "all"}),
        # A limited region on a table with no z: counted through every plane,
        # and said so rather than passed off as the limited count.
        **({"z_ignored": "the table has no z column, so every plane was counted"}
           if extent and not z_applied else {}),
    }
