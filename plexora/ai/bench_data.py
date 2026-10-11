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

The spatial scenes (`spatial_scene`, `SPATIAL_SCENARIOS`) are a second,
separate path -- the scenes above keep their random streams byte for byte:
a dense and a sparse region, membrane markers drawn as rings, and spill-over,
local background and dim positives painted into the pixels, with the per-cell
cause kept as truth (`cell_truth`).
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


# -- spatial scenes -----------------------------------------------------------------
#
# The problems a threshold cannot fix, painted into the pixels so the table's
# value is what a mask mean reads there: a dense region (left) and a sparse one
# (right) of jittered round cells; membrane markers drawn as rings at the
# cell's edge, nuclear ones as a disc; spill-over as the positives' rings
# blurred into their neighbours (close neighbours catch more of it); a local
# background (a bump or a plateau) added in raw units; and dim positives,
# painted half-way between background and positive. The truth stays the
# phenotype's: a dim positive is positive, a contaminated negative negative.

#: Markers drawn as a ring at the cell's edge; the rest fill the nucleus.
MEMBRANE = ("CD3", "CD8", "CD20", "CD4")

#: How common each phenotype is in a spatial scene (`PHENOTYPES` order): T
#: cells a quarter of the tissue, scattered -- so a negative beside one is
#: not the rule everywhere, and spill-over is not mistaken for clustering.
SPATIAL_MIX = (0.12, 0.13, 0.15, 0.60)

#: Each spatial scenario: `spill` {marker: fraction of the blurred positive
#: rings added}, `background` {marker: (kind, amplitude x the negatives'
#: level)}, `dim` {marker: share of true positives painted dim}; `mask`,
#: `calibrated`, `blank` switch the evidence the session gets.
SPATIAL_SCENARIOS = {
    "dense_spill": {"spill": {"CD3": 6.0}, "dim": {"CD3": 0.25}},
    "low_snr": {"background": {"CD4": ("bump", 2.5)}},
    "dim_positive": {"dim": {"CD8": 0.35}},
    "heterogeneous": {"spill": {"CD3": 6.0}, "background": {"CD4": ("plateau", 1.8)},
                      "dim": {"CD8": 0.35}},
    "dense_spill_nomask": {"spill": {"CD3": 6.0}, "dim": {"CD3": 0.25}, "mask": False},
    "dense_spill_uncal": {"spill": {"CD3": 6.0}, "dim": {"CD3": 0.25}, "calibrated": False},
    "graph_only": {"spill": {"CD3": 6.0}, "dim": {"CD3": 0.25}, "blank": True},
}


def _spatial_layout(size, rng, *, dense_per_side=36):
    """Cell centres: the left half on a grid of spacing size/dense_per_side,
    the right half at twice that, each jittered by up to a tenth of its
    spacing; ids row-major, dense region first."""
    spacing = size / dense_per_side
    centres, regions = [], []
    for region, step, x0, x1 in (("dense", spacing, 0.0, size / 2.0),
                                 ("sparse", 2 * spacing, size / 2.0, float(size))):
        ys = np.arange(step / 2, size, step)
        xs = np.arange(x0 + step / 2, x1, step)
        for y in ys:
            for x in xs:
                jx, jy = rng.uniform(-0.1, 0.1, 2) * step
                centres.append((float(x + jx), float(y + jy)))
                regions.append(region)
    return np.array(centres), np.array(regions), spacing


def _spatial_labels(size, centres, radius):
    """Disc labels (1-based, in `centres` order) and each pixel's distance from
    its own cell's centre (inf off the cells)."""
    labels = np.zeros((size, size), dtype=np.uint32)
    distance = np.full((size, size), np.inf, dtype=np.float32)
    r = int(np.ceil(radius))
    for index, (cx, cy) in enumerate(centres, start=1):
        x0, x1 = max(0, int(cx) - r - 1), min(size, int(cx) + r + 2)
        y0, y1 = max(0, int(cy) - r - 1), min(size, int(cy) + r + 2)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        d = np.hypot(xx - cx, yy - cy).astype(np.float32)
        take = (d <= radius) & (d < distance[y0:y1, x0:x1])
        labels[y0:y1, x0:x1][take] = index
        distance[y0:y1, x0:x1][take] = d[take]
    return labels, distance


