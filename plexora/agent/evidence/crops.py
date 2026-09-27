"""Many cells' crops at once, read a pyramid tile at a time.

A collage of 24 cells drawn as 24 renders is 24 pyramid reads per channel, 24
mask reads and 24 resamples -- and neighbouring cells on one tile decode that
tile again and again. Here cells are grouped by the pyramid tile they fall in
at the chosen level, each group's crops are covered by ONE read per channel
and ONE mask read, and every crop is a view into that block. Reads scale with
the tiles touched, not the cells shown.

The planes come back raw (the source's own dtype, as float32); panels are
blended from them with `source_image.blend_plane` -- the viewer's arithmetic,
the same function `composite` uses -- so a crop draws exactly what a render of
the same box at the same level draws.
"""

from __future__ import annotations

import math

import numpy as np

from plexora.agent.errors import AgentError

#: Largest block one group may read, per side, at the chosen level.
MAX_GROUP_SIDE = 2048


class Crop:
    """One cell's square crop: raw planes by channel name, labels, geometry."""

    __slots__ = ("cell_id", "x", "y", "planes", "labels", "box", "level", "mask_stats")

    def __init__(self, cell_id, x, y, box, level):
        self.cell_id, self.x, self.y, self.box, self.level = cell_id, x, y, box, level
        self.planes = {}
        self.labels = None
        self.mask_stats = None


def crop_side_px(record, crop_um=None, crop_px=None, default_um=24.0, default_px=48.0,
                 pixel=None):
    """(side in full-resolution px, how) -- microns when the image is calibrated,
    or when `pixel` (`{value, source}`) stands in for a calibration it lacks."""
    from plexora.server.utils import pixel_scale

    if crop_px is not None:
        return float(crop_px), "explicit_px"
    pixel = pixel if pixel is not None else pixel_scale.pixel_size(record)
    if crop_um is not None and pixel:
        return float(crop_um) / pixel["value"], "explicit_um"
    if pixel:
        return default_um / pixel["value"], "default_um"
    return default_px, "default_px"


