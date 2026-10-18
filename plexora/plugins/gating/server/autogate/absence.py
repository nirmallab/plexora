"""The absence check: does the marker have a positive population at all?

A person gating looks at a few regions of the tissue before trusting any
threshold, and when no region shows a cell that is clearly positive by its
own stain -- only the faint floor every cell has, glow from a neighbour,
debris -- they close the marker with no positives ("err on the safe side")
instead of gating noise. A mixture always splits a column in two, so the
numbers alone never say this; the regions do.

One picture per marker, before its first look: six regions of the tissue,
the marker on the engine's calibrated display with nuclei dim, nothing
marked (the reader judges the stain, not a gate). The regions are the ones
most likely to hold real positives if there are any (`clear_positive`, its
two best fields: the most cells well above the start's band; `bright`: the
brightest of them; `high_density`: crowded tissue such as an aggregate) and
the ones where a dim stain shows (`borderline`, `weak`), all from fields
about as full of cells as a typical one (`MIN_FILL`). Per region one
word (`REAL_WORDS`); a picture says absent when at least `MIN_JUDGED`
regions were judged and none shows clear positives. A region without
positives is no evidence of absence while another shows them: the weak and
borderline regions are chosen to have few.

A marker is closed only when `ROUNDS` pictures of different regions (each
avoiding the ones shown) say absent: four regions chosen around the start
missed LSP11361's dim, widespread E-cadherin (the person gates 26 % of the
cells positive) and closed it, where six regions over two packets of the
region-look study had shown it clear in three.

Measured before it was built (docs/internal/bench/GATEVISION_PHASE1_2026-10-18.md):
over 51 markers on three PCA images, six regions a marker closed the two the
person called absent and that the classic engine had gated, and no other.
"""

from __future__ import annotations

import io

import numpy as np

#: The region classes shown, in panel order (`gate_sampling.CLASSES`); a
#: class twice is its next best field.
CLASSES = ("clear_positive", "bright", "high_density", "borderline", "weak", "clear_positive")

#: [cal] a region is drawn only from fields at least this full of cells
#: relative to the typical field (`gate_sampling`'s `min_fill`): the most
#: positive and the brightest fields of a marker with no population are the
#: half-empty ones at a tissue edge, where stain pools (LSP11761 CD20L: a
#: 110-cell edge band read `clear`; typical fields hold 300-400).
MIN_FILL = 0.5

#: Panels a row of the picture.
COLUMNS = 3

#: Pictures of different regions that must all say absent before a marker
#: is closed (a second costs a packet only on a marker about to close).
ROUNDS = 2

#: The words a region takes: `clear` -- cells clearly positive by their own
#: stain; `few` -- one or two such cells; `none`; `cannot_tell`.
REAL_WORDS = ("clear", "few", "none", "cannot_tell")

#: Regions that must be judged (not `cannot_tell`, not missing) before an
#: absence is called.
MIN_JUDGED = 2

#: [cal] the side of a region in microns: cells about 20 output pixels
#: across, rings and fills visible.
FIELD_UM = 200.0

#: [cal] one region's side in output pixels (3 x 2 regions a picture).
PANEL_PX = 400

#: [cal] the start's band, in background core sds each side: the cells near
#: the threshold a weak or borderline region is chosen for; a `bright`
#: region's cells sit above it.
BAND_SD = 1.0

#: The renderer's version (`memo.versions`): a change here must not replay
#: answers given to the old pictures.
RENDERER = "plexora.gating.absence/3"

CAPTION_H = 14
GAP = 4
TITLE_H = 14


def absent(words) -> bool:
    """True when no judged region shows clear positives and enough regions
    were judged to say so."""
    judged = [w for w in (words or {}).values() if w in ("clear", "few", "none")]
    if len(judged) < MIN_JUDGED:
        return False
    return "clear" not in judged


def band(unit, col):
    """(low, high) table values around the unit's start: `BAND_SD` core sds
    each side in the fit space (the scored proposal's background sd, else
    the mixture's background sd)."""
    start = float(unit["candidate"])
    sd = (((unit.get("scoring") or {}).get("background") or {}).get("sd_fit")
          or (unit.get("metrics") or {}).get("sd_bg"))
    if not sd:
        return None
    g = float(col.to_fit(start))
    return (float(col.from_fit(g - BAND_SD * sd)), float(col.from_fit(g + BAND_SD * sd)))


