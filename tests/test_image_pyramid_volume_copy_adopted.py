"""A single-channel z-stack's pyramidized copy is adopted, not rebuilt.

Registering a flat z-stack builds a copy that holds EVERY plane
(`pyramidize_image(..., volume_description=...)`, data_model
`_pyramidized_copy`). The next registration of the same file -- a second
project, a re-import, a node attaching it -- must find that copy and use it,
the way a flat channel stack's copy is found (tests/test_image_pyramid.py).
A single-channel z-stack is the case `tiff_series.channel_series` reads as its
middle plane, so the copy's (planes, Y, X) has to be compared with the
original's planes, not with that one plane.
"""

from pathlib import Path

from plexora.server.models import data_model
from plexora.server.utils import image_pyramid
from plexora.server.utils.tiff_series import ImageTiff
from plexora.server.volume.sources import tiff as tiff_volume
from tests import volume_fixtures


def _single_channel_stack(folder: Path, depth=6):
    source, _ = volume_fixtures.write_ome_volume(
        folder / "stack.ome.tif", channels=1, depth=depth, height=600, width=500, axes="ZYX")
    return source


def _volume_description(source):
    """What `convertOmeTiff` hands `_pyramidized_copy` for this file."""
    tiff = ImageTiff(str(source))
    try:
        described = tiff_volume.describe(tiff, tiff.series[0], path=source,
                                         z_choice=None, t_choice=None, new_import=True)
        assert described is not None, "the fixture should read as a z-stack"
        _, layout = described
        return {
            "page_axes": [[name, size] for name, size in layout],
            "physical": dict(tiff_volume.page_layout(tiff, tiff.series[0])[2]),
        }
    finally:
        tiff.close()


def test_single_channel_z_stack_copy_is_adoptable(tmp_path):
    """A finished copy of every plane of a one-channel z-stack is recognised
    as that stack's copy: adoptable, and the one `find_derived_image` and
    `resolve_derived_image` return."""
    source = _single_channel_stack(tmp_path)
    copy = image_pyramid.derived_output_path(source)
    image_pyramid.pyramidize_image(source, copy, tile_size=256,
                                   volume_description=_volume_description(source))

    assert image_pyramid.is_adoptable_image(copy, source)
    assert image_pyramid.find_derived_image(source) == copy
    assert image_pyramid.resolve_derived_image(source).existing == copy


def test_registering_a_single_channel_z_stack_twice_builds_its_copy_once(tmp_path, monkeypatch):
    """`_pyramidized_copy` builds a one-channel z-stack's copy the first time
    and adopts it the second, as it does a flat channel stack's."""
    source = _single_channel_stack(tmp_path)
    description = _volume_description(source)
    original = image_pyramid.pyramidize_image
    builds = []

    def counting(src, dest, **kwargs):
        builds.append(Path(dest))
        kwargs["tile_size"] = 256  # small fixture: still more than one level
        return original(src, dest, **kwargs)

    monkeypatch.setattr(image_pyramid, "pyramidize_image", counting)
    project = tmp_path / "project"
    project.mkdir()

    def noop(*_args, **_kwargs):
        return None

    first = data_model._pyramidized_copy(str(source), str(project), noop, noop,
                                         volume_description=description)
    second = data_model._pyramidized_copy(str(source), str(project), noop, noop,
                                          volume_description=description)

    assert Path(second) == Path(first)
    assert len(builds) == 1, f"the copy was built {len(builds)} times: {builds}"
