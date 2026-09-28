"""What an agent is shown: the QC evidence sheets.

- **Channel audit**: every channel, whole tissue, eight to a sheet, at the
  project's calibrated windows, with the scan's candidates outlined and
  numbered -- about a hundred vision tokens a channel, so every channel is
  looked at even when no detector fired.
- **Confirm**: one candidate at three scales (the whole channel with the
  outline, the neighbourhood, a close crop) plus the detector's own map or,
  at deeper levels, the nuclear stain and a matched clean field.
- **Scope**: the same place across channels.
- **Localize**: the candidate outlines to choose from; **grid**: labelled
  squares when none fits.
- **Review**: every QC region on the whole tissue, coloured by class.

Every sheet is a pure function of the scan, the calibration and the candidate,
stored as a PNG artifact (its manifest says how to draw it again) and sent as
WebP.
"""

from __future__ import annotations

import numpy as np

from plexora.agent.evidence import sheet_layout as layout
from plexora.plugins.qc.server import polygons, schemas

TILE_PX = 256
CHANNELS_PER_SHEET = 8
PANEL_PX = 384
GRID_PX = 704
REVIEW_PX = 960
#: [cal] a close crop's side (microns, or map cells without a pixel size).
CROP_UM = 200.0
CROP_CELLS = 6
MESO_FACTOR = 3.0
MESO_MIN_UM = 800.0
RENDERER = "plexora.qc.sheets/1"

NUCLEAR_COLOR = "#4f6fae"
CHANNEL_COLOR = "#ffffff"
MARKER_COLOR = "#ffd60a"
OUTLINE = "#ff3df2"
TISSUE_OUTLINE = "#22e6e6"
VARIANT_COLORS = {"tight": "#22e6e6", "standard": "#ff3df2", "generous": "#ffd60a",
                  "hull": "#84cc16", "bbox": "#f97316"}
VARIANT_LETTERS = {"tight": "A", "standard": "B", "generous": "C", "hull": "D", "bbox": "E"}


# -- regions --------------------------------------------------------------------------


def _clip_box(x0, y0, x1, y1, image_size):
    width, height = image_size
    w, h = x1 - x0, y1 - y0
    if w > width:
        x0, x1 = 0, width
    else:
        x0 = min(max(0, x0), width - w)
        x1 = x0 + w
    if h > height:
        y0, y1 = 0, height
    else:
        y0 = min(max(0, y0), height - h)
        y1 = y0 + h
    return {"x": float(x0), "y": float(y0), "width": float(max(1, x1 - x0)),
            "height": float(max(1, y1 - y0))}


def square_around(cx, cy, side, image_size):
    half = side / 2.0
    return _clip_box(cx - half, cy - half, cx + half, cy + half, image_size)


def padded(box, factor, min_side, image_size):
    x0, y0, x1, y1 = box
    side = max(min_side, factor * max(x1 - x0, y1 - y0))
    return square_around((x0 + x1) / 2, (y0 + y1) / 2, side, image_size)


def tissue_box(scan, pad_cells=1):
    grid = scan.grid
    mask = scan.tissue()
    box = polygons_bbox(mask, grid) or (0, 0, *grid["image_size"])
    s = grid["cell_full_px"]
    x0, y0, x1, y1 = box
    return _clip_box(x0 - pad_cells * s, y0 - pad_cells * s, x1 + pad_cells * s,
                     y1 + pad_cells * s, grid["image_size"])


def polygons_bbox(mask, grid):
    from plexora.plugins.qc.server.scan import bbox_fullres

    return bbox_fullres(grid, mask)


def crop_side(scan, pixel):
    if pixel:
        return CROP_UM / float(pixel["value"])
    return CROP_CELLS * scan.grid["cell_full_px"]


def meso_side(scan, pixel):
    if pixel:
        return MESO_MIN_UM / float(pixel["value"])
    return 16 * scan.grid["cell_full_px"]


# -- rendering ------------------------------------------------------------------------


def _window(calibration, name):
    from plexora.agent.evidence import calibration as display

    window, _source = display.window_for(calibration, name)
    return list(window) if window else "auto"


def _channel(name, color, calibration):
    from plexora.agent.render_spec import ChannelSpec

    return ChannelSpec(name=name, color=color, window=_window(calibration, name))


