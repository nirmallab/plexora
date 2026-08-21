"""One edit, applied to the annotation state.

Edits arrive as a list of named operations rather than as a replacement
document. That is not an optimization -- the store writes the whole blob either
way today -- it is what makes the write meaningful. "Set the state to this"
cannot tell a deliberate deletion from a client that lost half its state and
saved anyway, and it cannot be undone, replayed, or reasoned about after the
fact. "Delete roi_7" can.

Every operation is applied to a copy and the batch is atomic: an invalid
operation anywhere leaves the stored state exactly as it was, rather than half
of a user's action landing.

The rules that live here rather than on the client, because the client's copy is
advice and this one is the rule:

* a locked ROI (or one in a locked category) cannot have its geometry changed or
  be deleted -- but CAN be renamed and recategorized, because the lock is on the
  shape, not on what it means;
* two categories cannot share a label, since the label is how the user tells
  them apart;
* every category is deletable and renameable, including the last one -- there is
  no reserved catch-all, so no row of the list behaves differently from its
  neighbours;
* deleting a category with shapes in it requires saying what happens to them,
  and `reassign` requires naming the category they move to. There is no default
  for either: silently deleting a user's regions because they tidied up a label
  is the one outcome nobody wants, and silently keeping them under a category
  that no longer exists is how you get shapes that cannot be drawn.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from plexora.plugins.roi.server import schema
from plexora.plugins.roi.server.geometry import validate_geometry

#: How many operations one request may carry. An import arrives as a single
#: bulk operation rather than N creates, so a batch this long is a client
#: misbehaving.
MAX_OPERATIONS = 5_000


def apply_operations(state, operations):
    """Apply every operation in order, returning a new state.

    Raises ValueError -- with a message meant for the user -- if any of them is
    invalid, having changed nothing.
    """
    if not isinstance(operations, list):
        raise ValueError("operations must be a list")
    if len(operations) > MAX_OPERATIONS:
        raise ValueError(f"too many operations in one request (max {MAX_OPERATIONS})")

    working = copy.deepcopy(state)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("each operation must be an object")
        name = operation.get("op")
        handler = _HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown operation {name!r}")
        handler(working, operation)
    return working


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# -- categories ---------------------------------------------------------


def _category_create(state, op):
    raw = op.get("category")
    if not isinstance(raw, dict):
        raise ValueError("category.create needs a category")
    category = schema.normalize_category(raw)

    _, existing = schema.find(state["categories"], category["id"])
    if existing is not None:
        raise ValueError(f"category {category['id']!r} already exists")
    if len(state["categories"]) >= schema.MAX_CATEGORIES:
        raise ValueError(f"a project may hold at most {schema.MAX_CATEGORIES} categories")
    _require_unique_label(state, category["label"], category["id"])

    state["categories"].append(category)


def _category_update(state, op):
    category = _category(state, op.get("id"))
    changes = op.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("category.update needs changes")

    if "label" in changes:
        label = schema.clean_text(changes["label"])
        if not label:
            raise ValueError("a category needs a name")
        _require_unique_label(state, label, category["id"])
        category["label"] = label
    if "color" in changes:
        category["color"] = schema.color(changes["color"])
    if "visible" in changes:
        category["visible"] = bool(changes["visible"])
    if "locked" in changes:
        category["locked"] = bool(changes["locked"])
    if "sort_order" in changes:
        category["sort_order"] = schema.as_int(changes["sort_order"], category["sort_order"])


def _category_delete(state, op):
    category = _category(state, op.get("id"))

    orphans = op.get("orphans")
    if orphans not in ("delete", "reassign"):
        raise ValueError("category.delete needs orphans='delete' or orphans='reassign'")

    if orphans == "reassign":
        # Named explicitly, because there is no longer a catch-all to default
        # to. A client that wants the shapes kept has to say where they go.
        target_id = op.get("reassign_to")
        if not target_id:
            raise ValueError("category.delete with orphans='reassign' needs reassign_to")
        target = _category(state, target_id)
        if target["id"] == category["id"]:
            raise ValueError("cannot reassign a category's ROIs to itself")

    for entry in state["images"].values():
        if orphans == "delete":
            entry["features"] = [f for f in entry["features"]
                                 if f["category_id"] != category["id"]]
        else:
            for feature in entry["features"]:
                if feature["category_id"] == category["id"]:
                    feature["category_id"] = target["id"]
                    feature["updated_at"] = now()

    index, _ = schema.find(state["categories"], category["id"])
    state["categories"].pop(index)


# -- ROIs ---------------------------------------------------------------


def _roi_create(state, op):
    entry = _image(state, op)
    raw = op.get("feature")
    if not isinstance(raw, dict):
        raise ValueError("roi.create needs a feature")

    feature = schema.normalize_feature(raw)
    _, existing = schema.find(entry["features"], feature["id"])
    if existing is not None:
        raise ValueError(f"ROI {feature['id']!r} already exists")
    if len(entry["features"]) >= schema.MAX_FEATURES:
        raise ValueError(f"an image may hold at most {schema.MAX_FEATURES} ROIs")
    _category(state, feature["category_id"])

    stamp = now()
    feature["created_at"] = feature["created_at"] or stamp
    feature["updated_at"] = stamp
    entry["features"].append(feature)


def _roi_update_geometry(state, op):
    entry = _image(state, op)
    feature = _feature(entry, op.get("id"))
    _require_unlocked(state, feature, "move or reshape")

    feature["geometry"] = validate_geometry(op.get("geometry"))
    if "flags" in op:
        feature["flags"] = schema.normalize_flags(op.get("flags"))
    feature["updated_at"] = now()


def _roi_update_properties(state, op):
    entry = _image(state, op)
    feature = _feature(entry, op.get("id"))
    changes = op.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("roi.update_properties needs changes")

    # Deliberately not gated on the lock: a locked shape can still be renamed
    # and reclassified. The lock protects the geometry from an accidental drag,
    # which is a different thing from freezing what the region means.
    if "name" in changes:
        feature["name"] = schema.clean_text(changes["name"])
    if "notes" in changes:
        feature["notes"] = schema.clean_text(changes["notes"], schema.MAX_NOTE_LENGTH)
    if "category_id" in changes:
        category = _category(state, changes["category_id"])
        feature["category_id"] = category["id"]
    if "locked" in changes:
        feature["locked"] = bool(changes["locked"])
    feature["updated_at"] = now()


def _roi_delete(state, op):
    entry = _image(state, op)
    feature = _feature(entry, op.get("id"))
    _require_unlocked(state, feature, "delete")
    index, _ = schema.find(entry["features"], feature["id"])
    entry["features"].pop(index)


def _roi_bulk_delete(state, op):
    entry = _image(state, op)
    ids = op.get("ids")
    if not isinstance(ids, list):
        raise ValueError("roi.bulk_delete needs ids")
    wanted = set()
    for roi_id in ids:
        feature = _feature(entry, roi_id)
        _require_unlocked(state, feature, "delete")
        wanted.add(feature["id"])
    entry["features"] = [f for f in entry["features"] if f["id"] not in wanted]


def _roi_bulk_create(state, op):
    """Many ROIs, and the categories they need, as one operation.

    This is what an import is. Written as one operation rather than N so that
    undoing it is one step: a user who imports 432 regions and changes their
    mind wants one Ctrl+Z, not 432.
    """
    for raw in op.get("categories") or []:
        category = schema.normalize_category(raw)
        _, existing = schema.find(state["categories"], category["id"])
        if existing is None:
            _require_unique_label(state, category["label"], category["id"])
            state["categories"].append(category)

    for raw in op.get("features") or []:
        _roi_create(state, {**op, "feature": raw})


# -- lookups and rules --------------------------------------------------


def _image(state, op):
    key = op.get("image") or schema.DEFAULT_IMAGE
    return schema.image_entry(state, key)


def _category(state, category_id):
    _, category = schema.find(state["categories"], category_id)
    if category is None:
        raise ValueError(f"unknown category {category_id!r}")
    return category


def _feature(entry, roi_id):
    _, feature = schema.find(entry["features"], roi_id)
    if feature is None:
        raise ValueError(f"unknown ROI {roi_id!r}")
    return feature


def _require_unlocked(state, feature, action):
    if feature["locked"]:
        raise ValueError(f"ROI {feature['id']!r} is locked and cannot be {action}d")
    _, category = schema.find(state["categories"], feature["category_id"])
    if category is not None and category["locked"]:
        raise ValueError(
            f"category {category['label']!r} is locked, so its ROIs cannot be {action}d"
        )


def _require_unique_label(state, label, own_id):
    """Two categories with the same name are two categories the user cannot
    tell apart -- in the list, in the dropdown, and in every export."""
    folded = label.casefold()
    for category in state["categories"]:
        if category["id"] != own_id and category["label"].casefold() == folded:
            raise ValueError(f"a category named {label!r} already exists")


_HANDLERS = {
    "category.create": _category_create,
    "category.update": _category_update,
    "category.delete": _category_delete,
    "roi.create": _roi_create,
    "roi.update_geometry": _roi_update_geometry,
    "roi.update_properties": _roi_update_properties,
    "roi.delete": _roi_delete,
    "roi.bulk_delete": _roi_bulk_delete,
    "roi.bulk_create": _roi_bulk_create,
}

OPERATION_NAMES = tuple(sorted(_HANDLERS))
