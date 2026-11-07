"""`/get_all_cells`' wire encoding: gzip everywhere but the desktop app.

The route sends one flat array (ids and coordinates as uint32, marker columns
as float32). Gzipped at level 3 it halves a big table's ids and positions, which
matters when the bytes cross a network -- a terminal `plexora` on a cluster
reached through an SSH tunnel -- and costs a third of a second on 1.26 M cells
when they do not: the desktop app's own server answering its own window
(P6D.md, "Step 2 measured"). So the desktop app's server sends it raw, and the
decision is the server's mode (`PLEXORA_DESKTOP`), never the caller's address:
through a tunnel every request arrives from 127.0.0.1 too.

Either way the client reads the same bytes: dataLayer.getAllCells takes the
response's `arrayBuffer()`, which a browser has already inflated when the
answer said `Content-Encoding: gzip`.
"""

from __future__ import annotations

import gzip

import numpy as np
import polars as pl
import pytest

import plexora
from plexora.server.models import data_model
from tests.helpers import ALL_CONFIRMED, csv_spec, project

CELL_COUNT = 300


@pytest.fixture
def cells_project(tmp_path):
    """A small project with a table: ids, centroids and two markers."""
    import tifffile

    rng = np.random.default_rng(7)
    table = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": list(range(1, CELL_COUNT + 1)),
        "X_centroid": (rng.random(CELL_COUNT) * 250).tolist(),
        "Y_centroid": (rng.random(CELL_COUNT) * 250).tolist(),
        "CD3": (rng.random(CELL_COUNT) * 9).tolist(),
        "CD8": (rng.random(CELL_COUNT) * 4).tolist(),
    }).write_csv(table)
    image = tmp_path / "image.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 256, 256), dtype=np.uint8))
    spec = csv_spec(table, cell_id="CellID", x="X_centroid", y="Y_centroid",
                    markers=("CD3", "CD8"),
                    metadata=("CellID", "X_centroid", "Y_centroid"))
    project("cells", dataset=spec, confirmed=ALL_CONFIRMED, src=str(image)).save()
    return "cells"


def _desktop(monkeypatch, on):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", on)


ROUTES = [
    ("integer", "CellID,X_centroid,Y_centroid", int, np.uint32),
    ("float", "CellID,X_centroid,Y_centroid,CD3", float, np.float32),
]


def _ask(name, dtype, keys, **environ):
    client = plexora.app.test_client()
    return client.get(f"/get_all_cells/{dtype}/?datasource={name}&start_keys={keys}",
                      environ_base=environ)


@pytest.mark.parametrize("dtype,keys,kind,wire", ROUTES)
def test_the_desktop_app_sends_the_array_raw(cells_project, monkeypatch,
                                             dtype, keys, kind, wire):
    _desktop(monkeypatch, True)
    response = _ask(cells_project, dtype, keys)

    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers
    assert int(response.headers["Content-Length"]) == len(response.data)
    expected = data_model.get_all_cells(cells_project, keys.split(","), kind)
    assert response.data == expected.tobytes("C")
    values = np.frombuffer(response.data, dtype=wire)
    assert values.size == CELL_COUNT * len(keys.split(","))


@pytest.mark.parametrize("dtype,keys,kind,wire", ROUTES)
def test_every_other_server_gzips_it(cells_project, monkeypatch, dtype, keys, kind, wire):
    _desktop(monkeypatch, False)
    response = _ask(cells_project, dtype, keys)

    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert int(response.headers["Content-Length"]) == len(response.data)
    expected = data_model.get_all_cells(cells_project, keys.split(","), kind)
    assert gzip.decompress(response.data) == expected.tobytes("C")


def test_a_tunnelled_server_keeps_gzip_although_it_is_asked_from_loopback(
        cells_project, monkeypatch):
    """`plexora --remote` on a cluster, reached through `ssh -L`: the request
    arrives from 127.0.0.1 like the desktop window's, and its bytes still
    cross the network. The address decides nothing."""
    _desktop(monkeypatch, False)
    monkeypatch.setitem(plexora.app.config, "PLEXORA_SERVER_IS_REMOTE", True)
    response = _ask(cells_project, "integer", "CellID,X_centroid,Y_centroid",
                    REMOTE_ADDR="127.0.0.1")

    assert response.headers["Content-Encoding"] == "gzip"


def test_the_desktop_app_decides_by_mode_not_by_address(cells_project, monkeypatch):
    """The other half: the desktop app's server answers raw whatever address
    the request names (it only ever listens on loopback anyway)."""
    _desktop(monkeypatch, True)
    for address in ("127.0.0.1", "::1"):
        response = _ask(cells_project, "integer", "CellID,X_centroid,Y_centroid",
                        REMOTE_ADDR=address)
        assert "Content-Encoding" not in response.headers


def test_the_client_reads_the_answer_either_way():
    """dataLayer.getAllCells takes `arrayBuffer()` of the fetch and nothing
    else: no gunzip of its own (which would break on a raw answer) and no
    reliance on a Content-Encoding header."""
    from pathlib import Path

    source = (Path(plexora.__file__).parent / "client" / "src" / "js" / "services"
              / "dataLayer.js").read_text(encoding="utf-8")
    start = source.index("async getAllCells(")
    body = source[start:source.index("\n    }\n", start)]
    assert "response.arrayBuffer()" in body
    for decoder in ("DecompressionStream", "pako", "inflate", "ungzip"):
        assert decoder not in body
