"""What Plexora concludes about a DICOM slide before it reads a pixel.

Two questions, both answered from headers alone. `is_dicom_path` is asked by a
file picker about a path that might be anything at all, so it has to be right
about a `.zarr` store and somebody's home directory as well as about a slide.
`detect_image_type` is asked once the path is known to be DICOM, and unlike the
TIFF detector it never has to guess: an instance states its Photometric
Interpretation, and that IS the brightfield/fluorescence distinction.
"""

import pytest

pytest.importorskip("wsidicom")
pytest.importorskip("pydicom")

from plexora.server.utils import brightfield, dicom_wsi  # noqa: E402
from tests.dicom_fixtures import (  # noqa: E402
    SlideIds,
    write_he_slide,
    write_if_slide,
    write_stained_only_slide,
    write_unnamed_slide,
)


# -- is this DICOM at all ------------------------------------------------


def test_a_dcm_file_is_dicom(tmp_path):
    write_if_slide(tmp_path / "slide")
    one = sorted((tmp_path / "slide").glob("*.dcm"))[0]

    assert dicom_wsi.is_dicom_path(one)


def test_a_folder_of_instances_is_dicom(tmp_path):
    write_if_slide(tmp_path / "slide")

    assert dicom_wsi.is_dicom_path(tmp_path / "slide")


def test_instances_nested_under_uid_folders_are_found(tmp_path):
    """The layout every real export has: `<slide>/<study>/<series>/*.dcm`.

    A scan that only looked in the folder the user picked would find nothing at
    all here, and the message would be "no DICOM in this folder" about a folder
    full of DICOM.
    """
    write_if_slide(tmp_path / "slide", nested=True)

    assert dicom_wsi.is_dicom_path(tmp_path / "slide")