def _render(session, project, bounds, channels, size, *, pixel, shapes=None,
            scale_bar=True):
    from plexora.agent.render_spec import Bounds, OutputSpec, RenderInput

    spec = RenderInput(project=project, bounds=Bounds(**bounds), channels=channels,
                       segmentation="none", output=OutputSpec(width=size, height=size),
                       scale_bar=scale_bar, layers="none")
    picture, manifest = layout.render_panel(session, spec, pixel=pixel)
    if shapes:
        layout.shapes_onto(picture, manifest, shapes)
    return picture, manifest


def _shape(id, geometry=None, bounds=None, *, color=OUTLINE, width=2, dash=False, label="",
           fill_alpha=0.0):
    from plexora.agent.render_spec import Bounds, ShapeSpec

    return ShapeSpec(id=str(id)[:32], geometry=geometry,
                     bounds=Bounds(**bounds) if bounds else None, color=color, width=width,
                     dash=dash, label=label[:24], fill_alpha=fill_alpha)


def _finish(sheet, project, fmt, manifest, kind, *, store=True):
    from plexora.agent import artifacts
    from plexora.server.utils import fast_png

    manifest = {"schema_version": 1, "kind": kind, "renderer": RENDERER,
                "size": list(sheet.size), **manifest}
    artifact = None
    if store:
        png = fast_png.encode_rgb8_png(np.asarray(sheet.image))
        artifact = artifacts.put(project, png, manifest, kind=kind)
    data, fmt = sheet.encode(fmt)
    return {"image": data, "format": fmt, "manifest": manifest, "artifact": artifact,
            "size": sheet.size}


def _brief(manifest):
    return {"bounds": manifest["bounds_fullres"], "level": manifest["level"],
            "windows": {c["name"]: [round(v, 4) for v in c["window"]]
                        for c in manifest.get("channels") or []}}


# -- the channel audit ------------------------------------------------------------------


def audit_sheet(session, project, scan, rows, *, index, total, fmt, pixel, calibration,
                store=True):
    """One audit sheet: the nuclear reference and up to eight channels, whole
    tissue, candidates outlined and labelled c1..cn (`rows[i]["candidates"]`
    carry `label` and `geometry`)."""
    bounds = tissue_box(scan)
    sheet = layout.Sheet(3, 3, TILE_PX,
                         title=f"{project} - channel audit {index}/{total} - one tile per "
                               "channel, whole tissue, candidates outlined")
    tissue_geometry = polygons.mask_to_geometry(scan.tissue(), scan.grid)
    nuclear = scan.nuclear()
    tiles = []
    if nuclear:
        shapes = [_shape("tissue", tissue_geometry, color=TISSUE_OUTLINE, width=1)] \
            if tissue_geometry else []
        picture, manifest = _render(session, project, bounds,
                                    [_channel(nuclear, CHANNEL_COLOR, calibration)], TILE_PX,
                                    pixel=pixel, shapes=shapes)
        sheet.place(0, picture, f"ref | {nuclear} | tissue outline")
        tiles.append({"slot": 0, "name": nuclear, "role": "reference", **_brief(manifest)})
    else:
        sheet.blank(0, "no nuclear channel")
    for slot, row in enumerate(rows, start=1):
        shapes = [_shape(c["label"], c["geometry"], color=OUTLINE, width=2, dash=True,
                         label=c["label"])
                  for c in row.get("candidates") or [] if c.get("geometry")]
        picture, manifest = _render(session, project, bounds,
                                    [_channel(row["channel"], CHANNEL_COLOR, calibration)],
                                    TILE_PX, pixel=pixel, shapes=shapes, scale_bar=False)
        flags = ",".join(f[:4] for f in (row.get("flags") or [])[:2])
        caption = f"{row['number']} | {row['channel']}" + \
            (f" | c{row['cycle']}" if row.get("cycle") else "") + (f" | {flags}" if flags else "")
        sheet.place(slot, picture, caption)
        tiles.append({"slot": slot, "name": row["channel"], "cycle": row.get("cycle"),
                      "candidates": [c["label"] for c in row.get("candidates") or []],
                      **_brief(manifest)})
    for slot in range(len(rows) + 1, 9):
        sheet.blank(slot)
    return _finish(sheet, project, fmt, {"project": project, "tiles": tiles,
                                          "bounds": bounds, "page": [index, total]},
                   "plexora.qc_channel_audit", store=store)


# -- candidates -------------------------------------------------------------------------


