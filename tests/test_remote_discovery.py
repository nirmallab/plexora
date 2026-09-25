"""Finding the image in a store that cannot be listed.

A local store is walked with `iterdir`. Over HTTPS an S3 gateway answers every
directory with nothing, so discovery has to run on metadata instead -- plate
wells, well fields, the `OME` series list, the `labels` list, consolidated
metadata -- and must reach the same answer the local walk reaches. Each case
runs against a gateway (no listing) and a listing host, for both NGFF 0.4
(zarr v2) and 0.5 (zarr v3).
"""

import pytest
import zarr

from plexora.server.utils import ome_zarr, remote_store
from tests.ngff_fixtures import (write_bioformats2raw_like, write_ngff,
                                 write_plate_like, write_spatialdata_like)
from tests.remote_fixtures import cache_root, http_store  # noqa: F401

VERSIONS = ("0.4", "0.5")
MODES = ("gateway", "listing")


@pytest.fixture(params=[(v, m) for v in VERSIONS for m in MODES],
                ids=lambda p: f"{p[0]}-{p[1]}")
def host(request, tmp_path, http_store):
    version, mode = request.param
    return version, http_store(mode), tmp_path / "served"


def test_a_multiscale_image_is_itself(host):
    version, server, served = host
    write_ngff(served / "img.zarr", version=version, labels=["DNA", "CD3", "CD8"])
    url = server.url("img.zarr")
    assert ome_zarr.is_zarr_image_path(url)
    assert ome_zarr.resolve_image_path(url) == url
    pyramid = ome_zarr.open_image(url)
    assert len(pyramid) == 3 and pyramid[0].shape == (3, 256, 256)
    assert ome_zarr.channel_labels(url, 3) == ["DNA", "CD3", "CD8"]


def test_a_numbered_series_store(host):
    version, server, served = host
    write_bioformats2raw_like(served / "b2r.zarr", series=("0",), version=version)
    url = server.url("b2r.zarr")
    assert ome_zarr.resolve_image_path(url) == remote_store.url_join(url, "0")


def test_several_series_are_named_like_the_local_answer(host):
    version, server, served = host
    write_bioformats2raw_like(served / "b2r.zarr", series=("0", "1", "2"), version=version)
    with pytest.raises(ValueError) as remote:
        ome_zarr.resolve_image_path(server.url("b2r.zarr"))
    with pytest.raises(ValueError) as local:
        ome_zarr.resolve_image_path(served / "b2r.zarr")
    assert str(remote.value) == str(local.value)


def test_series_come_from_the_ome_group_when_there_is_one(tmp_path, http_store):
    served = tmp_path / "served"
    write_bioformats2raw_like(served / "b2r.zarr", series=("0", "1"), version="0.5")
    ome = zarr.open_group(str(served / "b2r.zarr" / "OME"), mode="w", zarr_format=3)
    ome.attrs.update({"series": ["1"]})
    server = http_store()
    url = server.url("b2r.zarr")
    assert ome_zarr.resolve_image_path(url) == remote_store.url_join(url, "1")
    # Named by the OME group, so nothing was guessed past it.
    assert server.count(suffix="/2/zarr.json") == 0


def test_a_plate_matches_the_local_answer(host):
    version, server, served = host
    write_plate_like(served / "plate.zarr", wells=("B/2", "B/3"), fields=("0", "1"),
                     version=version)
    url = server.url("plate.zarr")
    assert ome_zarr._plate_fields(url) == ome_zarr._plate_fields(served / "plate.zarr")
    with pytest.raises(ValueError, match="high-content screening plate: 2 wells, 4 images"):
        ome_zarr.resolve_image_path(url)
    assert ome_zarr.resolve_image_path(remote_store.url_join(url, "B/3/1")) == \
        remote_store.url_join(url, "B/3/1")


def test_a_large_plate_reads_a_sample_of_its_wells(tmp_path, http_store, monkeypatch):
    monkeypatch.setattr(ome_zarr, "_PLATE_WELL_SAMPLE", 2)
    served = tmp_path / "served"
    wells = ("A/1", "A/2", "B/1", "B/2")
    write_plate_like(served / "plate.zarr", wells=wells, fields=("0",), version="0.5")
    server = http_store()
    fields = ome_zarr._plate_fields(server.url("plate.zarr"))
    assert fields == [f"{w}/0" for w in wells]
    # The wells past the sample were synthesised from `field_count`, not read.
    assert server.count(suffix="B/2/zarr.json") == 0


def test_label_names(host):
    version, server, served = host
    path = write_ngff(served / "img.zarr", version=version)
    fmt = 2 if version == "0.4" else 3
    labels = zarr.open_group(str(path / "labels"), mode="w", zarr_format=fmt)
    if fmt == 2:
        labels.attrs.update({"labels": ["cells", "nuclei"]})
    else:
        labels.attrs.update({"ome": {"labels": ["cells", "nuclei"]}})
    for name in ("cells", "nuclei"):
        write_ngff(path / "labels" / name, shape=(1, 64, 64), levels=1, version=version)
    view = ome_zarr._RemoteView.of(server.url("img.zarr"))
    assert ome_zarr.label_names(view) == ["cells", "nuclei"]


def test_a_spatialdata_store_needs_a_listing_or_consolidated_metadata(tmp_path, http_store):
    served = tmp_path / "served"
    write_spatialdata_like(served / "sd.zarr", elements=("morphology",), version="0.5")
    listing = http_store("listing")
    url = listing.url("sd.zarr")
    assert ome_zarr.resolve_image_path(url) == remote_store.url_join(url, "images/morphology")

    remote_store._reset_for_tests(tmp_path / ".remote_cache2")
    gateway = http_store("gateway")
    with pytest.raises(ValueError, match="cannot\\s+list it"):
        ome_zarr.resolve_image_path(gateway.url("sd.zarr"))

    zarr.consolidate_metadata(str(served / "sd.zarr"))
    remote_store._reset_for_tests(tmp_path / ".remote_cache3")
    url = gateway.url("sd.zarr")
    assert ome_zarr.resolve_image_path(url) == remote_store.url_join(url, "images/morphology")


def test_nothing_there_says_so(tmp_path, http_store):
    server = http_store()
    with pytest.raises(ValueError, match="not an OME-Zarr store"):
        ome_zarr.resolve_image_path(server.url("absent.zarr"))


def test_image_candidates_for_a_plate(tmp_path, http_store):
    served = tmp_path / "served"
    write_plate_like(served / "plate.zarr", wells=("B/2",), fields=("0", "1"), version="0.5")
    server = http_store()
    url = server.url("plate.zarr")
    assert ome_zarr.image_candidates(url) == [
        ("B_2_0", remote_store.url_join(url, "B/2/0")),
        ("B_2_1", remote_store.url_join(url, "B/2/1"))]


def test_suggest_name_inside_a_remote_store():
    assert ome_zarr.suggest_name("https://h/screen.ome.zarr/B/2/0") == "screen_B_2_0"
    assert ome_zarr.suggest_name("https://h/sd.zarr/images/morphology") == "sd_morphology"
    assert ome_zarr.suggest_name("https://h/screen.zarr") is None
