"""Opening an image reads it once (docs/internal/bench/web/WEBSITE_FIRST_VIEW_2026-11-10.md).

The home page inspects a pick, then imports it: the import used to inspect
the same files all over again, and both opened the TIFF again and again (the
label check, the geometry, the pixel size, the focal planes, the channel
names, the conversion) -- on a 106 GB slide from a file share, ten minutes on
the website. Now:

- the import takes the proposal the inspect a moment before made of the same
  unchanged files (`import_proposal.inspected`), once, and never after a
  sample is registered;
- a file that says it is three channels or more of one plane is a panel,
  answered without reading its pixels (`looks_like_label_image`);
- on the website one open of a file is shared by every reader of it
  (`tiff_region.open_tiff`), and `close_tiff` leaves it open;
- the local app's home page has a preview of a path at once (`/import/preview`).
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest
import tifffile

from plexora import _work
from plexora.server.models import import_proposal, import_sample
from plexora.server.utils import tiff_region, tiff_series
from tests.helpers import use_data_root
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def _panel(path, channels=4, size=96, names=None):
    """An OME-TIFF of `channels` channels with a two-level SubIFD pyramid."""
    rng = np.random.default_rng(channels)
    stack = rng.integers(0, 4000, size=(channels, size, size)).astype(np.uint16)
    metadata = {"axes": "CYX"}
    if names:
        metadata["Channel"] = {"Name": list(names)}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as tif:
        tif.write(stack, subifds=1, tile=(32, 32), metadata=metadata)
        tif.write(stack[:, ::2, ::2].copy(), subfiletype=1, tile=(32, 32))
    return path


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    use_data_root(monkeypatch, root)
    import_proposal.forget_inspected()
    yield tmp_path
    import_proposal.forget_inspected()


def _counting(monkeypatch):
    calls = []
    real = import_proposal._inspect_paths

    def counted(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(import_proposal, "_inspect_paths", counted)
    return calls


def test_the_import_takes_the_inspect_it_follows(data, monkeypatch):
    image = _panel(data / "slide.ome.tif")
    calls = _counting(monkeypatch)
    first = import_proposal.inspect_paths([str(image)])
    assert first.samples
    result = import_sample.import_sample([str(image)])
    assert result["name"]
    # One read of the files for the two requests.
    assert len(calls) == 1
    # Registered: the memo is gone, and the next look says the data exists.
    again = import_proposal.inspect_paths([str(image)])
    assert len(calls) == 2
    assert again.samples[0].existing == result["name"]


def test_a_file_changed_since_is_inspected_again(data, monkeypatch):
    image = _panel(data / "slide.ome.tif")
    calls = _counting(monkeypatch)
    import_proposal.inspect_paths([str(image)])
    later = time.time() + 5
    os.utime(image, (later, later))
    assert import_proposal.inspected([str(image)]) is None
    import_sample.import_sample([str(image)])
    assert len(calls) == 2


def test_the_memo_is_taken_once_and_only_for_the_same_question(data):
    image = _panel(data / "slide.ome.tif")
    import_proposal.inspect_paths([str(image)])
    assert import_proposal.inspected([str(image)], answers={"x": "y"}) is None
    assert import_proposal.inspected([str(image)]) is not None
    assert import_proposal.inspected([str(image)]) is None


def test_a_folder_is_never_remembered(data):
    folder = data / "run"
    folder.mkdir()
    _panel(folder / "slide.ome.tif")
    import_proposal.inspect_paths([str(folder)])
    assert import_proposal.inspected([str(folder)]) is None


def test_a_panel_is_an_image_without_a_pixel_read(data, monkeypatch):
    image = _panel(data / "panel.ome.tif", channels=5)
    monkeypatch.setattr(import_proposal, "_volume_looks_like_labels",
                        lambda *a, **k: pytest.fail("read the pixels of a stated panel"))
    assert import_proposal.looks_like_label_image(image) is False


def test_two_planes_are_still_read(data, monkeypatch):
    """A nuclei-and-cells pair can be a mask: its pixels decide, as before."""
    image = _panel(data / "pair.ome.tif", channels=2)
    seen = []
    monkeypatch.setattr(import_proposal, "_volume_looks_like_labels",
                        lambda *a, **k: seen.append(1) or False)
    import_proposal.looks_like_label_image(image)
    assert seen == [1]


def test_the_website_shares_one_open_and_the_desktop_does_not(data, monkeypatch):
    image = _panel(data / "slide.ome.tif")
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    _work._reset_for_tests()
    try:
        first = tiff_region.open_tiff(image)
        tiff_region.close_tiff(first)
        assert not first.filehandle.closed
        second = tiff_region.open_tiff(image)
        assert second is first and tiff_series.is_shared(first)
        with tiff_region.opened(image) as third:
            assert third is first
        assert not first.filehandle.closed
    finally:
        tiff_series._SHARED.clear()
        monkeypatch.setenv("PLEXORA_WORK", "threads")
        _work._reset_for_tests()
    own = tiff_region.open_tiff(image)
    other = tiff_region.open_tiff(image)
    assert own is not other and not tiff_series.is_shared(own)
    tiff_region.close_tiff(own)
    tiff_region.close_tiff(other)
    assert own.filehandle.closed and other.filehandle.closed


def test_the_preview_route_draws_the_first_channel_at_its_smallest_level(data):
    from plexora import app

    image = _panel(data / "slide.ome.tif")
    client = app.test_client()
    found = client.get("/import/preview", query_string={"path": str(image)})
    assert found.status_code == 200 and found.mimetype == "image/png"
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(found.data)) as picture:
        assert picture.size == (48, 48)
    assert client.get("/import/preview", query_string={"path": str(data / "missing.tif")}).status_code == 204
    (data / "notes.txt").write_text("not an image", encoding="utf-8")
    assert client.get("/import/preview", query_string={"path": str(data / "notes.txt")}).status_code == 204


def test_the_preview_names_the_channels_and_the_size_for_the_first_views_list(data):
    """The local first view's channel list: the OME-XML's names and SizeC in
    `X-Plexora-Channels`, the file's size in `X-Plexora-Bytes`."""
    import json
    from urllib.parse import unquote

    from plexora import app

    image = _panel(data / "named.ome.tif", channels=3, names=["DNA", "CD45 & co", "Ki-67"])
    found = app.test_client().get("/import/preview", query_string={"path": str(image)})
    assert found.status_code == 200
    channels = json.loads(unquote(found.headers["X-Plexora-Channels"]))
    assert channels == {"count": 3, "names": ["DNA", "CD45 & co", "Ki-67"]}
    assert int(found.headers["X-Plexora-Bytes"]) == os.path.getsize(image)

    # No names in the file: the count alone, from the XML.
    plain = _panel(data / "plain.ome.tif", channels=2)
    channels = json.loads(unquote(app.test_client().get(
        "/import/preview", query_string={"path": str(plain)}).headers["X-Plexora-Channels"]))
    assert channels["count"] == 2
    assert all(not name or name.startswith("Channel") for name in channels["names"])


