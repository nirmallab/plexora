"""An Olympus ETS opened on its own (`plexora/server/utils/formats/ets.py`):
one scan of a VSI, sent without the .vsi -- an H&E scan's `frame_t.ets`.

The files are the synthetic VSI slides of `tests/formats_fixtures.py`, with
the ETS read where the scanner put it (beside its .vsi) or copied out of its
folder. `vsi_value` gives every (level, channel, z, y, x) its own number, so a
read from the wrong tile, level or channel cannot pass; a tile's padding past
the scan is 9999 % 251 in the fixtures.
"""

import io
import shutil

import numpy as np
import pytest
from PIL import Image

import plexora
from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable, ets
from tests.formats_fixtures import VsiStack, vsi_expected, vsi_value, write_vsi

PADDING = 9999 % 251


def _ets_of(vsi, stack):
    return vsi.parent / f"_{vsi.stem}_" / f"stack{stack.ident}" / "frame_t.ets"


def _alone(vsi, stack, where, name="frame_t.ets"):
    """The ETS of `stack`, copied out of its folder to `where/name`."""
    where.mkdir(parents=True, exist_ok=True)
    return shutil.copyfile(_ets_of(vsi, stack), where / name)


# -- the reader ---------------------------------------------------------------


def test_an_ets_alone_is_the_whole_tile_grid_with_numbered_channels(tmp_path):
    stack = VsiStack(2, height=300, width=401, tile=128, dims=[(4, 2)],
                     channel_names=("DAPI", "Cy5"), pixel_size=(0.5, 0.5))
    vsi = write_vsi(tmp_path / "scanner", stem="scan", stacks=[stack])
    path = _alone(vsi, stack, tmp_path / "sent", "scan.ets")

    described = formats.describe(path)
    assert described.format_name == "Olympus ETS"
    # Nothing but the .vsi says how much of the last tiles is the scan.
    assert (described.height, described.width) == (384, 512)
    assert described.level_shapes == [[384, 512], [192, 256]]
    assert described.channel_names == ["Channel 1", "Channel 2"]
    assert described.pixel_size is None and formats.physical_metadata(path) == {}
    assert not described.rgb and described.images == ["scan"]
    assert formats.detect_image_type(path).verdict == "fluorescence"

    reader = formats.open_reader(path)
    try:
        for level in (0, 1):
            h, w = -(-300 // (1 << level)), -(-401 // (1 << level))
            for c in (0, 1):
                got = reader.read(level, c, 0, h, 0, w)
                np.testing.assert_array_equal(got, vsi_expected(stack, level, c, 0, 0, h, 0, w))
        edge = reader.read(0, 0, 300, 384, 401, 512)
        assert (edge == PADDING).all()
    finally:
        reader.close()


def test_an_ets_beside_its_vsi_takes_the_vsi_s_names_size_and_scale(tmp_path):
    stack = VsiStack(3, name="40x_BF_01", height=300, width=401, tile=128, dims=[(4, 2)],
                     channel_names=("DAPI", "Cy5"), pixel_size=(0.25, 0.25))
    vsi = write_vsi(tmp_path, stem="kidney", stacks=[stack])
    path = _ets_of(vsi, stack)

    described = formats.describe(path)
    assert (described.height, described.width) == (300, 401)
    assert described.channel_names == ["DAPI", "Cy5"]
    assert described.pixel_size == (0.25, 0.25)
    assert described.images == ["40x_BF_01"]
    # The same pixels the .vsi reads.
    alone, whole = formats.open_reader(path), formats.open_reader(vsi)
    try:
        for c in (0, 1):
            np.testing.assert_array_equal(alone.read(0, c, 0, 300, 0, 401),
                                          whole.read(0, c, 0, 300, 0, 401))
    finally:
        alone.close()
        whole.close()


def test_an_rgb_ets_alone_is_a_brightfield_picture(tmp_path):
    # As cellSens writes a colour scan: one dimension of size 1 beside its three samples.
    stack = VsiStack(2, height=200, width=300, tile=128, dims=[(4, 1)], pixel_type=2,
                     samples=3, background=0xEEEEEE, missing={(0, 1, 2)})
    vsi = write_vsi(tmp_path / "scanner", stem="he", stacks=[stack])
    path = _alone(vsi, stack, tmp_path / "sent")

    described = formats.describe(path)
    assert described.rgb and described.channels == 3
    assert described.channel_names == ["Red", "Green", "Blue"]
    detected = formats.detect_image_type(path)
    assert detected.verdict == "brightfield" and detected.confidence == "high"
    reader = formats.open_reader(path)
    try:
        got = reader.read_rgb(0, 0, 128, 0, 256)
        ys, xs = np.arange(128), np.arange(256)
        np.testing.assert_array_equal(got, np.stack([vsi_value(0, 0, 0, ys, xs, s)
                                                     for s in range(3)], -1))
        # A tile cellSens did not write is the scan's background colour.
        assert (reader.read_rgb(0, 128, 256, 256, 384) == 0xEE).all()
    finally:
        reader.close()


def test_a_vsi_colour_scan_with_a_channel_dimension_of_one_is_rgb(tmp_path):
    stack = VsiStack(2, height=200, width=300, tile=128, dims=[(4, 1)], pixel_type=2,
                     samples=3, background=0xFFFFFF)
    reader = formats.open_reader(write_vsi(tmp_path, stem="he", stacks=[stack]))
    try:
        assert reader.rgb and reader.channels == 3
        ys, xs = np.arange(200), np.arange(300)
        for sample in range(3):
            np.testing.assert_array_equal(reader.read(0, sample, 0, 200, 0, 300),
                                          vsi_value(0, 0, 0, ys, xs, sample).astype(np.uint8))
    finally:
        reader.close()


def test_a_cellsens_data_file_is_refused_as_not_an_image(tmp_path):
    stack = VsiStack(2, height=200, width=300, tile=128)
    vsi = write_vsi(tmp_path, stem="scan", stacks=[stack])
    data = bytearray(_ets_of(vsi, stack).read_bytes())
    # 65536 x 1 "tiles", as cellSens's blob_*_Frame#0.ets has.
    data[64 + 28:64 + 36] = (65536).to_bytes(4, "little") + (1).to_bytes(4, "little")
    blob = tmp_path / "blob_21_f_Frame#0.ets"
    blob.write_bytes(bytes(data))

    with pytest.raises(FormatUnreadable, match="not an image"):
        formats.describe(blob)


def test_a_file_that_is_not_an_ets_is_refused(tmp_path):
    path = tmp_path / "notes.ets"
    path.write_bytes(b"not a scan" * 20)
    assert formats.handles(path)
    with pytest.raises(FormatUnreadable, match="not an ETS"):
        formats.describe(path)


@pytest.mark.parametrize("path, name", [
    ("/data/_kidney_/stack1/frame_t.ets", "kidney stack1"),
    ("/data/_kidney_/stack10004/frame_t_0.ets", "kidney stack10004"),
    ("/data/sent/stack3/frame_t.ets", "stack3"),
    ("/picked/0a1b2c3d4e5f/frame_t.ets", "frame_t"),
    ("/data/_kidney_/stack1/patient 12.ets", "patient 12"),
    ("C:\\scans\\_liver_\\stack1\\frame_t.ets", "liver stack1"),
])
def test_an_ets_is_named_by_its_folders_when_cellsens_named_it(path, name):
    assert ets.name_of(path) == name


# -- the import and the app ---------------------------------------------------


def test_two_scans_sent_as_frame_t_ets_are_two_samples_named_by_slide(tmp_path):
    from plexora.server.models.import_proposal import inspect_paths

    stack = VsiStack(1, height=200, width=300, tile=128, dims=[], pixel_type=2, samples=3)
    paths = []
    for slide in ("kidney", "liver"):
        vsi = write_vsi(tmp_path / "sent", stem=slide, stacks=[stack])
        vsi.unlink()       # sent without it
        paths.append(_ets_of(vsi, stack))

    proposal = inspect_paths([str(p) for p in paths])
    assert sorted(sample.name for sample in proposal.samples) == ["kidney stack1", "liver stack1"]


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


def test_an_h_and_e_ets_alone_imports_as_a_brightfield_project(tmp_path, client):
    stack = VsiStack(2, height=200, width=300, tile=128, dims=[(4, 1)], pixel_type=2,
                     samples=3, background=0xFFFFFF)
    vsi = write_vsi(tmp_path / "scanner", stem="he", stacks=[stack])
    path = _alone(vsi, stack, tmp_path / "src", "he.ets")

    inspected = client.post("/import/inspect", json={"paths": [str(path)]})
    assert inspected.status_code == 200, inspected.data[:400]
    assert inspected.get_json()["samples"]
    response = client.post("/import/sample", json={"paths": [str(path)], "name": "he"})
    assert response.status_code == 200, response.data[:400]
    assert client.get("/init_database?datasource=he").status_code == 200
    entry = client.get("/config").get_json()["he"]

    assert entry["image_kind"] == "brightfield"
    assert entry["imageTypeDetected"] == "brightfield"
    assert (entry["height"], entry["width"]) == (256, 384)
    tile = client.get(f"{entry['imageData'][0]['src']}0/0_0.png?q=hd")
    assert tile.status_code == 200, tile.data[:300]
    pixels = np.asarray(Image.open(io.BytesIO(tile.data)))[..., :3]
    assert pixels.shape[:2] == (256, 384)
    ys, xs = np.arange(200), np.arange(300)
    want = np.stack([vsi_value(0, 0, 0, ys, xs, s) for s in range(3)], -1)
    np.testing.assert_array_equal(pixels[:200, :300], want)
