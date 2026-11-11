"""A large z-stack written without a pyramid (a stitched mosaic in z).

Its default plane's coarse levels are derived at import into the project
folder; every other plane resamples them from its own level above as they are
read (`base.build(derive=True)`), so the stack is served plane by plane rather
than as its middle plane alone. Each plane here is filled with its own value,
so a level read at plane z that came from any other plane is caught.
"""
import numpy as np
import pytest

from plexora.server.utils import brightfield, formats
from plexora.server.utils.formats import base
from plexora.server.volume.sources import formats as format_volume


HEIGHT, WIDTH, DEPTH, CHANNELS = 9000, 7000, 5, 2


def _value(channel, z):
    return 1000 * (channel + 1) + 100 * z


class _FlatStack(base.Reader):
    """One native level, too large for a coarse tile to be read from it."""

    format_name = "Test stack"

    def __init__(self, rgb=False):
        super().__init__()
        self.levels = [(HEIGHT, WIDTH)]
        self.rgb = rgb
        self.channels = 3 if rgb else CHANNELS
        self.dtype = np.dtype(np.uint8 if rgb else np.uint16)
        self.channel_names = (["Red", "Green", "Blue"] if rgb
                              else [f"C{index}" for index in range(CHANNELS)])
        self.z_count = DEPTH
        self.z_step = 2.0
        self.pixel_size = (0.5, 0.5)
        self.images = ["stack"]
        self.reads = []

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0):
        assert level == 0
        self.reads.append((int(z), y1 - y0, x1 - x0))
        if self.rgb:
            return self.read_rgb(level, y0, y1, x0, x1, z, t)[..., int(channel)].copy()
        return np.full((y1 - y0, x1 - x0), _value(channel, z), dtype=np.uint16)

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0):
        block = np.empty((y1 - y0, x1 - x0, 3), dtype=np.uint8)
        block[...] = (10 + 20 * z, 20 + 20 * z, 30 + 20 * z)
        return block


def _imported(tmp_path, reader):
    """The default plane with its coarse levels derived, as an import leaves it."""
    pyramid = base.build(reader, tmp_path / "stack.test")
    assert formats.needs_extension(pyramid)
    extension = tmp_path / formats.EXTENSION_NAME
    formats.build_extension(pyramid, str(extension))
    return base.build(reader, tmp_path / "stack.test", extension=str(extension))


def test_the_default_plane_alone_lacks_coarse_levels_until_they_are_derived(tmp_path):
    reader = _FlatStack()
    flat = base.build(reader, tmp_path / "stack.test")
    assert len(flat) < len(brightfield._dyadic_shapes(HEIGHT, WIDTH))
    assert format_volume.describe_volume(flat) is None


def test_every_plane_has_the_default_plane_s_levels(tmp_path):
    reader = _FlatStack()
    pyramid = _imported(tmp_path, reader)
    assert len(pyramid) == len(brightfield._dyadic_shapes(HEIGHT, WIDTH))
    for z in range(DEPTH):
        plane = pyramid.plane(z, 0)
        assert plane.level_shapes == pyramid.level_shapes
        assert plane.base_levels == pyramid.base_levels


@pytest.mark.parametrize("z", [0, 1, 3, 4])
def test_a_coarse_level_of_another_plane_is_that_plane_s_own_pixels(tmp_path, z):
    reader = _FlatStack()
    pyramid = _imported(tmp_path, reader)
    reader.reads.clear()  # the import read the default plane to derive its levels
    plane = pyramid.plane(z, 0)
    coarsest = plane[str(len(plane) - 1)]
    for channel in range(CHANNELS):
        block = np.asarray(coarsest[channel, :, :])
        assert block.shape == tuple(pyramid.level_shapes[-1])
        assert np.all(block == _value(channel, z)), (channel, np.unique(block))
    # Read from this plane only, and never the derived store of the default one.
    assert {read_z for read_z, _h, _w in reader.reads} == {z}


def test_a_coarse_tile_of_another_plane_is_bounded_reads_of_that_plane(tmp_path):
    reader = _FlatStack()
    pyramid = _imported(tmp_path, reader)
    plane = pyramid.plane(0, 0)
    level = plane[str(plane.base_levels)]
    tile = np.asarray(level[0, 0:brightfield.TILE_SIZE, 0:brightfield.TILE_SIZE])
    assert np.all(tile == _value(0, 0))
    assert reader.reads
    assert max(h for _z, h, _w in reader.reads) <= HEIGHT
    assert max(w for _z, _h, w in reader.reads) <= WIDTH


def test_a_colour_stack_s_planes_are_their_own_colour(tmp_path):
    reader = _FlatStack(rgb=True)
    pyramid = _imported(tmp_path, reader)
    plane = pyramid.plane(4, 0)
    assert plane.level_shapes == pyramid.level_shapes
    coarsest = plane[str(len(plane) - 1)]
    rgb = np.asarray(coarsest.read_rgb(slice(0, 8), slice(0, 8)))
    assert tuple(rgb[0, 0]) == (90, 100, 110)


def test_the_stack_is_described_as_a_volume_once_its_default_plane_is_derived(tmp_path):
    reader = _FlatStack()
    pyramid = _imported(tmp_path, reader)
    volume = format_volume.describe_volume(pyramid)
    assert volume is not None
    assert (volume.depth, volume.frames, volume.channels) == (DEPTH, 1, CHANNELS)
    assert (volume.height, volume.width) == (HEIGHT, WIDTH)
    assert len(volume.levels) == len(pyramid)
    assert volume.default_view.z == pyramid.z == DEPTH // 2


def test_a_small_stack_derives_nothing_at_any_plane(tmp_path):
    reader = _FlatStack()
    reader.levels = [(2000, 1500)]
    pyramid = base.build(reader, tmp_path / "stack.test")
    assert not formats.needs_extension(pyramid)
    plane = pyramid.plane(1, 0)
    assert plane.base_levels == len(plane) == len(pyramid)