def _preview_of(path):
    import json
    from io import BytesIO
    from urllib.parse import unquote

    from PIL import Image

    from plexora import app

    found = app.test_client().get("/import/preview", query_string={"path": str(path)})
    if found.status_code != 200:
        return found.status_code, None, None, found.headers
    with Image.open(BytesIO(found.data)) as picture:
        pixels = np.asarray(picture.convert("RGB") if picture.mode not in ("L", "RGB") else picture)
    return 200, pixels, json.loads(unquote(found.headers["X-Plexora-Channels"])), found.headers


def test_the_preview_reads_a_slide_in_its_own_colours_and_its_thumbnail_when_it_stops_large(data, monkeypatch):
    """The TIFF-based slides (an Aperio-like SVS here, JPEG tiles in RGB):
    the smallest level in colour, one RGB channel and no names; a slide whose
    pyramid stops above the limit shows its thumbnail."""
    from plexora.server.routes import import_routes
    from tests.slide_fixtures import write_svs_like

    path = data / "slide.svs"
    pictures = write_svs_like(path, jpeg=True)
    status, pixels, channels, headers = _preview_of(path)
    assert status == 200
    assert pixels.shape == pictures["baseline"].shape
    assert np.abs(pixels.astype(int) - pictures["baseline"].astype(int)).mean() < 4
    assert channels == {"count": 1, "names": [], "rgb": True}
    assert int(headers["X-Plexora-Bytes"]) == os.path.getsize(path)

    monkeypatch.setattr(import_routes, "_PREVIEW_MAX_SIDE", 400)
    status, pixels, _, _ = _preview_of(path)
    assert status == 200 and pixels.shape == pictures["thumbnail"].shape


