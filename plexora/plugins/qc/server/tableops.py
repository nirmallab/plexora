"""QC work that has to happen where the cell table's file is.

Writing the calls into the user's file reads that file and the loaded table
together -- the rows have to be checked against the ids the calls were made
for -- so it is a *table operation* (the ROI plugin's `tableops.py` is the
precedent): `ds.table.run` calls it directly for a local table and sends it
to the node for one that lives there. QC's own store stays on the primary;
`source_write.gather` reads it there and the payload carries the frames as
base64 Parquet.

The answer is a plain dict, never a raised refusal: `column_exists` has to
survive the wire to reach the user as "pass replace".

Registration happens on import; `routes.py` imports this module, so the
operation exists wherever the plugin's server half does -- the primary and any
node running the same build.
"""

from __future__ import annotations

from plexora.api import table_operation

COLUMN_EXISTS = "column_exists"
INVALID = "invalid"
MISSING = "missing"


def _refused(reason, **detail):
    return {"ok": False, "reason": reason, **detail}


@table_operation("qc.write_cells")
def write_cells(dataset, payload):
    """Write QC's calls (and Segmentation QC's) into this table's own file."""
    from plexora.plugins.qc.server import source_write

    try:
        result = source_write.apply(dataset, payload, replace=bool(payload.get("replace")))
    except source_write.Exists as exc:
        return _refused(COLUMN_EXISTS, existing=list(exc.existing))
    except LookupError as exc:
        return _refused(MISSING, message=str(exc))
    except ValueError as exc:
        return _refused(INVALID, message=str(exc))
    return {"ok": True, **result}


def run_write(ds, payload) -> dict:
    """Run `qc.write_cells` wherever `ds`'s table is, turning its refusals
    back into `source_write`'s exceptions."""
    from plexora.plugins.qc.server import source_write

    on_node = ds.project.resources.get("table") is not None \
        if hasattr(ds.project, "resources") else False
    try:
        answer = ds.table.run(source_write.OPERATION, payload)
    except Exception as exc:
        if "no table operation named" in str(exc):
            from plexora.agent.errors import AgentError

            raise AgentError("precondition_missing",
                             "the data node holding this table cannot write QC's calls: "
                             "update Plexora on it", detail={"operation":
                                                             source_write.OPERATION}) from exc
        raise
    if not isinstance(answer, dict):
        raise ValueError("the table operation answered nothing")
    if not answer.get("ok", False):
        reason = answer.get("reason")
        if reason == COLUMN_EXISTS:
            raise source_write.Exists(answer.get("existing") or [])
        if reason == MISSING:
            raise LookupError(answer.get("message") or "nothing to write")
        raise ValueError(answer.get("message") or "the write was refused")
    out = {k: v for k, v in answer.items() if k != "ok"}
    out["on_node"] = on_node
    return out
