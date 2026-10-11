"""A z-stack at a web address: an OME-TIFF or an OME-Zarr, served plane by plane.

The remote provider used to skip the volume layer, so a stack on a web
server read as its middle plane (zarr) or as planes folded into channels
(TIFF). Served here by a real HTTP server (tests/remote_fixtures.py), and
checked plane by plane against the arrays written.
"""

import io

import numpy as np
import pytest

import plexora
from plexora.datasource import register_image_datasource
from tests import ngff_fixtures as nf
from tests import volume_fixtures as vf
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "served"
    root.mkdir()
    return root


def _hd(response):
    from PIL import Image

    assert response.status_code == 200, response.get_data(as_text=True)
    return np.asarray(Image.open(io.BytesIO(response.data)))


def _views(client, entry, stack):
    """Every view of channel 1 against `stack` (C, Z, Y, X)."""
    src = entry["imageData"][1]["src"]
    depth = stack.shape[1]
    tiles = {view: _hd(client.get(f"{src}0/0_0.png?q=hd" + (f"&view={view}" if view else "")))
             for view in ("", "z0", f"z{depth - 1}", f"z0-{depth - 1}max")}
    np.testing.assert_array_equal(tiles[""], stack[1, depth // 2])
    np.testing.assert_array_equal(tiles["z0"], stack[1, 0])
    np.testing.assert_array_equal(tiles[f"z{depth - 1}"], stack[1, depth - 1])
    np.testing.assert_array_equal(tiles[f"z0-{depth - 1}max"], stack[1].max(axis=0))


def test_a_remote_ome_tiff_stack_is_a_volume(served, http_store, client):
    _, data = vf.write_ome_volume(served / "stack.ome.tif", axes="CZYX", channels=2,
                                  depth=5, height=96, width=80, names=["DNA", "CD8"])
    server = http_store()
    entry = register_image_datasource("rt", server.url("stack.ome.tif"))
    assert entry["num_channels"] == 2
    assert entry["imageShape"] == {"t": 1, "c": 2, "z": 5, "y": 96, "x": 80}
    assert entry["imageZSemantics"] == "depth"
    info = client.get("/volume/rt/info").get_json()
    assert info["volumetric"] is True and info["depth"] == 5
    assert info["spacing"]["z"]["value"] == pytest.approx(2.0)
    _views(client, entry, data[0])


def test_a_remote_stack_without_a_z_step_still_folds_into_cycles(served, http_store):
    vf.write_ome_volume(served / "cyc.ome.tif", axes="CZYX", channels=2, depth=4,
                        height=64, width=64, physical_z=None)
    entry = register_image_datasource("rc", http_store().url("cyc.ome.tif"))
    assert entry["num_channels"] == 8
    assert "imageShape" not in entry


def test_a_remote_ome_zarr_stack_is_a_volume(served, http_store, client):
    import zarr

    path = nf.write_ngff(served / "vol.zarr", shape=(1, 2, 7, 96, 80), levels=1,
                         axes="tczyx")
    stack = np.asarray(zarr.open_group(str(path), mode="r")["0"])[0]
    entry = register_image_datasource("rz", http_store().url("vol.zarr"))
    info = client.get("/volume/rz/info").get_json()
    assert info["volumetric"] is True and info["depth"] == 7
    _views(client, entry, stack)
