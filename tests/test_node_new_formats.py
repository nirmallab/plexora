"""Today's additions, on a data node: NaN tables and the new mask formats.

Everything a project can read locally it must read the same way when the file
is on another machine. These run a real node (tests/node_harness.py) against
the cases tests/test_nan_tables.py, tests/test_mask_formats.py and
tests/test_ims_masks.py cover locally.
"""

from __future__ import annotations

import io
import time

import numpy as np
import polars as pl
import pytest
from PIL import Image

from tests.helpers import ALL_CONFIRMED, csv_spec, project
from tests.node_harness import node_process, register  # noqa: F401 - fixture
from tests.test_ims_masks import write_ims

ROWS = 40


def _status(node, resource_id, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        status = node.get(f"/node/v1/resources/{resource_id}/status").get("resource") or {}
        if status.get("state") in ("ready", "error") or time.monotonic() > deadline:
            return status
        time.sleep(0.2)


def _decode(payload):
    rgba = np.asarray(Image.open(io.BytesIO(payload)).convert("RGBA")).astype(np.uint32)
    return rgba[..., 0] + (rgba[..., 1] << 8) + (rgba[..., 2] << 16)


def _labels(height=96, width=128):
    plane = np.zeros((height, width), dtype=np.uint16)
    plane[10:40, 10:40] = 1
    plane[50:80, 20:60] = 2
    plane[20:70, 80:120] = 300
    return plane


# -- masks ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["png", "ims"])
def test_a_png_or_imaris_mask_is_served_by_a_node(tmp_path, node_process, kind):
    labels = _labels()
    if kind == "png":
        path = tmp_path / "cells_cp_masks.png"
        Image.fromarray(labels).save(path)
    else:
        path = write_ims(tmp_path / "cells.ims", [("cells", labels[None].astype(np.float32))])
    node = node_process(f"segmentation:m={path}")

    status = _status(node, "m")
    assert status["state"] == "ready", status
    assert status["mask_mode"] == "filled"
    ids = _decode(node.get("/node/v1/seg/m/tile/0/0_0.png", raw=True))
    assert np.array_equal(ids[:96, :128], labels)


def test_a_jpeg_shared_as_a_mask_says_why_it_cannot_be_one(tmp_path, node_process):
    from plexora import nodes

    jpeg = tmp_path / "mask.jpg"
    Image.fromarray(np.zeros((32, 32), np.uint8)).save(jpeg)
    node = node_process(dynamic=True)
    register("o2", node)
    try:
        shared = nodes.share_path("o2", "segmentation", str(jpeg))
    except Exception as exc:  # refused at the door
        assert "JPEG" in str(exc)
        return
    status = _status(node, shared.get("id") if isinstance(shared, dict) else shared)
    assert status["state"] == "error" and "JPEG" in (status.get("error") or ""), status


# -- a table with NaNs ----------------------------------------------------------


def _nan_table(directory):
    rng = np.random.default_rng(0)
    x = rng.uniform(1, 250, ROWS)
    y = rng.uniform(1, 250, ROWS)
    x[3] = np.nan
    y[9] = np.nan
    cd3 = rng.uniform(0, 3, ROWS)
    cd3[::5] = np.nan
    frame = pl.DataFrame({"CellID": np.arange(1, ROWS + 1), "X_centroid": x,
                          "Y_centroid": y, "CD3": cd3, "CD8": rng.uniform(0, 3, ROWS)})
    path = directory / "cells.csv"
    frame.write_csv(path)
    return path, frame


def _image_file(directory):
    import tifffile

    path = directory / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def test_a_table_with_nan_positions_on_a_node_opens_and_says_so(tmp_path, node_process):
    from plexora.nodes import attach_table
    from plexora.server.models import data_model

    path, frame = _nan_table(tmp_path)
    node = node_process(f"table:cells={path}")
    register("o2", node)
    spec = csv_spec(path, cell_id="CellID", x="X_centroid", y="Y_centroid",
                    markers=("CD3", "CD8"), metadata=("CellID", "X_centroid", "Y_centroid"))
    project("remote", dataset=spec, confirmed=ALL_CONFIRMED,
            src=str(_image_file(tmp_path)), width=256, height=256).save()
    attach_table("remote", node="o2", resource_id="cells")
    data_model.load_datasource("remote", reload=True)

    # Every placed cell answers a hover as itself: the hover tree, built here
    # from the node's (id, x, y), leaves the two unplaced rows out and maps
    # its positions back to the right rows.
    for row in (0, 4, 10, ROWS - 1):
        hit = data_model.query_for_closest_cell(
            frame["X_centroid"][row], frame["Y_centroid"][row], "remote")
        assert hit["CellID"] == row + 1, row
    report = data_model.get_consistency_report("remote")
    assert [f["code"] for f in report] == ["cells_without_position"]
    assert report[0]["message"].startswith(f"2 of {ROWS} cells")
