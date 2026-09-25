"""The visium_hd plugin: its reader, its build, and its HTTP surface.

Built from a real 10x-shaped `.h5` (barcodes shuffled, CSC with barcodes as
columns), through core's job registry, and read back against the dense truth
the fixture wrote. The tiles themselves are core's route and are asserted
there too, because what matters is that the plugin's build produces a store
core serves -- not that each half works alone.
"""

import time

import h5py
import numpy as np
import pytest
import tifffile

import plexora
from plexora.plugins.visium_hd.server import routes, tenx
from plexora.server.models import bin_tiles, layer_jobs
from plexora.server.models.project import ImageSpec, LayerSpec, Project

from tests.helpers import use_data_root
from tests.tenx_fixtures import GRID_TO_FULLRES, write_visium_hd_run


@pytest.fixture
def project(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    outs, truth = write_visium_hd_run(tmp_path / "run", grid=40, levels=(2, 8))
    image = tmp_path / "he.ome.tif"
    tifffile.imwrite(image, np.zeros((1, 256, 256), dtype=np.uint16))
    level = truth["levels"][2]
    Project(
        name="hd",
        image=ImageSpec(src=str(image), width=256, height=256, max_level=1,
                        tile_width=256, tile_height=256,
                        channels=({"name": "c0", "fullname": "HE", "src": "/t/0"},)),
    ).with_layer(LayerSpec(
        id="bins", kind="points", label="Visium HD bins",
        src=str(level["dir"] / "filtered_feature_bc_matrix.h5"),
        modality="visium_bins", width=40, height=40,
        transform=GRID_TO_FULLRES, transform_source="run",
        render={"pointKind": "bin", "binMicrons": 2.0, "gridShape": [40, 40],
                "micronsPerPixel": 0.27},
        status="pending")).save()
    return {"name": "hd", "truth": truth, "level": level}


@pytest.fixture
def client(project):
    return plexora.app.test_client()


def _built(project):
    layer_jobs.register_builder("visium_bins", routes.build_layer,
                                stages=routes.BIN_STAGES)
    loaded = Project.load(project["name"])
    assert layer_jobs.start_builder(loaded, loaded.layer("bins"))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = layer_jobs.get(project["name"], "bins")
        if state and state["status"] != "pending":
            break
        time.sleep(0.05)
    assert state["status"] == "ready", state
    return bin_tiles.read_manifest(project["name"], "bins")


# -- the reader ------------------------------------------------------------------

def test_blocks_are_the_matrix_barcode_by_barcode(project):
    level = project["level"]
    source = level["dir"] / "filtered_feature_bc_matrix.h5"
    names, ids = tenx.read_features(source)
    assert names == project["truth"]["genes"]
    rows, cols, dense = [], [], []
    for brows, bcols, indptr, indices, data in tenx.iter_blocks(source, block=97):
        rows.append(brows)
        cols.append(bcols)
        block = np.zeros((len(brows), len(names)), dtype=np.int64)
        for i in range(len(brows)):
            block[i, indices[indptr[i]:indptr[i + 1]]] = data[indptr[i]:indptr[i + 1]]
        dense.append(block)
    rows, cols = np.concatenate(rows), np.concatenate(cols)
    assert np.array_equal(rows, level["rows"]) and np.array_equal(cols, level["cols"])
    expected = level["pooled"][:, level["rows"], level["cols"]].T
    assert np.array_equal(np.concatenate(dense), expected)
    assert tenx.block_count(source, block=97) == -(-len(rows) // 97)


def test_the_full_refit_recovers_the_planted_registration(project):
    transform, residual = tenx.refit_transform(project["level"]["dir"])
    assert np.allclose(transform, GRID_TO_FULLRES, atol=1e-6)
    assert residual < 1e-6


# -- the build ---------------------------------------------------------------------

def test_the_build_runs_through_core_and_the_layer_turns_ready(project):
    manifest = _built(project)
    assert Project.load("hd").layer("bins").status == "ready"
    assert manifest["columns"] == 40 and manifest["rows"] == 40
    assert manifest["genes"] == project["truth"]["genes"]
    assert manifest["gene_counts"][0] == int(project["truth"]["fine"][0].sum())
    stamp = manifest["built_ns"]
    # A second build of an unchanged matrix is a no-op.
    loaded = Project.load("hd")
    routes.build_layer(loaded, loaded.layer("bins"), lambda *_: None,
                       lambda *_: None)
    assert bin_tiles.read_manifest("hd", "bins")["built_ns"] == stamp


def test_core_serves_the_bin_tiles_the_build_wrote(client, project):
    _built(project)
    response = client.get(
        "/generated/layer/hd/bins/bins/0/0_0.png?color=ffffff&genes=INS"
        "&colors=ff0000&log=1")
    assert response.status_code == 200
    assert response.headers["ETag"].rstrip('"').split("-")[-1].isdigit()
    heat = client.get("/generated/layer/hd/bins/bins/0/0_0.png?color=ffffff"
                      "&ramp=viridis&bin=4")
    assert heat.status_code == 200
    assert heat.headers["ETag"] != response.headers["ETag"]


def _rgba(response):
    import io

    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(response.data)).convert("RGBA"))


