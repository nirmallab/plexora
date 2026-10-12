"""The volume layer on its own: the view grammar, projections, every reader.

No app, no project: `plexora.server.volume` never imports `data_model`, and
these tests hold it to being correct by itself. End-to-end serving is
tests/test_volume_routes.py.
"""

import numpy as np
import pytest
import tifffile

from plexora.server.volume import reduce as volume_reduce
from plexora.server.volume.plane_view import plane_pyramid, section_rows
from plexora.server.volume.sources import open_volume
from plexora.server.volume.sources import tiff as tiff_volume
from plexora.server.volume.viewspec import BadView, ViewSpec
from tests import volume_fixtures as vf


class _Vol:
    depth, frames, width, height = 40, 3, 1000, 800

    @property
    def default_view(self):
        return ViewSpec(kind="plane", z=20)


# -- the grammar ---------------------------------------------------------


@pytest.mark.parametrize("token", ["z12", "t2z12", "z4-19max", "z0-39mean", "t1z3-9p95",
                                   "z2-8p99.5", "xzy120", "yzx99", "t2xzy5", "z1-3std",
                                   "z1-3med", "z1-3min", "z0-3sum"])
def test_a_token_reads_back_as_itself(token):
    assert ViewSpec.parse(token).token() == token


def test_slab_bounds_are_inclusive_and_ordered():
    spec = ViewSpec.parse("z19-4max", _Vol())
    assert (spec.z0, spec.z1) == (4, 19)
    assert spec.token() == "z4-19max"
    assert spec.planes == (4, 19)


def test_a_whole_stack_projection_resolves_to_its_planes():
    spec = ViewSpec.parse("zmax", _Vol())
    assert (spec.z0, spec.z1) == (0, 39)
    assert spec.token() == "z0-39max"


def test_the_empty_token_is_the_default_view():
    assert ViewSpec.parse("", _Vol()) == _Vol().default_view
    assert ViewSpec.parse("t2", _Vol()).z == 20


@pytest.mark.parametrize("token", ["z40", "t3z1", "z", "q3", "z1-2avg", "xzx3", "yzy3",
                                   "z1-2p101", "xzy800", "zz1", "z" * 60])
def test_a_bad_or_out_of_range_token_is_refused_not_substituted(token):
    with pytest.raises(BadView):
        ViewSpec.parse(token, _Vol())


def test_sum_parses_but_is_never_served():
    assert not ViewSpec.parse("z0-3sum").served
    assert ViewSpec.parse("z0-3p95").served


def test_window_classes_and_stats_keys():
    assert ViewSpec.parse("z3").window_class() == "volume"
    assert ViewSpec.parse("z1-4max").window_class() == "volume"
    assert ViewSpec.parse("z1-4std").window_class() == "std"
    assert ViewSpec.parse("z3").stats_key() == ViewSpec.parse("z9").stats_key() == "plane"
    assert ViewSpec.parse("z1-4max").stats_key() == "z1-4max"


def test_labels_say_what_the_picture_is():
    vol = _Vol()
    assert ViewSpec.parse("z12", vol).label(40) == "z 12 / 40"
    assert ViewSpec.parse("z8-20max", vol).label(40) == "MIP z 8–20"
    assert ViewSpec.parse("xzy120", vol).label(40) == "XZ @ y 120"


def test_the_typed_spelling_converts_to_the_token():
    vol = _Vol()
    assert ViewSpec.from_dict({"kind": "slab", "z0": 3, "z1": 9, "op": "max"}, vol).token() \
        == "z3-9max"
    assert ViewSpec.from_dict({"kind": "slab", "z0": 3, "z1": 9, "op": "pct", "q": 90},
                              vol).token() == "z3-9p90"
    assert ViewSpec.from_dict({"kind": "section", "axis": "yz", "index": 7}, vol).token() \
        == "yzx7"
    with pytest.raises(BadView):
        ViewSpec.from_dict({"kind": "slab", "op": "avg"}, vol)


# -- projections ---------------------------------------------------------


