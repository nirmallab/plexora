"""Reloading a project that is not the one loaded.

Saving an edit on a project's Edit page reloads it with `reload=True,
wait_for_table=True` (project_routes -> tool_routes._reload_or_restore), and
nothing makes that project the loaded one first: another project may be open,
or none at all right after a server start. That reload is one load of the
project, with its table read in place -- not a load that, half-way through,
loads the same project all over again inside itself and leaves it with no
cells.
"""

import threading

import numpy as np
import pandas as pd
import anndata as ad
import tifffile

from plexora.server.models import data_model, import_sample
from plexora.server.models.project import Project
from plexora.server.routes import tool_routes

from tests.helpers import use_data_root


def _image(path, size=256, channels=2, seed=0):
    tifffile.imwrite(path, np.random.default_rng(seed).integers(
        0, 3000, (channels, size, size)).astype(np.uint16))
    return path


def _cells(path, image, count):
    rng = np.random.default_rng(1)
    obs = pd.DataFrame({
        "imageid": pd.Categorical([image] * count),
        "X_centroid": rng.uniform(0, 250, count),
        "Y_centroid": rng.uniform(0, 250, count),
        "Area": rng.uniform(10, 50, count),
    }, index=[f"c{i}" for i in range(count)])
    adata = ad.AnnData(X=rng.gamma(2, 100, (count, 3)).astype(np.float32), obs=obs,
                       var=pd.DataFrame(index=["DNA", "CD3", "CD8"]))
    adata.obsm["spatial"] = obs[["X_centroid", "Y_centroid"]].to_numpy()
    adata.write_h5ad(path)
    return path


def _two_projects(tmp_path, monkeypatch, edited, open_one):
    """`edited` has 120 cells; `open_one` is image-only and is the one loaded."""
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    _image(tmp_path / "LSP21.ome.tif")
    _cells(tmp_path / "LSP21_cells.h5ad", "LSP21", 120)
    import_sample.import_sample([str(tmp_path / "LSP21.ome.tif"),
                                 str(tmp_path / "LSP21_cells.h5ad")], name=edited)
    _image(tmp_path / "LSP29.ome.tif", seed=1)
    import_sample.import_sample([str(tmp_path / "LSP29.ome.tif")], name=open_one)
    data_model.load_datasource(open_one)
    assert data_model.is_loaded(open_one)


def _counting_loads(monkeypatch):
    calls = []
    real = data_model.load_datasource

    def counted(name, *args, **kwargs):
        calls.append((name, args, kwargs))
        return real(name, *args, **kwargs)

    monkeypatch.setattr(data_model, "load_datasource", counted)
    return calls


def _join_tables():
    for thread in threading.enumerate():
        if thread.name.startswith("table-"):
            thread.join(20)


def test_saving_an_edit_to_a_project_not_open_leaves_it_loaded_with_its_cells(
        tmp_path, monkeypatch):
    """A table large enough to read behind the viewer: the save's reload reads
    it in place and validates it, and the project it leaves loaded serves
    those cells -- not none, with nothing pending and no error to say why."""
    _two_projects(tmp_path, monkeypatch, "edited_big", "open_big")
    monkeypatch.setattr(data_model, "BACKGROUND_TABLE_BYTES", 0)
    calls = _counting_loads(monkeypatch)

    error = tool_routes._reload_or_restore("edited_big", Project.find("edited_big"))
    _join_tables()

    assert error is None
    assert data_model.is_loaded("edited_big")
    frame = data_model.get_datasource_df()
    assert frame is not None and len(frame) == 120, (
        f"loaded with no cells, pending={data_model.table_pending('edited_big')}, "
        f"errors={dict(data_model._resource_errors)}")
    assert len(data_model.get_all_cells("edited_big", ["id"], int)) == 120
    assert [name for name, _, _ in calls] == ["edited_big"], (
        f"the reload loaded the project more than once: {calls}")


def test_reloading_a_project_not_open_reads_its_table_and_image_once(
        tmp_path, monkeypatch):
    """A small table: one load, so the table is read and the image opened
    once under the load lock, not twice."""
    _two_projects(tmp_path, monkeypatch, "edited_small", "open_small")
    calls = _counting_loads(monkeypatch)
    from plexora.server.providers import local

    reads = []
    real_read = local.LocalTableProvider.load

    def counted_read(self, *args, **kwargs):
        reads.append(1)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(local.LocalTableProvider, "load", counted_read)

    data_model.load_datasource("edited_small", reload=True, wait_for_table=True)

    assert data_model.is_loaded("edited_small")
    assert len(data_model.get_datasource_df()) == 120
    assert [name for name, _, _ in calls] == ["edited_small"], (
        f"the reload loaded the project more than once: {calls}")
    assert len(reads) == 1, f"the table was read {len(reads)} times"
