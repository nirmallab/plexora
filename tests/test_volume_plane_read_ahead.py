"""Stepping through planes: reads that land in a cache, and where the time goes.

- A tile's `Server-Timing` keeps its whole `read` and adds the parts the
  reader can tell apart: `open`, `chunk` (hit or miss), `io`, `inflate`.
- An OME-Zarr store chunked across z reads its planes through the decoded-
  chunk cache, so the next plane -- and the block read ahead -- costs no
  decode; the pixels are exactly the direct read's.
- The primary's encoded tile cache is budgeted in bytes.
- A stack decoded a plane at a time (a TIFF stored plane by plane) has the
  tiles of the next planes encoded ahead into the tile cache, under the key
  the route would use, on the primary and on a data node alike -- and a read
  ahead never loads a project.
- Any other stack (Imaris, a zarr store chunked across z) has the tiles of
  its two coarsest levels made ahead, four planes deep: never its full
  resolution, and never levels still to be derived.
- `/volume/<ds>/perf` reports the tile cache and how much of the read-ahead
  landed.
"""

from __future__ import annotations

import io

import numpy as np
import pytest

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.routes import data_routes
from plexora.server.utils import ome_zarr
from plexora.server.volume import chunk_cache
from plexora.server.volume import prefetch as prefetch_module
from plexora.server.volume.sources import ngff as ngff_source
from plexora.telemetry import performance
from tests import volume_fixtures as vf

TOKEN = {"X-Plexora-Node-Token": "x"}


def _quiet(*_args, **_kwargs):
    pass


def _png16(payload):
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(payload)))


def _timing(response) -> dict:
    """`Server-Timing` as `{name: {"dur": ms, "desc": text}}`."""
    out = {}
    for entry in (response.headers.get("Server-Timing") or "").split(","):
        bits = [bit.strip() for bit in entry.split(";") if bit.strip()]
        if not bits:
            continue
        fields = {}
        for bit in bits[1:]:
            name, _, value = bit.partition("=")
            fields[name] = float(value) if name == "dur" else value
        out[bits[0]] = fields
    return out


def _key(entry, index):
    return entry["imageData"][index]["src"].rstrip("/").rsplit("/", 1)[-1]


@pytest.fixture()
def client():
    return plexora.app.test_client()


@pytest.fixture()
def fresh_cache(monkeypatch):
    cache = chunk_cache.ChunkCache(64 * 2**20)
    monkeypatch.setattr(chunk_cache, "CACHE", cache)
    return cache


@pytest.fixture()
def tiles_ahead():
    """The process's tile prefetcher, emptied before and drained after: a job
    left running would read after the test's project is gone."""
    ahead = prefetch_module.TILE_PREFETCHER
    ahead.drain()
    ahead.forget()
    data_routes._tile_png_cache.clear()
    yield ahead
    ahead.drain()
    ahead.forget()
    data_routes._tile_png_cache.clear()


# -- Server-Timing: the read's parts ---------------------------------------------


def test_the_read_keeps_its_total_and_lists_its_parts_beside_it():
    performance.begin("channel")
    performance.part("open", 0.5)
    phases = performance.current()
    performance.part("io", 2.0, phases)
    performance.part("io", 1.0, phases)
    performance.part("inflate", 3.0, phases)
    performance.part("chunk", 7.0)
    performance.note("chunk", "miss")
    performance.mark("read")
    performance.mark("enc")
    header = performance.server_timing_header(performance.end())
    names = [entry.split(";")[0].strip() for entry in header.split(",")]
    assert names[:1] == ["read"] and "enc" in names and names[-1] == "total"
    assert "io;dur=3.0" in header, "parts of the same name add up"
    assert "chunk;dur=7.0;desc=miss" in header
    assert "open;dur=0.5" in header and "inflate;dur=3.0" in header
    # Nothing recorded, nothing listed: a tile with no parts reads as before.
    performance.begin("channel")
    performance.mark("read")
    assert performance.server_timing_header(performance.end()).startswith("read;dur=")
    # Off a tile's thread, a part is nothing.
    performance.part("io", 1.0)


