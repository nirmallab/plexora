"""An OME-Zarr image at a web address, registered and served end to end.

The twin of test_register_zarr_image_datasource.py with the store behind a real
HTTP server. What has to hold is that nothing between registration and an
encoded tile needs a path -- and the three things that do differ for a remote
image (who opens it, how its identity is known, how its quantisation ceiling is
found) behave as documented in providers/remote.py.
"""

import json

import numpy as np
import pytest
import zarr

from plexora import datasource
from plexora.server import providers
from plexora.server.models import data_model, layer_sources
from plexora.server.models.project import Project
from plexora.server.providers.remote import RemoteImageProvider, sampled_window
from plexora.server.utils import ome_zarr, remote_store
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, http_store, touch_later  # noqa: F401


@pytest.fixture
def served(tmp_path):
    return tmp_path / "served"


def test_register_load_and_serve(served, http_store):
    write_ngff(served / "slide.ome.zarr", shape=(2, 600, 600), levels=2,
               labels=["DNA", "CD3"], version="0.5")
    server = http_store()
    url = server.url("slide.ome.zarr")

    entry = datasource.register_image_datasource(name="web", image=url)

    assert entry["channelFile"] == url
    assert entry["image_kind"] == "ome_zarr"
    assert entry["width"] == 600 and entry["maxLevel"] == 2
    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3"]

    data_model.load_datasource("web", reload=True)
    assert isinstance(data_model._providers.image, RemoteImageProvider)
    # Computed here, only the bytes travel: nothing is proxied to a node.
    assert data_model._remote is False
    assert not data_model._providers.has_remote

    key = entry["imageData"][1]["src"].rstrip("/").rsplit("/", 1)[-1]
    encoded, mimetype = data_model.encode_tile("web", key, 1, "0_0.png", "default")
    assert encoded and mimetype == "image/webp"


def test_the_project_records_only_the_canonical_url(served, http_store):
    write_ngff(served / "slide.ome.zarr", shape=(1, 64, 64), levels=1, version="0.4")
    server = http_store()
    url = server.url("slide.ome.zarr")
    messy = "  '" + url.replace("http://", "HTTP://").replace("/slide", "//slide") + "/'  "
    datasource.register_image_datasource(name="canon", image=messy)
    assert Project.load("canon").image.src == server.url("slide.ome.zarr")


def test_a_layer_at_a_web_address_gets_the_remote_provider(served, http_store):
    write_ngff(served / "slide.ome.zarr", shape=(1, 64, 64), levels=1)
    server = http_store()

    class Layer:
        binding = None
        src = server.url("slide.ome.zarr")
        pyramid = None
        render = {}

    assert isinstance(layer_sources.resolve_layer_provider(None, Layer()),
                      RemoteImageProvider)


def test_a_flat_store_gets_derived_levels_keyed_on_the_etag(served, http_store):
    write_ngff(served / "flat.ome.zarr", shape=(1, 3000, 2500), levels=1, version="0.5")
    server = http_store()

    entry = datasource.register_image_datasource(name="flat", image=server.url("flat.ome.zarr"))

    assert entry["maxLevel"] == 3
    assert entry["imagePyramidKey"].startswith("etag:")
    data_model.load_datasource("flat", reload=True)
    assert data_model.read_tile(data_model.channels, 0, 2, "0_0", 1024, 1024).shape == (750, 625)


def test_the_fingerprint_follows_the_metadata_document(served, http_store):
    path = write_ngff(served / "slide.ome.zarr", shape=(1, 64, 64), levels=1, version="0.5")
    server = http_store()
    provider = RemoteImageProvider(server.url("slide.ome.zarr"))
    before = provider.fingerprint()
    assert before is not None and before.matches(provider.fingerprint())

    doc = json.loads((path / "zarr.json").read_text())
    doc["attributes"]["ome"]["multiscales"][0]["name"] = "renamed"
    (path / "zarr.json").write_text(json.dumps(doc))
    touch_later(path / "zarr.json")
    remote_store._probes.clear()
    assert not before.matches(provider.fingerprint())


