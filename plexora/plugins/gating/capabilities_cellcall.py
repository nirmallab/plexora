"""The cell model's tools: check a gate's cells with it, and read what it said.

`run_cell_model` reads every cell near each gate with the cell model
(`autogate/cellcall.py`) as a job -- about a minute a marker -- and stores
the result per gate; `get_cell_model_calls` reads what is stored for the
gates as they stand. Neither changes a gate, a count or an export: the model
is a layer beside the gate (the gate's call stands unless the model is
confident), shown in the viewer and written beside the gates on request.

Loaded with the rest of gating's capabilities (`capabilities_autogate`).
"""

from __future__ import annotations

import time

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_LIST
from plexora.agent.registry import Capability
from plexora.agent.schemas import ProjectInput
from plexora.plugins.gating import PLUGIN
from plexora.plugins.gating.server import model

OWNER = "gating"
TAGS = ("gate", "gating", "threshold", "positive", "negative", "marker", "cell", "model",
        "spill", "spillover", "background", "neighbour", "own stain")

#: What the rule is, in words, in every answer.
RULE = ("the gate's call stands unless the cell model is confident: p_own >= 1 - confident "
        "makes a cell positive, <= confident negative; shown beside the gate, never applied "
        "to counts or exports")


class RunCellModelInput(ProjectInput):
    markers: list[str] | None = Field(
        None, description="Gated markers to check; every gated marker when omitted.")
    refresh: bool = Field(
        False, description="Read the cells again even where a run at the current gate is "
                           "stored.")


class CellModelInput(ProjectInput):
    marker: str | None = Field(None, description="One marker; every gated marker when omitted.")
    cells: bool = Field(
        False, description="With `marker`: the cell ids the model moves up, moves down and "
                           "leaves uncertain (at most the list limit each).")


def _model_card():
    from plexora.plugins.gating.server.autogate import cellcall, cellcall_weights

    return {"name": cellcall_weights.MODEL_NAME, "version": cellcall_weights.MODEL_VERSION,
            "layout": cellcall_weights.MODEL_LAYOUT, "confident": cellcall.CELLCALL["confident"],
            "band_halves": cellcall.CELLCALL["band_halves"],
            "max_cells": cellcall.CELLCALL["max_cells"]}


def run_cell_model(call, inp):
    """Each marker's cells near its gate, read by the cell model, stored per
    gate. A marker the model cannot read says why; it never fails the run."""
    from plexora.plugins.gating.server.autogate import cellcall, cellcall_weights

    ds = call.data
    active = model.active_gates(ds)
    wanted = list(inp.markers) if inp.markers else list(active)
    unknown = [m for m in wanted if m not in ds.table.markers]
    if unknown:
        raise AgentError("invalid_input", f"not markers of {ds.name!r}: {unknown[:MAX_LIST]}",
                         detail={"markers": ds.table.markers[:MAX_LIST]})
    if not wanted:
        raise AgentError("precondition_missing", f"{ds.name!r} has no gated marker to check",
                         detail={"hint": "gate a marker first"})
    total = len(wanted)
    call.progress(done=0, total=total, message="loading the cell model")
    try:
        predictor = cellcall.predictor()
    except cellcall_weights.CellModelMissing as exc:
        raise AgentError("precondition_missing", str(exc),
                         detail={"directory": cellcall_weights.model_dir()[0].as_posix(),
                                 "hint": "it is fetched on first use; offline, copy the file "
                                         f"there or set {cellcall_weights.ENV_MODEL_DIR}"}) from None
    gates = {m: float(low) for m, (low, _high) in active.items()}
    out = {}
    for index, marker in enumerate(wanted):
        call.check_cancelled()
        if marker not in active:
            out[marker] = {"state": "not_gated"}
            continue
        low, high = active[marker]
        last = [0.0]

        def progress(done, n, marker=marker, index=index, last=last):
            # A job record is written when its message changes: once a second
            # is plenty for a progress line.
            now = time.monotonic()
            if now - last[0] < 1.0 and done < n:
                return
            last[0] = now
            call.progress(done=index, total=total,
                          message=f"{marker}: {done:,} of {n:,} cells read")

        try:
            found = cellcall.run(call.session, ds, marker, float(low), float(high), gates=gates,
                                 model=predictor, progress=progress, cancelled=call.cancelled,
                                 refresh=inp.refresh)
        except cellcall.Unavailable as exc:
            out[marker] = {"state": "unavailable", "why": str(exc)}
            continue
        except InterruptedError:
            call.check_cancelled()
            raise
        out[marker] = {"state": "ready", **cellcall.summary(found)}
        call.progress(done=index + 1, total=total, message=f"{marker} read")
    return {"project": ds.name, "markers": out, "model": _model_card(), "rule": RULE,
            "experimental_unit": "cell (one image)"}


def get_cell_model_calls(call, inp):
    """What is stored for each gate as it stands: `ready` with the counts,
    `not_run` (never, or the gate moved since), `not_gated`."""
    from plexora.plugins.gating.server.autogate import cellcall, cells as cellmod

    ds = call.data
    if inp.marker is not None and inp.marker not in ds.table.markers:
        raise AgentError("invalid_input", f"{inp.marker!r} is not a marker of {ds.name!r}",
                         detail={"markers": ds.table.markers[:MAX_LIST]})
    markers = [inp.marker] if inp.marker else list(model.active_gates(ds))
    out = {}
    for marker in markers:
        found, state, gate = cellcall.current(ds, marker)
        row = {"state": state}
        if gate and gate.get("thresholded"):
            row["low"] = gate["low"]
        if found is not None:
            row.update(cellcall.summary(found))
            if inp.marker and inp.cells:
                decided = cellcall.decide(found["rows"], found["p"], found["run_call"])
                ids = cellmod.cells(ds).ids
                for name in ("promoted", "demoted", "uncertain"):
                    row[f"{name}_cells"] = [int(i) for i in ids[decided[name][:MAX_LIST]]]
        out[marker] = row
    return {"project": ds.name, "markers": out, "model": _model_card(), "rule": RULE,
            "experimental_unit": "cell (one image)"}


def capabilities():
    requires = PLUGIN.requires

    def cap(**kwargs):
        kwargs.setdefault("reads", ("table", "gates", "image", "mask"))
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, requires=requires, version="1", **kwargs)

    return [
        cap(name="gating.cell_model_run", entitlement="ai:gating:analytics",
            tool_name="run_cell_model",
            purpose="Check the cells near each gate with the cell model: per cell, whether "
                    "the stain is its own or a neighbour's glow or background. The gate's "
                    "call stands unless the model is confident; the result (cells it would "
                    "call positive below the gate, negative above it, and positives it "
                    "leaves uncertain) is stored per gate and shown beside it -- the gate, "
                    "counts and exports are unchanged. About a minute a marker.",
            permission="read", input_model=RunCellModelInput, handler=run_cell_model,
            execution="job", egress="aggregates",
            tags=TAGS + ("check", "per-cell", "classifier")),
        cap(name="gating.cell_model_calls", entitlement="ai:gating:analytics",
            tool_name="get_cell_model_calls",
            purpose="What the cell model said about each gate as it stands: how many cells "
                    "it would call positive below the gate and negative above it, how many "
                    "positives it leaves uncertain, or that it has not been run at this "
                    "gate; with a marker and `cells`, the cell ids.",
            permission="read", input_model=CellModelInput, handler=get_cell_model_calls,
            egress="aggregates", reads=("table", "gates"),
            tags=TAGS + ("per-cell", "review")),
    ]
