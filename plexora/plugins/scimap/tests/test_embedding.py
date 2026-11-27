"""The embedding behind the lasso, over the routes the page and the viewer's
window use: every cell's coordinates and identity as binary, a colour column,
labels written into the working file and undone, and the refusals that keep a
label from overwriting a step's result.

With the real Scimap Pro (a UMAP has to exist); skipped without it.
"""

from __future__ import annotations

import base64
import json
import struct

import numpy as np
import pytest

from plexora.plugins.scimap.server import embedding
from plexora.plugins.scimap.tests.test_scimap_flow import _add, _ok, _run, client  # noqa: F401

pytest.importorskip("scimappro")


def unpack(blob):
    assert blob[:4] == b"PXB1"
    (length,) = struct.unpack("<I", blob[4:8])
    meta = json.loads(blob[8:8 + length])
    start = 8 + length
    arrays = {a["name"]: np.frombuffer(blob, dtype=a["dtype"], count=a["length"], offset=start + a["offset"])
              for a in meta["arrays"]}
    return meta, arrays


def bitmask(rows):
    return base64.b64encode(np.packbits(rows.astype(np.uint8), bitorder="little").tobytes()).decode()


@pytest.fixture
def notebook(client):  # noqa: F811
    made = _ok(client.post("/plugins/scimap/api/notebooks", json={"kind": "project", "ref": "tonsil"}))
    nb = made["notebook_id"]
    prepare = made["view"]["notebook"]["cells"][0]["id"]
    _run(client, nb, prepare)
    cluster = _add(client, nb, "cluster", {"method": "kmeans", "k": 4})
    _run(client, nb, cluster)
    umap = _add(client, nb, "umap", {})
    _run(client, nb, umap)
    return nb


def test_pack_lays_arrays_out_aligned():
    blob = embedding.pack({"a": 1}, [("x", np.arange(3, dtype=np.uint16)), ("y", np.ones(2, np.float32))])
    meta, arrays = unpack(blob)
    assert meta["a"] == 1
    assert arrays["x"].tolist() == [0, 1, 2]
    assert arrays["y"].tolist() == [1.0, 1.0]
    assert all(a["offset"] % 4 == 0 for a in meta["arrays"])


def test_an_embedding_is_every_cell_with_its_identity(client, notebook):  # noqa: F811
    info = _ok(client.get(f"/plugins/scimap/api/notebooks/{notebook}/embedding"))["embedding"]
    assert info["embeddings"][0]["label"] == "UMAP"
    assert info["projects"] == ["tonsil"]
    assert "CD20" in info["columns"]["markers"]
    assert info["label_columns"] == []
    key = info["embeddings"][0]["key"]

    response = client.get(f"/plugins/scimap/api/notebooks/{notebook}/embedding/coords?key={key}")
    assert response.mimetype == "application/octet-stream"
    meta, arrays = unpack(response.data)
    assert meta["n"] == 3000 and arrays["xy"].size == 6000
    assert np.isfinite(arrays["xy"]).all()

    meta, arrays = unpack(client.get(f"/plugins/scimap/api/notebooks/{notebook}/embedding/identity").data)
    # The table's cell-id role (CellID 1..3000) is the mask label the viewer
    # keys cells by; every cell belongs to the one project.
    assert meta["matched"] == 3000
    assert sorted(arrays["labels"].tolist()) == list(range(1, 3001))
    assert set(arrays["codes"].tolist()) == {0}

    missing = client.get(f"/plugins/scimap/api/notebooks/{notebook}/embedding/coords?key=nope")
    assert missing.status_code == 400


def test_a_colour_is_codes_for_labels_and_values_for_a_marker(client, notebook):  # noqa: F811
    info = _ok(client.get(f"/plugins/scimap/api/notebooks/{notebook}/embedding"))["embedding"]
    column = info["columns"]["labels"][0]
    meta, arrays = unpack(client.get(f"/plugins/scimap/api/notebooks/{notebook}/embedding/color?column={column}").data)
    assert meta["kind"] == "category" and len(meta["categories"]) == 4
    assert sum(meta["counts"]) == 3000 and meta["missing"] == 0
    assert len(meta["colors"]) == 4 and all(c.startswith("#") for c in meta["colors"])
    assert arrays["values"].max() < 4

    meta, arrays = unpack(client.get(f"/plugins/scimap/api/notebooks/{notebook}/embedding/color?column=CD20").data)
    assert meta["kind"] == "number" and meta["marker"]
    assert meta["range"][0] < meta["range"][1]
    assert arrays["values"].dtype == np.float32 and arrays["values"].size == 3000


def _labels_or_skip(client, url, rows):
    """Labelling is Scimap Pro's tl.labelCells: on a release without it the
    notebook refuses, saying so, and the rest of the test has nothing to test."""
    from plexora.plugins.scimap.server import install

    if not install.supports("labels"):
        answer = client.post(url + "/labels", json={"column": "manual", "label": "T cell", "new": True,
                                                    "cells": bitmask(rows)})
        assert answer.status_code == 400 and "tl.labelCells" in answer.get_json()["message"]
        pytest.skip("this Scimap Pro has no tl.labelCells")