def test_thumbnail_from_a_web_address(served, http_store):
    write_ngff(served / "slide.ome.zarr", shape=(2, 300, 300), levels=1)
    server = http_store()
    datasource.register_image_datasource(name="thumb", image=server.url("slide.ome.zarr"))
    assert data_model.generate_thumbnail("thumb") is not None


def test_the_window_is_sampled_and_covers_the_planted_peak(served, http_store, monkeypatch):
    from plexora.server.providers import remote

    monkeypatch.setattr(remote, "WINDOW_SAMPLE_CHUNKS", 16)
    path = write_ngff(served / "slide.ome.zarr", shape=(1, 256, 256), levels=2, version="0.5")
    group = zarr.open_group(str(path), mode="a")
    plane = np.asarray(group["0"][:])
    plane[0, 130, 70] = 60000
    group["0"][:] = plane
    server = http_store()
    pyramid = ome_zarr.open_image(server.url("slide.ome.zarr"))

    low, high = sampled_window(pyramid, 0)

    assert low == 0.0
    # 64-px chunks in a 256-px plane, 16 of them: every chunk is sampled.
    assert high >= 60000
    assert high <= float(np.iinfo("uint16").max)


def test_the_window_reads_a_bounded_sample(served, http_store, monkeypatch):
    from plexora.server.providers import remote

    monkeypatch.setattr(remote, "WINDOW_SAMPLE_CHUNKS", 4)
    write_ngff(served / "big.ome.zarr", shape=(1, 1024, 1024), levels=3, version="0.5")
    server = http_store()
    pyramid = ome_zarr.open_image(server.url("big.ome.zarr"))
    server.clear()

    sampled_window(pyramid, 0)

    # 256 level-0 chunks in the plane, and four of them are read.
    level0 = {p for m, p, s in server.requests if m == "GET" and "/big.ome.zarr/0/c/" in p}
    assert len(level0) == 4


def test_resolve_providers_dispatches_on_the_address(served, http_store):
    write_ngff(served / "slide.ome.zarr", shape=(1, 64, 64), levels=1)
    server = http_store()
    datasource.register_image_datasource(name="disp", image=server.url("slide.ome.zarr"))
    resolved = providers.resolve_providers(Project.load("disp"))
    assert isinstance(resolved.image, RemoteImageProvider)
    assert resolved.image.locator.path == server.url("slide.ome.zarr")
    assert resolved.has_remote is False


def test_quick_view_sniffs_a_web_address(served, http_store):
    from plexora.datasource import _sniff_quick_view_kind

    write_ngff(served / "slide.ome.zarr", shape=(1, 64, 64), levels=1)
    server = http_store()
    assert _sniff_quick_view_kind(server.url("slide.ome.zarr")) == "ome_zarr"
    with pytest.raises(ValueError, match="OME-Zarr"):
        _sniff_quick_view_kind(server.url("slide.tif"))


def test_the_import_proposal_for_a_web_address(served, http_store):
    from plexora.server.models import import_proposal

    write_ngff(served / "slide.ome.zarr", shape=(2, 128, 128), levels=1,
               labels=["DNA", "CD3"], version="0.5")
    server = http_store()
    proposal = import_proposal.inspect_paths([server.url("slide.ome.zarr")]).to_dict()
    [sample] = proposal["samples"]
    [layer] = sample["layers"]
    assert layer["kind"] == "image" and layer["src"] == server.url("slide.ome.zarr")
    assert layer["geometry"]["numChannels"] == 2


def test_the_import_proposal_says_when_a_host_is_unreachable(tmp_path, cache_root):
    from plexora.server.models import import_proposal
    from tests.remote_fixtures import closed_port_url

    proposal = import_proposal.inspect_paths([closed_port_url("x.zarr")]).to_dict()
    text = json.dumps(proposal)
    assert "reach" in text
