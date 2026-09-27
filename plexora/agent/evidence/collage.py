"""Collages: many cells, few pixels, a fixed layout per question.

Vision tokens scale with pixel AREA, not bytes, so the budget is chosen first
and the layout fills it: rows of cells, each cell a small block of panels, a
one-line label per row and a six-character caption per cell. The numbers stay
in the JSON that travels with the picture; the picture carries what only
pixels can -- whether a stain is where it should be.

Panels per cell (`PANELS`):

- `nuclear`       the nuclear stain alone, grey;
- `marker`        the marker alone, white, at its calibrated window;
- `merge`         nuclear in muted blue, the marker in yellow, the cell's own
                  outline in magenta and its neighbours' in grey;
- `gate_relative` the marker on a log scale centred on the gate, so a cell
                  brighter than the gate is brighter than mid-grey whatever
                  the display window;
- `ref:<name>`    a reference channel alone, white.

Every collage is stored as PNG (content-addressed, deterministic) and returned
as WebP for transport -- fewer bytes over the wire, the same pixels and the
same token count.
"""

from __future__ import annotations

import io
import math

import numpy as np

from plexora.agent.errors import AgentError

MANIFEST_KIND = "plexora.gating_collage"

#: Pixels per vision token (Claude's rule of thumb; used for estimates only).
from plexora.agent.sessions.budget import PIXELS_PER_TOKEN  # noqa: E402,F401  (one source)

#: Layout name -> (panels per cell, grid of panels per cell (cols, rows),
#: cells per row, the side the whole collage must fit in).
#: `offered`: a layout the collage tool draws for an agent (the rest are
#: drawn by gating sessions and the report only).
LAYOUTS = {
    "t2": {"panels": ("nuclear", "marker", "merge", "gate_relative"), "grid": (2, 2),
           "per_row": 6, "max_width": 1024, "offered": True},
    "t3": {"panels": ("nuclear", "marker", "merge", "ref"), "grid": (2, 2),
           "per_row": 6, "max_width": 1024, "offered": True},
    "flips": {"panels": ("marker", "merge"), "grid": (2, 1), "per_row": 8,
              "max_width": 1024, "offered": True},
    "strip": {"panels": ("marker", "merge"), "grid": (2, 1), "per_row": 6,
              "max_width": 832, "row_label_px": 64},
    "quadrants": {"panels": ("a", "b"), "grid": (2, 1), "per_row": 6, "max_width": 768,
                  "offered": True},
    "comparison": {"panels": ("marker", "merge"), "grid": (2, 1), "per_row": 6,
                   "max_width": 768},
    "strata": {"panels": ("marker", "merge"), "grid": (2, 1), "per_row": 6,
               "max_width": 768, "offered": True},
    # The review report's cells: larger tiles, all four panels, a page wide.
    "report": {"panels": ("nuclear", "marker", "merge", "gate_relative"), "grid": (2, 2),
               "per_row": 6, "max_width": 1280},
}

#: The whole image with positives marked (`render_overview`), not a grid layout.
OVERVIEW = "overview"

#: What the collage tool offers, in the order it lists them.
LAYOUT_NAMES = tuple(name for name, spec in LAYOUTS.items() if spec.get("offered")) + (OVERVIEW,)

#: A collage tile's default side in pixels (a layout's width may shrink it).
#: Large enough that a membrane ring reads as a ring, not a smudge.
TILE_PX = 80

#: The collage's fixed geometry (a title bar, a label over each row, a caption
#: under each cell, the gap between cells), and the overview's title bar.
HEADER_H = 14
ROW_HEADER_H = 12
CAPTION_H = 11
GAP = 2
EMPTY_ROW_H = 13
OVERVIEW_TITLE_H = 14

NUCLEAR_GREY = "#c8c8c8"
WHITE = "#ffffff"
BG = (14, 16, 20)
HEADER_BG = (28, 31, 38)
TEXT = (232, 232, 232)
DIM = (150, 156, 166)
POSITIVE_TEXT = (255, 110, 245)


def _font(size):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover
        return ImageFont.load_default()


