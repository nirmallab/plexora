"""A DICOM slide at a web address, assembled, registered and served.

The fixtures `test_dicom_registration.py` registers from disk, behind a real
HTTP server. A listing host stands in for s3:// and gs:// (whose folders can
be listed); a gateway host is the https:// case where they cannot.
"""

import numpy as np
import pytest

pytest.importorskip("wsidicom")
pytest.importorskip("pydicom")

from plexora import datasource  # noqa: E402
from plexora.server.models import data_model, remote_sources  # noqa: E402
from plexora.server.providers.remote import RemoteImageProvider  # noqa: E402
from plexora.server.utils import dicom_wsi, remote_image, remote_store  # noqa: E402
from tests.dicom_fixtures import (write_he_slide, write_if_slide,  # noqa: E402
                                  write_two_slides)
from tests.remote_fixtures import (cache_root, closed_port_url,  # noqa: E402,F401
                                   http_store)


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "served"
    root.mkdir(exist_ok=True)
    return root


def test_names_answer_without_a_request(cache_root):
    assert dicom_wsi.is_dicom_path("https://nowhere.invalid/slide/x.dcm")
    assert remote_image.kind_of("https://nowhere.invalid/slide/x.dcm") == remote_image.DICOM


def test_a_folder_is_found_by_listing_it(served, http_store):
    write_if_slide(served / "panel", levels=2, nested=True)
    server = http_store("listing")
    url = server.url("panel")
    assert dicom_wsi.is_dicom_path(url)
    assert remote_image.kind_of(url) == remote_image.DICOM
    source = dicom_wsi.assemble_slide(url)
    assert source.kind == "remote" and len(source.files) == 6
    assert dicom_wsi.channel_names(url) == ["DNA", "CD3", "Ki67"]
    assert dicom_wsi.detect_image_type(url).verdict == "fluorescence"


def test_a_gateway_folder_is_not_dicom_and_does_not_raise(served, http_store):
    write_if_slide(served / "panel")
    server = http_store()
    assert dicom_wsi.is_dicom_path(server.url("panel")) is False


def test_a_closed_port_folder_is_not_dicom_and_is_quick(cache_root):
    import time

    started = time.monotonic()
    assert dicom_wsi.is_dicom_path(closed_port_url("folder")) is False
    assert time.monotonic() - started < 10


def test_a_folder_of_two_slides_is_refused(served, http_store):
    write_two_slides(served / "two")
    server = http_store("listing")
    with pytest.raises(ValueError, match="2 slides"):
        dicom_wsi.assemble_slide(server.url("two"))


def test_register_and_serve_the_local_pixels(served, http_store):
    write_if_slide(served / "panel", levels=2, nested=True, height=700, width=900)
    server = http_store("listing")
    url = server.url("panel")

    entry = datasource.register_image_datasource(name="webdicom", image=url)

    assert entry["channelFile"] == url
    assert entry["image_kind"] == "dicom"
    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3", "Ki67"]
    data_model.load_datasource("webdicom", reload=True)
    assert isinstance(data_model._providers.image, RemoteImageProvider)

    local = dicom_wsi.open_image(served / "panel")
    remote = data_model.channels
    try:
        for level in range(len(local)):
            for channel in range(3):
                assert np.array_equal(remote[level][channel, 0:300, 0:400],
                                      local[level][channel, 0:300, 0:400])
    finally:
        local.close()
    key = entry["imageData"][1]["src"].rstrip("/").rsplit("/", 1)[-1]
    encoded, mimetype = data_model.encode_tile("webdicom", key, 0, "0_0.png", "default")
    assert encoded and mimetype == "image/webp"
    assert remote_sources.cached_bytes(url) > 0


def test_a_reopen_reads_only_the_listing(served, http_store, cache_root):
    write_if_slide(served / "panel", levels=2)
    server = http_store("listing")
    url = server.url("panel")
    first = dicom_wsi.open_image(url)
    block = first[0][1, 0:200, 0:200]
    first.close()
    remote_store.cache_index().flush()
    remote_store._reset_for_tests(cache_root)
    server.clear()

    again = dicom_wsi.open_image(url)
    assert np.array_equal(again[0][1, 0:200, 0:200], block)
    again.close()
    assert all(not path.endswith(".dcm") for _, path, _ in server.requests)


