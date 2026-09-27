"""One decision packet per kind: the question, the numbers, at most two images.

Every packet is self-contained -- it never says "as you saw earlier" -- so an
agent can answer it from a fresh conversation, and it carries numbers only in
JSON: images show pixels and cell positions, never a value the agent would
have to read off a picture. Each builder returns `(packet, images)` with
`images = [(bytes, format, (width, height))]` and the packet's `_image_meta`
aligned with them, or None when it closed the unit instead.
"""

from __future__ import annotations

from typing import get_args

from plexora.agent.errors import AgentError
from plexora.plugins.gating.server.autogate import answers, schemas, tableops, views
from plexora.plugins.gating.server.autogate.engine import ENGINE, seeded_order

#: At most this many images in one packet (`Engine.issue` refuses more).
MAX_IMAGES = 2

#: How to read the context sheet (`sheet.py`), the second picture of a look.
SCALES_READING = (
    "the context sheet shows the marker at three scales. Top: three fields of the tissue "
    "(borderline, clearly positive, clearly negative; blue nuclei, yellow marker, magenta "
    "outlines = cells the gate calls positive). Bottom: the whole image's stain, the whole "
    "image's positive cells, and the marker against its first gated partner (x = this "
    "marker, y = the partner, both gates drawn, as on a flow plot) or its distribution. "
    "Read coarse to fine: is the pattern across the tissue right for this marker, do the "
    "fields show the architecture it should (glands, vessels, lymphoid aggregates), then "
    "are the cells at the gate called correctly. A marker with an obvious tissue pattern "
    "is judged at field scale first")

#: What the partner plot should look like, per relation (`bivariate.RELATIONS`).
BIVARIATE_READING = {
    "subset": "a subset marker: its positives (right of the vertical gate) should also be "
              "partner-positive (above the horizontal gate); cells right and below are "
              "suspect",
    "coexpressed": "a co-expressed pair: positives of one should be positives of the other, "
                   "on the diagonal; the off-diagonal quadrants should be thin",
    "exclusive": "an exclusive pair: the upper-right quadrant should be nearly empty; a "
                 "cloud there means one gate is too low (or spill-over between neighbours)",
    "independent": "no relation is expected; no quadrant should be empty by rule",
}


def _allowed(model, field):
    """The values a packet offers for an answer field: its Literal's."""
    annotation = model.model_fields[field].annotation
    return list(get_args(annotation))


def _fmt(engine):
    return engine.options["image_format"]


def _seed(engine):
    return int(engine.options["seed"])


def _image(rendered, role, caption):
    return ((rendered["image"], rendered["format"], tuple(rendered["manifest"]["size"])),
            {"role": role, "caption": caption,
             "artifact_id": (rendered.get("artifact") or {}).get("id")})


def _context_brief(unit):
    ctx = unit.get("context") or {}
    return {k: ctx.get(k) for k in ("canonical", "role", "compartment", "lineage", "binary",
                                    "caveats", "source") if ctx.get(k) is not None}


def _partner_numbers(engine, unit, ds, low):
    """Bivariate numbers against every partner gated so far -- by this run, or
    by the user in a gate the run kept (`Engine.references_ready`) -- with the
    FACS negative control each one gives (`bivariate.negative_control`)."""
    out = []
    for ref in engine.references_ready(unit):
        try:
            result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
                "a": unit["marker"], "gate_a": low, "b": ref["marker"],
                "gate_b": ref["gate"], "relation": ref["relation"], "with_grid": False})
        except Exception:
            continue
        entry = {"partner": ref["marker"], "relation": ref["relation"],
                 "partner_confidence": ref.get("confidence"),
                 "quadrants": result["quadrants"],
                 "frac_marker_in_partner": result["frac_a_in_b"],
                 "orphan_fraction": result["orphan_fraction"],
                 "double_positive_fraction": result["double_positive_fraction"],
                 "adjacent_orphan_share": result["adjacent_orphan_share"],
                 "contradiction": result["contradiction"]}
        try:
            control = tableops.local_or_node(ds, "gating.autogate.control", {
                "a": unit["marker"], "gate_a": low, "b": ref["marker"],
                "gate_b": ref["gate"], "relation": ref["relation"]}).get("control")
        except Exception:
            control = None
        if control:
            entry["control"] = control
        out.append(entry)
    return out