def render(session, ds, *, marker, channel, unit, seed=0, fmt="webp", pixel=None,
           avoid=()):
    """{png, image, format, manifest, fields} for one marker's absence
    picture: up to six regions, unmarked, captioned r1..r6, none overlapping
    a box in `avoid` ((x0, y0, x1, y1), full-resolution pixels)."""
    from PIL import Image, ImageDraw

    from plexora.agent import gate_sampling, render as rendermod, sections
    from plexora.agent.evidence import calibration, collage
    from plexora.agent.presets import nuclear_channel
    from plexora.agent.render_spec import (Bounds, ChannelCurve, ChannelSpec, OutputSpec,
                                           RenderInput)
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate import sheet
    from plexora.server.utils import fast_png

    record = ds.project
    names_all = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
    nuclear = nuclear_channel(names_all)
    names = [n for n in (nuclear, channel) if n]
    windows = collage.resolve_windows(session, record, dict.fromkeys(names))
    col = profmod.column(ds, marker)
    top = float(unit.get("high")) if unit.get("high") is not None else float(col.sorted32[-1])
    field = sheet.field_side_px(record, FIELD_UM, pixel)
    side = field["side_px"]
    calibrated = calibration.current(session, record.name, names)

    def layer(name, colour):
        at = calibration.display_spec(calibrated, name, side / PANEL_PX) or {
            "window": windows[name]["window"], "curve": windows[name].get("curve")}
        return ChannelSpec(name=name, color=colour, window=list(at["window"]),
                           curve=ChannelCurve(**at["curve"]) if at.get("curve")
                           else ChannelCurve(kind="linear"))

    layers = []
    if nuclear and nuclear != channel:
        layers.append(layer(nuclear, calibration.NUCLEAR_MUTED_BLUE))
    layers.append(layer(channel, calibration.MARKER_COLOR))
    the_band = band(unit, col)
    image_size = (record.image.width or 1, record.image.height or 1)
    sampled = gate_sampling.sample_gate_validation_regions(
        ds, marker, float(unit["candidate"]), top, field_px=side, image_size=image_size,
        n_per_class=1, classes=CLASSES, seed=seed, band=the_band, avoid=tuple(avoid),
        min_fill=MIN_FILL)
    found = list(sampled["fields"])

    rows = max(1, -(-len(found) // COLUMNS))
    width = COLUMNS * PANEL_PX + (COLUMNS - 1) * GAP
    height = TITLE_H + rows * (PANEL_PX + CAPTION_H) + (rows - 1) * GAP
    canvas = Image.new("RGB", (width, height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    font, small = collage._font(11), collage._font(10)
    draw.rectangle((0, 0, width, TITLE_H - 1), fill=collage.HEADER_BG)
    draw.text((4, 1), sheet._ascii(f"{marker}: yellow = stain, blue = nuclei, nothing marked "
                                   f"· {field['label']}"), fill=collage.TEXT, font=font)
    records = []
    for index, region in enumerate(found):
        name = f"r{index + 1}"
        x = (index % COLUMNS) * (PANEL_PX + GAP)
        y = TITLE_H + (index // COLUMNS) * (PANEL_PX + CAPTION_H + GAP)
        plane = sections.field_plane(ds, region["bounds"])
        spec = RenderInput(project=record.name, bounds=Bounds(**region["bounds"]),
                           channels=layers, segmentation="none",
                           output=OutputSpec(width=PANEL_PX, height=PANEL_PX), scale_bar=True,
                           **({"view": f"z{plane}"} if plane is not None else {}))
        rendered = rendermod.render_region(session, spec, store=False, pixel_size=pixel)
        panel = Image.open(io.BytesIO(rendered["png"])).convert("RGB")
        canvas.paste(panel.resize((PANEL_PX, PANEL_PX)) if panel.size != (PANEL_PX, PANEL_PX)
                     else panel, (x, y))
        draw.text((x + 2, y + PANEL_PX + 1), name, fill=collage.TEXT, font=small)
        records.append({"region": name, "class": region["class"], "bounds": region["bounds"],
                        "cells": region.get("cells"),
                        **({"plane": plane} if plane is not None else {})})
    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    transported, transport_fmt = collage.encode(canvas, fmt)
    manifest = {
        "kind": "gating_absence_regions", "project": record.name, "marker": marker,
        "channel": channel, "regions": records,
        "classes_without_field": sampled["classes_without_field"],
        "band": list(the_band) if the_band else None, "field_px": side,
        "field": {k: field[k] for k in ("field_um", "um_per_px", "source", "label")},
        "seed": int(seed), "size": [width, height], "avoided": len(tuple(avoid)),
        "channels": [{"name": n, "key": windows[n]["key"], "window": windows[n]["window"],
                      "window_source": windows[n]["source"]} for n in names],
        "estimated_vision_tokens": collage.estimated_tokens(width, height),
        "renderer": RENDERER, "egress": "rendered_pixels",
    }
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "fields": records}


def boxes(regions):
    """The (x0, y0, x1, y1) boxes of regions already shown (`avoid`)."""
    out = []
    for region in regions or ():
        b = region.get("bounds") or {}
        if b:
            out.append((b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]))
    return out
