"""The visium_hd panel over a STANDARD Visium run's 55 micron spots.

The same section as for HD bins, over a different drawing: a spot layer has
no store to build, its values are the rows of the sample's converted table
(`server/spots.py`), and it is drawn in the browser as circles
(`static/spotLayer.js`). What is pinned here is each seam that could hand the
panel a plausible wrong answer: the plugin claiming a sample it should not (or
missing one it should), a spot's total counting antibody UMIs, a gene's values
landing on the wrong spots, a radius in the wrong frame.

The run is the synthetic one `tests/test_visium_import.py` writes -- four
spots, three genes and an 'Antibody Capture' feature -- read back against the
dense matrix it was written from.
"""

import numpy as np
import pytest

import plexora
from plexora.api.plugin import Requires, layer_requirement, _layer_matches
from plexora.plugins.visium_hd import PLUGIN
from plexora.plugins.visium_hd.server import spots
from plexora.server.models import import_sample
from plexora.server.models.project import LayerSpec, Project
from plexora.server.utils import tenx_matrix

from tests import helpers
from tests.test_visium_import import (BARCODES, COUNTS, GENES, KINDS,
                                      SPOT_DIAMETER, write_visium_run)

EITHER = "visium_bins|visium_spots"


def _sample(*layers):
    project = helpers.project("demo")
    for layer in layers:
        project = project.with_layer(LayerSpec(**layer))
    return project


SPOT_LAYER = dict(id="spots", kind="points", modality="visium_spots",
                  label="Visium spots", render={"pointKind": "spot"})
BIN_LAYER = dict(id="bins", kind="points", modality="visium_bins",
                 label="Visium HD bins", render={"pointKind": "bin"})


# -- which samples the panel is for ------------------------------------------------

def test_either_modality_matches_an_alternation():
    spot, = [l for l in _sample(SPOT_LAYER).all_layers if l.id == "spots"]
    bins, = [l for l in _sample(BIN_LAYER).all_layers if l.id == "bins"]
    assert _layer_matches(spot, EITHER) and _layer_matches(bins, EITHER)
    assert not _layer_matches(spot, "visium_bins")
    tx = LayerSpec(id="tx", kind="points", modality="transcripts")
    assert not _layer_matches(tx, EITHER)


def test_a_sample_of_spots_alone_gets_the_panel_on_its_spots_layer():
    project = _sample(SPOT_LAYER)
    assert PLUGIN.requires.layers == (EITHER,)
    assert PLUGIN.requires.applies_to(project) is True
    assert PLUGIN.requires.first_layer(project).id == "spots"


def test_a_sample_of_hd_bins_still_gets_it_on_its_bins():
    project = _sample(BIN_LAYER)
    assert PLUGIN.requires.applies_to(project) is True
    assert PLUGIN.requires.first_layer(project).id == "bins"


def test_a_sample_of_neither_does_not():
    project = _sample(dict(id="tx", kind="points", modality="transcripts"))
    assert PLUGIN.requires.applies_to(project) is False
    assert PLUGIN.requires.first_layer(project) is None


def test_the_alternation_reads_as_words():
    """The requirements modal shows this label: `a|b` is two names, not one
    with a pipe in it -- and, once the layer is known, the one it is."""
    assert layer_requirement(EITHER).label == "Visium Bins or Visium Spots"
    building = LayerSpec(status="pending", **SPOT_LAYER)
    assert layer_requirement(EITHER, building).label == (
        "Visium Spots (still being prepared)")
    assert layer_requirement("transcripts").label == "Transcripts"


def test_a_spot_layer_still_preparing_is_waited_for_not_missing():
    project = _sample(dict(SPOT_LAYER, status="pending"))
    wants = Requires(layers=(EITHER,))
    assert wants.applies_to(project) is True
    assert [r.kind for r in wants.missing_from(project)] == ["layer"]


# -- reading the converted table -------------------------------------------------------

@pytest.fixture
def table(tmp_path):
    outs = write_visium_run(tmp_path / "run")
    layout = tenx_matrix.layout_for(outs / "filtered_feature_bc_matrix.h5")
    path = tenx_matrix.convert(layout, tmp_path / "out" / "spots.h5ad")
    spots._cache.clear()
    return path


def test_the_summary_is_the_table(table):
    found = spots.summary(table)
    assert found["genes"] == GENES
    assert found["feature_types"] == KINDS
    assert found["n_rows"] == len(BARCODES)
    assert len(found["x"]) == len(found["y"]) == len(BARCODES)


def test_a_spot_total_counts_gene_expression_only(table):
    """An antibody count is on another scale and would swamp the UMIs."""
    genes = np.array([k == spots.GENE_EXPRESSION for k in KINDS])
    expected = COUNTS[:, genes].sum(axis=1)
    found = spots.summary(table)
    assert np.array_equal(found["totals"], expected)
    assert found["total_count"] == pytest.approx(float(expected.sum()))
    assert found["total_count"] < COUNTS.sum()


