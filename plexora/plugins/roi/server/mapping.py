"""Which cells fall inside which regions.

The one place the annotation engine meets the cell table, and it stays a thin
one on purpose: this module answers "for each point, which ROIs contain it" and
turns that into two label columns. It reads no files and writes none -- the
callers in `adapters.py` do that -- so the containment rules can be tested
against hand-built geometry rather than against an .h5ad.

Two rules worth stating, because both are choices and neither is recoverable
from the code at a glance:

**A cell in several ROIs belongs to all of them.** Regions overlap on purpose --
a tumour nest inside a stromal region is not a mistake to be resolved by picking
one -- so the labels are joined with `_` rather than one winning. Names are
joined in document order, which is the order the GeoJSON export writes and the
panel lists, so two exports of the same project agree. Categories are joined the
same way but deduplicated: a cell inside two Tumor regions is `Tumor`, not
`Tumor_Tumor`, because the question the category column answers is "what kind of
place is this cell in".

**A cell in a hole is outside.** Interior rings are honoured, which is the whole
reason this goes through shapely rather than a hand-rolled ray cast: a donut
region drawn around a necrotic core means the core is not in it.
"""

from __future__ import annotations

#: Between the ROIs a cell falls in. Underscore rather than a comma or a pipe
#: because these land in a column that gets read back as a category level, and
#: a separator that survives a CSV round trip without quoting is worth more
#: here than one that reads well in a sentence.
JOINER = "_"

#: What a cell in no region gets. An empty string rather than a word like
#: "None" or "Background": those are labels a user might legitimately give a
#: category, and a value that cannot be told apart from a real answer is worse
#: than a blank. Callers turn this into NA where the format has one.
UNASSIGNED = ""


def assign(features, categories, xs, ys, zs=None, ts=None, cell_ids=None,
           summary=None):
    """(category_label, roi_name) for each point, in point order.

    `features` is the ROI list for one image, in document order; `categories`
    is the project's category list. Points are full-resolution image pixels --
    the same space ROI geometry is stored in, so nothing is transformed here.

    `zs` is each point's plane (the table's z role, in plane units) when the
    project has one. A region limited to some planes of a z-stack
    (`schema.extent_planes`) then holds only the cells whose nearest plane is
    among them, and a region with a shape through the planes
    (`schema.normalize_solid`) holds a cell when the cell lies inside that
    shape's outline on the cell's plane. Without `zs` every region covers every
    plane, as on a flat image, and a solid region is its footprint.

    `ts` is each point's frame (the table's `t` role) for a time series' table
    (one row per cell per frame): a region drawn on one frame (its extent's
    `t`) then holds only that frame's rows. A region with no frame covers
    every frame.

    `cell_ids` is each point's cell id: a region that IS some cells of a 3-D
    mask (a `labels` solid) then holds exactly those cells, by id. Without it
    such a region is its outlines, like contours.

    Returns two lists as long as `xs`. A point in no region gets UNASSIGNED in
    both, never a partial answer in one.

    `summary`, when a dict is passed, is filled with what the assignment found
    (see `_summarize`) -- what the Map to cells preview shows before anything
    is written. An argument rather than a third return value, so the callers
    that only want the labels are unchanged.
    """
    count = len(xs)
    if len(ys) != count:
        raise ValueError("x and y must be the same length")
    names = [UNASSIGNED] * count
    labels = [UNASSIGNED] * count
    if not count or not features:
        if summary is not None:
            summary.update(_summarize(features, categories, {}, count))
        return labels, names

    import numpy as np
    import shapely
    from shapely import geometry as sgeom

    by_id = {c["id"]: c for c in categories}
    # One polygon per region -- or, for a region with a shape through the
    # planes and a table that says where cells are in z, one per plane it
    # covers, tagged with that plane.
    polygons, owner, plane_of = [], [], []
    by_label = {}
    for index, feature in enumerate(features):
        solid = feature.get("solid") if zs is not None else None
        if cell_ids is not None and (feature.get("solid") or {}).get("kind") == "labels":
            by_label[index] = feature["solid"]["ids"]
            continue
        sections = list(solid_sections(feature, solid)) if solid else []
        if not sections:
            polygons.append(_shapely(feature["geometry"], sgeom))
            owner.append(index)
            plane_of.append(np.nan)
        for plane, geometry in sections:
            polygons.append(_shapely(geometry, sgeom))
            owner.append(index)
            plane_of.append(float(plane))
    owner = np.asarray(owner, dtype=int)
    plane_of = np.asarray(plane_of, dtype="float64")
    if not polygons:
        polygons, owner, plane_of = [sgeom.Polygon()], np.array([-1]), np.array([np.nan])

    # One vectorised containment query rather than a Python loop over cells: a
    # real slide is 10^5-10^6 cells against 10^1-10^3 regions, and the loop
    # version of this is minutes. `within` is strict about the boundary, which
    # is the same predicate a point-in-polygon test would give and is not worth
    # softening -- a centroid exactly on a hand-drawn edge is a coin flip
    # whatever rule is picked.
    tree = shapely.STRtree(polygons)
    points = shapely.points(np.asarray(xs, dtype="float64"),
                            np.asarray(ys, dtype="float64"))
    point_index, part_index = tree.query(points, predicate="within")
    polygon_index = owner[part_index]
    keep = polygon_index >= 0
    point_index, part_index, polygon_index = (point_index[keep], part_index[keep],
                                              polygon_index[keep])
    # Regions that are cells of the mask: their rows, by id.
    if by_label:
        ids = np.asarray(cell_ids)
        extra_points, extra_owner = [], []
        for index, labels in by_label.items():
            rows = np.flatnonzero(np.isin(ids, np.asarray(labels)))
            extra_points.append(rows)
            extra_owner.append(np.full(len(rows), index, dtype=int))
        label_points = np.concatenate(extra_points)
        label_owner = np.concatenate(extra_owner)
    else:
        label_points = label_owner = np.empty(0, dtype=int)
    if zs is not None:
        z = np.asarray(zs, dtype="float64")[point_index]
        sectioned = ~np.isnan(plane_of[part_index])
        with np.errstate(invalid="ignore"):
            on_plane = np.floor(z + 0.5) == plane_of[part_index]
        keep = np.where(sectioned, on_plane, planes_mask(features, polygon_index, z))
        point_index, polygon_index = point_index[keep], polygon_index[keep]
    point_index = np.concatenate([point_index, label_points]).astype(int)
    polygon_index = np.concatenate([polygon_index, label_owner]).astype(int)
    if ts is not None:
        keep = frames_mask(features, polygon_index, np.asarray(ts)[point_index])
        point_index, polygon_index = point_index[keep], polygon_index[keep]

    # Sorted by polygon index within each point, so the joined order is
    # document order and not whatever order the index happened to return.
    order = np.lexsort((polygon_index, point_index))
    hits = {}
    for position in order:
        hits.setdefault(int(point_index[position]), []).append(int(polygon_index[position]))

    for row, matched in hits.items():
        matched_names = []
        matched_labels = []
        for index in matched:
            feature = features[index]
            category = by_id.get(feature.get("category_id")) or {}
            matched_names.append(feature.get("name") or "")
            label = category.get("label") or ""
            # Deduplicated in first-seen order. See the module docstring: the
            # category column says what KIND of place a cell is in, and
            # repeating it once per overlapping region says nothing extra.
            if label not in matched_labels:
                matched_labels.append(label)
        names[row] = JOINER.join(matched_names)
        labels[row] = JOINER.join(matched_labels)

    if summary is not None:
        summary.update(_summarize(features, categories, hits, count))
    return labels, names


