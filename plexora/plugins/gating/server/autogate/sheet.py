"""The context sheet: a marker at three scales, on one picture.

A person gating a marker does not look only at the cells beside the gate. They
look at the whole slide (is the stain where the tissue says it should be?), at
the architecture (E-cadherin traces every gland; a membrane stain rings its
cells), and at the cells at the threshold -- the collage. This sheet is the
first two, drawn deterministically beside every look:

    +--------------------+--------------------+--------------------+
    | field: borderline  | field: clearly +   | field: clearly -   |   mesoscale
    +--------------------+--------------------+--------------------+
    | whole image: stain | whole image:       | the partner plot   |   slide scale
    |                    | positives          | (or distribution)  |
    +--------------------+--------------------+--------------------+

Fields are chosen by `plexora.agent.gate_sampling` (grid-snapped, scored from
counts) at the session's field size in microns (`presets` "gating_context",
400 µm by default) converted with THIS image's pixel size -- or the session's
estimate of it, for an image that states none (`pixel_estimate`) -- and drawn
with the same cell-anchored windows as the collage, with the cells the gate
calls positive outlined in magenta. In a look at candidate gates the top row
is instead one borderline field per candidate: the cells between it and the
threshold before it, magenta at that candidate. The partner plot is the FACS
view: the marker (x) against the partner the caller chose (y), both gates
drawn; without a partner, the marker's distribution with its gate.
"""

from __future__ import annotations

import numpy as np

MANIFEST_KIND = "plexora.gating_context_sheet"
#: A field's side on the sheet, screen px: about a micron per pixel at 400 µm,
#: so a 10 µm cell is ten pixels across.
SHEET_PX = 384
#: The slide-scale row (the whole image twice, and the plot): a core reads at
#: this size -- its job is the pattern, not the cells -- and it is a quarter
#: of the sheet's area saved (vision tokens are area).
BOTTOM_PX = 288
#: At most this many candidate fields on a T4 sheet (one row of three).
MAX_CANDIDATE_FIELDS = 3
CLASSES = ("borderline", "clear_positive", "clear_negative")
TITLE_H = 14
CAPTION_H = 14
GAP = 4

#: How each field class is captioned on the sheet.
CLASS_LABELS = {"borderline": "borderline", "clear_positive": "clearly positive",
                "clear_negative": "clearly negative"}


def _plot_px():
    from plexora.agent.evidence import collage

    return BOTTOM_PX + collage.OVERVIEW_TITLE_H


def sheet_size() -> tuple:
    """(width, height): fixed, whatever the image's shape (a missing field or
    a flat overview leaves a blank cell, never a smaller sheet)."""
    width = 3 * SHEET_PX + 2 * GAP
    height = TITLE_H + SHEET_PX + CAPTION_H + GAP + _plot_px() + CAPTION_H
    return width, height


def max_pixels() -> int:
    width, height = sheet_size()
    return width * height


def field_um_default() -> float:
    from plexora.agent import presets

    return float(presets.field_size("gating_context")[0])


def field_side_px(record, field_um=None, pixel=None) -> dict:
    """{side_px, field_um, um_per_px, source, label}: a field's side in
    full-resolution pixels, from microns and the image's own pixel size (or
    `pixel`, the session's stand-in for one it lacks). Without either, the
    preset's pixel fallback -- and the label says so."""
    from plexora.agent import presets
    from plexora.server.utils import pixel_scale

    preset_um, preset_px = presets.field_size("gating_context")
    field_um = float(field_um or preset_um)
    pixel = pixel if pixel is not None else pixel_scale.pixel_size(record)
    if pixel and pixel.get("value"):
        um_per_px = float(pixel["value"])
        source = pixel.get("source") or "metadata"
        approx = "≈" if source == "estimated" else ""
        return {"side_px": field_um / um_per_px, "field_um": field_um,
                "um_per_px": um_per_px, "source": source,
                "label": f"{approx}{field_um:g} µm fields"}
    return {"side_px": float(preset_px), "field_um": None, "um_per_px": None,
            "source": "fallback_px", "label": f"{preset_px:g} px fields (no pixel size)"}


