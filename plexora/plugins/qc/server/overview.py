"""The visual pass's pictures: the whole tissue on which large artifacts stand
out, a channel contact sheet round one place, and the ranking behind it.

The Artifact Detector finds what intensity rules can; a person spots a long
fold, a lifted corner or a smear of debris at a glance on a thumbnail. This
module draws that thumbnail for an agent (`build_sheet`): four views of the
tissue, cropped to its bounding box so the glass is never looked at --

- **dna_max**: every cycle's nuclear stain, each normalised to its own tissue,
  the brightest at each pixel. Folds (doubled tissue) and bright debris;
- **dna_cycles**: the first cycle's DNA in cyan under the last cycle's in red
  (white where both): tissue lost or lifted later is cyan only, debris gained
  or a shifted cycle red only. One cycle: the DNA inverted, holes and tears
  bright;
- **pan**: the detector's own pan recipe (`artifacts.pan_subset`, log,
  clipped, averaged): autofluorescent folds, debris bright in every channel,
  uneven illumination;
- **agreement**: how many of the pan channels are unusually bright (red) or
  dark (blue) at each place, over a dim pan -- the detector's `agree_bright`
  and `agree_dark` counts drawn. A computed "where to look", never a verdict.

Every tile carries the tissue outline, the QC regions already written (solid,
in their category colour, labelled r1..) and the detector's pending objects
(dashed, d1..), so what is already covered is not drawn twice, and a labelled
grid the agent may name places by. The manifest lists one frame per tile
(`frames.py`), so a click on any tile maps to the image.

`rank_channels` reads one place (a box) and a ring round it from every
channel and ranks the channels by how much the place differs from its
surroundings (the local form of `artifacts._attribute`), then draws the top
few on a contact sheet (`channel_sheet`): the agent picks by name from what
it sees; the ranking is a shortlist. `preview_sheet` draws a proposed outline
twice -- snug, and in context -- for `segment_qc_roi`.

The panels are numpy products, not composites (a max over cycles, a mean of
logs, a count cannot go through `render_region`), read one plane at a time
at an overview level so a forty-channel slide costs a few megapixels.
"""

from __future__ import annotations

import math

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import class_rules, schemas

VISUAL = schemas.VISUAL
KIND = "plexora.qc_visual_overview"
CHANNELS_KIND = "plexora.qc_channel_sheet"
PREVIEW_KIND = "plexora.qc_segment_preview"
PANELS = ("dna_max", "dna_cycles", "pan", "agreement")
PANEL_WORDS = {"dna_max": "DNA, brightest across cycles",
               "dna_cycles": "DNA first cycle cyan, last cycle red, both white",
               "dna_inverted": "DNA inverted: holes and tears bright",
               "pan": "pan: the mean of the channels",
               "agreement": "channels bright together (red) or dark together (blue)"}
#: Channels averaged into the pan and counted for the agreement.
PAN_CHANNELS = 8
#: Margin round the tissue box, as a share of its larger side; round a zoom.
TISSUE_PAD = 0.02
ZOOM_PAD = 0.15
#: The level read is the finest whose crop fits this many tile sides across.
DETAIL = 1.5
#: Regions and objects drawn on the overview, at most.
MAX_REGIONS_DRAWN = 40
MAX_OBJECTS_DRAWN = 30
TISSUE_OUTLINE = "#22e6e6"
OBJECT_OUTLINE = "#ff3df2"
PROPOSED = "#22d3ee"
PROMPT_BOX = "#fbbf24"
INCLUDE = "#22ff22"
EXCLUDE = "#ff3030"
GRID_RGB = (34, 230, 230)
CHANNEL_TILE_PX = 256
#: The contact sheet reads the box and a ring of its own width round it.
RING_WIDTH = 1.0
RING_MIN_PX = 64


# -- the plan: no pixel read ------------------------------------------------------------


def plan(session, project) -> dict:
    """The channels and their roles, the cycles, the pixel size and the image
    size, from the project record alone."""
    from plexora.agent import presets
    from plexora.plugins.qc.server import artifacts as detector
    from plexora.plugins.qc.server import cycles as cycle_rules
    from plexora.plugins.qc.server import results
    from plexora.server.utils import pixel_scale, source_image

    record = session.project(project)
    if record.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to look at")
    channel_records = list(record.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channel_records]
    if not names:
        raise AgentError("unsupported_modality", "this image has no channels to draw")
    keys = {name: source_image.channel_key(c) for name, c in zip(names, channel_records)}
    override = results.load(project).get("cycles_override")
    cycles = cycle_rules.infer(names, override=override)
    per_cycle = [c["nuclear"] for c in cycles["cycles"] if c.get("nuclear")]
    nuclear = list(dict.fromkeys(per_cycle)) or presets.nuclear_channels(names)
    pan = detector.pan_subset(names, PAN_CHANNELS)
    pixel = pixel_scale.pixel_size(record)
    return {"project": project, "names": names, "keys": keys, "nuclear": nuclear,
            "cycles": {"method": cycles.get("method"), "confidence": cycles.get("confidence"),
                       "n": len(cycles.get("cycles") or []),
                       "of_channel": dict(cycles.get("of_channel") or {})},
            "pan": pan, "pixel_um": float(pixel["value"]) if pixel else None,
            "image_size": (int(record.image.width or 0), int(record.image.height or 0))}


