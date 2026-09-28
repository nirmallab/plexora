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

from plexora.ai.qc_scenes import qc_scene
from tests.agent_fixtures import _write_pyramid
from tests.helpers import ALL_CONFIRMED, csv_spec, image_spec, project

PIXEL_SIZE_UM = 1.0
#: Map cells of 25 µm, so a 1024 px scene is a 41 x 41 grid.
SCAN_PARAMS = {"cell_um": 25.0}


def make_qc_project(data_root, name="qcsynth", *, size=1024, grid=40, artifacts=(),
                    seed=0, calibrated=True, mask=True, table=True, levels=4):
    from scipy import ndimage

    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells, channels, truth = qc_scene(size=size, grid=grid,
                                                     artifacts=artifacts, seed=seed)
    image_path = _write_pyramid(folder / "image.ome.tif", image, levels=levels,
                                pixel_size=PIXEL_SIZE_UM if calibrated else None)
    mask_path = _write_pyramid(folder / "mask.tif", labels, levels=levels, ome=False) \
        if mask else None
    dataset = None
    if table:
        ids = np.arange(1, len(cells) + 1)
        areas = np.bincount(labels.ravel(), minlength=len(cells) + 1)
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