def test_labels_go_into_the_working_file_and_undo(client, notebook):  # noqa: F811
    url = f"/plugins/scimap/api/notebooks/{notebook}"
    _labels_or_skip(client, url, np.ones(3000, bool))
    first = np.zeros(3000, bool)
    first[:1000] = True
    out = _ok(client.post(url + "/labels", json={"column": "manual", "label": "T cell", "new": True,
                                                  "cells": bitmask(first)}))["labels"]
    assert out["labelled"] == 1000 and out["counts"] == {"T cell": 1000}

    info = _ok(client.get(url + "/embedding"))["embedding"]
    assert info["label_columns"] == ["manual"]
    assert "manual" in info["columns"]["labels"]
    meta, arrays = unpack(client.get(url + "/embedding/color?column=manual").data)
    assert meta["categories"] == ["T cell"] and meta["missing"] == 2000
    assert (arrays["values"][:1000] == 0).all() and (arrays["values"][1000:] == embedding.MISSING).all()

    # A second label keeps the first unless asked to overwrite.
    second = np.zeros(3000, bool)
    second[500:1500] = True
    out = _ok(client.post(url + "/labels", json={"column": "manual", "label": "B cell",
                                                  "cells": bitmask(second)}))["labels"]
    assert out["labelled"] == 500 and out["kept"] == 500
    out = _ok(client.post(url + "/labels", json={"column": "manual", "label": "B cell", "overwrite": True,
                                                  "cells": bitmask(second)}))["labels"]
    assert out["labelled"] == 1000 and out["counts"] == {"T cell": 500, "B cell": 1000}

    # Undo puts the column back as it was before the last write.
    _ok(client.post(url + "/labels/undo", json={}))
    meta, _ = unpack(client.get(url + "/embedding/color?column=manual").data)
    assert dict(zip(meta["categories"], meta["counts"])) == {"B cell": 500, "T cell": 1000}
    # The step that reads labels sees the column (any categorical column is labels).
    view = _ok(client.get(url))["view"]
    assert "manual" in view["roles"]


def test_a_label_cannot_overwrite_a_step_result(client, notebook):  # noqa: F811
    url = f"/plugins/scimap/api/notebooks/{notebook}"
    info = _ok(client.get(url + "/embedding"))["embedding"]
    cluster = info["columns"]["labels"][0]
    rows = np.ones(3000, bool)
    for column, word in ((cluster, "a step's result"), ("imageid", "names the images"), ("9bad", "starts with a letter")):
        response = client.post(url + "/labels", json={"column": column, "label": "x", "cells": bitmask(rows)})
        assert response.status_code == 400
        assert word in response.get_json()["message"]
    nothing = client.post(url + "/labels", json={"column": "manual", "label": "x", "new": True,
                                                  "cells": bitmask(np.zeros(3000, bool))})
    assert nothing.status_code == 400 and "Select" in nothing.get_json()["message"]


def test_cell_explorer_finds_the_notebook_with_an_embedding(client, notebook):  # noqa: F811
    found = _ok(client.get("/plugins/scimap/api/embedding?project=tonsil"))["notebooks"]
    assert [n["notebook_id"] for n in found] == [notebook]
    assert _ok(client.get("/plugins/scimap/api/embedding?project=elsewhere"))["notebooks"] == []


def test_the_overlay_colours_the_viewer_by_a_label_column(client, notebook):  # noqa: F811
    url = f"/plugins/scimap/api/notebooks/{notebook}"
    rows = np.zeros(3000, bool)
    rows[:10] = True
    _labels_or_skip(client, url, rows)
    _ok(client.post(url + "/labels", json={"column": "manual", "label": "T cell", "new": True, "cells": bitmask(rows)}))
    out = _ok(client.post(url + "/overlay", json={"column": "manual"}))
    assert out["annotation"] == "scimap" and out["overlay"]["column"] == "manual"
    assert client.post(url + "/overlay", json={"column": "CD20"}).status_code == 400


def test_identity_without_a_cell_id_role_is_row_order(monkeypatch, tmp_path):
    import anndata as ad
    import pandas as pd

    from plexora import api

    obs = pd.DataFrame({"imageid": pd.Categorical(["a"] * 3 + ["b"] * 2)}, index=[f"c{i}" for i in range(5)])
    path = tmp_path / "w.h5ad"
    ad.AnnData(X=np.zeros((5, 1), np.float32), obs=obs).write_h5ad(path)
    monkeypatch.setattr(api, "project_info", lambda name: {"roles": {"cell_id": None}})
    codes, labels = embedding._identity({"path": str(path), "image_column": "imageid"}, ["a", "b"])
    assert codes.tolist() == [0, 0, 0, 1, 1]
    # The table's positional id, as the viewer keys a project without a role.
    assert labels.tolist() == [0, 1, 2, 0, 1]


def test_text_ids_that_cannot_be_mask_labels_are_zero():
    out = embedding._as_labels(np.array(["5", "x", "-1", "7.5", "8"], dtype=object))
    assert out.tolist() == [5, 0, 0, 0, 8]


def test_a_selection_is_saved_as_plexoras_own(client, notebook):  # noqa: F811
    from plexora.agent.core.selection import read_all

    url = f"/plugins/scimap/api/notebooks/{notebook}"
    rows = np.zeros(3000, bool)
    rows[[0, 5, 9]] = True
    saved = _ok(client.post(url + "/selections", json={"name": "Follicle B", "cells": bitmask(rows)}))["saved"]
    # Spaces become "_": saved selections are named in letters, digits, _ . -
    assert saved == [{"project": "tonsil", "name": "Follicle_B", "n": 3}]
    stored = read_all("tonsil")["Follicle_B"]
    # The table's cell ids (CellID 1..3000), as the viewer and agents read them.
    assert sorted(stored["cell_ids"]) == ["1", "10", "6"]
    nothing = client.post(url + "/selections", json={"name": "x", "cells": bitmask(np.zeros(3000, bool))})
    assert nothing.status_code == 400
    unnamed = client.post(url + "/selections", json={"name": " ", "cells": bitmask(rows)})
    assert unnamed.status_code == 400
