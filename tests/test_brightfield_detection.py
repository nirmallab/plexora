"""Telling a brightfield slide from a fluorescence stack.

One test per rung of the ladder in `detect_image_type`, plus the two mistakes
that would matter most if they were ever made: calling a three-plex panel H&E
because it has three channels, and calling an H&E scan fluorescence because its
container is usually one.
"""

import numpy as np
import pytest

from plexora.server.utils import brightfield as bf
from tests.brightfield_fixtures import (
    write_ambiguous_planar,
    write_grayscale,
    write_interleaved_tiff,
    write_ome_with_contrast,
    write_planar_fluorescence,
    write_planar_rgb_tiff,
    write_rgb_ome_tiff,
    write_svs_like,
)


# -- tier 1: the container -----------------------------------------------


def test_whole_slide_suffix_is_brightfield_without_opening_the_file(tmp_path):
    """OpenSlide's formats have no concept of a channel, so the extension is
    the evidence. Checked against a path that does not exist, which is what
    proves nothing was read."""
    verdict = bf.detect_image_type(tmp_path / "nothing-here.mrxs")

    assert verdict.verdict == bf.BRIGHTFIELD
    assert verdict.confidence == "high"
    assert ".mrxs" in verdict.reason


@pytest.mark.parametrize("suffix", [".svs", ".ndpi", ".scn", ".mrxs"])
def test_every_wsi_suffix_is_recognised(tmp_path, suffix):
    assert bf.is_wsi_path(tmp_path / f"slide{suffix}")
    assert bf.detect_image_type(tmp_path / f"slide{suffix}").is_brightfield


def test_svs_reads_as_colour_and_states_its_scale(tmp_path):
    path = write_svs_like(tmp_path / "slide.svs")

    assert bf.is_rgb_layout(path)
    assert bf.detect_image_type(path).is_brightfield
    assert bf.physical_metadata(path)["physical_size_x"] == pytest.approx(0.2465)


# -- tier 2: the storage layout ------------------------------------------


def test_interleaved_rgb_with_no_metadata_is_brightfield(tmp_path):
    """Bio-Formats' isRGB(), and the only signal this file carries."""
    path = write_interleaved_tiff(tmp_path / "colour.tif")
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.BRIGHTFIELD
    assert verdict.confidence == "high"
    assert bf.is_rgb_layout(path)


def test_separate_plane_rgb_is_not_taken_at_its_word(tmp_path):
    """`photometric=RGB` on separate planes is worth nothing on its own:
    tifffile writes it by DEFAULT for any three-plane uint8 array, so a large
    share of the 8-bit fluorescence stacks in existence declare themselves
    colour without anybody having meant it.

    This particular one is a stained section, so the pixel tier still gets it
    right -- but at low confidence, through the pixels, not by taking the tag
    as proof."""
    path = write_planar_rgb_tiff(tmp_path / "separate.tif")
    verdict = bf.detect_image_type(path)

    assert not bf.is_rgb_layout(path)
    assert verdict.is_brightfield
    assert verdict.confidence == "low"


def test_a_dark_three_plane_stack_is_not_flipped_by_the_default_tag(tmp_path):
    """The regression that rule prevents: an ordinary 8-bit fluorescence stack
    written the way tifffile writes one, which carries `photometric=RGB` and
    means nothing by it."""
    import tifffile as tf

    path = tmp_path / "default.tif"
    tf.imwrite(path, np.zeros((3, 128, 128), np.uint8))

    assert not bf.is_rgb_layout(path)
    assert bf.detect_image_type(path).verdict == bf.FLUORESCENCE


# -- tier 3: the OME block -----------------------------------------------


def test_rgb_ome_tiff_is_read_from_its_channel_element(tmp_path):
    """One Channel carrying three samples is how OME-XML spells RGB. This is
    the file the whole feature exists for -- an H&E scan in the container
    Plexora had only ever seen fluorescence in."""
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.BRIGHTFIELD
    assert verdict.confidence == "high"
    assert "three samples" in verdict.reason


def test_three_single_sample_channels_are_fluorescence(tmp_path):
    """The mistake that would matter most. Three channels, in the same
    container, at the same size as the RGB file above -- and nothing about the
    count is consulted."""
    path = write_planar_fluorescence(tmp_path / "panel.ome.tif")
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.FLUORESCENCE
    assert verdict.confidence == "high"
    assert not bf.is_rgb_layout(path)


