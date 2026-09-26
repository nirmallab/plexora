"""Known truth for automatic gating: populations, a fake handle set, a scene.

Two levels, because the two halves of automatic gating need different things:

- **Table only** (`FakeData`, `populations`): the profile, the sampler, the
  candidate generator and the reference model are functions of columns and
  positions. A FakeData over hundreds of thousands of synthetic cells tests
  them at realistic scale in milliseconds, with no file on disk.
- **A real project** (`make_gating_project`): a pyramid, a mask and a table
  that agree, with several markers whose biology is known -- CD3 on T cells,
  CD8 on a subset of them, CD20 on B cells and never on T cells -- so a
  collage, a bivariate check and a whole gating session can be checked
  against the truth painted into the pixels.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl

from tests.agent_fixtures import _write_pyramid
from tests.helpers import ALL_CONFIRMED, csv_spec, image_spec, project


# -- table only ----------------------------------------------------------------


def populations(n, fraction, *, bg=(4.0, 0.5), pos=(7.0, 0.4), seed=0):
    """(raw values, truth) -- a log-normal background and a log-normal positive
    population, `fraction` of the cells positive. Means and sds in log1p units."""
    rng = np.random.default_rng(seed)
    truth = rng.random(n) < fraction
    logs = np.where(truth, rng.normal(pos[0], pos[1], n), rng.normal(bg[0], bg[1], n))
    return np.expm1(np.clip(logs, 0, None)).astype(np.float32), truth


class _Cache:
    def __init__(self):
        self.entries = {}

    def get_or_set(self, key, compute):
        if key not in self.entries:
            self.entries[key] = compute()
        return self.entries[key]


class FakeTable:
    def __init__(self, columns, frame, log_transformed=False, metadata=()):
        self._columns = columns
        self._frame = frame
        self.log_transformed = log_transformed
        self.markers = [name for name in columns if name not in metadata]
        self.metadata_columns = list(metadata)
        self.is_local = True

    def columns(self, names):
        return {name: self._columns[name] for name in names}

    def geometry(self):
        return self._frame

    def describe(self):
        out = {}
        for name, values in self._columns.items():
            finite = values[np.isfinite(values)]
            out[name] = {"min": float(finite.min()), "max": float(finite.max()),
                         "count": int(finite.size)}
        return out


class FakeData:
    """Just enough of `ProjectData` for the table-side of automatic gating."""

    def __init__(self, columns, *, xs=None, ys=None, name="fake", log_transformed=False,
                 metadata=(), seed=0, extent=10_000.0):
        from plexora.api.dataset import DatasetSchema

        n = len(next(iter(columns.values())))
        rng = np.random.default_rng(seed + 99)
        xs = rng.uniform(0, extent, n) if xs is None else np.asarray(xs, dtype=np.float64)
        ys = rng.uniform(0, extent, n) if ys is None else np.asarray(ys, dtype=np.float64)
        frame = pl.DataFrame({"id": np.arange(1, n + 1), "CellID": np.arange(1, n + 1),
                              "X": xs, "Y": ys})
        columns = {k: np.asarray(v, dtype=np.float32) for k, v in columns.items()}
        self.name = name
        self.table = FakeTable(columns, frame, log_transformed, metadata)
        self.schema = DatasetSchema(cell_id="CellID", x="X", y="Y")
        self.project = SimpleNamespace(
            name=name, image=SimpleNamespace(pixel_size=None, is_blank=True, src=None),
            resources={})
        self._cache = _Cache()

    def cached(self, key, compute):
        return self._cache.get_or_set((self.name, key), compute)


# -- a real project ------------------------------------------------------------

#: Phenotypes by position, and what each marker reads in each (log1p means).
PHENOTYPES = ("cd8_t", "cd4_t", "b_cell", "other")
MARKER_LEVELS = {
    #          cd8_t  cd4_t  b_cell other
    "CD3":   (7.2,   7.0,   4.2,   4.0),
    "CD8":   (7.4,   4.1,   4.0,   4.2),
    "CD20":  (4.0,   4.1,   7.3,   4.0),
    # Hard on purpose: CD4's populations overlap (D about 2); FOXP3 is on one
    # CD4 T cell in eight (~3% of cells), too few for the mixture to see.
    "CD4":   (4.05,  4.95,  4.05,  4.05),
    "FOXP3": (4.0,   4.0,   4.0,   4.0),
}
#: Per-cell spread (log1p sd) per marker; 0.18 unless named.
MARKER_SD = {"CD4": 0.4, "FOXP3": 0.3}
FOXP3_POSITIVE = 6.3
BACKGROUND = 40.0


def _phenotype(row, column):
    return PHENOTYPES[(row * 7 + column * 3) % 4]


def _is_treg(row, column, kind):
    return kind == "cd4_t" and (row * 31 + column * 17) % 7 == 0


def truth_level(marker, cell):
    """The mean a cell was painted at (log1p) -- what 'positive' means here."""
    if marker == "FOXP3":
        return FOXP3_POSITIVE if cell.get("treg") else 4.0
    return MARKER_LEVELS[marker][PHENOTYPES.index(cell["kind"])]


def gating_scene(*, grid=16, size=768, radius=None, seed=0, markers=("CD3", "CD8", "CD20"),
                 variant=None, shift=0.0):
    """(image (C, size, size) uint16, labels, cells, channels).

    Round cells on a grid, each painted with its markers' values. `variant`
    puts a known technical problem into the pixels AND the table:
    `gradient` (CD8 background rises left to right), `flat` (CD20 carries no
    signal at all), `saturated` (CD3 positives clipped at 65535).
    """
    rng = np.random.default_rng(seed)
    channels = ("DNA",) + tuple(markers)
    spacing = size / grid
    radius = radius or max(4, int(spacing * 0.38))
    image = np.full((len(channels), size, size), BACKGROUND, dtype=np.float32)
    image += rng.normal(0, 3, size=image.shape).astype(np.float32)
    labels = np.zeros((size, size), dtype=np.uint32)
    yy, xx = np.mgrid[0:size, 0:size]
    cells = []
    label = 0
    for row in range(grid):
        for column in range(grid):
            label += 1
            cx, cy = (column + 0.5) * spacing, (row + 0.5) * spacing
            kind = _phenotype(row, column)
            values = {}
            for name in markers:
                mean = MARKER_LEVELS[name][PHENOTYPES.index(kind)]
                if name == "FOXP3" and _is_treg(row, column, kind):
                    mean = FOXP3_POSITIVE
                if variant == "gradient" and name == "CD8" and mean < 6:
                    mean += 1.2 * cx / size
                if variant == "flat" and name == "CD20":
                    mean = 4.0
                # A staining/exposure shift of the whole image, in log units.
                mean += shift
                value = float(np.expm1(rng.normal(mean, MARKER_SD.get(name, 0.18))))
                if variant == "saturated" and name == "CD3" and mean > 6:
                    value = 65535.0
                values[name] = min(value, 65535.0)
            dna = float(3000 * rng.uniform(0.9, 1.1))
            inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius ** 2
            labels[inside] = label
            image[0][inside] = dna
            for index, name in enumerate(markers, start=1):
                image[index][inside] = values[name]
            cells.append({"id": label, "x": float(cx), "y": float(cy), "kind": kind,
                          "dna": dna, "treg": _is_treg(row, column, kind), **values})
    return np.clip(image, 0, 65535).astype(np.uint16), labels, cells, channels


def make_gating_project(data_root, name="gsynth", *, grid=16, size=768, seed=0,
                        markers=("CD3", "CD8", "CD20"), variant=None, calibrated=True,
                        mask=True, shift=0.0):
    """Register a multi-marker synthetic project; returns what was made."""
    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells, channels = gating_scene(grid=grid, size=size, seed=seed,
                                                  markers=markers, variant=variant,
                                                  shift=shift)
    image_path = _write_pyramid(folder / "image.ome.tif", image,
                                pixel_size=0.5 if calibrated else None)
    mask_path = _write_pyramid(folder / "mask.tif", labels, ome=False) if mask else None
    header = ["CellID", "X_centroid", "Y_centroid", "Area", "DNA", *markers]
    lines = [",".join(header)]
    areas = np.bincount(labels.ravel(), minlength=len(cells) + 1)
    for cell in cells:
        row = [str(cell["id"]), f"{cell['x']:.3f}", f"{cell['y']:.3f}",
               str(int(areas[cell["id"]])), f"{cell['dna']:.4f}"]
        row += [f"{cell[m]:.4f}" for m in markers]
        lines.append(",".join(row))
    csv_path = folder / "cells.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dataset = csv_spec(csv_path, markers=("DNA", *markers), metadata=("Area",))
    record = project(name, dataset=dataset,
                     segmentation=str(mask_path) if mask_path else None,
                     image=image_spec(channels=channels, width=size, height=size,
                                      src=str(image_path)),
                     confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=2, tile_width=128, tile_height=128))
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    truth = {m: [c["id"] for c in cells if truth_level(m, c) > (4.5 if m == "CD4" else 6)]
             for m in markers}
    return {"name": name, "cells": cells, "labels": labels, "image": image,
            "channels": channels, "truth": truth, "image_path": str(image_path)}


def make_gating_dataset(data_root, name="cohort", *, shifts=(0.0, 0.0, 0.4), grid=16,
                        size=768, markers=("CD3", "CD8", "CD20")):
    """Several synthetic images in one dataset; returns {project: info}."""
    from plexora.server.models import datasets as registry

    made = {}
    for index, shift in enumerate(shifts):
        project_name = f"{name}_{index + 1}"
        made[project_name] = make_gating_project(
            data_root, project_name, grid=grid, size=size, seed=index + 1, markers=markers,
            shift=shift)
    registry.create(name, projects=list(made), known=list(made))
    return made
