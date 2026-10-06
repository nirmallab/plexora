"""Named sets of cells, kept with the project: what "these cells" means later.

Plexora's viewer has no selection model of its own -- a highlight fades, a
gate is a threshold, an ROI is a shape. What a person or an agent means by
"the tumour-edge cells" has had nowhere to live, so it could not be handed to
anything else. A selection is that: a name, the image it belongs to, the cell
ids (as the table names them), optionally the shape it was drawn from, and who
made it. Stored per project in core's own plugin store (`api.store(project,
"core")`, table `selections`), so it survives a restart and travels with the
project, never with the table file.

Reading one back is the row-level part: `get_selection` returns the ids, so it
is egress `row_level`, which an agent connection does not have unless the
server was started to allow it. The other application on the bridge reads them
through `bridge_collect` instead, which hands ids only to the bridge origin --
SCIMAP Pro owns the table those ids index, so nothing leaves that it does not
already hold.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel, ProjectInput

OWNER = "core"
STORE = "core"
TABLE = "selections"
TAGS = ("selection", "selections", "selected", "cells", "subset", "bridge")

#: How many ids travel inline in an answer before it carries a reference
#: instead. The shared protocol's own limit (spatialbridge `INLINE_LIMIT`), so
#: an object built here validates on the other side.
INLINE_LIMIT = 2000

#: The most ids one selection may hold. A whole-slide selection is a filter,
#: not a selection, and belongs in the table as a column.
MAX_IDS = 500_000

#: Cells `show=True` points at in the viewer; `highlight_cells`' own limit.
SHOW_LIMIT = 200

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"

_COLUMNS = ("name", "image_id", "cell_ids", "shape", "revision", "created", "updated",
            "origin", "operation_id")


def ids_hash(ids) -> str:
    """The protocol's digest of a set of ids (spatialbridge `anndata.ids_hash`):
    sha256 over the sorted ids, each followed by a NUL, first 16 hex digits.
    Order-free, so two sides compare a selection without sending it."""
    digest = hashlib.sha256()
    for value in sorted(str(i) for i in ids):
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def ref_for(project, name) -> str:
    return f"plexora://project/{project}/store/core/selections/{name}"


def _store(project):
    from plexora import api

    return api.store(project, STORE)


def _schema():
    import polars as pl

    return {"name": pl.Utf8, "image_id": pl.Utf8, "cell_ids": pl.List(pl.Utf8),
            "shape": pl.Utf8, "revision": pl.Int64, "created": pl.Utf8, "updated": pl.Utf8,
            "origin": pl.Utf8, "operation_id": pl.Utf8}


def read_all(project) -> dict:
    """{name: record} for every selection a project holds."""
    frame = _store(project).get_table(TABLE)
    if frame is None or frame.height == 0:
        return {}
    out = {}
    for row in frame.iter_rows(named=True):
        record = dict(row)
        record["cell_ids"] = [str(i) for i in (record.get("cell_ids") or [])]
        try:
            record["shape"] = json.loads(record["shape"]) if record.get("shape") else None
        except ValueError:
            record["shape"] = None
        out[record["name"]] = record
    return out


def _write_all(project, records: dict):
    import polars as pl

    rows = []
    for name in sorted(records):
        record = dict(records[name])
        rows.append({
            "name": name, "image_id": record.get("image_id"),
            "cell_ids": [str(i) for i in record.get("cell_ids") or []],
            "shape": json.dumps(record["shape"]) if record.get("shape") else None,
            "revision": int(record.get("revision") or 0), "created": record.get("created"),
            "updated": record.get("updated"), "origin": record.get("origin"),
            "operation_id": record.get("operation_id"),
        })
    frame = pl.DataFrame(rows, schema=_schema(), orient="row") if rows else \
        pl.DataFrame(schema=_schema())
    _store(project).put_table(TABLE, frame)


def describe(project, record, *, inline_limit=INLINE_LIMIT, with_ids=True) -> dict:
    """A stored selection as the protocol's `Selection`: ids inline up to
    `inline_limit`, else a reference; always `n` and `ids_hash`."""
    ids = record.get("cell_ids") or []
    out = {"name": record["name"], "image_id": record.get("image_id"), "n": len(ids),
           "ids_hash": ids_hash(ids), "revision": int(record.get("revision") or 0),
           "shape": record.get("shape"),
           "provenance": {"producer": "plexora", "capability": "selection.set",
                          "operation_id": record.get("operation_id"),
                          "origin": record.get("origin"), "when": record.get("updated")}}
    if with_ids and len(ids) <= min(int(inline_limit), INLINE_LIMIT):
        out["ids"] = list(ids)
    else:
        out["ref"] = {"uri": ref_for(project, record["name"]), "kind": "selection"}
    return out


def _check_project(call, project):
    call.session.project(project)   # unknown_project before anything is read


class SetInput(ProjectInput):
    name: str = Field(pattern=NAME_PATTERN, description="The selection's name: letters, "
                      "digits, '_', '.', '-'. Setting an existing name replaces it.")
    cell_ids: list[str | int] = Field(max_length=MAX_IDS, description="The cells, by the "
                                      "ids the table gives them (its cell-id column).")
    image_id: str | None = Field(None, description="Which image of a multi-image table "
                                 "these cells are in; default the project's own.")
    shape: dict[str, Any] | None = Field(None, description="The GeoJSON geometry it was "
                                         "drawn from, full-resolution pixels, if any.")
    show: bool = Field(False, description="Also point at the cells in an open viewer (the "
                       "first few hundred).")


def _image_id_of(record) -> str | None:
    value = ((record.dataset.subset or {}) if record.dataset is not None else {}).get("value")
    return None if value is None else str(value)


def set_selection(call, inp):
    from plexora.agent.audit import now_iso
    from plexora.agent.receipts import make_receipt
    from plexora.agent.registry import CALL_ORIGIN

    record = call.session.project(inp.project)
    ids = list(dict.fromkeys(str(i) for i in inp.cell_ids))
    existing = read_all(inp.project)
    previous = existing.get(inp.name)
    now = now_iso()
    stored = {
        "name": inp.name, "image_id": inp.image_id or _image_id_of(record), "cell_ids": ids,
        "shape": inp.shape, "revision": int((previous or {}).get("revision") or 0) + 1,
        "created": (previous or {}).get("created") or now, "updated": now,
        "origin": call.extras.get("origin") or CALL_ORIGIN.get() or "in_process",
        "operation_id": call.operation_id,
    }
    existing[inp.name] = stored
    _write_all(inp.project, existing)
    call.project_name = inp.project
    undo = None
    if previous is None:
        undo = {"tool": "delete_selection", "arguments": {"project": inp.project,
                                                          "name": inp.name}}
    elif len(previous.get("cell_ids") or []) <= INLINE_LIMIT:
        undo = {"tool": "set_selection", "arguments": {
            "project": inp.project, "name": inp.name, "cell_ids": previous["cell_ids"],
            "image_id": previous.get("image_id"), "shape": previous.get("shape")}}
    receipt = make_receipt(
        call, changed=True,
        before=None if previous is None else {"n": len(previous.get("cell_ids") or []),
                                              "revision": previous.get("revision")},
        after={"name": inp.name, "n": len(ids), "ids_hash": ids_hash(ids),
               "revision": stored["revision"]},
        revision_before=(previous or {}).get("revision"), revision_after=stored["revision"],
        persistent_state="plugin_store:core", reversible=undo is not None, undo_hint=undo)
    out = {"selection": describe(inp.project, stored, with_ids=False),
           "receipt": receipt.model_dump(mode="json")}
    if inp.show:
        out["shown"] = _show(call, inp.project, ids)
    return out


def _show(call, project, ids):
    """Point at the selection's first cells in an open viewer, best effort:
    a selection is stored whether or not anybody is looking."""
    from plexora.agent import viewer

    try:
        control = viewer.require(call.link)
        view = viewer.resolve_view(control, None, project)
        cells = _positions(call, project, ids[:SHOW_LIMIT])
        if not cells:
            return {"shown": 0, "reason": "none of these ids is in the table"}
        ack = control.send(view["view_id"], "highlight_cells",
                           {"cells": cells, "ttl_ms": 120_000, "clear": True})
        return {"shown": len(cells), "view_id": view["view_id"],
                "status": ack.get("status")}
    except AgentError as exc:
        return {"shown": 0, "reason": exc.message}


def _positions(call, project, ids):
    import polars as pl

    data = call.session.data(project)
    schema = data.schema
    if schema is None:
        return []
    frame = data.table.geometry()
    column = schema.cell_id if schema.cell_id in frame.columns else "id"
    match = frame.filter(pl.col(column).cast(pl.Utf8).is_in([str(i) for i in ids]))
    out = []
    for cell_id, x, y in match.select([column, schema.x, schema.y]).iter_rows():
        try:
            numeric = int(cell_id)
        except (TypeError, ValueError):
            continue    # highlight_cells names cells by integer id
        out.append({"id": numeric, "x": float(x), "y": float(y)})
    return out


class NameInput(ProjectInput):
    name: str = Field(pattern=NAME_PATTERN, description="The selection's name.")


class GetInput(NameInput):
    inline_limit: int = Field(INLINE_LIMIT, ge=0, le=INLINE_LIMIT,
                              description="Return the ids inline up to this many; past "
                                          "it, a reference.")


def _require(project, name):
    found = read_all(project).get(name)
    if found is None:
        raise AgentError("invalid_input", f"{project!r} has no selection {name!r}",
                         detail={"known": sorted(read_all(project))[:50],
                                 "hint": "list_selections"})
    return found


def get_selection(call, inp):
    _check_project(call, inp.project)
    record = _require(inp.project, inp.name)
    return {"selection": describe(inp.project, record, inline_limit=inp.inline_limit)}


def list_selections(call, inp):
    _check_project(call, inp.project)
    records = read_all(inp.project)
    return {"project": inp.project, "selections": [
        {"name": name, "image_id": r.get("image_id"), "n": len(r.get("cell_ids") or []),
         "revision": r.get("revision"), "updated": r.get("updated"), "origin": r.get("origin")}
        for name, r in sorted(records.items())]}


def delete_selection(call, inp):
    from plexora.agent.receipts import make_receipt

    _check_project(call, inp.project)
    records = read_all(inp.project)
    previous = records.pop(inp.name, None)
    if previous is None:
        raise AgentError("invalid_input", f"{inp.project!r} has no selection {inp.name!r}")
    _write_all(inp.project, records)
    call.project_name = inp.project
    ids = previous.get("cell_ids") or []
    undo = None
    if len(ids) <= INLINE_LIMIT:
        undo = {"tool": "set_selection", "arguments": {
            "project": inp.project, "name": inp.name, "cell_ids": ids,
            "image_id": previous.get("image_id"), "shape": previous.get("shape")}}
    receipt = make_receipt(
        call, changed=True, before={"name": inp.name, "n": len(ids),
                                    "revision": previous.get("revision")},
        after=None, persistent_state="plugin_store:core", reversible=undo is not None,
        undo_hint=undo)
    return {"deleted": inp.name, "receipt": receipt.model_dump(mode="json")}


def capabilities():
    def cap(**kwargs):
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, **kwargs)

    return [
        cap(name="selection.set", tool_name="set_selection",
            purpose="Keep a named set of cells with the project (by the table's cell ids, "
                    "with the shape it was drawn from), so 'these cells' can be named later "
                    "or handed to an analysis; optionally point at them in the viewer.",
            permission="reversible_write", input_model=SetInput, handler=set_selection,
            writes=("selections",), persistent=True),
        cap(name="selection.get", tool_name="get_selection",
            purpose="One stored selection: its cell ids (inline up to the limit, else a "
                    "reference), count, digest, image and shape.",
            permission="read", input_model=GetInput, handler=get_selection,
            egress="row_level", reads=("selections",)),
        cap(name="selection.list", tool_name="list_selections",
            purpose="The selections a project holds: name, image, size, who made them.",
            permission="read", input_model=ProjectInput, handler=list_selections,
            reads=("selections",)),
        cap(name="selection.delete", tool_name="delete_selection",
            purpose="Forget a stored selection. Undoable while it is small enough to carry "
                    "back in the receipt.",
            permission="reversible_write", input_model=NameInput, handler=delete_selection,
            writes=("selections",), persistent=True),
    ]
