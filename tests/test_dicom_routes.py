"""A DICOM slide, end to end through the app.

The claim being tested is a negative one: that nothing on the serving side
needed to learn about DICOM. Tiles, overviews, the scale bar and quick view are
all the routes that were already there, and they answer for a slide assembled
out of 252 files exactly as they answer for a TIFF.
"""

import io
import json

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("wsidicom")
pytest.importorskip("pydicom")

import plexora  # noqa: E402
from plexora.datasource import (  # noqa: E402
    _sniff_quick_view_kind,
    register_image_datasource,
)
from tests.dicom_fixtures import (  # noqa: E402
    write_he_slide,
    write_if_slide,
    write_two_slides,
)


def _decode(payload):
    return np.asarray(Image.open(io.BytesIO(payload)))


# -- quick view ----------------------------------------------------------


def test_quick_view_accepts_a_slide_folder(tmp_path):
    """A folder is the normal way to arrive at a DICOM slide, because a slide
    IS a folder of instances -- the second directory Plexora opens as an image,
    after a `.zarr` store."""
    slide = write_if_slide(tmp_path / "panel")

    assert _sniff_quick_view_kind(slide) == "ome_tiff"


def test_quick_view_accepts_a_single_instance(tmp_path):
    write_if_slide(tmp_path / "panel")
    one = sorted((tmp_path / "panel").glob("*.dcm"))[0]

    assert _sniff_quick_view_kind(one) == "ome_tiff"


def test_a_folder_of_two_slides_is_refused_while_choosing(tmp_path):
    """Not half way through an import. The sniffer assembles the slide for
    exactly this reason: the failure a user can act on is the one that arrives
    while the file dialog is still open."""
    folder, _first, _second = write_two_slides(tmp_path / "box")

    with pytest.raises(ValueError, match="2 slides"):
        _sniff_quick_view_kind(folder)


def test_a_plain_folder_is_still_refused(tmp_path):
    (tmp_path / "notes.txt").write_text("nothing", encoding="utf-8")

    with pytest.raises(ValueError, match="not an image"):
        _sniff_quick_view_kind(tmp_path)


