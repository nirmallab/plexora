"""A TIFF at a web address, registered, served and cached end to end.

The same files the brightfield and OME-TIFF tests register from disk, served
over a real HTTP server. What has to hold is that the record is the one a
local registration writes, the pixels are the local pixels, and a second open
needs no network.
"""

import numpy as np
import pytest
import tifffile as tf

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.providers.remote import RemoteImageProvider
from plexora.server.utils import brightfield, remote_store, tiff_region
from tests.brightfield_fixtures import (write_planar_fluorescence,
                                        write_rgb_ome_tiff, write_svs_like)
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "served"
    root.mkdir(exist_ok=True)
    return root


def test_a_fluorescence_tiff_registers_and_serves_the_local_pixels(served, http_store):
    local = served / "panel.ome.tif"
    write_planar_fluorescence(local)
    server = http_store()
    url = server.url("panel.ome.tif")

    entry = datasource.register_image_datasource(name="webtiff", image=url)

    assert entry["channelFile"] == url
    assert entry["image_kind"] == "ome_tiff"
    assert [c["name"] for c in entry["imageData"]] == ["DAPI", "CD3", "Ki67"]
    data_model.load_datasource("webtiff", reload=True)
    assert isinstance(data_model._providers.image, RemoteImageProvider)

    with tf.TiffFile(str(local)) as tiff:
        expected = tiff.series[0].asarray()
    pyramid = data_model.channels
    for channel in range(3):
        assert np.array_equal(np.asarray(pyramid[0][channel, 0:300, 100:500]),
                              expected[channel, 0:300, 100:500])
    key = entry["imageData"][1]["src"].rstrip("/").rsplit("/", 1)[-1]
    encoded, mimetype = data_model.encode_tile("webtiff", key, 0, "0_0.png", "default")
    assert encoded and mimetype == "image/webp"


def test_an_rgb_tiff_is_brightfield_with_the_local_colours(served, http_store):
    local = served / "he.ome.tif"
    write_rgb_ome_tiff(local, pyramid=2)
    server = http_store()
    url = server.url("he.ome.tif")

    entry = datasource.register_image_datasource(name="webhe", image=url)

    assert entry["image_kind"] == "brightfield"
    remote = brightfield.open_rgb(url)
    here = brightfield.open_rgb(local)
    assert len(remote) == len(here)
    for level in range(len(here)):
        assert np.array_equal(remote[level].rgb[0:200, 50:300],
                              here[level].rgb[0:200, 50:300])


def test_an_svs_reads_its_aperio_pixel_size(served, http_store):
    write_svs_like(served / "slide.svs")
    server = http_store()
    url = server.url("slide.svs")
    assert brightfield.detect_image_type(url).verdict == brightfield.BRIGHTFIELD
    local = brightfield.physical_metadata(served / "slide.svs")
    assert local and brightfield.physical_metadata(url) == local


def test_a_second_open_needs_no_network(served, http_store, cache_root):
    write_planar_fluorescence(served / "panel.ome.tif")
    server = http_store()
    url = server.url("panel.ome.tif")
    first = tiff_region.open_image(url)
    block = np.asarray(first[0][1, 0:256, 0:256])
    first.close()
    remote_store.cache_index().flush()
    remote_store._reset_for_tests(cache_root)
    server.clear()
    with server.outage():
        again = tiff_region.open_image(url)
        assert np.array_equal(np.asarray(again[0][1, 0:256, 0:256]), block)
        again.close()
    assert server.count(method="GET") == 0


def test_a_flat_tiff_gets_derived_levels(served, http_store, tmp_path, monkeypatch):
    # A small image standing in for a large flat one: with the source limit
    # at one tile, every level past the first needs deriving.
    monkeypatch.setattr(brightfield, "MAX_SOURCE_SIDE", 1024)
    data = np.random.default_rng(1).integers(0, 4000, size=(2, 2600, 2300),
                                             dtype=np.uint16)
    tf.imwrite(served / "flat.tif", data, tile=(256, 256))
    server = http_store()
    entry = datasource.register_image_datasource(name="flat", image=server.url("flat.tif"))
    assert entry["maxLevel"] == 3
    assert entry.get("imagePyramid")
    data_model.load_datasource("flat", reload=True)
    tile = data_model.read_tile(data_model.channels, 0, 2, "0_0", 1024, 1024)
    assert tile.shape == (650, 575)


def test_an_openslide_only_slide_needs_a_local_copy(served, http_store):
    from plexora.server.utils import remote_image

    with pytest.raises(ValueError, match="needs a local copy"):
        remote_image.kind_of("https://example.org/slides/slide.mrxs")


def test_quick_view_takes_a_tiff_address(served, http_store):
    from plexora.datasource import _sniff_quick_view_kind

    write_planar_fluorescence(served / "slide.ome.tif")
    server = http_store()
    assert _sniff_quick_view_kind(server.url("slide.ome.tif")) == "ome_tiff"


def test_the_import_proposal_for_a_tiff_address(served, http_store):
    from plexora.server.models import import_proposal

    write_svs_like(served / "slide.svs")
    server = http_store()
    proposal = import_proposal.inspect_paths([server.url("slide.svs")]).to_dict()
    [sample] = proposal["samples"]
    [layer] = sample["layers"]
    assert layer["kind"] == "image" and layer["src"] == server.url("slide.svs")
    assert layer["modality"] == "he"
    assert layer["geometry"]["width"] == 1280
    assert layer["pixelSize"] or layer.get("pixel_size")


def test_the_detect_route_answers_for_a_tiff_address(served, http_store):
    import plexora

    write_svs_like(served / "slide.svs")
    server = http_store("listing")
    client = plexora.app.test_client()
    found = client.post("/detect_image_type", json={"path": server.url("slide.svs")}).get_json()
    assert found["verdict"] == "brightfield"
    folder = client.post("/detect_image_type", json={"path": server.url()}).get_json()
    assert folder["verdict"] is None


def test_a_thumbnail_for_a_remote_tiff(served, http_store):
    write_planar_fluorescence(served / "panel.ome.tif")
    server = http_store()
    datasource.register_image_datasource(name="thumbtiff", image=server.url("panel.ome.tif"))
    assert data_model.generate_thumbnail("thumbtiff") is not None
