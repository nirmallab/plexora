"""What an agent may ask of the Single Cell Classifier (Free, like
Thresholding).

The same state the panel shows: examples clicked here are the panel's
examples, a run started here is drawn by an open panel when it finishes, and
the eraser and an agent's revert are one function (`classifier.runs`).

Two slots per marker: the run that decides it (a panel run, or an adopted
layer) and the layer a gating session -- or `classifier_run` with a `gate` --
made at a gate, which is drawn and exported but never decides until
`classifier_adopt`. The threshold and the cell calls stay two things: where a
layer calls a cell positive below its gate, or negative above it, is kept
and exported (`classifier_export`'s overrides CSV).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import tool_name_of
from plexora.agent.schemas import ProjectInput
from plexora.plugins.gating.server.classifier import ClassifierRefused

OWNER = "gating"
TAGS = ("classifier", "classify", "single cell", "train", "examples", "annotate",
        "positive", "negative", "marker", "machine learning", "model")


class StateInput(ProjectInput):
    marker: str | None = Field(None, description="A marker column; adds its examples, "
                               "run and whether its calls are stale.")


class MarkerInput(ProjectInput):
    marker: str = Field(description="A marker column, as `list_markers` names it.")


class AnnotateInput(MarkerInput):
    positive: list[int] = Field(default_factory=list, max_length=5000,
                                description="Cell ids (the table's cell_id = mask "
                                            "label) to label positive for the marker.")
    negative: list[int] = Field(default_factory=list, max_length=5000,
                                description="Cell ids to label negative.")
    remove: list[int] = Field(default_factory=list, max_length=5000,
                              description="Cell ids whose label to remove.")


class RunInput(MarkerInput):
    pixels: bool = Field(True, description="Also learn from the marker's pixels in each "
                                           "cell (needs an image channel and a mask).")
    auto_pick: bool = Field(False, description="Pick about 40 clear positive and 40 clear "
                            "negative cells across the image first (the panel's wand), "
                            "replacing earlier automatic picks; labelled cells are kept "
                            "and weigh twice as much.")
    gate: float | None = Field(None, description="A gate (lower bound, in the table's "
                               "units): make the run a layer AT that gate instead -- picks "
                               "made either side of it, each cell called by the gate unless "
                               "the classifier is confident the other way. It lands in the "
                               "marker's session slot (replacing an earlier layer), never "
                               "decides the marker until classifier_adopt, and keeps where "
                               "it parts ways with the gate. auto_pick does not apply.")


class ReviewInput(MarkerInput):
    count: int = Field(8, ge=1, le=50, description="How many cells to offer.")


class RevertInput(MarkerInput):
    discard: bool = Field(False, description="Also forget the layer a gating session made "
                          "for the marker (otherwise it stays drawn, never deciding).")


class ClearInput(ProjectInput):
    markers: list[str] | None = Field(None, description="Markers whose examples to "
                                      "forget; every marker when omitted.")


class ExportInput(ProjectInput):
    kinds: list[Literal["binary", "scores", "overrides", "annotations"]] = Field(
        default_factory=lambda: ["binary", "scores", "overrides", "annotations"],
        description="binary: 1/0 per cell and marker; scores: the model's score; overrides: "
                    "where a run made at a gate parts ways with it (1 positive below it, -1 "
                    "negative above it, 0 agrees); annotations: the training examples with "
                    "their origins, and each marker's runs. Blank = not classified.")
    include_session_runs: bool = Field(True, description="binary and scores also carry "
                                       "the markers that only have a gating session's "
                                       "layer (the layer's calls; the threshold still "
                                       "decides them). False: only the markers the "
                                       "classifier decides.")


def _refused(exc):
    return AgentError("precondition_missing", str(exc))


def state(call, inp):
    from plexora.plugins.gating.server.classifier import panel

    try:
        return panel.state(call.data, inp.marker)
    except ClassifierRefused as exc:
        raise AgentError("invalid_input", str(exc)) from None


def annotate(call, inp):
    from plexora.plugins.gating.server.classifier import panel, store

    ds = call.data
    try:
        panel.marker_of(ds, inp.marker)
    except ClassifierRefused as exc:
        raise AgentError("invalid_input", str(exc)) from None
    before = store.examples(ds.name, inp.marker)
    unknown = []
    for ids, label in ((inp.positive, "positive"), (inp.negative, "negative"),
                       (inp.remove, None)):
        for cell_id in ids:
            try:
                panel.annotate(ds, inp.marker, cell_id, label, toggle=False, origin="agent")
            except ClassifierRefused:
                unknown.append(int(cell_id))
    after = store.examples(ds.name, inp.marker)
    _tell(call, inp.marker)
    receipt = make_receipt(
        call, changed=before != after,
        before={"positive": len(before[0]), "negative": len(before[1])},
        after={"positive": len(after[0]), "negative": len(after[1])},
        persistent_state="plugin_store:gating",
        undo_hint={"tool": "classifier_annotate", "arguments": {
            "project": call.project_name, "marker": inp.marker,
            "remove": sorted(set(inp.positive) | set(inp.negative))}})
    return {"receipt": receipt.model_dump(mode="json"),
            "examples": {"positive": len(after[0]), "negative": len(after[1])},
            "unknown_cells": unknown[:100]}


def run(call, inp):
    from plexora.plugins.gating.server.classifier import runs

    if inp.gate is not None:
        return _layer_run(call, inp)
    ds = call.data
    runs.started(ds.name, (call.job or {}).get("job_id"), inp.marker)
    try:
        record = runs.run(
            ds, inp.marker, pixels=inp.pixels, auto_pick=inp.auto_pick,
            progress=lambda done, total, message: call.progress(done=done, total=total,
                                                                message=message),
            cancelled=call.cancelled)
    except ClassifierRefused as exc:
        raise _refused(exc) from None
    except InterruptedError:
        from plexora.agent.jobs import JobCancelled

        raise JobCancelled() from None
    finally:
        runs.finished(ds.name, (call.job or {}).get("job_id"))
    _tell(call, inp.marker)
    from plexora.plugins.gating.server.classifier import calls

    receipt = make_receipt(call, changed=True, before=None,
                           after={"marker": inp.marker, "run_id": record["run_id"]},
                           persistent_state="plugin_store:gating",
                           undo_hint={"tool": "classifier_revert", "arguments": {
                               "project": call.project_name, "marker": inp.marker}})
    return {"receipt": receipt.model_dump(mode="json"), "run": calls.public_run(record)}


def _layer_run(call, inp):
    """`classifier_run` with a gate: the layer at that gate, in the session
    slot, made the way a gating session makes one (`runs.session_run`)."""
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import calls, runs, store

    ds = call.data
    if inp.marker not in ds.table.markers:
        raise AgentError("invalid_input", f"{inp.marker!r} is not a marker of this project")
    stored = model.get_gate(ds, inp.marker) or {}
    before = store.session_run_record(ds.name, inp.marker)
    runs.started(ds.name, (call.job or {}).get("job_id"), inp.marker)
    try:
        result = runs.session_run(
            ds, inp.marker, low=inp.gate, high=stored.get("high"), origin="agent",
            gate_source="agent", keep=True, pixels=inp.pixels,
            progress=lambda done, total, message: call.progress(done=done, total=total,
                                                                message=message),
            cancelled=call.cancelled)
    except ClassifierRefused as exc:
        raise _refused(exc) from None
    except InterruptedError:
        from plexora.agent.jobs import JobCancelled

        raise JobCancelled() from None
    finally:
        runs.finished(ds.name, (call.job or {}).get("job_id"))
    record = result["record"]
    out = {"run": calls.public_run(record), "slot": "session_run",
           "decides": calls.active_source(ds, inp.marker)}
    if record.get("status") != "ready":
        # Withheld (no signal to learn from, no examples to pick): nothing
        # was stored, the slot is as it was.
        receipt = make_receipt(call, changed=False, before=None,
                               after={"marker": inp.marker, "status": record.get("status"),
                                      "why": record.get("why")},
                               persistent_state="plugin_store:gating", reversible=False)
        return {"receipt": receipt.model_dump(mode="json"), **out}
    _tell(call, inp.marker)
    decided = store.run_record(ds.name, inp.marker) is not None
    # Undo is dropping the layer -- which classifier_revert(discard) does only
    # without a run that decides the marker (it would revert that run too).
    undo = None if decided else {"tool": "classifier_revert", "arguments": {
        "project": call.project_name, "marker": inp.marker, "discard": True}}
    receipt = make_receipt(
        call, changed=True, before={"session_run": (before or {}).get("run_id")},
        after={"marker": inp.marker, "session_run": record["run_id"]},
        persistent_state="plugin_store:gating", reversible=undo is not None, undo_hint=undo)
    out["compare"] = calls.compare(ds, inp.marker, "session_run")
    out["next"] = (f"{tool_name_of('gating.classifier_adopt')}(project, marker) makes "
                   "this layer decide the marker; until then the threshold does.")
    return {"receipt": receipt.model_dump(mode="json"), **out}


def revert(call, inp):
    from plexora.plugins.gating.server.classifier import panel, runs

    try:
        panel.marker_of(call.data, inp.marker)
    except ClassifierRefused as exc:
        raise AgentError("invalid_input", str(exc)) from None
    result = runs.revert(call.data, inp.marker, discard=inp.discard)
    _tell(call, inp.marker)
    receipt = make_receipt(call, changed=result["reverted"] or result["discarded"],
                           before=None, after=result,
                           persistent_state="plugin_store:gating", reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), **result}


def adopt(call, inp):
    """The session's layer decides the marker from now on."""
    from plexora.plugins.gating.server.classifier import calls, panel, runs

    ds = call.data
    try:
        panel.marker_of(ds, inp.marker)
    except ClassifierRefused as exc:
        raise AgentError("invalid_input", str(exc)) from None
    before = calls.active_source(ds, inp.marker)
    try:
        result = runs.adopt_session(ds, inp.marker)
    except ClassifierRefused as exc:
        raise _refused(exc) from None
    _tell(call, inp.marker)
    receipt = make_receipt(
        call, changed=True, before={"active": before}, after=result,
        persistent_state="plugin_store:gating",
        undo_hint={"tool": "classifier_revert", "arguments": {
            "project": call.project_name, "marker": inp.marker}})
    return {"receipt": receipt.model_dump(mode="json"), **result,
            "summary": calls.summary(ds, inp.marker)}


