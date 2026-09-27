"""What an external agent may ask gating to do.

Loaded only when an agent asks for gating (`Plugin.capabilities_factory`), so
reading the plugin's descriptor stays as cheap as it always was.

Everything reads and writes the same state the sidebar does -- the pickled gate
list in this plugin's store -- through `model`'s handle-taking functions, so a
gate an agent sets is the one the sidebar shows next, and the counts an agent
reports are the counts the viewer colours. Two rules carried over from the
panel, unchanged:

- **Setting a gate never touches the source file.** Only `write_gates_to_source`
  does, it needs the server started with `--allow-source-writes`, and it needs
  `confirm: true` -- the agent equivalent of pressing "Save Gates to AnnData".
- **A gate is a lower bound on the marker's own scale.** Whatever the table
  stores (raw or log1p'd, `log_transformed` says which), the gate is in those
  units.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_LIST
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel, ProjectInput
from plexora.plugins.gating import PLUGIN
# Registers gating.gmm / gating.save_gates, which the handlers below run.
from plexora.plugins.gating.server import model, tableops  # noqa: F401

OWNER = "gating"
STATE = "plugin_store:gating"


class MarkerInput(ProjectInput):
    marker: str = Field(description="A marker column, as `list_markers` names it.")


def _marker(call, marker):
    if marker not in call.data.table.markers:
        raise AgentError("invalid_input",
                         f"{marker!r} is not a marker of {call.project_name!r}",
                         detail={"markers": call.data.table.markers[:MAX_LIST]})
    return marker


def get_gate(call, inp):
    _marker(call, inp.marker)
    gate = model.get_gate(call.data, inp.marker)
    return {"gate": gate, "summary": model.gated_summary(call.data, inp.marker),
            "revision": model.revision(call.data),
            "log_transformed": call.data.table.log_transformed}


def get_all(call, inp):
    from plexora.plugins.gating.server.autogate import provenance

    gates = model.all_gates(call.data)
    active = model.active_gates(call.data)
    return {"gates": gates[:MAX_LIST], "truncated": len(gates) > MAX_LIST,
            "thresholded": sorted(active), "revision": model.revision(call.data),
            "provenance": provenance.summary(call.data.name, gates),
            "provenance_revision": provenance.revision(call.data.name),
            "log_transformed": call.data.table.log_transformed}


def distribution(call, inp):
    _marker(call, inp.marker)
    ds = call.data
    fit = model.gmm_for(ds, inp.marker)
    desc = (ds.table.describe().get(inp.marker) or {})
    return {
        "marker": inp.marker,
        "histogram": [{"x": float(p["x"]), "y": float(p["y"])}
                      for p in desc.get("histogram", [])],
        "auto_gate": fit.get("gate"),
        "background_curve": fit.get("gmm_1", []),
        "positive_curve": fit.get("gmm_2", []),
        "current_gate": model.get_gate(ds, inp.marker),
        "log_transformed": ds.table.log_transformed,
    }


def auto(call, inp):
    _marker(call, inp.marker)
    ds = call.data
    fit = model.fit_for(ds, inp.marker)
    current = model.get_gate(ds, inp.marker)
    if fit is None:
        return {"marker": inp.marker, "auto_gate": None, "fit": None,
                "reason": "this column has no mixture to fit (too few distinct values)",
                "current_gate": current}
    summary = model.gated_summary(ds, inp.marker, fit["gate"], current["high"])
    return {"marker": inp.marker, "auto_gate": fit["gate"], "fit": fit,
            "summary_at_auto_gate": summary, "current_gate": current,
            "written": False,
            "next": "set_gate with this value to store it; nothing was changed"}


class DatasetGateInput(AgentModel):
    dataset: str = Field(description="A dataset's name or id, as `list_datasets` names it.")
    marker: str = Field(description="A marker column every image's table has.")
    low: float = Field(description="The gate, in the tables' own units, applied to every "
                       "image.")
    high: float | None = Field(None, description="Upper bound; default keeps each image's "
                               "stored one.")
    rule: Literal["same_threshold"] = Field(
        "same_threshold", description="same_threshold: one gate for every image. (Per-image "
                                      "automatic gates are not in this release.)")


def apply_to_dataset(call, inp):
    """One gate across a cohort, as a job: one receipt per image, so each is
    undone on its own, and the numbers reported per image -- the image is the
    experimental unit, not the cell."""
    import dataclasses

    from plexora import datasets

    try:
        cohort = datasets.dataset(inp.dataset)
    except KeyError as exc:
        raise AgentError("invalid_input", str(exc.args[0]),
                         detail={"hint": "list_datasets"}) from None
    names = list(cohort.projects)
    if not names:
        raise AgentError("precondition_missing", f"{cohort.name!r} has no projects",
                         detail={"dataset": cohort.name})
    requires = PLUGIN.requires
    done, skipped, receipts = [], [], []
    call.progress(done=0, total=len(names), message="starting")
    for index, name in enumerate(names, start=1):
        call.check_cancelled()
        child = dataclasses.replace(call, operation_id=f"{call.operation_id}.{index:03d}",
                                    project_name=name, _data=None, receipted=False,
                                    extras=dict(call.extras))
        try:
            record = call.session.project(name)
            if not requires.applies_to(record) or requires.missing_from(record):
                skipped.append({"project": name, "reason": "no cell table to gate"})
                continue
            ds = child.data
            if inp.marker not in ds.table.markers:
                skipped.append({"project": name,
                                "reason": f"{inp.marker!r} is not one of its markers"})
                continue
            revision_before = model.revision(ds)
            current = model.get_gate(ds, inp.marker)
            high = current["high"] if inp.high is None else inp.high
            before, after, revision_after = model.set_gate(ds, inp.marker, inp.low, high)
        except model.GateLocked as exc:
            skipped.append({"project": name, "reason": str(exc)})
            continue
        except (AgentError, ValueError, OSError) as exc:
            skipped.append({"project": name, "reason": str(exc)})
            continue
        receipt = make_receipt(
            child, changed=before != after, before=before, after=after,
            revision_before=revision_before, revision_after=revision_after,
            persistent_state=STATE,
            undo_hint=gate_undo_hint(name, inp.marker, before, revision_after),
            extra={"parent_operation_id": call.operation_id, "dataset": cohort.name})
        counted = model.gated_summary(ds, inp.marker)
        done.append({"project": name, "operation_id": receipt.operation_id,
                     "n_positive": counted["n_positive"], "n_cells": counted["n_cells"],
                     "fraction": counted["fraction"], "low": counted["low"],
                     "high": counted["high"]})
        receipts.append(receipt.operation_id)
        call.progress(done=index, total=len(names), message=f"gated {name}")
    fractions = [row["fraction"] for row in done if row["fraction"] is not None]
    summary = {
        "dataset": cohort.name, "marker": inp.marker, "rule": inp.rule, "low": inp.low,
        "high": inp.high, "projects": done, "skipped": skipped,
        "experimental_unit": "image", "n_images": len(done),
        "fraction_positive_per_image": {
            "min": min(fractions), "max": max(fractions),
            "median": sorted(fractions)[len(fractions) // 2]} if fractions else None,
        "note": "each image is one observation; pooled cell counts are not replicates",
    }
    # The cohort-level line: what was asked and which per-image receipts it
    # left. Each image's viewer was told by its own receipt, so this one tells
    # nobody.
    notify, call.notify = call.notify, None
    receipt = make_receipt(call, changed=bool(receipts), before=None,
                           after={"projects": receipts}, persistent_state=STATE,
                           reversible=False, extra={"dataset": cohort.name})
    call.notify = notify
    return {"receipt": receipt.model_dump(mode="json"), **summary}


class SummaryInput(MarkerInput):
    low: float | None = Field(None, description="Lower bound to count with (default: "
                              "the stored gate).")
    high: float | None = Field(None, description="Upper bound (default: the stored one).")


def summary(call, inp):
    _marker(call, inp.marker)
    return model.gated_summary(call.data, inp.marker, inp.low, inp.high)


class SetGateInput(MarkerInput):
    low: float = Field(description="The gate: cells above this are positive, in the "
                       "table's own units.")
    high: float | None = Field(None, description="Upper bound; default keeps the stored one.")
    expected_revision: str | None = Field(
        None, description="The `revision` last read; the write is refused if the gates "
                          "changed since.")
    empty: bool = Field(False, description="Allow low == high: a gate no cell passes (how a "
                                           "failed or all-negative marker is recorded).")


def gate_undo_hint(project, marker, before, expected_revision):
    """The `set_gate` call that puts `before` back. An empty gate (low == high)
    needs `empty`, or the undo would be refused as an invalid range."""
    arguments = {"project": project, "marker": marker, "low": before["low"],
                 "high": before["high"], "expected_revision": expected_revision}
    if before.get("low") is not None and before.get("low") == before.get("high"):
        arguments["empty"] = True
    return {"tool": "set_gate", "arguments": arguments}


def _conflict(exc):
    if isinstance(exc, model.GateLocked):
        return AgentError("conflict", str(exc),
                          detail={"marker": exc.marker, "status": exc.status}, retryable=False)
    return AgentError("conflict", str(exc),
                      detail={"current_revision": exc.current_revision}, retryable=True)


def _record_agent_write(call, ds, marker, after, operation_id, method="agent_set"):
    """A provenance row for a gate an agent set by hand -- or, for an undo,
    the note that the earlier write was rolled back."""
    from plexora.plugins.gating.server.autogate import provenance

    try:
        if call.extras.get("undo_of"):
            provenance.mark_rolled_back(ds.name, marker, operation_id=operation_id)
            return
        provenance.record(ds.name, marker, status="accepted", method=method,
                          value_low=after["low"], value_high=after["high"],
                          written_low=after["low"], written_high=after["high"],
                          operation_id=operation_id, state=None, confidence=None,
                          principal=getattr(call.policy, "principal", None) or "agent")
    except Exception:  # provenance records a write; it never undoes one
        pass


def set_gate(call, inp):
    _marker(call, inp.marker)
    ds = call.data
    revision_before = model.revision(ds)
    current = model.get_gate(ds, inp.marker)
    high = current["high"] if inp.high is None else inp.high
    try:
        before, after, revision_after = model.set_gate(
            ds, inp.marker, inp.low, high, expected_revision=inp.expected_revision,
            empty=inp.empty)
    except (model.GateConflict, model.GateLocked) as exc:
        raise _conflict(exc) from exc
    _record_agent_write(call, ds, inp.marker, after, call.operation_id)
    receipt = make_receipt(
        call, changed=before != after, before=before, after=after,
        revision_before=revision_before, revision_after=revision_after,
        persistent_state=STATE,
        undo_hint=gate_undo_hint(ds.name, inp.marker, before, revision_after))
    return {"receipt": receipt.model_dump(mode="json"),
            "summary": model.gated_summary(ds, inp.marker)}


class ResetInput(ProjectInput):
    markers: list[str] | None = Field(None, max_length=MAX_LIST,
                                      description="Default: every marker of the project.")
    include_approved: bool = Field(False, description="Also reset gates the user approved "
                                   "(never locked ones).")
    clear_provenance: bool = Field(True, description="Also forget how the reset gates were "
                                   "made (the agent's reasons, flags and confidence).")
    expected_revision: str | None = Field(
        None, description="The `revision` last read; the reset is refused if the gates "
                          "changed since.")


def _snapshot_path(project, operation_id):
    from plexora import paths

    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(project))
    return paths.agent_root() / "snapshots" / "gating" / f"{safe}__{operation_id}.json"


def reset_gates(call, inp):
    """Every (or the named) marker back at its full range, through the model,
    with a receipt that tells every open viewer -- a tab holding the old gates
    reloads them instead of saving them back over the reset. The gates and
    their provenance are kept in a snapshot `restore_gates` puts back."""
    import json

    from plexora.plugins.gating.server.autogate import provenance

    ds = call.data
    revision_before = model.revision(ds)
    rows_before = model.gate_rows(ds)
    prov_before = provenance.read(ds.name)
    before = [g["marker"] for g in model.all_gates(ds) if g["thresholded"]]
    try:
        reset, skipped, revision_after = model.reset_gates(
            ds, inp.markers, expected_revision=inp.expected_revision,
            include_approved=inp.include_approved)
    except model.GateConflict as exc:
        raise _conflict(exc) from exc
    except KeyError as exc:
        raise AgentError("invalid_input", str(exc).strip("'\""),
                         detail={"markers": list(ds.table.markers)}) from exc
    cleared = []
    if inp.clear_provenance:
        kept = {m: r for m, r in prov_before.items() if m not in reset}
        cleared = sorted(set(prov_before) - set(kept))
        if cleared:
            provenance._write(ds.name, kept)
    target = _snapshot_path(ds.name, call.operation_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"project": ds.name, "rows": rows_before,
                                  "provenance": {m: prov_before[m] for m in cleared}},
                                 default=str), encoding="utf-8")
    after = [g["marker"] for g in model.all_gates(ds) if g["thresholded"]]
    changed = revision_after != revision_before or bool(cleared)
    receipt = make_receipt(
        call, changed=changed, before={"thresholded": before},
        after={"thresholded": after, "reset": reset, "skipped": skipped,
               "provenance_cleared": cleared},
        revision_before=revision_before, revision_after=revision_after,
        persistent_state=STATE,
        undo_hint={"tool": "restore_gates",
                   "arguments": {"project": ds.name, "snapshot": call.operation_id,
                                 "expected_revision": revision_after}})
    return {"receipt": receipt.model_dump(mode="json"), "reset": reset, "skipped": skipped,
            "provenance_cleared": cleared, "revision": revision_after}


class RestoreInput(ProjectInput):
    snapshot: str = Field(description="The operation id of the reset_gates call to undo.")
    expected_revision: str | None = None


def restore_gates(call, inp):
    import json

    from plexora.plugins.gating.server.autogate import provenance

    ds = call.data
    target = _snapshot_path(ds.name, inp.snapshot)
    if not target.is_file():
        raise AgentError("invalid_input", f"no gate snapshot for {inp.snapshot!r}",
                         detail={"path": str(target)})
    saved = json.loads(target.read_text(encoding="utf-8"))
    revision_before = model.revision(ds)
    try:
        revision_after = model.put_rows(ds, saved["rows"],
                                        expected_revision=inp.expected_revision)
    except model.GateConflict as exc:
        raise _conflict(exc) from exc
    if saved.get("provenance"):
        rows = provenance.read(ds.name)
        rows.update(saved["provenance"])
        provenance._write(ds.name, rows)
    receipt = make_receipt(
        call, changed=revision_after != revision_before or bool(saved.get("provenance")),
        before=None, after={"snapshot": inp.snapshot,
                            "thresholded": [g["marker"] for g in model.all_gates(ds)
                                            if g["thresholded"]]},
        revision_before=revision_before, revision_after=revision_after,
        persistent_state=STATE, reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), "revision": revision_after}


class AdjustInput(MarkerInput):
    direction: Literal["up", "down"] = Field(
        description="`up` when too many cells are called positive (gate too low), "
                    "`down` when real positives are being missed (gate too high).")
    magnitude: Literal["small", "medium", "large"] = Field(
        description="How far: 10%, 25% or 50% of the way to the neighbouring "
                    "population's centre.")
    expected_revision: str | None = None


def adjust(call, inp):
    _marker(call, inp.marker)
    ds = call.data
    revision_before = model.revision(ds)
    summary_before = model.gated_summary(ds, inp.marker)
    try:
        before, after, revision_after, reason = model.adjust_gate(
            ds, inp.marker, inp.direction, inp.magnitude,
            expected_revision=inp.expected_revision)
    except (model.GateConflict, model.GateLocked) as exc:
        raise _conflict(exc) from exc
    _record_agent_write(call, ds, inp.marker, after, call.operation_id)
    summary_after = model.gated_summary(ds, inp.marker)
    receipt = make_receipt(
        call, changed=before != after, before=before, after=after,
        revision_before=revision_before, revision_after=revision_after,
        persistent_state=STATE,
        undo_hint=gate_undo_hint(ds.name, inp.marker, before, revision_after),
        extra={"reason": reason})
    return {"receipt": receipt.model_dump(mode="json"), "reason": reason,
            "summary_before": summary_before, "summary_after": summary_after,
            "delta_positive": summary_after["n_positive"] - summary_before["n_positive"]}


class WriteSourceInput(ProjectInput):
    confirm: Literal[True] = Field(description="Must be true, and only once the user has "
                                   "explicitly asked for the gates to be written into "
                                   "their file.")
    table_name: str = Field("gates", description="The `uns` key the gates are written under.")
    include_provenance: bool = Field(True, description="Also write where each gate came "
                                     "from (method, status, confidence) as "
                                     "`uns[<table_name>_provenance]`.")


def write_source(call, inp):
    ds = call.data
    if ds.source_kind not in ("anndata", "spatialdata"):
        raise AgentError("precondition_missing",
                         "gates can only be written into an AnnData or SpatialData file; "
                         f"this project's table is {ds.source_kind!r}",
                         detail={"missing": [{"key": "table", "label":
                                              "an AnnData or SpatialData table"}]})
    if not ds.schema.image_id:
        raise AgentError("precondition_missing",
                         "no image-id column is recorded for this project",
                         detail={"missing": [{"key": "role:image_id",
                                              "label": "Image ID column"}]})
    gates = {channel: low for channel, (low, _high) in model.active_gates(ds).items()}
    payload = {"image_id": ds.name, "gates": gates, "table_name": inp.table_name,
               "imageid_column": ds.schema.image_id}
    if inp.include_provenance:
        from plexora.plugins.gating.server.autogate import report

        payload["provenance"] = report.provenance_long(call, [ds.name])
    result = ds.table.run("gating.save_gates", payload)
    if not result.get("ok"):
        raise AgentError("invalid_input", result.get("message") or "the write was refused",
                         detail=result)
    source = ds.table.source
    receipt = make_receipt(
        call, changed=True, before=None,
        after={"gates": gates, "table_name": inp.table_name},
        persistent_state="source_file", source_file_modified=True,
        source_path=source.path if source else None, reversible=False)
    return {"receipt": receipt.model_dump(mode="json"),
            "written": {k: v for k, v in result.items() if k != "ok"}}


# -- visual validation -------------------------------------------------------

FieldClass = Literal["clear_negative", "clear_positive", "borderline", "high_density",
                     "low_density", "bright_isolated", "edge"]


class SampleInput(MarkerInput):
    low: float | None = Field(None, description="Gate to check (default: the stored one).")
    high: float | None = None
    field_size_um: float | None = Field(None, gt=0, description="Field side in microns "
                                        "(default: the marker-validation preset's, when the "
                                        "image is calibrated).")
    field_size_px: float | None = Field(None, gt=0, description="Field side in pixels "
                                        "(default: the preset's pixel fallback, when it is "
                                        "not).")
    classes: list[FieldClass] = Field(
        default_factory=lambda: ["clear_negative", "clear_positive", "borderline"],
        description="Which kinds of field to pick.")
    n_per_class: int = Field(2, ge=1, le=4)
    seed: int = 0


def _gate_and_field(call, inp):
    from plexora.agent import presets
    from plexora.server.utils import pixel_scale

    preset_um, preset_px = presets.field_size("marker_validation")
    ds = call.data
    gate = model.get_gate(ds, inp.marker)
    low = gate["low"] if inp.low is None else inp.low
    high = gate["high"] if inp.high is None else inp.high
    pixel = pixel_scale.pixel_size(ds.project)
    if inp.field_size_px is not None:
        field_px, how = inp.field_size_px, "field_size_px"
    elif inp.field_size_um is not None or pixel:
        if not pixel:
            raise AgentError("precondition_missing", "field_size_um needs a calibrated image",
                             detail={"missing": [{"key": "pixel_size", "label": "Pixel size"}]})
        um = inp.field_size_um or float(preset_um)
        field_px, how = um / pixel["value"], f"{um:g} µm at {pixel['value']:.4g} µm/px"
    else:
        field_px, how = float(preset_px), f"{preset_px:g} px (the image is uncalibrated)"
    return ds, gate, low, high, field_px, how


def fit_band(ds, marker, low):
    """The borderline band from the mixture fit: the gate plus or minus half the
    narrower spread of the two populations it separates, in the fit's space.
    None when there is no fit (the sampler then uses a percentile band)."""
    import numpy as np

    fit = model.fit_for(ds, marker)
    if fit is None or len(fit["sds"]) < 2:
        return None
    half = 0.5 * min(fit["sds"][-2:])
    if fit["fitted_in_log"]:
        g = float(np.log1p(max(low, 0.0)))
        return float(np.expm1(g - half)), float(np.expm1(g + half))
    return float(low - half), float(low + half)


def sample_regions(call, inp):
    from plexora.agent.gate_sampling import sample_gate_validation_regions

    ds, gate, low, high, field_px, how = _gate_and_field(call, inp)
    width, height = ds.image.size
    result = sample_gate_validation_regions(
        ds, inp.marker, low, high, field_px=field_px, image_size=(width or 0, height or 0),
        n_per_class=inp.n_per_class, classes=tuple(inp.classes), seed=inp.seed,
        band=fit_band(ds, inp.marker, low))
    if result["band"]["how"] == "explicit":
        result["band"]["how"] = "gate ± half the narrower fitted population's sd"
    result["field_size"] = how
    result["gate_source"] = "explicit" if inp.low is not None else (
        "stored_gate" if gate["thresholded"] else "default_range")
    return result


class ValidationInput(SampleInput):
    fields: list[dict] | None = Field(
        None, description="Fields from `sample_gate_validation_regions` (each with "
                          "`bounds`); sampled here when omitted.")
    context_marker: str | None = Field(None, description="A second channel drawn in cyan "
                                       "in panel B, e.g. a lineage marker.")
    nuclear_channel: str | None = None
    panel_px: int = Field(480, ge=160, le=528)
    max_fields: int = Field(6, ge=1, le=12)


def render_validation(call, inp):
    from plexora.agent.gate_panel import render_gate_validation
    from plexora.agent.gate_sampling import sample_gate_validation_regions

    ds, gate, low, high, field_px, how = _gate_and_field(call, inp)
    width, height = ds.image.size
    sampled = sample_gate_validation_regions(
        ds, inp.marker, low, high, field_px=field_px, image_size=(width or 0, height or 0),
        n_per_class=inp.n_per_class, classes=tuple(inp.classes), seed=inp.seed,
        band=fit_band(ds, inp.marker, low))
    if sampled["band"]["how"] == "explicit":
        sampled["band"]["how"] = "gate ± half the narrower fitted population's sd"
    fields = inp.fields or sampled["fields"]
    for field in fields:
        if "bounds" not in field:
            raise AgentError("invalid_input", "every field needs `bounds` "
                             "({x, y, width, height})")
    fields = fields[:inp.max_fields]
    fit = model.gmm_for(ds, inp.marker)
    curves = {"background": fit.get("gmm_1") or [], "positive": fit.get("gmm_2") or []}
    band = (sampled["band"]["low"], sampled["band"]["high"])
    panels, context = render_gate_validation(
        call.session, ds, inp.marker, low, high, fields, band=band, curves=curves,
        context_marker=inp.context_marker, nuclear=inp.nuclear_channel,
        panel_px=inp.panel_px)
    return {
        "marker": inp.marker,
        "gate": {"low": low, "high": high},
        "band": sampled["band"],
        "field_size": how,
        "dataset": sampled["dataset"],
        "context": context,
        "fields": [record for _png, record in panels],
        "response_schema": {
            "per_field": {"assessment": ["too_low", "about_right", "too_high",
                                         "cannot_tell", "segmentation_problem",
                                         "image_quality_problem"],
                          "magnitude": ["small", "medium", "large", None],
                          "confidence": "0-1", "reason": "one sentence",
                          "request_marker": "optional channel to add",
                          "request_more_regions": "bool"},
            "then": "adjust_gate with direction up (gate too low) or down (too high)"},
        "_images": [png for png, _record in panels],
    }


def capabilities():
    from plexora.plugins.gating import capabilities_autogate

    requires = PLUGIN.requires
    tags = ("gate", "gating", "threshold", "positive", "negative", "marker", "cutoff")

    def cap(**kwargs):
        kwargs.setdefault("reads", ("table", "gates"))
        return Capability(owner=OWNER, requires=requires, version="1", **kwargs)

    return [
        cap(name="gating.get", tool_name="get_gate",
            purpose="One marker's stored gate (lower/upper bound, whether it was ever "
                    "narrowed from the full range) and how many cells it calls positive.",
            permission="read", input_model=MarkerInput, handler=get_gate,
            egress="aggregates", tags=tags),
        cap(name="gating.get_all", tool_name="get_all_gates",
            purpose="Every marker's gate, and which markers have been thresholded.",
            permission="read", input_model=ProjectInput, handler=get_all, tags=tags),
        cap(name="gating.distribution", tool_name="get_gate_distribution",
            purpose="A marker's histogram with the fitted background and positive "
                    "population curves and the gate the fit implies.",
            permission="read", input_model=MarkerInput, handler=distribution,
            egress="aggregates", tags=tags + ("distribution", "histogram", "gmm")),
        cap(name="gating.auto", tool_name="suggest_auto_gate",
            purpose="Compute (but do not store) the automatic gate for a marker from a "
                    "3-component mixture fit, with the positive count it would give.",
            permission="read", input_model=MarkerInput, handler=auto,
            egress="aggregates", tags=tags + ("auto", "automatic", "suggest")),
        cap(name="gating.summary", tool_name="get_gated_summary",
            purpose="How many cells a range calls positive -- the stored gate, or a "
                    "candidate one -- without storing anything.",
            permission="read", input_model=SummaryInput, handler=summary,
            egress="aggregates", tags=tags + ("count", "fraction")),
        cap(name="gating.set", tool_name="set_gate",
            purpose="Store a marker's gate in Plexora (the sidebar's own state). "
                    "Reversible; returns a receipt with the previous gate. Never touches "
                    "the source file.",
            permission="reversible_write", input_model=SetGateInput, handler=set_gate,
            writes=("gates",), persistent=True, egress="aggregates", tags=tags + ("set",)),
        cap(name="gating.reset", tool_name="reset_gates",
            purpose="Put every gate of a project (or the named markers) back at its full "
                    "range and forget how they were made -- before re-running automatic "
                    "gating from scratch. Locked gates are never touched, approved ones "
                    "only on request. Open viewers reload the reset gates. Reversible "
                    "(restore_gates, through undo_operation).",
            permission="reversible_write", input_model=ResetInput, handler=reset_gates,
            writes=("gates",), persistent=True, egress="aggregates",
            tags=tags + ("reset", "clear", "remove", "start over")),
        cap(name="gating.restore", tool_name="restore_gates",
            purpose="Put back the gates and provenance a reset_gates call replaced (its "
                    "undo). Use undo_operation on the reset's receipt rather than calling "
                    "this directly.",
            permission="reversible_write", input_model=RestoreInput, handler=restore_gates,
            writes=("gates",), persistent=True, egress="aggregates",
            tags=tags + ("restore", "undo")),
        cap(name="gating.adjust", tool_name="adjust_gate",
            purpose="Move a marker's gate one qualitative step (small/medium/large, up or "
                    "down) by a fixed, reproducible rule, and store it. For acting on a "
                    "visual judgement without choosing a number.",
            permission="reversible_write", input_model=AdjustInput, handler=adjust,
            writes=("gates",), persistent=True, egress="aggregates",
            tags=tags + ("adjust", "raise", "lower", "move")),
        cap(name="gating.apply_to_dataset", tool_name="apply_gate_to_dataset",
            purpose="Apply one marker's gate to every image in a dataset (a job): one "
                    "receipt per image, and the positive fraction reported per image with "
                    "its spread -- the image is the experimental unit.",
            permission="reversible_write", input_model=DatasetGateInput,
            handler=apply_to_dataset, writes=("gates",), persistent=True,
            execution="job", egress="aggregates",
            tags=tags + ("dataset", "cohort", "apply")),
        cap(name="gating.write_source", tool_name="write_gates_to_source",
            purpose="Write every thresholded gate into the source AnnData/SpatialData "
                    "file's `uns`. Modifies the user's file: needs --allow-source-writes "
                    "and confirm: true, only on the user's explicit request.",
            permission="source_file_write", input_model=WriteSourceInput,
            handler=write_source, writes=("source_file",), reversible=False,
            source_file_write=True, persistent=True, remote_safe=True,
            tags=tags + ("save", "write", "anndata", "export")),
        cap(name="gating.sample_validation_regions", tool_name="sample_gate_validation_regions",
            purpose="Pick fields of the tissue to check a gate in -- clearly negative, "
                    "clearly positive, borderline (most cells near the threshold), and "
                    "optionally dense, sparse, bright-isolated or edge fields -- with "
                    "per-field counts. Deterministic; nothing is rendered or changed.",
            permission="read", input_model=SampleInput, handler=sample_regions,
            egress="aggregates", tags=tags + ("sample", "fields", "regions", "check",
                                             "validate", "verify")),
        cap(name="gating.render_validation", tool_name="render_gate_validation",
            purpose="Render a gate check per field: A the raw marker, B the marker over "
                    "nuclear grey with outlines and the gate's positive cells in magenta, "
                    "C where the field's cells fall on the whole distribution -- with "
                    "counts and the borderline cells by id. For judging a gate by eye.",
            permission="read", input_model=ValidationInput, handler=render_validation,
            visual_output=True, egress="rendered_pixels", reads=("table", "gates", "image",
                                                                 "mask"),
            tags=tags + ("render", "visual", "look", "check", "validate", "verify",
                         "inspect", "image")),
        *capabilities_autogate.capabilities(),
    ]
