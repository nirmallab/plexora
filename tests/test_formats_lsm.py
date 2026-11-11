"""Zeiss LSM, read from synthetic files written to the layout in the docstring
of `plexora/server/utils/formats/lsm.py` and the CZ_LSMINFO structure as
tifffile documents it (`formats_fixtures_more.write_lsm`, a hand-laid TIFF
checked against tifffile, not against this reader).

Every (position, t, z, channel) plane carries its own identity
(`lsm_plane`), and every data page is followed by a thumbnail page of 0xEE,
so a plane taken from the wrong page, a thumbnail, or the wrong strip cannot
pass.

Not covered here: files over 4 GB (the strip-offset unwrapping is
tifffile's, and a test file would have to be that big), and the time
interval, which `base.Reader` does not carry.
"""

import random
import threading

import numpy as np
import pytest
import tifffile

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable, lsm
from tests.formats_fixtures_more import LsmTrack, lsm_plane, lsm_strip_offset, write_lsm

H, W = 24, 40


def _open(path, index=0):
    return formats.open_reader(path, index)


# -- planes in place -------------------------------------------------------------


@pytest.mark.parametrize("compression", [1, 5], ids=["raw", "lzw"])
@pytest.mark.parametrize("dtype,channels", [("<u2", 2), ("u1", 3), ("<u2", 1)])
def test_every_plane_is_read_from_its_page_and_strip(tmp_path, dtype, channels, compression):
    path = write_lsm(tmp_path / "s.lsm", height=H, width=W, channels=channels, z=3, t=2,
                     dtype=dtype, compression=compression)
    with tifffile.TiffFile(str(path)) as tif:       # the fixture is an LSM to tifffile
        assert tif.is_lsm
    reader = _open(path)
    try:
        assert reader.levels == [(H, W)]
        assert (reader.channels, reader.z_count, reader.t_count) == (channels, 3, 2)
        assert reader.dtype == np.dtype(dtype)
        assert not reader.rgb
        for t in range(2):
            for z in range(3):
                for c in range(channels):
                    got = reader.read(0, c, 0, H, 0, W, z=z, t=t)
                    assert got.shape == (H, W) and got.dtype == np.dtype(dtype)
                    np.testing.assert_array_equal(got, lsm_plane(0, t, z, c, H, W, dtype),
                                                  err_msg=f"t={t} z={z} c={c}")
    finally:
        reader.close()


@pytest.mark.parametrize("scan_type,z,t", [(0, 4, 1), (3, 1, 3), (6, 2, 3)],
                         ids=["stack", "time-series", "time-series-stack"])
def test_z_and_time_counts_follow_the_scan_type(tmp_path, scan_type, z, t):
    path = write_lsm(tmp_path / "s.lsm", channels=2, z=z, t=t, scan_type=scan_type)
    reader = _open(path)
    try:
        assert (reader.z_count, reader.t_count) == (z, t)
        for tt in range(t):
            for zz in range(z):
                np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W, z=zz, t=tt),
                                              lsm_plane(0, tt, zz, 1, H, W))
    finally:
        reader.close()


def test_any_region_equals_the_slice_of_the_plane(tmp_path):
    height, width = 301, 257
    path = write_lsm(tmp_path / "r.lsm", height=height, width=width, channels=2, z=2)
    full = lsm_plane(0, 0, 1, 1, height, width)
    reader = _open(path)
    try:
        rng = random.Random(4)
        cases = [(0, height, 0, width), (300, 301, 256, 257), (0, 1, 0, width),
                 (0, height, 128, 129), (150, 151, 0, 1)]
        for _ in range(25):
            y0, x0 = rng.randrange(height), rng.randrange(width)
            cases.append((y0, rng.randint(y0 + 1, height), x0, rng.randint(x0 + 1, width)))
        for y0, y1, x0, x1 in cases:
            got = reader.read(0, 1, y0, y1, x0, x1, z=1)
            assert got.shape == (y1 - y0, x1 - x0)
            np.testing.assert_array_equal(got, full[y0:y1, x0:x1], err_msg=f"{y0, y1, x0, x1}")
    finally:
        reader.close()


# -- names --------------------------------------------------------------------------


def test_names_are_the_dyes_of_tracks_that_record_one_channel(tmp_path):
    tracks = [LsmTrack(("Ch1",), ("DAPI",), name="DAPI"),
              LsmTrack(("ChS1",), ("Alexa Fluor 488",), name="Alexa488"),
              LsmTrack(("ChS2",), ("Cy3",), name="Cy3")]
    path = write_lsm(tmp_path / "d.lsm", channels=3, dtype="u1", z=1, tracks=tracks,
                     color_names=["Ch1-T1", "ChS1-T2", "ChS2-T3"])
    assert formats.channel_names(path) == ["DAPI", "Alexa Fluor 488", "Cy3"]


