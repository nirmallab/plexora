"""Automatic gating's tools: the analytical primitives and the gating session.

Two levels, each usable on its own:

- **Analytical operations** answer one question with compact evidence --
  a marker's profile (how hard is it, and why), a display calibration, a
  stratified cell sample, a pixel-budgeted collage, a two-marker check, a
  panel-wide QC. An agent diagnosing one marker by hand uses these.
- **The workflow** (`gating_session_*`, `gating_next`, `gating_answer`) runs
  a whole image or dataset: the server does every deterministic step and
  hands the agent one small decision packet at a time.

Loaded with the rest of gating's capabilities (`capabilities.capabilities()`).
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
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import tableops

OWNER = "gating"
TAGS = ("gate", "gating", "threshold", "positive", "negative", "marker", "cutoff", "auto",
        "automatic")


class MarkerInput(ProjectInput):
    marker: str = Field(description="A marker column, as `list_markers` names it.")


def _marker(call, marker):
    if marker not in call.data.table.markers:
        raise AgentError("invalid_input",
                         f"{marker!r} is not a marker of {call.project_name!r}",
                         detail={"markers": call.data.table.markers[:MAX_LIST]})
    return marker


def resolve_gate(call, marker, low=None, high=None):
    """(low, high, source): the given bounds, else the stored gate, else the
    GMM proposal for a marker that was never thresholded."""
    ds = call.data
    stored = model.get_gate(ds, marker)
    source = "explicit"
    if low is None:
        if stored["thresholded"]:
            low, source = stored["low"], "stored_gate"
        else:
            fit = model.fit_for(ds, marker)
            if fit is None:
                raise AgentError("invalid_input", f"{marker!r} has no gate and no mixture to "
                                 "fit one from; give `low`")
            low, source = fit["gate"], "gmm_proposal"
    if high is None:
        high = stored["high"]
    return float(low), float(high), source


def _images(*rendered):
    from plexora.plugins.gating.server.autogate.views import image_payload

    return [image_payload(r) for r in rendered if r is not None]


# -- profile ------------------------------------------------------------------


class ProfileInput(MarkerInput):
    seed: int = Field(0, description="Seed for every subsample; the same seed gives the "
                                     "same profile.")
    with_image_qc: bool = Field(True, description="Also read the marker's overview for "
                                "saturation, empty-channel and illumination checks.")


def profile_marker(call, inp):
    from plexora.plugins.gating.server.autogate import context, views

    _marker(call, inp.marker)
    ds = call.data
    if ds.table.is_local:
        profile = views.full_profile(call.session, ds, inp.marker, seed=inp.seed,
                                     with_image=inp.with_image_qc)
    else:
        profile = tableops.local_or_node(ds, "gating.autogate.profile",
                                         {"marker": inp.marker, "seed": inp.seed})
        if inp.with_image_qc:
            from plexora.plugins.gating.server.autogate import profile as profmod

            profile["image_qc"] = views.image_qc_for(call.session, ds, inp.marker)
            profile = profmod.score(profile)
    panel = context.for_project(ds)
    gate = model.get_gate(ds, inp.marker)
    return tableops.jsonable({
        "profile": profile,
        "current_gate": gate,
        "gmm_proposal": (profile.get("fit") or {}).get("gate_raw"),
        "context": panel["entries"].get(inp.marker),
        "next": ("accept the GMM gate (set_gate) when t1.accept is true; otherwise look: "
                 "render_gating_collage layout=t2, or start a gating session"),
    })


# -- display calibration ------------------------------------------------------


class CalibrateInput(ProjectInput):
    channels: list[str] | None = Field(None, description="Channels to calibrate (default "
                                       "every image channel).")
    force: bool = Field(False, description="Recompute even when a calibration is stored.")


def calibrate_display(call, inp):
    from plexora.agent.evidence import calibration

    before = calibration.revision(inp.project)
    record, changed = calibration.calibrate(call.session, inp.project, inp.channels,
                                            force=inp.force)
    after = calibration.revision(inp.project)
    receipt = make_receipt(
        call, changed=changed, before={"revision": before}, after={"revision": after},
        revision_before=before, revision_after=after,
        persistent_state="plugin_store:display", reversible=False,
        extra={"note": "display calibration is derived and recomputable; it never "
                       "changes the user's channel list"})
    return {"receipt": receipt.model_dump(mode="json"),
            "calibration": {name: {k: v for k, v in entry.items() if k != "stats"}
                            for name, entry in record["channels"].items()},
            "level": record.get("level"),
            "needs_vision_check": calibration.needs_vision_check(record)}


# -- cells --------------------------------------------------------------------


class SampleCellsInput(MarkerInput):
    low: float | None = Field(None, description="Gate to sample around (default: stored "
                                                "gate, else the GMM proposal).")
    high: float | None = None
    mode: Literal["strata", "delta"] = Field(
        "strata", description="strata: cells in bands around the gate, spread over the "
                              "slide; delta: the cells that flip between `candidates`.")
    candidates: list[float] | None = Field(None, description="For delta: thresholds, the "
                                           "current one included.")
    n_per_stratum: int = Field(6, ge=1, le=12)
    seed: int = 0


def sample_cells(call, inp):
    _marker(call, inp.marker)
    low, high, source = resolve_gate(call, inp.marker, inp.low, inp.high)
    if inp.mode == "delta":
        if not inp.candidates or len(inp.candidates) < 2:
            raise AgentError("invalid_input", "delta needs at least two candidate thresholds")
        result = tableops.local_or_node(call.data, "gating.autogate.delta", {
            "marker": inp.marker, "candidates": inp.candidates, "high": high,
            "seed": inp.seed})
    else:
        result = tableops.local_or_node(call.data, "gating.autogate.sample", {
            "marker": inp.marker, "low": low, "high": high,
            "n_per_stratum": inp.n_per_stratum, "seed": inp.seed})
    result["gate_source"] = source
    return result


# -- collages -----------------------------------------------------------------

LAYOUT_CHOICES = Literal["t2", "t3", "strata", "flips", "overview", "quadrants"]


class CollageInput(MarkerInput):
    layout: LAYOUT_CHOICES = Field(
        "t2", description="t2: cells below/at/above the gate, 4 panels each (~450 vision "
                          "tokens); t3: the same with a reference channel; strata: every "
                          "band plus spatially inconsistent cells; flips: the cells between "
                          "candidate thresholds; overview: the whole image with positives "
                          "marked; quadrants: cells from each quadrant of marker vs partner. "
                          "(Whole fields of tissue: render_gate_validation.)")
    low: float | None = Field(None, description="The gate (default: stored, else GMM).")
    high: float | None = None
    candidates: list[float] | None = Field(None, description="flips: thresholds, the "
                                           "current one included.")
    references: list[str] | None = Field(None, description="t3: reference channels "
                                         "(default: the panel context's partners).")
    partner: str | None = Field(None, description="quadrants: the second marker.")
    partner_low: float | None = None
    tile_px: int = Field(64, ge=32, le=128)
    format: Literal["webp", "png"] = "webp"
    seed: int = 0


def render_collage(call, inp):
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import context, views

    _marker(call, inp.marker)
    ds = call.data
    low, high, source = resolve_gate(call, inp.marker, inp.low, inp.high)
    channel = views.image_channel(ds, inp.marker)
    if channel is None:
        raise AgentError("precondition_missing",
                         f"{inp.marker!r} is a table column with no image channel, so there "
                         "is nothing to look at", detail={"channels": views.channel_names(ds)})
    title = f"{inp.marker} {inp.layout} - gate {collage.compact_number(low)} ({source})"
    if inp.layout == "overview":
        rendered = collage.render_overview(call.session, ds, marker=channel,
                                           points=views.positive_points(ds, inp.marker, low,
                                                                        high),
                                           low=low, high=high, fmt=inp.format, title=title)
    elif inp.layout in ("t2", "t3", "strata"):
        sample = tableops.local_or_node(ds, "gating.autogate.sample", {
            "marker": inp.marker, "low": low, "high": high, "seed": inp.seed})
        rows = (views.strata_rows(sample, low, high) if inp.layout == "strata"
                else views.t2_rows(sample, low, high))
        references = inp.references
        if inp.layout == "t3" and not references:
            panel = context.for_project(ds)
            references = [p["marker"] for p in
                          (panel["entries"].get(inp.marker) or {}).get("partners", [])][:2]
        col_fit = sample.get("fit_space") == "log1p"
        rendered = collage.render_collage(
            call.session, ds, layout=inp.layout, rows=rows, marker=channel, gate=low,
            high=high, references=[views.image_channel(ds, r) or r for r in references or []],
            tile_px=inp.tile_px, fmt=inp.format, title=title, to_log=col_fit)
    elif inp.layout == "flips":
        if not inp.candidates or len(inp.candidates) < 2:
            raise AgentError("invalid_input", "flips needs at least two thresholds")
        delta = tableops.local_or_node(ds, "gating.autogate.delta", {
            "marker": inp.marker, "candidates": inp.candidates, "high": high,
            "seed": inp.seed})
        rendered = collage.render_collage(
            call.session, ds, layout="flips", rows=views.flip_rows(delta), marker=channel,
            gate=low, high=high, tile_px=inp.tile_px, fmt=inp.format, title=title)
    else:  # quadrants
        if not inp.partner:
            raise AgentError("invalid_input", "quadrants needs a partner marker")
        _marker(call, inp.partner)
        p_low, _p_high, _src = resolve_gate(call, inp.partner, inp.partner_low, None)
        quads = tableops.local_or_node(ds, "gating.autogate.quadrants", {
            "a": inp.marker, "gate_a": low, "b": inp.partner, "gate_b": p_low,
            "seed": inp.seed})
        partner_channel = views.image_channel(ds, inp.partner)
        if partner_channel is None:
            raise AgentError("precondition_missing", f"{inp.partner!r} has no image channel")
        rows = [{"key": name, "label": name.replace("_", " "),
                 "cells": [dict(c, value=c[inp.marker]) for c in cells]}
                for name, cells in quads.items()]
        rendered = collage.render_collage(
            call.session, ds, layout="quadrants", rows=rows, marker=channel, a=channel,
            b=partner_channel, gate=low, tile_px=inp.tile_px, fmt=inp.format,
            title=f"{inp.marker} (left) vs {inp.partner} (right)")
    manifest = rendered["manifest"]
    return {"artifact": rendered["artifact"], "gate": {"low": low, "high": high,
                                                        "source": source},
            "manifest": manifest, "format": rendered["format"],
            "estimated_vision_tokens": manifest.get("estimated_vision_tokens"),
            "_images": _images(rendered)}


# -- two markers --------------------------------------------------------------


class BivariateInput(MarkerInput):
    partner: str = Field(description="The second marker.")
    low: float | None = None
    partner_low: float | None = None
    relation: Literal["auto", "subset", "coexpressed", "exclusive", "independent"] = Field(
        "auto", description="How the two should relate; auto takes the panel context's.")
    include_image: Literal["never", "if_anomalous", "always"] = "if_anomalous"


def _relation(ds, marker, partner):
    from plexora.plugins.gating.server.autogate import context

    panel = context.for_project(ds)
    for entry in (panel["entries"].get(marker) or {}).get("partners", []):
        if entry["marker"] == partner:
            return entry["relation"]
    for entry in (panel["entries"].get(partner) or {}).get("partners", []):
        if entry["marker"] == marker:
            return {"subset": "coexpressed"}.get(entry["relation"], entry["relation"])
    return "independent"


def bivariate_evidence(call, inp):
    from plexora.agent import artifacts
    from plexora.agent.evidence import collage, density_plot
    from plexora.server.utils import fast_png

    _marker(call, inp.marker)
    _marker(call, inp.partner)
    ds = call.data
    low, _high, source = resolve_gate(call, inp.marker, inp.low)
    p_low, _p_high, p_source = resolve_gate(call, inp.partner, inp.partner_low)
    relation = _relation(ds, inp.marker, inp.partner) if inp.relation == "auto" \
        else inp.relation
    result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
        "a": inp.marker, "gate_a": low, "b": inp.partner, "gate_b": p_low,
        "relation": relation})
    result["gate_sources"] = {"a": source, "b": p_source}
    draw = inp.include_image == "always" or (inp.include_image == "if_anomalous"
                                             and result["plot_recommended"])
    out = {k: v for k, v in result.items() if k != "density"}
    if draw:
        import numpy as np

        from plexora.plugins.gating.server.autogate import profile as profmod

        log_axes = profmod.column(ds, inp.marker).to_log
        image = density_plot.draw_density(result, log_axes=log_axes)
        png = fast_png.encode_rgb8_png(np.asarray(image))
        webp, fmt = collage.encode(image, "webp")
        manifest = {"kind": "plexora.gating_bivariate", "project": ds.name,
                    "a": inp.marker, "b": inp.partner, "gates": result["gates"],
                    "relation": relation, "quadrants": result["quadrants"],
                    "estimated_vision_tokens": collage.estimated_tokens(*image.size),
                    "egress": "rendered_pixels"}
        out["artifact"] = artifacts.put(ds.name, png, manifest, kind="gating_bivariate")
        out["_images"] = [{"data": webp, "format": fmt}]
    return out


# -- panel QC -----------------------------------------------------------------


def gating_qc(call, inp):
    from plexora.plugins.gating.server.autogate import context, provenance

    ds = call.data
    panel = context.for_project(ds)
    active = model.active_gates(ds)
    rows = provenance.read(ds.name)
    fractions = {}
    for marker, (low, high) in active.items():
        summary = model.gated_summary(ds, marker, low, high)
        fractions[marker] = {"low": low, "fraction": summary["fraction"],
                             "n_positive": summary["n_positive"],
                             "method": (rows.get(marker) or {}).get("method"),
                             "status": (rows.get(marker) or {}).get("status"),
                             "confidence": (rows.get(marker) or {}).get("confidence")}
    pairs = []
    seen = set()
    for marker in active:
        for partner in (panel["entries"].get(marker) or {}).get("partners", []):
            other = partner["marker"]
            if other not in active or (other, marker) in seen:
                continue
            seen.add((marker, other))
            result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
                "a": marker, "gate_a": active[marker][0], "b": other,
                "gate_b": active[other][0], "relation": partner["relation"],
                "with_grid": False})
            pairs.append({"a": marker, "b": other, "relation": partner["relation"],
                          "contradiction": result["contradiction"],
                          "quadrants": result["quadrants"],
                          "adjacent_orphan_share": result["adjacent_orphan_share"]})
    pairs.sort(key=lambda p: -p["contradiction"])
    needs_review = sorted({m for p in pairs if p["contradiction"] >= 0.5 for m in (p["a"],
                                                                                   p["b"])}
                          | {m for m, r in rows.items()
                             if r.get("state") in ("manual_review_recommended",
                                                   "accepted_low_confidence",
                                                   "technically_failed")})
    return {"project": ds.name, "gated": fractions, "pairs": pairs[:MAX_LIST],
            "needs_review": needs_review, "experimental_unit": "cell (one image)",
            "not_gated": [m for m in ds.table.markers if m not in active][:MAX_LIST]}


# -- panel context ------------------------------------------------------------


class ContextInput(AgentModel):
    project: str = Field(description="A project whose panel to describe.")


def get_panel_context(call, inp):
    from plexora.plugins.gating.server.autogate import context

    call.project_name = inp.project
    panel = context.for_project(call.session.data(inp.project))
    return {**panel, "revision": context.revision(panel),
            "rules": "vocabulary entries outrank agent-filled ones; context chooses "
                     "references and order but never moves a gate"}


class MarkerContext(AgentModel):
    marker: str
    role: Literal["context", "lineage_reliable", "lineage_other", "tumour_stromal", "state",
                  "signalling"] | None = None
    compartment: Literal["nuclear", "cytoplasmic", "membrane", "nuclear_cytoplasmic",
                         "extracellular"] | None = None
    lineage: str | None = Field(None, max_length=120)
    binary: bool | None = None
    partners: list[dict] | None = Field(None, description="[{marker, relation: subset|"
                                        "coexpressed|exclusive, confidence: high|moderate|"
                                        "low}] -- markers of THIS panel only.")
    caveats: list[str] | None = None
    expected_fraction: list[float] | None = Field(None, min_length=2, max_length=2)


class SetContextInput(AgentModel):
    project: str
    entries: list[MarkerContext] = Field(max_length=100)
    source: Literal["user", "ai"] = Field("ai", description="user: the scientist said so "
                                          "(outranks the vocabulary); ai: your own "
                                          "knowledge, used only for markers the vocabulary "
                                          "does not know.")
    expected_revision: str | None = None


def set_panel_context(call, inp):
    from plexora.plugins.gating.server.autogate import context

    call.project_name = inp.project
    ds = call.session.data(inp.project)
    panel = context.for_project(ds)
    before = context.revision(panel)
    if inp.expected_revision and inp.expected_revision != before:
        raise AgentError("conflict", "the panel context changed since it was read",
                         detail={"current_revision": before}, retryable=True)
    applied, suggestions = [], []
    for entry in inp.entries:
        fields = entry.model_dump(exclude_none=True)
        marker = fields.pop("marker")
        try:
            result = context.apply_entry(panel, marker, fields, source=inp.source)
        except (KeyError, ValueError) as exc:
            raise AgentError("invalid_input", str(exc)) from None
        (applied if result.get("source") == inp.source else suggestions).append(marker)
    after = context.save(panel)
    receipt = make_receipt(call, changed=before != after, before={"revision": before},
                           after={"revision": after, "applied": applied},
                           revision_before=before, revision_after=after,
                           persistent_state="config", reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), "applied": applied,
            "kept_as_suggestions": suggestions, "unresolved": panel["unresolved"],
            "order": panel["order"]}


# -- statuses -----------------------------------------------------------------


class StatusInput(MarkerInput):
    status: Literal["approved", "locked", "unlocked", "excluded", "included",
                    "unapproved"] = Field(
        description="approved: signed off (agents will not overwrite it); locked: nobody "
                    "but the user changes it, and the sidebar's own edits are reverted; "
                    "excluded: automatic runs skip it.")
    note: str | None = Field(None, max_length=300)


def set_gate_status(call, inp):
    from plexora.plugins.gating.server.autogate import provenance

    _marker(call, inp.marker)
    ds = call.data
    gate = model.get_gate(ds, inp.marker)
    revision_before = provenance.revision(ds.name)
    before, after = provenance.set_status(
        ds.name, inp.marker, inp.status, note=inp.note,
        principal=getattr(call.policy, "principal", None) or "agent",
        current=(gate["low"], gate["high"]))
    revision_after = provenance.revision(ds.name)
    undo = {"approved": "unapproved", "locked": "unlocked", "excluded": "included"}.get(after)
    if before in ("approved", "locked", "excluded") and after not in (before,):
        undo = before
    receipt = make_receipt(
        call, changed=before != after, before={"status": before}, after={"status": after},
        revision_before=revision_before, revision_after=revision_after,
        persistent_state="plugin_store:gating_provenance",
        undo_hint=({"tool": "set_gate_status", "arguments": {
            "project": ds.name, "marker": inp.marker, "status": undo}} if undo else None))
    return {"receipt": receipt.model_dump(mode="json"), "marker": inp.marker,
            "status": after, "previous": before}


def get_provenance(call, inp):
    from plexora.plugins.gating.server.autogate import provenance

    return {"provenance": provenance.read(call.data.name),
            "revision": provenance.revision(call.data.name)}


def capabilities():
    requires = PLUGIN.requires

    def cap(**kwargs):
        kwargs.setdefault("reads", ("table", "gates"))
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, requires=requires, version="1", **kwargs)

    from plexora.plugins.gating import capabilities_session

    return [
        cap(name="gating.profile_marker", tool_name="profile_marker",
            purpose="How hard a marker is to gate, deterministically: five threshold "
                    "estimators and how far apart they land, population separation, "
                    "stability under resampling, distribution class, technical QC (cell "
                    "table and image overview) and a first-tier score that says whether "
                    "the GMM gate can be accepted without looking. Nothing is written.",
            permission="read", input_model=ProfileInput, handler=profile_marker,
            egress="aggregates", reads=("table", "gates", "image"),
            tags=TAGS + ("profile", "qc", "quality", "distribution", "bimodal")),
        cap(name="gating.calibrate_display", tool_name="calibrate_display",
            purpose="Compute and store the project's display calibration (per-channel "
                    "windows and colours, deterministic from the image overview) that "
                    "headless renders and a mirrored viewer both draw with; lists the "
                    "channels whose calibration raised a flag worth one look. Never "
                    "changes the user's channel list.",
            permission="reversible_write", input_model=CalibrateInput,
            handler=calibrate_display, writes=("display",), reads=("image",),
            egress="aggregates", tags=TAGS + ("display", "contrast", "window", "calibrate")),
        cap(name="gating.sample_cells", tool_name="sample_gating_cells",
            purpose="Which cells to look at for a gate: cells in bands around it "
                    "(oversampled at the boundary, spread over the slide and across dense "
                    "and sparse areas, plus spatially inconsistent ones), or the cells that "
                    "flip between candidate thresholds. Ids, positions and values only.",
            permission="read", input_model=SampleCellsInput, handler=sample_cells,
            egress="aggregates", tags=TAGS + ("sample", "cells", "borderline")),
        cap(name="gating.render_collage", tool_name="render_gating_collage",
            purpose="A pixel-budgeted picture of the cells that decide a gate -- below, at "
                    "and above it (t2), with a reference channel (t3), every band (strata), "
                    "between candidate thresholds (flips), per quadrant against a partner "
                    "(quadrants), or the whole image with positives marked (overview). "
                    "WebP by default; the manifest says which cell is where.",
            permission="read", input_model=CollageInput, handler=render_collage,
            visual_output=True, egress="rendered_pixels",
            reads=("table", "gates", "image", "mask"),
            tags=TAGS + ("render", "visual", "look", "collage", "cells", "check")),
        cap(name="gating.bivariate", tool_name="bivariate_evidence",
            purpose="Two markers' gates against each other: quadrant counts, association, "
                    "the orphan / double-positive fraction for the relation the panel "
                    "expects, a contradiction score, and a density plot only when that "
                    "score is anomalous.",
            permission="read", input_model=BivariateInput, handler=bivariate_evidence,
            egress="rendered_pixels",
            tags=TAGS + ("bivariate", "scatter", "quadrant", "coexpression", "facs")),
        cap(name="gating.qc", tool_name="gating_qc",
            purpose="Panel-wide gate QC: every gated marker's positive fraction and "
                    "provenance, the contradiction score of every partner pair the panel "
                    "context names, and which markers need review.",
            permission="read", input_model=ProjectInput, handler=gating_qc,
            egress="aggregates", tags=TAGS + ("qc", "review", "consistency")),
        cap(name="gating.get_panel_context", tool_name="get_panel_context",
            purpose="The panel's biology as Plexora resolved it: each marker's role, "
                    "compartment, lineage, whether it is binary, its partners and caveats "
                    "(shipped vocabulary first), which markers are unresolved, and the "
                    "gating order that follows.",
            permission="read", input_model=ContextInput, handler=get_panel_context,
            egress="metadata", reads=("table",), tags=TAGS + ("context", "biology", "panel")),
        cap(name="gating.set_panel_context", tool_name="set_panel_context",
            purpose="Fill in or correct the panel context: the user's word outranks the "
                    "vocabulary; an agent's fills only markers the vocabulary does not "
                    "know. Context orders markers and picks references; it never moves a "
                    "gate.",
            permission="reversible_write", input_model=SetContextInput,
            handler=set_panel_context, writes=("config",), reads=("table",),
            egress="metadata", tags=TAGS + ("context", "biology", "panel")),
        cap(name="gating.set_status", tool_name="set_gate_status",
            purpose="Approve, lock, exclude (or undo any of those for) one marker's gate. "
                    "Locked and approved gates are never overwritten by an agent; excluded "
                    "markers are skipped by automatic runs.",
            permission="reversible_write", input_model=StatusInput, handler=set_gate_status,
            writes=("gates",), persistent=True, egress="metadata",
            tags=TAGS + ("approve", "lock", "exclude", "status")),
        cap(name="gating.provenance", tool_name="get_gate_provenance",
            purpose="Where every gate came from: method (GMM, AI-refined, transferred, "
                    "manual), status, confidence, the run and decisions behind it.",
            permission="read", input_model=ProjectInput, handler=get_provenance,
            egress="metadata", tags=TAGS + ("provenance", "history", "methods")),
        *capabilities_session.capabilities(),
    ]