def test_the_heatmap_aggregation_reaches_the_tiles(client, project):
    _built(project)
    url = ("/generated/layer/hd/bins/bins/0/0_0.png?color=ffffff"
           "&genes=INS,GCG&ramp=viridis")
    mean, peak, plain = (client.get(url + extra)
                         for extra in ("&agg=mean", "&agg=max", ""))
    assert peak.data != mean.data
    assert peak.headers["ETag"] != mean.headers["ETag"]
    assert plain.data == mean.data


def test_a_composition_tile_is_exact_gene_colours(client, project):
    _built(project)
    url = ("/generated/layer/hd/bins/bins/0/0_0.png?color=ffffff"
           "&genes=INS,GCG&colors=e61e1e,1ec83c&bin=4")
    composed = client.get(url + "&comp=0,1")
    assert composed.status_code == 200
    assert composed.mimetype == "image/png"
    pixels = _rgba(composed)
    opaque = pixels[pixels[..., 3] > 0][:, :3]
    assert len(opaque)
    allowed = np.array([[0xe6, 0x1e, 0x1e], [0x1e, 0xc8, 0x3c]])
    assert (opaque[:, None, :] == allowed[None]).all(2).any(1).all()
    blended = client.get(url)
    assert blended.mimetype == "image/webp"
    assert blended.headers["ETag"] != composed.headers["ETag"]
    grouped = client.get(url + "&comp=0|1:max")
    assert grouped.headers["ETag"] != composed.headers["ETag"]


@pytest.mark.parametrize("comp", ["9", "0,0", "0|1"])
def test_a_malformed_composition_is_a_400(client, project, comp):
    _built(project)
    response = client.get("/generated/layer/hd/bins/bins/0/0_0.png?color=ffffff"
                          f"&genes=INS,GCG&comp={comp}")
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_no_store_is_a_404_the_card_reads_as_preparing(client, project):
    response = client.get("/generated/layer/hd/bins/bins/0/0_0.png?color=ffffff")
    assert response.status_code == 404


# -- the routes -----------------------------------------------------------------------

def test_the_manifest_says_missing_until_built(client, project):
    body = client.get("/plugins/visium_hd/manifest?datasource=hd&layer=bins").get_json()
    assert body["status"] == "missing"
    _built(project)
    body = client.get("/plugins/visium_hd/manifest?datasource=hd&layer=bins").get_json()
    assert body["status"] == "ready"
    assert body["genes"][:2] == ["INS", "GCG"]
    assert body["supersample"] == bin_tiles.DEFAULT_SUPERSAMPLE
    assert body["revision"].startswith("b")


def test_stats_are_the_window_the_tiles_stretch_to(client, project):
    manifest = _built(project)
    body = client.get("/plugins/visium_hd/stats?datasource=hd&layer=bins"
                      "&genes=INS,total,NOPE&bin=4").get_json()
    assert set(body) == {"INS", "total"}
    stats = bin_tiles.read_stats("hd", "bins")
    assert body["INS"]["window"] == pytest.approx(
        bin_tiles.auto_window(manifest, stats, 0, 4))
    # Normalised as the tiles normalise it: 5 asks for 4.
    odd = client.get("/plugins/visium_hd/stats?datasource=hd&layer=bins"
                     "&genes=INS&bin=5").get_json()
    assert odd["INS"]["window"] == body["INS"]["window"]


def test_the_square_under_the_cursor(client, project):
    _built(project)
    fine = project["truth"]["fine"]
    body = client.get("/plugins/visium_hd/bin?datasource=hd&layer=bins"
                      "&x=33.5&y=22.1&genes=INS,GCG").get_json()
    assert body["inside"] and body["column"] == 33 and body["row"] == 22
    assert body["counts"] == {"INS": int(fine[0, 22, 33]),
                              "GCG": int(fine[1, 22, 33])}
    assert body["total"] == int(fine[:, 22, 33].sum())
    outside = client.get("/plugins/visium_hd/bin?datasource=hd&layer=bins"
                         "&x=400&y=2").get_json()
    assert outside == {"inside": False}


def test_state_round_trips_per_project(client, project):
    saved = {"selected": ["INS"], "colors": {"INS": "#ff0000"}, "binUm": 8}
    assert client.post("/plugins/visium_hd/state",
                       json={"datasource": "hd", **saved}).get_json()["saved"]
    assert client.get("/plugins/visium_hd/state?datasource=hd").get_json() == saved


def test_build_route_starts_the_one_job(client, project):
    layer_jobs.register_builder("visium_bins", routes.build_layer,
                                stages=routes.BIN_STAGES)
    response = client.post("/plugins/visium_hd/build",
                           json={"datasource": "hd", "layer": "bins"})
    assert response.status_code == 202
    assert response.get_json()["status"] in ("pending", "ready")
    missing = client.post("/plugins/visium_hd/build",
                          json={"datasource": "hd", "layer": "nope"})
    assert missing.status_code == 404
