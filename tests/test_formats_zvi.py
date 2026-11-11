"""Zeiss ZVI, read from synthetic files written to the layout in the docstring
of `plexora/server/utils/formats/zvi.py` (the container by
`formats_fixtures_more.write_cfb`, to [MS-CFB]).

Every plane carries its own identity (`zvi_plane`: a base value per z,
channel, time and scene plus a ramp over the pixels), and the items are
written in a shuffled order, so a plane sorted by item number instead of by
its tags, or read from the wrong rows, cannot pass. The container's chains
are shuffled unless a test says otherwise.
"""

import random
import threading

import numpy as np
import pytest

from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable, zvi
from tests.formats_fixtures_more import (ZviPlane, stream_bytes, write_cfb, write_zvi,
                                         zvi_plane, zvi_rgb, zvi_stack, zvi_streams)

H, W = 24, 40


def _open(path, index=0):
    return formats.open_reader(path, index)


def _pixels_of(layout, path_in_file, data, cut, nbytes):
    """The last `nbytes` of an item stream (its pixels) as the cut file holds them."""
    return layout.held_after_cut(path_in_file, data, cut)[-nbytes:]


# -- planes in place -------------------------------------------------------------


@pytest.mark.parametrize("dtype", ["<u2", "u1"])
@pytest.mark.parametrize("version", [3, 4])
def test_every_plane_is_sorted_by_its_tags(tmp_path, version, dtype):
    planes = zvi_stack(z=3, c=2, t=2, height=H, width=W, dtype=dtype)
    write_zvi(tmp_path / "s.zvi", planes, version=version)
    reader = _open(tmp_path / "s.zvi")
    try:
        assert reader.levels == [(H, W)]
        assert (reader.channels, reader.z_count, reader.t_count) == (2, 3, 2)
        assert reader.dtype == np.dtype(dtype)
        assert not reader.rgb
        for z in range(3):
            for c in range(2):
                for t in range(2):
                    got = reader.read(0, c, 0, H, 0, W, z=z, t=t)
                    assert got.shape == (H, W) and got.dtype == np.dtype(dtype)
                    np.testing.assert_array_equal(got, zvi_plane(z, c, t, H, W, dtype),
                                                  err_msg=f"z={z} c={c} t={t}")
    finally:
        reader.close()


def test_items_without_tags_are_sorted_by_their_header_coordinates(tmp_path):
    planes = zvi_stack(z=3, c=2, t=1, tags=False, names=None)
    write_zvi(tmp_path / "n.zvi", planes)
    reader = _open(tmp_path / "n.zvi")
    try:
        assert (reader.channels, reader.z_count, reader.t_count) == (2, 3, 1)
        for z in range(3):
            for c in range(2):
                np.testing.assert_array_equal(reader.read(0, c, 0, H, 0, W, z=z),
                                              zvi_plane(z, c, 0, H, W), err_msg=f"z={z} c={c}")
    finally:
        reader.close()


def test_the_tags_win_over_the_header_coordinates(tmp_path):
    """Both say where a plane goes; when an item has tags they decide."""
    planes = zvi_stack(z=2, c=2, t=1)
    for plane in planes:
        plane.header_coordinates = False      # all zeros in the 128 bytes
    write_zvi(tmp_path / "t.zvi", planes)
    reader = _open(tmp_path / "t.zvi")
    try:
        assert (reader.channels, reader.z_count) == (2, 2)
        for z in range(2):
            for c in range(2):
                np.testing.assert_array_equal(reader.read(0, c, 0, H, 0, W, z=z),
                                              zvi_plane(z, c, 0, H, W))
    finally:
        reader.close()


