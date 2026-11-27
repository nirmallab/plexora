"""The notebook's data facts and words: what decides which tools, words and defaults a notebook shows.

Each test pins something the sidebar would get wrong: a Visium HD table read as spots, a Visium scale guessed,
a person's correction ignored, a 400-gene panel guessed at, a scale Plexora made up.
"""

from __future__ import annotations

import json

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sparse

from plexora.plugins.scimap.server import facts, lexicon


def _table(path, *, n_vars=30, n_obs=50, tenx=None, counts=True):
    rng = np.random.default_rng(0)
    values = rng.poisson(1.0, (n_obs, n_vars)).astype("float32") if counts else \
        rng.gamma(2.0, 300.0, (n_obs, n_vars)).astype("float32")
    adata = ad.AnnData(X=sparse.csr_matrix(values), var=pd.DataFrame(index=[f"G{i}" for i in range(n_vars)]),
                       obs=pd.DataFrame({"X_centroid": rng.uniform(0, 500, n_obs),
                                         "Y_centroid": rng.uniform(0, 500, n_obs), "imageid": "s1"},
                                        index=[f"c{i}" for i in range(n_obs)]))
    adata.write_h5ad(path)
    if tenx is not None:
        with h5py.File(path, "r+") as handle:
            handle.attrs["plexora_tenx"] = json.dumps(tenx)
    facts.forget()
    return str(path)


HD_BINS = {"kind": "bins", "source": "/runs/hd/outs/binned_outputs/square_008um/filtered_feature_bc_matrix.h5",
           "scalefactors": {"bin_size_um": 8.0, "microns_per_pixel": 0.2738, "spot_diameter_fullres": 29.2}}
HD_CELLS = {"kind": "cells", "source": "/runs/hd/outs/segmented_outputs/filtered_feature_cell_matrix.h5",
            "scalefactors": {"microns_per_pixel": 0.2738}}
VISIUM = {"kind": "spots", "source": "/runs/v1/outs/filtered_feature_bc_matrix.h5",
          "scalefactors": {"spot_diameter_fullres": 110.0, "tissue_hires_scalef": 0.17}}
XENIUM = {"kind": "cells", "source": "/runs/x/outs/cell_feature_matrix.h5", "scalefactors": {}}


@pytest.mark.parametrize("record, expected", [
    (HD_BINS, {"resolution": "bin", "platform": "visium_hd", "bin_size_um": 8.0, "microns_per_unit": 0.2738}),
    (HD_CELLS, {"resolution": "cell", "platform": "visium_hd", "microns_per_unit": 0.2738}),
    # No microns_per_pixel in the record (before Space Ranger 2.1): none is derived; SCIMAP Pro reads the data.
    (VISIUM, {"resolution": "spot", "platform": "visium"}),
    (XENIUM, {"resolution": "cell", "platform": "xenium"}),
], ids=["hd-bins", "hd-cells", "visium", "xenium"])
def test_a_10x_conversion_says_what_one_row_is_and_its_scale(tmp_path, record, expected):
    found = facts.tenx_context(_table(tmp_path / "t.h5ad", tenx=record))
    assert found["modality"] == "spatialTranscriptomics" and found["coordinate_unit"] == "px"
    for key, value in expected.items():
        assert found[key] == pytest.approx(value), key
    if record is VISIUM:
        assert "microns_per_unit" not in found and "spot_diameter_um" not in found


def test_a_table_plexora_did_not_convert_says_nothing(tmp_path):
    assert facts.tenx_context(_table(tmp_path / "t.h5ad")) == {}
    assert facts.tenx_context(None) == {}


def test_the_persons_correction_wins_and_is_marked_as_theirs(tmp_path):
    path = _table(tmp_path / "t.h5ad", tenx=HD_BINS)
    context = facts.context_for({"type": "anndata", "table": path, "pixel_size_effective": 0.5},
                                override={"resolution": "cell", "modality": "nonsense"})
    assert context["resolution"] == "cell" and context["by"]["resolution"] == "user"
    assert context["platform"] == "visium_hd" and context["by"]["platform"] == "decision"
    # Space Ranger's own scale, not the project's pixel size, and no unrecognised correction.
    assert context["microns_per_unit"] == pytest.approx(0.2738)
    assert "nonsense" not in context.values()


def test_a_segmented_image_table_is_in_the_images_pixels():
    context = facts.context_for({"type": "csv", "table": "/data/cells.csv", "pixel_size_effective": 0.65})
    assert context["coordinate_unit"] == "px" and context["microns_per_unit"] == 0.65


def test_a_dataset_keeps_only_what_its_projects_share(tmp_path):
    one = _table(tmp_path / "a.h5ad", tenx=HD_BINS)
    two = _table(tmp_path / "b.h5ad", tenx={**HD_BINS, "scalefactors": {"bin_size_um": 16.0,
                                                                         "microns_per_pixel": 0.2738}})
    context = facts.context_for([{"type": "h5ad", "table": one}, {"type": "h5ad", "table": two}])
    assert context["resolution"] == "bin" and "bin_size_um" not in context


def test_before_prepare_the_records_decide_and_nothing_is_guessed(tmp_path):
    hd = _table(tmp_path / "hd.h5ad", tenx=HD_BINS)
    found = facts.detect(None, context=facts.context_for({"type": "h5ad", "table": hd}), n_features=18085)
    assert found["mode"] == "st-bin" and found["source"] == "records"
    assert found["chip"] == "Visium HD · 8 µm bins · 18,085 genes · µm"
    # 400 features and no record: Plexora guesses nothing from a count (SCIMAP Pro reads the data).
    panel = facts.detect(None, context={}, n_features=400)
    assert panel["mode"] is None and "modality" in panel["unknown"]
    assert panel["chip"] == "data type unknown · 400 features"


def test_after_prepare_the_working_file_is_read(tmp_path):
    path = _table(tmp_path / "work.h5ad", n_vars=1200, tenx=HD_CELLS)
    found = facts.detect(path, context=facts.context_for(table_path=path))
    assert found["mode"] == "st-cell" and found["platform"] == "visium_hd"
    assert found["n_features"] == 1200
    if found["source"] == "scimappro":
        assert found["raw_counts"] is True
    # Cached on the file: the same answer without reading it again.
    assert facts.detect(path, context=facts.context_for(table_path=path)) is found


def test_an_imaging_table_stays_imaging(tmp_path):
    path = _table(tmp_path / "img.h5ad", n_vars=20, counts=False)
    found = facts.detect(path, context={"modality": "imaging", "coordinate_unit": "px", "microns_per_unit": 0.65,
                                        "by": {"modality": "decision"}})
    assert found["mode"] == "imaging"
    assert found["chip"] == "multiplexed imaging · 20 markers · µm"


@pytest.mark.parametrize("mode, text", [
    ("imaging", "Rank markers per cell type, from intensities"),
    ("st-cell", "Rank genes per cell type, from counts"),
    ("st-spot", "Rank genes per cluster, from counts"),
    (None, "Rank markers per cell type, from intensities"),
])
def test_one_sentence_reads_right_for_each_kind_of_data(mode, text):
    assert lexicon.fill("Rank {features} per {identity}, from {values}", mode) == text


def test_the_lexicon_fills_a_whole_description_and_leaves_other_braces():
    tool = {"name": "Spatially variable {features}", "fields": [{"label": "{Unit} radius"}],
            "hint": "{not_a_word} stays", "n": 3}
    filled = lexicon.fill_all(tool, "st-bin")
    assert filled == {"name": "Spatially variable genes", "fields": [{"label": "Bin radius"}],
                      "hint": "{not_a_word} stays", "n": 3}
