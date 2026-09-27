"""Carrying settled gates from the reference image to the rest of a dataset.

The reference image is gated in full first. Each other image's markers are
profiled by the bulk pass meanwhile and wait (`transfer_pending`); once the
reference's marker is settled, the image's quantile curve is aligned to the
reference's (`reference.align`) and the reference gate carried across:

- `stable`             the aligned gate is accepted (and, with the audit sheet
                       on, shown on it); confidence follows the reference's;
- a shift or a drift   one `transfer_check` packet: six cells either side of
                       the reference gate beside six either side of the
                       aligned gate in this image -- does the split hold?
- anything else        (the distribution changed, the staining failed, the
                       reference was not accepted, the aligned gate falls
                       outside this image's guard band) -- the image's own
                       full evaluation, as if it were the only one.

Every transferred gate carries its own method (`transfer_aligned`), alignment
and class in provenance. Nothing is copied blindly.
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import reference, schemas, tableops
from plexora.plugins.gating.server.autogate.engine import TERMINAL, unit_key

ACCEPTED = schemas.ACCEPTED_STATES

#: The provenance method of a gate carried from the reference image.
METHOD = "transfer_aligned"


def reference_done(engine) -> bool:
    ref = engine.record.get("reference_image")
    return ref is not None and all(u["state"] in TERMINAL for u in engine.units_of(ref))


def decide_image(engine, project):
    """Turn a target image's `transfer_pending` units into decisions."""
    from plexora.plugins.gating.server.autogate import bulk, candidates

    ref = engine.record["reference_image"]
    ds = engine.call.session.data(project)
    for unit in engine.units_of(project):
        if unit["state"] != "transfer_pending":
            continue
        ref_unit = engine.record["units"].get(unit_key(ref, unit["marker"]))
        if ref_unit is None or ref_unit["state"] not in ACCEPTED \
                or ref_unit.get("final") is None:
            unit["transfer"] = {"reference": ref, "class": None,
                                "reason": "the reference image has no accepted gate for "
                                          "this marker"}
            bulk.decide_first(engine, unit)
            continue
        space = unit.get("fit_space") or ref_unit.get("fit_space")
        alignment = reference.align(unit.get("summary") or {}, ref_unit.get("summary") or {},
                                    space)
        predicted = (reference.predict(alignment, ref_unit["final"], space)
                     if alignment else None)
        pf_pred = None
        if predicted is not None:
            counted = tableops.local_or_node(ds, "gating.autogate.gates_at", {
                "marker": unit["marker"], "lows": [predicted], "high": unit.get("high")})
            pf_pred = counted["n_positive"][0] / max(1, counted["n_finite"])
        pf_ref = (ref_unit.get("regression") or {}).get("fraction") or \
            (ref_unit.get("summary") or {}).get("positive_fraction")
        klass = reference.classify(alignment, unit, ref_unit, pf_target=pf_pred,
                                   pf_reference=pf_ref)
        sd = (ref_unit.get("metrics") or {}).get("sd_bg") or 1.0
        shift = None
        if alignment:
            shift = (alignment["a"] + (alignment["b"] - 1.0)
                     * ((ref_unit.get("metrics") or {}).get("mu_bg") or 0.0)) / sd
        unit["transfer"] = {"reference": ref, "reference_gate": ref_unit["final"],
                            "reference_confidence": ref_unit.get("confidence"),
                            "aligned_gate": predicted, "alignment": alignment,
                            "shift_bg_sd": shift, "class": klass, "pf_aligned": pf_pred,
                            "pf_reference": pf_ref, "own_gmm": unit.get("gmm")}
        inside = predicted is not None and candidates.inside_guard(ds, unit["marker"],
                                                                   predicted) is not False
        if klass == "stable" and inside:
            unit["candidate"] = float(predicted)
            unit["path"] = "transfer"
            unit["method"] = METHOD
            unit["transfer_confidence"] = ("high" if ref_unit.get("confidence") == "high"
                                           else "moderate")
            if engine.options["audit_sheet"] and not unit.get("no_image_channel"):
                engine.write(unit, unit["candidate"], method=METHOD,
                             confidence=unit["transfer_confidence"], state="accepted_t1",
                             tier="T1")
                if unit["state"] not in TERMINAL:
                    unit["state"] = "accepted_t1"
            else:
                unit["transfer_confidence"] = "moderate"
                engine.finalize(unit, method=METHOD)
            continue
        if klass in ("image_specific_shift", "smooth_drift", "batch_effect") and inside \
                and not unit.get("no_image_channel"):
            unit["candidate"] = float(predicted)
            unit["method"] = METHOD
            unit["state"] = "transfer_check"
            continue
        if klass == "staining_failure":
            unit["flags"] = sorted(set(unit.get("flags") or []) | {"staining_failure"})
        unit["transfer"]["reason"] = (f"{klass}; evaluated on its own"
                                      if inside else "the aligned gate is outside this "
                                                     "image's guard band; evaluated on its own")
        bulk.decide_first(engine, unit)