def _refuse_brightfield(source):
    if source.is_brightfield:
        raise AgentError("unsupported_modality", "the visual pass reads fluorescence channels; "
                         "a brightfield slide has one picture, which render_region shows")


# -- geometry ------------------------------------------------------------------------------


def _clamp(box, image_size):
    width, height = image_size
    x0, y0, x1, y1 = box
    x0, y0 = max(0.0, float(x0)), max(0.0, float(y0))
    x1, y1 = min(float(width), float(x1)), min(float(height), float(y1))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise AgentError("invalid_input", "the region is outside the image")
    return x0, y0, x1, y1


def _padded(box, share, image_size):
    x0, y0, x1, y1 = box
    pad = share * max(x1 - x0, y1 - y0)
    return _clamp((x0 - pad, y0 - pad, x1 + pad, y1 + pad), image_size)


def _choose_level(source, image_size, box, target_px, max_pixels):
    """(level, (fx, fy)): the finest level whose crop of `box` fits in
    `target_px` across and `max_pixels` in all; the coarsest when none does."""
    width, height = image_size
    x0, y0, x1, y1 = box
    levels = max(1, int(source.levels))
    chosen = levels - 1
    for level in range(levels):
        lh, lw = source.level_shape(level)
        fx, fy = width / max(1, lw), height / max(1, lh)
        cw, ch = (x1 - x0) / fx, (y1 - y0) / fy
        if max(cw, ch) <= target_px and cw * ch <= max_pixels:
            chosen = level
            break
    lh, lw = source.level_shape(chosen)
    return chosen, (width / max(1, lw), height / max(1, lh))


def _level_box(box, factor):
    fx, fy = factor
    x0, y0, x1, y1 = box
    lbox = (int(math.floor(x0 / fx)), int(math.floor(y0 / fy)),
            int(math.ceil(x1 / fx)), int(math.ceil(y1 / fy)))
    return (lbox[0], lbox[1], max(lbox[0] + 1, lbox[2]), max(lbox[1] + 1, lbox[3]))


def _fullres_of(lbox, factor):
    fx, fy = factor
    return {"x": lbox[0] * fx, "y": lbox[1] * fy, "width": (lbox[2] - lbox[0]) * fx,
            "height": (lbox[3] - lbox[1]) * fy}


def _mask_for_crop(mask, mask_factor, lbox, factor):
    """An overview-level mask sampled onto a crop (`lbox`, at `factor`)."""
    mask = np.asarray(mask, dtype=bool)
    h, w = lbox[3] - lbox[1], lbox[2] - lbox[0]
    rows = np.minimum(((np.arange(h) + lbox[1] + 0.5) * factor[1] / mask_factor).astype(int),
                      mask.shape[0] - 1)
    cols = np.minimum(((np.arange(w) + lbox[0] + 0.5) * factor[0] / mask_factor).astype(int),
                      mask.shape[1] - 1)
    return mask[rows[:, None], cols[None, :]]


def tissue_box(found, image_size, *, pad=TISSUE_PAD):
    """The tight tissue's bounding box in full-resolution pixels, padded; the
    whole image when the mask holds no tissue (or all of it)."""
    width, height = image_size
    mask = np.asarray(found["mask"], dtype=bool)
    factor = float(found.get("factor") or 1.0)
    if not mask.any() or mask.all():
        return (0.0, 0.0, float(width), float(height)), False
    ys, xs = np.nonzero(mask)
    box = (xs.min() * factor, ys.min() * factor, (xs.max() + 1) * factor, (ys.max() + 1) * factor)
    return _padded(box, pad, image_size), True


def _tissue_outline(found, image_size):
    from plexora.server.utils import mask_polygon

    mask = np.asarray(found["mask"], dtype=bool)
    if not mask.any() or mask.all():
        return None
    factor = float(found.get("factor") or 1.0)
    try:
        return mask_polygon.mask_to_polygon(mask, (0, 0), factor, simplify_px=factor,
                                            max_vertices=2000)
    except Exception:  # noqa: BLE001 -- no outline is better than no sheet
        return None


# -- the planes ------------------------------------------------------------------------