def _compartment_sentence(unit):
    """What the marker's compartment says about reading its cells
    (`schemas.COMPARTMENT_POLICY`), or ""."""
    compartment = (unit.get("context") or {}).get("compartment")
    policy = schemas.COMPARTMENT_POLICY.get(compartment)
    return f"; {policy['reading']}" if policy else ""


def _sheet(engine, unit, ds, channel, low, *, references=(), candidates=None, title=None):
    """The context sheet for a unit (the second picture of a look, the only
    one of a check); its artifact joins the unit's."""
    from plexora.plugins.gating.server.autogate import sheet

    refs = engine.references_ready(unit)
    partner = refs[0] if refs else None
    rendered = sheet.render_context_sheet(
        engine.call.session, ds, marker=unit["marker"], channel=channel, low=low,
        high=unit.get("high"), references=references, partner=partner,
        candidates=candidates, seed=_seed(engine), fmt=_fmt(engine),
        title=title or f"{unit['marker']} · gate {_compact(low)} · three scales")
    if rendered.get("artifact"):
        unit.setdefault("artifacts", []).append(rendered["artifact"]["id"])
    return rendered


def _compact(value):
    from plexora.agent.evidence import collage

    return collage.compact_number(value)


def _sheet_reading(rendered):
    plot = rendered["manifest"].get("plot") or {}
    extra = BIVARIATE_READING.get(plot.get("relation")) if plot.get("kind") == "density" \
        else None
    return SCALES_READING + (f"; the partner plot: {extra}" if extra else "")


def _sheet_evidence(rendered):
    manifest = rendered["manifest"]
    return {"fields": manifest.get("fields"),
            "sheet": {"overview": manifest.get("overview"), "plot": manifest.get("plot"),
                      "classes_without_field": manifest.get("classes_without_field")}}


def _display(engine, ds, channel):
    from plexora.agent.evidence import calibration, crops

    record = calibration.load(ds.name) or {}
    entry = (record.get("channels") or {}).get(channel) or {}
    return {"window": entry.get("window"), "window_source": entry.get("window_source"),
            "calibration_flags": entry.get("flags"), "hd": True,
            "colours": {"nuclear": calibration.NUCLEAR_MUTED_BLUE,
                        "marker": calibration.MARKER_COLOR, "outline": crops.OUTLINE_COLOR}}


def _sample(engine, ds, unit, low):
    return tableops.local_or_node(ds, "gating.autogate.sample", {
        "marker": unit["marker"], "low": low, "high": unit.get("high"),
        "seed": _seed(engine)})


def _unit_channel(engine, unit):
    ds = engine.call.session.data(unit["project"])
    channel = views.image_channel(ds, unit["marker"])
    if channel is None:
        raise AgentError("precondition_missing", f"{unit['marker']!r} has no image channel")
    return ds, channel


# -- T2 -----------------------------------------------------------------------------


