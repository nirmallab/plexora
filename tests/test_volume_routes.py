"""A volume end to end: registered, loaded, and served through the app.

What a project records about a z-stack, what comes down the tile route for
each view, and -- the half that is easy to lose -- that a planar project's
tiles, keys and ETags are exactly what they were.
"""

import io

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from tests import volume_fixtures as vf


def _png16(payload):
    """A `q=hd` tile: 16-bit grey PNG, decoded."""
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(payload)))


@pytest.fixture()
def client():
    return plexora.app.test_client()


def _register_volume(tmp_path, name="vol", **kwargs):
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX",
                                     channels=3, depth=5, levels=2, **kwargs)
    entry = register_image_datasource(name, path)
    return entry, data


def _tile(client, name, channel, level="0", tile="0_0.png", **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/generated/data/{name}/{name}.ome.tif_{channel}/{level}/{tile}"
                      + (f"?{query}" if query else ""))


def _channel_key(entry, index):
    return entry["imageData"][index]["src"].rstrip("/").rsplit("/", 1)[-1]


# -- registration --------------------------------------------------------


def test_registering_a_z_stack_records_a_volume(tmp_path):
    entry, _ = _register_volume(tmp_path)
    assert entry["num_channels"] == 3
    assert len(entry["imageData"]) == 3
    assert entry["imageShape"] == {"t": 1, "c": 3, "z": 5, "y": 96, "x": 80}
    assert entry["imageAxes"] == "czyx"
    assert entry["imageDefaultView"] == "z2"
    assert entry["imageZSemantics"] == "depth"
    # The file states the z step, so nothing is copied into the project.
    assert "pixelSizeZ" not in entry


def test_a_multi_channel_stack_with_no_z_step_registers_as_cycles(tmp_path):
    path, _ = vf.write_ome_volume(tmp_path / "cyc.ome.tif", axes="CZYX", channels=3,
                                  depth=4, physical_z=None)
    entry = register_image_datasource("cyc", path)
    assert entry["num_channels"] == 12
    assert entry["imageZSemantics"] == "cycle"
    assert "imageShape" not in entry


def test_a_planar_image_records_nothing_new(tmp_path):
    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.zeros((3, 64, 64), dtype=np.uint16), ome=True)
    entry = register_image_datasource("flat", path)
    for key in ("imageShape", "imageAxes", "imageDefaultView", "imageZSemantics",
                "imageTSemantics", "pixelSizeZ", "imageViews"):
        assert key not in entry


def test_an_imaris_file_registers(tmp_path):
    path, data = vf.write_ims(tmp_path / "v.ims", channels=2, depth=6,
                              names=["DAPI", "CD8"])
    entry = register_image_datasource("ims", path)
    assert entry["image_kind"] == "ims"
    assert entry["num_channels"] == 2
    assert entry["imageShape"]["z"] == 6
    assert [c["fullname"] for c in entry["imageData"]] == ["DAPI", "CD8"]


# -- serving -------------------------------------------------------------


def test_each_view_serves_its_own_pixels(tmp_path, client):
    entry, data = _register_volume(tmp_path)
    key = _channel_key(entry, 1)
    base = f"/generated/data/vol/{key}/0/0_0.png?q=hd"

    default = _png16(client.get(base).data)
    assert np.array_equal(default, data[0, 1, 2])

    plane = _png16(client.get(base + "&view=z4").data)
    assert np.array_equal(plane, data[0, 1, 4])

    mip = _png16(client.get(base + "&view=z1-3max").data)
    assert np.array_equal(mip, data[0, 1, 1:4].max(0))

    mean = _png16(client.get(base + "&view=zmean").data)
    assert np.array_equal(mean, np.rint(data[0, 1].mean(0)).astype(np.uint16))

    section = _png16(client.get(base + "&view=xzy10").data)
    rows = round(5 * 2.0 / 0.5)
    assert section.shape == (rows, 80)
    assert np.array_equal(section[0], data[0, 1, 0, 10])


