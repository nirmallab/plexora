"""Deep and chunked stacks: what two real test images showed was missing.

A mesoSPIM light-sheet brain (OME-Zarr 0.5, 1937 planes of 2048x2048, shards
ten planes deep) and an Imaris confocal stack (6 channels, 190 planes, gzip
chunks 64 planes deep) opened, but:

- a full-depth projection tile read every plane at once (4 GB for one tile)
  and timed out; a served tile now keeps to a budget, and stores chunked
  across z are read in bounded blocks of whole chunks;
- an Imaris section, window scan or coarse level decoded each 64-plane chunk
  once per plane;
- Imaris's "(name not specified)" placeholder became six channels of one name;
- a local OME-Zarr stack registered without its shape;
- the 3-D view's 8-bit voxels left the light-sheet stack two grey levels, and
  its 1937-texel depth was sampled one plane in four.
"""

import h5py
import numpy as np
import pytest

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.volume import plane_view
from plexora.server.volume.plane_view import plane_pyramid, section_plane_of
from plexora.server.volume.sources import open_volume
from plexora.server.volume.viewspec import ViewSpec
from plexora.server.volume.windows import volume_window
from tests import volume_fixtures as vf


@pytest.fixture()
def client():
    return plexora.app.test_client()


def _count_blocks(monkeypatch, source):
    """Record (z0, z1) of every `read_block` on `source`."""
    reads = []
    original = source.read_block

    def counted(level, t, channel, z0, z1, rows, cols):
        reads.append((z0, z1))
        return original(level, t, channel, z0, z1, rows, cols)

    monkeypatch.setattr(source, "read_block", counted)
    return reads


# -- projections ---------------------------------------------------------


def test_a_served_projection_too_deep_for_its_tile_is_computed_a_level_coarser(
        tmp_path, monkeypatch):
    path, data = vf.write_ngff_volume(tmp_path / "v.ome.zarr", depth=8, levels=3, chunks_z=4)
    source = open_volume(path)
    try:
        spec = ViewSpec.parse("z0-7max", source.volume)
        # A level-0 tile of all 8 planes costs 8 x 96 x 80 x 2 bytes; allow a quarter.
        monkeypatch.setattr(plane_view, "SERVED_SLAB_BYTES", 8 * 48 * 40 * 2)
        served = plane_pyramid(source, spec, served=True)
        coarse = data[1, :, ::2, ::2].max(0)
        drawn_up = np.repeat(np.repeat(coarse, 2, axis=0), 2, axis=1)
        assert np.array_equal(served["0"][1], drawn_up[:96, :80])
        assert np.array_equal(served["0"][1, 3:50, 5:61], drawn_up[3:50, 5:61])
        # A tile that fits is computed at its own level ...
        assert np.array_equal(served["1"][1], coarse)
        # ... and a read that is not served (agents, regions, pins) is exact.
        assert np.array_equal(plane_pyramid(source, spec)["0"][1], data[1].max(0))
    finally:
        source.close()


def test_a_z_chunked_slab_is_read_in_bounded_whole_chunk_blocks(tmp_path, monkeypatch):
    path, data = vf.write_ngff_volume(tmp_path / "v.ome.zarr", depth=12, chunks_z=3)
    source = open_volume(path)
    try:
        assert source.prefers_block and source.z_chunk == 3
        reads = _count_blocks(monkeypatch, source)
        # Room for four planes of the tile: rounded down to whole 3-plane chunks.
        monkeypatch.setattr(plane_view, "BLOCK_BYTES", 4 * 96 * 80 * 2)
        slab = plane_pyramid(source, ViewSpec.parse("z0-11max", source.volume))
        assert np.array_equal(slab["0"][2], data[2].max(0))
        assert reads == [(0, 2), (3, 5), (6, 8), (9, 11)]
    finally:
        source.close()


# -- Imaris ----------------------------------------------------------------


def test_an_imaris_section_reads_its_planes_in_one_block(tmp_path, monkeypatch):
    path, data = vf.write_ims(tmp_path / "v.ims", channels=2, depth=6)
    source = open_volume(path)
    try:
        reads = _count_blocks(monkeypatch, source)
        section = plane_pyramid(source, ViewSpec.parse("xzy10", source.volume))["0"][1]
        anisotropy = source.volume.spacing.anisotropy()
        expected = np.stack([data[0, 1, section_plane_of(row, 6, anisotropy, 0), 10, :]
                             for row in range(section.shape[0])])
        assert np.array_equal(section, expected)
        assert reads == [(0, 5)]
    finally:
        source.close()


