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
counts) at the marker-validation preset's field size, drawn with the same
cell-anchored windows as the collage, with the cells the gate calls positive
outlined in magenta. The partner plot is the FACS view: the marker (x) against
the first gated partner (y), both gates drawn; without a partner, the
marker's distribution with its gate.
"""

from __future__ import annotations

import numpy as np

MANIFEST_KIND = "plexora.gating_context_sheet"
SHEET_PX = 256
CLASSES = ("borderline", "clear_positive", "clear_negative")
TITLE_H = 14
CAPTION_H = 14
GAP = 4

#: How each field class is captioned on the sheet.
CLASS_LABELS = {"borderline": "borderline", "clear_positive": "clearly positive",
                "clear_negative": "clearly negative"}


def _plot_px():
    from plexora.agent.evidence import collage

    return SHEET_PX + collage.OVERVIEW_TITLE_H


def sheet_size() -> tuple:
    """(width, height): fixed, whatever the image's shape (a missing field or
    a flat overview leaves a blank cell, never a smaller sheet)."""
    width = 3 * SHEET_PX + 2 * GAP
    height = TITLE_H + SHEET_PX + CAPTION_H + GAP + _plot_px() + CAPTION_H
    return width, height


def max_pixels() -> int:
    width, height = sheet_size()
    return width * height


def _field_side_px(record):
    from plexora.agent import presets
    from plexora.server.utils import pixel_scale

    field_um, field_px = presets.field_size("marker_validation")
    pixel = pixel_scale.pixel_size(record)
    if field_um and pixel:
        return float(field_um) / float(pixel["value"])
    return float(field_px or 512)


def _field_panel(session, record, bounds, layers, marker, low, high):
    from PIL import Image
    import io

    from plexora.agent import render
    from plexora.agent.render_spec import (Bounds, CellsSpec, ChannelSpec, MarkerHighlight,
                                           OutputSpec, RenderInput)

    spec = RenderInput(
        project=record.name, bounds=Bounds(**bounds),
        channels=[ChannelSpec(name=name, color=color, window=list(window))
                  for name, color, window in layers],
        segmentation="none",
        cells=CellsSpec(highlight=MarkerHighlight(marker=marker, low=low, high=high),
                        show_negative=False),
        output=OutputSpec(width=SHEET_PX, height=SHEET_PX), scale_bar=True)
    rendered = render.render_region(session, spec, store=False)
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

    return density_plot.draw_density(result, size=SHEET_PX, compact=True,
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
                         partner=None, candidates=None, seed=0, fmt="webp", title=None,
                         store=True):
    """{png, image, format, manifest, artifact, fields} for one marker's sheet.

    `marker` is the table column, `channel` its image channel; `references`
    extra image channels drawn in the fields (a T3 look); `partner` a
    reference gate `{marker, gate, relation}` for the FACS plot; `candidates`
    `[{id, low}]` drawn on the plot (a T4 look)."""
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
    draw.rectangle((0, 0, width, TITLE_H - 1), fill=collage.HEADER_BG)
    draw.text((4, 1), title or f"{marker} · three scales", fill=collage.TEXT, font=font)

    # -- mesoscale: three fields ---------------------------------------------
    side = _field_side_px(record)
    sampled = gate_sampling.sample_gate_validation_regions(
        ds, marker, low, top, field_px=side,
        image_size=(record.image.width or 1, record.image.height or 1), n_per_class=1,
        classes=CLASSES, seed=seed)
    by_class = {f["class"]: f for f in sampled["fields"]}
    layers = []
    if nuclear and nuclear != channel:
        layers.append((nuclear, calibration.NUCLEAR_MUTED_BLUE, windows[nuclear]["window"]))
    layers.append((channel, calibration.MARKER_COLOR, windows[channel]["window"]))
    for ref, colour in zip(refs, calibration.REFERENCE_COLORS):
        layers.append((ref, colour, windows[ref]["window"]))
    field_records = []
    y0 = TITLE_H
    for index, cls in enumerate(CLASSES):
        x = index * (SHEET_PX + GAP)
        field = by_class.get(cls)
        if field is None:
            draw.text((x + 6, y0 + SHEET_PX // 2), f"no {CLASS_LABELS[cls]} field",
                      fill=collage.DIM, font=small)
            draw.text((x + 2, y0 + SHEET_PX + 1), CLASS_LABELS[cls], fill=collage.DIM,
                      font=small)
            continue
        panel, _manifest = _field_panel(session, record, field["bounds"], layers, marker, low,
                                        top)
        canvas.paste(panel, (x, y0))
        draw.text((x + 2, y0 + SHEET_PX + 1),
                  f"{field['field_id']} · {CLASS_LABELS[cls]} · "
                  f"{field['positives']}/{field['cells']} positive",
                  fill=collage.TEXT, font=small)
        field_records.append({k: field[k] for k in ("field_id", "class", "bounds", "cells",
                                                    "positives", "positive_fraction",
                                                    "borderline_below", "borderline_above")})

    # -- slide scale: the stain, the positives, the partner plot ---------------
    y1 = y0 + SHEET_PX + CAPTION_H + GAP
    plot_px = _plot_px()
    points = views.positive_points(ds, marker, low, high)
    stain = collage.render_overview(session, ds, marker=channel, points=([], []), low=low,
                                    high=high, size=SHEET_PX, fmt="png", show_marker=True,
                                    title=f"{channel}: the stain", store=False)
    positives = collage.render_overview(
        session, ds, marker=channel, points=points, low=low, high=high, size=SHEET_PX,
        fmt="png", show_marker=False,
        title=f"{marker}: {len(points[0])} positive", store=False)
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
    else:
        plot = _histogram(ds, marker, low, candidates, (SHEET_PX, plot_px))
    canvas.paste(_fit(plot, SHEET_PX, plot_px, collage.BG), (2 * (SHEET_PX + GAP), y1))
    captions = ("whole image: the stain", "whole image: cells above the gate",
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
        "band": sampled["band"], "field_px": sampled["field_px"], "seed": int(seed),
        "channels": [{"name": n, "key": windows[n]["key"], "window": windows[n]["window"],
                      "window_source": windows[n]["source"]} for n in names],
        "overview": {"level": positives["manifest"].get("level"),
                     "n_positive": int(len(points[0]))},
        "plot": plot_manifest, "size": [width, height],
        "estimated_vision_tokens": collage.estimated_tokens(width, height),
        "renderer": "plexora.gating.sheet/1", "egress": "rendered_pixels",
    }
    artifact = artifacts.put(record.name, png, manifest, kind="gating_context_sheet") \
        if store else None
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "artifact": artifact, "fields": field_records}