def test_a_volume_shares_one_window_across_its_planes(tmp_path, client):
    entry, data = _register_volume(tmp_path)
    data_model.load_datasource("vol")
    name = entry["imageData"][0]["fullname"]
    window = data_model.get_channel_quantization_window(name, "vol")
    assert window == (0.0, float(data[0, 0].max()))
    std = data_model.get_channel_quantization_window(name, "vol", "std")
    assert std == (0.0, float(np.ceil(data[0, 0].max() / 2)))
    # The default WebP tile of every plane is quantized with the same window.
    key = _channel_key(entry, 0)
    a = client.get(f"/generated/data/vol/{key}/0/0_0.png?view=z0")
    b = client.get(f"/generated/data/vol/{key}/0/0_0.png?view=z4")
    assert a.status_code == b.status_code == 200
    assert a.data != b.data


def test_bad_views_are_refused(tmp_path, client):
    entry, _ = _register_volume(tmp_path)
    key = _channel_key(entry, 0)
    for view in ("z9", "zz", "z0-3sum", "xzy96", "t1z0"):
        response = client.get(f"/generated/data/vol/{key}/0/0_0.png?view={view}")
        assert response.status_code == 400, view
        assert response.get_json()["error"]


def test_a_planar_image_refuses_a_view_and_keeps_its_etag(tmp_path, client):
    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.arange(3 * 64 * 64, dtype=np.uint16).reshape(3, 64, 64),
                     ome=True)
    entry = register_image_datasource("flat", path)
    key = _channel_key(entry, 0)
    response = client.get(f"/generated/data/flat/{key}/0/0_0.png?q=hd")
    assert response.status_code == 200
    assert response.headers["ETag"] == \
        f'"{data_model.load_generation}-flat-{key}-0-0_0.png-hd"'
    assert client.get(f"/generated/data/flat/{key}/0/0_0.png?view=z1").status_code == 400


def test_a_views_etag_names_the_view(tmp_path, client):
    entry, _ = _register_volume(tmp_path)
    key = _channel_key(entry, 0)
    plain = client.get(f"/generated/data/vol/{key}/0/0_0.png?q=hd")
    viewed = client.get(f"/generated/data/vol/{key}/0/0_0.png?q=hd&view=z1-3max")
    assert plain.headers["ETag"].endswith('-hd"')
    assert viewed.headers["ETag"].endswith('-hd-z1-3max"')


# -- info and state ------------------------------------------------------


def test_volume_info(tmp_path, client):
    _register_volume(tmp_path)
    info = client.get("/volume/vol/info").get_json()
    assert info["volumetric"] is True
    assert info["depth"] == 5 and info["channels"] == 3
    assert info["spacing"]["z"] == {"value": 2.0, "unit": "µm", "source": "metadata"}
    assert info["anisotropy"] == pytest.approx(4.0)
    assert info["default_view"] == "z2"
    assert [level["width"] for level in info["levels"]] == [80, 40]


def test_volume_info_for_a_planar_image(tmp_path, client):
    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 32, 32), dtype=np.uint16), ome=True)
    register_image_datasource("flat", path)
    assert client.get("/volume/flat/info").get_json()["volumetric"] is False


def test_the_edit_page_reads_planes_on_z_either_way(tmp_path, client):
    """No z step stated: a multi-channel stack imports as cycles (3 x 5 = 15
    channels). The edit page offers the other reading, and choosing it re-reads
    the same file as 3 channels over 5 planes -- and back again."""
    from plexora.server.models.project import Project

    _register_volume(tmp_path, physical_z=None, name="noz")
    project = Project.find("noz")
    assert project.image.z_semantics == "cycle"
    assert project.image.num_channels == 15
    page = client.get("/edit_config/noz").get_data(as_text=True)
    assert 'id="edit_z_semantics"' in page and 'data-stored="cycle"' in page

    assert client.post("/project/noz", json={"zSemantics": "depth"}).status_code == 200
    project = Project.find("noz")
    assert project.image.z_semantics == "depth"
    assert project.image.num_channels == 3 and project.image.depth == 5
    assert client.get("/volume/noz/info").get_json()["depth"] == 5

    assert client.post("/project/noz", json={"zSemantics": "cycle"}).status_code == 200
    project = Project.find("noz")
    assert project.image.z_semantics == "cycle" and project.image.num_channels == 15