def compact_number(value):
    """1234 -> '1.2k', 0.0123 -> '.012' -- at most five characters."""
    if value is None or not np.isfinite(value):
        return "n/a"
    a = abs(value)
    sign = "-" if value < 0 else ""
    if a >= 1e6:
        return f"{sign}{a / 1e6:.1f}M"
    if a >= 1e4:
        return f"{sign}{a / 1e3:.0f}k"
    if a >= 1e3:
        return f"{sign}{a / 1e3:.1f}k"
    if a >= 100:
        return f"{sign}{a:.0f}"
    if a >= 10:
        return f"{sign}{a:.1f}"
    if a >= 1:
        return f"{sign}{a:.2f}"
    return f"{sign}{a:.3f}".replace("0.", ".", 1)


def encode(image, fmt):
    """(bytes, format) -- WebP (quality 85) or PNG."""
    from plexora.server.utils import fast_png

    if fmt == "webp":
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=85, method=4)
        return buffer.getvalue(), "webp"
    return fast_png.encode_rgb8_png(np.asarray(image)), "png"


def estimated_tokens(width, height):
    from plexora.agent.sessions.budget import vision_tokens

    return vision_tokens(int(width) * int(height))


def tile_side(layout, tile_px=TILE_PX):
    """The tile side a layout draws at: `tile_px`, shrunk to fit its width."""
    spec = LAYOUTS[layout]
    per_row = spec["per_row"]
    grid_cols = spec["grid"][0]
    fit = (spec["max_width"] - spec.get("row_label_px", 0) - (per_row - 1) * GAP) \
        // (per_row * grid_cols)
    return int(max(24, min(int(tile_px), fit)))


def layout_size(layout, rows=3, tile_px=TILE_PX):
    """(width, height) of a collage with `rows` full rows (what a budget pins)."""
    spec = LAYOUTS[layout]
    per_row = spec["per_row"]
    grid_cols, grid_rows = spec["grid"]
    side = tile_side(layout, tile_px)
    row_label_px = spec.get("row_label_px", 0)
    row_header_h = 0 if row_label_px else ROW_HEADER_H
    width = row_label_px + per_row * grid_cols * side + (per_row - 1) * GAP
    height = HEADER_H + rows * (row_header_h + grid_rows * side + CAPTION_H + GAP)
    return width, height


def layout_pixels(layout, rows=3, tile_px=TILE_PX):
    width, height = layout_size(layout, rows, tile_px)
    return width * height


def resolve_windows(session, record, names):
    """{name: {key, window, source}} from the stored calibration, else computed."""
    from plexora.agent.evidence import calibration
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    channel_records = list(record.image.real_channels)
    wanted = {}
    for name in names:
        _index, found = resolve_channel(name, channel_records)
        wanted[found.get("fullname") or found.get("name")] = source_image.channel_key(found)
    stored = calibration.load(record.name)
    have = (stored or {}).get("channels") or {}
    missing = {n: k for n, k in wanted.items() if n not in have}
    if missing:
        fresh = calibration.current(session, record.name, list(wanted))
        have = {**have, **fresh.get("channels", {})}
    return {n: {"key": k, "window": have[n]["window"],
                "source": have[n].get("window_source", "calib")} for n, k in wanted.items()}


def _panel(crop, kind, spec):
    from plexora.agent.evidence import crops as cropmod

    size = spec["tile_px"]
    windows = spec["windows"]
    nuclear, marker = spec.get("nuclear"), spec.get("marker")
    if kind == "nuclear":
        if not nuclear:
            return np.zeros((size, size, 3), dtype=np.uint8)
        return cropmod.blend(crop, [{"name": nuclear, "window": windows[nuclear]["window"],
                                     "color": NUCLEAR_GREY}], size)
    if kind in ("marker", "a"):
        name = marker if kind == "marker" else spec["a"]
        return cropmod.blend(crop, [{"name": name, "window": windows[name]["window"],
                                     "color": WHITE}], size)
    if kind == "b":
        name = spec["b"]
        return cropmod.blend(crop, [{"name": name, "window": windows[name]["window"],
                                     "color": WHITE}], size)
    if kind == "merge":
        from plexora.agent.evidence import calibration

        layers = []
        if nuclear:
            layers.append({"name": nuclear, "window": windows[nuclear]["window"],
                           "color": calibration.NUCLEAR_MUTED_BLUE})
        layers.append({"name": marker, "window": windows[marker]["window"],
                       "color": calibration.MARKER_COLOR})
        panel = cropmod.blend(crop, layers, size)
        return cropmod.outline(panel, crop)
    if kind == "gate_relative":
        return cropmod.log_window_panel(crop, marker, spec["gate"], size,
                                        to_log=spec.get("to_log", True),
                                        half_width=spec.get("half_width"))
    if kind.startswith("ref"):
        name = kind.split(":", 1)[1] if ":" in kind else (spec.get("references") or [None])[0]
        if not name:
            return np.zeros((size, size, 3), dtype=np.uint8)
        return cropmod.blend(crop, [{"name": name, "window": windows[name]["window"],
                                     "color": WHITE}], size)
    raise ValueError(f"unknown panel {kind!r}")