def after_bulk(engine):
    """Preliminary alignment classes for every target image (status only)."""
    ref = engine.record.get("reference_image")
    for unit in engine.record["units"].values():
        if unit["project"] == ref or unit["state"] != "transfer_pending":
            continue
        ref_unit = engine.record["units"].get(unit_key(ref, unit["marker"]))
        if not ref_unit or not ref_unit.get("summary"):
            continue
        alignment = reference.align(unit.get("summary") or {}, ref_unit["summary"],
                                    unit.get("fit_space"))
        unit["preliminary_alignment"] = alignment


def transfer_packet(engine, units):
    """Reference cells beside this image's cells, split at the carried gate."""
    import io

    from PIL import Image, ImageDraw

    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import views
    from plexora.plugins.gating.server.autogate.packets import _fmt, _sample

    unit = units[0]
    info = unit["transfer"]
    ref = info["reference"]
    sheets = []
    for project, gate, label in ((ref, info["reference_gate"], "reference"),
                                 (unit["project"], unit["candidate"], "this image")):
        ds = engine.call.session.data(project)
        channel = views.image_channel(ds, unit["marker"])
        sample = _sample(engine, ds, {"marker": unit["marker"], "high": None}, gate)
        below = sorted([c for n in ("just_below", "borderline", "low_background")
                        for c in (sample["strata"].get(n) or []) if c["value"] <= gate],
                       key=lambda c: -c["value"])[:3]
        above = sorted([c for n in ("borderline", "just_above", "moderate_positive")
                        for c in (sample["strata"].get(n) or []) if c["value"] > gate],
                       key=lambda c: c["value"])[:3]
        cells = [dict(c, call="negative") for c in sorted(below, key=lambda c: c["value"])] + \
                [dict(c, call="positive") for c in above]
        rendered = collage.render_collage(
            engine.call.session, ds, layout="comparison",
            rows=[{"label": f"{label}: {project}", "cells": cells}], marker=channel,
            gate=gate, fmt="png", pixel=engine.pixel_for(project),
            title=f"{label} ({project}) split at "
                                        f"{collage.compact_number(gate)}")
        sheets.append(rendered)
    images = [Image.open(io.BytesIO(r["png"])).convert("RGB") for r in sheets]
    width = max(i.width for i in images)
    height = sum(i.height for i in images) + 4
    stacked = Image.new("RGB", (width, height), collage.BG)
    y = 0
    for image in images:
        stacked.paste(image, (0, y))
        y += image.height + 4
    ImageDraw.Draw(stacked)
    data, fmt = collage.encode(stacked, _fmt(engine))
    from plexora.agent import artifacts
    from plexora.server.utils import fast_png
    import numpy as np

    manifest = {"kind": "plexora.gating_transfer", "marker": unit["marker"],
                "reference": ref, "project": unit["project"],
                "gates": {"reference": info["reference_gate"], "aligned": unit["candidate"]},
                "rows": [s["manifest"]["rows"] for s in sheets], "size": [width, height],
                "estimated_vision_tokens": collage.estimated_tokens(width, height)}
    art = artifacts.put(unit["project"], fast_png.encode_rgb8_png(np.asarray(stacked)),
                        manifest, kind="gating_transfer")
    unit.setdefault("artifacts", []).append(art["id"])
    packet = {
        "question": (f"{unit['marker']}: the reference image's gate, carried to "
                     f"{unit['project']} through its intensity alignment. In each row the "
                     "three cells on the left are called negative and the three on the "
                     "right positive. Does this image's split hold as well as the "
                     "reference's?"),
        "evidence": {"marker": unit["marker"],
                     "reference": {"project": ref, "gate": info["reference_gate"],
                                   "confidence": info.get("reference_confidence")},
                     "this": {"project": unit["project"], "aligned_gate": unit["candidate"],
                              "own_gmm": info.get("own_gmm"),
                              "shift_bg_sd": info.get("shift_bg_sd"),
                              "alignment": info.get("alignment"), "class": info.get("class"),
                              "fraction_at_aligned": info.get("pf_aligned"),
                              "fraction_reference": info.get("pf_reference")}},
        "allowed": _verdicts(),
        "_image_meta": [{"role": "comparison", "caption": "reference row above, this image "
                                                          "below", "artifact_id": art["id"]}],
    }
    return packet, [(data, fmt, (width, height))]