def read_cell_crops(session, record, cells, *, channels, crop_px, tile_px, mask=True,
                    level=None):
    """({cell_id: Crop}, info) for `cells` = [{cell_id, x, y}].

    `channels` maps a channel NAME to its record key (`source_image.channel_key`).
    Crops are padded with zeros (and label 0) where they run off the image.
    """
    from plexora.agent import render
    from plexora.server.utils import source_image

    if not cells:
        return {}, {"reads": 0, "groups": 0, "level": 0}
    image_data = session.image_data(record.name)
    tile_w = int(record.image.tile_width or 1024)
    tile_h = int(record.image.tile_height or 1024)
    crops = {}
    reads = 0
    with source_image.SHELF.reader(image_data) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality",
                             "cell crops are drawn per channel; this image is brightfield")
        if level is None:
            level = source_image.choose_level(source, crop_px, tile_px)
        level = max(0, min(int(level), source.levels - 1))
        div = 2 ** level
        side = max(4, int(math.ceil(crop_px / div)))
        indices = {name: source.channel_index(key) for name, key in channels.items()}
        missing = [name for name, index in indices.items() if index is None]
        if missing:
            raise AgentError("invalid_input", f"not channels of this image: {missing}")
        groups = {}
        for cell in cells:
            cx, cy = float(cell["x"]) / div, float(cell["y"]) / div
            x0 = int(math.floor(cx - side / 2))
            y0 = int(math.floor(cy - side / 2))
            box = (x0, y0, x0 + side, y0 + side)
            crop = Crop(int(cell["cell_id"]), float(cell["x"]), float(cell["y"]), box, level)
            crops[crop.cell_id] = crop
            key = (int(cx // tile_w), int(cy // tile_h))
            groups.setdefault(key, []).append(crop)
        for members in groups.values():
            gx0 = min(c.box[0] for c in members)
            gy0 = min(c.box[1] for c in members)
            gx1 = max(c.box[2] for c in members)
            gy1 = max(c.box[3] for c in members)
            if gx1 - gx0 > MAX_GROUP_SIDE or gy1 - gy0 > MAX_GROUP_SIDE:
                blocks = [(c.box, [c]) for c in members]
            else:
                blocks = [((gx0, gy0, gx1, gy1), members)]
            for block, owners in blocks:
                bw, bh = block[2] - block[0], block[3] - block[1]
                for name, index in indices.items():
                    plane, clipped = source.read(index, level, block)
                    reads += 1
                    canvas = np.zeros((bh, bw), dtype=np.float32)
                    top, left = clipped[1] - block[1], clipped[0] - block[0]
                    plane = np.asarray(plane, dtype=np.float32)
                    h = min(plane.shape[0], bh - top)
                    w = min(plane.shape[1], bw - left)
                    if h > 0 and w > 0 and plane.size > 1:
                        canvas[top:top + h, left:left + w] = plane[:h, :w]
                    for crop in owners:
                        oy, ox = crop.box[1] - block[1], crop.box[0] - block[0]
                        crop.planes[name] = canvas[oy:oy + side, ox:ox + side]
        mask_status = "none"
        if mask:
            provider, mask_status, _reason, _locator = render._mask_for(record)
            if provider is not None:
                mask_level = level + record.segmentation.extra_levels
                for members in groups.values():
                    gx0 = min(c.box[0] for c in members)
                    gy0 = min(c.box[1] for c in members)
                    gx1 = max(c.box[2] for c in members)
                    gy1 = max(c.box[3] for c in members)
                    labels = provider.read_region(mask_level, (gx0, gy0, gx1, gy1))
                    reads += 1
                    for crop in members:
                        oy, ox = crop.box[1] - gy0, crop.box[0] - gx0
                        crop.labels = np.asarray(labels[oy:oy + side, ox:ox + side])
                _mask_statistics(crops.values(), side)
    return crops, {"reads": reads, "groups": len(groups), "level": int(level),
                   "side_level_px": side, "crop_px": float(crop_px), "mask": mask_status}


def _mask_statistics(crops, side):
    """Per crop: the target's mask area (level px) and how far the mask's own
    centroid sits from the table's -- more than ~5 px suggests the table and
    the mask disagree (registration, or a mislabelled id)."""
    from plexora.server.utils.label_kernels import label_bboxes

    centre = (side - 1) / 2
    for crop in crops:
        if crop.labels is None:
            continue
        stats = label_bboxes(crop.labels.astype(np.uint32), ids=[crop.cell_id])
        count = int(stats["count"][0])
        if count:
            offset = float(math.hypot(stats["cy"][0] - centre, stats["cx"][0] - centre))
        else:
            offset = None
        crop.mask_stats = {"area_level_px": count, "centroid_offset_px": offset}


def blend(crop, layers, size):
    """An RGB uint8 (size, size) panel from a crop's planes.

    `layers` is a list of `{name, window, color}` (`color` #rrggbb); blended
    with the viewer's arithmetic, then resampled (LANCZOS) to `size`.
    """
    from PIL import Image

    from plexora.server.utils import source_image

    side = next(iter(crop.planes.values())).shape[0] if crop.planes else size
    accumulator = np.zeros((side, side, 3), dtype=np.float32)
    for layer in layers:
        plane = crop.planes.get(layer["name"])
        if plane is None:
            continue
        colour = layer["color"].lstrip("#")
        rgb = {"r": int(colour[0:2], 16), "g": int(colour[2:4], 16), "b": int(colour[4:6], 16)}
        source_image.blend_plane(accumulator, plane, layer["window"], rgb)
    pixels = source_image.finish(accumulator)
    image = Image.fromarray(pixels, "RGB")
    if image.size != (size, size):
        image = image.resize((size, size), Image.LANCZOS)
    return np.array(image)


def log_window_panel(crop, name, gate, size, *, to_log=True, half_width=None):
    """A grey panel whose mid-grey IS the gate: log-scaled across [gate/4,
    gate*4] (raw intensities), or linear across gate +/- half_width.

    Not the viewer's arithmetic, on purpose: it is an analysis panel, and says
    so in the manifest. Whatever the display window, a cell brighter than the
    gate is brighter than mid-grey here.
    """
    from PIL import Image

    plane = crop.planes.get(name)
    if plane is None:
        return np.zeros((size, size, 3), dtype=np.uint8)
    if to_log and gate > 0:
        lo, hi = math.log(gate / 4.0), math.log(gate * 4.0)
        t = (np.log(np.maximum(plane, 1e-6)) - lo) / (hi - lo)
    else:
        width = half_width or max(abs(gate) * 0.5, 1.0)
        t = (plane - (gate - width)) / (2 * width)
    grey = (np.clip(t, 0, 1) * 255).astype(np.uint8)
    image = Image.fromarray(grey, "L").convert("RGB")
    if image.size != (size, size):
        image = image.resize((size, size), Image.LANCZOS)
    return np.array(image)


#: The judged cell's outline in every panel (and in the packet's legend).
OUTLINE_COLOR = "#ff3df2"
NEIGHBOUR_OUTLINE_COLOR = "#8a8f99"


def outline(panel, crop, *, target=OUTLINE_COLOR, neighbours=NEIGHBOUR_OUTLINE_COLOR,
            thickness=1):
    """The target cell's outline (and dim neighbours) over a panel, in place."""
    from plexora.server.utils.label_overlay import paint_labels, resize_labels_nearest

    if crop.labels is None:
        return panel
    labels = resize_labels_nearest(crop.labels, panel.shape[1], panel.shape[0])

    def colour_for(label):
        return target if label == crop.cell_id else neighbours

    paint_labels(panel, labels, mode="outlines", colour_for=colour_for, alpha=1.0,
                 thickness=thickness)
    return panel
