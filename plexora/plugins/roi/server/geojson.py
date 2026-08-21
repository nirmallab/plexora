"""Annotations as a file somebody else can open.

GeoJSON, because polygons are polygons: Shapely, GeoPandas, QGIS, SpatialData's
own shapes reader and every JavaScript geometry library already read it, and the
alternative is inventing a polygon format that only Plexora can open.

The one honest caveat, stated in the file itself rather than assumed: GeoJSON is
a GEOGRAPHIC format. Its positions are longitude/latitude on the WGS-84 datum
unless a document says otherwise, and these are image pixels with y increasing
downward -- which is not a coordinate reference system at all. So the export
carries an explicit `coordinate_space` and never claims a CRS. A reader that
treats these as degrees gets nonsense either way; one that reads the metadata
gets the truth.

Plexora-specific material lives under a `plexora` foreign member. Foreign
members are the part of the GeoJSON spec meant for exactly this: a strict reader
ignores it and still gets valid geometry, and Plexora gets its categories back.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from plexora.plugins.roi.server import schema
from plexora.plugins.roi.server.geometry import validate_geometry

MIME_TYPE = "application/geo+json"

#: Features in one imported document. An import is parsed, validated and applied
#: in one request, so this is the ceiling on how long that request can take.
MAX_IMPORT_FEATURES = 50_000

#: Where an imported feature carrying no category information at all is filed.
#: It has to go somewhere -- dropping the shape because some intermediate tool
#: stripped its properties would lose real annotation -- and since a project no
#: longer has a reserved catch-all, one named for how these arrived is at least
#: honest about what it holds. Created only if such a feature actually turns up.
IMPORTED_LABEL = "Imported"


def export_document(state, datasource, plugin_version, image_key=schema.DEFAULT_IMAGE,
                    image_id=None):
    """A FeatureCollection holding everything needed to reconstruct the project.

    Category metadata is written twice on purpose: once in the foreign member
    (so a Plexora import restores the categories themselves -- colours, order,
    visibility) and once flattened onto each feature's properties (so a reader
    that ignores foreign members still knows what each region is). The
    duplication is a few bytes and the alternative is an export that is either
    lossy for Plexora or opaque to everyone else.

    `image_id` is the value this project's cells carry in their image-id column,
    and it is written for the same reason and in both of the same places. An
    AnnData can hold cells from a dozen slides; a file of polygons that does not
    say which one it was drawn on is a file whose regions can be applied to the
    wrong slide without anything looking wrong. It is passed in rather than read
    from `state` because it is a fact about the project record, which can change
    after these shapes were drawn -- resolving it at export time is what keeps a
    stored copy from going quietly stale. None is a real answer: a single-image
    project has no such column, and the key is then absent rather than null.
    """
    entry = state["images"].get(image_key) or schema.empty_image()
    categories = {c["id"]: c for c in state["categories"]}

    features = []
    for feature in entry["features"]:
        category = categories.get(feature["category_id"]) or schema.placeholder_category()
        features.append({
            "type": "Feature",
            "id": feature["id"],
            "geometry": feature["geometry"],
            "properties": {
                "name": feature.get("name") or "",
                "category_id": feature["category_id"],
                "category": category["label"],
                "category_color": category["color"],
                "locked": feature.get("locked", False),
                "created_at": feature.get("created_at"),
                "updated_at": feature.get("updated_at"),
                "source_roi_id": feature.get("source_roi_id"),
                # Per feature as well as per document, so concatenating two
                # projects' exports into one collection -- which is what anyone
                # comparing slides does -- keeps each shape bound to its image.
                **({"image_id": image_id} if image_id is not None else {}),
                **({"notes": feature["notes"]} if feature.get("notes") else {}),
            },
        })

    return {
        "type": "FeatureCollection",
        "plexora": {
            "schema_version": schema.SCHEMA_VERSION,
            "plugin_version": plugin_version,
            "datasource": datasource,
            **({"image_id": image_id} if image_id is not None else {}),
            "exported_at": _now(),
            "coordinate_space": entry["coordinate_space"],
            "categories": state["categories"],
        },
        "features": features,
    }


def validate_document(document, image_size=None):
    """Check an incoming document, returning what is wrong with it.

    Returns (errors, warnings). Errors mean it cannot be imported at all;
    warnings mean it can, but the user should be asked first -- which today is
    exactly one thing, and the important one.
    """
    errors, warnings = [], {}

    if not isinstance(document, dict):
        return ["that file does not contain a GeoJSON object"], warnings
    if document.get("type") != "FeatureCollection":
        errors.append("expected a GeoJSON FeatureCollection")

    meta = document.get("plexora")
    if not isinstance(meta, dict):
        # v1 imports Plexora's own exports only. A bare GeoJSON from elsewhere
        # is readable geometry in an unknown coordinate space -- pixels of some
        # image, microns, or degrees -- and importing it would mean guessing
        # which. That guess is the one this whole module exists to avoid.
        errors.append(
            "this file was not exported by Plexora (no 'plexora' metadata), so "
            "there is no way to tell what its coordinates mean"
        )
    else:
        version = meta.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            errors.append("the file's Plexora metadata has no schema version")
        elif version > schema.SCHEMA_VERSION:
            errors.append(
                f"the file was written by a newer version of Plexora "
                f"(schema {version}, this build reads {schema.SCHEMA_VERSION})"
            )

    features = document.get("features")
    if not isinstance(features, list):
        errors.append("the file has no features list")
    elif len(features) > MAX_IMPORT_FEATURES:
        errors.append(f"the file holds more than {MAX_IMPORT_FEATURES} regions")

    if errors:
        return errors, warnings

    stored = (meta.get("coordinate_space") or {}) if isinstance(meta, dict) else {}
    stored_size = (stored.get("width"), stored.get("height"))
    if image_size and all(image_size) and all(stored_size) and tuple(image_size) != stored_size:
        # Not an error: importing anyway is a legitimate thing to want when the
        # two images really are the same field of view at different scales. It
        # is just never the thing to do by default, because the geometry will
        # land somewhere plausible and wrong.
        warnings["dimension_mismatch"] = {
            "found": list(stored_size),
            "expected": list(image_size),
        }

    return errors, warnings


def import_features(state, document, image_key=schema.DEFAULT_IMAGE):
    """Add a document's regions to `state`, returning (new_state, report).

    Nothing is ever overwritten. Every imported region is given a fresh id with
    its original kept in `source_roi_id`, because an id collision here is not a
    conflict to resolve -- the two regions are simply different regions that
    happen to have been numbered the same in two projects, and picking one would
    destroy the other. Importing the same file twice therefore duplicates its
    regions, which is visible and undoable; the alternative silently is not.

    Categories are matched first by id (a Plexora export carries the same ids)
    and then by label, so importing Tumor into a project that already has one
    lands in the existing category rather than creating "Tumor" twice.
    """
    by_id = {c["id"]: c for c in state["categories"]}
    by_label = {c["label"].casefold(): c for c in state["categories"]}

    new_categories, new_features = [], []
    remap = {}

    meta = document.get("plexora") or {}
    for raw in meta.get("categories") or []:
        if not isinstance(raw, dict):
            continue
        category = schema.normalize_category(raw)
        match = by_id.get(category["id"]) or by_label.get(category["label"].casefold())
        if match is not None:
            remap[category["id"]] = match["id"]
            continue
        new_categories.append(category)
        by_id[category["id"]] = category
        by_label[category["label"].casefold()] = category
        remap[category["id"]] = category["id"]

    for raw in document.get("features") or []:
        if not isinstance(raw, dict):
            raise ValueError("every entry in 'features' must be an object")
        properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}

        category_id = _resolve_category(properties, remap, by_id, by_label, new_categories)
        new_features.append({
            "id": new_id("r"),
            "category_id": category_id,
            "name": schema.clean_text(properties.get("name")),
            "locked": bool(properties.get("locked", False)),
            "geometry": validate_geometry(raw.get("geometry")),
            "flags": schema.normalize_flags(raw.get("flags")),
            # The id it had where it came from. Kept because it is the only
            # thread back to the source project once ids are regenerated, and
            # somebody reconciling two exports will want it.
            "source_roi_id": schema.clean_text(raw.get("id")) or None,
            "notes": schema.clean_text(properties.get("notes"), schema.MAX_NOTE_LENGTH),
        })

    return {
        "op": "roi.bulk_create",
        "image": image_key,
        "categories": new_categories,
        "features": [f for f in new_features],
    }, {
        "imported": len(new_features),
        "created_categories": len(new_categories),
    }


def _resolve_category(properties, remap, by_id, by_label, new_categories):
    """Which category an imported feature belongs in.

    Falls back through: the id the export used, then the label it printed, then
    IMPORTED_LABEL. The label fallback is what makes a feature survive a
    document whose foreign member was stripped by some intermediate tool, and
    the last step is what makes it survive losing its properties entirely.

    Every path ends at a category that exists or is created here, since there is
    no reserved one to point at.
    """
    source_id = properties.get("category_id")
    if isinstance(source_id, str) and source_id in remap:
        return remap[source_id]
    if isinstance(source_id, str) and source_id in by_id:
        return source_id

    label = schema.clean_text(properties.get("category")) or IMPORTED_LABEL
    match = by_label.get(label.casefold())
    if match is not None:
        return match["id"]

    category = schema.normalize_category({
        "id": new_id("c"),
        "label": label,
        "color": properties.get("category_color"),
        "sort_order": len(by_id),
    })
    new_categories.append(category)
    by_id[category["id"]] = category
    by_label[label.casefold()] = category
    return category["id"]


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4()}"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
