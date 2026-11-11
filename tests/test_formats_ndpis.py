"""Hamamatsu NDPIS: a text file naming one pyramidal NDPI per channel, read from
synthetic files written to the description in
`plexora/server/utils/formats/ndpis.py`.
"""

import numpy as np
import pytest

import plexora
from plexora.server.models import import_proposal
from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable
from tests.formats_fixtures import in_colour, write_ndpi, write_ndpis

H, W = 200, 300


def _planes():
    rng = np.random.default_rng(3)
    dapi = rng.integers(200, 4000, (H, W)).astype(np.uint8)
    fitc = rng.integers(200, 4000, (H, W)).astype(np.uint8)
    return dapi, fitc


def _set(tmp_path, *, names=("DAPI 2 (387)", "FITC (494)"), dtype=np.uint8, resolution=None):
    rng = np.random.default_rng(3)
    blue = rng.integers(10, 250, (H, W)).astype(dtype)
    green = rng.integers(10, 250, (H, W)).astype(dtype)
    write_ndpi(tmp_path / "a.ndpi", in_colour(blue, 2), names[0], resolution=resolution)
    write_ndpi(tmp_path / "b.ndpi", in_colour(green, 1), names[1], resolution=resolution)
    return write_ndpis(tmp_path / "set.ndpis", ["a.ndpi", "b.ndpi"]), blue, green


def test_each_channel_is_the_brightest_sample_of_its_picture(tmp_path):
    path, blue, green = _set(tmp_path)
    reader = formats.open_reader(path)
    try:
        assert reader.channels == 2
        assert reader.levels[0] == (H, W)
        np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W), blue)
        np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W), green)
        # a region, and a level of the files' own pyramid
        np.testing.assert_array_equal(reader.read(0, 0, 20, 90, 31, 250), blue[20:90, 31:250])
        assert len(reader.levels) == 2
        assert reader.levels[1] == (H // 2, W // 2)
        np.testing.assert_array_equal(reader.read(1, 1, 0, H // 2, 0, W // 2),
                                      green[::2, ::2])
    finally:
        reader.close()


def test_the_maximum_is_taken_per_pixel_when_samples_share_the_signal(tmp_path):
    """A channel in a mixed colour has signal in more than one sample."""
    rng = np.random.default_rng(9)
    rgb = rng.integers(0, 256, (H, W, 3)).astype(np.uint8)
    for name, data in (("a.ndpi", rgb), ("b.ndpi", rgb[..., ::-1].copy())):
        write_ndpi(tmp_path / name, data, name[0].upper())
    path = write_ndpis(tmp_path / "mixed.ndpis", ["a.ndpi", "b.ndpi"])
    reader = formats.open_reader(path)
    try:
        np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W), rgb.max(axis=-1))
        np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W), rgb.max(axis=-1))
    finally:
        reader.close()


def test_a_greyscale_member_is_read_as_it_is(tmp_path):
    rng = np.random.default_rng(5)
    grey = rng.integers(0, 60000, (H, W)).astype(np.uint16)
    write_ndpi(tmp_path / "g.ndpi", grey, "Grey")
    write_ndpi(tmp_path / "h.ndpi", grey // 2, "Half")
    path = write_ndpis(tmp_path / "grey.ndpis", ["g.ndpi", "h.ndpi"])
    reader = formats.open_reader(path)
    try:
        assert reader.dtype == np.dtype(np.uint16)
        np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W), grey)
        np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W), grey // 2)
    finally:
        reader.close()


def test_channel_names_come_from_each_files_name_tag(tmp_path):
    path, _blue, _green = _set(tmp_path)
    assert formats.channel_names(path) == ["DAPI 2 (387)", "FITC (494)"]
    description = formats.describe(path)
    assert description.format_name == "Hamamatsu NDPIS"
    assert description.channels == 2 and not description.rgb
    assert (description.height, description.width) == (H, W)


def test_a_member_without_a_name_tag_is_named_by_its_file(tmp_path):
    rng = np.random.default_rng(1)
    for name in ("one.ndpi", "two.ndpi"):
        write_ndpi(tmp_path / name, in_colour(rng.integers(0, 255, (H, W)).astype(np.uint8), 0),
                   None)
    path = write_ndpis(tmp_path / "plain.ndpis", ["one.ndpi", "two.ndpi"])
    assert formats.channel_names(path) == ["one", "two"]


