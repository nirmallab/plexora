"""A QC project on disk: a scene from `plexora.ai.qc_scenes`, registered.

The pyramid, the mask and the table agree: every cell's value in the table is
the mean of the (artifact-bearing) pixels inside its label, so a cell in a
dropped-out region reads dim in the table exactly as it does in the image.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np

from plexora.ai.qc_scenes import BACKGROUND, qc_scene
from tests.agent_fixtures import _write_pyramid
from tests.helpers import ALL_CONFIRMED, csv_spec, image_spec, project

PIXEL_SIZE_UM = 1.0
#: Map cells of 25 µm, so a 1024 px scene is a 41 x 41 grid.
SCAN_PARAMS = {"cell_um": 25.0}


def seg_errors_into(image, labels, cells, channels, errors, seed=0):
    """Segmentation mistakes with a known answer, made in place.

    A **merge** relabels two horizontally adjacent discs as one and joins
    them with a narrow neck of half-bright DNA (so the merged label is one
    connected object with two nuclei in it). A **split** gives the right 40 %
    of a disc a new id, the DNA untouched (one nucleus, two labels; the new,
    peakless piece is the fragment). A **big** nucleus replaces a cell and its
    eight neighbours with one disc 2.2x a cell's radius, cut into a centre
    piece and four quadrants (the quadrants are the fragments). **dim** (a
    fraction) turns clean cells' DNA down to 0.35x; **expand** (px) grows
    every label into the tissue round it, as a whole-cell mask does; **shift**
    (px) moves the labels off their nuclei. Returns {"under": ids, "over":
    ids, "dim": ids, "big": [{home, pieces}]} and the edited cell list."""
    from scipy import ndimage

    rng = np.random.default_rng(seed + 101)
    dna = [i for i, name in enumerate(channels) if name.startswith("DNA")]
    by_id = {c["id"]: c for c in cells}
    xs = np.array([c["x"] for c in cells])
    spacing = float(np.median(np.diff(np.unique(np.round(xs, 3))))) if len(cells) > 1 else 1.0
    areas = np.bincount(labels.ravel())
    used = set()

    where = {c["id"]: (c["x"], c["y"]) for c in cells}

    def free(cell, margin=2.2):
        return all(np.hypot(cell["x"] - where[u][0], cell["y"] - where[u][1])
                   > margin * spacing for u in used if u in where)

    order = rng.permutation(len(cells))
    under, over = [], []
    wanted_merges = int(errors.get("merge", 0))
    for i in order:
        if len(under) >= wanted_merges:
            break
        a = cells[i]
        b = next((c for c in cells if abs(c["y"] - a["y"]) < 0.5
                  and abs(c["x"] - a["x"] - spacing) < 0.5), None)
        if b is None or not free(a) or not free(b):
            continue
        r = float(np.sqrt(areas[a["id"]] / np.pi))
        labels[labels == b["id"]] = a["id"]
        x0, x1 = int(a["x"] + 0.6 * r), int(np.ceil(b["x"] - 0.6 * r))
        y0, y1 = int(a["y"] - 0.35 * r), int(np.ceil(a["y"] + 0.35 * r))
        band = np.zeros(labels.shape, dtype=bool)
        band[y0:y1, x0:x1] = True
        gap = band & (labels == 0)
        labels[band] = a["id"]
        for k in dna:
            image[k][gap] = np.uint16(0.5 * float(a[channels[k]]))
        used.update((a["id"], b["id"]))
        under.append(a["id"])
        by_id.pop(b["id"])
    next_id = int(labels.max()) + 1
    wanted_splits = int(errors.get("split", 0))
    for i in order:
        if len(over) >= wanted_splits:
            break
        a = cells[i]
        if a["id"] not in by_id or not free(a):
            continue
        r = float(np.sqrt(areas[a["id"]] / np.pi))
        yy, xx = np.nonzero(labels == a["id"])
        right = xx >= a["x"] + 0.2 * r
        labels[yy[right], xx[right]] = next_id
        piece = dict(a, id=next_id, x=float(xx[right].mean()), y=float(yy[right].mean()))
        by_id[next_id] = piece
        used.update((a["id"], next_id))
        over.append(next_id)
        next_id += 1
    big = []
    wanted_big = int(errors.get("big", 0))
    for i in order:
        if len(big) >= wanted_big:
            break
        a = cells[i]
        ring = [c for c in by_id.values() if c["id"] != a["id"]
                and np.hypot(c["x"] - a["x"], c["y"] - a["y"]) < 1.5 * spacing]
        if a["id"] not in by_id or len(ring) != 8 or not free(a, margin=4.5) \
                or not all(free(c, margin=3.0) for c in ring):
            continue
        # One big nucleus where nine small ones were: the ring cleared to
        # tissue, a disc 2.2 cells' radius at 0.8x the DNA with a soft edge,
        # cut into a centre piece (the cell's own id) and four quadrants.
        r = float(np.sqrt(areas[a["id"]] / np.pi))
        big_r = 2.2 * r
        for c in ring:
            gone = labels == c["id"]
            labels[gone] = 0
            for k in dna:
                image[k][gone] = BACKGROUND + 80.0
            by_id.pop(c["id"])
            used.add(c["id"])
        yy, xx = np.mgrid[0:labels.shape[0], 0:labels.shape[1]]
        dist = np.hypot(xx - a["x"], yy - a["y"])
        disc = dist <= big_r
        blur = 0.15 * big_r
        pad = int(np.ceil(big_r + 4 * blur))
        y0, y1 = max(0, int(a["y"]) - pad), min(labels.shape[0], int(a["y"]) + pad + 1)
        x0, x1 = max(0, int(a["x"]) - pad), min(labels.shape[1], int(a["x"]) + pad + 1)
        for k in dna:
            plane = image[k][y0:y1, x0:x1].astype(np.float64)
            plane[disc[y0:y1, x0:x1]] = 0.8 * float(a[channels[k]])
            image[k][y0:y1, x0:x1] = ndimage.gaussian_filter(plane, blur)
        labels[disc] = a["id"]
        angle = np.arctan2(yy - a["y"], xx - a["x"])
        quadrant = (np.floor((angle + np.pi) / (np.pi / 2)).astype(np.int64)) % 4
        pieces = []
        for q in range(4):
            part = disc & (dist > 0.5 * big_r) & (quadrant == q)
            labels[part] = next_id
            py, px = np.nonzero(part)
            by_id[next_id] = dict(a, id=next_id, x=float(px.mean()), y=float(py.mean()))
            pieces.append(next_id)
            next_id += 1
        used.update((a["id"], *pieces))
        over.extend(pieces)
        big.append({"home": a["id"], "pieces": pieces})
    dim = []
    wanted_dim = float(errors.get("dim", 0))
    if wanted_dim:
        clean = [c for c in by_id.values() if c["id"] not in used]
        pick = rng.permutation(len(clean))[:int(round(wanted_dim * len(clean)))]
        for j in sorted(pick):
            c = clean[j]
            inside = labels == c["id"]
            for k in dna:
                image[k][inside] = 0.35 * image[k][inside]
                c[channels[k]] = 0.35 * c[channels[k]]
            dim.append(c["id"])
    grow = int(errors.get("expand", 0))
    if grow:
        # A whole-cell mask: every label grown into the tissue round it, so
        # neighbours touch across cytoplasm, not across DNA.
        from skimage.segmentation import expand_labels

        tissue = image[dna[0]] > BACKGROUND + 40.0
        grown = expand_labels(labels, distance=grow)
        labels[(labels == 0) & tissue] = grown[(labels == 0) & tissue]
    shift = int(errors.get("shift", 0))
    if shift:
        # Labels displaced off their nuclei (what a ring or cytoplasm mask
        # looks like to the DNA): the peaks fall between labels.
        labels[:] = np.roll(labels, shift, axis=1)
        for c in by_id.values():
            c["x"] = c["x"] + shift
    out = {"under": sorted(under), "over": sorted(over)}
    if dim:
        out["dim"] = sorted(dim)
    if big:
        out["big"] = big
    return out, sorted(by_id.values(), key=lambda c: c["id"])


def make_qc_project(data_root, name="qcsynth", *, size=1024, grid=40, artifacts=(),
                    seed=0, calibrated=True, mask=True, table=True, levels=4,
                    shape="square", seg_errors=None):
    from scipy import ndimage

    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells, channels, truth = qc_scene(size=size, grid=grid,
                                                     artifacts=artifacts, seed=seed,
                                                     shape=shape)
    if seg_errors:
        truth["segmentation"], cells = seg_errors_into(image, labels, cells, channels,
                                                       seg_errors, seed=seed)
    image_path = _write_pyramid(folder / "image.ome.tif", image, levels=levels,
                                pixel_size=PIXEL_SIZE_UM if calibrated else None)
    mask_path = _write_pyramid(folder / "mask.tif", labels, levels=levels, ome=False) \
        if mask else None
    dataset = None
    if table:
        ids = np.array([cell["id"] for cell in cells])
        areas = np.bincount(labels.ravel(), minlength=int(ids.max()) + 1)
        means = {name: ndimage.mean(image[i].astype(np.float64), labels, ids)
                 for i, name in enumerate(channels)}
        header = ["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity", "Solidity",
                  *channels]
        lines = [",".join(header)]
        for index, cell in enumerate(cells):
            row = [str(cell["id"]), f"{cell['x']:.3f}", f"{cell['y']:.3f}",
                   str(int(areas[cell["id"]])), "0.2000", "0.9500"]
            row += [f"{means[c][index]:.4f}" for c in channels]
            lines.append(",".join(row))
        csv_path = folder / "cells.csv"
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        dataset = csv_spec(csv_path, markers=channels,
                           metadata=("Area", "Eccentricity", "Solidity"))
    record = project(name, dataset=dataset,
                     segmentation=str(mask_path) if mask_path and table else None,
                     image=image_spec(channels=channels, width=size, height=size,
                                      src=str(image_path)),
                     confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=levels - 1, tile_width=128, tile_height=128))
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return {"name": name, "cells": cells, "labels": labels, "image": image,
            "channels": channels, "truth": truth, "image_path": str(image_path),
            "size": size}