@pytest.mark.parametrize("op,q,expected", [
    ("max", None, lambda s: s.max(0)),
    ("min", None, lambda s: s.min(0)),
    ("mean", None, lambda s: np.rint(s.mean(0))),
    ("std", None, lambda s: np.rint(s.std(0))),
    ("med", None, lambda s: np.percentile(s, 50, axis=0, method="lower")),
    ("pct", 25.0, lambda s: np.percentile(s, 25, axis=0, method="lower")),
    ("pct", 90.0, lambda s: np.percentile(s, 90, axis=0, method="lower")),
])
def test_projections_match_numpy(op, q, expected):
    rng = np.random.default_rng(3)
    planes = rng.integers(0, 60000, size=(9, 300, 280), dtype=np.uint16)
    got = volume_reduce.reduce_stack(planes, op, q)
    assert got.dtype == np.uint16
    assert np.array_equal(got, expected(planes.astype(np.float64)).astype(np.uint16))


def test_sum_is_float_and_does_not_overflow():
    planes = np.full((40, 4, 4), 60000, dtype=np.uint16)
    got = volume_reduce.reduce_stack(planes, "sum")
    assert got.dtype == np.float32
    assert float(got[0, 0]) == 40 * 60000


def test_a_slab_reads_only_its_planes():
    seen = []

    def read(z):
        seen.append(z)
        return np.full((2, 2), z, dtype=np.uint16)

    volume_reduce.reduce_planes(read, 3, 6, "max")
    assert seen == [3, 4, 5, 6]


# -- TIFF ----------------------------------------------------------------


def test_an_ome_volume_with_a_z_step_is_read_as_depth_on_import(tmp_path):
    path, data = vf.write_ome_volume(tmp_path / "v.ome.tif", channels=3, depth=5,
                                     axes="CZYX", levels=2)
    source = open_volume(path, new_import=True)
    try:
        volume = source.volume
        assert (volume.channels, volume.depth, volume.frames) == (3, 5, 1)
        assert volume.spacing.z.value == pytest.approx(2.0)
        assert volume.spacing.z.source == "metadata"
        assert volume.spacing.anisotropy() == pytest.approx(4.0)
        assert volume.default_view.token() == "z2"
        assert len(volume.levels) == 2
        level = source.level_array(0, 3, 0)
        for c in range(3):
            assert np.array_equal(np.asarray(level[c, :10, :10]), data[0, c, 3, :10, :10])
    finally:
        source.close()


def test_an_existing_multi_channel_stack_keeps_its_flattened_reading(tmp_path):
    """A project registered before volumes has no recorded choice, and its
    channel list counted C x Z planes: opening it must not change that."""
    path, _ = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX")
    assert open_volume(path) is None
    assert open_volume(path, z_semantics="cycle", new_import=True) is None


def test_a_multi_channel_stack_without_a_z_step_imports_as_cycles(tmp_path):
    """The Akoya/CODEX convention: cycles stored on Z, no physical z step."""
    path, _ = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", physical_z=None)
    assert open_volume(path, new_import=True) is None
    chosen = open_volume(path, z_semantics="depth")
    try:
        assert chosen.volume.depth == 5
        assert chosen.volume.spacing.z.source == "missing"
        assert not chosen.volume.spacing.volumetric_ok()
    finally:
        chosen.close()


def test_a_single_channel_z_stack_default_plane_is_the_plane_always_shown(tmp_path):
    from plexora.server.utils import tiff_series
    import zarr

    path, data = vf.write_ome_volume(tmp_path / "z.ome.tif", axes="ZYX", depth=7)
    source = open_volume(path)
    try:
        assert source.volume.depth == 7 and source.volume.channels == 1
        with tifffile.TiffFile(str(path), is_ome=False) as handle:
            legacy = np.asarray(zarr.open(tiff_series.channel_series(handle).aszarr())[0])
        default = source.volume.default_view
        plane = np.asarray(source.level_array(default.t, default.z, 0)[0])
        assert np.array_equal(plane, legacy)
    finally:
        source.close()


