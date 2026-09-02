"""Working out which files are one slide.

The problem DICOM has and no other format Plexora reads does: a slide is a
collection of instances, the collection is named nowhere, and the directory
holding them is not the answer -- it may hold two slides, or one slide split
across subdirectories named after UIDs. So the identity comes out of the
headers, and these are the cases that make that worth doing.
"""

import pytest

pytest.importorskip("wsidicom")
pytest.importorskip("pydicom")

from plexora.server.utils import dicom_wsi  # noqa: E402
from tests.dicom_fixtures import (  # noqa: E402
    SlideIds,
    write_associated_images,
    write_if_slide,
    write_two_slides,
)


def test_a_folder_assembles_into_one_slide(tmp_path):
    slide = write_if_slide(tmp_path / "slide", levels=2)

    source = dicom_wsi.assemble_slide(slide)

    assert source.kind == "files"
    assert len(source.files) == 6      # two levels x three markers
    assert source.label == "SLIDE-1"   # the Container Identifier


def test_picking_one_instance_gathers_the_whole_slide(tmp_path):
    """The click that has to work: a user opens a file dialog, sees 252 files
    with UUID names, and picks one. That has to mean the slide, not the file --
    otherwise a 36-marker pyramid opens as one channel of one level."""
    write_if_slide(tmp_path / "slide", levels=2)
    one = sorted((tmp_path / "slide").glob("*.dcm"))[0]

    source = dicom_wsi.assemble_slide(one)

    assert len(source.files) == 6
    assert one in source.files


def test_instances_nested_under_uid_folders_are_gathered(tmp_path):
    slide = write_if_slide(tmp_path / "slide", levels=2, nested=True)

    assert len(dicom_wsi.assemble_slide(slide).files) == 6


def test_associated_images_belong_to_the_slide(tmp_path):
    """Label and overview instances are part of the *collection* -- they share
    every identifier -- even though they are not part of the image. Assembly
    gathers them; the pyramid is what leaves them out."""
    ids = SlideIds(container="WITH-LABEL")
    slide = write_if_slide(tmp_path / "slide", ids=ids)
    write_associated_images(slide, ids=ids)

    assert len(dicom_wsi.assemble_slide(slide).files) == 5


# -- more than one slide -------------------------------------------------


def test_a_folder_of_two_slides_is_refused_by_name(tmp_path):
    """Refused rather than guessed. Both slides are real, both are somebody's
    data, and opening whichever sorted first would be a coin flip presented as
    an answer -- so the error names them and says what to click instead."""
    folder, _first, _second = write_two_slides(tmp_path / "box")

    with pytest.raises(ValueError) as raised:
        dicom_wsi.assemble_slide(folder)

    message = str(raised.value)
    assert "2 slides" in message
    assert "SLIDE-A" in message and "SLIDE-B" in message
    assert "Pick a .dcm file" in message


def test_picking_a_file_in_a_two_slide_folder_is_never_ambiguous(tmp_path):
    """Which is why the error above tells the user to do exactly this: a file
    belongs to one slide by definition, so the refusal has a way out that needs
    no further questions."""
    folder, first, _second = write_two_slides(tmp_path / "box")
    one = sorted((folder / "a").glob("*.dcm"))[0]

    source = dicom_wsi.assemble_slide(one)

    assert source.label == first.container
    assert len(source.files) == 3
    assert all(path.parent.name == "a" for path in source.files)


def test_slides_sharing_a_study_are_still_two_slides(tmp_path):
    """Two sections off one block share a study, which is why the study alone
    cannot be the grouping key -- the Container Identifier is the barcode on
    the glass and that is what "one slide" means."""
    folder, _first, _second = write_two_slides(tmp_path / "box")
    instances = dicom_wsi.slide_instances(sorted((folder / "a").glob("*.dcm"))[0])

    assert len({instance.study_uid for instance in instances}) == 1
    assert {instance.container for instance in instances} == {"SLIDE-A"}


# -- nothing to assemble -------------------------------------------------


def test_a_folder_with_no_instances_says_so(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.txt").write_text("no slides", encoding="utf-8")

    with pytest.raises(ValueError, match="no DICOM whole-slide images"):
        dicom_wsi.assemble_slide(empty)


def test_a_file_that_is_not_a_slide_says_so(tmp_path):
    path = tmp_path / "broken.dcm"
    path.write_bytes(b"not a dicom file")

    with pytest.raises(ValueError, match="not a DICOM whole-slide image"):
        dicom_wsi.assemble_slide(path)


def test_a_dicomdir_is_read_as_its_folder(tmp_path):
    """A DICOMDIR is an index of the directory it sits in, so picking one means
    picking the directory -- the same slide either way."""
    slide = write_if_slide(tmp_path / "slide")
    (slide / "DICOMDIR").write_bytes(b"")

    source = dicom_wsi.assemble_slide(slide / "DICOMDIR")

    assert len(source.files) == 3


# -- the DICOMweb seam ---------------------------------------------------


def test_a_web_source_is_refused_until_there_is_a_client(tmp_path):
    """`SlideSource` is the one branch point between a slide on a filesystem
    and a slide in a PACS. Nothing else in the reader would change to support
    DICOMweb, and this asserts the shape that promise is made in."""
    source = dicom_wsi.SlideSource(kind="web", label="study/series")

    with pytest.raises(ValueError, match="not supported yet"):
        source.open()
