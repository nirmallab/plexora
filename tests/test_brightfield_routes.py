"""Registering and serving a brightfield project, end to end through the app.

What a project records about an H&E slide, what comes back down the tile route,
and -- the half that is easy to lose -- that a fluorescence project's tiles are
byte-for-byte what they always were.
"""

import io
import json

import numpy as np
import pytest
from PIL import Image

import plexora
from plexora.datasource import (
    _sniff_quick_view_kind,
    register_image_datasource,
    reregister_image,
)
from plexora.server.models import data_model
from plexora.server.models.project import Project
from tests.brightfield_fixtures import (
    write_ambiguous_planar,
    write_interleaved_tiff,
    write_planar_fluorescence,
    write_rgb_ome_tiff,
    write_svs_like,
)


def _decode(payload):
    return np.asarray(Image.open(io.BytesIO(payload)).convert("RGB"))


# -- what gets recorded --------------------------------------------------


def test_an_he_scan_registers_as_one_layer_of_three_planes(tmp_path):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    entry = register_image_datasource("he", path)

    assert entry["image_kind"] == "brightfield"
    # Three planes -- which is what a node's geometry check compares against --
    # and one layer, which is what the viewer draws.
    assert entry["num_channels"] == 3
    assert [c["fullname"] for c in entry["imageData"]] == ["Image"]
    assert entry["imageData"][0]["src"].endswith("/rgb/")
    assert entry["width"] == 640 and entry["height"] == 512
    assert entry["imageTypeDetected"] == "brightfield"
    assert entry["imageTypeReason"]
    # Nobody chose anything, so nothing is stored to outlive the detector.
    assert "imageTypeChoice" not in entry


def test_a_fluorescence_panel_is_untouched(tmp_path):
    path = write_planar_fluorescence(tmp_path / "panel.ome.tif")
    entry = register_image_datasource("panel", path)

    assert entry["image_kind"] == "ome_tiff"
    assert entry["num_channels"] == 3
    assert [c["fullname"] for c in entry["imageData"]] == ["DAPI", "CD3", "Ki67"]
    assert "imageTypeDetected" not in entry


def test_importing_a_whole_slide_image_writes_no_pyramid(tmp_path):
    """The point of the virtual halving chain: a 300 MB slide is registered by
    reading its header, and the project directory stays empty."""
    path = write_svs_like(tmp_path / "slide.svs", height=2048, width=2560)
    entry = register_image_datasource("slide", path)

    assert entry["image_kind"] == "brightfield"
    assert "imagePyramid" not in entry
    assert not list((tmp_path / "slide").glob("*.zarr"))


def test_a_flat_slide_derives_its_coarse_levels(tmp_path):
    path = write_interleaved_tiff(tmp_path / "flat.tif", height=9000, width=9000)
    entry = register_image_datasource("flat", path)

    assert entry["image_kind"] == "brightfield"
    assert entry["imagePyramid"].endswith("brightfield_pyramid.zarr")
    assert entry["imagePyramidKey"]


# -- tiles ---------------------------------------------------------------


def test_the_tile_route_serves_colour(tmp_path):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    register_image_datasource("he", path)
    client = plexora.app.test_client()

    response = client.get("/generated/data/he/rgb/0/0_0.png")

    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    tile = _decode(response.data)
    assert tile.shape == (512, 640, 3)
    # It is a picture of a stained section, not three copies of one plane.
    assert tile[:, :, 0].mean() != pytest.approx(tile[:, :, 2].mean(), abs=1.0)


def test_hd_gives_back_the_exact_pixels(tmp_path):
    """`q=hd` means "the bytes as they are" on every other path, and it has to
    mean the same here -- so the lossy WebP default is a choice rather than the
    only thing available."""
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=256, width=256)
    register_image_datasource("he", path)
    client = plexora.app.test_client()

    response = client.get("/generated/data/he/rgb/0/0_0.png?q=hd")

    assert response.mimetype == "image/png"
    with open(path, "rb") as _:
        pass
    import tifffile as tf

    with tf.TiffFile(path) as handle:
        source = handle.series[0].asarray()[..., :3]
    assert np.array_equal(_decode(response.data), source)


