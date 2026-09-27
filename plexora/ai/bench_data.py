"""Synthetic scenes with known truth, for benchmarking automatic gating.

Round cells on a grid, a pyramid, a mask and a cell table that agree, with
markers whose biology is known (CD3 on T cells, CD8 on a subset, CD20 on B
cells) and a catalogue of problems that can be switched on one at a time:
overlapping populations, a rare population, a flat antibody, a staining
gradient, saturation, a whole-image staining shift. The truth -- which cells
are positive for which marker -- is by phenotype, whatever the pixels or the
problem do to the values.

Registered straight into the data root's `config.json`, the way the test
suite's fixtures register theirs, so a benchmark does not wait on the import
pipeline.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np

PHENOTYPES = ("cd8_t", "cd4_t", "b_cell", "other")

#: log1p mean per phenotype (cd8_t, cd4_t, b_cell, other).
LEVELS = {
    "CD3": (7.2, 7.0, 4.2, 4.0),
    "CD8": (7.4, 4.1, 4.0, 4.2),
    "CD20": (4.0, 4.1, 7.3, 4.0),
    "CD4": (4.05, 5.2, 4.05, 4.05),
    "FOXP3": (4.0, 4.0, 4.0, 4.0),
}
SD = {"CD4": 0.35, "FOXP3": 0.3}
TREG_LEVEL = 6.3
BACKGROUND = 40.0

#: The problems a scenario can carry.
SCENARIOS = {
    "easy": {},
    "overlap": {"sd": {"CD4": 0.5}, "levels": {"CD4": (4.05, 4.95, 4.05, 4.05)}},
    "rare": {},
    "flat": {"flat": ("CD20",)},
    "gradient": {"gradient": ("CD8",)},
    "saturated": {"saturated": ("CD3",)},
    "shifted": {"shift": 0.5},
}


def phenotype(row, column):
    return PHENOTYPES[(row * 7 + column * 3) % 4]


def is_treg(row, column, kind):
    return kind == "cd4_t" and (row * 31 + column * 17) % 7 == 0


def truth_positive(marker, cell, scenario):
    if marker in SCENARIOS[scenario].get("flat", ()):
        return False
    if marker == "FOXP3":
        return bool(cell["treg"])
    levels = SCENARIOS[scenario].get("levels", {}).get(marker, LEVELS[marker])
    return levels[PHENOTYPES.index(cell["kind"])] > 4.5


def scene(*, grid=24, size=1024, seed=0, markers=("CD3", "CD8", "CD20", "CD4", "FOXP3"),
          scenario="easy"):
    spec = SCENARIOS[scenario]
    rng = np.random.default_rng(seed)
    channels = ("DNA",) + tuple(markers)
    spacing = size / grid
    radius = max(4, int(spacing * 0.38))
    image = np.full((len(channels), size, size), BACKGROUND, dtype=np.float32)
    image += rng.normal(0, 3, size=image.shape).astype(np.float32)
    labels = _disc_labels(size, grid, spacing, radius)
    cells = []
    label = 0
    per_label = np.zeros((len(channels), grid * grid + 1), dtype=np.float32)
    for row in range(grid):
        for column in range(grid):
            label += 1
            cx, cy = (column + 0.5) * spacing, (row + 0.5) * spacing
            kind = phenotype(row, column)
            treg = is_treg(row, column, kind)
            values = {}
            for name in markers:
                levels = spec.get("levels", {}).get(name, LEVELS[name])
                mean = levels[PHENOTYPES.index(kind)]
                if name == "FOXP3" and treg:
                    mean = TREG_LEVEL
                if name in spec.get("flat", ()):
                    mean = 4.0
                if name in spec.get("gradient", ()) and mean < 6:
                    mean += 1.2 * cx / size
                mean += spec.get("shift", 0.0)
                sd = spec.get("sd", {}).get(name, SD.get(name, 0.18))
                value = float(np.expm1(rng.normal(mean, sd)))
                if name in spec.get("saturated", ()) and mean > 6:
                    value = 65535.0
                values[name] = min(value, 65535.0)
            per_label[0, label] = float(3000 * rng.uniform(0.9, 1.1))
            for index, name in enumerate(markers, start=1):
                per_label[index, label] = values[name]
            cells.append({"id": label, "x": float(cx), "y": float(cy), "kind": kind,
                          "treg": treg, **values})
    inside = labels > 0
    for index in range(len(channels)):
        image[index][inside] = per_label[index][labels[inside]]
    return np.clip(image, 0, 65535).astype(np.uint16), labels, cells, channels


def _disc_labels(size, grid, spacing, radius):
    """A disc of radius `radius` at the centre of every grid cell, labelled
    row-major from 1 -- one pass over the pixels, not one per cell."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    column = np.minimum((xx // spacing).astype(np.int64), grid - 1)
    row = np.minimum((yy // spacing).astype(np.int64), grid - 1)
    cx, cy = (column + 0.5) * spacing, (row + 0.5) * spacing
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius ** 2
    return np.where(inside, row * grid + column + 1, 0).astype(np.uint32)


def _pyramid(path, plane, *, levels=3, tile=(128, 128), pixel_size=None, ome=True):
    import tifffile

    metadata = {"axes": "CYX" if plane.ndim == 3 else "YX"}
    if pixel_size:
        metadata.update({"PhysicalSizeX": pixel_size, "PhysicalSizeXUnit": "µm",
                         "PhysicalSizeY": pixel_size, "PhysicalSizeYUnit": "µm"})
    with tifffile.TiffWriter(path, ome=ome) as handle:
        handle.write(plane, subifds=levels - 1, tile=tile, metadata=metadata)
        for level in range(1, levels):
            factor = 2 ** level
            handle.write(plane[..., ::factor, ::factor], tile=tile, subfiletype=1)
    return path


def register(data_root, name, *, scenario="easy", grid=24, size=1024, seed=0,
             markers=("CD3", "CD8", "CD20", "CD4", "FOXP3")) -> dict:
    """Write a scenario's files and register it as a project; returns the truth."""
    from plexora.server.models.project import (ROLE_NAMES, ColumnGroups, ColumnRoles,
                                               DataSpec, ImageSpec, Project,
                                               SegmentationSpec)

    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells, channels = scene(grid=grid, size=size, seed=seed, markers=markers,
                                           scenario=scenario)
    image_path = _pyramid(folder / "image.ome.tif", image, pixel_size=0.5)
    mask_path = _pyramid(folder / "mask.tif", labels, ome=False)
    areas = np.bincount(labels.ravel(), minlength=len(cells) + 1)
    lines = [",".join(["CellID", "X_centroid", "Y_centroid", "Area", "DNA", *markers])]
    for cell in cells:
        lines.append(",".join([str(cell["id"]), f"{cell['x']:.3f}", f"{cell['y']:.3f}",
                               str(int(areas[cell["id"]])), "3000"]
                              + [f"{cell[m]:.4f}" for m in markers]))
    csv_path = folder / "cells.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dataset = DataSpec(type="csv", src=str(csv_path),
                       roles=ColumnRoles(cell_id="CellID", x="X_centroid", y="Y_centroid"),
                       columns=ColumnGroups(markers=("DNA", *markers), metadata=("Area",)),
                       single_image=True)
    image_spec = ImageSpec(
        src=str(image_path), kind="ome_tiff",
        channels=tuple({"name": c, "fullname": c, "src": f"/generated/data/x/{c}/"}
                       for c in channels),
        width=size, height=size, max_level=2, tile_width=128, tile_height=128,
        num_channels=len(channels))
    confirmed = ("table", "segmentation", "markers", "features") + tuple(
        f"role:{role}" for role in ROLE_NAMES)
    record = Project(name=name, image=image_spec,
                     segmentation=SegmentationSpec(derived=str(mask_path),
                                                   source=str(mask_path)),
                     dataset=dataset, confirmed=confirmed)
    record = dataclasses.replace(record)
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    truth = {m: np.array([truth_positive(m, c, scenario) for c in cells]) for m in markers}
    values = {m: np.array([c[m] for c in cells], dtype=np.float32) for m in markers}
    return {"name": name, "scenario": scenario, "truth": truth, "values": values,
            "ids": np.array([c["id"] for c in cells])}
