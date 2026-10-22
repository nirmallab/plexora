"""Pyramids in depth: levels that store fewer planes, one pooling rule, and a
level route that refuses what no GPU can draw.

- An Imaris file (and some OME-Zarr stores) halve z in their coarse levels:
  those levels are read once per STORED plane -- a texture of 3 planes for a
  6-plane stack at level 1, not 6 with each plane twice -- and an OME-Zarr
  store's coarse levels are indexed by their own planes, not the stack's.
- `model.pool_for` is the one z-pooling rule, a power of two.
- `level=auto` on a stack none of whose levels fits a 3-D texture is a 400
  with a sentence, not a level 0 of gigabytes.
- A 3-D label volume is built down to the image's level count; one built
  shorter (they used to stop at 512 px a side) serves the image's coarser
  levels by striding, as a 2-D mask does.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.volume import model, serve
from plexora.server.volume.sources import open_volume
from tests import volume_fixtures as vf


@pytest.fixture
def client():
    return plexora.app.test_client()


def _level(response):
    shape = tuple(int(v) for v in response.headers["X-Plexora-Shape"].split(","))
    qmin, qmax = (float(v) for v in response.headers["X-Plexora-Window"].split(","))
    return shape, qmin, qmax


def _u8(block, qmin, qmax):
    return np.clip(np.rint((block.astype(np.float32) - qmin) / (qmax - qmin) * 255),
                   0, 255).astype(np.uint8)


# -- the helpers -------------------------------------------------------------------


def test_stored_planes_and_pools(tmp_path):
    path, _ = vf.write_ims(tmp_path / "z.ims", depth=6, z_levels=True)
    source = open_volume(path)
    try:
        volume = source.volume
        assert model.stored_depth(volume, 0) == 6 and model.stored_depth(volume, 1) == 3
        assert model.z_scale(volume, 1) == 2
        assert [model.stored_plane(volume, 1, z) for z in range(6)] == [0, 0, 1, 1, 2, 2]
        assert model.stored_span(volume, 1, 3, 5) == (1, 2)
        # 0.5 µm pixels, 2 µm planes: a level-1 pixel is 1 µm, under a plane.
        assert model.pool_for(volume, 1) == 1
    finally:
        source.close()


def test_pool_for_is_a_power_of_two_and_one_without_a_z_step(tmp_path):
    path, _ = vf.write_ome_volume(tmp_path / "fine.ome.tif", axes="CZYX", channels=1,
                                  depth=24, physical_z=0.15)
    source = open_volume(path)
    try:
        # 0.5 µm pixels over 0.15 µm planes: 3.3 planes to a pixel -> 2, not 3.
        assert model.pool_for(source.volume, 0) == 2
        assert serve.z_pool(source.volume, 0) == 2
    finally:
        source.close()
    path, _ = vf.write_ome_volume(tmp_path / "noz.ome.tif", axes="CZYX", channels=1,
                                  depth=8, physical_z=None)
    source = open_volume(path, z_semantics="depth")
    try:
        assert model.pool_for(source.volume, 0) == 1
        assert model.pool_for(source.volume, 3, assume_square=True) == 8
    finally:
        source.close()


def test_a_pooled_read_is_the_maximum_of_each_run_of_planes(tmp_path):
    path, data = vf.write_ome_volume(tmp_path / "p.ome.tif", axes="CZYX", channels=1,
                                     depth=7)
    source = open_volume(path)
    try:
        rows, cols = slice(0, 96), slice(0, 80)
        got = model.read_pooled(source, 0, 0, 0, 0, 6, 3, rows, cols)
        expected = np.stack([data[0, 0, 0:3].max(0), data[0, 0, 3:6].max(0),
                             data[0, 0, 6:7].max(0)])
        assert np.array_equal(got, expected)
    finally:
        source.close()


# -- Imaris: stored depth ----------------------------------------------------------


def test_an_imaris_coarse_level_is_sent_once_per_stored_plane(tmp_path, client):
    path, data = vf.write_ims(tmp_path / "zl.ims", channels=2, depth=6, z_levels=True,
                              names=["DAPI", "CD8"])
    register_image_datasource("zl", path)
    data_model.load_datasource("zl")
    response = client.get("/volume/zl/level?channel=CD8&level=1")
    assert response.status_code == 200, response.get_data(as_text=True)
    shape, qmin, qmax = _level(response)
    assert shape == (3, 48, 40)
    expected = _u8(data[0, 1, ::2, ::2, ::2], qmin, qmax)
    assert np.array_equal(np.frombuffer(response.data, np.uint8).reshape(shape), expected)
    # Planes 3..4 of the stack are stored planes 1..2 of level 1.
    detail = client.get("/volume/zl/level?channel=CD8&level=1&z=3,4")
    assert _level(detail)[0][0] == 2


# -- OME-Zarr: coarse levels that halve z ------------------------------------------


def test_an_ome_zarr_level_that_halves_z_is_read_by_its_own_planes(tmp_path):
    path, data = vf.write_ngff_volume(tmp_path / "zh.zarr", channels=2, depth=6,
                                      z_factor=2)
    source = open_volume(path)
    try:
        volume = source.volume
        assert volume.levels[1].depth == 3
        level1 = data[:, ::2, ::2, ::2]   # (c, z, y, x) at level 1
        plane = np.asarray(source.level_array(0, 5, 1)[1])
        assert np.array_equal(plane, level1[1, 2])
        block = source.read_block(1, 0, 1, 2, 5, slice(0, 48), slice(0, 40))
        assert np.array_equal(block, level1[1, [1, 1, 2, 2]])
        assert np.array_equal(model.read_stored(source, 1, 0, 1, 0, 2, slice(0, 48),
                                                slice(0, 40)), level1[1])
    finally:
        source.close()


# -- nothing fits ------------------------------------------------------------------


def test_a_stack_no_level_of_which_fits_is_refused_with_a_sentence(tmp_path, client,
                                                                     monkeypatch):
    path, _ = vf.write_ome_volume(tmp_path / "big.ome.tif", axes="CZYX", channels=1,
                                  depth=8)
    entry = register_image_datasource("big", path)
    data_model.load_datasource("big")
    name = entry["imageData"][0]["fullname"]
    monkeypatch.setattr(serve, "MAX_LEVEL_BYTES", 1000)
    auto = client.get(f"/volume/big/level?channel={name}&budget=1")
    assert auto.status_code == 400
    assert "no level of this stack fits a 3-D texture" in auto.get_json()["error"]
    explicit = client.get(f"/volume/big/level?channel={name}&level=0")
    assert explicit.status_code == 400
    # Over the budget asked for but a texture all the same: still sent.
    monkeypatch.setattr(serve, "MAX_LEVEL_BYTES", 512 * 2**20)
    served = client.get(f"/volume/big/level?channel={name}&budget=1")
    assert served.status_code == 200
    narrow = client.get(f"/volume/big/level?channel={name}&max_side=16")
    assert narrow.status_code == 400


# -- labels ------------------------------------------------------------------------


def test_a_label_volume_shorter_than_the_image_strides_a_finer_level(tmp_path):
    from plexora.server.utils.label_overlay import label_region, mask_level_shape
    from plexora.server.volume.labels import build_label_volume, open_label_volume

    rng = np.random.default_rng(2)
    labels = rng.integers(0, 50, size=(3, 600, 520), dtype=np.uint32)
    dest = build_label_volume(labels, tmp_path / "labels.zarr")
    volume = open_label_volume(dest)
    plane = volume.plane(1)
    coarse = volume.levels + 2
    assert str(coarse) not in plane
    region = label_region(plane, coarse, 0, 0, 10, 10)
    base = volume.levels - 1
    step = 2 ** coarse // 2 ** base
    expected = labels[1, ::2 ** base, ::2 ** base][::step, ::step][:10, :10]
    assert np.array_equal(region, expected)
    assert mask_level_shape(plane, coarse) == (-(-600 // 2 ** coarse), -(-520 // 2 ** coarse))


def test_a_label_volume_goes_as_coarse_as_the_image_it_is_drawn_on(tmp_path):
    from plexora.server.utils.label_overlay import label_region
    from plexora.server.volume.labels import _level_shapes, build_label_volume, open_label_volume

    assert len(_level_shapes(600, 520)) == 2
    assert len(_level_shapes(600, 520, min_levels=5)) == 5
    assert len(_level_shapes(600, 520, min_levels=1)) == 2
    assert _level_shapes(3, 2, min_levels=8) == [(3, 2), (2, 1), (1, 1)]

    rng = np.random.default_rng(3)
    labels = rng.integers(0, 50, size=(3, 600, 520), dtype=np.uint32)
    volume = open_label_volume(build_label_volume(labels, tmp_path / "labels.zarr",
                                                  min_levels=5))
    assert volume.levels == 5
    plane = volume.plane(2)
    assert "4" in plane
    assert np.array_equal(np.asarray(plane[4]), labels[2, ::16, ::16])
    assert np.array_equal(label_region(plane, 4, 0, 0, 10, 10), labels[2, ::16, ::16][:10, :10])


def test_a_registered_3d_mask_has_a_level_for_every_image_level(tmp_path):
    from plexora.datasource import register_datasource
    from plexora.server.models.project import Project

    path, _ = vf.write_ome_volume(tmp_path / "deep.ome.tif", axes="CZYX", channels=2,
                                  depth=3, height=600, width=520, names=["DNA", "CD3"],
                                  levels=4)
    labels = np.zeros((3, 600, 520), dtype=np.uint16)
    labels[0:2, 10:40, 10:40] = 5
    tifffile.imwrite(tmp_path / "mask.tif", labels)
    table = tmp_path / "cells.csv"
    table.write_text("CellID,X,Y,DNA,CD3\n5,25,25,10,1\n", encoding="utf-8")
    register_datasource("deeplabels", path, table, x="X", y="Y", id_column="CellID",
                        segmentation=tmp_path / "mask.tif")
    project = Project.find("deeplabels")
    assert project.image.max_level == 4
    from plexora.server.volume import labels as volume_labels

    assert volume_labels.open_label_volume(project.segmentation.derived).levels == 4


# -- stacks get pyramids by voxels -------------------------------------------------


def test_a_stack_needs_a_pyramid_by_voxels_and_a_flat_image_by_side():
    from plexora.server.utils import image_pyramid as ip

    assert ip.pyramid_gaps((3, 4096, 4096), 1, "uint16") == []
    assert ip.pyramid_gaps((3, 4096, 4096), 1, "uint16", planes=1) == []
    assert ip.pyramid_gaps((600, 2048, 2048), 1, "uint16", planes=200)
    assert ip.pyramid_gaps((30, 1024, 1024), 1, "uint16", planes=10) == []
    assert ip.pyramid_gaps((600, 2048, 2048), 3, "uint16", planes=200) == []
    # 4096 x 4096 x 200 holds at most 64 M voxels a channel at 1/8 scale.
    assert ip.stack_min_levels(200, 4096, 4096) == 4
    assert ip.stack_min_levels(None, 4096, 4096) is None
    assert ip.stack_planes({"page_axes": [["c", 3], ["z", 50], ["t", 2]]}) == 100


def test_a_single_level_stack_is_imported_with_levels_down_to_one_that_fits(tmp_path,
                                                                           monkeypatch):
    from plexora.server.models.project import Project
    from plexora.server.utils import image_pyramid as ip

    # 8 planes of 256^2 against a 64 k-voxel rule: 256 and 128 are over it,
    # 64 (32 k voxels) is the first level that fits -- three levels, though
    # one 1024 px tile covers the plane.
    monkeypatch.setattr(ip, "STACK_LEVEL_VOXELS", 2**16)
    path, data = vf.write_ome_volume(tmp_path / "one.ome.tif", axes="CZYX", channels=2,
                                     depth=8, height=256, width=256, names=["DNA", "CD8"])
    register_image_datasource("onelevel", path)
    image = Project.find("onelevel").image
    assert image.max_level == 3
    copy = Project.find("onelevel").image.src
    with tifffile.TiffFile(copy) as handle:
        assert [level.shape[-2:] for level in handle.series[0].levels] == \
            [(256, 256), (128, 128), (64, 64)]
    data_model.load_datasource("onelevel")
    assert [info.height for info in data_model.volume_source.volume.levels] == [256, 128, 64]


def test_the_3d_copy_never_copies_a_stack_too_large_for_any_view(tmp_path):
    from plexora.server.volume import bricks
    from plexora.server.volume.axes import Spacing
    from plexora.server.volume.model import LevelInfo, Volume

    def volume(side, depth=200):
        return Volume(channels=1, depth=depth, frames=1, height=side, width=side,
                      dtype="uint16", spacing=Spacing.from_dict(None),
                      levels=(LevelInfo(index=0, height=side, width=side),),
                      source_kind="tiff")

    assert bricks.coarse_levels(volume(4096)) == []
    assert bricks.coarse_levels(volume(1024)) == [0]   # 400 MB: the fallback holds it
    assert bricks.coarse_levels(volume(512)) == [0]


# -- pooled levels in the 3-D copy -------------------------------------------------


def _fine_stack(tmp_path):
    # 0.5 µm pixels over 0.125 µm planes: pools of 4, 8 and 16 at levels 0..2.
    path, data = vf.write_ome_volume(tmp_path / "fine.ome.tif", axes="CZYX", channels=2,
                                     depth=40, height=300, width=260, levels=3,
                                     physical_z=0.125)
    return path, data, open_volume(path, z_semantics="depth")


def _naive(data, c, pool, rows, cols):
    planes = data[0, c, :, rows, cols]
    return np.stack([planes[z:z + pool].max(0) for z in range(0, planes.shape[0], pool)])


def test_pooled_levels_are_stored_and_read_back_as_the_planes_pool(tmp_path, monkeypatch):
    from plexora.server.volume import bricks

    monkeypatch.setattr(bricks, "LEVEL_BUDGET", 40 * 150 * 130 * 2)
    path, data, source = _fine_stack(tmp_path)
    try:
        volume = source.volume
        assert bricks.pooled_levels(volume) == [(0, 4), (1, 8), (2, 16)]
        store = bricks.Store(tmp_path / "volume_v1", volume, bricks.source_key(path))
        bricks.build_coarse(store, source)
        assert store.coarse_complete() and store.pooled_complete()
        bricked = bricks.adopt(source, store)

        reads = []
        original = source.read_block
        monkeypatch.setattr(source, "read_block",
                            lambda *args: reads.append(args) or original(*args))
        rows, cols = slice(0, 300), slice(0, 260)
        got = model.read_pooled(bricked, 0, 0, 1, 0, 39, 4, rows, cols)
        assert np.array_equal(got, _naive(data, 1, 4, slice(None), slice(None)))
        assert reads == [], "the pooled level is read, not the planes"
        # A pool that is a multiple of a stored one: pooled further.
        got = model.read_pooled(bricked, 0, 0, 0, 0, 39, 8, slice(20, 90), slice(5, 60))
        assert np.array_equal(got, _naive(data, 0, 8, slice(20, 90), slice(5, 60)))
        assert reads == []
        # Off a pool's start: from the planes, and still right.
        got = model.read_pooled(bricked, 0, 0, 0, 2, 33, 4, rows, cols)
        planes = data[0, 0, 2:34]
        assert np.array_equal(got, np.stack([planes[z:z + 4].max(0) for z in range(0, 32, 4)]))
        assert reads
        # The level route's read is the same either way.
        ask = serve.LevelAsk(t=0, level=1, voxel_format="u8", box=None, z0=0, z1=39,
                             pool=None, named=None)
        level1 = slice(0, 150), slice(0, 130)
        assert np.array_equal(serve.read_level(bricked, ask, 1, *level1, 8),
                              serve.read_level(source, ask, 1, *level1, 8))
    finally:
        source.close()


def test_only_a_large_deep_stack_with_a_z_step_gets_pooled_levels(tmp_path, monkeypatch):
    from plexora.server.volume import bricks

    path, _data, source = _fine_stack(tmp_path)
    try:
        assert not bricks.worth_pooling(source)          # under MIN_VOXELS
        monkeypatch.setattr(bricks, "MIN_VOXELS", 1)
        assert bricks.worth_pooling(source)
    finally:
        source.close()
    path, _ = vf.write_ome_volume(tmp_path / "noz.ome.tif", axes="CZYX", channels=1,
                                  depth=40, physical_z=None)
    flat = open_volume(path, z_semantics="depth")
    try:
        assert bricks.pooled_levels(flat.volume) == []
        assert not bricks.worth_pooling(flat)
    finally:
        flat.close()


# -- OME-Zarr: the coarse levels a stack lacks -------------------------------------


def _mean2(plane):
    h, w = plane.shape
    return plane.astype(float).reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))


def test_an_ome_zarr_stack_too_deep_for_3d_gets_levels_by_voxels(tmp_path, monkeypatch):
    from plexora.server.utils import image_pyramid
    from plexora.server.volume.sources import ngff

    path, data = vf.write_ngff_volume(tmp_path / "vox.zarr", channels=2, depth=8,
                                      height=128, width=128, levels=1)
    source = ngff.open_ngff_volume(path)
    # 8 x 128 x 128 is far under the real rule; a 16 k-voxel rule stands in.
    assert not ngff.needs_volume_extension(source)
    monkeypatch.setattr(image_pyramid, "STACK_LEVEL_VOXELS", 2**14)
    assert ngff.needs_volume_extension(source)
    assert ngff.extension_shapes(source) == [(64, 64), (32, 32)]
    assert ngff.extension_read_bytes(source) == 2 * 8 * 128 * 128 * 2
    built = ngff.build_volume_extension(source, tmp_path / "ext.zarr")
    assert built and not (tmp_path / "ext.zarr.building").exists()
    source.close()
    extended = ngff.open_ngff_volume(path, extension=built)
    assert [info.height for info in extended.volume.levels] == [128, 64, 32]
    level = np.asarray(extended.plane_pyramid(0, 5)[2][1])
    assert np.allclose(level, _mean2(_mean2(data[1, 5])), atol=1)


def test_a_large_derivation_runs_in_the_background_and_is_taken_up(tmp_path, client,
                                                                   monkeypatch):
    import threading
    import time

    from plexora.server.models import layer_jobs
    from plexora.server.models.project import Project
    from plexora.server.utils import image_pyramid, ome_zarr
    from plexora.server.volume.sources import ngff

    monkeypatch.setattr(image_pyramid, "STACK_LEVEL_VOXELS", 2**14)
    monkeypatch.setattr(ngff, "FOREGROUND_EXTENSION_BYTES", 0)
    release = threading.Event()
    real = ngff.build_volume_extension

    def gated(*args, **kwargs):
        release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(ngff, "build_volume_extension", gated)
    path, data = vf.write_ngff_volume(tmp_path / "bg.zarr", channels=2, depth=8,
                                      height=128, width=128, levels=1)
    register_image_datasource("bgext", path)
    # Where the levels go is recorded at import, though nothing is built then.
    pyramid = Project.find("bgext").image.pyramid
    assert pyramid and pyramid.endswith(ome_zarr.EXTENSION_NAME)
    if data_model.source != "bgext":
        data_model.load_datasource("bgext")
    try:
        source = data_model.volume_source
        assert source.pending_extension
        # The 3-D view waits for the real levels; a plane serves them now, as
        # subsamples of the store's own.
        assert len(source.volume.levels) == 1
        plane = source.plane_pyramid(0, 3)
        assert len(plane) == 3
        assert np.array_equal(np.asarray(plane[2][1]), data[1, 3, ::4, ::4])
        assert np.array_equal(plane[2][0, 5:10, 3:9], data[0, 3, 20:40:4, 12:36:4])
        state = client.get("/volume/bgext/prepare").get_json()
        assert state["state"] == "not_needed" and state["levels"]["status"] == "pending"
    finally:
        release.set()
    deadline = time.time() + 60
    while layer_jobs.tasks("bgext")[data_model.EXTENSION_TASK]["status"] == "pending":
        assert time.time() < deadline, "the derivation did not finish"
        time.sleep(0.05)
    assert layer_jobs.tasks("bgext")[data_model.EXTENSION_TASK]["status"] == "ready"
    assert source.pending_extension is None
    assert [info.height for info in source.volume.levels] == [128, 64, 32]
    level = np.asarray(source.plane_pyramid(0, 3)[2][1])
    assert np.allclose(level, _mean2(_mean2(data[1, 3])), atol=1)
    # The default plane the tile route holds was rebuilt from them.
    default = source.volume.default_view.z
    assert np.allclose(np.asarray(data_model.channels[2][1]),
                       _mean2(_mean2(data[1, default])), atol=1)


# -- OME-Zarr reads through the decoded-chunk cache --------------------------------


class _Counting:
    """A zarr array that counts its reads."""

    def __init__(self, array):
        self._array = array
        self.reads = 0

    def __getattr__(self, name):
        return getattr(self._array, name)

    def __getitem__(self, index):
        self.reads += 1
        return self._array[index]


def test_a_neighbouring_ome_zarr_read_decodes_nothing_new(tmp_path):
    from plexora.server.volume import chunk_cache

    path, data = vf.write_ngff_volume(tmp_path / "cc.zarr", channels=2, depth=6,
                                      height=128, width=128, chunks_z=2)
    source = open_volume(path)
    try:
        counting = _Counting(source._arrays[0])
        source._arrays[0] = counting
        first = source.read_block(0, 0, 1, 0, 5, slice(0, 40), slice(0, 64))
        assert np.array_equal(first, data[1, :, 0:40, 0:64])
        reads = counting.reads
        # The next section column over, inside the same 64 x 64 chunks.
        second = source.read_block(0, 0, 1, 0, 5, slice(40, 64), slice(0, 64))
        assert np.array_equal(second, data[1, :, 40:64, 0:64])
        assert counting.reads == reads
        # A whole level too large to keep is read directly, and keeps nothing.
        before = chunk_cache.CACHE.stats()
        budget = chunk_cache.CACHE.budget
        chunk_cache.CACHE.budget = 64
        try:
            whole = source.read_block(0, 0, 0, 0, 5, slice(0, 128), slice(0, 128))
        finally:
            chunk_cache.CACHE.budget = budget
        assert np.array_equal(whole, data[0])
        assert chunk_cache.CACHE.stats().get("misses") == before.get("misses")
    finally:
        source.close()


# -- drawn-up projections ----------------------------------------------------------


def test_a_drawn_up_projection_tile_is_final_and_says_so(tmp_path, client, monkeypatch):
    from plexora.server.models import views
    from plexora.server.routes import data_routes
    from plexora.server.volume import plane_view

    path, _data = vf.write_ngff_volume(tmp_path / "proj.zarr", channels=1, depth=8,
                                       levels=3, chunks_z=4)
    entry = register_image_datasource("proj", path)
    data_model.load_datasource("proj")
    src = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]
    monkeypatch.setattr(plane_view, "SERVED_SLAB_BYTES", 8 * 48 * 40 * 2)
    data_routes._tile_png_cache.clear()
    views._views.clear()
    url = f"/generated/data/proj/{src}/0/0_0.png?q=hd&view=z0-7max"
    drawn = client.get(url)
    assert drawn.headers.get("X-Plexora-Served-Steps") == "1"
    assert drawn.headers["Cache-Control"] == "private, max-age=31536000"
    # Kept: the next ask is a cache hit, which carries no stale flags.
    again = client.get(url)
    assert again.data == drawn.data and "X-Plexora-Served-Steps" not in again.headers


def test_an_agent_projection_read_keeps_to_the_budget_and_says_by_how_much(tmp_path,
                                                                          monkeypatch):
    from plexora.server.utils.source_image import SourceImage
    from plexora.server.volume import plane_view

    path, data = vf.write_ngff_volume(tmp_path / "agent.zarr", channels=1, depth=8,
                                      levels=3, chunks_z=4)
    register_image_datasource("agentproj", path)
    monkeypatch.setattr(plane_view, "SERVED_SLAB_BYTES", 8 * 48 * 40 * 2)
    with SourceImage("agentproj", view="z0-7max") as exact:
        block, _ = exact.read(0, 0, (0, 0, 80, 96))
        assert np.array_equal(block, data[0].max(0)) and exact.coarsened == 0
    with SourceImage("agentproj", view="z0-7max", budgeted=True) as budgeted:
        block, _ = budgeted.read(0, 0, (0, 0, 80, 96))
        assert budgeted.coarsened == 1
        coarse = data[0, :, ::2, ::2].max(0)
        assert np.array_equal(block, np.repeat(np.repeat(coarse, 2, 0), 2, 1)[:96, :80])


# -- the mini-map follows the plane ------------------------------------------------


def test_the_mini_map_overview_is_of_the_plane_on_screen(tmp_path, client):
    path, _data = vf.write_ome_volume(tmp_path / "mm.ome.tif", axes="CZYX", channels=2,
                                      depth=6, height=256, width=240, names=["DNA", "CD8"])
    register_image_datasource("mm", path)
    data_model.load_datasource("mm")
    default = data_model.generate_channel_overview("mm", "CD8")
    assert data_model.generate_channel_overview("mm", "CD8", None) == default
    one = data_model.generate_channel_overview("mm", "CD8", "z1")
    four = data_model.generate_channel_overview("mm", "CD8", "z4")
    assert one != four
    plain = client.get("/generated/overview/mm/CD8")
    planed = client.get("/generated/overview/mm/CD8?view=z4")
    assert plain.status_code == planed.status_code == 200
    assert plain.headers["ETag"] != planed.headers["ETag"]
    assert planed.data == four
    assert client.get("/generated/overview/mm/CD8?view=nonsense").status_code == 200


def test_the_mini_map_of_a_3d_view_is_one_picture_under_both_spellings(tmp_path, client):
    """In 3-D the map asks for the cut as `z{lo}-{hi}max`, inclusive, even for
    the whole stack (miniMap._volumeToken): the spelling `zmax` canonicalises
    to, so the two are one picture and one ETag, and not any one plane."""
    path, _data = vf.write_ome_volume(tmp_path / "mm3.ome.tif", axes="CZYX", channels=2,
                                      depth=6, height=256, width=240, names=["DNA", "CD8"])
    register_image_datasource("mm3", path)
    data_model.load_datasource("mm3")
    bare = client.get("/generated/overview/mm3/CD8?view=zmax")
    spelled = client.get("/generated/overview/mm3/CD8?view=z0-5max")
    plane = client.get("/generated/overview/mm3/CD8?view=z1")
    assert bare.status_code == spelled.status_code == plane.status_code == 200
    assert bare.headers["ETag"] == spelled.headers["ETag"]
    assert bare.data == spelled.data
    assert spelled.data != plane.data
    cut = client.get("/generated/overview/mm3/CD8?view=z2-4max")
    assert cut.status_code == 200 and cut.headers["ETag"] != spelled.headers["ETag"]