def test_contrast_method_beats_the_storage_layout(tmp_path):
    """A file that states how it was acquired is telling us something the byte
    layout cannot. Written onto planes that carry no other signal, so the
    ContrastMethod is demonstrably what decided it."""
    path = write_ome_with_contrast(tmp_path / "bf.ome.tif", "Brightfield")
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.BRIGHTFIELD
    assert verdict.confidence == "high"
    assert "ContrastMethod" in verdict.reason


def test_contrast_method_says_fluorescence_too(tmp_path):
    path = write_ome_with_contrast(tmp_path / "fl.ome.tif", "Fluorescence")
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.FLUORESCENCE
    assert verdict.confidence == "high"


# -- tier 4: channel names -----------------------------------------------


@pytest.mark.parametrize("name", ["DAPI", "AF647", "Alexa 488", "Opal 570",
                                  "Cy5", "Hoechst"])
def test_a_fluorophore_name_says_fluorescence(name):
    verdict = bf._detect_from_names([name, "Something", "Else"])

    assert verdict is not None
    assert verdict.verdict == bf.FLUORESCENCE
    assert verdict.confidence == "medium"


@pytest.mark.parametrize("names", [["R", "G", "B"], ["Red", "Green", "Blue"]])
def test_channels_named_after_colours_say_brightfield(names):
    verdict = bf._detect_from_names(names)

    assert verdict is not None
    assert verdict.verdict == bf.BRIGHTFIELD


def test_pure_rgb_channel_colours_say_brightfield():
    verdict = bf._detect_from_names(["one", "two", "three"],
                                    [0xFF0000, 0x00FF00, 0x0000FF])

    assert verdict is not None
    assert verdict.verdict == bf.BRIGHTFIELD


def test_ordinary_marker_names_decide_nothing():
    """Neither a fluorophore nor a colour. The ladder has to fall through to
    the pixels rather than guess from a panel's marker names."""
    assert bf._detect_from_names(["CD3", "CD8", "PanCK"]) is None


# -- tier 5: the pixels --------------------------------------------------


def test_a_light_three_plane_image_leans_brightfield(tmp_path):
    """Nothing structural to go on: three minisblack planes, 8-bit. All that is
    left is QuPath's move -- transmitted light is bright almost everywhere."""
    path = write_ambiguous_planar(tmp_path / "light.tif", light=True)
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.BRIGHTFIELD
    assert verdict.confidence == "low"


def test_a_dark_three_plane_image_leans_fluorescence(tmp_path):
    path = write_ambiguous_planar(tmp_path / "dark.tif", light=False)
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.FLUORESCENCE
    assert verdict.confidence == "low"


def test_sixteen_bit_pixels_are_not_a_camera(tmp_path):
    """A colour camera's output is 8 bits per sample. Deeper than that is a
    scientific detector, whatever the rest of the file looks like."""
    path = write_ambiguous_planar(tmp_path / "deep.tif", light=True,
                                  dtype=np.uint16)
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.FLUORESCENCE


def test_a_bright_single_plane_is_not_brightfield(tmp_path):
    """Bright, and still not colour. A scanned grayscale section would be read
    as RGB and fail on the two planes that are not there."""
    path = write_grayscale(tmp_path / "grey.tif", light=True)
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.FLUORESCENCE
    assert not bf.is_rgb_layout(path)


# -- tier 6: the default -------------------------------------------------


def test_an_unreadable_file_keeps_the_default(tmp_path):
    path = tmp_path / "truncated.tif"
    path.write_bytes(b"II*\x00nonsense")
    verdict = bf.detect_image_type(path)

    assert verdict.verdict == bf.FLUORESCENCE
    assert verdict.confidence == "low"
    assert not bf.is_rgb_layout(path)


def test_plane_count_reads_both_layouts(tmp_path):
    interleaved = bf._tiff_layout(write_interleaved_tiff(tmp_path / "i.tif"))
    planar = bf._tiff_layout(write_planar_fluorescence(tmp_path / "p.tif"))
    grey = bf._tiff_layout(write_grayscale(tmp_path / "g.tif"))

    assert bf.plane_count(interleaved) == 3
    assert bf.plane_count(planar) == 3
    assert bf.plane_count(grey) == 1