def _background_field(size, kind, amplitude, level):
    """An additive background (raw units): a Gaussian bump centred in the
    dense half's lower quadrant and the sparse half's upper one, or a plateau
    over the lower-right quadrant."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    if kind == "plateau":
        return np.where((xx >= size / 2) & (yy >= size / 2), amplitude * level, 0.0) \
            .astype(np.float32)
    sigma = size / 8.0
    field = np.zeros((size, size), dtype=np.float32)
    for cx, cy in ((size * 0.25, size * 0.75), (size * 0.75, size * 0.25)):
        field += np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma * sigma))
    return (amplitude * level * field).astype(np.float32)


def spatial_scene(*, scenario="dense_spill", size=1024, seed=0,
                  markers=("CD3", "CD8", "CD20", "CD4", "FOXP3"), dense_per_side=36):
    """(image (C, size, size) uint16, labels, cells, channels, painted) for a
    spatial scenario. `cells[i][marker]` is the per-label mean of the painted
    plane -- what a mask mean reads; `painted` holds the truth by cause."""
    spec = SPATIAL_SCENARIOS[scenario]
    rng = np.random.default_rng(seed)
    centres, regions, spacing = _spatial_layout(size, rng, dense_per_side=dense_per_side)
    radius = 0.4 * spacing
    labels, distance = _spatial_labels(size, centres, radius)
    n = len(centres)
    kinds = np.array(PHENOTYPES)[rng.choice(len(PHENOTYPES), n, p=SPATIAL_MIX)]
    tregs = (kinds == "cd4_t") & (rng.random(n) < 1 / 7)
    channels = ("DNA",) + tuple(markers)
    image = np.full((len(channels), size, size), BACKGROUND, dtype=np.float32)
    image += rng.normal(0, 3, size=image.shape).astype(np.float32)
    inside = labels > 0
    owner = labels.astype(np.int64) - 1
    ring = inside & (distance > 0.75 * radius)
    nucleus = inside & (distance <= 0.6 * radius)
    image[0][nucleus] = (3000 * rng.uniform(0.9, 1.1, n))[owner[nucleus]]
    painted = {"dim": {}, "contaminated": {}, "background": {}}
    values = {}
    background_region = np.full(n, "flat", dtype=object)
    for index, name in enumerate(markers, start=1):
        truth = np.array([truth_positive(name, {"kind": k, "treg": t}, "easy")
                          for k, t in zip(kinds, tregs)])
        levels = np.array([LEVELS[name][PHENOTYPES.index(k)] for k in kinds])
        if name == "FOXP3":
            levels = np.where(tregs, TREG_LEVEL, 4.0)
        cell_level = np.expm1(rng.normal(levels, SD.get(name, 0.18)))
        negative_level = float(np.expm1(4.0))
        dim = np.zeros(n, dtype=bool)
        share = (spec.get("dim") or {}).get(name)
        if share:
            # Half-way between background and the cell's own level, in log
            # units: a real stain the threshold sits on top of.
            dim = truth & (rng.random(n) < share)
            cell_level = np.where(dim, np.expm1(0.5 * (np.log1p(negative_level)
                                                       + np.log1p(cell_level))), cell_level)
        plane = image[index]
        where = ring if name in MEMBRANE else nucleus
        plane[inside] = negative_level
        plane[where] = cell_level[owner[where]]
        fraction = (spec.get("spill") or {}).get(name)
        clean_mean = None
        if fraction:
            from scipy.ndimage import gaussian_filter

            clean_mean = _label_means(plane, labels, n)
            light = np.where(where & truth[np.maximum(owner, 0)], plane - negative_level, 0.0)
            plane += float(fraction) * gaussian_filter(light.astype(np.float32),
                                                       sigma=0.25 * spacing)
        bg = (spec.get("background") or {}).get(name)
        if bg:
            field = _background_field(size, bg[0], float(bg[1]), negative_level)
            plane += field
            raised = _label_means(field, labels, n) > 0.25 * negative_level
            background_region = np.where(raised, bg[0], background_region)
            painted["background"][name] = raised
        means = _label_means(plane, labels, n)
        if clean_mean is not None:
            painted["contaminated"][name] = ~truth & (means / np.maximum(clean_mean, 1e-6)
                                                      > 1.15)
        painted["dim"][name] = dim
        values[name] = means.astype(np.float32)
    cells = []
    for i in range(n):
        cells.append({"id": i + 1, "x": float(centres[i, 0]), "y": float(centres[i, 1]),
                      "kind": str(kinds[i]), "treg": bool(tregs[i]),
                      "region": str(regions[i]), "background": str(background_region[i]),
                      **{m: float(values[m][i]) for m in markers}})
    return (np.clip(image, 0, 65535).astype(np.uint16), labels, cells, channels,
            {**painted, "regions": regions, "background_region": background_region})


def _label_means(plane, labels, n):
    flat = labels.ravel()
    sums = np.bincount(flat, weights=plane.ravel().astype(np.float64), minlength=n + 1)
    counts = np.bincount(flat, minlength=n + 1)
    return (sums / np.maximum(counts, 1))[1:]


def cell_truth_of(scenario, cells, painted, markers):
    """{marker: {positive, dim, contaminated, density_region, background_region,
    cause}} -- per-cell arrays."""
    out = {}
    regions = np.asarray(painted["regions"])
    background_region = np.asarray(painted["background_region"]).astype(str)
    for name in markers:
        positive = np.array([truth_positive(name, c, "easy") for c in cells])
        dim = np.asarray(painted["dim"].get(name, np.zeros(len(cells), bool)))
        contaminated = np.asarray(painted["contaminated"].get(name,
                                                              np.zeros(len(cells), bool)))
        raised = np.asarray(painted["background"].get(name, np.zeros(len(cells), bool)))
        cause = np.where(contaminated, "spill", np.where(raised, "background",
                                                         np.where(dim, "dim", "none")))
        out[name] = {"positive": positive, "dim": dim, "contaminated": contaminated,
                     "density_region": regions, "background_region":
                     np.where(raised, background_region, "flat"),
                     "cause": cause}
    return out


def register_spatial(data_root, name, *, scenario="dense_spill", size=1024, seed=0,
                     markers=("CD3", "CD8", "CD20", "CD4", "FOXP3"), mask=None,
                     calibrated=None) -> dict:
    """`register` for a spatial scenario: its files and project, the truth
    by phenotype, and the per-cell truth by cause (`cell_truth`). The
    scenario's `mask` / `calibrated` / `blank` apply unless overridden."""
    from plexora.server.models.project import (IMAGE_KIND_BLANK, ROLE_NAMES, ColumnGroups,
                                               ColumnRoles, DataSpec, ImageSpec, Project,
                                               SegmentationSpec)

    spec = SPATIAL_SCENARIOS[scenario]
    mask = spec.get("mask", True) if mask is None else mask
    calibrated = spec.get("calibrated", True) if calibrated is None else calibrated
    blank = bool(spec.get("blank"))
    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells, channels, painted = spatial_scene(scenario=scenario, size=size,
                                                            seed=seed, markers=markers)
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
    if blank:
        image_spec = ImageSpec(src=None, kind=IMAGE_KIND_BLANK, channels=(), width=size,
                               height=size, max_level=2, tile_width=128, tile_height=128,
                               num_channels=0, modality="blank",
                               pixel_size={"value": 0.5, "unit": "µm", "source": "metadata"}
                               if calibrated else None)
        segmentation = SegmentationSpec()
    else:
        image_path = _pyramid(folder / "image.ome.tif", image,
                              pixel_size=0.5 if calibrated else None)
        image_spec = ImageSpec(
            src=str(image_path), kind="ome_tiff",
            channels=tuple({"name": c, "fullname": c, "src": f"/generated/data/x/{c}/"}
                           for c in channels),
            width=size, height=size, max_level=2, tile_width=128, tile_height=128,
            num_channels=len(channels))
        if mask:
            mask_path = _pyramid(folder / "mask.tif", labels, ome=False)
            segmentation = SegmentationSpec(derived=str(mask_path), source=str(mask_path))
        else:
            segmentation = SegmentationSpec()
    confirmed = ("table", "segmentation", "markers", "features") + tuple(
        f"role:{role}" for role in ROLE_NAMES)
    record = Project(name=name, image=image_spec, segmentation=segmentation, dataset=dataset,
                     confirmed=confirmed)
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    truth = {m: np.array([truth_positive(m, c, "easy") for c in cells]) for m in markers}
    values = {m: np.array([c[m] for c in cells], dtype=np.float32) for m in markers}
    return {"name": name, "scenario": scenario, "truth": truth, "values": values,
            "ids": np.array([c["id"] for c in cells]),
            "cell_truth": cell_truth_of(scenario, cells, painted, markers),
            "mask": bool(mask) and not blank, "calibrated": bool(calibrated), "blank": blank}
