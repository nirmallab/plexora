"""One decision packet per kind: the question, the numbers, at most two images.

Every packet is self-contained -- it never says "as you saw earlier" -- so an
agent can answer it from a fresh conversation, and it carries numbers only in
JSON: images show pixels and cell positions, never a value the agent would
have to read off a picture. Each builder returns `(packet, images)` with
`images = [(bytes, format, (width, height))]` and the packet's `_image_meta`
aligned with them, or None when it closed the unit instead.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import tableops, views
from plexora.plugins.gating.server.autogate.engine import ENGINE, compact_profile, \
    seeded_order

MAX_IMAGES = 2


def _fmt(engine):
    return engine.options.get("image_format") or "webp"


def _image(rendered, role, caption):
    return ((rendered["image"], rendered["format"], tuple(rendered["manifest"]["size"])),
            {"role": role, "caption": caption,
             "artifact_id": (rendered.get("artifact") or {}).get("id")})


def _context_brief(unit):
    ctx = unit.get("context") or {}
    return {k: ctx.get(k) for k in ("canonical", "role", "compartment", "lineage", "binary",
                                    "caveats", "source") if ctx.get(k) is not None}


def _partner_numbers(engine, unit, ds, low):
    """Bivariate numbers against every partner already gated in this run."""
    out = []
    for ref in unit.get("partner_gates") or []:
        try:
            result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
                "a": unit["marker"], "gate_a": low, "b": ref["marker"],
                "gate_b": ref["gate"], "relation": ref["relation"], "with_grid": False})
        except Exception:
            continue
        out.append({"partner": ref["marker"], "relation": ref["relation"],
                    "partner_confidence": ref.get("confidence"),
                    "quadrants": result["quadrants"],
                    "frac_marker_in_partner": result["frac_a_in_b"],
                    "orphan_fraction": result["orphan_fraction"],
                    "double_positive_fraction": result["double_positive_fraction"],
                    "adjacent_orphan_share": result["adjacent_orphan_share"],
                    "contradiction": result["contradiction"]})
    return out


def _display(engine, ds, channel):
    from plexora.agent.evidence import calibration

    record = calibration.load(ds.name) or {}
    entry = (record.get("channels") or {}).get(channel) or {}
    return {"window": entry.get("window"), "window_source": entry.get("window_source"),
            "calibration_flags": entry.get("flags"), "hd": True,
            "colours": {"nuclear": calibration.NUCLEAR_MUTED_BLUE,
                        "marker": calibration.MARKER_COLOR, "outline": "#ff3df2"}}


def _sample(engine, ds, unit, low):
    return tableops.local_or_node(ds, "gating.autogate.sample", {
        "marker": unit["marker"], "low": low, "high": unit.get("high"),
        "seed": int(engine.options.get("seed") or 0)})


def _positive_map(engine, ds, unit, channel, low, size=384):
    from plexora.agent.evidence import collage

    return collage.render_overview(
        engine.call.session, ds, marker=channel,
        points=views.positive_points(ds, unit["marker"], low, unit.get("high")), low=low,
        high=unit.get("high"), size=size, fmt=_fmt(engine), show_marker=False,
        title=f"{unit['marker']} positives at {collage.compact_number(low)}")


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
        title=f"{unit['marker']} - gate {collage.compact_number(low)} - rows below/at/above "
              "(panels: nuclear | marker; merge | gate-relative)")
    pmap = _positive_map(engine, ds, unit, channel, low)
    unit.setdefault("artifacts", []).extend(
        a["id"] for a in (main.get("artifact"), pmap.get("artifact")) if a)
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
            "how_to_read": ("panels per cell: top-left nuclear, top-right marker (white), "
                            "bottom-left merge (blue nuclei, yellow marker, magenta = this "
                            "cell's outline), bottom-right the marker on a log scale whose "
                            "mid-grey IS the gate; caption = value and call (+/-)"),
        },
        "allowed": ["about_right", "too_low", "too_high", "cannot_tell", "not_binary"],
        "_image_meta": [],
    }
    images = []
    for rendered, role, caption in ((main, "t2_collage", "cells below / at / above the gate"),
                                    (pmap, "positive_map", "every positive cell, whole image")):
        image, meta = _image(rendered, role, caption)
        images.append(image)
        packet["_image_meta"].append(meta)
    unit["last_manifest"] = main["manifest"]
    return packet, images


# -- T3 -----------------------------------------------------------------------------


def t3_biological(engine, units):
    from plexora.agent.evidence import collage, density_plot

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
              f" - gate {collage.compact_number(low)} (panels: nuclear | marker; merge | "
              "reference)")
    numbers = []
    second = None
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
        if second is None and result["contradiction"] >= ENGINE["contradiction_render"]:
            from plexora.agent import artifacts
            from plexora.server.utils import fast_png
            import numpy as np

            image = density_plot.draw_density(result, log_axes=to_log)
            png = fast_png.encode_rgb8_png(np.asarray(image))
            data, fmt = collage.encode(image, _fmt(engine))
            manifest = {"kind": "plexora.gating_bivariate", "project": ds.name,
                        "a": unit["marker"], "b": ref["marker"], "size": list(image.size),
                        "estimated_vision_tokens": collage.estimated_tokens(*image.size)}
            art = artifacts.put(ds.name, png, manifest, kind="gating_bivariate")
            second = {"image": data, "format": fmt, "manifest": manifest, "artifact": art}
    if second is None:
        second = _positive_map(engine, ds, unit, channel, low)
    unit.setdefault("artifacts", []).extend(
        a["id"] for a in (main.get("artifact"), second.get("artifact")) if a)
    ctx = _context_brief(unit)
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) beside its reference "
                     f"{', '.join(r['marker'] for r in refs) or '(none gated yet)'}: is the "
                     "staining real and in the right cells, is the relation to the "
                     "reference as expected, and which way (if any) is the gate wrong?"),
        "evidence": {
            "marker": unit["marker"], "candidate": {"low": low, "high": unit.get("high")},
            "profile": unit.get("summary"), "context": ctx,
            "references": [{"marker": r["marker"], "relation": r["relation"],
                            "gate": r["gate"], "confidence": r.get("confidence")}
                           for r in refs],
            "bivariate": numbers, "strata_counts": sample["counts"],
            "previous_answer": unit.get("last_answer"),
            "display": _display(engine, ds, channel),
            "how_to_read": ("bottom-right panel = the reference channel (white); a subset "
                            "or co-expressed marker's positives should be reference-bright, "
                            "an exclusive one's should be reference-dark"),
        },
        "allowed": ["about_right", "too_low", "too_high", "cannot_tell", "not_binary"],
        "_image_meta": [],
    }
    images = []
    for rendered, role, caption in ((main, "t3_collage", "cells with the reference channel"),
                                    (second, "bivariate" if second.get("manifest", {})
                                     .get("kind") == "plexora.gating_bivariate"
                                     else "positive_map", "second view")):
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
    proposal = tableops.local_or_node(ds, "gating.autogate.candidates", {
        "marker": unit["marker"], "current_low": start, "direction": direction,
        "high": unit.get("high"), "gmm_gate": unit.get("gmm")})
    candidates = proposal["candidates"]
    if not candidates:
        reasons = "; ".join(r["reason"] for r in proposal["removed"][:3]) or "none proposed"
        engine.close(unit, "manual_review_recommended",
                     f"no threshold {'above' if direction == 'up' else 'below'} the current "
                     f"gate is admissible ({reasons})")
        return None
    unit["candidates"] = {c["id"]: c["low"] for c in candidates}
    thresholds = sorted([float(unit["candidate"])] + [c["low"] for c in candidates])
    delta = tableops.local_or_node(ds, "gating.autogate.delta", {
        "marker": unit["marker"], "candidates": thresholds, "high": unit.get("high"),
        "seed": int(engine.options.get("seed") or 0)})
    names = {}
    for index, interval in enumerate(delta["intervals"]):
        names[index] = (f"{collage.compact_number(interval['from'])} to "
                        f"{collage.compact_number(interval['to'])}")
    rows = views.flip_rows(delta, labels=names)
    main = collage.render_collage(
        engine.call.session, ds, layout="flips", rows=rows, marker=channel,
        gate=unit["candidate"], high=unit.get("high"), fmt=_fmt(engine),
        title=f"{unit['marker']} - the cells whose call changes between candidate gates "
              "(panels: marker | merge)")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    listing = seeded_order([{k: c[k] for k in ("id", "low", "n_positive", "fraction",
                                               "delta_bg_sd") if k in c}
                            for c in candidates],
                           int(engine.options.get("seed") or 0) + int(unit.get("rounds", 0)))
    intervals = [{"row": f"i{index + 1}", "from": iv["from"], "to": iv["to"],
                  "n_flip": iv["n_flip"],
                  "flips_if": ("the gate moves up past this interval" if direction == "up"
                               else "the gate moves down past this interval")}
                 for index, iv in enumerate(delta["intervals"])]
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): the gate looked too "
                     f"{'low' if direction == 'up' else 'high'}. Each row shows the cells "
                     "that would change call if the gate moved across it. Pick the "
                     "candidate after which the remaining positives look real (or `keep`, or "
                     "`none_separates`)."),
        "evidence": {"marker": unit["marker"], "current": unit["candidate"],
                     "direction": direction, "candidates": listing, "intervals": intervals,
                     "guard": proposal.get("guard"), "round": int(unit.get("rounds", 0)) + 1,
                     "profile": unit.get("summary"),
                     "how_to_read": ("rows run low to high threshold; a row's cells are "
                                     "positive at every gate below the row and negative at "
                                     "every gate above it")},
        "allowed": [c["id"] for c in candidates] + ["keep", "none_separates"],
        "_image_meta": [],
    }
    image, meta = _image(main, "flips_collage", "cells between candidate thresholds")
    packet["_image_meta"].append(meta)
    unit["last_manifest"] = main["manifest"]
    return packet, [image]


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
    view = collage.render_overview(
        engine.call.session, ds, marker=channel,
        points=views.positive_points(ds, unit["marker"], low, unit.get("high")), low=low,
        high=unit.get("high"), size=512, fmt=_fmt(engine),
        title=f"{unit['marker']}: yellow = stain, blue = nuclei, magenta = cells above "
              f"{collage.compact_number(low)}")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (view.get("artifact"),) if a)
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) raised technical flags "
                     f"({', '.join(unit.get('qc_reason') or unit.get('flags') or [])}). Is "
                     "there real, cell-shaped staining here, or is the channel technically "
                     "failed (flat, saturated, background only, artifact)?"),
        "evidence": {"marker": unit["marker"], "flags": unit.get("flags"),
                     "image_qc": unit.get("image_qc"), "profile": unit.get("summary"),
                     "context": _context_brief(unit)},
        "allowed": ["real_signal", "technical_failure", "cannot_tell"],
        "_image_meta": [],
    }
    image, meta = _image(view, "overview", "the whole image")
    packet["_image_meta"].append(meta)
    return packet, [image]


def regression_confirm(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    low = unit["candidate"]
    view = collage.render_overview(
        engine.call.session, ds, marker=channel,
        points=views.positive_points(ds, unit["marker"], low, unit.get("high")), low=low,
        high=unit.get("high"), size=512, fmt=_fmt(engine),
        title=f"{unit['marker']} at {collage.compact_number(low)}: magenta = positive cells")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (view.get("artifact"),) if a)
    regression = unit.get("regression") or {}
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): the chosen gate "
                     f"{collage.compact_number(low)} failed whole-image checks "
                     f"({', '.join(regression.get('failed') or [])}). Does the positive map "
                     "still look right across the tissue?"),
        "evidence": {"marker": unit["marker"], "final": low, "gmm": unit.get("gmm"),
                     "checks": regression.get("checks"), "fraction": regression.get("fraction"),
                     "context": _context_brief(unit)},
        "allowed": ["holds", "too_low", "too_high", "artifact", "cannot_tell"],
        "_image_meta": [],
    }
    image, meta = _image(view, "overview", "positives at the chosen gate")
    packet["_image_meta"].append(meta)
    return packet, [image]


def t1_strip(engine, units):
    """One sheet for up to eight T1-accepted markers of one image."""
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
    sheet = collage.render_collage(
        engine.call.session, ds, layout="strip", rows=rows, marker=rows[0]["marker"],
        gate=listed[0]["candidate"], fmt=_fmt(engine),
        title="accepted automatically - per row: 3 cells just below, 3 just above the gate "
              "(marker | merge)")
    for unit in listed:
        unit.setdefault("artifacts", []).extend(
            a["id"] for a in (sheet.get("artifact"),) if a)
    packet = {
        "question": ("These markers were accepted from their distributions alone. For each "
                     "row: do the three cells on the right look positive and the three on "
                     "the left negative? Answer `ok`, or `suspicious` for any row to look "
                     "at properly."),
        "evidence": {"markers": [{"marker": u["marker"], "gate": u["candidate"],
                                  "fraction": (u.get("summary") or {}).get("positive_fraction"),
                                  "t1_score": (u.get("t1") or {}).get("score"),
                                  "class": u.get("class")} for u in listed]},
        "allowed": ["ok", "suspicious", "cannot_tell"],
        "_image_meta": [],
    }
    image, meta = _image(sheet, "audit_sheet", "near-gate cells, one row per marker")
    packet["_image_meta"].append(meta)
    return packet, [image]


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


def transfer_check(engine, units):
    from plexora.plugins.gating.server.autogate import transfer

    return transfer.transfer_packet(engine, units)


BUILDERS = {"t2_confirm": t2_confirm, "t3_biological": t3_biological,
            "t4_candidates": t4_candidates, "qc_confirm": qc_confirm,
            "regression_confirm": regression_confirm, "t1_strip": t1_strip,
            "panel_context": panel_context, "transfer_check": transfer_check}


def thumbnail(engine, unit):
    """A small near-gate strip for the report, written at accept time (WebP)."""
    from plexora.agent.evidence import collage

    try:
        ds = engine.call.session.data(unit["project"])
        channel = views.image_channel(ds, unit["marker"])
        if channel is None or unit.get("final") is None:
            return None
        low = unit["final"]
        sample = _sample(engine, ds, unit, low)
        cells = sorted([c for n in ("just_below", "borderline", "just_above", "low_background",
                                    "moderate_positive") for c in sample["strata"].get(n) or []],
                       key=lambda c: abs(c["value"] - low))[:6]
        cells = sorted(cells, key=lambda c: c["value"])
        if not cells:
            return None
        rows = [{"label": unit["marker"], "cells": [
            dict(c, call=views.call_of(c["value"], low, unit.get("high") or float("inf")))
            for c in cells]}]
        rendered = collage.render_collage(engine.call.session, ds, layout="strip", rows=rows,
                                          marker=channel, gate=low, fmt="webp", store=False,
                                          tile_px=48, title=f"{unit['marker']} at "
                                          f"{collage.compact_number(low)}")
        folder = engine.store.thumbs_dir(engine.id) / _safe(unit["project"])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe(unit['marker'])}.webp"
        path.write_bytes(rendered["image"])
        return str(path)
    except Exception:
        return None


def _safe(name):
    import re

    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))[:80] or "_"


def gate_of(ds, marker):
    return model.get_gate(ds, marker)