def _read(source, index, level, lbox):
    from plexora.plugins.qc.server import artifacts as detector

    plane, _dtype, _n = detector._read_box(source, index, level, lbox)
    return plane


def _sample(mask, limit=1_000_000):
    idx = np.flatnonzero(mask.ravel())
    if idx.size > limit:
        idx = idx[::int(math.ceil(idx.size / limit))]
    return idx


def accumulate(source, spec, level, lbox, factor, on, *, panels=PANELS) -> dict:
    """Read each needed plane once and fold it into the panels' arrays:
    {dna_max, dna_first, dna_last, pan, agree_bright, agree_dark, n_pan,
    n_dna}. `on` is the tissue inside the crop (its statistics' sample)."""
    from plexora.plugins.qc.server import artifacts as detector

    h, w = lbox[3] - lbox[1], lbox[2] - lbox[0]
    on = np.asarray(on, dtype=bool)
    if not on.any():
        on = np.ones((h, w), dtype=bool)
    sample = _sample(on)
    um = spec["pixel_um"] * factor[0] if spec["pixel_um"] else None
    params = detector.PARAMS_DEFAULT
    sigma = (params["agree_sigma_um"] / um) if um else 2.0
    sigma = float(min(max(sigma, 0.5), 8.0))
    want_dna = any(p in ("dna_max", "dna_cycles") for p in panels)
    want_pan = any(p in ("pan", "agreement") for p in panels)
    want_agree = "agreement" in panels
    nuclear = list(spec["nuclear"])
    pan = set(spec["pan"])
    out = {"dna_max": np.zeros((h, w), np.float32), "dna_first": None, "dna_last": None,
           "pan": np.zeros((h, w), np.float32), "agree_bright": np.zeros((h, w), np.uint8),
           "agree_dark": np.zeros((h, w), np.uint8), "n_pan": 0, "n_dna": 0}
    for name in spec["names"]:
        is_dna = want_dna and name in nuclear
        is_pan = want_pan and name in pan
        if not (is_dna or is_pan):
            continue
        index = source.channel_index(spec["keys"][name])
        if index is None:
            continue
        la = np.log1p(np.maximum(_read(source, index, level, lbox), 0.0))
        if is_dna:
            top = float(np.percentile(la.ravel()[sample], 99.9)) if sample.size else 1.0
            norm = np.clip(la / max(top, 1e-6), 0.0, 1.0).astype(np.float32)
            np.maximum(out["dna_max"], norm, out=out["dna_max"])
            if out["dna_first"] is None:
                out["dna_first"] = norm
            out["dna_last"] = norm
            out["n_dna"] += 1
        if is_pan:
            top = float(np.percentile(la.ravel()[sample], 99)) if sample.size else 1.0
            out["pan"] += np.minimum(la / max(top, 1e-6), detector.PAN_CLIP)
            out["n_pan"] += 1
            if want_agree:
                g = detector._blur(la, sigma)
                med, mad = detector._robust(g.ravel()[sample])
                z = (g - med) / mad
                out["agree_bright"] += (z > params["agree_z"]).astype(np.uint8)
                out["agree_dark"] += (z < -params["agree_z"]).astype(np.uint8)
                del g, z
        del la
    return out


def _grey(values, gamma=1.0):
    v = np.clip(np.asarray(values, dtype=np.float32), 0.0, 1.0)
    if gamma != 1.0:
        v = np.power(v, gamma)
    u8 = (v * 255.0 + 0.5).astype(np.uint8)
    return np.stack([u8, u8, u8], axis=-1)


def panel_rgb(name, acc, on=None) -> tuple:
    """((H, W, 3) uint8, the panel actually drawn): a panel from the
    accumulated arrays. `dna_cycles` with one cycle becomes `dna_inverted`;
    the agreement is painted on the tissue (`on`) only -- the glass is dark
    in every channel, which is not channels agreeing on an artifact."""
    from plexora.plugins.qc.server import artifacts as detector

    if name == "dna_max":
        return _grey(acc["dna_max"], gamma=0.8), name
    if name == "dna_cycles":
        first, last = acc["dna_first"], acc["dna_last"]
        if first is None:
            return _grey(np.zeros_like(acc["dna_max"])), name
        if acc["n_dna"] < 2:
            return _grey(1.0 - first), "dna_inverted"
        rgb = np.zeros(first.shape + (3,), np.uint8)
        rgb[..., 0] = (np.clip(last, 0, 1) * 255 + 0.5).astype(np.uint8)
        rgb[..., 1] = (np.clip(first, 0, 1) * 255 + 0.5).astype(np.uint8)
        rgb[..., 2] = rgb[..., 1]
        return rgb, name
    pan = acc["pan"] / max(1, acc["n_pan"]) / detector.PAN_CLIP
    if name == "pan":
        return _grey(pan, gamma=0.9), name
    if name == "agreement":
        n = max(1, acc["n_pan"])
        base = np.clip(pan, 0, 1) * 0.35 * 255
        rgb = np.zeros(pan.shape + (3,), np.float32)
        rgb[..., 0] = base + 220.0 * acc["agree_bright"] / n
        rgb[..., 1] = base
        rgb[..., 2] = base + 220.0 * acc["agree_dark"] / n
        if on is not None and on.shape == pan.shape and on.any():
            off = ~np.asarray(on, dtype=bool)
            rgb[off] = base[off][:, None]
        return np.clip(rgb, 0, 255).astype(np.uint8), name
    raise AgentError("invalid_input", f"no panel named {name!r}", detail={"panels": PANELS})


