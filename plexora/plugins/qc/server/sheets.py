"""What an agent is shown: the QC evidence sheets.

- **Channel audit**: every channel, whole tissue, eight to a sheet, at the
  project's calibrated windows, with the scan's candidates outlined and
  numbered -- about a hundred vision tokens a channel, so every channel is
  looked at even when no detector fired.
- **Confirm**: one candidate at three scales (the whole channel with the
  outline, the neighbourhood, a close crop) plus the detector's own map or,
  at deeper levels, the nuclear stain and a matched clean field. First looks
  at several candidates share one sheet, a row each (`confirm_batch_sheet`).
- **Scope**: the same place across channels.
- **Localize**: the candidate outlines to choose from; **grid**: labelled
  squares when none fits.
- **Review**: every QC region on the whole tissue, coloured by class.
- **Score review**: one image check (blur, registration, segmentation) on
  one channel, a row of places from each part of its score distribution --
  clearly fine, just below and just above the bar, far above it, the heart
  of the largest flagged regions -- and a last row of the whole tissue with
  the regions at the bar and the score map (`score_sheet`).

Every sheet is a pure function of the scan, the calibration and the candidate,
stored as a PNG artifact (its manifest says how to draw it again) and sent as
WebP. A panel is rendered once (`_render` keeps them by image, region,
channels and windows), however many sheets show it.
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
RENDERER = "plexora.qc.sheets/2"

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


#: (name, calibrated window) -> the channel's overview window, so `_draw` can
#: give each panel the window for its scale (`calibration.window_at`) without
#: every sheet passing the record down. Pure data, bounded.
_OVERVIEW: dict = {}
_OVERVIEW_LIMIT = 1024


def _channel(name, color, calibration):
    from plexora.agent.evidence import calibration as display
    from plexora.agent.render_spec import ChannelSpec

    window = _window(calibration, name)
    if isinstance(window, list):
        overview = display.overview_window(calibration, name)
        if overview is not None and overview != window:
            if len(_OVERVIEW) >= _OVERVIEW_LIMIT:
                _OVERVIEW.pop(next(iter(_OVERVIEW)))
            _OVERVIEW[(name, tuple(window))] = list(overview)
    return ChannelSpec(name=name, color=color, window=window)


def _at_scale(channels, bounds, size):
    """`channels` with each calibrated window moved to the panel's scale: a
    whole-tissue tile draws coarse pixels and takes the overview window, a
    close crop keeps the cell-anchored one (`calibration.SCALE_BLEND`)."""
    from plexora.agent.evidence import calibration as display

    px_per_px = float(bounds["width"]) / max(int(size), 1)
    out = []
    for channel in channels:
        window = channel.window
        overview = _OVERVIEW.get((channel.name, tuple(window))) \
            if isinstance(window, (list, tuple)) else None
        if overview is not None:
            channel = channel.model_copy(update={
                "window": display.blend_windows(list(window), overview, px_per_px)})
        out.append(channel)
    return out


#: Rendered panels kept for reuse, newest last (see `_render`).
_PANELS: "OrderedDict" = None
PANEL_CACHE_SIZE = 64
PANEL_STATS = {"hits": 0, "misses": 0}


def _panel_key(scan, project, bounds, channels, size, scale_bar, pixel, segmentation="none"):
    """What a panel is a pure function of: the image (the scan's fingerprint
    names its pixels), the region, each channel's colour and window, the
    output size, the scale bar and the pixel size. Shapes are drawn on a copy
    afterwards, so the same tile outlined differently is still one render."""
    if scan is None or not getattr(scan, "meta", None) or not scan.meta.get("fingerprint"):
        return None
    windows = tuple((c.name, c.color, tuple(c.window) if isinstance(c.window, (list, tuple))
                     else c.window) for c in channels)
    box = tuple(round(float(bounds[k]), 3) for k in ("x", "y", "width", "height"))
    return (project, scan.meta["fingerprint"], box, windows, int(size), bool(scale_bar),
            (pixel or {}).get("value"), segmentation)


def clear_panel_cache():
    global _PANELS
    _PANELS = None
    PANEL_STATS.update(hits=0, misses=0)


def _render(session, project, bounds, channels, size, *, pixel, shapes=None,
            scale_bar=True, scan=None):
    """(picture, manifest) of one panel (the report's entry point; the
    sheets call `_draw` with their scan, which is what makes a panel
    cacheable)."""
    return _draw(session, project, scan, bounds, channels, size, pixel=pixel, shapes=shapes,
                 scale_bar=scale_bar)


def _draw(session, project, scan, bounds, channels, size, *, pixel, shapes=None,
          scale_bar=True, segmentation="none"):
    """(picture, manifest) of one panel. The same panel is drawn once: the
    whole-tissue view of a channel is on its audit tile, again on each
    first-look row of its candidates, and a deeper look or a re-render repeats
    the neighbourhood and the crop -- so renders are kept (bounded, newest
    last) by `_panel_key`, and each caller gets its own copy to draw on."""
    import copy
    from collections import OrderedDict

    from plexora.agent.render_spec import Bounds, OutputSpec, RenderInput

    global _PANELS
    channels = _at_scale(channels, bounds, size)
    key = _panel_key(scan, project, bounds, channels, size, scale_bar, pixel, segmentation)
    if _PANELS is None:
        _PANELS = OrderedDict()
    held = _PANELS.get(key) if key is not None else None
    if held is not None:
        _PANELS.move_to_end(key)
        PANEL_STATS["hits"] += 1
        picture, manifest = held[0].copy(), copy.deepcopy(held[1])
    else:
        spec = RenderInput(project=project, bounds=Bounds(**bounds), channels=channels,
                           segmentation=segmentation,
                           output=OutputSpec(width=size, height=size),
                           scale_bar=scale_bar, layers="none")
        picture, manifest = layout.render_panel(session, spec, pixel=pixel)
        PANEL_STATS["misses"] += 1
        if key is not None:
            _PANELS[key] = (picture.copy(), copy.deepcopy(manifest))
            while len(_PANELS) > PANEL_CACHE_SIZE:
                _PANELS.popitem(last=False)
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
        picture, manifest = _draw(session, project, scan, bounds,
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
        picture, manifest = _draw(session, project, scan, bounds,
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
        # A map finer than the panel (a big image) makes a cell under a pixel.
        draw.rectangle((x * sx, y * sy, max(x * sx, (x + 1) * sx - 1),
                        max(y * sy, (y + 1) * sy - 1)), outline=(255, 61, 242))
    return picture


def confirm_sheet(session, project, scan, candidate, mask, *, level, fmt, pixel, calibration,
                  store=True):
    """The confirm sheet of one candidate at look `level` (0, 1, 2)."""
    grid = scan.grid
    size = grid["image_size"]
    channel = _found_on(candidate, scan)
    nuclear = scan.nuclear()
    seg = _mask_of(candidate)
    geometry = (candidate.get("variants") or {}).get("standard", {}).get("geometry") \
        or candidate.get("geometry")
    box = candidate["bbox"]
    peak = _peak(scan, candidate, mask, channel)
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
        panels.append((_draw(session, project, scan, whole, [ch], PANEL_PX, pixel=pixel,
                             shapes=outline + dashed_box, scale_bar=False),
                       f"{channel} | whole tissue | outline"))
        panels.append((_draw(session, project, scan, meso, [*ref, marker], PANEL_PX, pixel=pixel,
                             shapes=outline, segmentation=seg), f"{channel} | neighbourhood"))
        panels.append(_close_crop(session, project, scan, candidate, mask, channel,
                                  crop["width"], PANEL_PX, pixel=pixel,
                                  calibration=calibration, segmentation=seg))
        heat = _map_panel(scan, candidate, mask, PANEL_PX)
        panels.append(((heat, None), f"map: {candidate.get('primary_metric') or 'region'}"))
    else:
        clean = _clean_field(scan, mask, channel, 3)
        panels.append((_draw(session, project, scan, meso, [*ref, marker], PANEL_PX, pixel=pixel,
                             shapes=outline, segmentation=seg), f"{channel} | neighbourhood"))
        drawn, caption = _close_crop(session, project, scan, candidate, mask, channel,
                                     crop["width"], PANEL_PX, pixel=pixel,
                                     calibration=calibration, segmentation=seg)
        panels.append((drawn, caption.replace("close crop",
                                              "closer crop" if level >= 2 else "close crop")))
        # The crop may have moved off the peak (`_close_crop`): "same crop"
        # is where it went, and the clean field is drawn at its window.
        at = (drawn[1] or {}).get("bounds_fullres") or crop
        if nuclear and nuclear != channel:
            other = nuclear
        else:
            other = next((c["name"] for c in scan.channels if c["name"] != channel), None)
        if other:
            spec = _channel(other, CHANNEL_COLOR, calibration)
            window = _drawn_windows(drawn[1]).get(channel)
            if other == nuclear and cycle_nuclear(scan, channel) and window:
                # Cycle 1 beside a later cycle's nuclear stain: matched, or
                # the reference's dim-layer window saturates it at this scale.
                from plexora.agent.render_spec import ChannelSpec

                spec = matched_reference(calibration, nuclear, ChannelSpec(
                    name=channel, color=CHANNEL_COLOR, window=window), CHANNEL_COLOR)
            seen, stretched = _draw_seen(session, project, scan, at, [spec], PANEL_PX,
                                         pixel=pixel)
            panels.append((seen, f"{other} | same crop" + (STRETCHED if stretched else "")))
        if clean is not None:
            clean_box = square_around(clean[0], clean[1], at["width"], size)
            window = _drawn_windows(drawn[1]).get(channel)
            same = ch.model_copy(update={"window": window}) if window else ch
            panels.append((_draw(session, project, scan, clean_box, [same], PANEL_PX,
                                 pixel=pixel),
                           f"{channel} | clean field, same size and window"))
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


BATCH_PX = 288


def _found_on(candidate, scan):
    """The channel a candidate is judged on: the one it was found on, else
    its first channel, else the nuclear stain."""
    return candidate.get("channel") or (candidate.get("channels") or [None])[0] \
        or scan.nuclear()


def _mask_of(candidate):
    """Segmentation findings are about the mask: draw its outlines."""
    return "outlines" if candidate.get("class_hint") == "segmentation_error" else "none"


def _peak(scan, candidate, mask, channel):
    """Where the close crop goes: the detector's peak, else the brightest
    tissue cell of the candidate's own cells (a region named on a grid or
    raised by the audit has no peak, and its box centre can be empty slide)."""
    if candidate.get("peak"):
        return candidate["peak"]
    box = candidate["bbox"]
    centre = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
    if mask is None or not np.asarray(mask).any():
        return centre
    mask = np.asarray(mask, dtype=bool)
    tissue = scan.tissue()
    inside = mask & (tissue > 0.5) if tissue is not None and tissue.shape == mask.shape \
        else mask
    if not inside.any():
        inside = mask
    values = scan.map(channel, "median") if channel else None
    if values is None or values.shape != mask.shape:
        ys, xs = np.nonzero(inside)
        pick = int(np.argmin((ys - ys.mean()) ** 2 + (xs - xs.mean()) ** 2))
        iy, ix = ys[pick], xs[pick]
    else:
        score = np.where(inside, np.nan_to_num(values, nan=-np.inf), -np.inf)
        iy, ix = np.unravel_index(int(np.argmax(score)), score.shape)
    s = scan.grid["cell_full_px"]
    return [(ix + 0.5) * s, (iy + 0.5) * s]


#: [cal] A crop is "visible" when at least this share of its pixels is
#: brighter than `VISIBLE_LEVEL` (0..255, any colour): a black crop shows the
#: agent nothing and only sends a region to manual review.
VISIBLE_SHARE = 0.01
VISIBLE_LEVEL = 40


def visible(picture) -> bool:
    """Whether a rendered panel shows anything (see `VISIBLE_SHARE`)."""
    data = np.asarray(picture)
    if data.ndim == 3:
        data = data[..., :3].max(axis=-1)
    if not data.size:
        return False
    return float((data >= VISIBLE_LEVEL).mean()) >= VISIBLE_SHARE


def _crop_channels(candidate, scan, channel, calibration):
    """The close crop's channels: a registration region is the two cycles
    red / green (as the score review's registration tiles), so an offset
    shows as crescents and a one-cycle place as one colour; any other region
    its channel in white."""
    if candidate.get("origin") == "check" and candidate.get("detector") == "registration":
        reference = candidate.get("reference") or scan.nuclear()
        comparison = candidate.get("channel") or channel
        if reference and comparison and reference != comparison:
            return score_channels("registration", calibration, channel=comparison,
                                  reference=reference), f"{reference} red | {comparison} green"
    return [_channel(channel, CHANNEL_COLOR, calibration)], channel


#: [cal] A crop is stretched only when the brightest map cell under it is at
#: least this many times the channel's floor -- the larger of the slide's
#: background (the median p99 of the off-tissue cells, zero where the image
#: is padded) and the dim end of its tissue (the `STRETCH_TISSUE_FLOOR`
#: percentile of the tissue cells' medians): an empty crop is shown empty,
#: never as amplified noise.
STRETCH_OVER_BACKGROUND = 1.25
STRETCH_TISSUE_FLOOR = 10.0


def stretch_window(scan, name, box):
    """[low, high] for channel `name` from the scan's own map cells under
    `box` -- their lowest p10 to their highest p99 -- for a crop its
    calibrated window draws black (a diffuse background, a sparse stain,
    stroma with few nuclei: the calibrated window is anchored on bright cells
    and puts them all under 1 %). None when nothing under the box rises above
    the slide's background (`STRETCH_OVER_BACKGROUND`) or the maps are
    missing."""
    import math

    p10, p99 = scan.map(name, "p10"), scan.map(name, "p99")
    if p10 is None or p99 is None:
        return None
    s = float(scan.grid["cell_full_px"])
    ny, nx = p99.shape
    x0, y0 = max(0, int(box["x"] // s)), max(0, int(box["y"] // s))
    x1 = min(nx, max(x0 + 1, int(math.ceil((box["x"] + box["width"]) / s))))
    y1 = min(ny, max(y0 + 1, int(math.ceil((box["y"] + box["height"]) / s))))
    top, bottom = p99[y0:y1, x0:x1], p10[y0:y1, x0:x1]
    if not np.isfinite(top).any() or not np.isfinite(bottom).any():
        return None
    tissue = scan.tissue() > 0.5
    floor = 0.0
    if tissue.shape == p99.shape:
        off_values = p99[~tissue & np.isfinite(p99)]
        if off_values.size:
            floor = float(np.median(off_values))
        median = scan.map(name, "median")
        on_values = median[tissue & np.isfinite(median)] if median is not None else []
        if len(on_values):
            floor = max(floor, float(np.percentile(on_values, STRETCH_TISSUE_FLOOR)))
    low, high = float(np.nanmin(bottom)), float(np.nanmax(top))
    if not high > max(floor * STRETCH_OVER_BACKGROUND, low + 1.0):
        return None
    return [low, high]


#: [cal] A score-review tile is redrawn stretched (`_draw_seen`'s
#: `stretch_dim`) when its 99th-percentile level (0..255, the brightest
#: colour of each pixel) is under this: dim enough that an agent cannot
#: judge it, though not black.
DIM_P99_LEVEL = 80
#: Rows at a tile's foot left out of its level: the scale bar is bright.
SCALE_BAR_ROWS = 40


def _level_p99(picture, scale_bar=False) -> float:
    """The 99th percentile of a panel's brightest colour per pixel."""
    data = np.asarray(picture)
    if data.ndim == 3:
        data = data[..., :3].max(axis=-1)
    if scale_bar and data.shape[0] > 2 * SCALE_BAR_ROWS:
        data = data[:-SCALE_BAR_ROWS]
    return float(np.percentile(data, 99)) if data.size else 0.0


def _draw_seen(session, project, scan, box, channels, size_px, *, pixel, segmentation="none",
               shapes=None, scale_bar=True, stretch_dim=False):
    """(drawn, stretched): a panel at the calibrated windows, or -- when that
    shows nothing (`visible`), or with `stretch_dim` when it is dim (its
    p99 level under `DIM_P99_LEVEL`) -- again with each channel stretched to
    what the scan measured under the box (`stretch_window`); `stretched`
    says which, for the caption. A box with nothing above the slide's
    background stays as drawn: black, and truly empty. `shapes` go on after
    the judging, so an outline never makes a black panel look seen."""
    drawn = _draw(session, project, scan, box, channels, size_px, pixel=pixel,
                  segmentation=segmentation, scale_bar=scale_bar)

    def marked(panel):
        if shapes:
            layout.shapes_onto(panel[0], panel[1], shapes)
        return panel

    seen = visible(drawn[0])
    dim = stretch_dim and seen and _level_p99(drawn[0], scale_bar) < DIM_P99_LEVEL
    if (seen and not dim) or scan is None:
        return marked(drawn), False
    windows = [stretch_window(scan, c.name, box) for c in channels]
    if not any(windows):
        return marked(drawn), False
    stretched = [c.model_copy(update={"window": w}) if w else c
                 for c, w in zip(channels, windows)]
    again = _draw(session, project, scan, box, stretched, size_px, pixel=pixel,
                  segmentation=segmentation, scale_bar=scale_bar)
    better = visible(again[0]) and (not seen or _level_p99(again[0], scale_bar)
                                    > _level_p99(drawn[0], scale_bar))
    return (marked(again), True) if better else (marked(drawn), False)


#: How a caption says its panel was drawn at a stretched window.
STRETCHED = " (contrast stretched)"


def _drawn_windows(manifest):
    """{channel: [low, high]} a rendered panel was drawn with."""
    return {c["name"]: list(c["window"]) for c in (manifest or {}).get("channels") or []
            if isinstance(c.get("window"), (list, tuple))}


def _close_crop(session, project, scan, candidate, mask, channel, side, size_px, *, pixel,
                calibration, segmentation="none"):
    """((picture, manifest), caption) of the close crop: at the candidate's
    peak (a check region's strongest place that holds nuclei), and -- when
    that shows nothing -- at the strongest tissue place of its cells, and
    then stretched to what is there (`_draw_seen`), so a crop is never black
    when anything of the region can be seen. The manifest's bounds are where
    the crop went (`same crop` panels follow it)."""
    size = scan.grid["image_size"]
    channels, words = _crop_channels(candidate, scan, channel, calibration)
    peak = _peak(scan, candidate, mask, channel)
    crop = square_around(peak[0], peak[1], side, size)
    drawn = _draw(session, project, scan, crop, channels, size_px, pixel=pixel,
                  segmentation=segmentation)
    where, at = "at the peak", crop
    if not visible(drawn[0]):
        fallback = _peak(scan, {**candidate, "peak": None}, mask, channel)
        if fallback and (abs(fallback[0] - peak[0]) > 1 or abs(fallback[1] - peak[1]) > 1):
            box = square_around(fallback[0], fallback[1], side, size)
            again = _draw(session, project, scan, box, channels, size_px, pixel=pixel,
                          segmentation=segmentation)
            if visible(again[0]):
                drawn, where, at = again, "at its strongest tissue", box
    if not visible(drawn[0]):
        drawn, stretched = _draw_seen(session, project, scan, at, channels, size_px,
                                      pixel=pixel, segmentation=segmentation)
        if stretched:
            where += STRETCHED
    return drawn, f"{words} | close crop {where}"


def _region_map(scan, candidate, mask, size):
    """A check region's map panel: its cells over the tissue, cropped to the
    region's neighbourhood -- on a big image a 6.5 um region is under a
    pixel of the whole-image map, which then reads black."""
    tissue = scan.tissue()
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return _heat_panel(scan, "", mask, size)
    ys, xs = np.nonzero(mask)
    h = max(int(ys.max() - ys.min() + 1), int(xs.max() - xs.min() + 1))
    pad = max(3, h)
    y0, y1 = max(0, int(ys.min()) - pad), min(mask.shape[0], int(ys.max()) + pad + 1)
    x0, x1 = max(0, int(xs.min()) - pad), min(mask.shape[1], int(xs.max()) + pad + 1)
    values = np.where(mask, 1.0, 0.25 * (np.asarray(tissue, dtype=np.float64) > 0.5))
    return layout.heatmap(values[y0:y1, x0:x1], size=(size, size), clip=(0.0, 1.0))


def _map_panel(scan, candidate, mask, size):
    if candidate.get("origin") == "check" and not candidate.get("primary_metric"):
        return _region_map(scan, candidate, mask, size)
    return _heat_panel(scan, candidate.get("primary_metric") or "", mask, size)


def confirm_batch_sheet(session, project, scan, candidates, masks, *, fmt, pixel,
                        calibration, store=True):
    """First looks at several candidates on one sheet: a row each -- the
    channel's whole tissue with the outline (the audit tile's own render,
    reused), the neighbourhood (nuclear blue, channel yellow), a close crop
    at the peak, and the detector's map -- labelled with the candidate's
    label."""
    size = scan.grid["image_size"]
    nuclear = scan.nuclear()
    whole = tissue_box(scan)
    sheet = layout.Sheet(4, len(candidates), BATCH_PX,
                         title=f"{project} - {len(candidates)} suspected artifacts, one row "
                               "each (whole tissue | neighbourhood | close crop | map)")
    rows = []
    for index, (candidate, mask) in enumerate(zip(candidates, masks)):
        channel = _found_on(candidate, scan)
        seg = _mask_of(candidate)
        label = candidate.get("label", "")
        words = schemas.CLASS_WORDS.get(candidate.get("class_hint"),
                                        candidate.get("class_hint"))
        geometry = (candidate.get("variants") or {}).get("standard", {}).get("geometry") \
            or candidate.get("geometry")
        box = candidate["bbox"]
        peak = _peak(scan, candidate, mask, channel)
        outline = [_shape("candidate", geometry, color=OUTLINE, width=2)] if geometry else []
        dashed = [_shape("bbox", bounds={"x": box[0], "y": box[1], "width": box[2] - box[0],
                                         "height": box[3] - box[1]}, color=OUTLINE, width=1,
                         dash=True)]
        meso = padded(box, MESO_FACTOR, meso_side(scan, pixel), size)
        crop = square_around(peak[0], peak[1], crop_side(scan, pixel), size)
        ch = _channel(channel, CHANNEL_COLOR, calibration)
        marker = _channel(channel, MARKER_COLOR, calibration)
        ref = [_channel(nuclear, NUCLEAR_COLOR, calibration)] \
            if nuclear and nuclear != channel else []
        panels = [
            (_draw(session, project, scan, whole, [ch], TILE_PX, pixel=pixel,
                   shapes=outline + dashed, scale_bar=False),
             f"{label} | {channel} | {words}"),
            (_draw(session, project, scan, meso, [*ref, marker], BATCH_PX, pixel=pixel,
                   shapes=outline, segmentation=seg), f"{label} | neighbourhood"),
            _labelled(label, _close_crop(session, project, scan, candidate, mask, channel,
                                         crop["width"], BATCH_PX, pixel=pixel,
                                         calibration=calibration, segmentation=seg)),
            ((_map_panel(scan, candidate, mask, BATCH_PX), None),
             f"{label} | map: {(candidate.get('primary_metric') or '').partition('::')[2]}"
             if candidate.get("primary_metric") else f"{label} | map: region"),
        ]
        placed = []
        for column, ((picture, manifest), caption) in enumerate(panels):
            sheet.place(index * 4 + column, picture, caption)
            placed.append({"slot": index * 4 + column, "caption": caption,
                           **(_brief(manifest) if manifest else {"kind": "map"})})
        rows.append({"row": index, "label": label, "candidate": candidate["id"],
                     "channel": channel, "panels": placed})
    return _finish(sheet, project, fmt, {"project": project, "rows": rows,
                                          "candidates": [c["id"] for c in candidates]},
                   "plexora.qc_confirm_batch", store=store)


def _labelled(label, panel):
    drawn, caption = panel
    return drawn, f"{label} | {caption}" if label else caption


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
        picture, manifest = _draw(session, project, scan, meso,
                                  [_channel(name, CHANNEL_COLOR, calibration)], TILE_PX,
                                  pixel=pixel, shapes=outline, scale_bar=slot == 0)
        cycle = scan.channel(name).get("cycle")
        sheet.place(slot, picture, f"{name}" + (f" | c{cycle}" if cycle else ""))
        tiles.append({"slot": slot, "name": name, **_brief(manifest)})
    return _finish(sheet, project, fmt, {"project": project, "candidate": candidate["id"],
                                          "tiles": tiles}, "plexora.qc_scope", store=store)


def localize_sheet(session, project, scan, candidate, variants, *, fmt, pixel, calibration,
                   traces=None, store=True):
    """The candidate outlines A..E, one per panel, and all of them together.

    Each outline is an envelope (thin, dashed); with `traces` ({variant:
    {geometry, area_um2, kept_fraction}}) the artifact traced inside it is
    drawn solid and filled in the same colour -- what would be written."""
    size = scan.grid["image_size"]
    channel = candidate["channels"][0] if candidate.get("channels") else scan.nuclear()
    box = candidate["bbox"]
    meso = padded(box, 2.0, meso_side(scan, pixel) / 2, size)
    ch = _channel(channel, CHANNEL_COLOR, calibration)
    words = schemas.CLASS_WORDS.get(candidate.get("class_hint"), "")
    title = (f"{project} - which envelope should the {words} in {channel} be traced in?"
             if traces else f"{project} - which outline covers the {words} in {channel}?")
    sheet = layout.Sheet(3, 2, 320, title=title)
    traces = traces or {}
    panels = []
    names = [n for n in ("tight", "standard", "generous", "hull", "bbox") if n in variants]
    for slot, name in enumerate(names):
        letter = VARIANT_LETTERS[name]
        trace = traces.get(name) or {}
        shapes = [_shape(letter, variants[name]["geometry"], color=VARIANT_COLORS[name],
                         width=1 if trace.get("geometry") else 2,
                         dash=bool(trace.get("geometry")), label=letter)]
        if trace.get("geometry"):
            shapes.append(_shape(f"{letter}-trace", trace["geometry"],
                                 color=VARIANT_COLORS[name], width=2, fill_alpha=0.25))
        picture, manifest = _draw(session, project, scan, meso, [ch], 320, pixel=pixel,
                                  shapes=shapes, scale_bar=slot == 0)
        sheet.place(slot, picture, _localize_caption(letter, name, variants[name], trace))
        panels.append({"slot": slot, "id": letter, "variant": name, **_brief(manifest)})
    together = [_shape(VARIANT_LETTERS[n], variants[n]["geometry"], color=VARIANT_COLORS[n],
                       width=1, label=VARIANT_LETTERS[n]) for n in names]
    widest = (traces.get("bbox") or traces.get(names[-1]) or {}) if names else {}
    if widest.get("geometry"):
        together.append(_shape("trace", widest["geometry"], color="#ffffff", width=1,
                               fill_alpha=0.15))
    picture, manifest = _draw(session, project, scan, meso, [ch], 320, pixel=pixel,
                              shapes=together, scale_bar=False)
    sheet.place(min(5, len(names)), picture, "all outlines" + (" | white: traced"
                                                               if widest.get("geometry")
                                                               else ""))
    return _finish(sheet, project, fmt, {"project": project, "candidate": candidate["id"],
                                          "panels": panels, "traced": bool(traces)},
                   "plexora.qc_localize", store=store)


def _localize_caption(letter, name, variant, trace):
    area = variant.get("area_um2")
    text = f"{letter} {name}"
    if trace.get("geometry") and area and trace.get("area_um2") is not None:
        kept = trace.get("kept_fraction")
        share = f" ({100 * kept:.0f}%)" if kept is not None else ""
        return f"{text} | env {area / 1e6:.3g} | trace {trace['area_um2'] / 1e6:.3g} mm2{share}"
    return text + (f" | {area / 1e6:.3g} mm2" if area else "")


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
    picture, manifest = _draw(session, project, scan, bounds, [ch], GRID_PX, pixel=pixel,
                              scale_bar=False)
    plain, plain_manifest = _draw(session, project, scan, bounds, [ch], GRID_PX, pixel=pixel)
    draw = ImageDraw.Draw(picture)
    for square in spec["squares"]:
        bx0, by0, bx1, by1 = square["bounds"]
        px0, py0 = layout.to_panel_px(manifest, picture, bx0, by0)
        px1, py1 = layout.to_panel_px(manifest, picture, bx1, by1)
        draw.rectangle((px0, py0, max(px0, px1 - 1), max(py0, py1 - 1)), outline=(34, 230, 230),
                       width=1)
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
        color = schemas.category_color(schemas.category_of_class(region["class"]))
        exclude = region.get("action") == "exclude"
        shapes.append(_shape(f"r{index + 1}", region["geometry"], color=color,
                             width=2, dash=not exclude, fill_alpha=0.3 if exclude else 0.0,
                             label=region.get("label", "")))
    picture, manifest = _draw(session, project, scan, bounds, channels, REVIEW_PX // 2,
                              pixel=pixel, shapes=shapes)
    plain, _m = _draw(session, project, scan, bounds, channels, REVIEW_PX // 2, pixel=pixel,
                      scale_bar=False)
    sheet = layout.Sheet(2, 1, REVIEW_PX // 2, title=f"{project} - every QC region "
                                                      "(filled: excluded, dashed: warning)")
    sheet.place(0, picture, "QC regions by category")
    sheet.place(1, plain, f"{nuclear} | the same view, no overlay")
    legend = sorted({schemas.category_of_class(r["class"]) for r in regions})
    return _finish(sheet, project, fmt, {"project": project, "regions": len(regions),
                                          "legend": {k: schemas.category_color(k)
                                                     for k in legend}, **_brief(manifest)},
                   "plexora.qc_review", store=store)


# -- score review --------------------------------------------------------------------------

SCORE_TILE_PX = 152
SCORE_COLUMNS = 6
#: How a tile's place is marked: the grid cell (or cell) the score is of.
SCORED_OUTLINE = "#22e6e6"
REGION_OUTLINE = "#ff3df2"
#: The registration pair as the panel shows it: reference red, comparison
#: green -- yellow where the two stains agree, a red or green crescent where
#: a nucleus moved.
REFERENCE_COLOR = "#ff2d2d"
COMPARISON_COLOR = "#2bd46f"
STRATUM_CAPTIONS = {"clear_good": "FINE", "borderline_below": "JUST BELOW",
                    "borderline_above": "JUST ABOVE", "strongly_abnormal": "FAR ABOVE",
                    "clustered": "IN REGIONS", "global": "WHOLE TISSUE"}


def score_channels(check, calibration, *, channel, reference=None, display=None):
    """The channels a check's tiles are drawn in: a registration pair at
    matched windows (`matched_reference`); a field of no single channel
    (`display`: the Artifact Detector's nuclear and lead channel) the first
    in the nuclear colour, the rest in the marker colour."""
    if display:
        return [_channel(name, NUCLEAR_COLOR if index == 0 else MARKER_COLOR, calibration)
                for index, name in enumerate(display)]
    if check == "registration" and reference:
        comparison = _channel(channel, COMPARISON_COLOR, calibration)
        return [matched_reference(calibration, reference, comparison, REFERENCE_COLOR),
                comparison]
    return [_channel(channel, CHANNEL_COLOR, calibration)]


#: [cal] the tissue-brightness ratio between two cycles' nuclear stains is
#: trusted only inside this band; outside it each keeps its own window.
MATCH_RATIO_BAND = (0.25, 4.0)


def _tissue_ratio(calibration, reference, comparison):
    """The comparison's median tissue pixel over the reference's (the
    calibration's `p50_tissue`), or None."""
    channels = (calibration or {}).get("channels") or {}
    try:
        ref = float(channels[reference]["stats"]["p50_tissue"])
        cmp_ = float(channels[comparison]["stats"]["p50_tissue"])
    except (KeyError, TypeError, ValueError):
        return None
    if not ref > 0 or not cmp_ > 0:
        return None
    ratio = cmp_ / ref
    low, high = MATCH_RATIO_BAND
    return ratio if low <= ratio <= high else None


def matched_reference(calibration, reference, comparison, color):
    """The reference nuclear stain drawn to match `comparison` (a
    ChannelSpec): its window is the comparison's divided by their tissue
    brightness ratio, at every scale. The two cycles' nuclear stains are
    calibrated differently -- the reference as the dim context layer (one
    overview window), a later cycle's as a marker anchored on its cells -- so
    drawn each at its own window the reference was ~2x brighter at cell
    scale, and every red / green crop read as cycle-2 loss."""
    ratio = _tissue_ratio(calibration, reference, comparison.name)
    if ratio is None or not isinstance(comparison.window, list):
        return _channel(reference, color, calibration)
    from plexora.agent.render_spec import ChannelSpec

    window = [v / ratio for v in comparison.window]
    overview = _OVERVIEW.get((comparison.name, tuple(comparison.window)))
    if overview is not None:
        if len(_OVERVIEW) >= _OVERVIEW_LIMIT:
            _OVERVIEW.pop(next(iter(_OVERVIEW)))
        _OVERVIEW[(reference, tuple(window))] = [v / ratio for v in overview]
    return ChannelSpec(name=reference, color=color, window=window)


def cycle_nuclear(scan, name) -> bool:
    """Whether `name` is a later cycle's nuclear stain (not the reference)."""
    cycles = ((getattr(scan, "meta", None) or {}).get("cycles") or {}).get("cycles") or []
    return any(c.get("nuclear") == name for c in cycles) and name != scan.nuclear()


def _score_heat(values, valid, threshold, size, marks=(), grid=None, regions_mask=None):
    """The score map: dark where nothing was measured, the flagged cells
    outlined, and each sampled place marked with its row's letter."""
    from PIL import ImageDraw

    finite = values[np.isfinite(values)]
    top = float(np.quantile(finite, 0.99)) if finite.size else 1.0
    hi = float(min(1.0, max(top, 2.0 * float(threshold), 1e-3)))
    picture = layout.heatmap(values, size=(size, size), mask=valid, clip=(0.0, hi))
    draw = ImageDraw.Draw(picture)
    ny, nx = values.shape
    sy, sx = size / max(1, ny), size / max(1, nx)
    if regions_mask is not None and regions_mask.any() and regions_mask.size <= 250_000:
        from scipy import ndimage

        edge = regions_mask & ~ndimage.binary_erosion(regions_mask)
        for y, x in zip(*np.nonzero(edge)):
            # A map cell may be under a pixel of the tile: never a reversed box.
            x0, y0 = x * sx, y * sy
            draw.rectangle((x0, y0, max(x0, (x + 1) * sx - 1), max(y0, (y + 1) * sy - 1)),
                           outline=(255, 61, 242))
    for letter, (iy, ix) in marks:
        cx, cy = (ix + 0.5) * sx, (iy + 0.5) * sy
        draw.text((cx - 3, cy - 6), letter, fill=(255, 255, 255), font=layout.font(10),
                  stroke_width=2, stroke_fill=(0, 0, 0))
    return picture


def score_sheet(session, project, scan, field, rows, *, threshold, tile_px_side, channels,
                fmt, pixel, regions=None, segmentation="none", title=None, overview=True,
                cell_marks=None, store=True):
    """One check's places across its score distribution, a row per stratum,
    and (with `overview`) a last row of the whole tissue: the regions at the
    bar outlined on the channel, and the score map with the places marked.

    `rows` is [(stratum, [{x, y, score, cell?}])]; `tile_px_side` the tile's
    side in full-resolution pixels; a place's scored grid cell (`field`'s,
    or `cell_marks` px round a cell) is outlined dashed."""
    size = field.grid["image_size"]
    n_rows = len(rows) + (1 if overview else 0)
    sheet = layout.Sheet(SCORE_COLUMNS, max(1, n_rows), SCORE_TILE_PX,
                         title=title or f"{project} - {field.score_name} across its range, "
                                        f"bar {threshold:.2f}")
    placed_rows = []
    any_stretched = False
    note_slot = None
    step = float(field.grid["step"])
    for r, (stratum, places) in enumerate(rows):
        tiles = []
        for c, place in enumerate(places[:SCORE_COLUMNS]):
            bounds = square_around(place["x"], place["y"], tile_px_side, size)
            half = (cell_marks / 2.0) if cell_marks else step / 2.0
            mark = _shape("scored", bounds={"x": place["x"] - half, "y": place["y"] - half,
                                            "width": 2 * half, "height": 2 * half},
                          color=SCORED_OUTLINE, width=1, dash=True)
            # Black or dim at the calibrated windows (a registration pair's
            # dim cycle, a sparse marker): redrawn stretched, marked `*`.
            (picture, manifest), stretched = _draw_seen(
                session, project, scan, bounds, channels, SCORE_TILE_PX, pixel=pixel,
                shapes=[mark], scale_bar=c == 0, segmentation=segmentation,
                stretch_dim=True)
            any_stretched |= stretched
            caption = (f"{STRATUM_CAPTIONS.get(stratum, stratum)} {place['score']:.2f}"
                       if c == 0 else f"{place['score']:.2f}") + ("*" if stretched else "")
            sheet.place(r * SCORE_COLUMNS + c, picture, caption)
            tiles.append({"slot": r * SCORE_COLUMNS + c, "score": place["score"],
                          "position": {"x": place["x"], "y": place["y"],
                                       "size_px": round(float(tile_px_side), 1)},
                          **({"cell": place["cell"]} if place.get("cell") is not None else {}),
                          **({"cell_id": place["cell_id"]} if place.get("cell_id") is not None
                             else {}),
                          **({"stretched": True} if stretched else {}),
                          **_brief(manifest)})
        for c in range(len(places[:SCORE_COLUMNS]), SCORE_COLUMNS):
            sheet.blank(r * SCORE_COLUMNS + c)
        placed_rows.append({"row": r, "stratum": stratum, "tiles": tiles})
    if overview:
        r = len(rows)
        whole = _clip_box(0, 0, size[0], size[1], size)
        if scan is not None:
            whole = tissue_box(scan)
        shapes = [_shape(region["id"], region["geometry"], color=REGION_OUTLINE, width=1)
                  for region in (regions or {}).get("regions", [])[:40]
                  if region.get("geometry")]
        picture, manifest = _draw(session, project, scan, whole, channels, SCORE_TILE_PX,
                                  pixel=pixel, shapes=shapes, scale_bar=False)
        found = len((regions or {}).get("regions") or [])
        sheet.place(r * SCORE_COLUMNS, picture,
                    f"WHOLE TISSUE {found} region{'s' if found != 1 else ''}")
        tiles = [{"slot": r * SCORE_COLUMNS, "kind": "whole_tissue", **_brief(manifest)}]
        if field.values.size > 1 and not cell_marks:
            letters = {"clear_good": "g", "borderline_below": "-", "borderline_above": "+",
                       "strongly_abnormal": "s", "clustered": "c"}
            marks = [(letters.get(stratum, "?"), tuple(place["cell"]))
                     for stratum, places in rows for place in places
                     if place.get("cell") is not None]
            heat = _score_heat(field.values, field.valid, threshold, SCORE_TILE_PX,
                               marks=marks, regions_mask=(regions or {}).get("mask"))
            sheet.place(r * SCORE_COLUMNS + 1, heat, f"map, bar {threshold:.2f}")
            tiles.append({"slot": r * SCORE_COLUMNS + 1, "kind": "score_map"})
        for c in range(len(tiles), SCORE_COLUMNS):
            sheet.blank(r * SCORE_COLUMNS + c)
            note_slot = r * SCORE_COLUMNS + c
        placed_rows.append({"row": r, "stratum": "global", "tiles": tiles})
    if any_stretched:
        if note_slot is not None:
            sheet.blank(note_slot, STRETCHED_FOOTNOTE)
        else:
            _footnote(sheet, STRETCHED_FOOTNOTE)
    return _finish(sheet, project, fmt, {
        "project": project, "check": field.check, "channel": field.channel,
        "reference": field.reference, "fingerprint": field.fingerprint,
        "threshold": threshold, "rows": placed_rows,
        **({"stretched": True, "footnote": STRETCHED_FOOTNOTE} if any_stretched else {}),
        "tile_px_side": round(float(tile_px_side), 1)}, "plexora.qc_score_review",
        store=store)


#: The score sheet's note when a tile was drawn stretched (its caption's `*`).
STRETCHED_FOOTNOTE = "* contrast stretched"


def _footnote(sheet, text):
    """`text` at the right of the sheet's title bar (the overview stays at
    calibrated windows; only tiles marked `*` were stretched)."""
    width = sheet.size[0]
    sheet.draw.text((max(sheet.gap + 2, width - 8 * len(text) - 8), 8),
                    layout.ascii_text(text), fill=layout.TEXT, font=layout.font(12))