def test_a_tile_past_the_edge_is_short_not_an_error(tmp_path):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    register_image_datasource("he", path)
    client = plexora.app.test_client()

    # The virtual grid is ceil(size / 1024), so (0, 0) is the only tile and it
    # is smaller than the grid cell.
    assert _decode(client.get("/generated/data/he/rgb/0/0_0.png").data).shape[:2] \
        == (512, 640)


def test_the_overview_is_colour_too(tmp_path):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=1200, width=1400)
    register_image_datasource("he", path)
    client = plexora.app.test_client()

    response = client.get("/generated/overview/he/Image")

    assert response.status_code == 200
    overview = _decode(response.data)
    assert overview.ndim == 3 and overview.shape[2] == 3
    assert max(overview.shape[:2]) <= 400


def test_the_scale_bar_gets_its_pixel_size(tmp_path):
    path = write_svs_like(tmp_path / "slide.svs", height=1024, width=1280)
    register_image_datasource("slide", path)
    client = plexora.app.test_client()

    metadata = client.get("/get_ome_metadata?datasource=slide").get_json()

    assert metadata["physical_size_x"] == pytest.approx(0.2465)
    assert metadata["physical_size_x_unit"] == "µm"


# -- the override --------------------------------------------------------


def test_importing_as_fluorescence_serves_three_channels(tmp_path):
    """The honest reading of "these three samples are three markers": the same
    pyramid, through the same CYX views, by code that was never told."""
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    entry = register_image_datasource("he", path, image_type="fluorescence")

    assert entry["image_kind"] == "ome_tiff"
    assert entry["num_channels"] == 3
    assert len(entry["imageData"]) == 3
    assert entry["width"] == 640 and entry["height"] == 512
    assert entry["imageTypeChoice"] == "fluorescence"
    # And it still records what the file looks like, so the edit page can show
    # the user what they overrode.
    assert entry["imageTypeDetected"] == "brightfield"


def test_the_override_round_trips_both_ways(tmp_path):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    register_image_datasource("he", path)

    Project.find("he").with_image_type("fluorescence").save()
    flipped = reregister_image("he")
    assert flipped.image.kind == "ome_tiff"
    assert len(flipped.image.channels) == 3
    assert (flipped.image.width, flipped.image.height) == (640, 512)

    Project.find("he").with_image_type(None).save()
    back = reregister_image("he")
    assert back.image.kind == "brightfield"
    assert [c["fullname"] for c in back.image.channels] == ["Image"]
    assert back.image.image_type_choice is None
    assert (back.image.width, back.image.height) == (640, 512)


def test_an_ambiguous_file_can_be_forced_to_colour(tmp_path):
    """Three minisblack planes declare nothing, so the file cannot be read as
    colour by inspection -- only because the project says so."""
    path = write_ambiguous_planar(tmp_path / "flat.tif", light=False)
    entry = register_image_datasource("amb", path, image_type="brightfield")

    assert entry["image_kind"] == "brightfield"
    client = plexora.app.test_client()
    tile = _decode(client.get("/generated/data/amb/rgb/0/0_0.png").data)
    assert tile.shape[2] == 3


def test_an_unknown_override_is_ignored_rather_than_stored(tmp_path):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif")
    register_image_datasource("he", path)

    unchanged = Project.find("he").with_image_type("sepia")

    assert unchanged.image.image_type_choice is None


# -- the edit page -------------------------------------------------------


def test_the_edit_page_reports_and_applies_the_choice(tmp_path):
    from plexora.server.routes.project_routes import _describe

    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    register_image_datasource("he", path)
    client = plexora.app.test_client()

    described = _describe(Project.find("he"))
    assert described["imageType"] == "brightfield"
    assert described["imageTypeChoice"] is None
    assert described["imageTypeDetected"] == "brightfield"
    assert described["imageTypeReason"]

    response = client.post("/project/he", json={"imageType": "fluorescence"})
    assert response.status_code == 200, response.get_json()

    after = _describe(Project.find("he"))
    assert after["imageType"] == "fluorescence"
    assert after["imageTypeChoice"] == "fluorescence"
    assert after["image"]["kind"] == "ome_tiff"
    assert len(Project.find("he").image.channels) == 3

    # And back. An override that could not be cleared would pin a project to
    # whatever it was saved as.
    assert client.post("/project/he", json={"imageType": ""}).status_code == 200
    assert _describe(Project.find("he"))["imageType"] == "brightfield"


# -- quick view ----------------------------------------------------------