#: How many regions that caught no cells the preview names. The rest are a
#: count: a project can hold hundreds of regions, and a confirmation that lists
#: them all is one nobody reads to the end.
EMPTY_EXAMPLES = 3


def _summarize(features, categories, hits, count):
    """Totals for one assignment, and nothing per category.

    `hits` maps a row to the regions (feature indices) that hold it. Counts
    only, deliberately: the number of categories is unbounded, so a breakdown
    by category is a list that grows with the project -- the totals answer
    "is this what I meant?" at any size. The empty regions are the exception
    worth naming, a few of them: a region that holds no cell is usually one
    drawn on the wrong plane or frame, and its name is how a user finds it.
    """
    by_id = {c["id"]: c for c in categories}
    used = set()
    for matched in hits.values():
        used.update(matched)
    empty = [features[index].get("name") or "Unnamed"
             for index in range(len(features)) if index not in used]
    used_categories = {features[index].get("category_id") for index in used}
    return {
        "n_cells": int(count),
        "n_assigned": len(hits),
        "n_overlap": sum(1 for matched in hits.values() if len(set(matched)) > 1),
        "n_regions": len(features),
        "n_regions_used": len(used),
        "n_categories": len({f.get("category_id") for f in features
                             if f.get("category_id") in by_id}),
        "n_categories_used": len(used_categories & set(by_id)),
        "n_empty_regions": len(empty),
        "empty_regions": empty[:EMPTY_EXAMPLES],
    }


def solid_sections(feature, solid):
    """(plane, geometry) for every plane a region's solid covers: its outline
    there (contours), or its footprint shrunk about its centre (ellipsoid)."""
    from plexora.plugins.roi.server import schema

    if solid["kind"] in ("contours", "labels"):
        for key, geometry in solid["planes"].items():
            yield int(key), geometry
        return
    from shapely import affinity
    from shapely.geometry import mapping, shape

    footprint = shape(feature["geometry"])
    centre = footprint.centroid
    bounds = schema.extent_planes(schema.solid_extent(solid))
    for plane in range(bounds[0], bounds[1] + 1):
        factor = schema.solid_scale(solid, plane)
        if factor <= 0:
            continue
        yield plane, mapping(affinity.scale(footprint, factor, factor, origin=centre))