# -- drawing ---------------------------------------------------------------------------


def _fit(picture, slot):
    """`picture` scaled (up or down) to fit a square slot."""
    from PIL import Image

    scale = min(slot / max(1, picture.width), slot / max(1, picture.height))
    size = (max(1, int(round(picture.width * scale))), max(1, int(round(picture.height * scale))))
    if size == picture.size:
        return picture
    return picture.resize(size, Image.LANCZOS if scale < 1 else Image.BICUBIC)


def _shape(id, geometry=None, bounds=None, *, color, width=2, dash=False, label="",
           fill_alpha=0.0):
    from plexora.agent.render_spec import Bounds, ShapeSpec

    return ShapeSpec(id=str(id)[:32], geometry=geometry,
                     bounds=Bounds(**bounds) if bounds else None, color=color, width=width,
                     dash=dash, label=str(label)[:24], fill_alpha=fill_alpha)


def _draw(picture, shapes, fullres):
    from plexora.agent import render

    if shapes:
        render._draw_shapes(picture, shapes, fullres, picture.size)
    return picture


def _bbox_of(geometry):
    from shapely.geometry import shape

    try:
        x0, y0, x1, y1 = shape(geometry).bounds
    except Exception:  # noqa: BLE001
        return None
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def _intersects(geometry, box):
    b = _bbox_of(geometry)
    if b is None:
        return False
    return not (b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3])


def existing_regions(session, project, box=None, *, result=None) -> tuple:
    """([{label, roi_id, class, category, action, geometry, bbox, name}],
    more): the QC regions that count now (of `result`, else the active
    one), largest first, those touching `box`."""
    from plexora.plugins.qc.server import polygons, results, roi_link

    try:
        if result is None:
            result = results.active(results.load(project))
        live = roi_link.live_regions(session.image_data(project), result)
    except Exception:  # noqa: BLE001 -- no QC yet
        live = []
    rows = []
    for region in live:
        if box is not None and not _intersects(region["geometry"], box):
            continue
        rows.append({"roi_id": region["roi_id"], "class": region["class"],
                     "category": region.get("category")
                     or schemas.category_of_class(region["class"]),
                     "action": region.get("action"), "geometry": region["geometry"],
                     "name": region.get("name"),
                     "area_px": polygons.area_of(region["geometry"])})
    rows.sort(key=lambda r: -r["area_px"])
    kept = rows[:MAX_REGIONS_DRAWN]
    for i, row in enumerate(kept, start=1):
        row["label"] = f"r{i}"
        row["bbox"] = _bbox_of(row["geometry"])
    return kept, max(0, len(rows) - len(kept))


def for_agent(manifest) -> dict:
    """What an agent reading the overview needs of its manifest: the labels on
    the picture and what each names, the grid's size. Places stay pictures --
    the agent points on them -- so the boxes, names and grid squares the
    stored manifest keeps are not sent."""
    grid = manifest.get("grid") or {}
    return {"regions": [{k: r.get(k) for k in ("label", "roi_id", "class", "action")}
                        for r in manifest.get("regions") or []],
            "regions_more": manifest.get("regions_more"),
            "objects": [{k: o.get(k) for k in ("label", "id", "class", "score")}
                        for o in manifest.get("objects") or []],
            "grid": {k: grid.get(k) for k in ("rows", "columns")} if grid else None}


def pending_objects(project, regions, box=None) -> list:
    """The Artifact Detector's objects above their bars that no written
    region already covers (IoU under the session's `explained_iou`),
    strongest first: [{label, id, category, class, score, geometry, bbox}]."""
    from plexora.plugins.qc.server import artifacts as detector
    from plexora.plugins.qc.server import polygons

    try:
        summary = detector.current(project)
    except Exception:  # noqa: BLE001
        summary = None
    if not summary:
        return []
    found = detector.objects_at(summary, detector.thresholds_of(project, summary), geometry=True)
    out = []
    for o in found:
        geometry = o.get("geometry")
        if not geometry or (box is not None and not _intersects(geometry, box)):
            continue
        covered = any(polygons.overlap(geometry, r["geometry"])["iou"]
                      >= schemas.ENGINE["explained_iou"] for r in regions)
        if covered:
            continue
        out.append({"id": o.get("id"), "category": o["category"],
                    "class": detector.CATEGORY_CLASS.get(o["category"]),
                    "score": round(float(o.get("score") or 0.0), 3),
                    "geometry": geometry, "bbox": _bbox_of(geometry)})
        if len(out) >= MAX_OBJECTS_DRAWN:
            break
    for i, row in enumerate(out, start=1):
        row["label"] = f"d{i}"
    return out