def test_a_gene_is_its_matrix_column(table):
    fields = spots.values(table, ["KRT19", "CD4_TotalSeqC", "CD3E"])
    assert set(fields) == {"KRT19", "CD4_TotalSeqC", "CD3E"}
    for name, field in fields.items():
        assert field.dtype == np.float32
        assert np.array_equal(field, COUNTS[:, GENES.index(name)]), name


def test_an_unknown_name_is_left_out_not_zero_filled(table):
    fields = spots.values(table, ["NOPE", spots.TOTAL])
    assert set(fields) == {spots.TOTAL}
    assert spots.values(table, ["NOPE"]) == {}


def test_the_window_is_the_99th_percentile_of_the_non_empty():
    field = np.array([0, 0, 2, 4, 6, 8, 100], dtype=np.float32)
    assert spots.auto_window(field) == pytest.approx(
        np.percentile([2, 4, 6, 8, 100], 99))
    # Floor one: a gene seen once at 0.2 still saturates at a whole count.
    assert spots.auto_window([0, 0.2, 0.5]) == 1.0
    assert spots.auto_window(np.zeros(5)) == 1.0


# -- the routes, over a registered run ----------------------------------------------------

@pytest.fixture
def sample(tmp_path):
    outs = write_visium_run(tmp_path / "IF_run")
    spots._cache.clear()
    return import_sample.import_sample([str(outs)])["name"]


@pytest.fixture
def client(sample):
    return plexora.app.test_client()


def test_the_spots_route_is_the_vocabulary_and_positions(client, sample):
    body = client.get(
        f"/plugins/visium_hd/spots?datasource={sample}&layer=spots").get_json()
    assert body["status"] == "ready"
    assert body["genes"] == GENES
    assert body["spot_count"] == len(BARCODES)
    assert len(body["x"]) == len(body["y"]) == body["spot_count"]
    # In the layer's own full-res pixels: the transform scales it.
    assert body["radius"] == pytest.approx(SPOT_DIAMETER / 2)
    kinds = [body["feature_kinds"][c] for c in body["feature_codes"]]
    assert kinds == KINDS


def test_spot_values_are_arrays_and_windows(client, sample):
    body = client.get(f"/plugins/visium_hd/spot_values?datasource={sample}"
                      "&layer=spots&genes=CD3E,total").get_json()
    assert set(body["values"]) == {"CD3E", "total"}
    assert body["values"]["CD3E"] == pytest.approx(
        COUNTS[:, GENES.index("CD3E")].tolist())
    assert len(body["values"]["total"]) == len(BARCODES)
    assert body["windows"]["CD3E"] == pytest.approx(
        spots.auto_window(COUNTS[:, GENES.index("CD3E")]))


def test_a_layer_that_is_not_spots_has_no_spots(client, sample):
    body = client.get(f"/plugins/visium_hd/spots?datasource={sample}"
                      "&layer=__image__").get_json()
    assert body["status"] == "missing" and body["genes"] == []
    values = client.get(f"/plugins/visium_hd/spot_values?datasource={sample}"
                        "&layer=__image__&genes=CD3E")
    assert values.status_code == 404
    assert client.get("/plugins/visium_hd/spot_values?datasource=nope"
                      "&layer=spots").status_code == 404


def test_a_group_file_is_matched_against_the_spot_vocabulary(
        client, sample, tmp_path):
    path = tmp_path / "groups.csv"
    path.write_text("gene,group\ncd3e,T cells\nKRT19,Epithelium\n"
                    "MADEUP,Ghosts\n", encoding="utf-8")
    body = client.post("/plugins/visium_hd/groups",
                       data={"datasource": sample, "layer": "spots",
                             "path": str(path)}).get_json()
    assert body["groups"] == [{"name": "T cells", "genes": ["CD3E"]},
                              {"name": "Epithelium", "genes": ["KRT19"]}]
    assert body["unknown"] == ["MADEUP"]


# -- the page ------------------------------------------------------------------------------

def test_the_viewer_page_mounts_the_panel_on_the_spots_card(client, sample):
    from bs4 import BeautifulSoup

    response = client.get(f"/{sample}")
    assert response.status_code == 200
    page = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    scripts = [s.get("src") or "" for s in page.find_all("script")]
    assert any("/plugins/visium_hd/static/spotLayer.js" in s for s in scripts)
    mount = page.select_one('[data-layer-section="visium_hd"]')
    assert mount is not None
    assert mount["data-layer-body"] == "spots"
    assert mount["data-layer-modality"] == "visium_spots"