def t2_confirm(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    low = unit["candidate"]
    sample = _sample(engine, ds, unit, low)
    rows = views.t2_rows(sample, low, unit.get("high"))
    to_log = sample.get("fit_space") == "log1p"
    main = collage.render_collage(
        engine.call.session, ds, layout="t2", rows=rows, marker=channel, gate=low,
        high=unit.get("high"), fmt=_fmt(engine), to_log=to_log,
        title=f"{unit['marker']} · gate {collage.compact_number(low)} · rows below/at/above "
              "(panels: nuclear | marker; merge | gate-relative)")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    context_sheet = _sheet(engine, unit, ds, channel, low)
    ctx = _context_brief(unit)
    compartment = ctx.get("compartment") or "the expected"
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): do the cells above the gate carry real "
                     f"{compartment} staining, and is the gate too low, about right or too "
                     "high? Judge the rows nearest the gate most."),
        "evidence": {
            "marker": unit["marker"], "project": ds.name,
            "candidate": {"low": low, "high": unit.get("high"), "source": unit.get("source")},
            "profile": unit.get("summary"),
            "strata_counts": sample["counts"],
            "rows": {r["key"]: r["label"] for r in rows},
            "context": ctx, "qc_flags": unit.get("flags"),
            "display": _display(engine, ds, channel),
            "partners": _partner_numbers(engine, unit, ds, low),
            **_sheet_evidence(context_sheet),
            "how_to_read": ("the collage, panels per cell: top-left nuclear, top-right marker "
                            "(white), bottom-left merge (blue nuclei, yellow marker, magenta = "
                            "this cell's outline), bottom-right the marker on a log scale "
                            "whose mid-grey IS the gate; caption = value and call (+/-). "
                            + _sheet_reading(context_sheet) + _compartment_sentence(unit)
                            + ". `partners` are the whole-image numbers against each partner "
                            "gated so far, with the negative control each gives (the "
                            "marker's p99 among cells the partner says are negative for it); "
                            "a request for a reference channel is honoured when one is "
                            "listed there. `no_positives` when no cell anywhere is really "
                            "positive"),
        },
        "allowed": _allowed(answers.T2Answer, "direction"),
        "_image_meta": [],
    }
    images = []
    for rendered, role, caption in ((main, "t2_collage", "cells below / at / above the gate"),
                                    (context_sheet, "context_sheet",
                                     "the tissue at three scales, and the partner plot")):
        image, meta = _image(rendered, role, caption)
        images.append(image)
        packet["_image_meta"].append(meta)
    unit["last_manifest"] = main["manifest"]
    return packet, images


# -- T3 -----------------------------------------------------------------------------


def t3_biological(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    low = unit["candidate"]
    refs = unit.get("reference_gates") or []
    ref_channels = [views.image_channel(ds, r["marker"]) for r in refs]
    ref_channels = [c for c in ref_channels if c]
    sample = _sample(engine, ds, unit, low)
    rows = views.t2_rows(sample, low, unit.get("high"))
    to_log = sample.get("fit_space") == "log1p"
    main = collage.render_collage(
        engine.call.session, ds, layout="t3", rows=rows, marker=channel, gate=low,
        high=unit.get("high"), references=ref_channels, fmt=_fmt(engine), to_log=to_log,
        title=f"{unit['marker']} with {', '.join(r['marker'] for r in refs) or 'no reference'}"
              f" · gate {collage.compact_number(low)} (panels: nuclear | marker; merge | "
              "reference)")
    numbers = []
    for ref in refs:
        result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
            "a": unit["marker"], "gate_a": low, "b": ref["marker"], "gate_b": ref["gate"],
            "relation": ref["relation"]})
        brief = {k: result[k] for k in ("relation", "quadrants", "frac_a_in_b", "frac_b_in_a",
                                        "orphan_fraction", "double_positive_fraction",
                                        "adjacent_orphan_share", "conditional_shift_fit",
                                        "contradiction")}
        brief["partner"] = ref["marker"]
        numbers.append(brief)
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    second = _sheet(engine, unit, ds, channel, low, references=ref_channels[:1])
    ctx = _context_brief(unit)
    compartment = ctx.get("compartment") or "the expected"
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) beside its reference "
                     f"{', '.join(r['marker'] for r in refs) or '(none gated yet)'}: is the "
                     f"{compartment} staining real and in the right cells, is the relation "
                     "to the reference as expected, and which way (if any) is the gate "
                     "wrong?"),
        "evidence": {
            "marker": unit["marker"], "candidate": {"low": low, "high": unit.get("high")},
            "profile": unit.get("summary"), "context": ctx,
            "references": [{"marker": r["marker"], "relation": r["relation"],
                            "gate": r["gate"], "confidence": r.get("confidence")}
                           for r in refs],
            "bivariate": numbers, "strata_counts": sample["counts"],
            "previous_answer": unit.get("last_answer"),
            "display": _display(engine, ds, channel),
            **_sheet_evidence(second),
            "how_to_read": ("the collage's bottom-right panel = the reference channel "
                            "(white); a subset or co-expressed marker's positives should be "
                            "reference-bright, an exclusive one's should be reference-dark; "
                            "the fields add the reference in cyan. " + _sheet_reading(second)
                            + _compartment_sentence(unit)),
        },
        "allowed": _allowed(answers.T3Answer, "direction"),
        "_image_meta": [],
    }
    images = []
    for rendered, role, caption in ((main, "t3_collage", "cells with the reference channel"),
                                    (second, "context_sheet",
                                     "the tissue at three scales, and the partner plot")):
        image, meta = _image(rendered, role, caption)
        images.append(image)
        packet["_image_meta"].append(meta)
    unit["last_manifest"] = main["manifest"]
    return packet, images