def test_regions_cross_sector_and_band_boundaries(tmp_path):
    """A 700 x 333 uint16 plane is 466 KB: several of the ~256 KB row bands,
    and rows of 666 bytes that straddle 512-byte sectors of a shuffled chain."""
    height, width = 700, 333
    planes = [ZviPlane(zvi_plane(z, 0, 0, height, width), z=z, c=0, name="DAPI") for z in (1, 0)]
    write_zvi(tmp_path / "b.zvi", planes, layout="shuffled", seed=4)
    full = [zvi_plane(z, 0, 0, height, width) for z in range(2)]
    reader = _open(tmp_path / "b.zvi")
    try:
        rng = random.Random(1)
        cases = [(0, height, 0, width), (380, 420, 0, width), (390, 400, 330, 333),
                 (699, 700, 332, 333), (0, 1, 0, 1), (250, 520, 17, 300), (393, 394, 5, 6)]
        for _ in range(30):
            y0, x0 = rng.randrange(height), rng.randrange(width)
            cases.append((y0, rng.randint(y0 + 1, height), x0, rng.randint(x0 + 1, width)))
        for z in (0, 1):
            for y0, y1, x0, x1 in cases:
                got = reader.read(0, 0, y0, y1, x0, x1, z=z)
                assert got.shape == (y1 - y0, x1 - x0)
                np.testing.assert_array_equal(got, full[z][y0:y1, x0:x1],
                                              err_msg=f"z={z} [{y0}:{y1}, {x0}:{x1}]")
    finally:
        reader.close()


def test_a_plane_small_enough_for_the_mini_stream(tmp_path):
    planes = [ZviPlane(zvi_plane(0, c, 0, 12, 20, "u1"), c=c, name=f"C{c}") for c in range(3)]
    layout = write_zvi(tmp_path / "m.zvi", planes)
    assert all(layout.mini[f"Image/Item({n})/Contents"] for n in range(3))
    reader = _open(tmp_path / "m.zvi")
    try:
        assert reader.levels == [(12, 20)] and reader.channels == 3
        for c in range(3):
            np.testing.assert_array_equal(reader.read(0, c, 0, 12, 0, 20),
                                          zvi_plane(0, c, 0, 12, 20, "u1"))
            np.testing.assert_array_equal(reader.read(0, c, 5, 9, 3, 17),
                                          zvi_plane(0, c, 0, 12, 20, "u1")[5:9, 3:17])
    finally:
        reader.close()


@pytest.mark.parametrize("dtype,fmt", [("<i4", 5), ("<f4", 6), ("<f8", 7)])
def test_the_wider_grey_pixel_formats(tmp_path, dtype, fmt):
    plane = zvi_plane(0, 0, 0, H, W, "<u2").astype(dtype)
    if np.dtype(dtype).kind == "f":
        plane = plane / 7.0 - 3.0
    else:
        plane = plane - 2000
    write_zvi(tmp_path / "w.zvi", [ZviPlane(plane, pixel_format=fmt, name="A")])
    reader = _open(tmp_path / "w.zvi")
    try:
        assert reader.dtype == np.dtype(dtype)
        np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W), plane)
        np.testing.assert_array_equal(reader.read(0, 0, 3, 9, 30, 40), plane[3:9, 30:40])
    finally:
        reader.close()


def test_the_bytes_per_pixel_decide_when_they_disagree_with_the_format(tmp_path):
    """Pixel format 3 (grey uint8) but 2 bytes a pixel: the pixels are uint16."""
    plane = zvi_plane(0, 0, 0, H, W, "<u2")
    write_zvi(tmp_path / "d.zvi", [ZviPlane(plane, pixel_format=3, bytes_per_pixel=2, name="A")])
    reader = _open(tmp_path / "d.zvi")
    try:
        assert reader.dtype == np.dtype(np.uint16)
        np.testing.assert_array_equal(reader.read(0, 0, 0, H, 0, W), plane)
    finally:
        reader.close()


# -- colour ------------------------------------------------------------------------


