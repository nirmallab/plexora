"""An Imaris (.ims) file at a web address, as an image and as a mask.

Read with h5py over `remote_store.RemoteFile`: the file's metadata through
the block cache, its chunks by offset, a tile's chunks fetched in one batch.
Against a real HTTP server (`remote_fixtures`), so what is pinned is what h5py
actually asks the host for.
"""

import numpy as np
import pytest

from plexora.server.providers.remote import RemoteImageProvider
from plexora.server.utils import remote_image, remote_store
from plexora.server.volume.sources import ims, is_volume_only_path, open_volume

from tests import test_ims_masks as ims_files
from tests.remote_fixtures import cache_root, http_store  # noqa: F401

DEPTH, HEIGHT, WIDTH = 12, 200, 260


def _stack(channels=3, seed=0):
    rng = np.random.default_rng(seed)
    # Smooth enough to compress, as a real stain does.
    base = rng.integers(0, 4000, (DEPTH, HEIGHT // 10 + 1, WIDTH // 10 + 1)).astype(np.uint16)
    big = np.repeat(np.repeat(base, 10, axis=1), 10, axis=2)[:, :HEIGHT, :WIDTH]
    return [(f"ch{c}", (big // (c + 1)).astype(np.uint16)) for c in range(channels)]


@pytest.fixture
def served(tmp_path):
    folder = tmp_path / "served"
    folder.mkdir()
    return folder


def test_an_ims_address_is_named_by_its_suffix_without_a_request(cache_root):
    url = "https://example.org/data/stack.ims"
    assert remote_image.kind_of(url, probe=False) == remote_image.IMS
    assert remote_store.is_file_url(url)
    assert ims.is_ims_path(url) and ims.is_remote_ims(url)
    assert is_volume_only_path(url)
    assert not ims.is_remote_ims("https://example.org/data/stack.tif")


def test_a_remote_stack_reads_the_same_pixels_as_on_disk(served, http_store):
    path = ims_files.write_ims(served / "stack.ims", _stack(), chunks=(4, 64, 64))
    server = http_store()
    url = server.url("stack.ims")

    local = ims.open_ims_volume(path)
    remote = open_volume(url)
    try:
        assert isinstance(remote, ims.ImsVolumeSource)
        assert remote.volume.channels == 3 and remote.volume.depth == DEPTH
        assert remote.volume.channel_names == ("ch0", "ch1", "ch2")
        assert remote.volume.spacing.z.value == local.volume.spacing.z.value
        rows, cols = slice(30, 170), slice(10, 250)
        for channel in range(3):
            np.testing.assert_array_equal(
                remote.read_block(0, 0, channel, 3, 6, rows, cols),
                local.read_block(0, 0, channel, 3, 6, rows, cols))
        # Every chunk was read by offset, not through h5py's own path.
        assert all(remote._direct.values())
        # Coarse levels, through the plane pyramid the viewer reads.
        plane = remote.plane_pyramid(0, 5)
        np.testing.assert_array_equal(np.asarray(plane[1][2]), np.asarray(local.plane_pyramid(0, 5)[1][2]))
    finally:
        remote.close()
        local.close()


def test_a_tile_read_twice_is_fetched_once(served, http_store):
    ims_files.write_ims(served / "stack.ims", _stack(channels=1), chunks=(4, 64, 64))
    server = http_store()
    source = open_volume(server.url("stack.ims"))
    try:
        source.read_block(0, 0, 0, 4, 4, slice(0, 128), slice(0, 128))
        server.clear()
        source.read_block(0, 0, 0, 5, 5, slice(0, 128), slice(0, 128))
        assert server.count() == 0
    finally:
        source.close()


def test_the_provider_serves_a_remote_ims_as_a_volume(served, http_store):
    ims_files.write_ims(served / "stack.ims", _stack(), chunks=(4, 64, 64))
    server = http_store()
    provider = RemoteImageProvider(server.url("stack.ims"))
    channels, _overview, _metadata = provider.open()
    try:
        assert provider.volume() is not None
        assert np.asarray(channels[0][1]).shape == (HEIGHT, WIDTH)
    finally:
        provider.volume().close()


def test_a_remote_ims_mask_reads_its_ids(served, http_store):
    cells = ims_files._cells(depth=6, height=70, width=90)
    path = ims_files.write_ims(served / "cells.ims",
                               [("stain", ims_files._stain(6, 70, 90)), ("mask", cells)])
    server = http_store()
    url = server.url("cells.ims")

    assert ims.mask_verdict(url) is True
    assert ims.label_shape(url) == (6, 70, 90)
    labels = ims.ImsLabels(url)
    try:
        assert labels.channel == 1
        for z in range(6):
            np.testing.assert_array_equal(labels[z], cells[z])
        np.testing.assert_array_equal(labels.plane(0, 2, slice(10, 40), slice(5, 60)),
                                      cells[2, 10:40, 5:60])
    finally:
        labels.close()
    assert path.exists()


def test_a_remote_ims_mask_becomes_a_label_volume_in_the_project(tmp_path, served, http_store):
    from plexora.server.models import data_model
    from plexora.server.utils import segmentation_pyramid as sp
    from plexora.server.volume import labels as volume_labels

    cells = ims_files._cells()
    ims_files.write_ims(served / "cells.ims", [("cells", cells)])
    server = http_store()
    url = server.url("cells.ims")
    project = tmp_path / "project"
    project.mkdir()

    assert volume_labels.label_volume_shape(url) == cells.shape
    built = data_model.resolve_outline_segmentation(url, project, mode=sp.MODE_FILLED)
    assert volume_labels.is_label_volume(built)
    assert built.endswith("cells_labels_v1.zarr") and str(project) in built
    volume = volume_labels.open_label_volume(built)
    for z in range(cells.shape[0]):
        np.testing.assert_array_equal(np.asarray(volume.plane(z)["0"]), cells[z])
    # A second resolve adopts what was built: same address, same fingerprint.
    assert data_model.resolve_outline_segmentation(url, project, mode=sp.MODE_FILLED) == built


def test_a_planar_remote_ims_mask_converts_to_a_2d_pyramid(tmp_path, served, http_store):
    from plexora.server.models import data_model
    from plexora.server.utils import segmentation_pyramid as sp

    cells = ims_files._cells(depth=1)
    ims_files.write_ims(served / "flat.ims", [("cells", cells)])
    server = http_store()
    url = server.url("flat.ims")
    plane, close = sp._open_level_zero(url)
    try:
        np.testing.assert_array_equal(np.asarray(plane), cells[0])
    finally:
        close()
    project = tmp_path / "project"
    project.mkdir()
    built = data_model.resolve_outline_segmentation(url, project, mode=sp.MODE_FILLED)
    assert str(project) in built


def test_the_import_proposal_for_an_ims_address(served, http_store):
    from plexora.datasource import _sniff_quick_view_kind
    from plexora.server.models import import_proposal

    ims_files.write_ims(served / "stack.ims", _stack(), chunks=(4, 64, 64))
    ims_files.write_ims(served / "stack_cells.ims", [("cells", ims_files._cells())])
    server = http_store()
    assert _sniff_quick_view_kind(server.url("stack.ims")) == "ome_tiff"

    proposal = import_proposal.inspect_paths([server.url("stack.ims")]).to_dict()
    [sample] = proposal["samples"]
    [layer] = sample["layers"]
    assert layer["kind"] == "image" and layer["src"] == server.url("stack.ims")
    assert not proposal.get("questions")

    proposal = import_proposal.inspect_paths([server.url("stack_cells.ims")]).to_dict()
    [layer] = proposal["samples"][0]["layers"]
    assert layer["kind"] == "labels" and layer["role"] == "mask"


def test_a_remote_ims_registers_and_serves_its_planes(tmp_path, served, http_store, monkeypatch):
    from plexora import datasource
    from plexora.server.models import data_model

    from tests.helpers import use_data_root

    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    path = ims_files.write_ims(served / "stack.ims", _stack(), chunks=(4, 64, 64))
    server = http_store()
    datasource.register_image_datasource(name="remoteims", image=server.url("stack.ims"))
    data_model.load_datasource("remoteims")
    source = data_model.volume_source
    local = ims.open_ims_volume(path)
    try:
        assert source is not None and source.volume.depth == DEPTH
        assert data_model.get_channel_names("remoteims")[:3] == ["ch0", "ch1", "ch2"]
        np.testing.assert_array_equal(source.read_block(0, 0, 1, 7, 7, slice(0, 64), slice(0, 64)),
                                      local.read_block(0, 0, 1, 7, 7, slice(0, 64), slice(0, 64)))
    finally:
        local.close()


def test_a_missing_remote_ims_says_so(served, http_store):
    server = http_store()
    with pytest.raises(FileNotFoundError):
        open_volume(server.url("nothing.ims"))


def test_a_dropped_connection_is_an_error_not_a_hang(served, http_store):
    import concurrent.futures

    ims_files.write_ims(served / "stack.ims", _stack(), chunks=(4, 64, 64))
    server = http_store()
    source = open_volume(server.url("stack.ims"))
    try:
        def read():
            # A channel and planes nothing has read: its chunk index and its
            # chunks both need the network.
            return source.read_block(0, 0, 2, 8, 11, slice(0, HEIGHT), slice(0, WIDTH))

        with server.outage(), concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(read)
            with pytest.raises(Exception) as caught:
                future.result(timeout=60)
            assert not isinstance(caught.value, concurrent.futures.TimeoutError)
        # And the source still reads once the host is back.
        assert read().shape == (4, HEIGHT, WIDTH)
    finally:
        source.close()
