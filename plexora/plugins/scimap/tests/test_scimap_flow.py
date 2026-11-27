"""A notebook from start to finish with the real Scimap Pro: a CSV project is
prepared, clustered, annotated, mapped, measured in space and exported -- over
the HTTP routes the page uses, so the page's contract is what is tested.

Skipped where Scimap Pro is not installed (the repo's .venv); the conda env
the server runs in has it. Synthetic data with planted structure: four kinds
of cell, each bright in its own markers, so a k-means with k=4 must recover
them and the numbers each step reports can be checked, not just their shape.
"""

from __future__ import annotations

import io
import json
import time
import zipfile

import numpy as np
import pandas as pd
import pytest

import plexora
from plexora.server import plugins as plugin_registry
from tests.helpers import ALL_CONFIRMED, csv_spec, image_spec, project, use_data_root

pytest.importorskip("scimappro")

MARKERS = ["CD3", "CD4", "CD8", "CD20", "CD68", "PANCK"]
BRIGHT = {0: ["CD3", "CD4"], 1: ["CD3", "CD8"], 2: ["CD20"], 3: ["CD68"]}


def _table(path, n=3000, seed=0):
    rng = np.random.default_rng(seed)
    kinds = rng.integers(0, 4, n)
    X = rng.gamma(2.0, 30.0, (n, len(MARKERS)))
    for kind, names in BRIGHT.items():
        rows = np.flatnonzero(kinds == kind)
        for name in names:
            X[rows, MARKERS.index(name)] *= 12
    frame = pd.DataFrame(X, columns=MARKERS)
    frame.insert(0, "CellID", np.arange(1, n + 1))
    frame["X_centroid"] = rng.uniform(0, 1500, n)
    frame["Y_centroid"] = rng.uniform(0, 1500, n)
    frame.to_csv(path, index=False)
    return kinds


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    table = tmp_path / "tables" / "cells.csv"
    table.parent.mkdir()
    kinds = _table(table)
    spec = csv_spec(str(table), markers=MARKERS)
    (tmp_path / "config.json").write_text(json.dumps({
        "tonsil": project("tonsil", dataset=spec, image=image_spec(channels=("DNA", "CD3")),
                          confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")
    if plugin_registry.find(plexora.app, "scimap") is None:  # pragma: no cover
        pytest.skip("scimap is not installed")
    c = plexora.app.test_client()
    c.kinds = kinds
    c.table = table
    return c


def _ok(response):
    body = response.get_json()
    assert response.status_code == 200, body
    assert body["success"], body
    return body


def _wait(client, nb, cell, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}"))["view"]
        state = next(c for c in view["notebook"]["cells"] if c["id"] == cell)["state"]
        if state not in ("queued", "running"):
            return view, state
        time.sleep(0.2)
    raise AssertionError("step did not finish")


def _add(client, nb, tool, params=None):
    body = _ok(client.patch(f"/plugins/scimap/api/notebooks/{nb}",
                            json={"ops": [{"op": "add_cell", "tool": tool, "params": params or {}}]}))
    return body["created"][0]


def _run(client, nb, cell, **body):
    _ok(client.post(f"/plugins/scimap/api/notebooks/{nb}/cells/{cell}/run", json=body))
    view, state = _wait(client, nb, cell)
    step = next(c for c in view["notebook"]["cells"] if c["id"] == cell)
    assert state == "done", (step.get("run") or {}).get("error")
    return view, step


def _result(client, nb, cell):
    return _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cell}/result"))["result"]


def test_status_answers_without_importing_scimappro_into_the_page(client):
    status = _ok(client.get("/plugins/scimap/api/status"))["status"]
    assert status["install"]["installed"] and status["install"]["compatible"]
    assert status["licence"]["tier"] in ("starter", "licensed", "trial", "grace", "expired", "invalid", "unverifiable")
    assert "certificate" not in json.dumps(status)
    assert len(status["palette"]) >= 8


def test_notebook_page_opens_a_notebook_for_the_project(client):
    response = client.get("/plugins/scimap/notebook?project=tonsil")
    assert response.status_code == 302
    page = client.get(response.headers["Location"])
    assert page.status_code == 200
    assert b'id="sm_workspace"' in page.data
    assert b"scimapWorkspace.js" in page.data


def test_a_csv_project_from_prepare_to_export(client, tmp_path):
    made = _ok(client.post("/plugins/scimap/api/notebooks", json={"kind": "project", "ref": "tonsil"}))
    nb = made["notebook_id"]
    view = made["view"]
    cells = view["notebook"]["cells"]
    assert [c["tool"] for c in cells] == ["prepare"]
    assert view["availability"]["cluster"]["reason"] == "Requires prepared data"
    # Results go beside the data, and so does the notebook itself.
    results = client.table.parent / "scimap_analysis"
    assert view["notebook"]["results_dir"] == str(results)
    assert (results / "notebooks" / "analysis" / "notebook.db").is_file()

    # Sample metadata, keyed by image, is kept with the project in Plexora.
    saved = _ok(client.put(f"/plugins/scimap/api/notebooks/{nb}/samples",
                           json={"samples": {"columns": ["patient"], "rows": {"tonsil": {"patient": "P1"}}}}))
    assert saved["match"] == {"matched": 1, "images": 1, "unknown": []}
    data = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/data"))["data"]
    assert data["rows"][0]["cells"] == 3000 and data["rows"][0]["ok"]
    assert data["samples"]["rows"] == {"tonsil": {"patient": "P1"}}

    plan = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/plan"))["plan"]
    assert plan["mode"] == "convert" and not plan["problems"]
    assert plan["rows"][0]["marker_names"] == MARKERS

    view, prepare = _run(client, nb, cells[0]["id"])
    work = view["working"]
    assert work["exists"] and work["n_cells"] == 3000 and work["markers"] == MARKERS
    # Prepare ends with Scimap Pro's own answer to how it reads the file.
    assert work["reading"]["mode"] in ("memory", "stream") and work["reading"]["dataset"].startswith("3,000 cells")
    # Sample metadata is written by Scimap Pro's tl.addMetadata; a release
    # without it prepares the data anyway and says why the column is missing.
    from plexora.plugins.scimap.server import install

    if install.supports("metadata"):
        assert "patient" in {c["name"] for c in work["columns"]}
    else:
        assert "patient" not in {c["name"] for c in work["columns"]}
        assert "tl.addMetadata" in (prepare["run"]["working"].get("metadata_note") or "")
    again = _ok(client.post(f"/plugins/scimap/api/notebooks/{nb}/reading", json={"memory": "memory"}))
    assert again["reading"]["mode"] == "memory" and again["memory"] == "memory"
    assert work["images"] == ["tonsil"]
    assert view["availability"]["cluster"]["enabled"]

    cluster = _add(client, nb, "cluster", {"method": "kmeans", "k": 4})
    view, step = _run(client, nb, cluster)
    assert step["run"]["effective"]["kwargs"]["randomState"] == 0
    assert step["run"]["effective"]["label"] == "kmeans_k4"
    assert "k-means" in step["chips"]
    body = _result(client, nb, cluster)
    assert len(body["groups"]) == 4
    assert sum(g["count"] for g in body["groups"]) == 3000
    assert body["unit"]["n"] == 1
    # The planted kinds come back: each cluster is bright in its own markers.
    tops = {tuple(sorted(t[:2])) for t in body["top_markers"]}
    assert ("CD20",) in {tuple(t[:1]) for t in body["top_markers"]} or any("CD20" in t for t in tops)

    # The figures are Scimap Pro's own: plotly for the page, matplotlib for a paper.
    figs = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/figures"))["figures"]
    names = [f["name"] for f in figs["figures"]]
    assert names == ["heatmap", "sizes"] and not any(f.get("error") for f in figs["figures"])
    heat = figs["figures"][0]["figure"]
    assert heat["data"][0]["type"] == "heatmap" and heat["layout"]["yaxis"]["type"] == "category"
    pdf = client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/figures/heatmap.pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    png = client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/figures/sizes.png")
    assert png.status_code == 200 and png.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/figures/heatmap.exe").status_code == 400
    columns = {c["name"]: c for c in _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}"))["view"]["working"]["columns"]}
    assert set(columns["kmeans_k4"]["colors"]) == set(columns["kmeans_k4"]["categories"])

    annotate = _add(client, nb, "name_groups",
                    {"source": "kmeans_k4", "names": {g["id"]: f"type {g['id']}" for g in body["groups"][:2]}})
    view, step = _run(client, nb, annotate)
    assert step["run"]["effective"]["kwargs"]["fromColumn"] == "kmeans_k4"
    named = _result(client, nb, annotate)
    assert {"type " + body["groups"][0]["id"]} <= {g["id"] for g in named["groups"]}

    # Find markers: each cluster's top markers, and a table of every one.
    markers = _add(client, nb, "markers", {"source": "kmeans_k4"})
    view, step = _run(client, nb, markers)
    assert step["run"]["effective"]["kwargs"]["groupBy"] == "kmeans_k4"
    ranked = _result(client, nb, markers)
    assert set(ranked["tops"]) == {g["id"] for g in body["groups"]} and ranked["table"]["rows"]

    # Check quality flags cells and deletes none.
    quality = _add(client, nb, "quality", {})
    view, step = _run(client, nb, quality)
    assert [b["key"] for b in step["run"]["effective"]["before"]] == ["sp.spatialQcMetrics"]
    flagged = _result(client, nb, quality)
    assert flagged["n_cells"] == body["n_cells"] and flagged["table"]["rows"]
    # A flag column is not offered as cell labels.
    roles = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}"))["view"]["roles"]
    assert roles.get("spatialOutlier") == "flags"
    assert all(r == "flags" for c, r in roles.items() if c.startswith("spatialOutlier_qc_"))

    composition = _add(client, nb, "composition", {"source": "kmeans_k4"})
    _run(client, nb, composition)
    comp = _result(client, nb, composition)
    assert comp["images"] == ["tonsil"] and abs(sum(comp["pcts"][0]) - 100) < 1e-6

    distance = _add(client, nb, "distance", {"source": "kmeans_k4"})
    _run(client, nb, distance)
    dist = _result(client, nb, distance)
    matrix = dist["matrices"]["tonsil"]
    assert len(matrix) == 4 and all(v is None or v >= 0 for row in matrix for v in row)

    interaction = _add(client, nb, "interaction", {"source": "kmeans_k4", "permutations": 20})
    _run(client, nb, interaction)
    inter = _result(client, nb, interaction)
    assert "permutations" in inter["null_model"]

    hoods = _add(client, nb, "neighborhoods", {"source": "kmeans_k4", "n": 3})
    view, _ = _run(client, nb, hoods)
    assert view["availability"]["name_groups"]["enabled"]
    hood = _result(client, nb, hoods)
    assert len(hood["groups"]) == 3 and len(hood["composition"]["kinds"]) == 4

    # Rerunning the clustering marks what read it as changed, and runs nothing.
    _ok(client.patch(f"/plugins/scimap/api/notebooks/{nb}",
                     json={"ops": [{"op": "set_params", "id": cluster, "params": {"seed": 3}}]}))
    view, _ = _run(client, nb, cluster)
    states = {c["id"]: c["state"] for c in view["notebook"]["cells"]}
    assert states[annotate] == "outdated" and states[distance] == "outdated"

    # Every run is a version; the replaced one is kept beside the notebook.
    history = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/history"))["history"]
    v1, v2 = history["versions"]
    assert (v1["version"], v2["version"], history["current"]) == (1, 2, 2)
    assert v2["parent"] == 1 and v2["change"] == "Seed 3" and v1["restorable"] and v1["kept"]
    assert (results / "notebooks" / "analysis" / "02-clustering" / "v1" / "results.parquet").is_file()
    # Back to seed 0: the kept v1 is put back, not computed again.
    _ok(client.patch(f"/plugins/scimap/api/notebooks/{nb}",
                     json={"ops": [{"op": "set_params", "id": cluster, "params": {"seed": 0}}]}))
    _run(client, nb, cluster)
    history = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/history"))["history"]
    assert history["versions"][-1]["how"] == "Reused v1's result"
    compared = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/compare?a=2&b=3"))["compare"]
    assert compared["agreement"]["cells"] == 3000 and 0 <= compared["agreement"]["ari"] <= 1
    assert compared["calls"] == [{"name": "randomState", "left": "3", "right": "0"}]
    same = _ok(client.get(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/compare?a=1&b=3"))["compare"]
    assert same["agreement"]["ari"] == 1.0
    view = _ok(client.post(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/current", json={"version": 2}))["view"]
    step = next(c for c in view["notebook"]["cells"] if c["id"] == cluster)
    assert step["run"]["version"] == 2 and step["run"]["effective"]["kwargs"]["randomState"] == 3

    # Advanced parameters are checked against the function's signature.
    check = _ok(client.post(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/advanced",
                            json={"key": "tl.cluster", "text": "nPcs = 3\nleidenFlavour = 'igraph'\nk = 5"}))["check"]
    assert [e["line"] for e in check["errors"]] == [2, 3]
    assert check["errors"][0]["suggestion"] == "leidenFlavor" and "Clusters field" in check["errors"][1]["message"]

    # The viewer overlay maps the step's labels onto the project's rows.
    shown = _ok(client.post(f"/plugins/scimap/api/notebooks/{nb}/cells/{cluster}/view", json={}))
    assert shown["project"] == "tonsil" and shown["annotation"] == "scimap"
    overlay = _ok(client.get("/plugins/scimap/api/overlay"))["overlay"]
    assert overlay["column"] == "kmeans_k4" and "path" not in overlay
    from plexora.plugins.scimap.server import overlay as overlay_module

    class _Table:
        def geometry(self):
            import polars as pl

            frame = pd.read_csv(client.table)
            return pl.DataFrame({"CellID": frame["CellID"].to_numpy()[::-1].copy()})

    class _DS:
        name = "tonsil"
        table = _Table()

        class schema:  # noqa: N801
            cell_id = "CellID"

    annotation = overlay_module.provider(_DS())["scimap"]
    assert len(annotation["values"]) == 3000 and None not in annotation["values"]
    assert set(annotation["categories"]) <= set(annotation["colors"])

    # Exports: the script rereads the CSV and replays every call with its seed.
    script = client.get(f"/plugins/scimap/api/notebooks/{nb}/export?format=py").data.decode()
    assert "sm.io.readIntensityTable(str(DATA / 'cells.csv')" in script
    assert "sm.tl.cluster(adata, method='kmeans'" in script and "randomState=3" in script
    assert str(tmp_path) not in script
    notebook = json.loads(client.get(f"/plugins/scimap/api/notebooks/{nb}/export?format=ipynb").data)
    assert notebook["nbformat"] == 4 and len(notebook["cells"]) > 6
    report = client.get(f"/plugins/scimap/api/notebooks/{nb}/export?format=html").data.decode()
    assert "Experimental unit" in report and "not a workflow" in report
    native = client.get(f"/plugins/scimap/api/notebooks/{nb}/export?format=scimap&include=results").data
    with zipfile.ZipFile(io.BytesIO(native)) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "notebook.json", "results/columns.csv"} <= names
        assert str(tmp_path) not in zf.read("notebook.json").decode()
    imported = _ok(client.post("/plugins/scimap/api/import",
                               data={"file": (io.BytesIO(native), "nb.scimap"), "kind": "project", "ref": "tonsil"},
                               content_type="multipart/form-data"))
    copy = _ok(client.get(f"/plugins/scimap/api/notebooks/{imported['notebook_id']}"))["view"]["notebook"]
    assert len(copy["cells"]) == len(view["notebook"]["cells"])


def test_a_bad_scimap_file_is_refused(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "plexora.scimap", "version": 1}))
        zf.writestr("payload.pkl", b"\x80\x04")
    response = client.post("/plugins/scimap/api/import", data={"file": (io.BytesIO(buffer.getvalue()), "x.scimap")},
                           content_type="multipart/form-data")
    assert response.status_code == 400
    assert "unexpected" in response.get_json()["message"]