def test_an_imaris_tile_splits_its_read_and_the_next_plane_is_a_chunk_hit(
        tmp_path, client, fresh_cache):
    path, data = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                              compression="gzip", chunks=(4, 16, 16), levels=1)
    entry = register_image_datasource("ims", path)
    base = f"/generated/data/ims/{_key(entry, 0)}/0/0_0.png?q=hd"
    first = client.get(base + "&view=z1")
    assert first.status_code == 200
    assert np.array_equal(_png16(first.data), data[0, 0, 1])
    timing = _timing(first)
    assert {"read", "open", "chunk", "io", "inflate", "total"} <= set(timing), timing
    assert timing["chunk"]["desc"] == "miss"
    # Plane 2 is in the chunks plane 1 decoded: no bytes read, nothing inflated.
    second = client.get(base + "&view=z2")
    assert np.array_equal(_png16(second.data), data[0, 0, 2])
    timing = _timing(second)
    assert timing["chunk"]["desc"] == "hit"
    assert "io" not in timing and "inflate" not in timing
    assert "read" in timing, "the whole read is still reported for older readers"


# -- OME-Zarr planes through the decoded-chunk cache --------------------------------


@pytest.mark.parametrize("z_factor", [1, 2])
def test_a_zarr_plane_read_through_the_cache_is_the_direct_read(tmp_path, fresh_cache,
                                                                 z_factor):
    path, _ = vf.write_ngff_volume(tmp_path / "v.zarr", channels=2, depth=20, height=150,
                                   width=133, levels=3, chunks_z=8, z_factor=z_factor)
    source = ngff_source.open_ngff_volume(path)
    assert source.z_decode == 8
    for z in (0, 1, 7, 8, 13, 19):
        pyramid = source.plane_pyramid(0, z)
        for level in range(3):
            array = source._arrays[level]
            stored = ngff_source.stored_plane(source.volume, level, z)
            direct = ome_zarr._LevelView(array, source._axes, t=0, z=stored)
            cached = pyramid[str(level)]
            assert isinstance(cached, ngff_source._CachedPlane)
            height, width = (int(v) for v in array.shape[-2:])
            for channel in range(2):
                for ty in range(-(-height // 32)):
                    for tx in range(-(-width // 32)):
                        tile = f"{tx}_{ty}"
                        got = data_model.read_tile(pyramid, channel, level, tile, 32, 32)
                        want = data_model.read_tile(ome_zarr.NgffPyramid([direct] * (level + 1)),
                                                    channel, level, tile, 32, 32)
                        assert got.dtype == want.dtype
                        assert np.array_equal(got, want), (z, level, channel, tile)
                # A tile wholly past the edge, a stride, a whole channel.
                assert np.array_equal(cached[channel, height:height + 32, 0:32],
                                      direct[channel, height:height + 32, 0:32])
                assert np.array_equal(cached[channel, ::3, 1::2], direct[channel, ::3, 1::2])
                assert np.array_equal(np.asarray(cached), np.asarray(direct))
    source.close()


def test_the_next_zarr_plane_decodes_nothing(tmp_path, fresh_cache):
    path, data = vf.write_ngff_volume(tmp_path / "v.zarr", channels=1, depth=16, height=64,
                                      width=64, levels=1, chunks_z=8)
    source = ngff_source.open_ngff_volume(path)
    first = data_model.read_tile(source.plane_pyramid(0, 3), 0, 0, "0_0", 64, 64)
    loads = fresh_cache.stats()["loads"]
    assert loads >= 1, "the plane was not read through the cache"
    for z in range(4, 8):
        got = data_model.read_tile(source.plane_pyramid(0, z), 0, 0, "0_0", 64, 64)
        assert np.array_equal(got, data[0, z])
    assert np.array_equal(first, data[0, 3])
    assert fresh_cache.stats()["loads"] == loads, "a plane inside the chunk decoded again"
    source.close()


def test_a_zarr_store_one_plane_deep_reads_its_planes_directly(tmp_path, fresh_cache):
    path, _ = vf.write_ngff_volume(tmp_path / "v.zarr", channels=1, depth=6, levels=1,
                                   chunks_z=1)
    source = ngff_source.open_ngff_volume(path)
    level = source.plane_pyramid(0, 2)["0"]
    assert type(level) is ome_zarr._LevelView
    source.close()


def test_a_zarr_plane_tile_reads_the_block_ahead(tmp_path, client, fresh_cache, monkeypatch):
    path, _ = vf.write_ngff_volume(tmp_path / "v.zarr", channels=1, depth=24, height=64,
                                   width=64, levels=1, chunks_z=8)
    entry = register_image_datasource("zv", path)
    ahead = prefetch_module.PREFETCHER
    monkeypatch.setattr(ahead, "min_budget", 0)
    ahead.forget()
    base = f"/generated/data/zv/{_key(entry, 0)}/0/0_0.png?q=hd"
    assert client.get(base + "&view=z9").status_code == 200
    assert client.get(base + "&view=z10").status_code == 200
    assert ahead.drain()
    # Planes 16-23 -- the next chunk -- are decoded before anybody asks.
    blocks = {key[4] for key in list(fresh_cache._entries)}
    assert 2 in blocks, blocks
    assert _timing(client.get(base + "&view=z16"))["chunk"]["desc"] == "hit"
    stats = ahead.snapshot()
    assert stats["entered"] >= 2 and stats["warmed"] >= 1 and stats["hit_rate"] > 0
    ahead.forget()


# -- the primary's tile cache, in bytes ---------------------------------------------


def test_the_tile_cache_is_budgeted_in_bytes(tmp_path, client, monkeypatch, tiles_ahead):
    path, _ = vf.write_ome_volume(tmp_path / "flat.ome.tif", axes="CZYX", channels=3,
                                  depth=2, height=128, width=128)
    entry = register_image_datasource("flat", path)
    one = len(client.get(f"/generated/data/flat/{_key(entry, 0)}/0/0_0.png?q=hd").data)
    data_routes._tile_png_cache.clear()
    monkeypatch.setattr(data_routes, "_TILE_PNG_CACHE_BYTES", int(one * 2.5))
    for index in range(3):
        assert client.get(f"/generated/data/flat/{_key(entry, index)}/0/0_0.png?q=hd"
                          ).status_code == 200
    # Two fit, the third pushed out the least recently used.
    assert len(data_routes._tile_png_cache) == 2
    assert data_routes._tile_png_cache.nbytes <= int(one * 2.5)
    assert [key[2] for key in data_routes._tile_png_cache] == [_key(entry, 1), _key(entry, 2)]
    # A tile bigger than the whole budget is still kept, alone.
    monkeypatch.setattr(data_routes, "_TILE_PNG_CACHE_BYTES", 10)
    client.get(f"/generated/data/flat/{_key(entry, 0)}/0/0_0.png?q=hd")
    assert len(data_routes._tile_png_cache) == 1
    data_routes._tile_png_cache.clear()
    assert data_routes._tile_png_cache.nbytes == 0


def test_the_tile_cache_budget_comes_from_the_allocation(monkeypatch):
    monkeypatch.setenv("PLEXORA_TILE_CACHE_MB", "64")
    assert data_routes._tile_cache_budget() == 64 * 2**20
    monkeypatch.setenv("PLEXORA_TILE_CACHE_MB", "lots")
    monkeypatch.setattr("plexora._resources.allocated_memory_bytes", lambda: 16 * 2**30)
    assert data_routes._tile_cache_budget() == 2**30
    monkeypatch.delenv("PLEXORA_TILE_CACHE_MB")
    monkeypatch.setattr("plexora._resources.allocated_memory_bytes", lambda: 2**30)
    assert data_routes._tile_cache_budget() == 128 * 2**20
    monkeypatch.setattr("plexora._resources.allocated_memory_bytes", lambda: 256 * 2**30)
    assert data_routes._tile_cache_budget() == 2 * 2**30


# -- tiles made ahead of a stack decoded a plane at a time ---------------------------


def test_after_plane_tile_follows_the_hand_nearest_first():
    ahead = prefetch_module.PlanePrefetcher(workers=1, min_budget=0)
    made, held = [], {7}

    def encode(z):
        made.append(z)
        return True

    # No direction yet: one plane either side, the one after first.
    assert ahead.after_plane_tile("tile", 5, 20, encode, held.__contains__) == [6, 4]
    # Deeper: the next two, not the one already held.
    assert ahead.after_plane_tile("tile", 6, 20, encode, held.__contains__) == [8]
    assert ahead.drain()
    # Back up, from the top edge.
    assert ahead.after_plane_tile("tile", 1, 20, encode) == [0]
    assert ahead.drain()
    assert sorted(made) == [0, 4, 6, 8]
    # A tile served from the cache that was made ahead counts as landed.
    ahead.after_plane_tile("tile", 8, 20, encode, hit=True)
    assert ahead.drain()
    stats = ahead.snapshot()
    assert stats["made"] >= 4 and stats["tiles_warmed"] == 1 and stats["served"] == 4
    # A read ahead moves no hand and queues nothing.
    with chunk_cache.background():
        assert ahead.after_plane_tile("tile", 9, 20, encode) == []
        assert ahead.after_tile("block", 9, 40, 8, encode) == []
    assert ahead.after_plane_tile("flat", 0, 1, encode) == [], "a single plane"
    assert ahead.after_plane_tile("tile", 12, 20, encode, ahead=0) == [], "no planes ahead"
    # Four ahead (a coarse tile of a chunked stack): still one either side
    # until a direction is known, then the next four nearest first, to the edge.
    assert ahead.after_plane_tile("coarse", 10, 20, encode, ahead=4) == [11, 9]
    assert ahead.after_plane_tile("coarse", 11, 20, encode, ahead=4) == [12, 13, 14, 15]
    assert ahead.after_plane_tile("coarse", 17, 20, encode, ahead=4) == [18, 19]
    assert ahead.drain()
    assert ahead.after_plane_tile("coarse", 14, 20, encode, ahead=4) == [13, 12, 11, 10]
    assert ahead.drain()
    assert {11, 9, 12, 13, 14, 15, 18, 19, 10} <= set(made)


def _plane_major(tmp_path, name="pm", depth=8):
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                     depth=depth, height=96, width=80)
    entry = register_image_datasource(name, path)
    return entry, data


def test_the_next_planes_tiles_are_made_ahead_and_served_from_the_cache(
        tmp_path, client, tiles_ahead):
    entry, data = _plane_major(tmp_path)
    channel = _key(entry, 1)
    url = f"/generated/data/pm/{channel}/0/0_0.png?q=hd&view=z{{}}"
    first = client.get(url.format(2))
    assert first.status_code == 200 and _timing(first)["cache"]["desc"] == "miss"
    assert tiles_ahead.drain()
    # Direction unknown: the planes either side.
    generation = data_model.load_generation
    key = lambda z: data_routes._tile_key(generation, "pm", channel, "0", "0_0.png",  # noqa: E731
                                          "hd", f"z{z}")
    assert key(3) in data_routes._tile_png_cache and key(1) in data_routes._tile_png_cache
    step = client.get(url.format(3))
    assert _timing(step)["cache"]["desc"] == "hit"
    assert np.array_equal(_png16(step.data), data[0, 1, 3])
    # Moving deeper: the next two.
    assert tiles_ahead.drain()
    assert key(4) in data_routes._tile_png_cache and key(5) in data_routes._tile_png_cache
    made = data_routes._tile_png_cache[key(4)]
    fresh = data_model.encode_tile("pm", channel, "0", "0_0.png", "hd", "z4")
    assert made == fresh, "a tile made ahead is the tile the route would encode"
    stats = tiles_ahead.snapshot()
    assert stats["tiles_warmed"] >= 1 and stats["tile_hit_rate"] > 0


def test_an_8_bit_tile_is_made_ahead_under_its_window(tmp_path, client, tiles_ahead):
    entry, _data = _plane_major(tmp_path)
    channel = _key(entry, 0)
    url = f"/generated/data/pm/{channel}/0/0_0.png?view=z{{}}&w=0,3000"
    assert client.get(url.format(4)).status_code == 200
    assert client.get(url.format(5)).status_code == 200
    assert tiles_ahead.drain()
    ahead = client.get(url.format(6))
    assert _timing(ahead)["cache"]["desc"] == "hit"
    data_routes._tile_png_cache.clear()
    again = client.get(url.format(6))
    assert _timing(again)["cache"]["desc"] == "miss"
    assert again.data == ahead.data
    # Another window is another tile: not answered from the one made ahead.
    other = client.get(f"/generated/data/pm/{channel}/0/0_0.png?view=z7&w=0,2000")
    assert _timing(other)["cache"]["desc"] == "miss"


def test_nothing_is_made_ahead_of_a_stack_whose_reads_span_planes(tmp_path, client,
                                                                   fresh_cache, tiles_ahead):
    path, _ = vf.write_ims(tmp_path / "s.ims", channels=1, depth=8, height=64, width=64,
                           chunks=(4, 16, 16), levels=1)
    entry = register_image_datasource("ims", path)
    queued = tiles_ahead.snapshot()["queued"]
    client.get(f"/generated/data/ims/{_key(entry, 0)}/0/0_0.png?q=hd&view=z1")
    assert tiles_ahead.drain()
    assert tiles_ahead.snapshot()["queued"] == queued
    assert len(data_routes._tile_png_cache) == 1


def test_only_the_coarsest_levels_of_a_pyramid_are_coarse(tmp_path):
    coarse = prefetch_module.coarse_level
    assert prefetch_module.COARSE_LEVELS == 2 and prefetch_module.COARSE_TILE_AHEAD == 4
    # One level is the full resolution: never coarse.
    assert not coarse(1, 0)
    # Two: only the second. More: the two coarsest.
    assert [coarse(2, level) for level in range(2)] == [False, True]
    assert [coarse(3, level) for level in range(3)] == [False, True, True]
    assert [coarse(6, level) for level in range(6)] == [False] * 4 + [True, True]
    # The level as the route has it; a level the pyramid has not.
    assert coarse(3, "2") and not coarse(3, 3) and not coarse(3, -1)
    assert not coarse(3, None) and not coarse(3, "x") and not coarse(None, 1)
    # Stand-ins for levels yet to be derived are not cheap: none is coarse.
    assert not any(coarse(6, level, pending=True) for level in range(6))
    # How far ahead: planes decoded one at a time, every level, as before;
    # any other stack, its coarse levels only.
    planes = prefetch_module.planes_ahead
    assert planes(1, 1, 0) == planes(1, 3, 0) == prefetch_module.TILE_AHEAD
    assert planes(4, 3, 2) == planes(64, 3, "1") == prefetch_module.COARSE_TILE_AHEAD
    assert planes(4, 3, 0) is None and planes(4, 1, 0) is None
    assert planes(4, 3, 2, pending=True) is None
    # Pending: an OME-Zarr stack serving its coarse levels as subsamples.
    path, _ = vf.write_ngff_volume(tmp_path / "v.zarr", channels=1, depth=8, height=64,
                                   width=64, levels=2, chunks_z=4)
    source = ngff_source.open_ngff_volume(path)
    try:
        assert not prefetch_module.levels_pending(source)
        source.plan_extension(tmp_path / "derived.zarr")
        assert prefetch_module.levels_pending(source)
    finally:
        source.close()
    # Subsamples for good (no derivation could be written): pending all the same.
    assert prefetch_module.levels_pending(type("Interim", (), {"_interim": [(8, 8)]})())


def test_the_coarsest_levels_of_a_chunked_stack_are_made_ahead(tmp_path, client, fresh_cache,
                                                               tiles_ahead):
    path, data = vf.write_ims(tmp_path / "c.ims", channels=1, depth=12, height=64, width=64,
                              chunks=(4, 16, 16), levels=3)
    entry = register_image_datasource("chunked", path)
    channel = _key(entry, 0)
    url = f"/generated/data/chunked/{channel}/{{}}/0_0.png?q=hd&view=z{{}}"
    cache = data_routes._tile_png_cache
    first = client.get(url.format(2, 3))
    assert first.status_code == 200 and _timing(first)["cache"]["desc"] == "miss"
    assert tiles_ahead.drain()
    generation = data_model.load_generation
    key = lambda level, z: data_routes._tile_key(  # noqa: E731
        generation, "chunked", channel, str(level), "0_0.png", "hd", f"z{z}")
    # The coarsest level, direction unknown: the planes either side.
    assert key(2, 4) in cache and key(2, 2) in cache
    step = client.get(url.format(2, 4))
    assert _timing(step)["cache"]["desc"] == "hit"
    assert np.array_equal(_png16(step.data), data[0, 0, 4, ::4, ::4])
    # Moving deeper: the next four.
    assert tiles_ahead.drain()
    assert all(key(2, z) in cache for z in range(5, 9)), [k for k in cache]
    assert key(2, 9) not in cache
    assert cache[key(2, 6)] == data_model.encode_tile("chunked", channel, "2", "0_0.png", "hd",
                                                      "z6"), \
        "a tile made ahead is the tile the route would encode"
    # The next level is coarse too.
    assert client.get(url.format(1, 4)).status_code == 200
    assert tiles_ahead.drain()
    assert key(1, 5) in cache and key(1, 3) in cache
    # The full resolution: its tiles are the block read-ahead's, none made ahead.
    queued, held = tiles_ahead.snapshot()["queued"], len(cache)
    assert client.get(url.format(0, 4)).status_code == 200
    assert tiles_ahead.drain()
    assert tiles_ahead.snapshot()["queued"] == queued
    assert len(cache) == held + 1


def test_a_read_ahead_never_loads_a_project(tmp_path, client, tiles_ahead):
    entry, _ = _plane_major(tmp_path, "one")
    _plane_major(tmp_path, "two")
    data_model.load_datasource("one")
    with data_model.reading_ahead():
        with pytest.raises(data_model.NotLoaded):
            data_model.encode_tile("two", _key(entry, 0).replace("one", "two"), "0",
                                   "0_0.png", "hd", "z1")
    assert data_model.is_loaded("one")
    # A tile queued for a project switched away from is dropped, not loaded.
    client.get(f"/generated/data/one/{_key(entry, 0)}/0/0_0.png?q=hd&view=z3")
    data_model.load_datasource("two")
    assert tiles_ahead.drain()
    assert data_model.is_loaded("two")


def test_volume_perf_reports_the_tile_cache_and_what_landed(tmp_path, client, tiles_ahead):
    entry, _ = _plane_major(tmp_path)
    channel = _key(entry, 0)
    for z in (2, 3):
        client.get(f"/generated/data/pm/{channel}/0/0_0.png?q=hd&view=z{z}")
        tiles_ahead.drain()
    perf = client.get("/volume/pm/perf").get_json()
    assert perf["tile_cache"]["entries"] >= 3 and perf["tile_cache"]["bytes"] > 0
    assert perf["tile_cache"]["budget"] == data_routes._TILE_PNG_CACHE_BYTES
    assert perf["tile_cache"]["hits"] >= 1
    assert perf["tile_prefetch"]["made"] >= 2 and perf["tile_prefetch"]["tile_hit_rate"] > 0
    assert "hit_rate" in perf["plane_prefetch"]


# -- the same on a data node ------------------------------------------------------


def test_a_node_makes_the_next_planes_tiles_ahead(tmp_path, fresh_cache, tiles_ahead):
    from plexora.server.node import api as node_api
    from plexora.server.node.app import create_node_app

    path, data = vf.write_ome_volume(tmp_path / "n.ome.tif", axes="CZYX", channels=2,
                                     depth=8, height=96, width=80)
    app = create_node_app([f"image:vol={path}"], token="x", log=_quiet)
    node_api._tile_cache.clear()
    client = app.test_client()
    url = "/node/v1/image/vol/tile/ch_1/0/0_0?view=z{}&zs=depth&tw=128&th=128&q=hd"
    first = client.get(url.format(4), headers=TOKEN)
    assert first.status_code == 200, first.data[:200]
    assert "open" in _timing(first)
    assert tiles_ahead.drain()
    assert len(node_api._tile_cache) == 3, "the plane served and one either side"
    step = client.get(url.format(5), headers=TOKEN)
    assert _timing(step)["cache"]["desc"] == "hit"
    assert np.array_equal(_png16(step.data), data[0, 1, 5])
    node_api._tile_cache.clear()
    assert client.get(url.format(5), headers=TOKEN).data == step.data
    assert tiles_ahead.drain()
    node_api._tile_cache.clear()

    # A stack read a block of planes at a time: only its coarse tiles are made
    # ahead -- four planes deep once the hand has a direction.
    path, data = vf.write_ims(tmp_path / "n.ims", channels=1, depth=12, height=64, width=64,
                              chunks=(4, 16, 16), levels=3)
    client = create_node_app([f"image:ims={path}"], token="x", log=_quiet).test_client()
    url = "/node/v1/image/ims/tile/ch_0/{}/0_0?view=z{}&zs=depth&tw=128&th=128&q=hd"
    first = client.get(url.format(2, 3), headers=TOKEN)
    assert first.status_code == 200, first.data[:200]
    assert tiles_ahead.drain()
    assert len(node_api._tile_cache) == 3, "the plane served and one either side"
    step = client.get(url.format(2, 4), headers=TOKEN)
    assert _timing(step)["cache"]["desc"] == "hit"
    assert np.array_equal(_png16(step.data), data[0, 0, 4, ::4, ::4])
    assert tiles_ahead.drain()
    assert len(node_api._tile_cache) == 7, "and the next four"
    assert _timing(client.get(url.format(2, 8), headers=TOKEN))["cache"]["desc"] == "hit"
    assert tiles_ahead.drain()
    queued, held = tiles_ahead.snapshot()["queued"], len(node_api._tile_cache)
    assert client.get(url.format(0, 4), headers=TOKEN).status_code == 200
    assert tiles_ahead.drain()
    assert tiles_ahead.snapshot()["queued"] == queued, "nothing ahead at full resolution"
    assert len(node_api._tile_cache) == held + 1
    node_api._tile_cache.clear()


def test_a_nodes_zarr_planes_come_from_the_decoded_chunk_cache(tmp_path, fresh_cache,
                                                                tiles_ahead):
    from plexora.server.node import api as node_api
    from plexora.server.node.app import create_node_app

    path, data = vf.write_ngff_volume(tmp_path / "n.zarr", channels=1, depth=16, height=64,
                                      width=64, levels=1, chunks_z=8)
    app = create_node_app([f"image:vol={path}"], token="x", log=_quiet)
    node_api._tile_cache.clear()
    client = app.test_client()
    queued = tiles_ahead.snapshot()["queued"]
    url = "/node/v1/image/vol/tile/ch_0/0/0_0?view=z{}&zs=depth&tw=64&th=64&q=hd"
    assert _timing(client.get(url.format(2), headers=TOKEN))["chunk"]["desc"] == "miss"
    step = client.get(url.format(3), headers=TOKEN)
    assert _timing(step)["chunk"]["desc"] == "hit"
    assert np.array_equal(_png16(step.data), data[0, 3])
    assert tiles_ahead.snapshot()["queued"] == queued, \
        "the block read-ahead covers a chunked store"
    node_api._tile_cache.clear()