def test_a_track_recording_two_channels_gives_the_names_zen_shows(tmp_path):
    """Which detector made which channel is not written down: no dye."""
    tracks = [LsmTrack(("Ch1", "Ch2"), ("DAPI", "FITC"))]
    path = write_lsm(tmp_path / "t.lsm", channels=2, z=1, tracks=tracks,
                     color_names=["Ch1-T1", "Ch2-T1"])
    assert formats.channel_names(path) == ["Ch1-T1", "Ch2-T1"]


def test_without_shown_names_the_data_channels_name_them(tmp_path):
    tracks = [LsmTrack(("Ch1", "Ch2"), ("DAPI", "FITC"))]
    path = write_lsm(tmp_path / "n.lsm", channels=2, z=1, tracks=tracks)
    assert formats.channel_names(path) == ["Ch1", "Ch2"]


def test_a_track_that_does_not_acquire_is_not_a_channel(tmp_path):
    tracks = [LsmTrack(("Ch9",), ("Unused",), acquire=0),
              LsmTrack(("Ch1",), ("DAPI",)), LsmTrack(("Ch2",), ("Cy5",))]
    path = write_lsm(tmp_path / "a.lsm", channels=2, z=1, tracks=tracks)
    assert formats.channel_names(path) == ["DAPI", "Cy5"]


def test_without_any_name_channels_are_numbered(tmp_path):
    path = write_lsm(tmp_path / "x.lsm", channels=2, z=1)
    assert formats.channel_names(path) == ["Channel 1", "Channel 2"]


def test_a_lambda_stack_names_its_channels_by_centre_wavelength(tmp_path):
    path = write_lsm(tmp_path / "l.lsm", channels=3, dtype="u1", z=1, spectral=True,
                     wavelengths=[(418e-9, 428e-9), (500e-9, 510e-9), (650e-9, 670e-9)])
    assert formats.channel_names(path) == ["423 nm", "505 nm", "660 nm"]


# -- scale ---------------------------------------------------------------------------


def test_pixel_size_and_z_step_from_the_voxel_size_in_metres(tmp_path):
    path = write_lsm(tmp_path / "v.lsm", channels=2, z=3, voxel=(0.2e-6, 0.25e-6, 1.5e-6),
                     spacing=(9.0, 9.0, 9.0), tracks=[LsmTrack(("A", "B"), ())])
    description = formats.describe(path)
    assert description.format_name == "Zeiss LSM"
    assert description.pixel_size == (pytest.approx(0.2), pytest.approx(0.25))
    assert description.z_step == pytest.approx(1.5)
    meta = formats.physical_metadata(path)
    assert meta["physical_size_x"] == pytest.approx(0.2)
    assert meta["physical_size_z"] == pytest.approx(1.5)


def test_without_a_voxel_size_the_scan_information_spacing_scales(tmp_path):
    path = write_lsm(tmp_path / "s.lsm", channels=2, z=3, voxel=(0.0, 0.0, 0.0),
                     spacing=(0.5, 0.6, 2.0), tracks=[LsmTrack(("A", "B"), ())])
    description = formats.describe(path)
    assert description.pixel_size == (pytest.approx(0.5), pytest.approx(0.6))
    assert description.z_step == pytest.approx(2.0)


def test_no_scale_at_all_states_none(tmp_path):
    path = write_lsm(tmp_path / "n.lsm", channels=1, z=1, voxel=(0.0, 0.0, 0.0))
    assert formats.pixel_size(path) is None
    assert formats.physical_metadata(path) == {}


def test_an_x_z_section_has_rows_of_z_scaled_by_the_z_voxel_size(tmp_path):
    path = write_lsm(tmp_path / "xz.lsm", height=30, width=W, channels=2, z=1, scan_type=1,
                     voxel=(0.2e-6, 0.2e-6, 0.8e-6))
    reader = _open(path)
    try:
        assert reader.levels == [(30, W)]
        assert reader.z_count == 1 and reader.t_count == 1
        assert reader.pixel_size == (pytest.approx(0.2), pytest.approx(0.8))
        for c in range(2):
            np.testing.assert_array_equal(reader.read(0, c, 0, 30, 0, W),
                                          lsm_plane(0, 0, 0, c, 30, W))
    finally:
        reader.close()


