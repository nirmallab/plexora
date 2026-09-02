"""What a project records about a DICOM slide.

The conversion's output is the contract everything downstream reads: how many
layers the viewer draws, what they are called, which reader opens the file next
time, and what the edit page shows next to the override. A DICOM slide has to
arrive at the same shape of answer as every other format -- that is the whole
claim of "no separate viewer pathway" -- so this checks the record rather than
the pixels.
"""

import pytest

pytest.importorskip("wsidicom")
pytest.importorskip("pydicom")

from plexora.datasource import (  # noqa: E402
    _derive_dataset_name_from_path,
    derive_image_channel_names,
    register_image_datasource,
    reregister_image,
)
from plexora.server.models.data_model import _image_channel_stem  # noqa: E402
from plexora.server.models.project import Project  # noqa: E402
from tests.dicom_fixtures import (  # noqa: E402
    write_he_slide,
    write_if_slide,
    write_unnamed_slide,
)


# -- multiplex -----------------------------------------------------------


def test_a_multiplex_slide_registers_as_one_layer_per_marker(tmp_path):
    slide = write_if_slide(tmp_path / "panel")

    entry = register_image_datasource("panel", slide)

    # A new kind, because the file is not a TIFF and `_missing_pyramid` has to
    # be able to tell them apart. The browser never compares against it: every
    # image_kind test there is `== "brightfield"` or `== "rgb"`.
    assert entry["image_kind"] == "dicom"
    assert entry["num_channels"] == 3
    assert entry["width"] == 320 and entry["height"] == 256
    assert entry["tileWidth"] == 1024 and entry["tileHeight"] == 1024
    assert entry["maxLevel"] == 1
    assert entry["imageTypeDetected"] == "fluorescence"
    assert entry["imageTypeReason"]
    # Nobody chose anything, so nothing outlives the detector.
    assert "imageTypeChoice" not in entry


def test_the_markers_name_themselves(tmp_path):
    """The point of reading Optical Path Description at all: a 36-marker slide
    should not need a channel-names CSV uploaded after the fact to find CD45."""
    slide = write_if_slide(tmp_path / "panel")

    register_image_datasource("panel", slide)

    assert Project.find("panel").image.channel_names == ["DNA", "CD3", "Ki67"]


def test_an_unnamed_panel_falls_back_to_generic_names(tmp_path):
    """All-or-nothing. A half-named layer list reads as though the unnamed ones
    are broken rather than merely anonymous -- so when a tier cannot answer for
    every channel, none of them takes its answer."""
    slide = write_unnamed_slide(tmp_path / "anon")
    register_image_datasource("anon", slide)

    # This fixture does carry wavelengths, which is the last tier above the
    # floor, so the names are those rather than "Channel N".
    assert Project.find("anon").image.channel_names == \
        ["488 nm", "588 nm", "688 nm"]


def test_generic_names_are_the_floor(tmp_path):
    """And when even the wavelengths are missing, `derive_image_channel_names`
    supplies the same "Channel N" every other format falls back to."""
    slide = write_if_slide(tmp_path / "plain", describe=True)
    names, source = derive_image_channel_names(slide, 3)

    assert source == "image metadata"
    assert names == ["DNA", "CD3", "Ki67"]


def test_the_folder_names_the_project(tmp_path):
    """A DICOM slide's files have UUID names; the folder is what a person calls
    it. Both halves of the name derivation -- the server's and the browser's --
    have to agree, or a quick view and a wizard import of one slide suggest two
    different project names."""
    assert _derive_dataset_name_from_path(tmp_path / "HTA7_926") == "HTA7_926"
    assert _derive_dataset_name_from_path("00ecf0d8-bd25.dcm") == "00ecf0d8-bd25"


def test_channel_keys_strip_the_dicom_suffix(tmp_path):
    """`_parse_channel` reads the trailing `_<N>` off a channel key to get the
    index, so every format has to arrive at a stem it can do that to."""
    assert _image_channel_stem("slide.dcm") == "slide"
    assert _image_channel_stem("HTA7_926") == "HTA7_926"


# -- brightfield ---------------------------------------------------------


