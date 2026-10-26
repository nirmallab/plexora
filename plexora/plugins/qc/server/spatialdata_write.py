"""QC into a SpatialData store: the table's rows checked, the regions as shapes.

The table half is `source_write`'s AnnData write (a SpatialData table is an
AnnData group); this adds what only SpatialData has:

- **the table's own annotation** (`uns/spatialdata_attrs`: `region`,
  `region_key`, `instance_key`): the rows written must be the cells of the
  labels element the project's mask is. `instance_key` is checked against the
  loaded table's own ids, row by row, before anything is written; a table that
  annotates another element is reported (`notes`).
- **the regions** as a shapes element, `plexora_qc_regions`: one row per live
  region (id, category, class, action, level, channels, cycles, score,
  threshold, confidence, source, cells, the result and image it belongs to),
  outlines in the reference image's full-resolution pixels mapped into every
  coordinate system the labels element is in -- a scale onto the labels'
  pixel grid (the mask's `scale`) composed with the labels' own
  transformation. Without a labels element, the image element's; without
  either, the identity into "global", and the write says so.

The element is QC's own (`producer` "plexora_qc"): writing it again replaces
this image's rows and keeps every other image's; an element of that name
somebody else wrote is never touched.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

ELEMENT = "plexora_qc_regions"
PRODUCER = "plexora_qc"


def table_attrs(handle, read_elem) -> dict:
    """`uns/spatialdata_attrs` of an open table group, or {}."""
    try:
        if "uns" in handle and "spatialdata_attrs" in handle["uns"]:
            attrs = read_elem(handle["uns"]["spatialdata_attrs"])
            return dict(attrs) if isinstance(attrs, dict) else {}
    except Exception:
        return {}
    return {}


def _element_name(path, kind):
    parts = Path(str(path)).parts
    if kind in parts:
        at = parts.index(kind)
        if at + 1 < len(parts):
            return parts[at + 1]
    return None


def labels_element(dataset):
    """The name of the labels element the project's mask is, or None."""
    seg = getattr(dataset.project, "segmentation", None)
    for path in (getattr(seg, "source", None), getattr(seg, "derived", None)):
        if path and "labels" in Path(str(path)).parts:
            name = _element_name(path, "labels")
            if name:
                return name
    try:
        from plexora.plugins.roi.server import adapters

        folder = Path(adapters._spatialdata_store(dataset)) / "labels"
        names = sorted(p.name for p in folder.iterdir() if adapters._is_group(p)) \
            if folder.is_dir() else []
    except Exception:
        return None
    return names[0] if len(names) == 1 else None


def _raw_ids(dataset):
    """The loaded table's own ids (before any correction), or None."""
    from plexora.server.models import table_correction

    frame = dataset.table.frame()
    spec = dataset.project.dataset
    roles = getattr(spec, "roles", None)
    column = table_correction.raw_cell_id_column(roles, getattr(spec, "correction", None)) \
        if roles is not None else None
    if not column or frame is None or column not in frame.columns:
        return None
    return frame[column].cast(float, strict=False).fill_null(float("nan")).to_numpy()


def check_table(attrs, obs, mask, dataset) -> list:
    """Refuse (ValueError) a table whose `instance_key` does not hold the
    loaded table's ids on the rows about to be written; return notes for
    what is only worth saying."""
    notes = []
    labels = labels_element(dataset)
    region = attrs.get("region")
    regions = [region] if isinstance(region, str) else list(region or [])
    if labels and regions and labels not in regions:
        notes.append(f"the table annotates {', '.join(map(str, regions))}, not the labels "
                     f"element {labels!r} this project's mask is")
    region_key = attrs.get("region_key")
    if labels and region_key and region_key in obs.columns:
        here = obs[region_key].astype(str).to_numpy()[np.asarray(mask)]
        if here.size and not np.all(here == labels):
            notes.append(f"some of this project's rows annotate another element than "
                         f"{labels!r} ({region_key})")
    instance_key = attrs.get("instance_key")
    if instance_key and instance_key in obs.columns:
        ids = _raw_ids(dataset)
        if ids is None:
            notes.append(f"the table's ids could not be compared with {instance_key!r}")
            return notes
        theirs = np.asarray(
            obs[instance_key].astype("float64").to_numpy()[np.asarray(mask)], dtype=float)
        if theirs.size != ids.size or not np.array_equal(
                np.nan_to_num(theirs, nan=-1), np.nan_to_num(ids, nan=-1)):
            raise ValueError(f"the table's {instance_key!r} does not hold this project's cell "
                             "ids in the same rows; nothing was written")
    elif instance_key:
        notes.append(f"the table names instance_key {instance_key!r}, which obs does not have")
    return notes


