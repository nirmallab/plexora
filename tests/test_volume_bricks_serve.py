"""Bricks for the 3-D view's atlas (server/volume/brick_serve.py, /volume/<ds>/bricks).

What has to hold: the grid covers every level that keeps every plane, with
planes pooled so voxels stay about as deep as wide; a brick's bytes are the
stack's voxels there -- its apron the neighbours', the stack's edge repeated
past its ends, max-pooled planes where the level pools -- quantized with the
window asked for; a brick at or below the window's floor is sent as empty;
and the address is a cache key: a request naming every window is kept, and
asked again it is not modified.
"""

import json
import struct

import numpy as np
import pytest

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.volume import brick_serve, serve
from tests import volume_fixtures as vf


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture(autouse=True)
def _fresh_cache():
    brick_serve.clear()
    yield
    brick_serve.clear()


def _stack(tmp_path, name="bricks", physical_z=2.0, depth=8, height=128, width=112):
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                     depth=depth, height=height, width=width, levels=2,
                                     names=["DNA", "CD3"], physical_z=physical_z)
    register_image_datasource(name, path)
    return data[0]                                    # (C, Z, Y, X)


def _parse(body):
    (length,) = struct.unpack("<I", body[:4])
    header = json.loads(body[4:4 + length])
    return header, body[4 + length:]


def _reference(volume, at, pool=1):
    """One brick with its apron, from numpy: pooled planes, edges repeated."""
    bz, by, bx = brick_serve.BRICK
    if pool > 1:
        z = volume.shape[0]
        short = -z % pool
        if short:
            volume = np.concatenate([volume, np.repeat(volume[-1:], short, axis=0)])
        volume = volume.reshape(-1, pool, *volume.shape[1:]).max(axis=1)
    padded = np.pad(volume, ((1, bz + 1), (1, by + 1), (1, bx + 1)), mode="edge")
    z, y, x = at
    return padded[z * bz:z * bz + bz + 2, y * by:y * by + by + 2, x * bx:x * bx + bx + 2]


def test_the_manifest_grids_every_level(tmp_path, client):
    _stack(tmp_path)
    body = client.get("/volume/bricks/bricks/manifest").get_json()
    assert body["brick"] == list(brick_serve.BRICK) and body["apron"] == 1
    first, second = body["levels"]
    assert first == {"level": 0, "pool": 1, "shape": [8, 128, 112], "grid": [1, 2, 2],
                     "brick_full": [32, 64, 64]}
    assert second["level"] == 1 and second["grid"] == [1, 1, 1]
    assert second["brick_full"] == [32, 128, 128]


def test_planes_are_pooled_where_they_are_thinner_than_a_level_pixel(tmp_path):
    """0.5 µm pixels, 0.25 µm planes: level 0's pixel is two planes deep."""
    from plexora.server.models import views

    _stack(tmp_path, "thin", physical_z=0.25)
    from plexora.server.models import data_model

    data_model.ensure_loaded("thin")
    volume = views.current_source("thin").volume
    assert brick_serve.pool_for(volume, 0) == 2 and brick_serve.pool_for(volume, 1) == 4
    assert [e["shape"][0] for e in brick_serve.levels(volume)] == [4, 2]


def test_a_brick_is_the_stacks_voxels_with_its_neighbours_round_it(tmp_path, client):
    data = _stack(tmp_path)
    response = client.get("/volume/bricks/bricks?level=0&format=u8&at=0.0.1,0.1.0"
                          "&channel=CD3&w=0,1000&channel=DNA&w=0,2000")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert "immutable" in response.headers["Cache-Control"]
    header, voxels = _parse(response.data)
    assert header["brick"] == [34, 66, 66] and header["level"] == 0
    size = header["bytes_per_brick"]
    for brick in header["bricks"]:
        at = tuple(brick["at"])
        assert brick["offset"] is not None
        for k, (channel, window) in enumerate(((1, (0, 1000)), (0, (0, 2000)))):
            got = np.frombuffer(voxels, np.uint8, size, brick["offset"] + k * size)
            expected = serve.quantize(_reference(data[channel], at), *window, "u8")
            assert np.array_equal(got.reshape(34, 66, 66), expected), (at, channel)
            core = expected[1:-1, 1:-1, 1:-1]
            assert brick["max"][k] == core.max()