def test_the_preview_names_a_qptiffs_biomarkers(data):
    from tests.slide_fixtures import write_qptiff_like

    path = data / "panel.qptiff"
    write_qptiff_like(path, names=("DAPI", "CD8", "PanCK"))
    status, pixels, channels, _ = _preview_of(path)
    assert status == 200 and pixels.ndim == 2
    assert channels == {"count": 3, "names": ["DAPI", "CD8", "PanCK"]}


def test_the_preview_reads_an_ome_zarr_folder(data):
    """An OME-Zarr folder: channel 0 of its smallest level at least 512 a side
    (else its largest, as firstView.js chooses), the omero labels
    as its names, no size (a folder's is not one number worth walking it for)."""
    from tests.ngff_fixtures import write_ngff

    store = write_ngff(data / "image.ome.zarr", shape=(3, 256, 256), levels=3, labels=["DAPI", "CD3", "CD20"])
    status, pixels, channels, headers = _preview_of(store)
    assert status == 200 and pixels.shape == (256, 256)
    assert channels == {"count": 3, "names": ["DAPI", "CD3", "CD20"]}
    assert "X-Plexora-Bytes" not in headers
    (data / "plain").mkdir()
    assert _preview_of(data / "plain")[0] == 204

    deep = write_ngff(data / "deep.ome.zarr", shape=(1, 2048, 2048), levels=6)
    status, pixels, _, _ = _preview_of(deep)
    assert status == 200 and pixels.shape == (512, 512)


@pytest.mark.skipif(sys.platform == "win32", reason="a 4 GB file is sparse here only where the file system makes it so")
def test_the_preview_reads_an_ndpi_past_4_gb(data):
    from tests.slide_fixtures import write_ndpi_like

    path = data / "slide.ndpi"
    pictures = write_ndpi_like(path, beyond=2**32 + 999, jpeg=True)
    status, pixels, channels, _ = _preview_of(path)
    assert status == 200 and pixels.shape == pictures["level1"].shape
    assert channels["rgb"] is True


def test_the_preview_reads_a_store_whatever_its_name_and_layout(data):
    """A folder whose name says nothing, and bioformats2raw's layout (the
    image at "0"): resolved as the import resolves them."""
    from tests.ngff_fixtures import write_bioformats2raw_like, write_ngff

    plain = write_ngff(data / "sample_7", shape=(2, 128, 128), levels=2, labels=["DAPI", "CD8"])
    status, pixels, channels, _ = _preview_of(plain)
    assert status == 200 and pixels.shape == (128, 128)
    assert channels["names"] == ["DAPI", "CD8"]
    raw = write_bioformats2raw_like(data / "export", shape=(3, 96, 96), levels=2, axes="cyx",
                                    labels=["a", "b", "c"])
    status, pixels, channels, _ = _preview_of(raw)
    assert status == 200 and pixels.shape == (96, 96) and channels["names"] == ["a", "b", "c"]


def test_the_preview_reads_a_store_at_a_web_address(data, http_store):
    """A store on a web host, as the import reads it (remote_store), named
    `.zarr` or not; a web address that is not a store has none."""
    from tests.ngff_fixtures import write_ngff

    server = http_store()
    write_ngff(server.root / "remote.ome.zarr", shape=(2, 256, 256), levels=3, labels=["DAPI", "CD3"])
    write_ngff(server.root / "unnamed", shape=(1, 64, 64), levels=1)
    status, pixels, channels, headers = _preview_of(server.url("remote.ome.zarr"))
    assert status == 200 and pixels.shape == (256, 256)
    assert channels == {"count": 2, "names": ["DAPI", "CD3"]}
    assert "X-Plexora-Bytes" not in headers
    assert _preview_of(server.url("unnamed"))[0] == 200
    assert _preview_of(server.url("nothing-here"))[0] == 204


def test_the_preview_of_a_volume_is_its_middle_plane(data):
    """A z-stack's preview is the plane the viewer opens on (volume/model.py,
    depth // 2), not the first, which in a real stack is often empty."""
    import zarr

    from tests.ngff_fixtures import write_ngff

    store = write_ngff(data / "volume.ome.zarr", shape=(2, 9, 64, 64), axes="czyx", levels=1)
    array = zarr.open_group(str(store))["0"]
    signal = np.random.default_rng(3).integers(100, 4000, size=(64, 64)).astype("uint16")
    array[0] = 0
    array[0, 4] = signal
    status, pixels, _, _ = _preview_of(store)
    assert status == 200 and pixels.max() > 0