def test_a_zarr_store_is_not_dicom(tmp_path):
    """A store is a directory of files, and walking into one hunting for `.dcm`
    is work that can only ever come back empty."""
    store = tmp_path / "image.zarr"
    (store / "0").mkdir(parents=True)
    (store / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")

    assert dicom_wsi.is_dicom_path(store) is False


def test_a_plain_folder_is_not_dicom(tmp_path):
    (tmp_path / "notes.txt").write_text("nothing here", encoding="utf-8")

    assert dicom_wsi.is_dicom_path(tmp_path) is False


def test_a_tiff_is_not_dicom(tmp_path):
    path = tmp_path / "slide.ome.tif"
    path.write_bytes(b"II*\0")

    assert dicom_wsi.is_dicom_path(path) is False


def test_nothing_is_not_dicom():
    assert dicom_wsi.is_dicom_path(None) is False
    assert dicom_wsi.is_dicom_path("") is False


# -- which kind of slide -------------------------------------------------


def test_a_multiplex_slide_is_fluorescence(tmp_path):
    slide = write_if_slide(tmp_path / "if")

    detection = dicom_wsi.detect_image_type(slide)

    assert detection.verdict == brightfield.FLUORESCENCE
    # High, not low: the file said MONOCHROME2. Confidence is what decides
    # whether the edit page volunteers the override, and volunteering it for a
    # slide that stated its own mode would be noise.
    assert detection.confidence == "high"
    assert "MONOCHROME2" in detection.reason
    assert "3 optical paths" in detection.reason
    # The markers are in the sentence, because "3 optical paths" is a fact
    # about the file and "DNA, CD3, Ki67" is a fact the user can check.
    assert "DNA" in detection.reason


def test_an_he_slide_is_brightfield(tmp_path):
    slide = write_he_slide(tmp_path / "he")

    detection = dicom_wsi.detect_image_type(slide)

    assert detection.verdict == brightfield.BRIGHTFIELD
    assert detection.confidence == "high"
    assert "RGB" in detection.reason


def test_a_single_path_slide_is_still_fluorescence(tmp_path):
    """One monochrome channel is a channel stack of one, not a picture.

    The mistake in the other direction from the three-plane one: a single grey
    plane is bright in places and has no colour in it, and reading it as
    brightfield would leave two samples with nothing to put in them.
    """
    slide = write_if_slide(tmp_path / "single", markers=("DAPI",))

    detection = dicom_wsi.detect_image_type(slide)

    assert detection.verdict == brightfield.FLUORESCENCE
    assert "single optical path" in detection.reason


def test_a_z_stack_says_so_in_the_reason(tmp_path):
    """The only place the focal plane count reaches a person.

    `ImageSpec` stores no field for it, so the detection reason -- which the
    edit page shows next to the override -- is what tells somebody their slide
    has three planes and they are looking at the middle one.
    """
    slide = write_if_slide(tmp_path / "z", focal_planes=3)

    detection = dicom_wsi.detect_image_type(slide)

    assert "3 focal planes" in detection.reason


def test_a_flat_slide_does_not_mention_focal_planes(tmp_path):
    slide = write_if_slide(tmp_path / "flat")

    assert "focal plane" not in dicom_wsi.detect_image_type(slide).reason


def test_an_unreadable_slide_keeps_the_default(tmp_path):
    """Never raises out of detection. A slide whose headers cannot be read is
    the case the override exists for, and low confidence is what makes the edit
    page offer it."""
    broken = tmp_path / "broken.dcm"
    broken.write_bytes(b"not a dicom file")

    detection = dicom_wsi.detect_image_type(broken)

    assert detection.verdict == brightfield.FLUORESCENCE
    assert detection.confidence == "low"


# -- what the channels are called ----------------------------------------


def test_optical_path_descriptions_become_marker_names(tmp_path):
    """Tier one, and the tier that answers for real multiplex data: a t-CyCIF
    export writes the marker into Optical Path Description."""
    slide = write_if_slide(tmp_path / "if")

    assert dicom_wsi.channel_names(slide) == ["DNA", "CD3", "Ki67"]


def test_names_come_from_the_staining_record_when_paths_are_unnamed(tmp_path):
    """Tier two: the specimen preparation steps say what was stained for, even
    when the optical paths themselves were left blank."""
    slide = write_stained_only_slide(tmp_path / "stained")

    assert dicom_wsi.channel_names(slide) == ["DNA", "CD3", "Ki67"]


def test_wavelengths_are_the_floor_before_giving_up(tmp_path):
    """Tier three. Not marker names, but they tell the channels apart, which is
    more than "Channel 1, 2, 3" does."""
    slide = write_unnamed_slide(tmp_path / "anon")

    assert dicom_wsi.channel_names(slide) == ["488 nm", "588 nm", "688 nm"]


def test_a_brightfield_slide_has_no_channel_names(tmp_path):
    """None, so the caller falls back -- and for brightfield the fallback is
    `["Image"]`, because there is one layer and it is not a marker."""
    slide = write_he_slide(tmp_path / "he")

    assert dicom_wsi.channel_names(slide) is None


def test_channel_names_never_raise(tmp_path):
    """Names are a nicety and tiles are not. A slide nobody can name still
    opens; it just opens anonymously."""
    broken = tmp_path / "broken.dcm"
    broken.write_bytes(b"not a dicom file")

    assert dicom_wsi.channel_names(broken) is None


def test_the_order_of_the_names_is_the_order_of_the_channels(tmp_path):
    """wsidicom reports a level's optical paths in the order it happened to
    read the instances, and that order differs BETWEEN LEVELS of one slide. If
    the reader did not impose one, channel 3 would be a different marker at a
    different zoom -- which nothing downstream could detect and no user would
    forgive."""
    slide = write_if_slide(tmp_path / "if", levels=2)
    pyramid = dicom_wsi.open_image(slide)
    try:
        assert list(pyramid.optical_paths) == ["0", "1", "2"]
        assert pyramid.channel_names == ["DNA", "CD3", "Ki67"]
    finally:
        pyramid.close()


def test_two_slides_do_not_pool_their_channels(tmp_path):
    """Names are read from the assembled slide, not the folder -- so a second
    slide sitting beside the first cannot lend it channels."""
    first = SlideIds(container="A")
    write_if_slide(tmp_path / "a", ids=first, markers=("DNA", "CD3"))
    write_if_slide(tmp_path / "b", markers=("DNA", "CD3", "Ki67"))

    assert dicom_wsi.channel_names(tmp_path / "a") == ["DNA", "CD3"]
