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
               color=None, base_revision=None):
    """Add one region. Returns (before_revision, after_revision, summary).

    `geometry` is GeoJSON (Polygon/MultiPolygon) in full-resolution image
    pixels, or `points` a list of [x, y] vertices for one ring.
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
    ops.append({"op": "roi.create", "feature": feature})
    after = repo.apply(base, ops)
    _, created = get_roi(ds, roi_id)
    return base, after, created


def update_roi(ds, roi_id, *, name=None, notes=None, category=None, geometry=None,
               points=None, visible=None, locked=None, base_revision=None):
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
    if category is not None:
        category_id, category_ops = _resolve_category(state, category)
        ops.extend(category_ops)
        changes["category_id"] = category_id
    if changes:
        ops.append({"op": "roi.update_properties", "id": roi_id, "changes": changes})
    if geometry is None and points:
        geometry = _polygon(points)
    if geometry is not None:
        ops.append({"op": "roi.update_geometry", "id": roi_id, "geometry": geometry})
    if not ops:
        return base, base, before, before
    after_revision = repo.apply(base, ops)
    _, after = get_roi(ds, roi_id)
    return base, after_revision, before, after


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
    }