# -- T4 -----------------------------------------------------------------------------


def t4_candidates(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    direction = unit.get("direction")
    start = unit.get("span_from", unit["candidate"])
    controls = [p["control"] for p in _partner_numbers(engine, unit, ds, unit["candidate"])
                if p.get("control")]
    proposal = tableops.local_or_node(ds, "gating.autogate.candidates", {
        "marker": unit["marker"], "current_low": start, "direction": direction,
        "high": unit.get("high"), "gmm_gate": unit.get("gmm"), "controls": controls})
    candidates = proposal["candidates"]
    if not candidates:
        reasons = "; ".join(r["reason"] for r in proposal["removed"][:3]) or "none proposed"
        engine.close(unit, "manual_review_recommended",
                     f"no threshold {'above' if direction == 'up' else 'below'} the current "
                     f"gate is admissible ({reasons})")
        return None
    unit["candidates"] = {c["id"]: c["low"] for c in candidates}
    unit["candidates_reach_edge"] = bool(proposal.get("reaches_edge"))
    thresholds = sorted([float(unit["candidate"])] + [c["low"] for c in candidates])
    delta = tableops.local_or_node(ds, "gating.autogate.delta", {
        "marker": unit["marker"], "candidates": thresholds, "high": unit.get("high"),
        "seed": _seed(engine)})
    names = {}
    for index, interval in enumerate(delta["intervals"]):
        names[index] = (f"{collage.compact_number(interval['from'])} to "
                        f"{collage.compact_number(interval['to'])}")
    rows = views.flip_rows(delta, labels=names)
    main = collage.render_collage(
        engine.call.session, ds, layout="flips", rows=rows, marker=channel,
        gate=unit["candidate"], high=unit.get("high"), fmt=_fmt(engine),
        title=f"{unit['marker']} · the cells whose call changes between candidate gates "
              "(panels: marker | merge)")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    context_sheet = _sheet(engine, unit, ds, channel, unit["candidate"],
                           candidates=[{"id": c["id"], "low": c["low"]} for c in candidates])
    listing = seeded_order([{k: c[k] for k in ("id", "low", "n_positive", "fraction",
                                               "delta_bg_sd", "step", "control") if k in c}
                            for c in candidates],
                           _seed(engine) + int(unit.get("rounds", 0)))
    intervals = [{"row": f"i{index + 1}", "from": iv["from"], "to": iv["to"],
                  "n_flip": iv["n_flip"],
                  "flips_if": ("the gate moves up past this interval" if direction == "up"
                               else "the gate moves down past this interval")}
                 for index, iv in enumerate(delta["intervals"])]
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): the gate looked too "
                     f"{'low' if direction == 'up' else 'high'}. Each row shows the cells "
                     "that would change call if the gate moved across it. Pick the "
                     "candidate after which the remaining positives look real (or "
                     + " or ".join(f"`{c}`" for c in schemas.T4_CHOICES) + ")."),
        "evidence": {"marker": unit["marker"], "current": unit["candidate"],
                     "direction": direction, "candidates": listing, "intervals": intervals,
                     "guard": proposal.get("guard"), "round": int(unit.get("rounds", 0)) + 1,
                     "profile": unit.get("summary"), **_sheet_evidence(context_sheet),
                     "how_to_read": ("rows run low to high threshold; a row's cells are "
                                     "positive at every gate below the row and negative at "
                                     "every gate above it. A candidate whose `step` starts "
                                     "`ctrl:` (or that carries `control`) is the negative-"
                                     "control threshold: the marker's p99 among cells the "
                                     "partner says are negative for it. The sheet's plot "
                                     "draws every candidate as a dashed line at its id. "
                                     + _sheet_reading(context_sheet)
                                     + _compartment_sentence(unit))},
        "allowed": [c["id"] for c in candidates] + list(schemas.T4_CHOICES),
        "_image_meta": [],
    }
    images = []
    for rendered, role, caption in ((main, "flips_collage",
                                     "cells between candidate thresholds"),
                                    (context_sheet, "context_sheet",
                                     "the tissue at three scales, candidates on the plot")):
        image, meta = _image(rendered, role, caption)
        images.append(image)
        packet["_image_meta"].append(meta)
    unit["last_manifest"] = main["manifest"]
    return packet, images