def test_an_imaris_level_with_fewer_planes_is_read_in_one_span(tmp_path):
    path, data = vf.write_ims(tmp_path / "v.ims", channels=1, depth=6, levels=1)
    # Imaris halves z as well as x and y once its voxels are cubic.
    coarse = np.ascontiguousarray(data[0, 0, ::2, ::2, ::2])
    with h5py.File(str(path), "a") as handle:
        group = handle.create_group("DataSet/ResolutionLevel 1/TimePoint 0/Channel 0")
        group.create_dataset("Data", data=coarse, chunks=(3, 16, 16))
        for key, size in (("ImageSizeX", 40), ("ImageSizeY", 48), ("ImageSizeZ", 3)):
            group.attrs[key] = vf._attr(size)
    source = open_volume(path)
    try:
        block = source.read_block(1, 0, 0, 0, 5, slice(0, 48), slice(0, 40))
        assert np.array_equal(block, coarse[[0, 0, 1, 1, 2, 2]])
        # The same plane the plane pyramid shows at that level.
        assert np.array_equal(np.asarray(source.plane_pyramid(0, 3)["1"][0]), coarse[1])
    finally:
        source.close()


def test_the_window_scan_reads_whole_chunks_once(tmp_path, monkeypatch):
    path, data = vf.write_ims(tmp_path / "v.ims", channels=2, depth=8)
    source = open_volume(path)
    try:
        reads = _count_blocks(monkeypatch, source)
        window, how = volume_window(source, 1)
        assert window == (0.0, float(data[0, 1].max())) and how["exact"]
        assert reads == [(0, 7)]
    finally:
        source.close()


def test_imaris_placeholder_names_are_no_names(tmp_path):
    path, _ = vf.write_ims(tmp_path / "unnamed.ims", channels=2,
                           names=["(name not specified)"] * 2)
    entry = register_image_datasource("unnamed", path)
    assert [c["name"] for c in entry["imageData"]] == ["Channel 1", "Channel 2"]


def test_repeated_imaris_names_are_suffixed(tmp_path):
    path, _ = vf.write_ims(tmp_path / "twice.ims", channels=3, names=["DAPI", "CD8", "DAPI"])
    source = open_volume(path)
    try:
        assert source.volume.channel_names == ("DAPI", "CD8", "DAPI_1")
    finally:
        source.close()


# -- OME-Zarr ------------------------------------------------------------------


def test_a_local_ome_zarr_stack_records_its_shape(tmp_path):
    from tests.ngff_fixtures import write_ngff

    path, _ = vf.write_ngff_volume(tmp_path / "v.ome.zarr", depth=5)
    entry = register_image_datasource("zstack", path)
    assert entry["imageShape"] == {"t": 1, "c": 3, "z": 5, "y": 96, "x": 80}
    assert entry["imageDefaultView"] == "z2"
    assert entry["imageZSemantics"] == "depth"
    flat = register_image_datasource("flat", write_ngff(tmp_path / "flat.ome.zarr"))
    assert "imageShape" not in flat


# -- the 3-D view's level ---------------------------------------------------------


def _level(response):
    shape = tuple(int(v) for v in response.headers["X-Plexora-Shape"].split(","))
    qmin, qmax = (float(v) for v in response.headers["X-Plexora-Window"].split(","))
    return shape, qmin, qmax


def test_the_hd_level_is_half_floats_with_its_own_contrast_hint(tmp_path, client):
    path, data = vf.write_ome_volume(tmp_path / "vol.ome.tif", axes="CZYX", channels=2,
                                     depth=5)
    entry = register_image_datasource("vol", path)
    data_model.load_datasource("vol")
    name = entry["imageData"][1]["fullname"]
    response = client.get(f"/volume/vol/level?channel={name}&level=0&format=f16")
    assert response.status_code == 200
    assert response.headers["X-Plexora-Format"] == "f16"
    shape, qmin, qmax = _level(response)
    assert shape == (5, 96, 80)
    unit = np.frombuffer(response.data, dtype="<f2").reshape(shape)
    expected = np.clip((data[0, 1].astype(np.float32) - qmin) / (qmax - qmin), 0, 1)
    assert np.array_equal(unit, expected.astype("<f2"))
    # No contrast of its own: the 3-D view is drawn with the channel's one
    # contrast, like every plane and projection, so a level costs no more
    # than its read and its quantization.
    assert "X-Plexora-Hint" not in response.headers
    assert client.get(f"/volume/vol/level?channel={name}&format=rgb").status_code == 400


def test_a_fine_z_step_is_max_pooled_into_cubic_voxels(tmp_path, client):
    # Eight planes 0.25 µm apart under 0.5 µm pixels: two planes to a voxel.
    path, data = vf.write_ome_volume(tmp_path / "fine.ome.tif", axes="CZYX", channels=1,
                                     depth=8, physical_z=0.25)
    entry = register_image_datasource("fine", path)
    data_model.load_datasource("fine")
    name = entry["imageData"][0]["fullname"]
    response = client.get(f"/volume/fine/level?channel={name}&level=0")
    assert response.headers["X-Plexora-ZPool"] == "2"
    shape, qmin, qmax = _level(response)
    assert shape == (4, 96, 80)
    pooled = data[0, 0].reshape(4, 2, 96, 80).max(axis=1)
    expected = np.clip(np.rint((pooled.astype(np.float32) - qmin) / (qmax - qmin) * 255),
                       0, 255).astype(np.uint8)
    assert np.array_equal(np.frombuffer(response.data, np.uint8).reshape(shape), expected)
