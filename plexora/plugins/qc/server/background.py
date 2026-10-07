"""The Background ROI: the glass outside the feathered tissue, as one region.

An annotation, never a finding. Its cells are NOTED `background` (kept, and
recorded in `noted_by` and the `plexora_qc_background` export column), never
excluded: renaming the ROI "QC exclude: Background", or `approve_qc_roi`
with `action="exclude"`, is how a user removes them -- nothing does it
automatically. Every export keeps every input row either way.

The outline is the complement of the feathered tissue mask (`tissue.py`) at
the overview level -- the tissue a hole in it -- simplified to the same
vertex budget as a consolidated layer, specks of glass smaller than
`schemas.TISSUE["background_min_area_um2"]` dropped. It is written through
`region_write` like a check's regions: written again, it replaces its own
earlier ROI unless the user edited, locked, renamed or moved it.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.qc.server import schemas

#: Bumped when the background's outline means something else.
VERSION = "1"
DETECTOR = "tissue"
KEY = "background"


def geometry_of_mask(feathered, factor, *, pixel_um, image_size=None):
    """GeoJSON (full-resolution px) of the glass outside `feathered` (an
    overview-level tissue mask, `factor` full-resolution px per overview
    px), the tissue a hole in it; None when there is no glass worth a
    region."""
    import shapely

    from plexora.plugins.qc.server import consolidate, polygons
    from plexora.server.utils import mask_polygon

    glass = ~np.asarray(feathered, dtype=bool)
    if not glass.any():
        return None
    shape = mask_polygon.mask_to_shape(glass, (0, 0), float(factor))
    if shape is None:
        return None
    if image_size is not None:
        width, height = image_size
        shape = shapely.intersection(shape, shapely.box(0, 0, float(width), float(height)))
    rules = schemas.TISSUE
    min_area_px = rules["background_min_area_um2"] / (float(pixel_um) ** 2) if pixel_um \
        else (2.0 * rules["feather_px"]) ** 2
    return polygons.to_geojson(shape, simplify_px=0.5 * float(factor),
                               max_vertices=consolidate.LAYER_MAX_VERTICES,
                               min_area_px=min_area_px)


def candidate_record(geometry, tissue, *, fingerprint=None):
    """The background as a result candidate (action pinned `note`)."""
    from plexora.plugins.qc.server import region_write
    from shapely.geometry import shape as to_shape

    area = float(to_shape(geometry).area)
    pixel_um = tissue.get("pixel_um")
    return region_write.check_candidate(
        DETECTOR, VERSION, schemas.BACKGROUND_CLASS, f"{fingerprint or 'tissue'}:{KEY}",
        {"geometry": geometry, "max": None}, channels=[], scope="all_channels",
        threshold={"threshold": None, "threshold_source": "auto"}, action="note",
        trace="map",
        extra_metrics={"tissue_method": tissue.get("method"),
                       "feather_um": schemas.TISSUE["feather_um"] if pixel_um else None,
                       "feather_px": None if pixel_um else schemas.TISSUE["feather_px"],
                       "area_px2": area,
                       "area_um2": area * pixel_um * pixel_um if pixel_um else None,
                       "source": tissue.get("source"), "fingerprint": fingerprint})


def plan(session, project, *, scan=None) -> dict:
    """{geometry, tissue} of the background, or {geometry: None, reason}."""
    from plexora.plugins.qc.server import tissue as tissue_rules
    from plexora.server.utils import source_image

    found = tissue_rules.for_project(session, project, scan_result=scan)
    if found.get("method") == "all_pixels":
        return {"geometry": None, "tissue": found,
                "reason": "no tissue could be told from the glass (every pixel was "
                          "counted as tissue): no background written"}
    image_size = None
    try:
        with source_image.SHELF.reader(session.image_data(project)) as source:
            height, width = source.level_shape(0)
            image_size = (width, height)
    except Exception:  # noqa: BLE001 -- the outline is clipped only when the size is known
        image_size = None
    geometry = geometry_of_mask(found["feathered"], found["factor"],
                                pixel_um=found.get("pixel_um"), image_size=image_size)
    if geometry is None:
        return {"geometry": None, "tissue": found,
                "reason": "the tissue fills the image: there is no glass to annotate"}
    return {"geometry": geometry, "tissue": found}


def write(call, project, *, scan=None, result_id=None, cells=True, session_id=None) -> dict:
    """Write (or rewrite) the Background ROI. Returns {written, kept,
    removed, receipts, revisions, reason?}; nothing is written when there is
    no glass or no tissue could be found (`reason` says which)."""
    from plexora.plugins.qc.server import region_write

    planned = plan(call.session, project, scan=scan)
    out = {"written": [], "kept": [], "removed": [], "receipts": [], "revisions": None,
           "tissue_method": planned["tissue"].get("method")}
    if planned["geometry"] is None:
        out["reason"] = planned["reason"]
        return out
    owned = users_background(call.session, project)
    if owned:
        # Deleting, reshaping or renaming it was the user's call: one
        # background, theirs, and nothing written beside it.
        out["kept"] = owned
        out["reason"] = "the Background ROI is the user's (edited, locked, renamed or " \
                        "moved): kept as it is"
        return out
    record = candidate_record(planned["geometry"], planned["tissue"],
                              fingerprint=planned["tissue"].get("fingerprint"))
    written, kept, removed, receipts, _per_key, revisions = region_write.write_regions(
        call, project, detector=DETECTOR, klass=schemas.BACKGROUND_CLASS, covered={KEY},
        work=[(KEY, [record])], row_keys=lambda row: [KEY], pinned="note",
        result_id=result_id, cells=cells, session_id=session_id)
    out.update(written=written, kept=kept, removed=[r["roi_id"] for r in removed],
               receipts=receipts, revisions=revisions)
    return out


def users_background(session, project) -> list:
    """The ids of background ROIs QC wrote that the user has made theirs
    (`region_write.replaceable` says no): a rewrite leaves them alone."""
    from plexora.plugins.qc.server import region_write, results, roi_link

    meta = results.roi_meta(project)
    if not meta.height:
        return []
    try:
        state = roi_link._repo(session.image_data(project)).load()
    except Exception:  # noqa: BLE001 -- no ROI document: nothing of the user's
        return []
    features = {f["id"]: f for f in roi_link._features(state)}
    from plexora.plugins.qc.server import polygons

    out = []
    for row in meta.to_dicts():
        feature = features.get(row["roi_id"])
        if row.get("detector") != DETECTOR or row.get("deleted") or feature is None:
            continue
        # What `roi_link.sync` would record, read straight off the document
        # (a sync may not have run since the user's edit).
        named = schemas.action_of_name(feature.get("name"))
        theirs = polygons.geometry_hash(feature["geometry"]) != row.get("written_geometry_hash") \
            or feature.get("category_id") != row.get("written_category_id") \
            or (named is not None and named != "note")
        if theirs or not region_write.replaceable(row, feature, schemas.BACKGROUND_CLASS,
                                                  "note"):
            out.append(row["roi_id"])
    return out