def _clean_field(scan, candidate_mask, channel, side_cells):
    """The map cell of a matched clean field: core tissue, far from the
    candidate, the channel's typical level (deterministic argmin)."""
    from scipy import ndimage

    core = scan.tissue(core=True)
    grow = ndimage.binary_dilation(candidate_mask, iterations=max(2, int(side_cells)))
    allowed = core & ~grow
    if not allowed.any():
        allowed = core & ~candidate_mask
    if not allowed.any():
        return None
    median = scan.map(channel, "median")
    level = float(np.nanmedian(median[core])) if core.any() else 0.0
    cost = np.abs(np.log1p(np.maximum(np.nan_to_num(median), 0)) - np.log1p(max(level, 0)))
    cost = np.where(allowed, cost, np.inf)
    iy, ix = np.unravel_index(int(np.argmin(cost)), cost.shape)
    s = scan.grid["cell_full_px"]
    return (ix + 0.5) * s, (iy + 0.5) * s


def _heat_panel(scan, metric_key, mask, size):
    channel, _sep, metric = metric_key.partition("::")
    values = scan.map(channel, metric) if metric else scan.shared(channel)
    if values is None:
        values = mask.astype(np.float32)
    picture = layout.heatmap(values, size=(size, size), mask=scan.tissue())
    # The candidate's cells outlined on the map.
    from PIL import ImageDraw

    draw = ImageDraw.Draw(picture)
    ny, nx = mask.shape
    sy, sx = size / ny, size / nx
    ys, xs = np.nonzero(mask)
    for y, x in zip(ys, xs):
        draw.rectangle((x * sx, y * sy, (x + 1) * sx - 1, (y + 1) * sy - 1), outline=(255, 61, 242))
    return picture