def _transformations(dataset, store, seg_scale):
    """({coordinate system: transformation} for shapes in reference-image
    pixels, note or None)."""
    import spatialdata
    from spatialdata.transformations import Identity, Scale, Sequence, get_transformation

    labels = labels_element(dataset)
    if labels:
        try:
            sdata = spatialdata.read_zarr(store, selection=("labels",))
            element = sdata.labels.get(labels) if hasattr(sdata.labels, "get") \
                else sdata.labels[labels]
        except Exception:
            element = None
        if element is not None:
            found = get_transformation(element, get_all=True) or {}
            factor = float(seg_scale or 1)
            onto = Scale([1.0 / factor, 1.0 / factor], axes=("x", "y")) if factor != 1 \
                else None
            return ({system: (Sequence([onto, t]) if onto is not None else t)
                     for system, t in found.items()} or {"global": Identity()}), None
    image_name = _element_name(getattr(dataset.project.image, "src", "") or "", "images")
    if image_name:
        try:
            sdata = spatialdata.read_zarr(store, selection=("images",))
            element = sdata.images.get(image_name) if hasattr(sdata.images, "get") \
                else sdata.images[image_name]
            found = get_transformation(element, get_all=True) or {}
            if found:
                return dict(found), None
        except Exception:
            pass
    return {"global": Identity()}, ("no labels or image element was found to place the "
                                    "regions on: they are in image pixels under the identity "
                                    "into 'global'")


def write_regions(dataset, regions, *, image_id, seg_scale=1, replace=True) -> dict:
    """The regions as the `plexora_qc_regions` shapes element ({written,
    element?, coordinate_systems?, notes})."""
    if not regions:
        return {"written": False, "notes": ["no live QC regions: no shapes element written"]}
    try:
        import geopandas as gpd
        import shapely.geometry as sgeom
    except ImportError as exc:  # pragma: no cover - optional at runtime
        return {"written": False, "notes": [f"shapes need {exc.name!r} installed"]}
    from plexora.plugins.roi.server import adapters

    store = adapters._spatialdata_store(dataset)
    rows, geometries = [], []
    for region in regions:
        try:
            geometry = sgeom.shape(region["geometry"]).buffer(0)
        except Exception:
            continue
        if geometry.is_empty:
            continue
        geometries.append(geometry)
        rows.append({
            "roi_id": str(region.get("roi_id") or ""),
            "candidate_id": str(region.get("candidate_id") or ""),
            "image_id": str(image_id), "category": str(region.get("category") or ""),
            "class": str(region.get("class") or ""), "action": str(region.get("action") or ""),
            "level": str(region.get("level") or ""),
            "channels": ";".join(map(str, region.get("channels") or [])),
            "cycles": ";".join(map(str, region.get("cycles") or [])),
            "score": float(region["score"]) if region.get("score") is not None else np.nan,
            "threshold": float(region["threshold"]) if region.get("threshold") is not None
            else np.nan,
            "threshold_source": str(region.get("threshold_source") or ""),
            "severity": str(region.get("severity") or ""),
            "confidence": str(region.get("confidence") or ""),
            "source": str(region.get("source") or ""),
            "created_by": str(region.get("created_by") or ""),
            "n_cells": int(region["n_cells"]) if region.get("n_cells") is not None else -1,
            "producer": PRODUCER})
    if not rows:
        return {"written": False, "notes": ["no region outline could be read"]}
    transformations, note = _transformations(dataset, store, seg_scale)
    frame = gpd.GeoDataFrame(rows, geometry=geometries)
    try:
        written = adapters.write_shapes_element(dataset, frame, ELEMENT, transformations,
                                                replace=True, owned_by=PRODUCER,
                                                image_id=image_id)
    except adapters.ElementExists:
        return {"written": False, "notes": [f"a shapes element {ELEMENT!r} exists that "
                                            "Plexora QC did not write: left as it is"]}
    return {"written": True, "element": written["element"], "n_regions": len(rows),
            "coordinate_systems": sorted(transformations), "notes": [note] if note else []}