def test_a_bgr_plane_is_an_rgb_picture_in_rgb_order(tmp_path):
    picture = zvi_rgb(H, W)
    write_zvi(tmp_path / "c.zvi", [ZviPlane(picture)])
    path = tmp_path / "c.zvi"
    description = formats.describe(path)
    assert description.rgb is True and description.dtype == "uint8"
    assert formats.detect_image_type(path).verdict == "brightfield"
    reader = _open(path)
    try:
        assert reader.rgb
        block = reader.read_rgb(0, 0, H, 0, W)
        assert block.shape == (H, W, 3) and block.dtype == np.uint8
        np.testing.assert_array_equal(block, picture)
        np.testing.assert_array_equal(reader.read_rgb(0, 5, 17, 11, 29), picture[5:17, 11:29])
        for sample in range(3):
            np.testing.assert_array_equal(reader.read(0, sample, 0, H, 0, W), picture[..., sample])
    finally:
        reader.close()


# -- what the tags say -----------------------------------------------------------


def test_channel_names_pixel_size_and_z_step(tmp_path):
    planes = zvi_stack(z=4, c=2, names=("Hoechst 33342", "Cy3"))
    for plane in planes:      # the filter set and emission are there too; the name wins
        plane.filter_set = ("49 DAPI BP", "43 Cy3")[plane.c]
        plane.emission = (461, 570)[plane.c]
    write_zvi(tmp_path / "m.zvi", planes, pixel_size=(0.0645, 0.0650), z_step=0.25)
    path = tmp_path / "m.zvi"
    description = formats.describe(path)
    assert description.format_name == "Zeiss ZVI"
    assert description.channel_names == ["Hoechst 33342", "Cy3"]
    assert description.pixel_size == (pytest.approx(0.0645), pytest.approx(0.0650))
    assert description.z_step == pytest.approx(0.25)
    assert (description.height, description.width) == (H, W)
    assert description.z_count == 4 and description.channels == 2
    assert description.level_shapes == [[H, W]]
    assert formats.channel_names(path) == ["Hoechst 33342", "Cy3"]
    meta = formats.physical_metadata(path)
    assert meta["physical_size_x"] == pytest.approx(0.0645)
    assert meta["physical_size_y"] == pytest.approx(0.0650)
    assert meta["physical_size_z"] == pytest.approx(0.25)


@pytest.mark.parametrize("unit,value,want", [(76, 0.5, 0.5), (75, 0.0005, 0.5), (77, 650.0, 0.65)],
                         ids=["um", "mm", "nm"])
def test_the_scaling_unit_is_applied(tmp_path, unit, value, want):
    planes = zvi_stack(z=2, c=1, names=("A",))
    write_zvi(tmp_path / "u.zvi", planes, pixel_size=(value, value), z_step=value * 4, unit=unit)
    description = formats.describe(tmp_path / "u.zvi")
    assert description.pixel_size == (pytest.approx(want), pytest.approx(want))
    assert description.z_step == pytest.approx(want * 4)


def test_a_unit_that_is_not_known_gives_no_pixel_size(tmp_path):
    planes = zvi_stack(z=2, c=1, names=("A",))
    write_zvi(tmp_path / "u.zvi", planes, pixel_size=(0.5, 0.5), z_step=1.0, unit=0)
    assert formats.pixel_size(tmp_path / "u.zvi") is None
    assert formats.physical_metadata(tmp_path / "u.zvi") == {}


def test_the_scaling_is_read_without_its_own_record(tmp_path):
    planes = zvi_stack(z=2, c=1, names=("A",))
    write_zvi(tmp_path / "s.zvi", planes, pixel_size=(0.32, 0.33), z_step=1.5,
              scaling_stream=False)
    description = formats.describe(tmp_path / "s.zvi")
    assert description.pixel_size == (pytest.approx(0.32), pytest.approx(0.33))
    assert description.z_step == pytest.approx(1.5)


def test_channels_without_names_are_numbered(tmp_path):
    write_zvi(tmp_path / "n.zvi", zvi_stack(z=1, c=2, names=None))
    assert formats.channel_names(tmp_path / "n.zvi") == ["Channel 1", "Channel 2"]


# -- several images in one file ----------------------------------------------------