def test_a_flat_image_offers_no_planes_choice(tmp_path, client):
    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.zeros((3, 32, 32), dtype=np.uint16), ome=True)
    register_image_datasource("flat", path)
    page = client.get("/edit_config/flat").get_data(as_text=True)
    assert "edit_z_semantics" not in page
    # And a stray value changes nothing.
    assert client.post("/project/flat", json={"zSemantics": "depth"}).status_code == 200


def test_setting_the_z_step(tmp_path, client):
    _register_volume(tmp_path)
    response = client.post("/set_pixel_size", json={"datasource": "vol", "value": 1.5,
                                                     "axis": "z"})
    assert response.get_json()["pixel_size"]["value"] == 1.5
    info = client.get("/volume/vol/info").get_json()
    assert info["spacing"]["z"]["source"] == "manual"
    assert info["spacing"]["z"]["value"] == 1.5
    metadata = client.get("/get_ome_metadata?datasource=vol").get_json()
    assert metadata["physical_size_z"] == 1.5
    assert metadata["pixel_size_z_source"] == "manual"


def test_view_state_round_trips_and_is_clamped(tmp_path, client):
    _register_volume(tmp_path)
    assert client.get("/view_state/vol").get_json() == {}
    put = client.put("/view_state/vol", json={
        "z": 99, "mode": "slab", "slab": {"z0": 4, "z1": 1, "op": "mean"},
        "layout": "nonsense"})
    stored = put.get_json()["state"]
    assert stored["z"] == 4
    assert stored["slab"]["z0"] == 1 and stored["slab"]["z1"] == 4
    assert stored["layout"] == "single"
    assert client.get("/view_state/vol").get_json()["mode"] == "slab"
    assert client.put("/view_state/vol", data="nope",
                      content_type="application/json").status_code == 400


# -- whole levels and section geometry -------------------------------------


def test_a_whole_level_comes_back_as_quantized_voxels(tmp_path, client):
    entry, data = _register_volume(tmp_path)
    data_model.load_datasource("vol")
    name = entry["imageData"][1]["fullname"]
    response = client.get(f"/volume/vol/level?channel={name}&level=1")
    assert response.status_code == 200
    shape = tuple(int(v) for v in response.headers["X-Plexora-Shape"].split(","))
    assert shape == (5, 48, 40)
    voxels = np.frombuffer(response.data, dtype=np.uint8).reshape(shape)
    qmin, qmax = (float(v) for v in response.headers["X-Plexora-Window"].split(","))
    expected = np.clip(np.rint((data[0, 1, :, ::2, ::2].astype(np.float32) - qmin)
                               / (qmax - qmin) * 255), 0, 255).astype(np.uint8)
    assert np.array_equal(voxels, expected)


def test_auto_picks_the_finest_level_under_the_budget(tmp_path, client):
    entry, _ = _register_volume(tmp_path)
    name = entry["imageData"][0]["fullname"]
    full = client.get(f"/volume/vol/level?channel={name}")
    assert full.headers["X-Plexora-Level"] == "0"
    # 5 x 96 x 80 = 38400 voxels: a 1 MiB floor still fits level 0, so ask
    # for less texture side instead.
    small = client.get(f"/volume/vol/level?channel={name}&max_side=60")
    assert small.headers["X-Plexora-Level"] == "1"


def test_a_level_of_a_planar_image_is_refused(tmp_path, client):
    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 32, 32), dtype=np.uint16), ome=True)
    entry = register_image_datasource("flat", path)
    name = entry["imageData"][0]["fullname"]
    assert client.get(f"/volume/flat/level?channel={name}").status_code == 400


def test_section_geometry(tmp_path, client):
    _register_volume(tmp_path)
    geometry = client.get("/volume/vol/section?view=xzy10").get_json()
    # 5 planes 2.0 um apart over 0.5 um pixels: 20 rows at level 0.
    assert geometry["height"] == 20 and geometry["width"] == 80
    assert geometry["level_shapes"] == [[20, 80], [10, 40]]
    assert client.get("/volume/vol/section?view=z1").status_code == 400