def test_a_line_scan_has_rows_of_time_and_no_scale(tmp_path):
    path = write_lsm(tmp_path / "line.lsm", height=50, width=W, channels=1, z=1, scan_type=2,
                     voxel=(0.2e-6, 0.2e-6, 0.8e-6))
    reader = _open(path)
    try:
        assert reader.levels == [(50, W)]
        assert reader.z_count == 1 and reader.t_count == 1
        np.testing.assert_array_equal(reader.read(0, 0, 0, 50, 0, W), lsm_plane(0, 0, 0, 0, 50, W))
    finally:
        reader.close()
    # rows are time: no pixel size may claim they are distance
    assert formats.pixel_size(path) is None


# -- positions and tiles ------------------------------------------------------------


@pytest.mark.parametrize("dimension", ["positions", "tiles"])
def test_each_position_or_tile_is_an_image_of_its_own(tmp_path, dimension):
    path = write_lsm(tmp_path / "p.lsm", channels=2, z=2, t=2, **{dimension: 3})
    description = formats.describe(path)
    assert len(description.images) == 3 and len(set(description.images)) == 3
    assert (description.z_count, description.t_count, description.channels) == (2, 2, 2)
    for index in range(3):
        reader = _open(path, index)
        try:
            assert reader.image_index == index
            for t in range(2):
                for z in range(2):
                    for c in range(2):
                        np.testing.assert_array_equal(
                            reader.read(0, c, 0, H, 0, W, z=z, t=t),
                            lsm_plane(index, t, z, c, H, W),
                            err_msg=f"{dimension} {index} t={t} z={z} c={c}")
        finally:
            reader.close()
    with pytest.raises(FormatUnreadable):
        _open(path, 3)


def test_a_single_image_file_has_no_second_image(tmp_path):
    path = write_lsm(tmp_path / "one.lsm", channels=1, z=1)
    assert len(formats.describe(path).images) == 1
    with pytest.raises(FormatUnreadable):
        _open(path, 1)
    with pytest.raises(FormatUnreadable):
        lsm.reader(str(path), 5)


# -- damaged and foreign files ---------------------------------------------------


def test_a_strip_cut_short_reads_its_whole_rows_and_zeros_after(tmp_path):
    """The last page's last strip is the end of the file: cut into it."""
    channels, z = 2, 3
    full = write_lsm(tmp_path / "full.lsm", channels=channels, z=z)
    size = full.stat().st_size
    last_strip = lsm_strip_offset(full, z - 1, channels - 1)
    thumbnail_bytes = 3 * 8 * 6
    strip_end = size - thumbnail_bytes
    assert strip_end - last_strip == H * W * 2
    cut_row = 10
    cut_at = last_strip + cut_row * W * 2 + 7          # mid-way into row 10
    path = write_lsm(tmp_path / "cut.lsm", channels=channels, z=z, cut_bytes=size - cut_at)
    reader = _open(path)
    try:
        for zz in range(z):
            for c in range(channels):
                got = reader.read(0, c, 0, H, 0, W, z=zz)
                want = lsm_plane(0, 0, zz, c, H, W)
                assert got.shape == (H, W)
                if (zz, c) != (z - 1, channels - 1):
                    np.testing.assert_array_equal(got, want, err_msg=f"z={zz} c={c}")
                    continue
                np.testing.assert_array_equal(got[:cut_row], want[:cut_row])
                assert not got[cut_row + 1:].any()
        # a region wholly in what is gone, and one across the cut
        assert not reader.read(0, 1, 15, 20, 3, 9, z=z - 1).any()
        np.testing.assert_array_equal(reader.read(0, 1, 5, 10, 0, W, z=z - 1),
                                      lsm_plane(0, 0, z - 1, 1, H, W)[5:10])
    finally:
        reader.close()


def test_an_acquisition_stopped_part_way_opens_and_reads_what_it_has(tmp_path):
    """4 of the 6 planes the header states were written. Whether the last,
    incomplete time point is shown is the reader's choice; what it shows must
    be what was written, and a plane it lists but the file lacks reads zeros."""
    path = write_lsm(tmp_path / "abort.lsm", channels=2, z=3, t=2, pages_written=4)
    reader = _open(path)
    try:
        assert reader.z_count == 3 and reader.t_count in (1, 2)
        for z in range(3):
            np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W, z=z, t=0),
                                          lsm_plane(0, 0, z, 0, H, W))
        if reader.t_count == 2:
            np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W, z=0, t=1),
                                          lsm_plane(0, 1, 0, 1, H, W))
            for z in (1, 2):
                got = reader.read(0, 1, 0, H, 0, W, z=z, t=1)
                assert got.shape == (H, W) and not got.any()
    finally:
        reader.close()