def confirm_sheet(session, project, scan, candidate, mask, *, level, fmt, pixel, calibration,
                  store=True):
    """The confirm sheet of one candidate at look `level` (0, 1, 2)."""
    grid = scan.grid
    size = grid["image_size"]
    channel = candidate["channels"][0] if candidate.get("channels") else scan.nuclear()
    nuclear = scan.nuclear()
    geometry = (candidate.get("variants") or {}).get("standard", {}).get("geometry") \
        or candidate.get("geometry")
    box = candidate["bbox"]
    peak = candidate.get("peak") or [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
    outline = [_shape("candidate", geometry, color=OUTLINE, width=2)] if geometry else []
    dashed_box = [_shape("bbox", bounds={"x": box[0], "y": box[1], "width": box[2] - box[0],
                                         "height": box[3] - box[1]}, color=OUTLINE, width=1,
                         dash=True)]
    whole = tissue_box(scan)
    meso = padded(box, MESO_FACTOR, meso_side(scan, pixel), size)
    crop = square_around(peak[0], peak[1], crop_side(scan, pixel), size)
    if level >= 2:
        crop = square_around(peak[0], peak[1], crop_side(scan, pixel) / 2, size)
    ch = _channel(channel, CHANNEL_COLOR, calibration)
    marker = _channel(channel, MARKER_COLOR, calibration)
    ref = [_channel(nuclear, NUCLEAR_COLOR, calibration)] if nuclear and nuclear != channel else []
    panels = []
    words = schemas.CLASS_WORDS.get(candidate.get("class_hint"), candidate.get("class_hint"))
    if level == 0:
        panels.append((_render(session, project, whole, [ch], PANEL_PX, pixel=pixel,
                               shapes=outline + dashed_box, scale_bar=False),
                       f"{channel} | whole tissue | outline"))
        panels.append((_render(session, project, meso, [*ref, marker], PANEL_PX, pixel=pixel,
                               shapes=outline), f"{channel} | neighbourhood"))
        panels.append((_render(session, project, crop, [ch], PANEL_PX, pixel=pixel),
                       f"{channel} | close crop at the peak"))
        heat = _heat_panel(scan, candidate.get("primary_metric") or "", mask, PANEL_PX)
        panels.append(((heat, None), f"map: {candidate.get('primary_metric', '')}"))
    else:
        clean = _clean_field(scan, mask, channel, 3)
        panels.append((_render(session, project, meso, [*ref, marker], PANEL_PX, pixel=pixel,
                               shapes=outline), f"{channel} | neighbourhood"))
        panels.append((_render(session, project, crop, [ch], PANEL_PX, pixel=pixel),
                       f"{channel} | {'closer' if level >= 2 else 'close'} crop"))
        if nuclear and nuclear != channel:
            panels.append((_render(session, project, crop,
                                   [_channel(nuclear, CHANNEL_COLOR, calibration)], PANEL_PX,
                                   pixel=pixel), f"{nuclear} | same crop"))
        else:
            other = next((c for c in scan.channels if c["name"] != channel), None)
            if other:
                panels.append((_render(session, project, crop,
                                       [_channel(other["name"], CHANNEL_COLOR, calibration)],
                                       PANEL_PX, pixel=pixel), f"{other['name']} | same crop"))
        if clean is not None:
            clean_box = square_around(clean[0], clean[1], crop["width"], size)
            panels.append((_render(session, project, clean_box, [ch], PANEL_PX, pixel=pixel),
                           f"{channel} | clean field, same size"))
    sheet = layout.Sheet(2, 2, PANEL_PX, title=f"{project} - {candidate.get('label', '')} "
                                                f"suspected {words} in {channel} - look "
                                                f"{level + 1}")
    placed = []
    for slot, ((picture, manifest), caption) in enumerate(panels[:4]):
        sheet.place(slot, picture, caption)
        placed.append({"slot": slot, "caption": caption,
                       **(_brief(manifest) if manifest else {"kind": "map"})})
    return _finish(sheet, project, fmt, {"project": project, "candidate": candidate["id"],
                                          "level": level, "panels": placed,
                                          "channel": channel},
                   "plexora.qc_confirm", store=store)


def scope_sheet(session, project, scan, candidate, *, fmt, pixel, calibration, store=True):
    """The candidate's neighbourhood across up to eight other channels."""
    size = scan.grid["image_size"]
    box = candidate["bbox"]
    meso = padded(box, MESO_FACTOR, meso_side(scan, pixel), size)
    geometry = (candidate.get("variants") or {}).get("standard", {}).get("geometry") \
        or candidate.get("geometry")
    outline = [_shape("candidate", geometry, color=OUTLINE, width=2)] if geometry else []
    names = list(candidate.get("channels") or [])
    names += [c["name"] for c in scan.channels if c["name"] not in names]
    names = names[:9]
    sheet = layout.Sheet(3, 3, TILE_PX, title=f"{project} - the same place in "
                                               f"{len(names)} channels")
    tiles = []
    for slot, name in enumerate(names):
        picture, manifest = _render(session, project, meso,
                                    [_channel(name, CHANNEL_COLOR, calibration)], TILE_PX,
                                    pixel=pixel, shapes=outline, scale_bar=slot == 0)
        cycle = scan.channel(name).get("cycle")
        sheet.place(slot, picture, f"{name}" + (f" | c{cycle}" if cycle else ""))
        tiles.append({"slot": slot, "name": name, **_brief(manifest)})
    return _finish(sheet, project, fmt, {"project": project, "candidate": candidate["id"],
                                          "tiles": tiles}, "plexora.qc_scope", store=store)


def localize_sheet(session, project, scan, candidate, variants, *, fmt, pixel, calibration,
                   store=True):
    """The candidate outlines A..E, one per panel, and all of them together."""
    size = scan.grid["image_size"]
    channel = candidate["channels"][0] if candidate.get("channels") else scan.nuclear()
    box = candidate["bbox"]
    meso = padded(box, 2.0, meso_side(scan, pixel) / 2, size)
    ch = _channel(channel, CHANNEL_COLOR, calibration)
    sheet = layout.Sheet(3, 2, 320, title=f"{project} - which outline covers the "
                                           f"{schemas.CLASS_WORDS.get(candidate.get('class_hint'), '')}"
                                           f" in {channel}?")
    panels = []
    names = [n for n in ("tight", "standard", "generous", "hull", "bbox") if n in variants]
    for slot, name in enumerate(names):
        letter = VARIANT_LETTERS[name]
        shapes = [_shape(letter, variants[name]["geometry"], color=VARIANT_COLORS[name],
                         width=2, label=letter)]
        picture, manifest = _render(session, project, meso, [ch], 320, pixel=pixel,
                                    shapes=shapes, scale_bar=slot == 0)
        area = variants[name].get("area_um2")
        sheet.place(slot, picture, f"{letter} {name}" + (f" | {area / 1e6:.3g} mm2"
                                                         if area else ""))
        panels.append({"slot": slot, "id": letter, "variant": name, **_brief(manifest)})
    together = [_shape(VARIANT_LETTERS[n], variants[n]["geometry"], color=VARIANT_COLORS[n],
                       width=1, label=VARIANT_LETTERS[n]) for n in names]
    picture, manifest = _render(session, project, meso, [ch], 320, pixel=pixel,
                                shapes=together, scale_bar=False)
    sheet.place(min(5, len(names)), picture, "all outlines")
    return _finish(sheet, project, fmt, {"project": project, "candidate": candidate["id"],
                                          "panels": panels}, "plexora.qc_localize",
                   store=store)


def grid_sheet(session, project, scan, candidate, spec, *, fmt, pixel, calibration,
               store=True):
    """The candidate's region under a labelled grid (squares A1..H8)."""
    from PIL import ImageDraw

    s = scan.grid["cell_full_px"]
    y0, x0, y1, x1 = spec["region_cells"]
    width, height = scan.grid["image_size"]
    bounds = {"x": x0 * s, "y": y0 * s, "width": max(1.0, min(width, x1 * s) - x0 * s),
              "height": max(1.0, min(height, y1 * s) - y0 * s)}
    channel = candidate["channels"][0] if candidate.get("channels") else scan.nuclear()
    ch = _channel(channel, CHANNEL_COLOR, calibration)
    picture, manifest = _render(session, project, bounds, [ch], GRID_PX, pixel=pixel,
                                scale_bar=False)
    plain, plain_manifest = _render(session, project, bounds, [ch], GRID_PX, pixel=pixel)
    draw = ImageDraw.Draw(picture)
    for square in spec["squares"]:
        bx0, by0, bx1, by1 = square["bounds"]
        px0, py0 = layout.to_panel_px(manifest, picture, bx0, by0)
        px1, py1 = layout.to_panel_px(manifest, picture, bx1, by1)
        draw.rectangle((px0, py0, px1 - 1, py1 - 1), outline=(34, 230, 230), width=1)
        draw.text((px0 + 3, py0 + 2), square["id"], fill=(255, 255, 255), font=layout.font(11),
                  stroke_width=2, stroke_fill=(0, 0, 0))
    sheet = layout.Sheet(2, 1, GRID_PX, title=f"{project} - name the squares the artifact "
                                               f"covers in {channel}")
    sheet.place(0, picture, "grid over the region")
    sheet.place(1, plain, f"{channel} | the same region, no grid")
    return _finish(sheet, project, fmt, {"project": project, "candidate": candidate["id"],
                                          "grid": {"rows": spec["rows"],
                                                   "columns": spec["columns"]},
                                          "bounds": bounds, **_brief(manifest)},
                   "plexora.qc_grid", store=store)


def review_sheet(session, project, scan, regions, *, fmt, pixel, calibration, store=True):
    """Every QC region on the whole tissue: excluded filled, warnings dashed,
    one colour per class; beside it the regions over the nuclear stain."""
    from plexora.agent.render_spec import ShapeSpec  # noqa: F401

    bounds = tissue_box(scan)
    nuclear = scan.nuclear() or (scan.channels[0]["name"] if scan.channels else None)
    channels = [_channel(nuclear, CHANNEL_COLOR, calibration)] if nuclear else []
    shapes = []
    for index, region in enumerate(regions):
        color = schemas.CLASS_COLORS.get(region["class"], OUTLINE)
        exclude = region.get("action") == "exclude"
        shapes.append(_shape(f"r{index + 1}", region["geometry"], color=color,
                             width=2, dash=not exclude, fill_alpha=0.3 if exclude else 0.0,
                             label=region.get("label", "")))
    picture, manifest = _render(session, project, bounds, channels, REVIEW_PX // 2,
                                pixel=pixel, shapes=shapes)
    plain, _m = _render(session, project, bounds, channels, REVIEW_PX // 2, pixel=pixel,
                        scale_bar=False)
    sheet = layout.Sheet(2, 1, REVIEW_PX // 2, title=f"{project} - every QC region "
                                                      "(filled: excluded, dashed: warning)")
    sheet.place(0, picture, "QC regions by class")
    sheet.place(1, plain, f"{nuclear} | the same view, no overlay")
    legend = sorted({r["class"] for r in regions})
    return _finish(sheet, project, fmt, {"project": project, "regions": len(regions),
                                          "legend": {k: schemas.CLASS_COLORS.get(k)
                                                     for k in legend}, **_brief(manifest)},
                   "plexora.qc_review", store=store)
