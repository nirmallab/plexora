"""ROI work that has to happen where the cell table's file is.

Everything in `adapters.py` and the spatial join in `mapping.py` reads the
project's data file, the loaded frame, or both -- and the interesting ones read
both at once, because that is the only way to check that the rows being written
still describe the cells the labels were computed for. A network cannot sit in
the middle of that check, so these are registered as *table operations*: the
route names one, and it runs on whichever machine holds the table.

Every function here answers with a plain dict rather than by raising, and the
reason is the wire. `ColumnExists` carries a list of taken names and a
suggestion, which is a refusal the user acts on rather than an error -- and a
refusal that has to survive a round trip through JSON to reach them. Turning it
into an exception on this side and back into one on the other would be two
translations for a value that only ever gets rendered as text.

Registration happens on import, and `routes.py` imports this module, so the
operations exist wherever the plugin's server half does -- the primary and any
node running the same build.
"""

from __future__ import annotations

from plexora.api import table_operation

#: Every refusal these return. The route branches on this, never on wording --
#: same rule the client follows for `needs`.
COLUMN_EXISTS = "column_exists"
KEY_EXISTS = "key_exists"
ELEMENT_EXISTS = "element_exists"
INVALID = "invalid"


def _refused(reason, **detail):
    return {"ok": False, "reason": reason, **detail}


def _done(result):
    return {"ok": True, **result}


@table_operation("roi.map_to_cells")
def map_to_cells(dataset, payload):
    """Assign every cell the regions it falls inside, and write the columns.

    One operation rather than two, deliberately. The assignment produces one
    value per row of the loaded frame and the write consumes it in that same
    order, so splitting them would mean sending a million labels back to the
    primary only to send them straight out again -- and would open a window in
    which the table could be reloaded between the two halves.
    """
    from plexora.plugins.roi.server import adapters, mapping

    frame = dataset.table.frame()
    if frame is None:
        return _refused(INVALID, message="This project has no cell-level data")

    x_column = payload.get("x_column")
    y_column = payload.get("y_column")
    missing = [c for c in (x_column, y_column) if not c or c not in frame.columns]
    if missing:
        return _refused(INVALID, message=(
            f"coordinate column {missing[0]!r} is not in this project's table"))

    # numpy rather than to_list(): a real slide is 10^5-10^6 cells, and this is
    # the array shapely wants anyway. A cell with no coordinates arrives as NaN,
    # which is never inside anything -- so it is simply left unassigned rather
    # than needing a case of its own.
    labels, names = mapping.assign(
        payload.get("features") or [], payload.get("categories") or [],
        frame[x_column].to_numpy(), frame[y_column].to_numpy())

    try:
        result = adapters.write_cell_columns(
            dataset, labels, names,
            prefix=payload.get("prefix"), replace=bool(payload.get("replace")),
        )
    except adapters.ColumnExists as exc:
        return _refused(COLUMN_EXISTS, existing=list(exc.existing),
                        suggestion=exc.suggestion)
    except ValueError as exc:
        return _refused(INVALID, message=str(exc))
    return _done(result)


@table_operation("roi.save_anndata")
def save_anndata(dataset, payload):
    """Write the annotation document into the source .h5ad's `uns/plexora`."""
    from plexora.plugins.roi.server import adapters

    try:
        result = adapters.save_to_anndata(
            dataset, payload.get("state") or {}, payload.get("plugin_version"),
            key=payload.get("key"), replace=bool(payload.get("replace")),
        )
    except adapters.KeyExists as exc:
        return _refused(KEY_EXISTS, existing=list(exc.existing),
                        suggestion=exc.suggestion)
    except ValueError as exc:
        return _refused(INVALID, message=str(exc))
    return _done(result)


@table_operation("roi.save_spatialdata")
def save_spatialdata(dataset, payload):
    """Write the annotations into the store as a shapes element."""
    from plexora.plugins.roi.server import adapters

    try:
        result = adapters.save_to_spatialdata(
            dataset, payload.get("state") or {}, payload.get("element_name"))
    except adapters.ElementExists as exc:
        return _refused(ELEMENT_EXISTS, existing=list(exc.existing),
                        suggestion=exc.suggestion)
    except ValueError as exc:
        return _refused(INVALID, message=str(exc))
    return _done(result)


@table_operation("roi.destinations")
def destinations(dataset, payload):
    """Names already taken in the user's file, for the naming control.

    Best-effort at both ends: a file that cannot be listed right now costs the
    user nothing, because the write path checks again and refuses there. That
    is why the failure here is an empty list and not a refusal.
    """
    from plexora.plugins.roi.server import adapters

    kind = dataset.source_kind
    if kind == "anndata":
        return _done({"existing": adapters.existing_anndata_keys(dataset)})
    if kind == "spatialdata":
        try:
            return _done({"existing": adapters.existing_shapes(dataset)})
        except ValueError:
            return _done({"existing": []})
    return _done({"existing": []})
