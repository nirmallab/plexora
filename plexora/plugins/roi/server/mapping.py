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


def assign(features, categories, xs, ys):
    """(category_label, roi_name) for each point, in point order.

    `features` is the ROI list for one image, in document order; `categories`
    is the project's category list. Points are full-resolution image pixels --
    the same space ROI geometry is stored in, so nothing is transformed here.

    Returns two lists as long as `xs`. A point in no region gets UNASSIGNED in
    both, never a partial answer in one.
    """
    count = len(xs)
    if len(ys) != count:
        raise ValueError("x and y must be the same length")
    names = [UNASSIGNED] * count
    labels = [UNASSIGNED] * count
    if not count or not features:
        return labels, names

    import numpy as np
    import shapely
    from shapely import geometry as sgeom

    by_id = {c["id"]: c for c in categories}
    polygons = [_shapely(feature["geometry"], sgeom) for feature in features]

    # One vectorised containment query rather than a Python loop over cells: a
    # real slide is 10^5-10^6 cells against 10^1-10^3 regions, and the loop
    # version of this is minutes. `within` is strict about the boundary, which
    # is the same predicate a point-in-polygon test would give and is not worth
    # softening -- a centroid exactly on a hand-drawn edge is a coin flip
    # whatever rule is picked.
    tree = shapely.STRtree(polygons)
    points = shapely.points(np.asarray(xs, dtype="float64"),
                            np.asarray(ys, dtype="float64"))
    point_index, polygon_index = tree.query(points, predicate="within")

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

    return labels, names


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

    frame = dataset.table.frame()
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