# -- confirmations -------------------------------------------------------------------


def qc_confirm(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    try:
        ds, channel = _unit_channel(engine, unit)
    except AgentError:
        engine.close(unit, "technically_failed",
                     "technical flags and no image channel to check them against",
                     confidence="failed_qc")
        return None
    low = unit["candidate"]
    if low is None:
        low = (unit.get("seen") or [None])[0]
    view = _sheet(engine, unit, ds, channel, low,
                  title=f"{unit['marker']}: yellow = stain, blue = nuclei, magenta = cells "
                        f"above {collage.compact_number(low)}")
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) needs a whole-image check: "
                     f"{', '.join(unit.get('qc_reason') or unit.get('flags') or [])}. Is "
                     "there real, cell-shaped staining in some cells (`real_signal`), did "
                     "the stain work but no cell in this image is positive "
                     "(`no_positive_population`: the gate is put at the maximum), or is the "
                     "channel technically failed (`technical_failure`: flat, saturated, "
                     "background only, artifact; also gated at the maximum)?"),
        "evidence": {"marker": unit["marker"], "flags": unit.get("flags"),
                     "image_qc": unit.get("image_qc"), "profile": unit.get("summary"),
                     "context": _context_brief(unit), **_sheet_evidence(view),
                     "how_to_read": _sheet_reading(view) + _compartment_sentence(unit)},
        "allowed": _allowed(answers.QCAnswer, "verdict"),
        "_image_meta": [],
    }
    image, meta = _image(view, "context_sheet", "the whole image and three fields")
    packet["_image_meta"].append(meta)
    return packet, [image]


def regression_confirm(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    low = unit["candidate"]
    view = _sheet(engine, unit, ds, channel, low,
                  title=f"{unit['marker']} at {collage.compact_number(low)}: magenta = "
                        "positive cells")
    regression = unit.get("regression") or {}
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): the chosen gate "
                     f"{collage.compact_number(low)} failed whole-image checks "
                     f"({', '.join(regression.get('failed') or [])}). Does the positive map "
                     "still look right across the tissue?"),
        "evidence": {"marker": unit["marker"], "final": low, "gmm": unit.get("gmm"),
                     "checks": regression.get("checks"), "fraction": regression.get("fraction"),
                     "context": _context_brief(unit), **_sheet_evidence(view),
                     "how_to_read": _sheet_reading(view) + _compartment_sentence(unit)},
        "allowed": _allowed(answers.ConfirmAnswer, "verdict"),
        "_image_meta": [],
    }
    image, meta = _image(view, "context_sheet", "positives at the chosen gate, three scales")
    packet["_image_meta"].append(meta)
    return packet, [image]


