"""What one pixel is worth, when the image does not say: from the cells' own size.

A context sheet's fields are sized in microns (a gland, a vessel, a lymphoid
aggregate have a size in the tissue, not on the sensor), and a collage's crop
too. An image that states no pixel size would otherwise get fields of a fixed
pixel count -- 400 µm on one scanner, 100 µm on another. So a session
estimates the scale from what every image of cells has: cells. The median
equivalent diameter of the segmented cells, in pixels, against a prior for
how wide a segmented cell is in microns (`PRIOR`), gives microns per pixel
and a range.

The estimate is a starting point for the agent's check, never a calibration:
the `pixel_setup` packet shows three fields of nuclei and outlines with a
scale bar drawn at the estimate and a 10 µm reference ring, and the agent
confirms it, adjusts it, or passes on what the user said. Only a value the
user stated is written to the project (`set_pixel_size`); the rest stays with
the session, and every scale bar drawn from it is marked approximate.
"""

from __future__ import annotations

import io

import numpy as np

#: [cal] how wide a segmented cell is, in microns: its typical value and the
#: range a real tissue's median falls in (a nuclear mask near the bottom, a
#: whole-cell mask of large cells near the top).
PRIOR = {"cell_diameter_um": 9.0, "range_um": (6.0, 14.0)}

#: [cal] how the cells are measured: a seeded sample, each cell's own mask read
#: in a crop wide enough for a large cell at a fine scale.
MEASURE = {"n_cells": 200, "crop_px": 128, "min_cells": 30}

#: [cal] the three snapshots: fields at dense, typical and sparse cell density,
#: each this many image pixels across and drawn this many screen pixels wide.
SNAPSHOTS = {"field_px": 256, "output_px": 320, "density_quantiles":
             (("dense", 0.9), ("typical", 0.5), ("sparse", 0.15)), "ring_um": 10.0}

#: The accepted range of an answer, microns per pixel.
BOUNDS = (0.05, 5.0)

TITLE_H = 16
CAPTION_H = 14
GAP = 4


def estimate(areas_px, prior=PRIOR, *, min_cells=MEASURE["min_cells"]) -> dict | None:
    """{microns_per_pixel, low, high, n, median_diameter_px, ...} from cell
    areas in square pixels, or None with too few cells to say."""
    areas = np.asarray(list(areas_px), dtype=np.float64)
    areas = areas[np.isfinite(areas) & (areas > 0)]
    if areas.size < int(min_cells):
        return None
    diameters = 2.0 * np.sqrt(areas / np.pi)
    median = float(np.median(diameters))
    if not median > 0:
        return None
    low_um, high_um = prior["range_um"]
    return {"microns_per_pixel": float(prior["cell_diameter_um"]) / median,
            "low": float(low_um) / median, "high": float(high_um) / median,
            "n": int(areas.size), "median_diameter_px": median,
            "iqr_diameter_px": [float(np.percentile(diameters, 25)),
                                float(np.percentile(diameters, 75))],
            "prior": {"cell_diameter_um": float(prior["cell_diameter_um"]),
                      "range_um": [float(low_um), float(high_um)]}}


def _sample(ds, seed, n):
    from plexora.plugins.gating.server.autogate import cells as cellmod

    c = cellmod.cells(ds)
    index = cellmod.seeded_subset(np.flatnonzero(c.valid), n, seed)
    return c, index


def measure_areas(session, ds, *, seed=0, spec=MEASURE):
    """(areas in square full-resolution pixels, how): each sampled cell's own
    mask, read at level 0; without a mask, the table's area column (taken to
    be in pixels, which is what it is for an image with no pixel size)."""
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.presets import nuclear_channel
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    c, index = _sample(ds, seed, int(spec["n_cells"]))
    record = ds.project
    if record.segmentation.available and index.size:
        records = list(record.image.real_channels)
        names = [r.get("fullname") or r.get("name") for r in records]
        name = nuclear_channel(names) or names[0]
        _i, found = resolve_channel(name, records)
        cells = [{"cell_id": int(c.ids[i]), "x": float(c.xs[i]), "y": float(c.ys[i])}
                 for i in index]
        try:
            crops, _info = cropmod.read_cell_crops(
                session, record, cells, channels={name: source_image.channel_key(found)},
                crop_px=int(spec["crop_px"]), tile_px=int(spec["crop_px"]), mask=True, level=0)
        except Exception:
            crops = {}
        areas = [crop.mask_stats["area_level_px"] for crop in crops.values()
                 if crop.mask_stats and crop.mask_stats.get("area_level_px")]
        if len(areas) >= int(spec["min_cells"]):
            return areas, "mask"
    if c.area is not None and index.size:
        return [float(v) for v in c.area[index]], f"table column {c.area_column!r}"
    return [], "none"


def estimate_for(session, ds, *, seed=0) -> dict:
    """The estimate for one image, with how its cells were measured; `{"how":
    ..., "microns_per_pixel": None}` when nothing could be measured."""
    areas, how = measure_areas(session, ds, seed=seed)
    found = estimate(areas)
    if found is None:
        return {"microns_per_pixel": None, "how": how, "n": len(areas)}
    return {**found, "how": how}