def planes_mask(features, feature_index, z):
    """For each (feature, plane) pair, whether the feature covers that plane.

    `z` is a point's plane, possibly fractional (an object's centroid): it
    belongs to its nearest plane. A region covering every plane holds a cell
    whatever its z, a NaN z included; a limited one never holds a NaN.
    """
    import numpy as np

    from plexora.plugins.roi.server import schema

    bounds = [schema.extent_planes(feature.get("extent")) for feature in features]
    low = np.array([b[0] if b else -np.inf for b in bounds], dtype="float64")
    high = np.array([b[1] if b else np.inf for b in bounds], dtype="float64")
    every = np.array([b is None for b in bounds], dtype=bool)
    feature_index = np.asarray(feature_index, dtype=int)
    plane = np.floor(np.asarray(z, dtype="float64") + 0.5)
    with np.errstate(invalid="ignore"):
        inside = (plane >= low[feature_index]) & (plane <= high[feature_index])
    return every[feature_index] | inside


def frames_mask(features, feature_index, t):
    """For each (feature, row) pair, whether the feature is on the row's frame.

    A region drawn on one frame of a time series names it (`extent["t"]`) and
    holds only rows of that frame; one that names none holds every frame's.
    A row with no frame (NaN) is held only by a region on every frame.
    """
    import numpy as np

    frames = []
    for feature in features:
        extent = feature.get("extent") or {}
        value = extent.get("t") if isinstance(extent, dict) else None
        frames.append(np.nan if value is None else float(value))
    frame_of = np.asarray(frames, dtype="float64")
    feature_index = np.asarray(feature_index, dtype=int)
    wanted = frame_of[feature_index]
    with np.errstate(invalid="ignore"):
        same = np.asarray(t, dtype="float64") == wanted
    return np.isnan(wanted) | same


def current_image_id(dataset):
    """The image-id value this project's cells carry, or None.

    None is a real answer and not a failure: "this table covers one image"
    (DataSpec.single_image) and "there is no table at all" both land here, and
    in both cases every row the project can see belongs to this image.

    Deliberately the same rule gating applies in `resolve_current_image_id` --
    the two plugins have to mean the same thing by "which image is this
    project", or the same .h5ad gets gates filed under one name and ROIs under
    another. It is read off the loaded frame rather than the file because the
    frame is already narrowed by the project's registration subset, which is
    exactly the scoping the question needs.

    Raises ValueError when the column holds more than one value within this
    project's own rows. Refusing to guess is the entire point of asking which
    column it is: writing ROI columns against the wrong image is not a
    cosmetic error, it annotates somebody else's cells.
    """
    if dataset.schema is None or not dataset.table.available:
        return None
    column = dataset.schema.image_id
    if not column:
        return None

    # The registration subset first, and without reading anything. For an
    # AnnData or SpatialData project narrowed to one image at import, the value
    # the user picked IS the answer -- and it is the only place the answer
    # reliably survives, because the adapter emits a table of its own columns
    # and need not carry the obs column the subset was taken on.
    source = dataset.table.source
    subset = dict((source.subset if source else None) or {})
    if subset.get("column") == column and subset.get("value") is not None:
        return str(subset["value"])

    # geometry(), not frame(): the image-id column fills a role, so it travels
    # with the ids and coordinates in the copy this server always has -- which
    # matters because this question is asked on the primary even when the table
    # itself is on a node.
    frame = dataset.table.geometry()
    if frame is None or column not in frame.columns:
        return None
    values = [value for value in frame[column].unique().to_list() if value is not None]
    if not values:
        return None
    if len(values) > 1:
        raise ValueError(
            f"column {column!r} holds {len(values)} different image ids within "
            f"this project's own cells, so Plexora cannot tell which image "
            f"these regions belong to"
        )
    return str(values[0])


def _shapely(geometry, sgeom):
    """A stored geometry as a shapely object, holes and all.

    The same conversion `adapters.save_to_spatialdata` does. Kept as its own
    copy rather than imported from there because that module is the file-writing
    one and this module deliberately touches no files -- the tests for each
    should be able to run without the other's dependencies.
    """
    if (geometry or {}).get("type") == "MultiPolygon":
        return sgeom.MultiPolygon(
            [_polygon(part, sgeom) for part in geometry["coordinates"]])
    return _polygon((geometry or {}).get("coordinates") or [], sgeom)


def _polygon(rings, sgeom):
    outer, *holes = rings
    return sgeom.Polygon(outer, holes)
