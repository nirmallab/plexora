"""SpatialDataAdapter and its store-introspection helpers.

A SpatialData table is an AnnData, so SpatialDataAdapter inherits all of
AnnDataAdapter's resolution/validation behavior (covered by
test_anndata_adapter.py) and overrides only the read. These tests cover what
is genuinely new: locating one table inside a .zarr store, listing the
store's tables cheaply for the import form, and refusing anything that isn't
a plain table name.
"""

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp
import spatialdata as sd

from plexora.server.models.project import ColumnRoles, DataSpec
from plexora.server.models.adapters import SpatialDataAdapter, get_adapter
from plexora.server.models.adapters.anndata_adapter import DEFAULT_ID_COLUMN
from plexora.server.models.adapters.inspection import inspect_spatialdata_table
from plexora.server.models.adapters.spatialdata_adapter import (
    list_spatialdata_tables,
    list_table_layers,
    read_spatialdata_table,
    table_path,
)


def _make_adata(n=20, n_vars=4, image_ids=("only_image",)):
    rng = np.random.default_rng(0)
    obs_rows = []
    obs_names = []
    for image_id in image_ids:
        for i in range(n // len(image_ids)):
            obs_names.append(f"{image_id}_cell_{i}")
            obs_rows.append(image_id)
    total = len(obs_names)
    obs = pd.DataFrame(
        {
            "imageid": obs_rows,
            "cell_type": (["typeA", "typeB"] * ((total // 2) + 1))[:total],
        },
        index=obs_names,
    )
    var = pd.DataFrame(index=[f"protein_{i}" for i in range(n_vars)])
    adata = ad.AnnData(
        X=rng.random((total, n_vars)).astype(np.float32),
        obs=obs,
        var=var,
        layers={"protein": rng.random((total, n_vars)).astype(np.float32) * 10},
    )
    adata.obsm["spatial"] = np.stack(
        [np.arange(total, dtype=np.float64) * 5, np.arange(total, dtype=np.float64) * 3], axis=1
    )
    return adata


def _write_store(path, tables):
    sd.SpatialData(tables=tables).write(path)
    return path


def _feature_config(store, table, **read_spec):
    """A DataSpec for one table of a store, in the read-spec vocabulary these
    tests already use."""
    spec = {
        "coordinates": {"source": "obsm", "obsm_key": "spatial"},
        "features": {"source": "X"},
        "subset": {},
    }
    spec.update(read_spec)
    return DataSpec(
        type="spatialdata",
        src=str(store),
        table=spec.pop("table", table),
        coordinates=spec["coordinates"],
        features=spec["features"],
        subset=spec["subset"],
        obs_id_field=spec.get("obs_id_field"),
        roles=ColumnRoles(cell_id=spec.get("obs_id_field") or "id", x="X", y="Y"),
    )


def test_loads_the_named_table_and_ignores_the_others(tmp_path):
    store = _write_store(
        tmp_path / "s.zarr",
        {"cells": _make_adata(n=20, n_vars=4), "embeddings": _make_adata(n=8, n_vars=64)},
    )

    result = SpatialDataAdapter(_feature_config(store, "cells")).load_table()

    assert result.table.height == 20
    assert result.feature_columns == [f"protein_{i}" for i in range(4)]
    assert result.x_column == "X" and result.y_column == "Y"


def test_registered_for_the_spatialdata_data_type():
    assert get_adapter("spatialdata") is SpatialDataAdapter


def test_inherits_anndata_feature_and_subset_resolution(tmp_path):
    """The point of subclassing: layer selection and subsetting are not
    reimplemented, so they must work through the SpatialData reader too."""
    store = _write_store(
        tmp_path / "s.zarr", {"cells": _make_adata(n=30, image_ids=("img_a", "img_b", "img_c"))}
    )

    config = _feature_config(
        store,
        "cells",
        features={"source": "layer", "layer": "protein"},
        subset={"column": "imageid", "value": "img_b"},
    )
    result = SpatialDataAdapter(config).load_table()

    assert result.table.height == 10
    assert all(name.startswith("img_b_")
               for name in result.table[DEFAULT_ID_COLUMN].to_list())


def test_missing_table_name_is_rejected_at_construction(tmp_path):
    store = _write_store(tmp_path / "s.zarr", {"cells": _make_adata()})
    config = _feature_config(store, None)

    with pytest.raises(ValueError, match="needs a table"):
        SpatialDataAdapter(config)


def test_unknown_table_reports_the_store_and_table(tmp_path):
    store = _write_store(tmp_path / "s.zarr", {"cells": _make_adata()})

    with pytest.raises(ValueError, match="no table named 'nope'"):
        read_spatialdata_table(store, "nope")


@pytest.mark.parametrize("table", ["", "   ", None, ".", "..", "../tables", "a/b", "a\\b"])
def test_table_name_must_be_a_plain_name(tmp_path, table):
    """An empty name would otherwise resolve to the tables/ group itself --
    a real zarr group, so the read gets far enough to fail deep inside
    anndata with a confusing message. Separators are refused so a name
    arriving from a form post can't address anything outside tables/."""
    store = _write_store(tmp_path / "s.zarr", {"cells": _make_adata()})

    with pytest.raises(ValueError, match="Invalid SpatialData table name"):
        table_path(store, table)


def test_lists_every_table_with_its_shape(tmp_path):
    store = _write_store(
        tmp_path / "s.zarr",
        {"cells": _make_adata(n=20, n_vars=4), "embeddings": _make_adata(n=8, n_vars=64)},
    )

    assert list_spatialdata_tables(store) == [
        {"name": "cells", "n_obs": 20, "n_var": 4},
        {"name": "embeddings", "n_obs": 8, "n_var": 64},
    ]


def test_lists_a_tables_extra_matrices(tmp_path):
    """What the edit page offers a project whose layers were never recorded --
    read from zarr's group listing, so it costs a directory walk rather than
    materializing a second matrix."""
    store = _write_store(tmp_path / "s.zarr", {"cells": _make_adata()})

    assert list_table_layers(store, "cells") == ["protein"]


def test_a_table_with_nothing_to_choose_between_lists_no_matrices(tmp_path):
    """"No layers" is an ordinary answer to "is there anything to choose
    between here?", and so is an unreadable table -- neither is an error."""
    plain = _make_adata()
    del plain.layers["protein"]
    store = _write_store(tmp_path / "s.zarr", {"cells": plain})

    assert list_table_layers(store, "cells") == []
    assert list_table_layers(store, "nope") == []


def test_lists_shapes_for_sparse_and_x_less_tables(tmp_path):
    """Shapes come from zarr metadata rather than a read, so every on-disk
    encoding has to be handled: dense X is an array, sparse X is a group
    carrying a shape attr, and a table with no X falls back to obs/var."""
    dense = _make_adata(n=12, n_vars=3)
    sparse = _make_adata(n=12, n_vars=3)
    sparse.X = sp.csr_matrix(sparse.X)
    x_less = ad.AnnData(
        obs=pd.DataFrame(index=[f"cell_{i}" for i in range(12)]),
        var=pd.DataFrame(index=[f"protein_{i}" for i in range(3)]),
    )
    store = _write_store(
        tmp_path / "s.zarr", {"dense": dense, "sparse": sparse, "xless": x_less}
    )

    by_name = {t["name"]: t for t in list_spatialdata_tables(store)}

    assert by_name["dense"] == {"name": "dense", "n_obs": 12, "n_var": 3}
    assert by_name["sparse"] == {"name": "sparse", "n_obs": 12, "n_var": 3}
    assert by_name["xless"] == {"name": "xless", "n_obs": 12, "n_var": 3}


def test_listing_a_non_store_raises(tmp_path):
    plain = tmp_path / "not_a_store"
    plain.mkdir()

    with pytest.raises(ValueError, match="is not a zarr store"):
        list_spatialdata_tables(plain)


def test_store_without_tables_lists_empty(tmp_path):
    """A store may legitimately hold only images/labels/shapes -- distinct
    from 'not a store', which the form reports differently."""
    store = tmp_path / "s.zarr"
    sd.SpatialData().write(store)

    assert list_spatialdata_tables(store) == []


def test_inspection_matches_the_anndata_shape_plus_the_table(tmp_path):
    store = _write_store(tmp_path / "s.zarr", {"cells": _make_adata(n=20, n_vars=4)})

    inspection = inspect_spatialdata_table(store, "cells")

    assert inspection["data_type"] == "spatialdata"
    assert inspection["table"] == "cells"
    assert inspection["store"] == str(store)
    assert inspection["obs_count"] == 20
    assert inspection["n_var"] == 4
    assert inspection["obsm_keys"] == ["spatial"]
    assert inspection["layers"] == ["protein"]
    assert {c["name"] for c in inspection["obs_columns"]} == {"imageid", "cell_type"}


def test_single_image_identifier_does_not_trigger_the_ambiguity_warning(tmp_path):
    """A store exported per-image carries an imageid column with exactly one
    value; flagging that would put a misleading "spans multiple images"
    warning on ordinary single-image data."""
    store = _write_store(tmp_path / "s.zarr", {"cells": _make_adata(image_ids=("only_image",))})

    inspection = inspect_spatialdata_table(store, "cells")

    assert not any(c["likely_multi_image_identifier"] for c in inspection["obs_columns"])


def test_spatialdata_private_table_reader_is_still_importable():
    """read_spatialdata_table prefers spatialdata's own per-table reader so
    the whole store isn't materialized just to get one table. It's a
    leading-underscore symbol with a fallback behind it, so this pins the
    import: if a spatialdata upgrade moves it, that should surface here
    rather than silently degrading to the fallback path."""
    from spatialdata._io.io_table import _read_table

    assert callable(_read_table)
