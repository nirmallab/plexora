"""The contrast check: markers in rows, five brightnesses in columns.

How bright a marker is drawn decides what a model sees in every later look:
too bright and every cell looks positive, too dark and the crop is black. The
calibration (`calibration`) fixes a curve per marker from its own cells; this
sheet puts that curve and four others -- the brightness ladder `columns_for`
builds around it, darker to brighter -- side by side, so a model can say where
the background reads as a faint floor and where the bright cells' structure
(a ring, a filled nucleus, granules) shows without washing out. Code picks the
column from the two answers (`calibration.pick_column`); the model never
types a window.

Each column of a row is a 2 x 2 block of level-0 crops centred on four cells
the calibration picked: a background (negative) cell, a dim one, and two of
the brightest. The nuclear stain is drawn in its calibrated muted blue, the
marker in yellow through the column's window and curve, and pixels at full
white in a fixed red (`calibration.CLIP_COLOR`), so saturation is visible as
such. Columns are labelled c1..c5 and nothing marks the calibration's own:
the picture does not suggest an answer. Per column the manifest records what
the numbers say (`levels`): how bright the background tile draws (its disc
p90), the share of the bright cells' disc at white, and how bright their
p99 draws -- the vetoes `pick_column` applies.

A second picture goes with it (`render_spread_strip`): per marker, a few of
its brightest cells and a few dim ones (`cell_window._spread`), each picked
far from the others, drawn at the rule's own column. It asks what the grid
cannot show: whether the brightest stain is the cells' own or debris, and
whether the dim cells carry the stain -- what gating should not be pulled up
by, and what it should not miss (`transitions.apply_display` keeps the
answers with the choice).
"""

from __future__ import annotations

import numpy as np

from plexora.agent.errors import AgentError
from plexora.agent.evidence.cell_window import CELL_WINDOW

RENDERER = "plexora.agent.evidence.display_grid/1"
MANIFEST_KIND = "plexora.display_grid"
SPREAD_RENDERER = "plexora.agent.evidence.display_spread/1"
SPREAD_KIND = "plexora.display_spread"

#: Pixels per crop tile, and the crop's side in typical cell diameters.
TILE_PX = 56
CROP_DIAMETERS = 3.0
#: Layout: the row label's width, gaps between columns and rows, header.
LABEL_PX = 74
COLUMN_GAP = 6
ROW_GAP = 4
HEADER_H = 18
#: Pixels at or above this brightness are drawn in the clip colour.
CLIP_AT = 0.999
#: The block's cells, in reading order (top-left, top-right, bottom row).
BLOCK = ("negative", "near", "bright", "bright")
#: The spread strip's tiles: bright cells b1.., then dim cells d1.., by the
#: counts the calibration picks (`cell_window.CELL_WINDOW`).
BRIGHT_TILES = tuple(f"b{i + 1}" for i in range(CELL_WINDOW["spread_bright"]))
DIM_TILES = tuple(f"d{i + 1}" for i in range(CELL_WINDOW["spread_dim"]))
#: The gap between the bright and the dim tiles of a strip row.
SPREAD_GAP = 10


def _rgb(hex_colour):
    value = hex_colour.lstrip("#")
    return {"r": int(value[0:2], 16), "g": int(value[2:4], 16), "b": int(value[4:6], 16)}


def _tile(crop, nuclear, nuclear_spec, marker, window, curve, size):
    """(uint8 RGB tile, marker brightness plane) for one crop at one column."""
    from PIL import Image

    from plexora.agent.evidence import calibration
    from plexora.server.utils import display_curve, source_image

    plane = crop.planes.get(marker)
    side = plane.shape[0] if plane is not None else size
    accumulator = np.zeros((side, side, 3), dtype=np.float32)
    if nuclear and nuclear != marker and crop.planes.get(nuclear) is not None and nuclear_spec:
        source_image.blend_plane(accumulator, crop.planes[nuclear], nuclear_spec["window"],
                                 _rgb(calibration.NUCLEAR_MUTED_BLUE),
                                 curve=nuclear_spec.get("curve"))
    level = None
    if plane is not None:
        source_image.blend_plane(accumulator, plane, window, _rgb(calibration.MARKER_COLOR),
                                 curve=curve)
        level = display_curve.transfer(plane, window, curve)
    pixels = source_image.finish(accumulator)
    if level is not None:
        clip = _rgb(calibration.CLIP_COLOR)
        pixels[level >= CLIP_AT] = (clip["r"], clip["g"], clip["b"])
    image = Image.fromarray(pixels, "RGB")
    if image.size != (size, size):
        image = image.resize((size, size), Image.LANCZOS)
    return np.array(image), level