def _overlay_shapes(tissue_geometry, regions, objects, project):
    from plexora.plugins.qc.server import artifacts as detector

    shapes = []
    if tissue_geometry:
        shapes.append(_shape("tissue", tissue_geometry, color=TISSUE_OUTLINE, width=1))
    for r in regions:
        shapes.append(_shape(r["label"], r["geometry"],
                             color=schemas.CATEGORY_COLORS.get(r["category"], "#eab308"),
                             width=2, label=r["label"]))
    for o in objects:
        try:
            colour = detector.color_of(project, o["category"])
        except Exception:  # noqa: BLE001
            colour = OBJECT_OUTLINE
        shapes.append(_shape(o["label"], o["geometry"], color=colour, width=1, dash=True,
                             label=o["label"]))
    return shapes


def grid_spec(box, side) -> dict:
    """Squares A1.. over `box` (full-resolution): {rows, columns, squares}."""
    import string

    x0, y0, x1, y1 = box
    rows = columns = int(side)
    ex = np.linspace(x0, x1, columns + 1)
    ey = np.linspace(y0, y1, rows + 1)
    squares = []
    for r in range(rows):
        for c in range(columns):
            squares.append({"id": f"{string.ascii_uppercase[r]}{c + 1}",
                            "bounds": [round(float(ex[c]), 1), round(float(ey[r]), 1),
                                       round(float(ex[c + 1]), 1), round(float(ey[r + 1]), 1)]})
    return {"rows": rows, "columns": columns, "squares": squares}


def _draw_grid(picture, spec, fullres):
    from PIL import ImageDraw

    from plexora.agent.evidence import sheet_layout as layout

    draw = ImageDraw.Draw(picture)
    sx = picture.width / max(1e-9, fullres[2] - fullres[0])
    sy = picture.height / max(1e-9, fullres[3] - fullres[1])
    for square in spec["squares"]:
        bx0, by0, bx1, by1 = square["bounds"]
        px0, py0 = (bx0 - fullres[0]) * sx, (by0 - fullres[1]) * sy
        px1, py1 = (bx1 - fullres[0]) * sx, (by1 - fullres[1]) * sy
        draw.rectangle((px0, py0, max(px0, px1 - 1), max(py0, py1 - 1)), outline=GRID_RGB,
                       width=1)
        draw.text((px0 + 3, py0 + 2), square["id"], fill=(255, 255, 255), font=layout.font(10),
                  stroke_width=2, stroke_fill=(0, 0, 0))


# -- the overview sheet --------------------------------------------------------------


