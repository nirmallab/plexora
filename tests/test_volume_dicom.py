"""A DICOM slide's focal planes, served as a z-stack.

The 2-D reader keeps the middle focal plane; the volume layer serves every
one of them from the same open handle. What has to hold: each plane is its
own pixels (the fixture adds 100 per plane, so a wrong plane is a wrong
value), the default plane is byte-for-byte what the 2-D reader served, the z
step comes from the file, and a slide with one plane stays a flat image.
"""

import io

import numpy as np
import pytest

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.utils import dicom_wsi
from plexora.server.volume.sources import open_volume
from plexora.server.volume.sources import dicom as dicom_volume
from tests.dicom_fixtures import write_he_slide, write_if_slide

STEP = 100


@pytest.fixture()
def client():
    return plexora.app.test_client()


def _slide(tmp_path, planes=5, **kwargs):
    return write_if_slide(tmp_path / "z", focal_planes=planes, plane_step=STEP, **kwargs)


def _hd(response):
    from PIL import Image

    assert response.status_code == 200, response.get_data(as_text=True)
    return np.asarray(Image.open(io.BytesIO(response.data)))


def test_each_focal_plane_is_its_own_pixels(tmp_path):
    source = open_volume(str(_slide(tmp_path)))
    try:
        assert type(source).__name__ == "DicomVolumeSource"
        volume = source.volume
        assert (volume.depth, volume.channels) == (5, 3)
        assert volume.channel_names == ("DNA", "CD3", "Ki67")
        assert volume.default_view.token() == "z2"
        base = source.read_plane(0, 0, 0, 1, slice(0, 16), slice(0, 16)).astype(int)
        for z in range(1, 5):
            plane = source.read_plane(0, 0, z, 1, slice(0, 16), slice(0, 16)).astype(int)
            np.testing.assert_array_equal(plane - base, z * STEP)
        # The default plane IS the 2-D reader's pyramid, not a copy of it.
        assert source.plane_pyramid(0, 2) is source._pyramid
    finally:
        source.close()


def test_the_z_step_is_the_distance_between_focal_planes(tmp_path):
    source = open_volume(str(_slide(tmp_path)))
    try:
        z = source.volume.spacing.z
        assert (z.value, z.unit, z.source) == (1.0, "µm", "metadata")
        assert source.volume.spacing.x.value == pytest.approx(0.5)
    finally:
        source.close()


def test_uneven_focal_planes_state_no_step():
    step, provenance = dicom_volume._z_spacing([0.0, 1.0, 5.0])
    assert step.value is None
    assert "not evenly spaced" in provenance["note"]


def test_one_focal_plane_stays_a_flat_image(tmp_path):
    assert open_volume(str(write_if_slide(tmp_path / "flat"))) is None
    entry = register_image_datasource("flat", str(write_if_slide(tmp_path / "flat2")))
    assert "imageShape" not in entry


def test_registering_records_the_stack_and_tiles_follow_the_view(tmp_path, client):
    entry = register_image_datasource("dz", str(_slide(tmp_path, height=600, width=700)))
    assert entry["num_channels"] == 3
    assert entry["imageShape"] == {"t": 1, "c": 3, "z": 5, "y": 600, "x": 700}
    assert entry["imageDefaultView"] == "z2"
    assert entry["imageZSemantics"] == "depth"
    info = client.get("/volume/dz/info").get_json()
    assert info["volumetric"] is True and info["depth"] == 5
    src = entry["imageData"][1]["src"]
    planes = {view: _hd(client.get(f"{src}0/0_0.png?q=hd"
                                   + (f"&view={view}" if view else "")))
              for view in ("", "z0", "z2", "z4", "z0-4max", "z0-4mean")}
    np.testing.assert_array_equal(planes[""], planes["z2"])
    np.testing.assert_array_equal(planes["z4"].astype(int) - planes["z0"], 4 * STEP)
    np.testing.assert_array_equal(planes["z0-4max"], planes["z4"])
    np.testing.assert_array_equal(planes["z0-4mean"], planes["z2"])


def test_another_plane_never_reads_the_default_planes_derived_levels(tmp_path):
    """Levels the default plane reads from its extension store are resampled
    from the other plane's own pixels, never borrowed from the store."""
    import zarr

    slide = _slide(tmp_path)
    store = dicom_wsi.extension_path(tmp_path / "project")
    store.parent.mkdir(parents=True, exist_ok=True)
    group = zarr.open_group(str(store), mode="w")
    derived = group.create_array("1", shape=(3, 128, 160), dtype="uint16",
                                 chunks=(1, 128, 160))
    derived[:] = 4321
    pyramid = dicom_wsi.open_image(slide, extension=str(store))
    try:
        assert pyramid.base_levels == 1 and len(pyramid) == 2
        assert int(pyramid["1"][1, 0:8, 0:8].max()) == 4321
        other = dicom_wsi.at_focal_plane(pyramid, pyramid.focal_planes[0])
        assert other.level_shapes == pyramid.level_shapes
        coarse = other["1"][1, 0:8, 0:8]
        assert coarse.max() < 4321
        own = np.asarray(other["0"][1, 0:16, 0:16], dtype=np.float64)
        assert coarse.mean() == pytest.approx(own.mean(), rel=0.2)
        other.close()
        # The copy shares the handle; closing it leaves the default readable.
        assert pyramid["0"][0, 0:2, 0:2].shape == (2, 2)
    finally:
        pyramid.close()


def test_a_colour_slides_focal_planes_are_a_colour_stack(tmp_path, client):
    """A brightfield slide's focal planes are served one at a time, each the
    RGB picture recorded at that depth; nothing combines or cuts them."""
    from PIL import Image

    slide = write_he_slide(tmp_path / "he", focal_planes=3, plane_step=40)
    entry = register_image_datasource("hez", str(slide))
    assert entry["image_kind"] == "brightfield"
    assert entry["imageShape"]["z"] == 3 and entry["imageDefaultView"] == "z1"
    info = client.get("/volume/hez/info").get_json()
    assert info["volumetric"] is True and info["color"] is True
    src = next(item["src"] for item in entry["imageData"] if item["src"].rstrip("/").endswith("rgb"))

    def plane(view):
        # HD: lossless, so the planes compare exactly.
        response = client.get(f"{src}0/0_0.png?q=hd" + (f"&view={view}" if view else ""))
        assert response.status_code == 200, response.get_data(as_text=True)
        return np.asarray(Image.open(io.BytesIO(response.data)).convert("RGB")).astype(int)

    default, top, bottom = plane(""), plane("z0"), plane("z2")
    np.testing.assert_array_equal(default, plane("z1"))
    # Plane p is the picture plus p * 40, clipped at white.
    np.testing.assert_array_equal(bottom, np.clip(top + 80, 0, 255)[..., :3])
    for view in ("z0-2max", "xzy10"):
        refused = client.get(f"{src}0/0_0.png?view={view}")
        assert refused.status_code == 400 and b"one at a time" in refused.data, view


def test_a_colour_slide_read_as_fluorescence_is_three_channels_through_z(tmp_path):
    slide = write_he_slide(tmp_path / "he3", focal_planes=3, plane_step=40)
    source = open_volume(str(slide), rgb=False)
    try:
        assert source.volume.color is False and source.volume.channels == 3
    finally:
        source.close()