def test_an_imagej_hyperstack_pages_are_indexed_in_its_order(tmp_path):
    path, data = vf.write_imagej_volume(tmp_path / "ij.tif", frames=2, channels=3, depth=4)
    source = open_volume(path, z_semantics="depth", t_semantics="time")
    try:
        volume = source.volume
        assert (volume.frames, volume.channels, volume.depth) == (2, 3, 4)
        assert volume.spacing.z.value == pytest.approx(2.0)
        for t, z in ((0, 0), (1, 3), (1, 1)):
            level = source.level_array(t, z, 0)
            for c in range(3):
                assert np.array_equal(np.asarray(level[c]), data[t, c, z])
    finally:
        source.close()


def test_folded_axes_keep_the_flattened_channel_order(tmp_path):
    """Depth served, frames folded: channels enumerate (t, c) in page order."""
    path, data = vf.write_imagej_volume(tmp_path / "ij.tif", frames=2, channels=3, depth=4)
    source = open_volume(path, z_semantics="depth", t_semantics="cycle")
    try:
        assert (source.volume.channels, source.volume.depth) == (6, 4)
        level = source.level_array(0, 2, 0)
        for t in range(2):
            for c in range(3):
                assert np.array_equal(np.asarray(level[t * 3 + c]), data[t, c, 2])
    finally:
        source.close()


def test_page_indices_ravel_in_page_order():
    layout = [("t", 2), ("z", 3), ("c", 4)]
    assert tiff_volume.page_indices(layout, {"t": 1, "z": 2}) == [20, 21, 22, 23]
    assert tiff_volume.page_indices(layout, {"z": 1}) == [4, 5, 6, 7, 16, 17, 18, 19]


# -- views ---------------------------------------------------------------


def test_slab_and_section_views_over_a_pyramidal_volume(tmp_path):
    path, data = vf.write_ome_volume(tmp_path / "v.ome.tif", axes="CZYX", levels=2,
                                     depth=5, physical_z=1.0, physical_xy=0.5)
    source = open_volume(path, new_import=True)
    try:
        volume = source.volume
        slab = plane_pyramid(source, ViewSpec.parse("z1-3max", volume))
        assert slab.level_shapes == [[96, 80], [48, 40]]
        assert np.array_equal(slab["0"][1, 0:16, 0:16], data[0, 1, 1:4, 0:16, 0:16].max(0))
        mean = plane_pyramid(source, ViewSpec.parse("z0-4mean", volume))
        assert np.array_equal(
            mean["0"][2, 5:9, 5:9],
            np.rint(data[0, 2, :, 5:9, 5:9].mean(0)).astype(np.uint16))

        section = plane_pyramid(source, ViewSpec.parse("xzy10", volume))
        # 5 planes at 1.0 um over 0.5 um pixels: 10 rows, each plane twice.
        assert section_rows(5, 2.0, 0) == 10
        assert section.level_shapes[0] == [10, 80]
        rows = np.asarray(section["0"][0, :, 3])
        assert list(rows) == [int(data[0, 0, z, 10, 3]) for z in (0, 0, 1, 1, 2, 2, 3, 3, 4, 4)]
        yz = plane_pyramid(source, ViewSpec.parse("yzx7", volume))
        assert yz.level_shapes[0] == [10, 96]
        assert int(yz["0"][1, 4, 20]) == int(data[0, 1, 2, 20, 7])
    finally:
        source.close()


# -- OME-Zarr ------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.4", "0.5"])
def test_an_ngff_volume_serves_any_plane(tmp_path, version):
    path, data = vf.write_ngff_volume(tmp_path / "v.ome.zarr", frames=2, depth=4,
                                      version=version)
    source = open_volume(path)
    try:
        volume = source.volume
        assert (volume.frames, volume.channels, volume.depth) == (2, 3, 4)
        assert volume.spacing.z.value == pytest.approx(2.0)
        assert volume.spacing.t.value == pytest.approx(1.0)
        level = source.level_array(1, 3, 0)
        assert np.array_equal(np.asarray(level[2, :8, :8]), data[1, 2, 3, :8, :8])
        coarse = source.level_array(1, 3, 1)
        assert np.array_equal(np.asarray(coarse[0]), data[1, 0, 3, ::2, ::2])
    finally:
        source.close()


