"""One edit, applied to a figure document.

Edits arrive as a list of named operations rather than as a replacement
document, for the reason ROI's do: "set the figure to this" cannot tell a
deliberate deletion from a client that lost half its state and autosaved
anyway, and it cannot be undone, replayed or reasoned about afterwards.
"remove pnl_7" can.

**One `apply` call is one undo step.** That is the contract the client's history
is built on, and it is why the batch operations exist at all: dragging a
five-panel selection is one `move_panels`, and Split Composite is one request
carrying `add_panel` x N plus `link_panels` -- so undoing a split is one
keystroke rather than five.

Every operation is applied to a copy and the batch is atomic: an invalid
operation anywhere leaves the stored document exactly as it was, rather than
half of a user's action landing.

The rules that live here rather than on the client, because the client's copy is
advice and this one is the rule:

* nothing is ever silently orphaned. Deleting a page says what happens to the
  panels on it; deleting a source says what happens to the panels that
  reference it. There is no default for either, because both possible defaults
  are wrong -- destroying captured scenes because somebody tidied up a page, or
  keeping panels that point at nothing and cannot be drawn;
* a panel must name a source that exists, and a placement must name a page that
  exists;
* a link group needs at least two panels, since a group of one is a group whose
  synchronisation has nothing to synchronise with.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from plexora.plugins.figure_builder.server import schema

#: How many operations one request may carry. Split Composite of a 40-channel
#: panel is ~41; an import of a saved ROI category is one per region. A batch
#: past this is a client misbehaving, not a user working.
MAX_OPERATIONS = 2_000


def apply_operations(document, operations):
    """Apply every operation in order, returning a new document.

    Raises ValueError -- with a message meant for the user -- if any of them is
    invalid, having changed nothing.
    """
    if not isinstance(operations, list):
        raise ValueError("operations must be a list")
    if not operations:
        raise ValueError("no operations were sent")
    if len(operations) > MAX_OPERATIONS:
        raise ValueError(f"too many operations in one request (max {MAX_OPERATIONS})")

    working = copy.deepcopy(document)
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


# -- document ------------------------------------------------------------


def _set_meta(document, op):
    """Title and document-level settings.

    Both on one operation because both are "the figure as a whole", and a
    separate `set_title` would be a second way to do the same thing.
    """
    changes = op.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("set_meta needs changes")
    if "title" in changes:
        title = schema.clean_text(changes["title"])
        if not title:
            raise ValueError("a figure needs a title")
        document["title"] = title
    if "settings" in changes:
        merged = {**document["settings"], **(changes["settings"] or {})}
        if isinstance(changes.get("settings"), dict) and isinstance(changes["settings"].get("style"), dict):
            merged["style"] = {**document["settings"]["style"], **changes["settings"]["style"]}
        document["settings"] = schema.normalize_settings(merged)


# -- pages ---------------------------------------------------------------


def _add_page(document, op):
    page = schema.normalize_page(op.get("page") if isinstance(op.get("page"), dict) else {})
    if _page_index(document, page["page_id"]) >= 0:
        raise ValueError(f"page {page['page_id']!r} already exists")
    if len(document["pages"]) >= schema.MAX_PAGES:
        raise ValueError(f"a figure may hold at most {schema.MAX_PAGES} pages")
    index = op.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index <= len(document["pages"]):
        document["pages"].insert(index, page)
    else:
        document["pages"].append(page)


def _update_page(document, op):
    index = _page_index(document, op.get("page_id"))
    if index < 0:
        raise ValueError(f"unknown page {op.get('page_id')!r}")
    changes = op.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("update_page needs changes")
    merged = {**document["pages"][index], **changes, "page_id": document["pages"][index]["page_id"]}
    document["pages"][index] = schema.normalize_page(merged)


def _remove_page(document, op):
    """Delete a page, having been told what happens to what is on it.

    `panels` is "delete" or "tray": destroy the captured scenes, or keep them
    and unplace them. No default -- a page is deleted by one click and a
    captured field may be the only record of a view somebody spent an hour
    finding.
    """
    index = _page_index(document, op.get("page_id"))
    if index < 0:
        raise ValueError(f"unknown page {op.get('page_id')!r}")
    if len(document["pages"]) <= 1:
        raise ValueError("a figure needs at least one page")

    disposition = op.get("panels")
    if disposition not in ("delete", "tray"):
        raise ValueError("remove_page needs panels='delete' or panels='tray'")

    page_id = document["pages"][index]["page_id"]
    doomed = [pid for pid, panel in document["panels"].items()
              if panel["placement"] and panel["placement"]["page_id"] == page_id]
    if disposition == "delete":
        for panel_id in doomed:
            _detach_panel(document, panel_id)
            document["panels"].pop(panel_id, None)
    else:
        for panel_id in doomed:
            document["panels"][panel_id]["placement"] = None
            document["panels"][panel_id]["updated_at"] = now()

    # Annotations belong to the page, not to the figure -- an arrow pointing at
    # a panel that is gone has nothing to point at, so it goes with the page.
    for annotation_id in [aid for aid, a in document["annotations"].items()
                          if a["page_id"] == page_id]:
        document["annotations"].pop(annotation_id, None)

    document["pages"].pop(index)


def _reorder_pages(document, op):
    order = op.get("page_ids")
    if not isinstance(order, list):
        raise ValueError("reorder_pages needs page_ids")
    existing = {page["page_id"]: page for page in document["pages"]}
    if sorted(order) != sorted(existing):
        raise ValueError("reorder_pages must list every page exactly once")
    document["pages"] = [existing[page_id] for page_id in order]


# -- sources -------------------------------------------------------------


def _add_source(document, op):
    source = schema.normalize_source(op.get("source") if isinstance(op.get("source"), dict) else {})
    if source["source_id"] in document["sources"]:
        raise ValueError(f"source {source['source_id']!r} already exists")
    if len(document["sources"]) >= schema.MAX_SOURCES:
        raise ValueError(f"a figure may reference at most {schema.MAX_SOURCES} sources")
    if source["kind"] == "plexora_project" and not source["datasource"]:
        raise ValueError("a project source needs a datasource")
    if source["kind"] == "imported_asset" and not source["asset_id"]:
        raise ValueError("an imported source needs an asset_id")
    document["sources"][source["source_id"]] = source


def _update_source(document, op):
    source_id = op.get("source_id")
    current = document["sources"].get(source_id)
    if current is None:
        raise ValueError(f"unknown source {source_id!r}")
    changes = op.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("update_source needs changes")
    merged = {**current, **changes, "source_id": current["source_id"], "kind": current["kind"]}
    document["sources"][source_id] = schema.normalize_source(merged)


def _remove_source(document, op):
    """Stop referencing an image, having been told what happens to its panels.

    `panels` is "delete" or "keep". "keep" leaves them in place as
    cached-preview-only: they still draw, they still lay out, they simply
    cannot be re-edited or re-rendered until the source is relinked. That is a
    real answer, and often the right one for a figure that is finished.
    """
    source_id = op.get("source_id")
    if source_id not in document["sources"]:
        raise ValueError(f"unknown source {source_id!r}")
    disposition = op.get("panels")
    if disposition not in ("delete", "keep"):
        raise ValueError("remove_source needs panels='delete' or panels='keep'")

    if disposition == "delete":
        for panel_id in [pid for pid, p in document["panels"].items()
                         if p["source_id"] == source_id]:
            _detach_panel(document, panel_id)
            document["panels"].pop(panel_id, None)
    document["sources"].pop(source_id)


# -- panels --------------------------------------------------------------


def _add_panel(document, op):
    raw = op.get("panel")
    if not isinstance(raw, dict):
        raise ValueError("add_panel needs a panel")
    panel = schema.normalize_panel(raw)
    if panel["panel_id"] in document["panels"]:
        raise ValueError(f"panel {panel['panel_id']!r} already exists")
    if len(document["panels"]) >= schema.MAX_PANELS:
        raise ValueError(f"a figure may hold at most {schema.MAX_PANELS} panels")
    _require_source(document, panel["source_id"])
    if panel["placement"]:
        _require_page(document, panel["placement"]["page_id"])

    stamp = now()
    panel["created_at"] = panel["created_at"] or stamp
    panel["updated_at"] = stamp
    # A group is joined through link_panels, never by asserting membership on
    # the way in -- otherwise a panel can name a group that does not exist.
    panel["link_group"] = None
    document["panels"][panel["panel_id"]] = panel


def _update_panel(document, op):
    panel_id = op.get("panel_id")
    current = document["panels"].get(panel_id)
    if current is None:
        raise ValueError(f"unknown panel {panel_id!r}")
    changes = op.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("update_panel needs changes")

    merged = {**current, **changes,
              "panel_id": current["panel_id"],
              # Group membership and lineage are set by their own operations.
              # Letting a generic update touch them is how a panel ends up in a
              # group the group has never heard of.
              "link_group": current["link_group"],
              "derived_from": current["derived_from"]}
    if "source_id" in changes:
        _require_source(document, schema.clean_id(changes["source_id"]))
    updated = schema.normalize_panel(merged)
    if updated["placement"]:
        _require_page(document, updated["placement"]["page_id"])

    # The render revision only ever goes forwards, and only the client that
    # captured a new preview may advance it: a stale preview upload is refused
    # by comparing against this number (see repository.put_preview), which only
    # works while it is monotonic.
    if updated["render_revision"] < current["render_revision"]:
        updated["render_revision"] = current["render_revision"]
    updated["created_at"] = current["created_at"]
    updated["updated_at"] = now()
    document["panels"][panel_id] = updated


def _move_panels(document, op):
    """Reposition several panels at once.

    One operation rather than N, because dragging a selection of five panels is
    one thing the user did and must be one thing they can undo. Also what an
    align or a distribute compiles to.
    """
    moves = op.get("moves")
    if not isinstance(moves, list) or not moves:
        raise ValueError("move_panels needs moves")
    stamp = now()
    for move in moves:
        if not isinstance(move, dict):
            raise ValueError("each move must be an object")
        panel = document["panels"].get(move.get("panel_id"))
        if panel is None:
            raise ValueError(f"unknown panel {move.get('panel_id')!r}")
        placement = move.get("placement")
        if placement is None:
            panel["placement"] = None
        else:
            if not isinstance(placement, dict):
                raise ValueError("a move's placement must be an object or null")
            merged = {**(panel["placement"] or {}), **placement}
            panel["placement"] = schema.normalize_placement(merged)
            _require_page(document, panel["placement"]["page_id"])
        panel["updated_at"] = stamp


def _remove_panels(document, op):
    ids = op.get("panel_ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("remove_panels needs panel_ids")
    for panel_id in ids:
        if panel_id not in document["panels"]:
            raise ValueError(f"unknown panel {panel_id!r}")
    for panel_id in ids:
        _detach_panel(document, panel_id)
        document["panels"].pop(panel_id, None)


# -- annotations ---------------------------------------------------------


def _add_annotation(document, op):
    raw = op.get("annotation")
    if not isinstance(raw, dict):
        raise ValueError("add_annotation needs an annotation")
    annotation = schema.normalize_annotation(raw)
    if annotation["annotation_id"] in document["annotations"]:
        raise ValueError(f"annotation {annotation['annotation_id']!r} already exists")
    if len(document["annotations"]) >= schema.MAX_ANNOTATIONS:
        raise ValueError(f"a figure may hold at most {schema.MAX_ANNOTATIONS} annotations")
    _require_page(document, annotation["page_id"])
    document["annotations"][annotation["annotation_id"]] = annotation


def _update_annotation(document, op):
    annotation_id = op.get("annotation_id")
    current = document["annotations"].get(annotation_id)
    if current is None:
        raise ValueError(f"unknown annotation {annotation_id!r}")
    changes = op.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("update_annotation needs changes")
    if isinstance(changes.get("geometry"), dict):
        changes = {**changes, "geometry": {**current["geometry"], **changes["geometry"]}}
    if isinstance(changes.get("style"), dict):
        changes = {**changes, "style": {**current["style"], **changes["style"]}}
    merged = {**current, **changes,
              "annotation_id": current["annotation_id"], "type": current["type"]}
    updated = schema.normalize_annotation(merged)
    _require_page(document, updated["page_id"])
    document["annotations"][annotation_id] = updated


def _remove_annotations(document, op):
    ids = op.get("annotation_ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("remove_annotations needs annotation_ids")
    for annotation_id in ids:
        if annotation_id not in document["annotations"]:
            raise ValueError(f"unknown annotation {annotation_id!r}")
    for annotation_id in ids:
        document["annotations"].pop(annotation_id, None)


# -- linked groups -------------------------------------------------------


def _link_panels(document, op):
    """Bind panels so an edit to one propagates to the others.

    What propagates is `sync`, and it is deliberately a list rather than
    all-or-nothing: a split-channel row shares a viewport and a size, and
    emphatically does not share channels -- that is the entire point of it.
    """
    group = schema.normalize_link_group(op.get("group") if isinstance(op.get("group"), dict) else {})
    if group["group_id"] in document["link_groups"]:
        raise ValueError(f"group {group['group_id']!r} already exists")
    for panel_id in group["panel_ids"]:
        if panel_id not in document["panels"]:
            raise ValueError(f"unknown panel {panel_id!r}")
        if document["panels"][panel_id]["link_group"]:
            raise ValueError(f"panel {panel_id!r} is already linked")
    if len(set(group["panel_ids"])) < 2:
        raise ValueError("a linked group needs at least two panels")

    group["panel_ids"] = list(dict.fromkeys(group["panel_ids"]))
    document["link_groups"][group["group_id"]] = group
    for panel_id in group["panel_ids"]:
        document["panels"][panel_id]["link_group"] = group["group_id"]


def _unlink_panels(document, op):
    group_id = op.get("group_id")
    group = document["link_groups"].get(group_id)
    if group is None:
        raise ValueError(f"unknown group {group_id!r}")
    for panel_id in group["panel_ids"]:
        panel = document["panels"].get(panel_id)
        if panel is not None:
            panel["link_group"] = None
    document["link_groups"].pop(group_id)


# -- lookups and rules ---------------------------------------------------


def _page_index(document, page_id):
    for index, page in enumerate(document["pages"]):
        if page["page_id"] == page_id:
            return index
    return -1


def _require_page(document, page_id):
    if _page_index(document, page_id) < 0:
        raise ValueError(f"unknown page {page_id!r}")


def _require_source(document, source_id):
    if source_id not in document["sources"]:
        raise ValueError(f"unknown source {source_id!r}")


def _detach_panel(document, panel_id):
    """Take a panel out of whatever group holds it, dissolving a group that
    drops below two members -- a group of one synchronises with nothing."""
    panel = document["panels"].get(panel_id)
    group_id = panel and panel.get("link_group")
    group = document["link_groups"].get(group_id) if group_id else None
    if group is None:
        return
    group["panel_ids"] = [p for p in group["panel_ids"] if p != panel_id]
    if len(group["panel_ids"]) < 2:
        for remaining in group["panel_ids"]:
            if remaining in document["panels"]:
                document["panels"][remaining]["link_group"] = None
        document["link_groups"].pop(group_id, None)


_HANDLERS = {
    "set_meta": _set_meta,
    "add_page": _add_page,
    "update_page": _update_page,
    "remove_page": _remove_page,
    "reorder_pages": _reorder_pages,
    "add_source": _add_source,
    "update_source": _update_source,
    "remove_source": _remove_source,
    "add_panel": _add_panel,
    "update_panel": _update_panel,
    "move_panels": _move_panels,
    "remove_panels": _remove_panels,
    "add_annotation": _add_annotation,
    "update_annotation": _update_annotation,
    "remove_annotations": _remove_annotations,
    "link_panels": _link_panels,
    "unlink_panels": _unlink_panels,
}

OPERATION_NAMES = tuple(sorted(_HANDLERS))