def test_quick_view_registers_a_multiplex_slide(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    write_if_slide(tmp_path / "panel")
    client = plexora.app.test_client()

    answer = client.post("/quick_view",
                         json={"path": str(tmp_path / "panel")}).get_json()

    assert answer["success"] is True
    assert answer["name"] == "panel"
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["panel"]["image_kind"] == "dicom"
    assert [c["fullname"] for c in config["panel"]["imageData"]] == \
        ["DNA", "CD3", "Ki67"]


# -- tiles ---------------------------------------------------------------


def test_each_marker_serves_its_own_tile(tmp_path):
    """Independent per-channel reads, which is what makes toggling one marker
    in the viewer cost one marker's tiles. If `path=` were ignored, all three
    responses would be byte-identical."""
    slide = write_if_slide(tmp_path / "panel")
    register_image_datasource("panel", slide)
    client = plexora.app.test_client()

    tiles = []
    for index in range(3):
        response = client.get(f"/generated/data/panel/panel_{index}/0/0_0.png")
        assert response.status_code == 200
        tiles.append(response.data)

    assert len(set(tiles)) == 3


def test_a_channel_tile_is_the_pixels_the_fixture_wrote(tmp_path):
    """`q=hd` means "the bytes as they are" on every other path and has to mean
    the same here, so the lossy default is a choice rather than the only thing
    on offer."""
    slide = write_if_slide(tmp_path / "panel")
    register_image_datasource("panel", slide)
    client = plexora.app.test_client()

    response = client.get("/generated/data/panel/panel_1/0/0_0.png?q=hd")

    assert response.status_code == 200
    tile = _decode(response.data)
    # The virtual grid is ceil(size / 1024), so (0, 0) is the only tile and it
    # comes back at the slide's own size rather than padded to the cell.
    assert tile.shape[:2] == (256, 320)
    assert int(np.asarray(tile).max()) == 32767


def test_an_he_slide_serves_one_colour_tile(tmp_path):
    """The `rgb` sentinel channel, and the reason a DICOM H&E project records
    `image_kind='brightfield'`: this route is the one that was already there."""
    slide = write_he_slide(tmp_path / "he")
    register_image_datasource("he", slide)
    client = plexora.app.test_client()

    response = client.get("/generated/data/he/rgb/0/0_0.png")

    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    tile = _decode(response.data)[..., :3]
    assert tile.shape == (256, 320, 3)
    # A picture of a stained section, not three copies of one plane composited
    # on black.
    assert tile[:, :, 0].mean() != pytest.approx(tile[:, :, 2].mean(), abs=1.0)


def test_the_overview_is_served(tmp_path):
    slide = write_if_slide(tmp_path / "panel")
    register_image_datasource("panel", slide)
    client = plexora.app.test_client()

    response = client.get("/generated/overview/panel/CD3")

    assert response.status_code == 200
    assert max(_decode(response.data).shape[:2]) <= 400


def test_the_scale_bar_gets_its_pixel_size(tmp_path):
    """DICOM records millimetres per pixel; the scale bar wants micrometres.
    Out by a thousand is the kind of wrong that looks plausible on screen."""
    slide = write_if_slide(tmp_path / "panel")
    register_image_datasource("panel", slide)
    client = plexora.app.test_client()

    metadata = client.get("/get_ome_metadata?datasource=panel").get_json()

    assert metadata["physical_size_x"] == pytest.approx(0.5)
    assert metadata["physical_size_x_unit"] == "µm"


# -- geometry, for a node ------------------------------------------------


def test_a_slide_still_opens_when_its_derived_levels_are_gone(tmp_path):
    """People clear out project directories. Refusing to open the image would
    be no viewer; opening it with whatever levels the slide itself can serve is
    a worse viewer only for a slide that needed derived ones -- and this one,
    like almost every DICOM slide, does not."""
    from plexora.server.providers.local import LocalImageProvider

    slide = write_if_slide(tmp_path / "panel")
    provider = LocalImageProvider(str(slide),
                                  pyramid=str(tmp_path / "gone.zarr"))

    channels, overview, metadata = provider.open()

    assert channels["0"].shape == (3, 256, 320)
    assert overview.shape[0] == 3
    assert metadata["physical_size_x"] == pytest.approx(0.5)
    channels.close()


def test_a_node_can_size_a_slide_without_a_project(tmp_path):
    """The node path: dispatch is on the path, because a node has no project to
    have recorded anything. Getting the order wrong here would hand a DICOM
    slide to OpenSlide, which reads it and flattens it to RGB."""
    from plexora.server.providers.local import image_geometry

    slide = write_if_slide(tmp_path / "panel")

    assert image_geometry(slide) == {
        "levels": 1,
        "num_channels": 3,
        "height": 256,
        "width": 320,
        "tile_height": 1024,
        "tile_width": 1024,
        "level_shapes": [[256, 320]],
    }


# -- the missing extra ---------------------------------------------------


def test_a_missing_wsidicom_is_reported_as_an_install_line(tmp_path):
    """Whether the extra is installed here decides which half of this runs;
    both are the same promise. A DICOM slide in an environment without the
    reader has to say `pip install 'plexora[wsi]'` while the user is still
    choosing a file, not raise `No module named 'wsidicom'` at them.
    """
    from plexora.server.utils import dicom_wsi

    path = tmp_path / "slide.dcm"
    path.write_bytes(b"")
    try:
        import wsidicom  # noqa: F401
    except ImportError:
        with pytest.raises(dicom_wsi.DicomSupportMissing) as raised:
            _sniff_quick_view_kind(path)
        assert "plexora[wsi]" in str(raised.value)
        client = plexora.app.test_client()
        answer = client.post("/quick_view", json={"path": str(path)})
        assert answer.status_code == 400
        assert "plexora[wsi]" in answer.get_json()["error"]
    else:
        # Installed: the probe reads the (empty) file and fails on its
        # contents, which is a different message and not this test's business.
        with pytest.raises(ValueError, match="not a DICOM whole-slide image"):
            _sniff_quick_view_kind(path)
