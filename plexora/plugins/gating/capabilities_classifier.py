"""What an agent may ask of the Single Cell Classifier (Free, like
Thresholding).

The same state the panel shows: examples clicked here are the panel's
examples, a run started here is drawn by an open panel when it finishes, and
the eraser and an agent's revert are one function (`classifier.runs`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
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


class ClearInput(ProjectInput):
    markers: list[str] | None = Field(None, description="Markers whose examples to "
                                      "forget; every marker when omitted.")


class ExportInput(ProjectInput):
    kinds: list[Literal["binary", "scores", "annotations"]] = Field(
        default_factory=lambda: ["binary", "scores", "annotations"])


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


def revert(call, inp):
    from plexora.plugins.gating.server.classifier import panel, runs

    try:
        panel.marker_of(call.data, inp.marker)
    except ClassifierRefused as exc:
        raise AgentError("invalid_input", str(exc)) from None
    result = runs.revert(call.data, inp.marker)
    _tell(call, inp.marker)
    receipt = make_receipt(call, changed=result["reverted"], before=None, after=result,
                           persistent_state="plugin_store:gating", reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), **result}


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
    """Write the files into gating's export folder; their paths."""
    import json

    from plexora.plugins.gating.server.classifier import export as exporter
    from plexora.plugins.gating.server.classifier import store

    ds = call.data
    folder = store.folder(ds.name) / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    written = {}
    for kind in inp.kinds:
        if kind == "annotations":
            path = folder / f"{store.safe_name(ds.name)}_annotations.json"
            store.write_atomic(path, json.dumps(exporter.annotations_document(ds),
                                                indent=1).encode("utf-8"))
        else:
            markers, chunks = exporter.stream_csv(ds, kind)
            if not markers:
                continue
            path = folder / f"{store.safe_name(ds.name)}_{kind}.csv"
            part = path.with_name(path.name + ".part")
            with open(part, "w", encoding="utf-8", newline="") as handle:
                for chunk in chunks:
                    handle.write(chunk)
            part.replace(path)
        written[kind] = str(path)
    return {"written": written}


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
                    "and for one marker its examples, last run and whether it is stale.",
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
                    "classifier_revert.",
            permission="reversible_write", input_model=RunInput, handler=run,
            writes=("gates",), persistent=True, execution="job", egress="aggregates",
            reads=("table", "gates", "image", "mask")),
        cap(name="gating.classifier_revert", tool_name="classifier_revert",
            purpose="Undo a marker's classifier run: the marker goes back to how it was "
                    "before its first run (Thresholding); its examples are kept.",
            permission="reversible_write", input_model=MarkerInput, handler=revert,
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.classifier_clear_examples", tool_name="classifier_clear_examples",
            purpose="Forget the classifier's training examples for some markers (or all). "
                    "Runs already made are kept.",
            permission="reversible_write", input_model=ClearInput, handler=clear_examples,
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.classifier_export", tool_name="classifier_export",
            purpose="Write the classifier's binary calls CSV, scores CSV and annotations "
                    "JSON (one row per cell, one column per classified marker) into "
                    "Plexora's export folder; returns their paths.",
            permission="read", input_model=ExportInput, handler=export, egress="aggregates"),
    ]