def _two_scenes(tagged=True):
    planes = []
    for scene in (0, 1):
        for c in range(2):
            planes.append(ZviPlane(zvi_plane(0, c, 0, H, W, scene=scene), c=c,
                                   scene=scene if tagged else None, name=("DAPI", "FITC")[c]))
    return planes


def test_planes_repeating_z_channel_and_time_are_separate_images_by_scene(tmp_path):
    planes = _two_scenes()
    random.Random(3).shuffle(planes)
    write_zvi(tmp_path / "p.zvi", planes)
    path = tmp_path / "p.zvi"
    description = formats.describe(path)
    assert len(description.images) == 2
    assert len(set(description.images)) == 2
    assert description.channels == 2 and description.z_count == 1
    assert "2 images" in formats.note(path)
    # which scene is listed first is not fixed by the format; each image is
    # one scene, whole, and the two are the two scenes
    shown = []
    for index in (0, 1):
        reader = _open(path, index)
        try:
            assert reader.image_index == index
            scene = int(reader.read(0, 0, 0, 1, 0, 1)[0, 0]) // 5000
            shown.append(scene)
            for c in range(2):
                np.testing.assert_array_equal(reader.read(0, c, 0, H, 0, W),
                                              zvi_plane(0, c, 0, H, W, scene=scene),
                                              err_msg=f"image {index} channel {c}")
        finally:
            reader.close()
    assert sorted(shown) == [0, 1]
    with pytest.raises(FormatUnreadable):
        _open(path, 2)
    with pytest.raises(FormatUnreadable):
        zvi.reader(str(path), 2)


def test_untagged_repeats_are_grouped_by_the_order_the_planes_come_in(tmp_path):
    write_zvi(tmp_path / "o.zvi", _two_scenes(tagged=False))
    path = tmp_path / "o.zvi"
    assert len(formats.describe(path).images) == 2
    for index in (0, 1):
        reader = _open(path, index)
        try:
            for c in range(2):
                np.testing.assert_array_equal(reader.read(0, c, 0, H, 0, W),
                                              zvi_plane(0, c, 0, H, W, scene=index))
        finally:
            reader.close()


def test_an_image_beyond_a_single_image_file_is_refused(tmp_path):
    write_zvi(tmp_path / "one.zvi", zvi_stack(z=1, c=1, names=("A",)))
    path = tmp_path / "one.zvi"
    assert formats.describe(path).images and len(formats.describe(path).images) == 1
    with pytest.raises(FormatUnreadable):
        _open(path, 1)
    with pytest.raises(FormatUnreadable):
        formats.describe(path, 3)


# -- damaged and foreign files ---------------------------------------------------


@pytest.mark.parametrize("content", [b"", b"\x00" * 4096, b"Zeiss? no." * 300])
def test_a_file_that_is_not_a_compound_file_is_refused(tmp_path, content):
    path = tmp_path / "x.zvi"
    path.write_bytes(content)
    with pytest.raises(FormatUnreadable):
        zvi.reader(str(path))


def test_a_compound_file_without_an_image_is_refused(tmp_path):
    write_cfb(tmp_path / "doc.zvi", {"WordDocument": stream_bytes(5000, 1),
                                     "SummaryInformation": stream_bytes(100, 2)})
    with pytest.raises(FormatUnreadable):
        zvi.reader(str(tmp_path / "doc.zvi"))


def test_an_image_without_items_is_refused(tmp_path):
    streams = zvi_streams(zvi_stack(z=1, c=1, names=("A",)))
    streams = {k: v for k, v in streams.items() if not k.startswith("Image/Item(")}
    write_cfb(tmp_path / "empty.zvi", streams)
    with pytest.raises(FormatUnreadable):
        zvi.reader(str(tmp_path / "empty.zvi"))