def build_sheet(session, project, *, region=None, panels=None, size="standard", grid=True,
                show_regions=True, result=None, fmt="png", store=True) -> dict:
    """The overview sheet: {image, format, manifest, artifact, size}. `region`
    (full-resolution {x, y, width, height}) zooms the same four views on one
    place; without it the sheet is the tissue's bounding box. `result` is the
    QC result whose regions are outlined (default the active one)."""
    from PIL import Image

    from plexora.agent.evidence import sheet_layout as layout
    from plexora.plugins.qc.server import sheets, tissue
    from plexora.server.utils import source_image

    spec = plan(session, project)
    image_size = spec["image_size"]
    wanted = [p for p in (panels or PANELS) if p in PANELS] or list(PANELS)
    slot = int(VISUAL["overview_px_large" if size == "large" else "overview_px"])
    found = tissue.for_project(session, project)
    full_box, cropped = tissue_box(found, image_size)
    if region is not None:
        box = _padded((region["x"], region["y"], region["x"] + region["width"],
                       region["y"] + region["height"]), ZOOM_PAD, image_size)
        zoomed = True
    else:
        box, zoomed = full_box, False
    with source_image.SHELF.reader(session.image_data(project)) as source:
        _refuse_brightfield(source)
        level, factor = _choose_level(source, image_size, box, slot * DETAIL,
                                      VISUAL["overview_max_pixels"])
        lbox = _level_box(box, factor)
        on = _mask_for_crop(found["mask"], float(found.get("factor") or 1.0), lbox, factor)
        acc = accumulate(source, spec, level, lbox, factor, on, panels=wanted)
    fullres = _fullres_of(lbox, factor)
    frame_box = (fullres["x"], fullres["y"], fullres["x"] + fullres["width"],
                 fullres["y"] + fullres["height"])
    regions, more = existing_regions(session, project, frame_box, result=result) \
        if show_regions else ([], 0)
    objects = pending_objects(project, regions, frame_box) if show_regions else []
    shapes = _overlay_shapes(_tissue_outline(found, image_size), regions, objects, project)
    squares = grid_spec(frame_box, VISUAL["grid_side"]) if grid else None
    columns = 2 if len(wanted) > 1 else 1
    rows = int(math.ceil(len(wanted) / columns))
    title = (f"{project} - {'zoom' if zoomed else 'whole tissue'} - large artifacts: folds, "
             "tears, debris, bubbles, lifted tissue")
    sheet = layout.Sheet(columns, rows, slot, title=title)
    frames, drawn = [], []
    for index, name in enumerate(wanted):
        rgb, actual = panel_rgb(name, acc, on)
        picture = _fit(Image.fromarray(rgb, "RGB"), slot)
        _draw(picture, shapes, frame_box)
        if squares:
            _draw_grid(picture, squares, frame_box)
        caption = f"{index + 1} | {PANEL_WORDS.get(actual, actual)}"
        where = sheet.place(index, picture, caption)
        frames.append({"slot": index, "panel": actual, "origin": [where["x"], where["y"]],
                       "size": [where["width"], where["height"]],
                       "bounds_fullres": dict(fullres), "level": level})
        drawn.append({"slot": index, "panel": actual, "words": PANEL_WORDS.get(actual, actual)})
    manifest = {
        "project": project, "bounds": dict(fullres), "level": level,
        "factor": round(float(factor[0]), 4), "zoomed": zoomed, "panels": drawn,
        "frames": frames,
        "tissue": {"bbox": {"x": full_box[0], "y": full_box[1],
                            "width": full_box[2] - full_box[0],
                            "height": full_box[3] - full_box[1]},
                   "cropped_to_tissue": cropped, "method": found.get("method"),
                   "source": found.get("source"),
                   "fraction_of_image": round(float(np.asarray(found["mask"]).mean()), 4)},
        "regions": [{k: r[k] for k in ("label", "roi_id", "class", "category", "action",
                                       "bbox", "name")} for r in regions],
        "regions_more": more,
        "objects": [{k: o[k] for k in ("label", "id", "category", "class", "score", "bbox")}
                    for o in objects],
        "grid": squares,
        "channels": {"dna": spec["nuclear"], "pan": spec["pan"],
                     "cycles": {k: spec["cycles"][k] for k in ("method", "confidence", "n")}},
        "pixel_um": spec["pixel_um"],
        "how_to_point": ("give {artifact_id, px} with px in this sheet's own pixels, on any "
                         "tile; every tile shows the same place, so the same offset inside "
                         "each tile is the same spot"),
    }
    return sheets._finish(sheet, project, fmt, manifest, KIND, store=store)


# -- channels round a place --------------------------------------------------------


def _ring_box(box, image_size):
    x0, y0, x1, y1 = box
    w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    pad_x, pad_y = max(RING_WIDTH * w, RING_MIN_PX), max(RING_WIDTH * h, RING_MIN_PX)
    return _clamp((x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y), image_size)