def review(call, inp):
    """The uncertain unlabelled cells of the run the panel shows."""
    from plexora.plugins.gating.server.classifier import panel

    try:
        out = panel.review_cells(call.data, inp.marker, inp.count)
    except ClassifierRefused as exc:
        raise AgentError("invalid_input", str(exc)) from None
    ids = [cell["cell_id"] for cell in out.get("cells") or []]
    out["next"] = (
        f"Pass cell_ids={ids} to {tool_name_of('cell.gallery')} to look at these cells. "
        "Only the user labels cells for the classifier, one at a time in the panel's "
        "review; what you see is a note for them, not a label."
        if ids else "No uncertain unlabelled cells to review.")
    return out


def clear_examples(call, inp):
    from plexora.plugins.gating.server.classifier import panel

    try:
        result = panel.clear_examples(call.data, inp.markers)
    except ClassifierRefused as exc:
        raise AgentError("invalid_input", str(exc)) from None
    _tell(call, None)
    receipt = make_receipt(call, changed=bool(result["cleared"]), before=None, after=result,
                           persistent_state="plugin_store:gating", reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), **result}


def export(call, inp):
    """Write the files into gating's export folder; their paths, and where
    each CSV column came from."""
    import json

    from plexora.plugins.gating.server.classifier import export as exporter
    from plexora.plugins.gating.server.classifier import store

    ds = call.data
    folder = store.folder(ds.name) / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    written, columns, skipped = {}, {}, {}
    for kind in dict.fromkeys(inp.kinds):
        if kind == "annotations":
            path = folder / f"{store.safe_name(ds.name)}_annotations.json"
            store.write_atomic(path, json.dumps(exporter.annotations_document(ds),
                                                indent=1).encode("utf-8"))
        else:
            picked = exporter.plan(ds, kind, inp.include_session_runs)
            if not picked:
                skipped[kind] = ("no marker has a run made at a gate" if kind == "overrides"
                                 else "no marker has a classifier run to export")
                continue
            _markers, chunks = exporter.stream_csv(ds, kind, picked=picked)
            path = folder / f"{store.safe_name(ds.name)}_{kind}.csv"
            part = path.with_name(path.name + ".part")
            with open(part, "w", encoding="utf-8", newline="") as handle:
                for chunk in chunks:
                    handle.write(chunk)
            part.replace(path)
            columns[kind] = exporter.columns_of(picked)
        written[kind] = str(path)
    return {"written": written, "columns": columns, "skipped": skipped,
            "codes": {"binary": "1 positive, 0 negative, blank not classified",
                      "overrides": "1 positive below the run's gate, -1 negative above "
                                   "it, 0 agrees, blank not classified"},
            "note": "a column whose active is threshold is a gating session's layer: the "
                    "threshold still decides that marker"}


