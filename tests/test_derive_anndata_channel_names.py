"""Unit tests for the channel-name derivation tiers used when registering an
AnnData datasource without an explicit channel_names override: embedded
OME-XML metadata, then adata.var_names, then adata.uns['all_markers'], then a
generic fallback -- each only accepted if its length matches the image's
actual channel count.
"""

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import tifffile

from plexora.datasource import derive_anndata_channel_names, register_anndata_datasource


def _write_image(path, channels=3, size=32, ome_channel_names=None, ome_without_names=False):
    data = np.zeros((channels, size, size), dtype=np.uint8)
    if ome_channel_names is not None:
        tifffile.imwrite(path, data, ome=True, metadata={"Channel": {"Name": ome_channel_names}})
    elif ome_without_names:
        # A real OME-TIFF whose Channel elements simply have no Name
        # attribute set -- ome_types parses each .name as None, unlike a
        # plain (non-OME) TIFF which has no Channel metadata at all.
        tifffile.imwrite(path, data, ome=True)
    else:
        tifffile.imwrite(path, data)


def _write_adata(path, var_names, n=10, all_markers=None):
    obs = pd.DataFrame(index=[f"cell_{i}" for i in range(n)])
    var = pd.DataFrame(index=var_names)
    x = np.random.default_rng(0).random((n, len(var_names))).astype(np.float32)
    adata = ad.AnnData(X=x, obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 200, n, dtype=np.float64), np.linspace(10, 200, n, dtype=np.float64)], axis=1
    )
    if all_markers is not None:
        adata.uns["all_markers"] = np.array(all_markers)
    adata.write_h5ad(path)


def test_prefers_var_names_over_embedded_ome_metadata_when_both_match_length(tmp_path):
    # OME-XML and var_names disagree on text but agree on count -- gating
    # always matches by var_names, so var_names must win: the two are
    # assumed to describe the same channels in the same order (linked by
    # index), not compared by text.
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=3, ome_channel_names=["DAPI", "CD3", "CD8"])
    _write_adata(h5ad_path, var_names=["MarkerA", "MarkerB", "MarkerC"])

    names, source = derive_anndata_channel_names(image_path, h5ad_path, n_channels=3)

    assert names == ["MarkerA", "MarkerB", "MarkerC"]
    assert source == "adata.var_names"


def test_falls_back_to_ome_metadata_when_var_names_length_mismatches(tmp_path):
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=3, ome_channel_names=["DAPI", "CD3", "CD8"])
    # 2 var_names (e.g. QC-filtered panel) but 3 real acquisition channels.
    _write_adata(h5ad_path, var_names=["MarkerA", "MarkerB"])

    names, source = derive_anndata_channel_names(image_path, h5ad_path, n_channels=3)

    assert names == ["DAPI", "CD3", "CD8"]
    assert source == "image metadata"


def test_falls_back_to_var_names_when_ome_metadata_absent(tmp_path):
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=2)  # plain TIFF, no OME-XML at all
    _write_adata(h5ad_path, var_names=["MarkerA", "MarkerB"])

    names, source = derive_anndata_channel_names(image_path, h5ad_path, n_channels=2)

    assert names == ["MarkerA", "MarkerB"]
    assert source == "adata.var_names"


def test_falls_back_to_var_names_when_ome_metadata_incomplete(tmp_path):
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    # OME-XML with 3 channel entries but no names set -- incomplete, must be rejected.
    _write_image(image_path, channels=3, ome_without_names=True)
    _write_adata(h5ad_path, var_names=["MarkerA", "MarkerB", "MarkerC"])

    names, source = derive_anndata_channel_names(image_path, h5ad_path, n_channels=3)

    assert names == ["MarkerA", "MarkerB", "MarkerC"]
    assert source == "adata.var_names"


def test_deduplicates_var_names(tmp_path):
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=2)
    _write_adata(h5ad_path, var_names=["MarkerA", "MarkerA"])

    names, source = derive_anndata_channel_names(image_path, h5ad_path, n_channels=2)

    assert names == ["MarkerA", "MarkerA_1"]
    assert source == "adata.var_names"


def test_falls_back_to_all_markers_when_var_names_count_mismatches(tmp_path):
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=3)
    # 2 var_names (e.g. QC-filtered panel) but 3 real acquisition channels.
    _write_adata(
        h5ad_path,
        var_names=["MarkerA", "MarkerB"],
        all_markers=["DAPI", "CD3", "CD8"],
    )

    names, source = derive_anndata_channel_names(image_path, h5ad_path, n_channels=3)

    assert names == ["DAPI", "CD3", "CD8"]
    assert source == "adata.uns['all_markers']"


def test_falls_back_to_generic_names_when_nothing_matches(tmp_path):
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=3)
    _write_adata(
        h5ad_path,
        var_names=["MarkerA", "MarkerB"],
        all_markers=["DAPI", "CD3"],  # also wrong length -- must be rejected too
    )

    names, source = derive_anndata_channel_names(image_path, h5ad_path, n_channels=3)

    assert names == ["Channel 1", "Channel 2", "Channel 3"]
    assert source == "generic"


def test_register_rejects_explicit_channel_names_with_wrong_length(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    h5ad_path = tmp_path / "cells.h5ad"
    _write_image(image_path, channels=2)
    _write_adata(h5ad_path, var_names=["MarkerA", "MarkerB"])

    with pytest.raises(ValueError, match="channel_names has 1 entries but the image has 2 channels"):
        register_anndata_datasource(
            name="mismatched",
            image=image_path,
            features=h5ad_path,
            data_dir=data_dir,
            channel_names=["OnlyOne"],
        )