def render_collage(session, data, *, layout, rows, marker, gate=None, high=None,
                   references=(), a=None, b=None, tile_px=TILE_PX, crop_px=None, crop_um=None,
                   fmt="webp", title=None, store=True, to_log=True, half_width=None,
                   extra_manifest=None):
    """{png, image, format, manifest, artifact} for a collage.

    `rows` is `[{label, cells: [{cell_id, x, y, value?, call?}]}]`; a row's
    cells beyond the layout's `per_row` are dropped (and counted).
    """
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts
    from plexora.agent.evidence import calibration
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.presets import nuclear_channel
    from plexora.server.utils import fast_png

    if layout not in LAYOUTS:
        raise AgentError("invalid_input", f"layout is one of {sorted(LAYOUTS)}")
    spec_layout = LAYOUTS[layout]
    record = data.project
    channel_names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
    if marker is not None and marker not in channel_names:
        raise AgentError("precondition_missing",
                         f"{marker!r} has no image channel of that name, so there is nothing "
                         "to look at", detail={"channels": channel_names})
    nuclear = nuclear_channel(channel_names)
    panels = list(spec_layout["panels"])
    references = [r for r in references if r in channel_names][:2]
    if "ref" in panels:
        panels = [p if p != "ref" else (f"ref:{references[0]}" if references else "gate_relative")
                  for p in panels]
    row_markers = [row["marker"] for row in rows if row.get("marker")]
    for name in row_markers:
        if name not in channel_names:
            raise AgentError("precondition_missing", f"{name!r} has no image channel")
    needed = list(dict.fromkeys(n for n in (nuclear, marker, a, b, *references, *row_markers)
                                if n))
    windows = resolve_windows(session, record, dict.fromkeys(needed))
    per_row = spec_layout["per_row"]
    grid_cols, grid_rows = spec_layout["grid"]
    gap = GAP
    caption_h = CAPTION_H
    row_label_px = spec_layout.get("row_label_px", 0)
    # The budget is the layout's width: panels shrink to fit it, never the
    # other way round.
    tile_px = tile_side(layout, tile_px)
    cell_w, cell_h = grid_cols * tile_px, grid_rows * tile_px
    header_h = HEADER_H
    row_header_h = 0 if row_label_px else ROW_HEADER_H
    all_cells = [cell for row in rows for cell in row["cells"][:per_row]]
    if not all_cells:
        raise AgentError("invalid_input", "the collage has no cells to draw")
    side_px, crop_how = (float(crop_px), "explicit_px") if crop_px else \
        cropmod.crop_side_px(record, crop_um=crop_um)
    # One read per distinct channel set: a strip of eight markers reads each
    # row's own marker, not all eight for every cell.
    crops, info = {}, {"reads": 0, "level": 0}
    by_channels = {}
    for row in rows:
        row_marker = row.get("marker") or marker
        names = tuple(n for n in (nuclear, row_marker, a, b, *references) if n)
        by_channels.setdefault(names, []).extend(row["cells"][:per_row])
    for names, cells_here in by_channels.items():
        got, got_info = cropmod.read_cell_crops(
            session, record, cells_here, channels={n: windows[n]["key"] for n in names},
            crop_px=side_px, tile_px=tile_px)
        for cid, crop in got.items():
            if cid in crops:
                crops[cid].planes.update(crop.planes)
            else:
                crops[cid] = crop
        info["reads"] += got_info["reads"]
        info["level"] = got_info["level"]
        info["side_level_px"] = got_info.get("side_level_px")
    width = row_label_px + per_row * cell_w + (per_row - 1) * gap
    empty_row_h = EMPTY_ROW_H
    height = header_h + sum((row_header_h + cell_h + caption_h + gap) if row["cells"]
                            else empty_row_h for row in rows)
    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas)
    font, small = _font(11), _font(10)
    draw.rectangle((0, 0, width, header_h - 1), fill=HEADER_BG)
    draw.text((4, 1), title or f"{marker} · {layout}", fill=TEXT, font=font)
    # The gate-relative panel works on pixels; a gate on a log1p'd table is
    # in log units and must come back to intensities first, or [gate/4,
    # gate*4] is a window of single digits and every cell is white.
    table_log = bool(getattr(getattr(data, "table", None), "log_transformed", False))

    def pixel_gate(value):
        if value is None or not table_log:
            return value
        return float(math.expm1(float(value)))

    spec = {"tile_px": tile_px, "windows": windows, "nuclear": nuclear, "marker": marker,
            "gate": pixel_gate(gate), "references": references, "a": a, "b": b,
            "to_log": to_log or table_log,
            "half_width": half_width}
    manifest_rows = []
    y = header_h
    dropped = 0
    for row in rows:
        cells = row["cells"][:per_row]
        dropped += max(0, len(row["cells"]) - per_row)
        if not cells:
            draw.text((3, y), f"{row['label']}: no cells here", fill=DIM, font=small)
            manifest_rows.append({"label": row["label"], "cells": [],
                                  **({"key": row["key"]} if row.get("key") else {})})
            y += empty_row_h
            continue
        if row_label_px:
            draw.text((3, y + cell_h // 2 - 6), str(row["label"])[:10], fill=TEXT, font=small)
        else:
            draw.text((3, y), f"{row['label']}", fill=DIM, font=small)
            y += row_header_h
        placed = []
        row_spec = spec if not row.get("marker") else {
            **spec, "marker": row["marker"],
            "gate": pixel_gate(row["gate"]) if "gate" in row else spec["gate"]}
        for col, cell in enumerate(cells):
            crop = crops[int(cell["cell_id"])]
            x = row_label_px + col * (cell_w + gap)
            for index, kind in enumerate(panels):
                panel = _panel(crop, kind, row_spec)
                px = x + (index % grid_cols) * tile_px
                py = y + (index // grid_cols) * tile_px
                canvas.paste(Image.fromarray(panel, "RGB"), (px, py))
            caption = cell.get("caption")
            if caption is None:
                caption = compact_number(cell.get("value"))
                if cell.get("call") in ("positive", "negative"):
                    caption += "+" if cell["call"] == "positive" else "-"
            draw.text((x + 2, y + cell_h), str(caption)[:8],
                      fill=POSITIVE_TEXT if str(caption).endswith("+") else TEXT, font=small)
            placed.append({"cell_id": int(cell["cell_id"]), "col": col,
                           "x": float(cell["x"]), "y": float(cell["y"]),
                           "value": cell.get("value"), "call": cell.get("call"),
                           **({"why": cell["why"]} if cell.get("why") else {}),
                           **(crop.mask_stats or {})})
        manifest_rows.append({"label": row["label"], "cells": placed,
                              **({"key": row["key"]} if row.get("key") else {}),
                              **({"marker": row["marker"]} if row.get("marker") else {})})
        y += cell_h + caption_h + gap
    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    transported, transport_fmt = encode(canvas, fmt)
    manifest = {
        "kind": MANIFEST_KIND, "project": record.name, "marker": marker, "layout": layout,
        "gate": None if gate is None else {"low": float(gate),
                                           "high": None if high is None else float(high)},
        "panels": panels, "references": references, "tile_px": int(tile_px),
        "crop": {"side_px": side_px, "how": crop_how, "level": info["level"],
                 "side_level_px": info.get("side_level_px")},
        "channels": [{"name": n, "key": windows[n]["key"], "window": windows[n]["window"],
                      "window_source": windows[n]["source"]} for n in needed],
        "display": {"nuclear": nuclear, "marker_color": calibration.MARKER_COLOR,
                    "nuclear_color": calibration.NUCLEAR_MUTED_BLUE,
                    "outline_color": cropmod.OUTLINE_COLOR, "cell_mode": "outlines",
                    "gate_relative": "log scale, gate at mid-grey, gate/4 to gate*4"
                    if to_log else "linear, gate at mid-grey"},
        "rows": manifest_rows, "dropped_cells": dropped,
        "reads": info["reads"], "size": [width, height],
        "estimated_vision_tokens": estimated_tokens(width, height),
        "egress": "rendered_pixels", "renderer": "plexora.agent.evidence.collage/1",
        **(extra_manifest or {}),
    }
    artifact = artifacts.put(record.name, png, manifest, kind="gating_collage") if store \
        else None
    return {"png": png, "image": transported, "format": transport_fmt,
            "manifest": manifest, "artifact": artifact}


def render_overview(session, data, *, marker, points, low, high=None, size=512, fmt="webp",
                    show_marker=True, title=None, store=True):
    """The whole image at `size`: nuclear in muted blue, optionally the marker
    in yellow, and `points` -- (xs, ys) of the cells to mark, full-resolution
    pixels, the caller's positives -- as magenta dots."""
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts
    from plexora.agent.evidence import calibration
    from plexora.agent.presets import nuclear_channel
    from plexora.server.utils import fast_png, source_image

    record = data.project
    channel_names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
    nuclear = nuclear_channel(channel_names)
    names = [n for n in (nuclear, marker if show_marker and marker in channel_names else None)
             if n]
    windows = resolve_windows(session, record, dict.fromkeys(names)) if names else {}
    width, height = record.image.width or 1, record.image.height or 1
    scale = size / max(width, height)
    out_w, out_h = max(16, int(round(width * scale))), max(16, int(round(height * scale)))
    image_data = session.image_data(record.name)
    with source_image.SHELF.reader(image_data) as source:
        level = source_image.choose_level(source, width, out_w)
        div = 2 ** level
        box = (0, 0, int(math.ceil(width / div)), int(math.ceil(height / div)))
        layers = []
        if nuclear in windows:
            layers.append({"key": windows[nuclear]["key"], "window": windows[nuclear]["window"],
                           "visible": True, "color": _rgb(calibration.NUCLEAR_MUTED_BLUE)})
        if marker in windows:
            layers.append({"key": windows[marker]["key"], "window": windows[marker]["window"],
                           "visible": True, "color": _rgb(calibration.MARKER_COLOR)})
        pixels, _rendered, _bg = source_image.composite(source, layers, level, box)
    image = Image.fromarray(np.ascontiguousarray(pixels), "RGB").resize((out_w, out_h),
                                                                        Image.LANCZOS)
    xs = np.asarray(points[0], dtype=np.float64) * scale
    ys = np.asarray(points[1], dtype=np.float64) * scale
    n_marked = int(xs.shape[0])
    draw = ImageDraw.Draw(image)
    dot = max(1, int(round(size / 400)))
    for px, py in zip(xs.tolist(), ys.tolist()):
        draw.rectangle((px - dot, py - dot, px + dot, py + dot), fill=(255, 61, 242))
    canvas = Image.new("RGB", (out_w, out_h + OVERVIEW_TITLE_H), BG)
    canvas.paste(image, (0, OVERVIEW_TITLE_H))
    ImageDraw.Draw(canvas).text((4, 1), title or f"{marker}: {n_marked} positive",
                                fill=TEXT, font=_font(11))
    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    transported, transport_fmt = encode(canvas, fmt)
    manifest = {"kind": "plexora.gating_overview", "project": record.name, "marker": marker,
                "gate": {"low": float(low), "high": None if high is None else float(high)},
                "level": int(level), "size": [out_w, out_h + OVERVIEW_TITLE_H],
                "n_positive": n_marked,
                "channels": [{"name": n, "window": windows[n]["window"]} for n in windows],
                "estimated_vision_tokens": estimated_tokens(out_w, out_h + OVERVIEW_TITLE_H),
                "egress": "rendered_pixels"}
    artifact = artifacts.put(record.name, png, manifest, kind="gating_overview") if store \
        else None
    return {"png": png, "image": transported, "format": transport_fmt, "manifest": manifest,
            "artifact": artifact}


def _rgb(hex_colour):
    value = hex_colour.lstrip("#")
    return {"r": int(value[0:2], 16), "g": int(value[2:4], 16), "b": int(value[4:6], 16)}