def test_a_pooled_level_is_max_pooled(tmp_path):
    from plexora.server.models import data_model, views

    data = _stack(tmp_path, "pooled", physical_z=0.25, depth=7)
    data_model.ensure_loaded("pooled")
    source = views.current_source("pooled")
    entry = brick_serve.level_of(source.volume, 0)
    got = brick_serve.read_brick(source, entry, 0, 1, (0, 1, 1))
    assert np.array_equal(got, _reference(data[1], (0, 1, 1), pool=2))


def test_a_brick_below_the_window_floor_is_empty(tmp_path, client):
    _stack(tmp_path)
    response = client.get("/volume/bricks/bricks?level=0&at=0.0.0&channel=CD3&w=60000,65000")
    header, voxels = _parse(response.data)
    assert header["bricks"] == [{"at": [0, 0, 0], "max": [0.0], "offset": None}]
    assert voxels == b""


def test_the_address_is_the_cache_key(tmp_path, client):
    _stack(tmp_path)
    address = "/volume/bricks/bricks?level=1&at=0.0.0&channel=CD3&w=0,1000"
    first = client.get(address)
    again = client.get(address, headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304
    other = client.get(address.replace("w=0,1000", "w=0,999"))
    assert other.headers["ETag"] != first.headers["ETag"]
    # Without a window the bytes may change with it: revalidated or not kept.
    unnamed = client.get("/volume/bricks/bricks?level=1&at=0.0.0&channel=CD3")
    assert "immutable" not in unnamed.headers["Cache-Control"]


def test_what_it_refuses(tmp_path, client):
    _stack(tmp_path)
    base = "/volume/bricks/bricks?level=0&channel=CD3&w=0,1000"
    assert client.get(base + "&at=0.5.0").status_code == 400            # off the grid
    assert client.get(base + "&at=").status_code == 400                 # none asked
    many = ",".join("0.0.0" for _ in range(3))
    assert client.get(base + f"&at={many}").status_code == 200          # repeats are one
    assert client.get("/volume/bricks/bricks?level=9&at=0.0.0&channel=CD3").status_code == 400
    assert client.get("/volume/bricks/bricks?level=0&at=0.0.0&channel=nope").status_code == 400
    flat, _ = vf.write_ome_volume(tmp_path / "flat.ome.tif", axes="CZYX", channels=1, depth=1,
                                  names=["DNA"])
    register_image_datasource("flatbricks", flat)
    assert client.get("/volume/flatbricks/bricks/manifest").status_code == 400


def test_bricks_read_together_are_the_bricks_read_alone(tmp_path):
    from plexora.server.models import data_model, views

    _stack(tmp_path, "together", physical_z=0.25, depth=7)
    data_model.ensure_loaded("together")
    source = views.current_source("together")
    entry = brick_serve.level_of(source.volume, 0)
    ats = [(0, 0, 0), (0, 0, 1), (0, 1, 1)]
    together = brick_serve.read_bricks(source, entry, 0, 1, ats)
    for at in ats:
        assert np.array_equal(together[at], brick_serve.read_brick(source, entry, 0, 1, at)), at


def test_bricks_come_compressed_only_when_asked(tmp_path, client):
    import gzip

    _stack(tmp_path, "zipped")
    address = "/volume/zipped/bricks?level=0&at=0.0.0&channel=CD3&w=0,1000"
    plain = client.get(address, headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in plain.headers
    assert plain.headers["Server-Timing"].startswith("total;dur=")
    zipped = client.get(address + "&z=gzip", headers={"Accept-Encoding": "gzip"})
    assert zipped.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(zipped.data) == plain.data and len(zipped.data) < len(plain.data)
    # A client that cannot take gzip gets the plain bytes even if it asks.
    refused = client.get(address + "&z=gzip")
    assert "Content-Encoding" not in refused.headers and refused.data == plain.data