def snapshot_fields(ds, *, spec=SNAPSHOTS):
    """[{name, bounds}] of three fields centred on cells at dense, typical and
    sparse neighbour density (clamped to the image)."""
    from plexora.plugins.gating.server.autogate import cells as cellmod

    c = cellmod.cells(ds)
    valid = np.flatnonzero(c.valid)
    if not valid.size:
        return []
    density = cellmod.neighbour_density(ds)[valid]
    order = valid[np.argsort(density, kind="stable")]
    width = float(ds.project.image.width or 1)
    height = float(ds.project.image.height or 1)
    side = float(min(spec["field_px"], width, height))
    out = []
    for name, q in spec["density_quantiles"]:
        i = order[min(order.size - 1, int(q * (order.size - 1)))]
        x0 = float(np.clip(c.xs[i] - side / 2, 0, max(0.0, width - side)))
        y0 = float(np.clip(c.ys[i] - side / 2, 0, max(0.0, height - side)))
        out.append({"name": name, "bounds": {"x": x0, "y": y0, "width": side,
                                             "height": side}})
    return out


def _bar(draw, x, y, um_per_screen_px, label_font, approximate=True):
    """A scale bar near a snapshot's bottom-left, at the estimate."""
    from plexora.agent.render import nice_length

    length_um = nice_length(um_per_screen_px * SNAPSHOTS["output_px"] / 4)
    if not length_um:
        return None
    length = int(round(length_um / um_per_screen_px))
    draw.rectangle((x - 1, y - 5, x + length + 1, y + 1), fill=(0, 0, 0))
    draw.rectangle((x, y - 4, x + length, y), fill=(255, 255, 255))
    draw.text((x, y - 18), f"{'~' if approximate else ''}{length_um:g} um",
              fill=(255, 255, 255), font=label_font, stroke_width=2, stroke_fill=(0, 0, 0))
    return {"length_um": float(length_um), "length_px": length}


def render_snapshots(session, ds, found, *, seed=0, fmt="webp", store=True):
    """{png, image, format, manifest, artifact}: three fields of nuclei and
    outlines side by side, each with a scale bar and a 10 µm ring drawn at the
    estimate (`found["microns_per_pixel"]`), or a bar in pixels without one."""
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts, render
    from plexora.agent.evidence import collage
    from plexora.agent.presets import nuclear_channel
    from plexora.agent.render_spec import Bounds, ChannelSpec, OutputSpec, RenderInput
    from plexora.server.utils import fast_png

    record = ds.project
    names = [r.get("fullname") or r.get("name") for r in record.image.real_channels]
    nuclear = nuclear_channel(names) or names[0]
    window = collage.resolve_windows(session, record, {nuclear: None})[nuclear]["window"]
    fields = snapshot_fields(ds)
    out_px = int(SNAPSHOTS["output_px"])
    width = len(fields) * out_px + max(0, len(fields) - 1) * GAP
    height = TITLE_H + out_px + CAPTION_H
    canvas = Image.new("RGB", (max(width, out_px), height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    font, small = collage._font(11), collage._font(10)
    mpp = (found or {}).get("microns_per_pixel")
    title = (f"{record.name}: nuclei and outlines; bar and ring at ~{mpp:.3g} um/px (estimate)"
             if mpp else f"{record.name}: nuclei and outlines; no estimate, bar in image px")
    draw.rectangle((0, 0, canvas.width, TITLE_H - 1), fill=collage.HEADER_BG)
    draw.text((4, 2), title, fill=collage.TEXT, font=font)
    shown = []
    for index, field in enumerate(fields):
        spec = RenderInput(project=record.name, bounds=Bounds(**field["bounds"]),
                           channels=[ChannelSpec(name=nuclear, color="#8fb4ff",
                                                 window=list(window))],
                           segmentation="outlines", output=OutputSpec(width=out_px,
                                                                      height=out_px),
                           scale_bar=False)
        rendered = render.render_region(session, spec, store=False)
        panel = Image.open(io.BytesIO(rendered["png"])).convert("RGB")
        x = index * (out_px + GAP)
        canvas.paste(panel, (x, TITLE_H))
        image_px_per_screen = field["bounds"]["width"] / float(out_px)
        pdraw = ImageDraw.Draw(canvas)
        bar = ring = None
        if mpp:
            um_per_screen = mpp * image_px_per_screen
            bar = _bar(pdraw, x + 12, TITLE_H + out_px - 10, um_per_screen, small)
            radius = SNAPSHOTS["ring_um"] / um_per_screen / 2.0
            cx, cy = x + out_px - 12 - radius, TITLE_H + 12 + radius
            pdraw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                          outline=(255, 214, 10), width=2)
            ring = {"diameter_um": SNAPSHOTS["ring_um"], "diameter_screen_px": 2 * radius}
        else:
            length = int(round(50 / image_px_per_screen))
            pdraw.rectangle((x + 12, TITLE_H + out_px - 14, x + 12 + length,
                             TITLE_H + out_px - 10), fill=(255, 255, 255))
            pdraw.text((x + 12, TITLE_H + out_px - 30), "50 px", fill=(255, 255, 255),
                       font=small, stroke_width=2, stroke_fill=(0, 0, 0))
            bar = {"length_image_px": 50}
        draw.text((x + 2, TITLE_H + out_px + 1), f"{field['name']} cells", fill=collage.DIM,
                  font=small)
        shown.append({**field, "scale_bar": bar, "ring": ring,
                      "image_px_per_screen_px": image_px_per_screen})
    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    transported, transport_fmt = collage.encode(canvas, fmt)
    manifest = {"kind": "plexora.gating_pixel_snapshots", "project": record.name,
                "channel": nuclear, "estimate": found, "fields": shown,
                "size": [canvas.width, canvas.height],
                "estimated_vision_tokens": collage.estimated_tokens(canvas.width,
                                                                    canvas.height),
                "renderer": "plexora.gating.pixel_snapshots/1", "egress": "rendered_pixels"}
    artifact = artifacts.put(record.name, png, manifest, kind="gating_pixel_snapshots") \
        if store else None
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "artifact": artifact}