def test_a_reopen_works_with_the_host_gone(served, http_store, cache_root):
    write_if_slide(served / "panel")
    server = http_store("listing")
    url = server.url("panel")
    first = dicom_wsi.open_image(url)
    block = first[0][0, 0:100, 0:100]
    first.close()
    remote_store.cache_index().flush()
    remote_store._reset_for_tests(cache_root)
    with server.outage():
        again = dicom_wsi.open_image(url)
        assert np.array_equal(again[0][0, 0:100, 0:100], block)
        again.close()


def test_an_he_slide_is_brightfield(served, http_store):
    write_he_slide(served / "he")
    server = http_store("listing")
    entry = datasource.register_image_datasource(name="webhe", image=server.url("he"))
    assert entry["image_kind"] == "brightfield"
    data_model.load_datasource("webhe", reload=True)
    assert data_model.channels.is_color


def test_one_instance_on_a_gateway_opens_alone(served, http_store):
    write_if_slide(served / "panel")
    server = http_store()
    source = dicom_wsi.assemble_slide(server.url("panel", "level0_path1.dcm"))
    assert len(source.files) == 1


def test_one_instance_on_a_listing_host_gathers_its_siblings(served, http_store):
    write_if_slide(served / "panel")
    server = http_store("listing")
    source = dicom_wsi.assemble_slide(server.url("panel", "level0_path1.dcm"))
    assert len(source.files) == 3


def test_a_folder_probe_has_a_listing_identity(served, http_store):
    write_if_slide(served / "panel")
    server = http_store("listing")
    result = remote_store.probe(server.url("panel"), fresh=True)
    assert result.status == "ok" and result.etag.startswith("listing:")


def test_warming_fetches_every_instance_head(served, http_store):
    write_if_slide(served / "panel")
    server = http_store("listing")
    url = server.url("panel")
    store, ranges = remote_sources.warm_plan(url)
    assert len(ranges) == 2 * 3
    assert store.fetch_ranges(ranges) > 0


def test_pinning_covers_every_instance(served, http_store, cache_root):
    write_if_slide(served / "panel")
    server = http_store("listing")
    url = server.url("panel")
    store, ranges, estimate = remote_sources.pin_plan(url)
    sizes = {p.name: p.stat().st_size for p in (served / "panel").glob("*.dcm")}
    assert estimate == sum(sizes.values())
    store.fetch_ranges(ranges)
    remote_store.cache_index().flush()
    remote_store._reset_for_tests(cache_root)
    server.clear()
    with server.outage():
        pyramid = dicom_wsi.open_image(url)
        assert pyramid[0][2, 0:256, 0:320].shape == (256, 320)
        pyramid.close()


def test_the_remote_install_line_names_the_remote_extra():
    assert "plexora[remote]" in dicom_wsi.DicomSupportMissingRemote.INSTALL
    assert issubclass(dicom_wsi.DicomSupportMissingRemote, dicom_wsi.DicomSupportMissing)


def test_the_wsidicom_constructors_this_depends_on():
    """If wsidicom moves these, this fails first and loudly."""
    import inspect

    from wsidicom import WsiDicom
    from wsidicom.file import WsiDicomFileSource
    from wsidicom.file.io import WsiDicomIO

    assert list(inspect.signature(WsiDicomIO).parameters)[:2] == ["stream", "filepath"]
    assert "streams" in inspect.signature(WsiDicomFileSource).parameters
    assert list(inspect.signature(WsiDicom).parameters)[:2] == ["source", "source_owned"]


def test_the_import_proposal_for_a_dicom_folder(served, http_store):
    from plexora.server.models import import_proposal

    write_if_slide(served / "panel", nested=True)
    server = http_store("listing")
    proposal = import_proposal.inspect_paths([server.url("panel")]).to_dict()
    [sample] = proposal["samples"]
    [layer] = sample["layers"]
    assert layer["kind"] == "image" and layer["src"] == server.url("panel")
    assert layer["geometry"]["numChannels"] == 3
