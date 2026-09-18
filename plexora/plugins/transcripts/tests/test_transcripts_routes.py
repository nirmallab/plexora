"""The transcripts plugin's HTTP surface.

Three routes, and the interesting assertions are about the line between this
plugin and core rather than about any one of them:

**The gene vocabulary is served from the derived manifest, never from /config.**
`/config` reaches the client on every viewer boot for every project the user has.
A 300-gene list inlined there would be downloaded on every page load of every
project, including the ones with no transcripts at all.

**Density is not served here.** It is a uint16 raster on core's layer route,
because it is a channel like any other -- which is what makes the low-zoom view
cost no new client rendering code and survive into Figure Builder's export.

**The wire format is binary.** JSON for two million points is 60 MB of ASCII and
a second of parsing, and the records are 10 bytes each, packed.
"""

import gzip

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import transcript_tiles as tt
from plexora.server.models.project import ImageSpec, LayerSpec, Project

from tests.helpers import use_data_root


GENES = ["EPCAM", "CD3E", "PTPRC"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image = tmp_path / "morphology.ome.tif"
    tifffile.imwrite(image, np.zeros((1, 256, 256), dtype=np.uint16))

    Project(
        name="xen",
        image=ImageSpec(src=str(image), width=256, height=256, max_level=1,
                        tile_width=256, tile_height=256,
                        channels=({"name": "c0", "fullname": "DAPI", "src": "/t/0"},)),
    ).with_layer(LayerSpec(id="tx", kind="points", label="Transcripts")).save()

    rng = np.random.default_rng(3)
    n = 600
    source = tmp_path / "transcripts.parquet"
    source.write_bytes(b"stand-in; only its size and mtime are read")
    tt.build("xen", "tx",
             genes=GENES,
             gene_index=rng.integers(0, len(GENES), size=n).astype(np.uint16),
             x=rng.uniform(0, 256, size=n).astype(np.float32),
             y=rng.uniform(0, 256, size=n).astype(np.float32),
             expected=tt.expected_manifest(source, width=256, height=256,
                                           tile_size=128, layer_id="tx"))
    return "xen"


@pytest.fixture
def client(project):
    return plexora.app.test_client()


def _points(response):
    raw = response.get_data()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return np.frombuffer(raw, dtype=tt.POINT_DTYPE)


# -- the manifest -----------------------------------------------------------

def test_the_manifest_carries_the_gene_vocabulary(client, project):
    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    assert body["status"] == "ready"
    assert body["genes"] == GENES
    assert body["point_count"] == 600


def test_the_manifest_carries_the_per_tile_counts(client, project):
    """What the client's LOD reads. Without them the viewer would have to fetch
    a tile to find out how many points are in it -- which is fetching the thing
    it is deciding whether to fetch."""
    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    counts = np.array(body["tile_counts"])
    assert counts.shape == (2, 2)
    assert counts.sum() == 600


def test_the_gene_list_is_not_in_the_config_route(client, project):
    """The rule, not an implementation detail. /config reaches the client on
    every viewer boot for every project; a vocabulary inlined there would be
    downloaded for projects that have no transcripts at all."""
    config = client.get("/config").get_json()
    entry = config["xen"]

    assert "EPCAM" not in str(entry)
    # The LAYER is there -- that is what the viewer needs to know exists.
    assert any(layer["id"] == "tx" for layer in entry["layers"])


def test_a_layer_with_no_cache_reports_missing(client, project):
    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=nope").get_json()

    assert body["status"] == "missing"
    assert body["genes"] == []


# -- points -----------------------------------------------------------------

def test_points_come_back_as_packed_binary(client, project):
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert response.status_code == 200
    assert response.mimetype == "application/octet-stream"
    assert int(response.headers["X-Transcript-Record-Count"]) == 600
    assert len(_points(response)) == 600


def test_points_can_be_restricted_to_some_genes(client, project):
    everything = _points(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256"))
    one = _points(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&genes=CD3E"
        "&minX=0&minY=0&maxX=256&maxY=256"))

    assert 0 < len(one) < len(everything)
    assert set(one["gene"].tolist()) == {GENES.index("CD3E")}


def test_a_gene_the_panel_does_not_have_draws_nothing(client, project):
    """Nothing, not everything. The user asked for specific genes; drawing the
    whole panel instead is the opposite of what they asked for."""
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&genes=NOT_A_GENE"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert int(response.headers["X-Transcript-Record-Count"]) == 0


def test_a_known_gene_beside_an_unknown_one_still_draws(client, project):
    """A saved view naming a gene a re-imported run no longer carries should
    draw the rest."""
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&genes=CD3E,NOT_A_GENE"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert int(response.headers["X-Transcript-Record-Count"]) > 0


def test_a_viewport_restricts_what_comes_back(client, project):
    everything = int(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256").headers["X-Transcript-Record-Count"])
    corner = int(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=100&maxY=100").headers["X-Transcript-Record-Count"])

    assert corner < everything


def test_a_truncated_response_says_so(client, project):
    """A sample drawn as though it were everything reads as "this gene is not
    expressed here", which is the one wrong conclusion to invite."""
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&max=50"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert response.headers["X-Transcript-Truncated"] == "1"
    assert int(response.headers["X-Transcript-Record-Count"]) <= 50


def test_an_untruncated_response_says_that_too(client, project):
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert response.headers["X-Transcript-Truncated"] == "0"


def test_a_layer_with_no_cache_serves_no_points(client, project):
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=nope"
        "&minX=0&minY=0&maxX=10&maxY=10")

    assert response.status_code == 200
    assert int(response.headers["X-Transcript-Record-Count"]) == 0


# -- build ------------------------------------------------------------------

def test_building_needs_a_datasource_and_a_source(client, project):
    assert client.post("/plugins/transcripts/build", json={}).status_code == 400


def test_building_for_an_unknown_project_is_a_404(client, project):
    response = client.post("/plugins/transcripts/build",
                           json={"datasource": "ghost", "source": "/x.parquet"})

    assert response.status_code == 404


def test_status_reports_a_layer_that_is_already_built(client, project):
    body = client.get("/plugins/transcripts/status?datasource=xen&layer=tx").get_json()

    assert body["status"] == "ready"


def test_status_reports_a_layer_that_was_never_built(client, project):
    body = client.get("/plugins/transcripts/status?datasource=xen&layer=nope").get_json()

    assert body["status"] == "missing"