def _disc(crop, level, radius_px):
    from plexora.agent.evidence import crops as cropmod

    if level is None:
        return None
    inside = level[cropmod.disc_mask(crop, radius_px)]
    return inside if inside.size else None


def render_display_grid(session, data, record, rows, *, fmt="webp", store=True,
                        tile_px=TILE_PX):
    """{png, image, format, manifest, artifact} for the rows `rows` =
    [{marker, channel, pattern?}] of one project, drawn from `record` (the
    project's calibration, v5: each row's channel entry carries `columns`,
    `curve`, `cells`)."""
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts
    from plexora.agent.evidence import calibration, collage
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.presets import nuclear_channel
    from plexora.server.utils import fast_png

    project = data.project
    channels = (record or {}).get("channels") or {}
    names = [c.get("fullname") or c.get("name") for c in project.image.real_channels]
    nuclear = nuclear_channel(names)
    if not rows:
        raise AgentError("invalid_input", "the contrast check has no rows")
    for row in rows:
        entry = channels.get(row["channel"]) or {}
        if not entry.get("columns") or not (entry.get("cells") or {}).get("bright"):
            raise AgentError("precondition_missing",
                             f"{row['channel']!r} has no cell calibration to check")
    radius, _how = cropmod.cell_radius_px(project)
    side = float(np.ceil(CROP_DIAMETERS * 2 * radius))
    keys = collage.resolve_windows(session, project,
                                   dict.fromkeys([n for n in (nuclear, *[r["channel"] for r in rows])
                                                  if n]))
    nuclear_spec = None
    if nuclear and nuclear in channels:
        nuclear_spec = calibration.display_spec(record, nuclear, side / tile_px)
    block = 2 * tile_px
    width = LABEL_PX + 5 * block + 4 * COLUMN_GAP
    height = HEADER_H + len(rows) * (block + ROW_GAP)
    canvas = Image.new("RGB", (width, height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    font, small = collage._font(11), collage._font(10)
    draw.rectangle((0, 0, width, HEADER_H - 1), fill=collage.HEADER_BG)
    for index, column_id in enumerate(calibration.COLUMN_IDS):
        x = LABEL_PX + index * (block + COLUMN_GAP)
        draw.text((x + block // 2 - 6, 2), column_id, fill=collage.TEXT, font=font)
    draw.text((3, 2), "darker -> brighter", fill=collage.DIM, font=small)
    manifest_rows = []
    reads = 0
    for row_index, row in enumerate(rows):
        channel = row["channel"]
        entry = channels[channel]
        cells = entry["cells"]
        picked = {"negative": (cells.get("negative") or [None])[0],
                  "near": (cells.get("near") or [None])[0]}
        bright = list(cells.get("bright") or [])
        picked["bright"] = bright[:2] + [None] * (2 - len(bright[:2]))
        wanted = [c for c in (picked["negative"], picked["near"], *picked["bright"]) if c]
        crops, info = cropmod.read_cell_crops(
            session, project, wanted,
            channels={n: keys[n]["key"] for n in (nuclear, channel) if n and n in keys},
            crop_px=side, tile_px=tile_px, mask=False)
        reads += int(info.get("reads") or 0)
        y = HEADER_H + row_index * (block + ROW_GAP)
        draw.text((3, y + block // 2 - 12), str(row["marker"])[:11], fill=collage.TEXT,
                  font=font)
        if row.get("short_pattern"):
            draw.text((3, y + block // 2 + 2), str(row["short_pattern"])[:12],
                      fill=collage.DIM, font=small)
        if calibration.SCALE == "linear":
            # The ladder the viewer's Linear canvas would draw: gains over the
            # marker's linear window, the column picked applied to it there.
            ladder = calibration.linear_columns(entry)
            black, curve = float(calibration.linear_window({**entry, "choice": None})[0]), None
        else:
            rule = entry.get("rule") or entry
            ladder = entry["columns"]
            black, curve = float(rule["window"][0]), calibration.entry_curve(rule)
        levels = {}
        for index, column in enumerate(ladder):
            window = (black, float(column["white"]))
            x0 = LABEL_PX + index * (block + COLUMN_GAP)
            bright_levels, negative_p90 = [], None
            for slot, cell in enumerate((picked["negative"], picked["near"], *picked["bright"])):
                tx = x0 + (slot % 2) * tile_px
                ty = y + (slot // 2) * tile_px
                crop = crops.get(int(cell["cell_id"])) if cell else None
                if crop is None:
                    continue
                tile, level = _tile(crop, nuclear, nuclear_spec, channel, window, curve, tile_px)
                tile = cropmod.mark_centre(np.ascontiguousarray(tile), crop, radius)
                canvas.paste(Image.fromarray(tile, "RGB"), (tx, ty))
                disc = _disc(crop, level, radius)
                if disc is None:
                    continue
                if slot == 0:
                    negative_p90 = float(np.percentile(disc, 90))
                elif slot >= 2:
                    bright_levels.append(disc)
            pooled = np.concatenate(bright_levels) if bright_levels else None
            levels[column["id"]] = {
                "negative_level": round(negative_p90, 4) if negative_p90 is not None else 0.0,
                "clipped_share": round(float((pooled >= CLIP_AT).mean()), 4)
                if pooled is not None else 0.0,
                "bright_level": round(float(np.percentile(pooled, 99)), 4)
                if pooled is not None else 1.0}
        manifest_rows.append({
            "marker": row["marker"], "channel": channel, "pattern": row.get("pattern"),
            "cells": {"negative": picked["negative"]["cell_id"] if picked["negative"] else None,
                      "near": picked["near"]["cell_id"] if picked["near"] else None,
                      "bright": [c["cell_id"] for c in picked["bright"] if c]},
            "black": black, "curve": curve,
            "columns": [{"id": c["id"], "gain": c["gain"], "white": c["white"]}
                        for c in ladder], "scale": calibration.SCALE,
            "levels": levels,
            "titre": (entry.get("anchors") or {}).get("titre")})
    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    transported, transport_fmt = collage.encode(canvas, fmt)
    manifest = {"kind": MANIFEST_KIND, "project": project.name, "rows": manifest_rows,
                "tile_px": int(tile_px), "crop_side_px": side, "radius_px": radius,
                "nuclear": nuclear, "block": list(BLOCK),
                "clip_color": calibration.CLIP_COLOR, "clip_at": CLIP_AT,
                "calibration": {"version": calibration.VERSION},
                "reads": reads, "size": [width, height],
                "estimated_vision_tokens": collage.estimated_tokens(width, height),
                "egress": "rendered_pixels", "renderer": RENDERER}
    artifact = artifacts.put(project.name, png, manifest, kind="gating_display_grid") \
        if store else None
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "artifact": artifact}


def spread_cells(entry) -> dict:
    """{tile id: cell} of a calibration entry's spread picks, in tile order."""
    spread = (entry.get("cells") or {}).get("spread") or {}
    out = {}
    for ids, cells in ((BRIGHT_TILES, spread.get("bright")), (DIM_TILES, spread.get("dim"))):
        for tile_id, cell in zip(ids, cells or ()):
            out[tile_id] = cell
    return out


def render_spread_strip(session, data, record, rows, *, fmt="webp", store=True,
                        tile_px=TILE_PX):
    """{png, image, format, manifest, artifact} for the rows `rows` (as
    `render_display_grid`'s): per row its spread bright tiles b1.. and dim
    tiles d1.. (`spread_cells`), each a level-0 crop drawn at the rule's own
    column. The manifest's `rows[].tiles` maps each tile id to its cell.
    Raises precondition_missing when no row has spread picks."""
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts
    from plexora.agent.evidence import calibration, collage
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.presets import nuclear_channel
    from plexora.server.utils import fast_png

    project = data.project
    channels = (record or {}).get("channels") or {}
    names = [c.get("fullname") or c.get("name") for c in project.image.real_channels]
    nuclear = nuclear_channel(names)
    rows = [r for r in rows or () if spread_cells(channels.get(r["channel"]) or {})]
    if not rows:
        raise AgentError("precondition_missing", "no marker has spread cells to show")
    radius, _how = cropmod.cell_radius_px(project)
    side = float(np.ceil(CROP_DIAMETERS * 2 * radius))
    keys = collage.resolve_windows(session, project,
                                   dict.fromkeys([n for n in (nuclear, *[r["channel"] for r in rows])
                                                  if n]))
    nuclear_spec = None
    if nuclear and nuclear in channels:
        nuclear_spec = calibration.display_spec(record, nuclear, side / tile_px)
    width = LABEL_PX + (len(BRIGHT_TILES) + len(DIM_TILES)) * tile_px + SPREAD_GAP
    height = HEADER_H + len(rows) * (tile_px + ROW_GAP)
    canvas = Image.new("RGB", (width, height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    font, small = collage._font(11), collage._font(10)
    draw.rectangle((0, 0, width, HEADER_H - 1), fill=collage.HEADER_BG)

    def x_of(tile_id):
        if tile_id in BRIGHT_TILES:
            return LABEL_PX + BRIGHT_TILES.index(tile_id) * tile_px
        return LABEL_PX + (len(BRIGHT_TILES) + DIM_TILES.index(tile_id)) * tile_px + SPREAD_GAP

    for tile_id in (*BRIGHT_TILES, *DIM_TILES):
        draw.text((x_of(tile_id) + tile_px // 2 - 6, 2), tile_id, fill=collage.TEXT, font=font)
    draw.text((3, 2), "bright | dim", fill=collage.DIM, font=small)
    manifest_rows, reads = [], 0
    for row_index, row in enumerate(rows):
        channel = row["channel"]
        entry = channels[channel]
        tiles = spread_cells(entry)
        crops, info = cropmod.read_cell_crops(
            session, project, list(tiles.values()),
            channels={n: keys[n]["key"] for n in (nuclear, channel) if n and n in keys},
            crop_px=side, tile_px=tile_px, mask=False)
        reads += int(info.get("reads") or 0)
        y = HEADER_H + row_index * (tile_px + ROW_GAP)
        draw.text((3, y + tile_px // 2 - 12), str(row["marker"])[:11], fill=collage.TEXT,
                  font=font)
        if row.get("short_pattern"):
            draw.text((3, y + tile_px // 2 + 2), str(row["short_pattern"])[:12],
                      fill=collage.DIM, font=small)
        rule = entry.get("rule") or entry
        column = next((c for c in entry.get("columns") or ()
                       if c["id"] == calibration.RULE_COLUMN), None)
        window = (float(rule["window"][0]),
                  float(column["white"]) if column else float(rule["window"][1]))
        curve = calibration.entry_curve(rule)
        drawn = {}
        for tile_id, cell in tiles.items():
            crop = crops.get(int(cell["cell_id"]))
            if crop is None:
                continue
            tile, _level = _tile(crop, nuclear, nuclear_spec, channel, window, curve, tile_px)
            tile = cropmod.mark_centre(np.ascontiguousarray(tile), crop, radius)
            canvas.paste(Image.fromarray(tile, "RGB"), (x_of(tile_id), y))
            drawn[tile_id] = {k: cell[k] for k in ("cell_id", "x", "y", "value") if k in cell}
        manifest_rows.append({"marker": row["marker"], "channel": channel,
                              "column": calibration.RULE_COLUMN, "tiles": drawn})
    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    transported, transport_fmt = collage.encode(canvas, fmt)
    manifest = {"kind": SPREAD_KIND, "project": project.name, "rows": manifest_rows,
                "tile_px": int(tile_px), "crop_side_px": side, "radius_px": radius,
                "nuclear": nuclear, "bright_tiles": list(BRIGHT_TILES),
                "dim_tiles": list(DIM_TILES), "clip_color": calibration.CLIP_COLOR,
                "calibration": {"version": calibration.VERSION},
                "reads": reads, "size": [width, height],
                "estimated_vision_tokens": collage.estimated_tokens(width, height),
                "egress": "rendered_pixels", "renderer": SPREAD_RENDERER}
    artifact = artifacts.put(project.name, png, manifest, kind="gating_display_spread")         if store else None
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "artifact": artifact}