def _tell(call, marker):
    if call.notify is not None:
        try:
            call.notify(call.project_name, "gating", "classifier.changed", {"marker": marker})
        except Exception:
            pass


def capabilities(requires):
    from plexora.agent.registry import Capability

    def cap(**kwargs):
        kwargs.setdefault("reads", ("table", "gates"))
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, requires=requires, version="1", **kwargs)

    return [
        cap(name="gating.classifier_state", tool_name="classifier_state",
            purpose="The Single Cell Classifier's state: which mode the Thresholding panel "
                    "is in, example counts per marker, which markers the classifier decides, "
                    "and for one marker its examples, its run and whether it is stale. A "
                    "gating session's layer at a gate (never deciding until "
                    "classifier_adopt) is listed per marker by get_gate and get_all_gates.",
            permission="read", input_model=StateInput, handler=state, egress="aggregates"),
        cap(name="gating.classifier_annotate", tool_name="classifier_annotate",
            purpose="Label cells positive or negative for a marker (training examples for "
                    "the Single Cell Classifier), or remove labels. Cell ids are the table's "
                    "cell_id (the mask label).",
            permission="reversible_write", input_model=AnnotateInput, handler=annotate,
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.classifier_run", tool_name="classifier_run",
            purpose="Train the Single Cell Classifier on a marker's examples (at least 3 "
                    "positive and 3 negative, or auto_pick: true to have about 40 of each "
                    "picked across the image) and call every eligible cell (a job). "
                    "Replaces the marker's previous run; the threshold is kept for "
                    "classifier_revert. With gate: a layer at that gate in the session "
                    "slot instead, which never decides until classifier_adopt.",
            permission="reversible_write", input_model=RunInput, handler=run,
            writes=("gates",), persistent=True, execution="job", egress="aggregates",
            reads=("table", "gates", "image", "mask")),
        cap(name="gating.classifier_revert", tool_name="classifier_revert",
            purpose="Undo a marker's classifier run: the marker goes back to how it was "
                    "before its first run (Thresholding); its examples are kept. A gating "
                    "session's layer stays drawn unless discard: true.",
            permission="reversible_write", input_model=RevertInput, handler=revert,
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.classifier_adopt", tool_name="classifier_adopt",
            purpose="Let the layer a gating session made at a marker's gate decide the "
                    "marker from now on (its calls, not the threshold's, are the marker's "
                    "positives). The threshold is kept; classifier_revert undoes it.",
            permission="reversible_write", input_model=MarkerInput, handler=adopt,
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.classifier_review", tool_name="classifier_review",
            purpose="The uncertain unlabelled cells of the run the panel shows for a "
                    "marker, spread across the tissue: ids, positions and scores, to look "
                    "at with render_cell_gallery. Only the user labels them, in the panel.",
            permission="read", input_model=ReviewInput, handler=review, egress="aggregates",
            tags=TAGS + ("review", "uncertain", "boundary")),
        cap(name="gating.classifier_clear_examples", tool_name="classifier_clear_examples",
            purpose="Forget the classifier's training examples for some markers (or all). "
                    "Runs already made are kept.",
            permission="reversible_write", input_model=ClearInput, handler=clear_examples,
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.classifier_export", tool_name="classifier_export",
            purpose="Write the classifier's files into Plexora's export folder: binary "
                    "calls, scores and overrides CSVs (one row per cell, one column per "
                    "marker; overrides marks where a run made at a gate calls a cell "
                    "positive below it or negative above it) and the annotations JSON "
                    "(examples with their origins, and each marker's runs). binary and "
                    "scores carry the markers the classifier decides and, by default, "
                    "those with only a gating session's layer. Returns the paths and "
                    "where each column came from.",
            permission="read", input_model=ExportInput, handler=export, egress="aggregates"),
    ]