def rank_channels(session, project, box, *, top=None, exclude_nuclear=False,
                  include=()) -> dict:
    """Every channel's contrast at `box` (full-resolution {x, y, width,
    height}) against a ring round it on the tissue, strongest first, and the
    planes of the top few for the contact sheet -- the channels named in
    `include` first, wherever they rank."""
    from plexora.plugins.qc.server import artifacts as detector
    from plexora.plugins.qc.server import tissue
    from plexora.server.utils import source_image

    top = int(top or VISUAL["channel_sheet_top"])
    spec = plan(session, project)
    unknown = [name for name in include if name not in spec["keys"]]
    if unknown:
        raise AgentError("invalid_input", f"not channels of {project}: {', '.join(unknown)}",
                         detail={"channels": list(spec["names"])})
    include = list(dict.fromkeys(include))[:top]
    by_rank = top - len(include)
    image_size = spec["image_size"]
    inner_box = _clamp((box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]),
                       image_size)
    ring_box = _ring_box(inner_box, image_size)
    found = tissue.for_project(session, project)
    with source_image.SHELF.reader(session.image_data(project)) as source:
        _refuse_brightfield(source)
        level, factor = _choose_level(source, image_size, ring_box, 3 * CHANNEL_TILE_PX,
                                      1_500_000)
        lbox = _level_box(ring_box, factor)
        h, w = lbox[3] - lbox[1], lbox[2] - lbox[0]
        on = _mask_for_crop(found["mask"], float(found.get("factor") or 1.0), lbox, factor)
        inside = np.zeros((h, w), dtype=bool)
        ix0 = int(max(0, math.floor(inner_box[0] / factor[0]) - lbox[0]))
        iy0 = int(max(0, math.floor(inner_box[1] / factor[1]) - lbox[1]))
        ix1 = int(min(w, math.ceil(inner_box[2] / factor[0]) - lbox[0]))
        iy1 = int(min(h, math.ceil(inner_box[3] / factor[1]) - lbox[1]))
        inside[iy0:max(iy0 + 1, iy1), ix0:max(ix0 + 1, ix1)] = True
        inner = inside & on
        if not inner.any():
            inner = inside
        ring = on & ~inside
        ring_on_tissue = float(ring.sum()) / max(1, int((~inside).sum()))
        if ring.sum() < RING_MIN_PX:
            ring = ~inside
        # One plane is held per channel the sheet can still show (the `top`
        # strongest so far) and one running DNA maximum: with a hundred
        # channels, holding every plane would hold a gigabyte.
        planes, strength, dna_reference, ranked = {}, {}, None, []
        for name in spec["names"]:
            index = source.channel_index(spec["keys"][name])
            if index is None:
                continue
            la = np.log1p(np.maximum(_read(source, index, level, lbox), 0.0)).astype(np.float32)
            med, mad = detector._robust(la[ring])
            hi = (float(np.percentile(la[inner], 90)) - med) / mad
            lo = (float(np.percentile(la[inner], 10)) - med) / mad
            contrast = hi if hi >= -lo else lo
            is_nuclear = name in spec["nuclear"]
            row = {"channel": name, "nuclear": is_nuclear,
                   "cycle": spec["cycles"]["of_channel"].get(name),
                   "contrast": round(float(contrast), 2),
                   "direction": "brighter" if contrast >= 0 else "darker"}
            if class_rules.is_af_channel(name):
                row["autofluorescence"] = True
            ranked.append(row)
            if is_nuclear:
                scaled = np.clip(la / max(float(np.percentile(la, 99.9)), 1e-6), 0, 1)
                dna_reference = scaled if dna_reference is None \
                    else np.maximum(dna_reference, scaled)
            if name in include:
                planes[name] = la
                continue
            if (exclude_nuclear and is_nuclear) or by_rank <= 0:
                continue
            planes[name], strength[name] = la, abs(float(contrast))
            if len(strength) > by_rank:
                weakest = min(strength, key=strength.get)
                del planes[weakest], strength[weakest]
    ranked.sort(key=lambda r: -abs(r["contrast"]))
    shown = [*include, *[r["channel"] for r in ranked if r["channel"] not in include
                         and not (exclude_nuclear and r["nuclear"])][:by_rank]]
    return {"ranked": ranked, "shown": shown,
            "box": {"x": inner_box[0], "y": inner_box[1], "width": inner_box[2] - inner_box[0],
                    "height": inner_box[3] - inner_box[1]},
            "ring_box": ring_box, "ring_on_tissue_fraction": round(ring_on_tissue, 3),
            "level": level, "factor": factor, "lbox": lbox,
            "planes": {name: planes[name] for name in shown},
            "dna_reference": dna_reference, "spec": spec}


def _window_grey(la, lo_pct=1.0, hi_pct=99.5):
    lo, hi = np.percentile(la, [lo_pct, hi_pct])
    if not hi > lo:
        hi = lo + 1e-6
    return _grey((la - lo) / (hi - lo))


def channel_sheet(session, project, ranking, *, fmt="png", store=True) -> dict:
    """The contact sheet of `rank_channels`: a DNA reference tile, then the
    top channels, each the ring box with the place outlined."""
    from PIL import Image

    from plexora.agent.evidence import sheet_layout as layout
    from plexora.plugins.qc.server import sheets

    shown = ranking["shown"]
    fullres = _fullres_of(ranking["lbox"], ranking["factor"])
    frame_box = (fullres["x"], fullres["y"], fullres["x"] + fullres["width"],
                 fullres["y"] + fullres["height"])
    box = ranking["box"]
    marks = [_shape("place", bounds=box, color=PROMPT_BOX, width=1, dash=True)]
    slots = 1 + len(shown)
    columns = 3 if slots > 4 else 2 if slots > 1 else 1
    rows = int(math.ceil(slots / columns))
    sheet = layout.Sheet(columns, rows, CHANNEL_TILE_PX,
                         title=f"{project} - the place (dashed) in each channel, ranked by "
                               "contrast against its surroundings")
    frames = []

    def place(index, rgb, caption, panel):
        picture = _fit(Image.fromarray(rgb, "RGB"), CHANNEL_TILE_PX)
        _draw(picture, marks, frame_box)
        where = sheet.place(index, picture, caption)
        frames.append({"slot": index, "panel": panel, "origin": [where["x"], where["y"]],
                       "size": [where["width"], where["height"]],
                       "bounds_fullres": dict(fullres), "level": ranking["level"]})

    if ranking["dna_reference"] is not None:
        place(0, _grey(ranking["dna_reference"], gamma=0.8), "ref | DNA, brightest across cycles",
              "dna_max")
    else:
        sheet.blank(0, "no DNA channel")
    by_name = {r["channel"]: r for r in ranking["ranked"]}
    for i, name in enumerate(shown, start=1):
        row = by_name[name]
        caption = f"{i} | {name} | z {row['contrast']:+.1f} {row['direction']}"
        place(i, _window_grey(ranking["planes"][name]), caption, name)
    for index in range(slots, columns * rows):
        sheet.blank(index)
    manifest = {"project": project, "bounds": dict(fullres), "level": ranking["level"],
                "frames": frames, "box": box, "ranked": ranking["ranked"], "shown": shown,
                "ring_on_tissue_fraction": ranking["ring_on_tissue_fraction"],
                "channels": {"dna": ranking["spec"]["nuclear"]}}
    return sheets._finish(sheet, project, fmt, manifest, CHANNELS_KIND, store=store)