def _ascii(text):
    """What the bundled font can draw (no µ, no ≈)."""
    return text.replace("µ", "u").replace("≈", "~")


def _field_panel(session, record, bounds, layers, marker, low, high, pixel=None,
                 ids=None):
    from PIL import Image
    import io

    from plexora.agent import render
    from plexora.agent.render_spec import (Bounds, CellsSpec, ChannelSpec, IdHighlight,
                                           MarkerHighlight, OutputSpec, RenderInput)

    spec = RenderInput(
        project=record.name, bounds=Bounds(**bounds),
        channels=[ChannelSpec(name=name, color=color, window=list(window))
                  for name, color, window in layers],
        segmentation="none",
        cells=CellsSpec(highlight=(IdHighlight(cell_ids=ids) if ids is not None
                                   else MarkerHighlight(marker=marker, low=low, high=high)),
                        show_negative=False),
        output=OutputSpec(width=SHEET_PX, height=SHEET_PX), scale_bar=True)
    rendered = render.render_region(session, spec, store=False, pixel_size=pixel)
    image = Image.open(io.BytesIO(rendered["png"])).convert("RGB")
    return image, rendered["manifest"]


def _fit(image, width, height, background):
    """`image` letterboxed into (width, height), top-centred."""
    from PIL import Image

    if image.width > width or image.height > height:
        scale = min(width / image.width, height / image.height)
        image = image.resize((max(1, int(image.width * scale)),
                              max(1, int(image.height * scale))), Image.LANCZOS)
    out = Image.new("RGB", (width, height), background)
    out.paste(image, ((width - image.width) // 2, 0))
    return out


def _density(result, candidates):
    from plexora.agent.evidence import density_plot

    return density_plot.draw_density(result, size=BOTTOM_PX, compact=True,
                                     extra_a_gates=[(c["low"], c["id"])
                                                    for c in candidates or ()])


def _histogram(ds, marker, low, candidates, size):
    from plexora.agent import plots
    from plexora.plugins.gating.server.autogate import bivariate
    from plexora.plugins.gating.server.autogate import profile as profmod

    values = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    col = profmod.column(ds, marker)
    lo, hi = (float(col.from_fit(v)) for v in bivariate._axis_range(col))
    values = values[(values >= lo) & (values <= hi)]
    log_table = bool(ds.table.log_transformed)
    return plots.draw_histogram(
        values, gate=low, width=size[0], height=size[1], title=f"{marker}: every cell",
        log_axis=False if log_table else None,
        axis_note="table units (log1p)" if log_table else None,
        extra_gates=[(c["low"], c["id"]) for c in candidates or ()])


def render_context_sheet(session, ds, *, marker, channel, low, high=None, references=(),
                         partner=None, candidates=None, candidate_fields=None, seed=0,
                         fmt="webp", title=None, store=True, field_um=None, pixel=None,
                         positives_within=None):
    """{png, image, format, manifest, artifact, fields} for one marker's sheet.

    `marker` is the table column, `channel` its image channel; `references`
    extra image channels drawn in the fields (a T3 look); `partner` a
    reference gate `{marker, gate, relation, why?}` for the FACS plot;
    `candidates` `[{id, low}]` drawn on the plot, and `candidate_fields`
    `[{id, low, prev}]` one tissue field each (a T4 look); `field_um` the
    field side in microns and `pixel` the session's pixel size for an image
    that states none; `positives_within` `{marker, gate}` counts the
    whole-image positives among that partner's positives only (a conditional
    gate)."""
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts, gate_sampling
    from plexora.agent.evidence import calibration, collage
    from plexora.agent.presets import nuclear_channel
    from plexora.plugins.gating.server.autogate import tableops, views
    from plexora.server.utils import fast_png

    record = ds.project
    channel_names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
    nuclear = nuclear_channel(channel_names)
    refs = [r for r in references if r in channel_names and r != channel][:1]
    names = [n for n in (nuclear, channel, *refs) if n]
    windows = collage.resolve_windows(session, record, dict.fromkeys(names))
    top = float(high) if high is not None else float(np.nanmax(np.asarray(
        ds.table.columns([marker])[marker], dtype=np.float64)))
    width, height = sheet_size()
    canvas = Image.new("RGB", (width, height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    font, small = collage._font(11), collage._font(10)
    field = field_side_px(record, field_um, pixel)
    draw.rectangle((0, 0, width, TITLE_H - 1), fill=collage.HEADER_BG)
    draw.text((4, 1), _ascii(f"{title or f'{marker} · three scales'} · {field['label']}"),
              fill=collage.TEXT, font=font)

    # -- mesoscale: three fields, or one per candidate ------------------------
    side = field["side_px"]
    image_size = (record.image.width or 1, record.image.height or 1)
    layers = []
    if nuclear and nuclear != channel:
        layers.append((nuclear, calibration.NUCLEAR_MUTED_BLUE, windows[nuclear]["window"]))
    layers.append((channel, calibration.MARKER_COLOR, windows[channel]["window"]))
    for ref, colour in zip(refs, calibration.REFERENCE_COLORS):
        layers.append((ref, colour, windows[ref]["window"]))
    slots = []          # (label when empty, field or None, gate low, caption)
    sampled = {"classes_without_field": [], "band": None, "field_px": side}
    if candidate_fields:
        shown = []      # boxes already on the sheet: each candidate gets its own field
        for cand in list(candidate_fields)[:MAX_CANDIDATE_FIELDS]:
            lo, hi = sorted((float(cand["prev"]), float(cand["low"])))
            got = gate_sampling.sample_gate_validation_regions(
                ds, marker, float(cand["low"]), top, field_px=side, image_size=image_size,
                n_per_class=1, classes=("borderline",), seed=seed, band=(lo, hi),
                avoid=shown)
            found = got["fields"][0] if got["fields"] else None
            if found is not None:
                b = found["bounds"]
                shown.append((b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]))
                found = {**found, "field_id": f"f{len(shown)}"}
            flips = (int(found.get("borderline_below") or 0)
                     + int(found.get("borderline_above") or 0)) if found else 0
            slots.append((f"no other field with cells flipping at {cand['id']}", found,
                          float(cand["low"]),
                          f"{cand['id']} · {collage.compact_number(cand['low'])} · "
                          f"{flips} cells flip here", cand["id"]))
        sampled["band"] = "per candidate: between it and the threshold before it"
    else:
        sampled = gate_sampling.sample_gate_validation_regions(
            ds, marker, low, top, field_px=side, image_size=image_size, n_per_class=1,
            classes=CLASSES, seed=seed)
        by_class = {f["class"]: f for f in sampled["fields"]}
        for cls in CLASSES:
            found = by_class.get(cls)
            caption = (f"{found['field_id']} · {CLASS_LABELS[cls]} · "
                       f"{found['positives']}/{found['cells']} positive") if found else ""
            slots.append((f"no {CLASS_LABELS[cls]} field", found, float(low), caption, None))
    field_records = []
    y0 = TITLE_H
    for index, (empty, found, gate_low, caption, cand_id) in enumerate(slots):
        x = index * (SHEET_PX + GAP)
        if found is None:
            draw.text((x + 6, y0 + SHEET_PX // 2), empty, fill=collage.DIM, font=small)
            continue
        ids = (views.positive_ids_in(ds, marker, gate_low, top, found["bounds"],
                                     within=positives_within)
               if positives_within else None)
        if ids is not None:
            found = {**found, "positives": len(ids)}
            if cand_id is None:
                caption = (f"{found['field_id']} · {CLASS_LABELS[found['class']]} · "
                           f"{len(ids)}/{found['cells']} positive within "
                           f"{positives_within['marker']}+")
        panel, _manifest = _field_panel(session, record, found["bounds"], layers, marker,
                                        gate_low, top, pixel=pixel, ids=ids)
        canvas.paste(panel, (x, y0))
        draw.text((x + 2, y0 + SHEET_PX + 1), _ascii(caption), fill=collage.TEXT, font=small)
        entry = {k: found[k] for k in ("field_id", "class", "bounds", "cells", "positives",
                                       "positive_fraction", "borderline_below",
                                       "borderline_above") if k in found}
        if cand_id:
            entry.update(candidate=cand_id, low=gate_low)
        field_records.append(entry)

    # -- slide scale: the stain, the positives, the partner plot ---------------
    y1 = y0 + SHEET_PX + CAPTION_H + GAP
    plot_px = _plot_px()
    points = views.positive_points(ds, marker, low, high, within=positives_within)
    stain = collage.render_overview(session, ds, marker=channel, points=([], []), low=low,
                                    high=high, size=BOTTOM_PX, fmt="png", show_marker=True,
                                    title=f"{channel}: the stain", store=False)
    within_note = f" within {positives_within['marker']}+" if positives_within else ""
    positives = collage.render_overview(
        session, ds, marker=channel, points=points, low=low, high=high, size=BOTTOM_PX,
        fmt="png", show_marker=False,
        title=f"{marker}: {len(points[0])} positive{within_note}", store=False)
    import io

    for index, rendered in enumerate((stain, positives)):
        image = Image.open(io.BytesIO(rendered["png"])).convert("RGB")
        canvas.paste(_fit(image, SHEET_PX, plot_px, collage.BG),
                     (index * (SHEET_PX + GAP), y1))
    plot_manifest = {"kind": "histogram", "partner": None}
    if partner is not None:
        result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
            "a": marker, "gate_a": low, "b": partner["marker"], "gate_b": partner["gate"],
            "relation": partner["relation"], "with_grid": True, "seed": int(seed)})
        plot = _density(result, candidates)
        plot_manifest = {"kind": "density", "partner": partner["marker"],
                         "relation": partner["relation"],
                         "contradiction": result.get("contradiction"),
                         "quadrants": result.get("quadrants")}
        if partner.get("why"):
            plot_manifest["why"] = partner["why"]
    else:
        plot = _histogram(ds, marker, low, candidates, (SHEET_PX, plot_px))
    canvas.paste(_fit(plot, SHEET_PX, plot_px, collage.BG), (2 * (SHEET_PX + GAP), y1))
    captions = ("whole image: the stain", f"whole image: cells above the gate{within_note}",
                f"{marker} against {partner['marker']} ({partner['relation']})"
                if partner is not None else f"{marker}: the distribution and the gate")
    for index, caption in enumerate(captions):
        draw.text((index * (SHEET_PX + GAP) + 2, y1 + plot_px + 1), caption,
                  fill=collage.DIM, font=small)
    if candidates:
        plot_manifest["candidates"] = [{"id": c["id"], "low": c["low"]} for c in candidates]

    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    transported, transport_fmt = collage.encode(canvas, fmt)
    manifest = {
        "kind": MANIFEST_KIND, "project": record.name, "marker": marker, "channel": channel,
        "gate": {"low": float(low), "high": top},
        "fields": field_records, "classes_without_field": sampled["classes_without_field"],
        "band": sampled["band"], "field_px": sampled["field_px"],
        "field": {k: field[k] for k in ("field_um", "um_per_px", "source", "label")},
        "field_mode": "candidates" if candidate_fields else "classes", "seed": int(seed),
        "channels": [{"name": n, "key": windows[n]["key"], "window": windows[n]["window"],
                      "window_source": windows[n]["source"]} for n in names],
        "overview": {"level": positives["manifest"].get("level"),
                     "n_positive": int(len(points[0])),
                     **({"within": positives_within["marker"]} if positives_within else {})},
        "plot": plot_manifest, "size": [width, height],
        "estimated_vision_tokens": collage.estimated_tokens(width, height),
        "renderer": "plexora.gating.sheet/2", "egress": "rendered_pixels",
    }
    artifact = artifacts.put(record.name, png, manifest, kind="gating_context_sheet") \
        if store else None
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "artifact": artifact, "fields": field_records}