def t1_strip(engine, units):
    """One sheet for the T1-accepted markers of one image (`strip_batch` of
    them at most), `strip_cells` cells a row."""
    from plexora.agent.evidence import collage

    project = units[0]["project"]
    ds = engine.call.session.data(project)
    rows = []
    listed = []
    for unit in units:
        channel = views.image_channel(ds, unit["marker"])
        if channel is None:
            unit["audited"] = False
            engine.finalize(unit, method="gmm")
            continue
        low = unit["candidate"]
        sample = _sample(engine, ds, unit, low)
        below = sorted([c for n in ("just_below", "borderline", "low_background")
                        for c in (sample["strata"].get(n) or []) if c["value"] <= low],
                       key=lambda c: -c["value"])[:ENGINE["strip_cells"] // 2]
        above = sorted([c for n in ("borderline", "just_above", "moderate_positive")
                        for c in (sample["strata"].get(n) or []) if c["value"] > low],
                       key=lambda c: c["value"])[:ENGINE["strip_cells"] // 2]
        cells = [dict(c, call="negative") for c in sorted(below, key=lambda c: c["value"])] + \
                [dict(c, call="positive") for c in above]
        rows.append({"key": unit["marker"], "label": unit["marker"], "marker": channel,
                     "gate": low, "cells": cells})
        listed.append(unit)
    if not listed:
        return None
    half = ENGINE["strip_cells"] // 2
    sheet = collage.render_collage(
        engine.call.session, ds, layout="strip", rows=rows, marker=rows[0]["marker"],
        gate=listed[0]["candidate"], fmt=_fmt(engine),
        title=f"accepted automatically - per row: {half} cells just below, {half} just above "
              "the gate (marker | merge)")
    for unit in listed:
        unit.setdefault("artifacts", []).extend(
            a["id"] for a in (sheet.get("artifact"),) if a)
    packet = {
        "question": ("These markers were accepted from their distributions alone. For each "
                     f"row: do the {half} cells on the right look positive and the {half} on "
                     "the left negative? Answer per row with one of "
                     + ", ".join(f"`{v}`" for v in _strip_verdicts())
                     + " (`suspicious` sends it to a proper look)."),
        "evidence": {"markers": [{"marker": u["marker"], "gate": u["candidate"],
                                  "fraction": (u.get("summary") or {}).get("positive_fraction"),
                                  "t1_score": (u.get("t1") or {}).get("score"),
                                  "class": u.get("class")} for u in listed]},
        "allowed": _strip_verdicts(),
        "_image_meta": [],
    }
    image, meta = _image(sheet, "audit_sheet", "near-gate cells, one row per marker")
    packet["_image_meta"].append(meta)
    return packet, [image]


def _strip_verdicts():
    annotation = answers.T1StripAnswer.model_fields["verdicts"].annotation
    return list(get_args(get_args(annotation)[1]))


def panel_context(engine, units):
    from plexora.plugins.gating.server.autogate import context

    project = engine.record["images"][0]
    ds = engine.call.session.data(project)
    panel = context.for_project(ds)
    unresolved = panel["unresolved"]
    if not unresolved:
        engine.record["panel_pending"] = False
        return None
    packet = {
        "question": ("Plexora's vocabulary does not know these markers. For each, give its "
                     "role, compartment, lineage, whether it is binary, and partners -- only "
                     "markers of this panel, only what you are confident of. Skip any you "
                     "do not know; they are gated on their own data."),
        "evidence": {"unresolved": unresolved,
                     "panel": [{"marker": m, "canonical": panel["entries"][m].get("canonical"),
                                "role": panel["entries"][m].get("role")}
                               for m in panel["markers"]],
                     "roles": list(context.ROLES),
                     "rule": "your entries order the markers and pick references; they never "
                             "move a gate, and the vocabulary outranks them"},
        "allowed": ["entries"],
        "_image_meta": [],
    }
    return packet, []


def expression_setup(engine, units):
    """Which matrix to gate, when the values could not settle it: put to the
    user through the agent (and the viewer's requirements modal)."""
    expression = engine.record.get("expression") or {}
    if expression.get("status") != "pending":
        return None
    projects = expression.get("projects") or engine.record["images"]
    current = expression.get("current") or {}
    options = expression.get("options") or []
    where = projects[0] if len(projects) == 1 else f"{len(projects)} images"
    packet = {
        "question": (f"{where} reads its marker values from {current.get('features_layer')} "
                     f"with log1p {'on' if current.get('features_log') else 'off'}, and nobody "
                     "has confirmed that. Which matrix holds the intensities to gate, and "
                     "should log1p be applied as they are read? Put the options to the user; "
                     "do not guess."),
        "evidence": {"projects": projects, "source_kind": expression.get("source_kind"),
                     "current": current, "options": options,
                     "recommendation": expression.get("recommendation"),
                     "rule": expression.get("rule"), "ask_user_required": True},
        "allowed": [o["value"] for o in options],
        "_image_meta": [],
    }
    return packet, []


def transfer_check(engine, units):
    from plexora.plugins.gating.server.autogate import transfer

    return transfer.transfer_packet(engine, units)


BUILDERS = {"t2_confirm": t2_confirm, "t3_biological": t3_biological,
            "t4_candidates": t4_candidates, "qc_confirm": qc_confirm,
            "regression_confirm": regression_confirm, "t1_strip": t1_strip,
            "panel_context": panel_context, "transfer_check": transfer_check,
            "expression_setup": expression_setup}
