"""The right-click pick on a flat project, and the cell index's disk cache
(data_model.load_ball_tree / query_for_closest_cell).

A 2-D project's pick is the nearest cell anywhere, exactly as it was with the
sklearn BallTree; the index is cached under the project's derived directory
with a manifest of what it was built from, and rebuilt when any of that
changes.
"""

import json

import numpy as np
import polars as pl
import pytest
import tifffile

from plexora import paths
from plexora.server.models import data_model
from tests.helpers import use_data_root

_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


def _register(tmp_path, name, xs, ys):
    from plexora import datasource as datasource_module

    image = tmp_path / f"{name}.tif"
    tifffile.imwrite(image, np.zeros((1, 64, 64), dtype=np.uint8))
    table = tmp_path / f"{name}.csv"
    pl.DataFrame({"CellID": np.arange(len(xs), dtype=np.uint32),
                  "X_centroid": np.asarray(xs, dtype=np.float64),
                  "Y_centroid": np.asarray(ys, dtype=np.float64)}).write_csv(table)
    datasource_module.register_datasource(
        name=name, image=image, features=table, x="X_centroid", y="Y_centroid",
        segmentation=None, data_dir=tmp_path / "data")
    return table


@pytest.fixture
def root(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    return tmp_path


def _builds(monkeypatch):
    from plexora.server.utils import spatial_index

    built = []
    real = spatial_index.SpatialIndex.build.__func__

    def counted(cls, *args, **kwargs):
        built.append(1)
        return real(cls, *args, **kwargs)

    monkeypatch.setattr(spatial_index.SpatialIndex, "build", classmethod(counted))
    return built


def test_a_flat_pick_is_the_nearest_cell_anywhere(root):
    rng = np.random.default_rng(3)
    xs, ys = rng.uniform(0, 60, 300), rng.uniform(0, 60, 300)
    _register(root, "flat", xs, ys)
    for qx, qy in rng.uniform(-30, 90, (40, 2)):
        found = data_model.query_for_closest_cell(qx, qy, "flat")
        assert found["CellID"] == int(np.argmin(np.hypot(xs - qx, ys - qy)))


def test_a_project_loaded_directly_is_picked_against_its_own_cells(root):
    """`load_datasource` without the index (the edit page, the requirements
    modal) used to leave the previous project's tree in place."""
    _register(root, "a", [5.0, 6.0], [5.0, 6.0])
    _register(root, "b", [50.0, 51.0, 52.0], [50.0, 51.0, 52.0])
    assert data_model.query_for_closest_cell(50, 50, "a")["CellID"] == 1
    data_model.load_datasource("b")
    assert data_model.query_for_closest_cell(50, 50, "b")["CellID"] == 0
    assert len(data_model.get_current_ball_tree()) == 3


def _cache(name):
    return paths.derived_root(name) / data_model.SPATIAL_INDEX_DIR


def test_the_index_is_cached_and_read_back_without_a_build(root, monkeypatch):
    _register(root, "cached", [1.0, 9.0], [1.0, 9.0])
    data_model.load_ball_tree("cached")
    assert (_cache("cached") / "manifest.json").is_file()
    built = _builds(monkeypatch)
    data_model.load_datasource("cached", reload=False)
    data_model.ball_tree = None
    data_model.load_ball_tree("cached")
    assert built == [] and len(data_model.ball_tree) == 2


def test_a_changed_manifest_rebuilds(root, monkeypatch):
    _register(root, "stale", [1.0, 9.0], [1.0, 9.0])
    data_model.load_ball_tree("stale")
    manifest = _cache("stale") / "manifest.json"
    built = _builds(monkeypatch)
    for key, value in (("version", -1), ("x_column", "X"), ("rows", 99)):
        current = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**current, key: value}))
        built.clear()
        data_model.ball_tree = None
        data_model.load_ball_tree("stale")
        assert built == [1], key


def test_the_legacy_ball_tree_pickle_is_removed_and_never_read(root):
    _register(root, "legacy", [1.0, 9.0], [1.0, 9.0])
    legacy = paths.derived_root("legacy") / "ball_tree.pickle"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"not a pickle")
    found = data_model.query_for_closest_cell(8, 8, "legacy")
    assert found["CellID"] == 1
    assert not legacy.exists()


def test_the_route_answers_as_before_without_planes(root):
    import plexora

    _register(root, "route", [1.0, 9.0], [1.0, 9.0])
    client = plexora.app.test_client()
    got = client.get("/get_nearest_cell?point_x=8&point_y=8&datasource=route").get_json()
    assert got["CellID"] == 1
    bad = client.get("/get_nearest_cell?point_x=8&point_y=8&datasource=route&z0=a&z1=1")
    assert bad.status_code == 400