def test_a_page_whose_strips_were_never_written_reads_zeros_not_the_header(tmp_path):
    """Strip offsets of 0: reading 'there' would read the TIFF header."""
    path = write_lsm(tmp_path / "hole.lsm", channels=2, z=3, unwritten=(1,))
    reader = _open(path)
    try:
        assert reader.z_count == 3
        for c in range(2):
            got = reader.read(0, c, 0, H, 0, W, z=1)
            assert got.shape == (H, W) and not got.any(), f"channel {c}"
            for z in (0, 2):
                np.testing.assert_array_equal(reader.read(0, c, 0, H, 0, W, z=z),
                                              lsm_plane(0, 0, z, c, H, W))
    finally:
        reader.close()


def test_a_corrupt_compressed_strip_is_refused_and_its_neighbours_read(tmp_path):
    path = write_lsm(tmp_path / "bad.lsm", channels=2, z=3, compression=5)
    at = lsm_strip_offset(path, 1, 0)
    data = bytearray(path.read_bytes())
    data[at:at + 60] = bytes(range(60))
    path.write_bytes(bytes(data))
    reader = _open(path)
    try:
        with pytest.raises(FormatUnreadable):
            reader.read(0, 0, 0, H, 0, W, z=1)
        np.testing.assert_array_equal(reader.read(0, 1, 0, H, 0, W, z=1),
                                      lsm_plane(0, 0, 1, 1, H, W))
        np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W, z=2),
                                      lsm_plane(0, 0, 2, 0, H, W))
    finally:
        reader.close()


def test_a_plain_tiff_named_lsm_is_refused(tmp_path):
    path = tmp_path / "plain.lsm"
    tifffile.imwrite(str(path), np.zeros((2, 16, 16), np.uint16))
    with pytest.raises(FormatUnreadable):
        lsm.reader(str(path))
    with pytest.raises(FormatUnreadable):
        formats.describe(path)


@pytest.mark.parametrize("content", [b"", b"II*\x00" + b"\xff" * 4, b"no tiff here" * 50])
def test_what_is_not_a_tiff_is_refused(tmp_path, content):
    path = tmp_path / "x.lsm"
    path.write_bytes(content)
    with pytest.raises(FormatUnreadable):
        lsm.reader(str(path))


# -- threads and the app -----------------------------------------------------------


def test_concurrent_reads_of_one_reader_agree(tmp_path):
    height, width = 200, 150
    path = write_lsm(tmp_path / "t.lsm", height=height, width=width, channels=3, z=4,
                     compression=5)
    full = {(z, c): lsm_plane(0, 0, z, c, height, width) for z in range(4) for c in range(3)}
    reader = _open(path)
    failures = []
    try:
        def work(seed):
            rng = random.Random(seed)
            for _ in range(30):
                z, c = rng.randrange(4), rng.randrange(3)
                y0, x0 = rng.randrange(height), rng.randrange(width)
                y1, x1 = rng.randint(y0 + 1, height), rng.randint(x0 + 1, width)
                if not np.array_equal(reader.read(0, c, y0, y1, x0, x1, z=z),
                                      full[(z, c)][y0:y1, x0:x1]):
                    failures.append((z, c, y0, y1, x0, x1))

        threads = [threading.Thread(target=work, args=(seed,)) for seed in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        reader.close()
    assert not failures, failures[:5]


def test_through_the_package_functions(tmp_path):
    path = write_lsm(tmp_path / "a.lsm", channels=2, z=5,
                     tracks=[LsmTrack(("Ch1",), ("DAPI",)), LsmTrack(("Ch2",), ("Cy5",))])
    assert formats.handles(path)
    assert formats.format_name(path) == "Zeiss LSM"
    verdict = formats.detect_image_type(path)
    assert verdict.verdict == "fluorescence" and verdict.confidence == "high"
    geometry = formats.geometry_of(path)
    assert (geometry["height"], geometry["width"], geometry["num_channels"]) == (H, W, 2)
    pyramid = formats.open_image(path)
    try:
        assert pyramid.z == 2 and pyramid.z_count == 5
        assert pyramid.channel_names == ["DAPI", "Cy5"]
        np.testing.assert_array_equal(np.asarray(pyramid["0"][1]), lsm_plane(0, 0, 2, 1, H, W))
        np.testing.assert_array_equal(np.asarray(pyramid.plane(4, 0)["0"][0]),
                                      lsm_plane(0, 0, 4, 0, H, W))
    finally:
        pyramid.close()
