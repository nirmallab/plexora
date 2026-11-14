"""The cached cell index must follow how the table is read, not just the file.

An AnnData project's positions are the file PLUS its read spec: which obsm
array holds them (`coordinates`), at which scale. Changing that leaves the
.h5ad, its size and mtime, and the adapter's synthesized 'X'/'Y' role names
exactly as they were. A cell index cached under a manifest of those alone is
read back after the change, and a pick then answers with the cell that was
nearest in the OLD positions.
"""

from dataclasses import replace

import numpy as np
import pytest
import tifffile

from plexora import _work
from plexora.server.models import data_model
from plexora.server.models.project import Project
from tests.helpers import ALL_CONFIRMED, anndata_spec, use_data_root

_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")

_N = 300


@pytest.fixture
def root(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    monkeypatch.setattr(data_model, "_warm_datasource_caches", lambda name: None)
    return tmp_path


def _register(root, name):
    """An .h5ad whose `X_umap` and `spatial` arrays have the same shape and
    unrelated contents, so a pick in one space lands on another cell in the
    other."""
    import anndata as ad

    from plexora import datasource as datasource_module

    rng = np.random.default_rng(0)
    spatial = np.column_stack([rng.uniform(0, 80, _N), rng.uniform(0, 96, _N)])
    umap = np.column_stack([rng.uniform(0, 80, _N), rng.uniform(0, 96, _N)])
    adata = ad.AnnData(X=rng.uniform(0, 10, (_N, 2)).astype(np.float32))
    adata.var_names = ["DNA", "CD3"]
    adata.obsm["spatial"] = spatial.astype(np.float32)
    adata.obsm["X_umap"] = umap.astype(np.float32)
    h5ad = root / f"{name}.h5ad"
    adata.write_h5ad(h5ad)
    image = root / f"{name}.tif"
    tifffile.imwrite(image, np.zeros((2, 96, 80), dtype=np.uint16), photometric="minisblack")
    datasource_module.register_image_datasource(name, image=image, data_dir=root / "data")
    return h5ad, adata.obsm["spatial"].astype(np.float64), adata.obsm["X_umap"].astype(np.float64)


def _read_positions_from(name, h5ad, obsm_key):
    spec = anndata_spec(h5ad, coordinates={"source": "obsm", "obsm_key": obsm_key},
                        markers=("DNA", "CD3"))
    Project.mutate(name, lambda p: replace(p, dataset=spec, confirmed=tuple(ALL_CONFIRMED)))


def _misses(name, spatial, umap):
    """Clicks exactly on a cell's real position that answer another cell:
    (asked, answered, the cell whose UMAP point is nearest the click)."""
    wrong = []
    for cell in range(0, _N, 15):
        x, y = spatial[cell]
        found = data_model.query_for_closest_cell(float(x), float(y), name)
        if found["id"] != cell:
            wrong.append((cell, found["id"],
                          int(np.argmin(np.hypot(*(umap - spatial[cell]).T)))))
    return wrong


def test_on_the_website_a_pick_after_correcting_the_obsm_key_is_the_cell_clicked(
        root, monkeypatch):
    """The website builds no index on a reload (`_work.threaded()` is false);
    the next pick builds or reads one, and must not read the one built over the
    previous coordinates."""
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    _work._reset_for_tests()
    h5ad, spatial, umap = _register(root, "web_obsm")

    _read_positions_from("web_obsm", h5ad, "X_umap")
    data_model.load_datasource("web_obsm", reload=True)
    assert data_model.spatial_index("web_obsm") is not None  # built and cached

    # The user corrects which array holds the positions (the requirements
    # modal stores the spec and reloads: tool_routes._reload_or_restore).
    _read_positions_from("web_obsm", h5ad, "spatial")
    data_model.load_datasource("web_obsm", reload=True)
    _work.run_pending()
    assert np.allclose(data_model.datasource["X"].to_numpy(), spatial[:, 0], atol=1e-3)

    wrong = _misses("web_obsm", spatial, umap)
    _work._reset_for_tests()
    assert wrong == [], (
        "picks answered the cell nearest in the old (X_umap) positions: "
        f"(asked, answered, nearest in X_umap) = {wrong[:5]}")


def test_a_first_load_after_the_spec_changed_elsewhere_picks_the_cell_clicked(root):
    """Desktop: the spec was changed while this process did not have the
    project loaded (another process sharing the library, a notebook). The next
    open is not a reload, and must not read the index of the old positions."""
    h5ad, spatial, umap = _register(root, "elsewhere_obsm")
    _read_positions_from("elsewhere_obsm", h5ad, "X_umap")
    data_model.load_ball_tree("elsewhere_obsm")  # built and cached
    assert data_model.ball_tree is not None

    # Another project is opened, and meanwhile the spec is corrected elsewhere.
    _register(root, "other_obsm")
    _read_positions_from("other_obsm", root / "other_obsm.h5ad", "spatial")
    data_model.load_ball_tree("other_obsm")
    _read_positions_from("elsewhere_obsm", h5ad, "spatial")

    wrong = _misses("elsewhere_obsm", spatial, umap)
    assert wrong == [], (
        "picks answered the cell nearest in the old (X_umap) positions: "
        f"(asked, answered, nearest in X_umap) = {wrong[:5]}")