def test_an_item_whose_header_is_garbage_is_refused_or_skipped_not_crashed(tmp_path):
    streams = zvi_streams(zvi_stack(z=2, c=1, names=("A",)))
    streams["Image/Item(0)/Contents"] = stream_bytes(5000, 99)
    write_cfb(tmp_path / "g.zvi", streams)
    try:
        reader = zvi.reader(str(tmp_path / "g.zvi"))
    except FormatUnreadable:
        return
    try:
        # whatever it kept must read as written
        assert reader.levels == [(H, W)]
        for z in range(reader.z_count):
            got = reader.read(0, 0, 0, H, 0, W, z=z)
            assert got.shape == (H, W)
    finally:
        reader.close()


def test_a_cut_file_reads_zeros_where_it_holds_nothing(tmp_path):
    """The last item's pixels are the last sectors of a contiguous file: cut
    into them and that plane reads zeros from the cut on; the others whole."""
    height, width = 64, 100
    planes = [ZviPlane(zvi_plane(z, 0, 0, height, width), z=z, name="A") for z in range(3)]
    streams = zvi_streams(planes)
    layout = write_cfb(tmp_path / "f.zvi", streams, layout="contiguous", shuffle_entries=False)
    last = "Image/Item(2)/Contents"
    end = max(offset + length for offset, length in layout.pieces[last])
    cut = end - 3000
    (tmp_path / "cut.zvi").write_bytes((tmp_path / "f.zvi").read_bytes()[:cut])
    nbytes = height * width * 2
    want_last = np.frombuffer(_pixels_of(layout, last, streams[last], cut, nbytes),
                              "<u2").reshape(height, width)
    assert (want_last == 0).any() and (want_last != 0).any()
    reader = _open(tmp_path / "cut.zvi")
    try:
        for z in range(2):
            np.testing.assert_array_equal(reader.read(0, 0, 0, height, 0, width, z=z),
                                          zvi_plane(z, 0, 0, height, width))
        np.testing.assert_array_equal(reader.read(0, 0, 0, height, 0, width, z=2), want_last)
    finally:
        reader.close()


# -- threads and the app -----------------------------------------------------------


def test_concurrent_reads_of_one_reader_agree(tmp_path):
    height, width = 300, 211
    planes = [ZviPlane(zvi_plane(z, c, 0, height, width), z=z, c=c, name=f"C{c}")
              for z in range(3) for c in range(2)]
    random.Random(2).shuffle(planes)
    write_zvi(tmp_path / "t.zvi", planes, version=4)
    full = {(z, c): zvi_plane(z, c, 0, height, width) for z in range(3) for c in range(2)}
    reader = _open(tmp_path / "t.zvi")
    failures = []
    try:
        def work(seed):
            rng = random.Random(seed)
            for _ in range(25):
                z, c = rng.randrange(3), rng.randrange(2)
                y0, x0 = rng.randrange(height), rng.randrange(width)
                y1, x1 = rng.randint(y0 + 1, height), rng.randint(x0 + 1, width)
                got = reader.read(0, c, y0, y1, x0, x1, z=z)
                if not np.array_equal(got, full[(z, c)][y0:y1, x0:x1]):
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
    planes = zvi_stack(z=5, c=2, names=("DAPI", "Cy5"))
    write_zvi(tmp_path / "a.zvi", planes)
    path = tmp_path / "a.zvi"
    assert formats.handles(path)
    assert not formats.handles(tmp_path / "missing.zvi")
    assert formats.format_name(path) == "Zeiss ZVI"
    verdict = formats.detect_image_type(path)
    assert verdict.verdict == "fluorescence" and verdict.confidence == "high"
    geometry = formats.geometry_of(path)
    assert (geometry["height"], geometry["width"], geometry["num_channels"]) == (H, W, 2)
    pyramid = formats.open_image(path)
    try:
        assert pyramid.z == 2 and pyramid.z_count == 5
        assert pyramid.level_shapes[0] == [H, W]
        np.testing.assert_array_equal(np.asarray(pyramid["0"][1]), zvi_plane(2, 1, 0, H, W))
        np.testing.assert_array_equal(np.asarray(pyramid.plane(4, 0)["0"][0]),
                                      zvi_plane(4, 0, 0, H, W))
    finally:
        pyramid.close()
