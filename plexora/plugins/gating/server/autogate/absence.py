"""The absence check: does the marker have a positive population at all?

A person gating looks at a few regions of the tissue before trusting any
threshold, and when no region shows a cell that is clearly positive by its
own stain -- only the faint floor every cell has, glow from a neighbour,
debris -- they close the marker with no positives ("err on the safe side")
instead of gating noise. A mixture always splits a column in two, so the
numbers alone never say this; the regions do.

One picture per marker, before its first look: four regions of the tissue,
the marker on the engine's calibrated display with nuclei dim, nothing
marked (the reader judges the stain, not a gate). The regions are the ones
most likely to hold real positives if there are any (`bright`: the most
cells well above the start's band; `high_density`: crowded tissue such as
an aggregate) and the ones where a dim stain shows (`borderline`, `weak`).
Per region one word (`REAL_WORDS`); the marker is absent when at least
`MIN_JUDGED` regions were judged and none shows clear positives. A region
without positives is no evidence of absence while another shows them: the
weak and borderline regions are chosen to have few.

Measured before it was built (docs/internal/bench/GATEVISION_PHASE1_2026-10-18.md):
over 51 markers on three PCA images it closed the two the person called
absent and that the classic engine had gated, and no other.
"""

from __future__ import annotations

import io

import numpy as np

#: The region classes shown, in panel order (`gate_sampling.CLASSES`).
CLASSES = ("bright", "high_density", "borderline", "weak")

#: The words a region takes: `clear` -- cells clearly positive by their own
#: stain; `few` -- one or two such cells; `none`; `cannot_tell`.
REAL_WORDS = ("clear", "few", "none", "cannot_tell")

#: Regions that must be judged (not `cannot_tell`, not missing) before an
#: absence is called.
MIN_JUDGED = 2

#: [cal] the side of a region in microns: cells about 20 output pixels
#: across, rings and fills visible.
FIELD_UM = 200.0

#: [cal] one region's side in output pixels (2 x 2 regions a picture).
PANEL_PX = 400

#: [cal] the start's band, in background core sds each side: the cells near
#: the threshold a weak or borderline region is chosen for; a `bright`
#: region's cells sit above it.
BAND_SD = 1.0

#: The renderer's version (`memo.versions`): a change here must not replay
#: answers given to the old pictures.
RENDERER = "plexora.gating.absence/1"

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


def render(session, ds, *, marker, channel, unit, seed=0, fmt="webp", pixel=None):
    """{png, image, format, manifest, fields} for one marker's absence
    picture: up to four regions, unmarked, captioned r1..r4."""
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
        n_per_class=1, classes=CLASSES, seed=seed, band=the_band)
    by_class = {f["class"]: f for f in sampled["fields"]}
    found = [by_class[c] for c in CLASSES if c in by_class]

    width = 2 * PANEL_PX + GAP
    height = TITLE_H + 2 * (PANEL_PX + CAPTION_H) + GAP
    canvas = Image.new("RGB", (width, height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    font, small = collage._font(11), collage._font(10)
    draw.rectangle((0, 0, width, TITLE_H - 1), fill=collage.HEADER_BG)
    draw.text((4, 1), sheet._ascii(f"{marker}: yellow = stain, blue = nuclei, nothing marked "
                                   f"· {field['label']}"), fill=collage.TEXT, font=font)
    records = []
    for index, region in enumerate(found):
        name = f"r{index + 1}"
        x = (index % 2) * (PANEL_PX + GAP)
        y = TITLE_H + (index // 2) * (PANEL_PX + CAPTION_H + GAP)
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
        "classes_without_field": [c for c in CLASSES if c not in by_class],
        "band": list(the_band) if the_band else None, "field_px": side,
        "field": {k: field[k] for k in ("field_um", "um_per_px", "source", "label")},
        "seed": int(seed), "size": [width, height],
        "channels": [{"name": n, "key": windows[n]["key"], "window": windows[n]["window"],
                      "window_source": windows[n]["source"]} for n in names],
        "estimated_vision_tokens": collage.estimated_tokens(width, height),
        "renderer": RENDERER, "egress": "rendered_pixels",
    }
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "fields": records}
