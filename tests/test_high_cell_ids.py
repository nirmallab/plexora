"""Cell ids past 2^24 on the desktop's server (the envelope's mask-id cell).

float32's last exact integer is 16,777,216: an id that passes through a
float32 anywhere comes back as its neighbour. A project whose ids straddle
2^24, and one whose ids sit near 2^32, is imported as the Import dialog
imports one, and every answer that carries an id is checked against the
table: the label tiles (whose fourth byte was dropped until 2026-11, so
16,777,216 drew as background and 16,777,217 as cell 1), the viewer's id
list, the nearest-cell pick and a gate. tests/browser/high_ids.mjs checks the
same on the website.
"""

from __future__ import annotations

import gzip
import io

import numpy as np
import pytest
import tifffile

import plexora

SIDE = 256
CELL = 64          # a 4 x 4 grid of cells, each a 48-pixel square in its own 64


def _project(folder, offset):
    """image.tif (2 channels), mask.tif (uint32) and cells.csv with ids
    offset + 1 .. offset + 16, and the table's rows."""
    folder.mkdir()
    rng = np.random.default_rng(3)
    image = rng.integers(100, 1000, (2, SIDE, SIDE), dtype=np.uint16)
    mask = np.zeros((SIDE, SIDE), dtype=np.uint32)
    rows = []
    for k in range(16):
        y, x = divmod(k, 4)
        cell = offset + k + 1
        mask[y * CELL + 8:y * CELL + 56, x * CELL + 8:x * CELL + 56] = cell
        rows.append((cell, x * CELL + 31.5, y * CELL + 31.5, 48 * 48, float(100 + 50 * k), float(5 * k)))
    tifffile.imwrite(folder / "image.tif", image, photometric="minisblack")
    tifffile.imwrite(folder / "mask.tif", mask, photometric="minisblack")
    lines = ["CellID,X_centroid,Y_centroid,Area,M1,M2"]
    lines += [f"{c},{x},{y},{a},{m1},{m2}" for c, x, y, a, m1, m2 in rows]
    (folder / "cells.csv").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return mask, rows


def _body(response):
    data = response.get_data()
    return gzip.decompress(data) if response.headers.get("Content-Encoding") == "gzip" else data


@pytest.fixture(params=[(1 << 24) - 8, 4_000_000_000], ids=["straddles-2^24", "near-2^32"])
def imported(request, tmp_path):
    offset = request.param
    mask, rows = _project(tmp_path / "files", offset)
    client = plexora.app.test_client()
    paths = [str(tmp_path / "files" / name) for name in ("image.tif", "mask.tif", "cells.csv")]
    response = client.post("/import/sample", json={"paths": paths, "name": f"hi{offset}"})
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    return client, response.get_json()["name"], mask, rows


def test_the_viewers_id_list_is_the_tables(imported):
    client, name, _, rows = imported
    response = client.get(f"/get_all_cells/integer/?start_keys=CellID&datasource={name}")
    assert response.status_code == 200
    assert np.frombuffer(_body(response), dtype=np.uint32).tolist() == [row[0] for row in rows]


def test_the_nearest_cell_pick_names_every_cell(imported):
    client, name, _, rows = imported
    for cell, x, y, *_ in rows:
        response = client.get(f"/get_nearest_cell?point_x={x}&point_y={y}&datasource={name}")
        assert response.status_code == 200
        body = response.get_json()
        assert int(body.get("CellID", body.get("id"))) == cell, body


def test_a_gate_selects_exactly_the_tables_cells(imported):
    import json

    client, name, _, rows = imported
    cut = 100 + 50 * 7.5
    response = client.get("/plugins/gating/get_gated_cell_ids", query_string={
        "filter": json.dumps({"M1": [cut, 1e12]}), "start_keys": "CellID", "datasource": name})
    assert response.status_code == 200
    got = sorted(int(row.get("CellID", row.get("id"))) for row in response.get_json())
    assert got == [row[0] for row in rows if row[4] > cut]


def test_the_label_tiles_carry_every_id_whole(imported):
    from PIL import Image

    client, name, mask, rows = imported
    # The mask's pyramid is built after the import, as the viewer waits for it.
    import time

    until = time.monotonic() + 120
    status = {}
    while time.monotonic() < until:
        status = client.get(f"/get_segmentation_status?datasource={name}").get_json() or {}
        if status.get("status") in ("ready", "failed", "error"):
            break
        time.sleep(0.2)
    assert status.get("status") == "ready", status
    # The label layer's address, as the viewer takes it (config.imageData[0]).
    config = (client.get("/config").get_json() or {}).get(name) or {}
    srcs = [d["src"] for d in config.get("imageData", []) if d.get("src")]
    assert srcs, sorted(config)[:20]
    # Every level's top-left tile: each id seen is a cell of the table, and
    # the full-resolution one holds all sixteen.
    seen = set()
    for level in range(3):
        response = client.get(f"{srcs[0].rstrip('/')}/{level}/0_0.png")
        if response.status_code != 200:
            continue
        rgba = np.asarray(Image.open(io.BytesIO(response.get_data())).convert("RGBA"))
        ids = np.ascontiguousarray(rgba).view(np.uint32)[..., 0]
        seen |= set(np.unique(ids[ids > 0]).tolist())
    assert seen and seen <= {row[0] for row in rows}, (srcs[0], sorted(seen)[:5])
    assert seen == {row[0] for row in rows}