def test_a_z_chunked_ngff_slab_is_one_read(tmp_path):
    path, data = vf.write_ngff_volume(tmp_path / "v.ome.zarr", depth=6, chunks_z=6)
    source = open_volume(path)
    try:
        assert source.prefers_block
        slab = plane_pyramid(source, ViewSpec.parse("z1-4max", source.volume))
        assert np.array_equal(slab["0"][0], data[0, 1:5].max(0))
    finally:
        source.close()


def test_a_planar_ngff_store_is_left_to_the_2d_reader(tmp_path):
    from tests.ngff_fixtures import write_ngff

    assert open_volume(write_ngff(tmp_path / "p.ome.zarr")) is None


def test_the_ngff_level_view_pins_the_plane_it_was_asked_for(tmp_path):
    from plexora.server.utils import ome_zarr

    path, data = vf.write_ngff_volume(tmp_path / "v.ome.zarr", depth=4)
    pyramid = ome_zarr.open_image(path, z=2)
    assert np.array_equal(np.asarray(pyramid[0][1]), data[1, 2])
    # The default is still plane 0, which every caller had before volumes.
    assert np.array_equal(np.asarray(ome_zarr.open_image(path)[0][1]), data[1, 0])


def test_ngff_physical_metadata_adds_z_and_t(tmp_path):
    from plexora.server.utils import ome_zarr

    path, _ = vf.write_ngff_volume(tmp_path / "v.ome.zarr", frames=2, depth=3)
    metadata = ome_zarr.physical_metadata(ome_zarr.open_image(path))
    assert metadata["physical_size_x"] == pytest.approx(0.5)
    assert metadata["physical_size_z"] == pytest.approx(2.0)
    assert metadata["physical_size_z_unit"] == "µm"
    assert metadata["time_increment"] == pytest.approx(1.0)


# -- IMS -----------------------------------------------------------------


def test_an_imaris_file_is_a_volume(tmp_path):
    path, data = vf.write_ims(tmp_path / "v.ims", frames=2, channels=2, depth=6,
                              names=["DAPI", "CD8"])
    source = open_volume(path)
    try:
        volume = source.volume
        assert (volume.frames, volume.channels, volume.depth) == (2, 2, 6)
        assert (volume.height, volume.width) == (96, 80)
        assert volume.channel_names == ("DAPI", "CD8")
        assert volume.spacing.x.value == pytest.approx(0.5)
        assert volume.spacing.z.value == pytest.approx(2.0)
        assert volume.spacing.t.value == pytest.approx(30.0)
        level = source.level_array(1, 4, 0)
        # The padding Imaris writes past the image is never served.
        assert level.shape == (2, 96, 80)
        assert np.array_equal(np.asarray(level[1, :, :]), data[1, 1, 4])
        slab = plane_pyramid(source, ViewSpec.parse("t1z0-5max", volume))
        assert np.array_equal(slab["1"][0], data[1, 0, :, ::2, ::2].max(0))
    finally:
        source.close()


def test_an_imaris_files_channel_keys_drop_the_extension(tmp_path):
    """`stack_0`, not `stack.ims_0`, as every other format's keys drop theirs.
    Only the trailing `_<N>` is ever parsed (`_parse_channel`), so a stack
    imported before this keeps the keys it recorded and still serves."""
    from plexora.server.models.data_model import _convert_ims_image, _parse_channel

    path, _ = vf.write_ims(tmp_path / "stack.ims", channels=2, depth=3)
    names = _convert_ims_image(str(path))["channel_names"]
    assert names == ["stack_0", "stack_1"]
    assert _parse_channel("stack.ims_1") == (1, False)


def test_a_planar_imaris_file_still_opens(tmp_path):
    path, data = vf.write_ims(tmp_path / "p.ims", depth=1)
    source = open_volume(path)
    try:
        assert source.volume.is_planar
        assert np.array_equal(np.asarray(source.level_array(0, 0, 0)[1]), data[0, 1, 0])
    finally:
        source.close()