def _verdicts():
    from typing import get_args

    from plexora.plugins.gating.server.autogate import answers

    annotation = answers.TransferAnswer.model_fields["per_image"].annotation
    return list(get_args(get_args(annotation)[1]))


def apply_transfer(engine, packet, answer):
    from plexora.plugins.gating.server.autogate import transitions

    units = transitions._units(engine, packet)
    outcomes = []
    for unit in units:
        transitions._note(unit, answer)
        verdict = answer.per_image.get(unit["project"]) or \
            (next(iter(answer.per_image.values())) if len(answer.per_image) == 1
             else "cannot_tell")
        if verdict == "holds":
            unit["path"] = "transfer"
            unit["transfer_confidence"] = "moderate"
            engine.finalize(unit, method=METHOD)
        elif verdict in ("too_low", "too_high"):
            unit["path"] = "t4"
            unit["method"] = "ai_refined"
            unit["ai_confidence"] = 0.6
            transitions._to_t4(engine, unit, verdict)
        else:
            unit["candidate"] = unit.get("gmm") or unit["candidate"]
            unit["method"] = None
            unit["state"] = "awaiting_t2"
            unit["reason"] = "the carried gate could not be confirmed; evaluated on its own"
        outcomes.append(transitions._outcome(unit, verdict=verdict))
    return {"units": outcomes}


def dataset_summary(engine) -> dict:
    """Per marker: each image's drift class, and the strategy they imply."""
    ref = engine.record.get("reference_image")
    per_marker = {}
    for marker in engine.record["order"]:
        rows = []
        for project in engine.record["images"]:
            unit = engine.record["units"].get(unit_key(project, marker))
            if unit is None:
                continue
            info = unit.get("transfer") or {}
            rows.append({"project": project, "class": "reference" if project == ref
                         else info.get("class"), "shift": info.get("shift_bg_sd"),
                         "final": unit.get("final"), "state": unit["state"],
                         "confidence": unit.get("confidence")})
        refined = reference.dataset_classes([r for r in rows if r["class"] != "reference"])
        classes = [r["class"] for r in refined if r.get("class")]
        per_marker[marker] = {"images": rows, "refined": refined,
                              "strategy": reference.strategy(classes) if classes else None}
    return {"reference_image": ref, "markers": per_marker,
            "experimental_unit": "image", "n_images": len(engine.record["images"])}
