"""A notebook's z-stack array, served one plane at a time from the kernel.

`register_memory_datasource(name, array, axes="czyx", z_step=...)` snapshots
the array onto the kernel's data node; the viewer then moves through its
planes and projections exactly as for a file. Each plane is checked against
the array it came from, because a wrong plane is a picture with no error.
"""

import io

import numpy as np
import pytest

import plexora
from plexora import memory as memory_api
from plexora.server.models.project import Project
from plexora.server.volume.sources import memory as memory_volume

QUIET = {"log": lambda *args, **kwargs: None}


@pytest.fixture
def client():
    return plexora.app.test_client()


def _stack(channels=2, depth=6, height=300, width=400):
    """Plane z of channel c is c*1000 + z*100 plus a small texture, so every
    plane and channel is told apart by its value."""
    rng = np.random.default_rng(3)
    texture = rng.integers(0, 50, (height, width))
    out = np.empty((channels, depth, height, width), dtype=np.uint16)
    for c in range(channels):
        for z in range(depth):
            out[c, z] = c * 1000 + z * 100 + texture
    return out


def _hd(response):
    from PIL import Image

    assert response.status_code == 200, response.get_data(as_text=True)
    return np.asarray(Image.open(io.BytesIO(response.data)))


@pytest.mark.parametrize("axes, ndim, expected", [
    (None, 3, "cyx"), (None, 4, "czyx"), (None, 5, "tczyx"), ("ZYX", 3, "zyx"),
    ("zcyx", 4, "zcyx"),
])
def test_axes_default_and_normalise(axes, ndim, expected):
    assert memory_volume.normalize_axes(axes, ndim) == expected


@pytest.mark.parametrize("axes, ndim", [("cyx", 4), ("czxy", 4), ("ccyx", 4), ("qzyx", 4)])
def test_bad_axes_are_refused(axes, ndim):
    with pytest.raises(ValueError):
        memory_volume.normalize_axes(axes, ndim)


def test_a_channel_stack_stays_one():
    assert memory_volume.volume_from_array(np.zeros((3, 32, 32), np.uint16)) is None


def test_the_source_reads_the_callers_array_without_copying():
    stack = _stack()
    source = memory_volume.volume_from_array(stack, "czyx", z_step=2.0)
    assert (source.volume.depth, source.volume.channels) == (6, 2)
    assert source.volume.default_view.token() == "z3"
    assert source.volume.spacing.z.value == 2.0
    assert np.shares_memory(source._array, stack)
    np.testing.assert_array_equal(
        source.read_plane(0, 0, 4, 1, slice(0, 8), slice(0, 8)), stack[1, 4, :8, :8])
    # zcyx is the same data in another order.
    swapped = memory_volume.volume_from_array(np.moveaxis(stack, 1, 0), "zcyx")
    np.testing.assert_array_equal(
        swapped.read_plane(0, 0, 4, 1, slice(0, 8), slice(0, 8)), stack[1, 4, :8, :8])


def test_a_z_stack_array_is_served_plane_by_plane(plexora_data_root, client):
    stack = _stack()
    project = memory_api.register_memory_datasource(
        "mem3d", stack, axes="czyx", z_step=2.0, pixel_size=0.5,
        channel_names=["DNA", "CD8"], data_dir=plexora_data_root, **QUIET)
    project = Project.find("mem3d")
    assert project.image.num_channels == 2
    assert project.image.depth == 6
    info = client.get("/volume/mem3d/info").get_json()
    assert info["volumetric"] is True and info["depth"] == 6
    assert info["spacing"]["z"]["value"] == 2.0

    src = next(entry["src"] for entry in project.image.channels
               if entry["fullname"] == "CD8")
    tile = {view: _hd(client.get(f"{src}0/0_0.png?q=hd" + (f"&view={view}" if view else "")))
            for view in ("", "z0", "z5", "z0-5max", "z1-2mean")}
    np.testing.assert_array_equal(tile[""], stack[1, 3])
    np.testing.assert_array_equal(tile["z0"], stack[1, 0])
    np.testing.assert_array_equal(tile["z5"], stack[1, 5])
    np.testing.assert_array_equal(tile["z0-5max"], stack[1].max(axis=0))
    np.testing.assert_array_equal(
        tile["z1-2mean"], np.rint(stack[1, 1:3].astype(np.float64).mean(axis=0)))