def test_an_he_slide_registers_as_one_colour_layer(tmp_path):
    """`image_kind` is `brightfield`, not `dicom`: nothing about drawing an H&E
    slide differs by where it came from, so nothing should have to know. That
    is also what gets it `["Image"]` and the colour tile route for free."""
    slide = write_he_slide(tmp_path / "he")

    entry = register_image_datasource("he", slide)

    assert entry["image_kind"] == "brightfield"
    # Three planes -- what a node's geometry check compares against -- and one
    # layer, which is what the viewer draws.
    assert entry["num_channels"] == 3
    assert [c["fullname"] for c in entry["imageData"]] == ["Image"]
    assert entry["imageData"][0]["src"].endswith("/rgb/")
    assert entry["imageTypeDetected"] == "brightfield"


# -- the override --------------------------------------------------------


def test_an_he_slide_can_be_read_as_three_markers(tmp_path):
    """The same pyramid through the same CYX views, by code that was never told
    the samples were interleaved."""
    slide = write_he_slide(tmp_path / "he")

    entry = register_image_datasource("he", slide, image_type="fluorescence")

    assert entry["image_kind"] == "dicom"
    assert entry["num_channels"] == 3
    assert len(entry["imageData"]) == 3
    assert entry["imageTypeDetected"] == "brightfield"


def test_the_override_round_trips_both_ways(tmp_path):
    slide = write_he_slide(tmp_path / "he")
    register_image_datasource("he", slide)

    Project.find("he").with_image_type("fluorescence").save()
    flipped = reregister_image("he")
    assert flipped.image.kind == "dicom"
    assert len(flipped.image.channels) == 3

    Project.find("he").with_image_type(None).save()
    back = reregister_image("he")
    assert back.image.kind == "brightfield"
    assert [c["fullname"] for c in back.image.channels] == ["Image"]


def test_a_multiplex_slide_refuses_to_be_read_as_one_colour_image(tmp_path):
    """The one override DICOM will not take, and the reason it is refused
    rather than approximated: a monochrome slide has no colour in it, so
    "brightfield" could only mean borrowing three markers and calling them red,
    green and blue. Three markers displayed as a colour photograph is not a
    worse rendering of the data, it is a different claim about it.
    """
    slide = write_if_slide(tmp_path / "panel")

    with pytest.raises(ValueError) as raised:
        register_image_datasource("panel", slide, image_type="brightfield")

    message = str(raised.value)
    assert "3 optical paths" in message
    assert "Auto or Fluorescence" in message


def test_a_single_path_slide_is_refused_in_the_singular(tmp_path):
    slide = write_if_slide(tmp_path / "one", markers=("DAPI",))

    with pytest.raises(ValueError, match="1 optical path,"):
        register_image_datasource("one", slide, image_type="brightfield")


# -- the edit page -------------------------------------------------------


def test_the_edit_page_reports_and_applies_the_choice(tmp_path):
    from plexora.server.routes.project_routes import _describe

    import plexora

    slide = write_he_slide(tmp_path / "he")
    register_image_datasource("he", slide)
    client = plexora.app.test_client()

    described = _describe(Project.find("he"))
    assert described["imageType"] == "brightfield"
    assert described["imageTypeChoice"] is None
    assert described["imageTypeDetected"] == "brightfield"
    assert described["imageTypeReason"]

    assert client.post("/project/he",
                       json={"imageType": "fluorescence"}).status_code == 200

    after = _describe(Project.find("he"))
    assert after["imageType"] == "fluorescence"
    assert after["image"]["kind"] == "dicom"
    assert len(Project.find("he").image.channels) == 3


# -- z-stacks ------------------------------------------------------------


def test_a_z_stack_registers_as_markers_not_planes(tmp_path):
    """Three focal planes and three markers is three layers. Nine would be the
    reader having flattened two orthogonal axes into one."""
    slide = write_if_slide(tmp_path / "z", focal_planes=3)

    entry = register_image_datasource("z", slide)

    assert entry["num_channels"] == 3
    assert len(entry["imageData"]) == 3
    # The one place the count reaches a person: `ImageSpec` has no field for
    # it, and the edit page shows this sentence beside the override.
    assert "3 focal planes" in entry["imageTypeReason"]


def test_the_conversion_reports_the_focal_plane_count(tmp_path):
    """Not persisted -- `ImageSpec` stores neither this nor
    `imageTypeConfidence` -- but reported, because it is what a future Z
    control would read and because the reader had to know it to pin the plane
    it serves.
    """
    from plexora.server.models.data_model import convertOmeTiff

    flat = write_if_slide(tmp_path / "flat")
    stack = write_if_slide(tmp_path / "z", focal_planes=3)

    assert convertOmeTiff(flat)["focalPlanes"] == 1
    assert convertOmeTiff(stack)["focalPlanes"] == 3
