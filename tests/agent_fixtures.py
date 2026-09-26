"""A small, fully known project for the agent layer's tests.

Every test of plexora/agent and plexora/mcp needs the same thing: a real image
pyramid, a real label mask and a real cell table that agree with each other, so
that a rendered region can be checked cell by cell and a gate can be checked
against the pixels it claims to describe. Built here once.

The scene is a grid of round cells on a dark background. Every cell has a DNA
signal; CD8 splits them into three known groups -- clearly negative, clearly
positive, and a borderline group sitting near where a gate belongs -- and the
table's CD8 value for each cell IS the intensity painted into that cell, so
"which cells are positive" has one answer in the pixels and in the table.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np

from tests.helpers import ALL_CONFIRMED, csv_spec, image_spec, project

#: Channel names, in pyramid order.
CHANNELS = ("DNA", "CD8")

NEGATIVE = 150.0
BORDERLINE = 700.0
POSITIVE = 4000.0
DNA_LEVEL = 3000.0
BACKGROUND = 40.0

#: Microns per pixel when the image is calibrated.
PIXEL_SIZE_UM = 0.5


def blob_scene(seed=0, size=512, grid=8, radius=18):
    """(image (2, size, size) uint16, labels (size, size) uint32, cells).

    `cells` is a list of {id, x, y, group, cd8, dna}. Groups cycle
    negative/positive/borderline by position, so every region of the image has
    some of each.
    """
    rng = np.random.default_rng(seed)
    image = np.full((len(CHANNELS), size, size), BACKGROUND, dtype=np.float32)
    image += rng.normal(0, 4, size=image.shape).astype(np.float32)
    labels = np.zeros((size, size), dtype=np.uint32)
    yy, xx = np.mgrid[0:size, 0:size]
    spacing = size / grid
    groups = ("negative", "positive", "borderline")
    cells = []
    label = 0
    for row in range(grid):
        for column in range(grid):
            label += 1
            cx = (column + 0.5) * spacing
            cy = (row + 0.5) * spacing
            group = groups[(row + column) % 3]
            base = {"negative": NEGATIVE, "positive": POSITIVE,
                    "borderline": BORDERLINE}[group]
            cd8 = float(base * rng.uniform(0.85, 1.15))
            dna = float(DNA_LEVEL * rng.uniform(0.9, 1.1))
            inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius ** 2
            labels[inside] = label
            image[0][inside] = dna
            image[1][inside] = cd8
            cells.append({"id": label, "x": float(cx), "y": float(cy), "group": group,
                          "cd8": cd8, "dna": dna})
    return np.clip(image, 0, 65535).astype(np.uint16), labels, cells


def _write_pyramid(path, plane, *, levels=3, tile=(128, 128), pixel_size=None, ome=True):
    import tifffile

    metadata = {"axes": "CYX" if plane.ndim == 3 else "YX"}
    if pixel_size:
        metadata.update({"PhysicalSizeX": pixel_size, "PhysicalSizeXUnit": "µm",
                         "PhysicalSizeY": pixel_size, "PhysicalSizeYUnit": "µm"})
    with tifffile.TiffWriter(path, ome=ome) as handle:
        handle.write(plane, subifds=levels - 1, tile=tile, metadata=metadata)
        for level in range(1, levels):
            factor = 2 ** level
            reduced = plane[..., ::factor, ::factor]
            handle.write(reduced, tile=tile, subfiletype=1)
    return path


def write_image_pyramid(path, image, *, calibrated=True):
    return _write_pyramid(path, image,
                          pixel_size=PIXEL_SIZE_UM if calibrated else None)


def write_mask_pyramid(path, labels):
    return _write_pyramid(path, labels, ome=False)


def write_table(path, cells):
    lines = ["CellID,X_centroid,Y_centroid,DNA,CD8"]
    for cell in cells:
        lines.append(f"{cell['id']},{cell['x']:.3f},{cell['y']:.3f},"
                     f"{cell['dna']:.4f},{cell['cd8']:.4f}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_synthetic_project(data_root, name="synth", *, calibrated=True, mask=True,
                           table=True, seed=0, size=512):
    """Register one synthetic project under `data_root`; returns what was made.

    Written straight into `config.json`, the way the Figure Builder tests
    register theirs, so nothing here depends on the import pipeline.
    """
    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells = blob_scene(seed=seed, size=size)
    image_path = write_image_pyramid(folder / "image.ome.tif", image, calibrated=calibrated)
    mask_path = write_mask_pyramid(folder / "mask.tif", labels) if mask else None
    csv_path = write_table(folder / "cells.csv", cells) if table else None

    dataset = csv_spec(csv_path, markers=CHANNELS, metadata=()) if table else None
    record = project(
        name,
        dataset=dataset,
        segmentation=str(mask_path) if mask else None,
        image=image_spec(channels=CHANNELS, width=size, height=size, src=str(image_path)),
        confirmed=ALL_CONFIRMED,
    )
    # Three levels, tiled at 128: what the pyramid written above really holds.
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=2, tile_width=128, tile_height=128))
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return {
        "name": name,
        "cells": cells,
        "labels": labels,
        "image": image,
        "positives": [c["id"] for c in cells if c["group"] == "positive"],
        "negatives": [c["id"] for c in cells if c["group"] == "negative"],
        "borderline": [c["id"] for c in cells if c["group"] == "borderline"],
        "image_path": str(image_path),
        "mask_path": str(mask_path) if mask_path else None,
        "csv_path": str(csv_path) if csv_path else None,
    }


class _DictCache:
    """`ProjectData._cache` for a test: a plain dict with the one method."""

    def __init__(self):
        self.entries = {}

    def get_or_set(self, key, compute):
        if key not in self.entries:
            self.entries[key] = compute()
        return self.entries[key]


def local_handles(name):
    """Provider-backed handles for a registered project -- the arrangement an
    agent session makes, without the session. Reads never touch data_model."""
    from plexora.api.dataset import _project_data_for
    from plexora.server.models.project import Project
    from plexora.server.providers.local import LocalTableProvider

    record = Project.load(name)
    provider = None
    if record.has_table:
        provider = LocalTableProvider(record.dataset, name)
        provider.load()
    return _project_data_for(record, provider, cache=_DictCache())