def test_the_pixel_size_comes_from_the_first_files_resolution(tmp_path):
    path, _b, _g = _set(tmp_path, resolution=0.46)
    assert formats.pixel_size(path) == (pytest.approx(0.46, rel=1e-3), pytest.approx(0.46, rel=1e-3))
    assert formats.physical_metadata(path)["physical_size_x"] == pytest.approx(0.46, rel=1e-3)


def test_crlf_and_lf_line_endings_and_the_stated_count(tmp_path):
    rng = np.random.default_rng(2)
    for name in ("a.ndpi", "b.ndpi", "c.ndpi"):
        write_ndpi(tmp_path / name, in_colour(rng.integers(0, 255, (H, W)).astype(np.uint8), 0),
                   name[0].upper())
    lf = write_ndpis(tmp_path / "lf.ndpis", ["a.ndpi", "b.ndpi"], newline="\n")
    assert formats.channel_names(lf) == ["A", "B"]
    # NoImages=2 names two of three listed files: the count wins
    counted = write_ndpis(tmp_path / "counted.ndpis", ["a.ndpi", "b.ndpi", "c.ndpi"], count=2)
    assert formats.channel_names(counted) == ["A", "B"]
    three = write_ndpis(tmp_path / "three.ndpis", ["a.ndpi", "b.ndpi", "c.ndpi"])
    assert formats.channel_names(three) == ["A", "B", "C"]


def test_a_missing_member_is_refused_naming_it(tmp_path):
    path, _b, _g = _set(tmp_path)
    (tmp_path / "b.ndpi").unlink()
    with pytest.raises(FormatUnreadable, match=r"b\.ndpi"):
        formats.open_reader(path)


def test_a_set_that_names_nothing_is_refused(tmp_path):
    path = write_ndpis(tmp_path / "empty.ndpis", [])
    with pytest.raises(FormatUnreadable):
        formats.open_reader(path)


def test_members_of_different_sizes_are_not_one_scan(tmp_path):
    rng = np.random.default_rng(4)
    write_ndpi(tmp_path / "a.ndpi", in_colour(rng.integers(0, 255, (H, W)).astype(np.uint8), 0), "A")
    write_ndpi(tmp_path / "b.ndpi",
               in_colour(rng.integers(0, 255, (H + 40, W)).astype(np.uint8), 1), "B")
    path = write_ndpis(tmp_path / "bad.ndpis", ["a.ndpi", "b.ndpi"])
    with pytest.raises(FormatUnreadable, match="same size"):
        formats.open_reader(path)


def test_the_pyramid_serves_the_channels(tmp_path):
    path, blue, green = _set(tmp_path)
    pyramid = formats.open_image(path)
    try:
        assert pyramid.channel_names == ["DAPI 2 (387)", "FITC (494)"]
        assert pyramid.level_shapes == [[H, W]]
        np.testing.assert_array_equal(pyramid["0"][0], blue)
        np.testing.assert_array_equal(pyramid["0"][1], green)
    finally:
        pyramid.close()


# -- a folder import -------------------------------------------------------


def test_the_set_members_are_named_by_set_members(tmp_path):
    _set(tmp_path)
    assert import_proposal._set_members(tmp_path) == {"a.ndpi", "b.ndpi"}
    other = tmp_path / "other"
    other.mkdir()
    assert import_proposal._set_members(other) == set()


def test_a_folder_import_offers_the_set_and_not_its_member_files(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    folder = tmp_path / "scan"
    folder.mkdir()
    _set(folder)
    client = plexora.app.test_client()
    proposal = client.post("/import/inspect", json={"paths": [str(folder)]}).get_json()

    files = []
    for sample in proposal["samples"]:
        for layer in sample["layers"]:
            files.append(str(layer.get("path") or layer.get("name") or layer))
    assert any("set.ndpis" in entry for entry in files), files
    assert not any(entry.endswith(("a.ndpi", "b.ndpi")) for entry in files), files