# -- the preview of a proposed outline -----------------------------------------------


def _prompt_marks(points, box, view):
    side = max(4.0, 0.015 * max(view["width"], view["height"]))
    marks = []
    for i, p in enumerate(points or []):
        marks.append(_shape(f"p{i}", bounds={"x": p["x"] - side / 2, "y": p["y"] - side / 2,
                                            "width": side, "height": side},
                            color=INCLUDE if p.get("label", 1) == 1 else EXCLUDE, width=2,
                            fill_alpha=0.6))
    if box:
        marks.append(_shape("box", bounds=box, color=PROMPT_BOX, width=1, dash=True))
    return marks


def preview_sheet(session, project, *, geometry, points, box, view, channels, image_size,
                  result=None, fmt="png", store=True) -> dict:
    """Two panels: the proposed outline snug on the prompt view, and in
    context (the view grown `context_pad` times) beside the regions already
    written. Both tiles carry frames, so the next round may point on either."""
    from plexora.agent.evidence import sheet_layout as layout
    from plexora.agent.render_spec import Bounds, ChannelSpec, OutputSpec, RenderInput
    from plexora.plugins.qc.server import sheets

    colours = ("#ffffff", "#00ff00", "#ff00ff", "#00ffff")
    specs = [ChannelSpec(name=c["name"], color=c.get("color") or colours[i % 4],
                         window=list(c["range"]) if c.get("range") else "auto")
             for i, c in enumerate((channels or [])[:4])] or None
    proposed = _shape("proposed", geometry, color=PROPOSED, width=2, fill_alpha=0.15,
                      label="proposed")
    fit_px = int(VISUAL["preview_px"])
    context_box = _padded((view["x"], view["y"], view["x"] + view["width"],
                           view["y"] + view["height"]), (VISUAL["context_pad"] - 1) / 2,
                          image_size)
    context = {"x": context_box[0], "y": context_box[1],
               "width": context_box[2] - context_box[0],
               "height": context_box[3] - context_box[1]}
    regions, _more = existing_regions(session, project, context_box, result=result)
    sheet = layout.Sheet(2, 1, fit_px, title=f"{project} - the proposed outline (cyan): snug, "
                                             "and in context")
    frames = []
    panels = ((view, [proposed, *_prompt_marks(points, box, view)], "fit",
               "1 | the outline on the prompt view"),
              (context, [proposed, *_overlay_shapes(None, regions, [], project)], "context",
               "2 | in context, with the regions already written"))
    for index, (bounds, shapes, panel, caption) in enumerate(panels):
        spec = RenderInput(project=project, bounds=Bounds(**bounds), channels=specs,
                           segmentation="none", output=OutputSpec(width=fit_px), layers="none")
        picture, manifest = layout.render_panel(session, spec)
        layout.shapes_onto(picture, manifest, shapes)
        where = sheet.place(index, picture, caption)
        frames.append({"slot": index, "panel": panel, "origin": [where["x"], where["y"]],
                       "size": [where["width"], where["height"]],
                       "bounds_fullres": dict(manifest["bounds_fullres"]),
                       "level": manifest.get("level")})
    manifest = {"project": project, "frames": frames, "view": dict(view), "context": context,
                "regions": [{k: r[k] for k in ("label", "roi_id", "class", "action")}
                            for r in regions],
                "channels": [{"name": c["name"], "color": c.get("color"),
                              "window": c.get("range")} for c in (channels or [])[:4]]}
    return sheets._finish(sheet, project, fmt, manifest, PREVIEW_KIND, store=store)