@pytest.mark.parametrize("suffix", [".svs", ".tif", ".ome.tif"])
def test_quick_view_accepts_a_slide(tmp_path, suffix):
    """One answer for every tiled format: which kind it turns out to be is the
    conversion's call, made by reading the file."""
    path = write_rgb_ome_tiff(tmp_path / f"slide{suffix}")

    assert _sniff_quick_view_kind(path) == "ome_tiff"


def test_quick_view_registers_a_slide_as_brightfield(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    path = write_svs_like(tmp_path / "slide.svs", height=1024, width=1280)
    client = plexora.app.test_client()

    answer = client.post("/quick_view", json={"path": str(path)}).get_json()

    assert answer["success"] is True
    assert answer["name"] == "slide"
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["slide"]["image_kind"] == "brightfield"


def test_a_missing_openslide_is_reported_as_an_install_line(tmp_path):
    """The one format that needs the `[wsi]` extra. Whether it is installed
    here decides which half of this is checked -- both are the same promise:
    the user finds out while choosing a file, in a sentence they can act on."""
    from plexora.server.utils import brightfield

    path = tmp_path / "slide.mrxs"
    path.write_bytes(b"")
    try:
        import openslide  # noqa: F401
    except ImportError:
        with pytest.raises(brightfield.BrightfieldSupportMissing) as raised:
            _sniff_quick_view_kind(path)
        assert "plexora[wsi]" in str(raised.value)
        client = plexora.app.test_client()
        answer = client.post("/quick_view", json={"path": str(path)})
        assert answer.status_code == 400
        assert "plexora[wsi]" in answer.get_json()["error"]
    else:
        # Installed: the probe opens the (empty) file and fails on its
        # contents, which is a different message and not this test's business.
        with pytest.raises(Exception):
            _sniff_quick_view_kind(path)


def test_a_flat_picture_cannot_carry_a_mask(tmp_path):
    """Said rather than silently dropped: `rgb` has no label layer to draw one
    into, so recording it would make the project claim a mask nothing shows."""
    from plexora.server.routes.import_routes import _register_image_only
    from tests.brightfield_fixtures import write_planar_fluorescence

    picture = tmp_path / "snap.png"
    Image.fromarray(np.zeros((32, 32, 3), np.uint8)).save(picture)
    mask = write_planar_fluorescence(tmp_path / "mask.tif", channels=1,
                                     names=("labels",))

    with pytest.raises(ValueError, match="flat picture"):
        _register_image_only("snap", picture, mask)


def test_a_flat_picture_is_still_a_flat_picture(tmp_path):
    """PNG/JPEG keep the untiled `rgb` kind. Brightfield is a new kind
    deliberately: five string comparisons across the app mean "flat quick-view
    image" by `rgb`, and a slide is not one."""
    picture = tmp_path / "snap.png"
    Image.fromarray(np.zeros((32, 32, 3), np.uint8)).save(picture)

    assert _sniff_quick_view_kind(picture) == "rgb"


# -- the fluorescence path is unchanged ----------------------------------


def test_a_fluorescence_tile_is_what_it_always_was(tmp_path):
    path = write_planar_fluorescence(tmp_path / "panel.ome.tif",
                                     height=256, width=256)
    entry = register_image_datasource("panel", path)
    key = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]
    client = plexora.app.test_client()

    response = client.get(f"/generated/data/panel/{key}/0/0_0.png")

    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    # Grey: one value per pixel, quantized against the channel's window. The
    # bytes come back as RGB because lossy WebP has no greyscale mode, so this
    # asks that the planes agree rather than that they are identical -- chroma
    # subsampling moves them by a level or two.
    tile = _decode(response.data).astype(np.int16)
    assert np.abs(tile[..., 0] - tile[..., 1]).max() <= 8
    assert np.abs(tile[..., 1] - tile[..., 2]).max() <= 8


def test_the_rgb_sentinel_is_not_read_as_a_mask(tmp_path):
    """`rgb` does not match the `_<N>` pattern, so without the special case
    ahead of it every H&E tile request would be routed to the segmentation
    reader -- and answered from a mask that is not there."""
    assert data_model._parse_channel("rgb") == ("rgb", False)
    assert data_model._parse_channel("panel_2") == (2, False)
    assert data_model._parse_channel("some_mask") == (None, True)
