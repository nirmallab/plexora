"""TIFF layouts older software writes, opened as the image they hold.

Regression tests for the legacy-format changes in `tiff_series`,
`tiff_region`, `brightfield`, `xenium_focus`, `volume/sources/ims.py` and
`providers/local.py`:

- several same-size OME Images in one file (a bfconvert of a multi-scene
  file) open as the FIRST image's channels, pyramid kept;
- a multi-file OME-TIFF (one file per channel, `TiffData/UUID@FileName`)
  opens as one image from the file that holds channel 0, and only then;
- an Imaris 3/4 "classic" `.ims` (a TIFF behind a thumbnail) opens as its
  channels, not as the thumbnail and not through HDF5;
- `tiff_series.arrange_series` puts the image first;
- a whole-slide suffix holding grey channel pages is fluorescence;
- a pyramid that steps by 4 is served on a halving chain;
- channels stored as interleaved samples open as channels, and 16-bit
  colour opens as 8-bit colour.

Fixtures are synthetic and small, with known pixels, so every read is
compared against the array that was written.
"""

import io
from types import SimpleNamespace

import numpy as np
import pytest
import tifffile
from PIL import Image as PILImage

import plexora
from plexora.server.models import data_model
from plexora.server.providers.local import LocalImageProvider, image_geometry
from plexora.server.utils import brightfield, tiff_series, xenium_focus
from plexora.server.volume.sources import ims as ims_source
from tests.helpers import use_data_root

NS = "http://www.openmicroscopy.org/Schemas/OME/2016-06"


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = use_data_root(monkeypatch, tmp_path / "root")
    (root / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    return plexora.app.test_client(), source


def _register(client, path, name):
    inspected = client.post("/import/inspect", json={"paths": [str(path)]})
    assert inspected.status_code == 200, inspected.data[:400]
    response = client.post("/import/sample", json={"paths": [str(path)], "name": name})
    assert response.status_code == 200, response.data[:400]
    assert client.get(f"/init_database?datasource={name}").status_code == 200
    names = client.get(f"/get_channel_names?datasource={name}").get_json()
    entry = client.get("/config").get_json()[name]
    return inspected.get_json(), names, entry


def _tile(client, src, level, x, y):
    response = client.get(f"{src}{level}/{x}_{y}.png?q=hd")
    assert response.status_code == 200, response.data[:300]
    return np.asarray(PILImage.open(io.BytesIO(response.data)))


def _panel(channels=3, height=600, width=700, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(1, 60000, size=(channels, height, width), dtype=np.uint16)


def _smooth(channels=3, height=2048, width=2304):
    """Smooth content, so a 2x reduction is well defined and comparable."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    planes = []
    for c in range(channels):
        plane = (20000 + 15000 * np.sin(yy / (90.0 + 17 * c)) * np.cos(xx / (130.0 + 11 * c))
                 + 5 * xx + 3 * yy)
        planes.append(plane)
    return np.clip(np.stack(planes), 0, 65535).astype(np.uint16)


def _open(path):
    channels, overview, metadata = LocalImageProvider(str(path)).open()
    return channels, overview, metadata


def _read(channels, channel, level, x, y, size):
    return np.asarray(data_model.read_tile(channels, channel, level, f"{x}_{y}", size, size))


def _pyramid_len(channels):
    return 1 if hasattr(channels, "shape") else len(channels)


# -- 4. several OME Images of one size in one file -------------------------


def _scenes_xml(scenes=2, channels=3, height=600, width=700):
    images = []
    for s in range(scenes):
        names = "".join(f'<Channel ID="Channel:{s}:{c}" Name="S{s}C{c}" SamplesPerPixel="1"/>'
                        for c in range(channels))
        images.append(
            f'<Image ID="Image:{s}" Name="Scene #{s}"><Pixels ID="Pixels:{s}" '
            f'DimensionOrder="XYCZT" Type="uint16" SizeX="{width}" SizeY="{height}" '
            f'SizeC="{channels}" SizeZ="1" SizeT="1" PhysicalSizeX="0.5" PhysicalSizeY="0.5">'
            f'{names}<TiffData IFD="{s * channels}" PlaneCount="{channels}"/></Pixels></Image>')
    return f'<OME xmlns="{NS}">' + "".join(images) + "</OME>"


def _write_scenes(path, data, xml, pyramid):
    with tifffile.TiffWriter(path, bigtiff=True) as writer:
        writer.write(data, description=xml.encode("utf-8"), metadata=None,
                     photometric="minisblack", tile=(128, 128),
                     subifds=1 if pyramid else 0)
        if pyramid:
            writer.write(data[:, ::2, ::2], subfiletype=1, metadata=None,
                         photometric="minisblack", tile=(128, 128))


@pytest.mark.parametrize("pyramid", [False, True])
def test_two_scenes_of_one_size_open_as_the_first_scenes_channels(app, pyramid):
    client, source = app
    data = _panel(channels=6)
    path = source / "scenes.ome.tif"
    _write_scenes(path, data, _scenes_xml(), pyramid)

    with tiff_series.ImageTiff(str(path)) as handle:
        series = tiff_series.channel_series(handle)
        assert series.shape == (3, 600, 700)
        assert len(series.levels) == (2 if pyramid else 1)

    channels, overview, _ = _open(path)
    assert _pyramid_len(channels) == (2 if pyramid else 1)
    assert overview.shape[0] == 3
    for c in range(3):
        assert np.array_equal(_read(channels, c, 0, 1, 1, 256), data[c, 256:512, 256:512])
    if pyramid:
        assert np.array_equal(_read(channels, 2, 1, 0, 0, 128), data[2, ::2, ::2][:128, :128])

    _, names, entry = _register(client, path, "scenes")
    assert entry["num_channels"] == 3
    assert names == ["S0C0", "S0C1", "S0C2"]
    assert entry["maxLevel"] == (2 if pyramid else 1)
    tile = _tile(client, entry["imageData"][2]["src"], 0, 0, 0)
    assert np.array_equal(tile, data[2, :tile.shape[0], :tile.shape[1]])


def test_one_image_of_six_channels_still_opens_six(app):
    client, source = app
    data = _panel(channels=6)
    xml = _scenes_xml(scenes=1, channels=6)
    path = source / "six.ome.tif"
    _write_scenes(path, data, xml, pyramid=True)
    channels, _, _ = _open(path)
    assert _read(channels, 5, 0, 0, 0, 256).shape == (256, 256)
    assert np.array_equal(_read(channels, 5, 0, 0, 0, 256), data[5, :256, :256])
    _, _, entry = _register(client, path, "six")
    assert entry["num_channels"] == 6


def test_scenes_of_different_sizes_are_left_alone(tmp_path):
    # Two Images whose first is not the size of the pages: not a scene list.
    data = _panel(channels=6)
    xml = _scenes_xml().replace('SizeX="700" SizeY="600" SizeC="3"', 'SizeX="70" SizeY="60" SizeC="3"', 1)
    path = tmp_path / "odd.ome.tif"
    _write_scenes(path, data, xml, pyramid=False)
    with tiff_series.ImageTiff(str(path)) as handle:
        assert tiff_series.channel_series(handle).shape == (6, 600, 700)


# -- 5. multi-file OME-TIFF, one file per channel --------------------------


def _ome_set(folder, names=("DAPI", "CD3", "CD8"), *, size_z=1, height=600, width=700,
             missing=(), stem="set"):
    files = [f"{stem}_c{c}.ome.tif" for c in range(len(names))]
    channels = "".join(f'<Channel ID="Channel:0:{c}" Name="{n}" SamplesPerPixel="1"/>'
                       for c, n in enumerate(names))
    if size_z == 1:
        tiffdata = "".join(
            f'<TiffData FirstC="{c}" FirstZ="0" FirstT="0" IFD="0" PlaneCount="1">'
            f'<UUID FileName="{f}">urn:uuid:{c:04d}</UUID></TiffData>'
            for c, f in enumerate(files))
        size_c = len(names)
    else:
        # The planes of a z-stack, one file per plane.
        tiffdata = "".join(
            f'<TiffData FirstC="0" FirstZ="{z}" FirstT="0" IFD="0" PlaneCount="1">'
            f'<UUID FileName="{f}">urn:uuid:{z:04d}</UUID></TiffData>'
            for z, f in enumerate(files))
        size_c = 1
        channels = '<Channel ID="Channel:0:0" Name="DAPI" SamplesPerPixel="1"/>'
    xml = (f'<OME xmlns="{NS}" UUID="urn:uuid:set">'
           f'<Image ID="Image:0" Name="set"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
           f'Type="uint16" SizeX="{width}" SizeY="{height}" SizeC="{size_c}" SizeZ="{size_z}" '
           f'SizeT="1" PhysicalSizeX="0.65" PhysicalSizeY="0.65">{channels}{tiffdata}'
           '</Pixels></Image></OME>')
    planes = _panel(channels=len(files), height=height, width=width, seed=3)
    for index, name in enumerate(files):
        if index in missing:
            continue
        with tifffile.TiffWriter(folder / name, bigtiff=True) as writer:
            writer.write(planes[index], description=xml.encode("utf-8"), metadata=None,
                         photometric="minisblack", tile=(128, 128), subifds=2)
            for step in (2, 4):
                writer.write(planes[index][::step, ::step], subfiletype=1, metadata=None,
                             photometric="minisblack", tile=(128, 128))
    return [folder / name for name in files], planes


def test_the_first_file_of_a_set_opens_every_channel(app):
    client, source = app
    files, planes = _ome_set(source)
    assert xenium_focus.ome_set(str(files[0])) == tuple(str(f) for f in files)

    channels, overview, _ = _open(files[0])
    assert overview.shape[0] == 3
    for c in range(3):
        assert np.array_equal(_read(channels, c, 0, 1, 0, 256), planes[c, :256, 256:512])
    assert xenium_focus.channel_names(str(files[0])) == ["DAPI", "CD3", "CD8"]

    _, names, entry = _register(client, files[0], "omeset")
    assert entry["num_channels"] == 3
    assert names == ["DAPI", "CD3", "CD8"]
    for c in range(3):
        tile = _tile(client, entry["imageData"][c]["src"], 0, 0, 0)
        assert np.array_equal(tile, planes[c, :tile.shape[0], :tile.shape[1]])


def test_another_member_opens_by_itself(app):
    client, source = app
    files, planes = _ome_set(source)
    assert xenium_focus.ome_set(str(files[1])) == ()
    channels, _, _ = _open(files[1])
    assert np.array_equal(_read(channels, 0, 0, 0, 0, 256), planes[1, :256, :256])
    _, _, entry = _register(client, files[1], "member")
    assert entry["num_channels"] == 1
    tile = _tile(client, entry["imageData"][0]["src"], 0, 0, 0)
    assert np.array_equal(tile, planes[1, :tile.shape[0], :tile.shape[1]])


def test_a_set_with_a_missing_member_is_not_assembled(app):
    client, source = app
    files, planes = _ome_set(source, missing=(2,))
    assert xenium_focus.ome_set(str(files[0])) == ()
    assert not xenium_focus.is_focus_dir(str(files[0]))
    channels, _, _ = _open(files[0])
    assert np.array_equal(_read(channels, 0, 0, 0, 0, 256), planes[0, :256, :256])
    _, _, entry = _register(client, files[0], "partial")
    assert entry["num_channels"] == 1


def test_a_z_stack_spread_over_files_is_not_assembled(tmp_path):
    files, planes = _ome_set(tmp_path, size_z=3)
    assert xenium_focus.ome_set(str(files[0])) == ()
    channels, _, _ = _open(files[0])
    assert np.array_equal(_read(channels, 0, 0, 0, 0, 256), planes[0, :256, :256])


# -- 6a. Imaris 3/4 "classic" -------------------------------------------------

IMARIS_DESCRIPTION = (
    "[ImarisDataSet]\r\nNumberOfImages=1\r\n\r\n"
    "[Image 0]\r\nSizeX=700\r\nSizeY=600\r\nSizeZ=1\r\nSizeC=2\r\n\r\n"
    "[Channel 0.0.0.0]\r\nColor=1.000 0.000 0.000\r\nName = Red\r\n\r\n"
    "[Channel 0.0.0.1]\r\nColor=0.000 1.000 0.000\r\nName = Green\r\n")


def _write_classic_ims(path, height=600, width=700):
    data = _panel(channels=2, height=height, width=width, seed=5)
    thumbnail = np.full((64, 64, 3), 200, dtype=np.uint8)
    with tifffile.TiffWriter(path) as writer:
        writer.write(thumbnail, photometric="rgb", subfiletype=1,
                     description=IMARIS_DESCRIPTION, metadata=None)
        for plane in data:
            writer.write(plane, photometric="minisblack", metadata=None)
    return data


def test_a_classic_ims_is_not_hdf5_and_a_real_one_is(tmp_path):
    from tests.volume_fixtures import write_ims

    classic = tmp_path / "classic.ims"
    _write_classic_ims(classic)
    assert ims_source.is_ims_path(str(classic)) is False
    real = tmp_path / "real.ims"
    write_ims(real)
    assert ims_source.is_ims_path(str(real)) is True


def test_a_classic_ims_opens_as_its_channels(app):
    client, source = app
    path = source / "x.ims"
    data = _write_classic_ims(path)

    with tiff_series.ImageTiff(str(path)) as handle:
        assert tiff_series.channel_series(handle).shape == (2, 600, 700)
        assert tiff_series.embedded_channel_names(handle, 2) == ["Red", "Green"]

    detection = brightfield.detect_image_type(path)
    assert detection.verdict == "fluorescence", detection
    assert brightfield.is_rgb_layout(path) is False

    channels, _, _ = _open(path)
    for c in range(2):
        assert np.array_equal(_read(channels, c, 0, 0, 0, 256), data[c, :256, :256])

    _, names, entry = _register(client, path, "classic")
    assert entry["image_kind"] != "brightfield"
    assert entry["num_channels"] == 2
    assert (entry["width"], entry["height"]) == (700, 600)
    assert names == ["Red", "Green"]
    tile = _tile(client, entry["imageData"][1]["src"], 0, 0, 0)
    assert np.array_equal(tile, data[1, :tile.shape[0], :tile.shape[1]])


# -- 6b. arrange_series ------------------------------------------------------


def _series(shape, reduced=False, name=""):
    page = SimpleNamespace(subfiletype=1 if reduced else 0)
    return SimpleNamespace(shape=shape, pages=[page], name=name)


def test_scn_puts_the_largest_series_first():
    tiff = SimpleNamespace(is_scn=True)
    macro, small, big = _series((1616, 4668, 3), name="macro"), \
        _series((2, 3000, 3000), name="small"), _series((3, 8000, 9000), name="big")
    arranged = tiff_series.arrange_series(tiff, [macro, small, big])
    assert [s.name for s in arranged] == ["big", "macro", "small"]


def test_a_reduced_first_series_yields_to_the_largest_full_one():
    tiff = SimpleNamespace(is_scn=False)
    thumb = _series((64, 64, 3), reduced=True, name="thumb")
    a, b = _series((2, 100, 100), name="a"), _series((2, 600, 700), name="b")
    arranged = tiff_series.arrange_series(tiff, [thumb, a, b])
    assert [s.name for s in arranged] == ["b", "thumb", "a"]


def test_all_reduced_or_plain_files_keep_their_order():
    tiff = SimpleNamespace(is_scn=False)
    first, second = _series((2, 10, 10), name="first"), _series((2, 900, 900), name="second")
    assert [s.name for s in tiff_series.arrange_series(tiff, [first, second])] == ["first", "second"]
    r1, r2 = _series((3, 3), reduced=True, name="r1"), _series((9, 9), reduced=True, name="r2")
    assert [s.name for s in tiff_series.arrange_series(tiff, [r1, r2])] == ["r1", "r2"]


def test_a_single_series_is_unchanged():
    only = [_series((1, 1), reduced=True, name="only")]
    assert tiff_series.arrange_series(SimpleNamespace(is_scn=True), only) is only


# -- 7. fluorescence in a whole-slide-suffixed file ------------------------


@pytest.mark.parametrize("suffix", [".svs", ".scn"])
def test_grey_pages_with_a_slide_suffix_are_fluorescence(app, suffix):
    client, source = app
    data = _panel()
    path = source / f"fluor{suffix}"
    tifffile.imwrite(path, data, photometric="minisblack", metadata=None, tile=(256, 256))
    detection = brightfield.detect_image_type(path)
    assert detection.verdict == "fluorescence", detection
    assert brightfield.is_rgb_layout(path) is False

    _, _, entry = _register(client, path, "fluor")
    assert entry["image_kind"] != "brightfield"
    assert entry["num_channels"] == 3
    tile = _tile(client, entry["imageData"][1]["src"], 0, 0, 0)
    assert np.array_equal(tile, data[1, :tile.shape[0], :tile.shape[1]])


def test_interleaved_rgb_with_a_slide_suffix_stays_brightfield(tmp_path):
    from tests.brightfield_fixtures import write_svs_like

    path = write_svs_like(tmp_path / "he.svs", height=512, width=640)
    assert brightfield.detect_image_type(path).verdict == "brightfield"
    assert brightfield.is_rgb_layout(path) is True
    scn = tmp_path / "he.scn"
    scn.write_bytes(path.read_bytes())
    assert brightfield.detect_image_type(scn).verdict == "brightfield"
    assert brightfield.is_rgb_layout(scn) is True


def test_ycbcr_jpeg_with_a_slide_suffix_stays_brightfield(tmp_path):
    rng = np.random.default_rng(1)
    rgb = rng.integers(0, 255, size=(512, 640, 3), dtype=np.uint8)
    path = tmp_path / "jpeg.svs"
    tifffile.imwrite(path, rgb, photometric="ycbcr", compression="jpeg", tile=(256, 256),
                     metadata=None)
    with tifffile.TiffFile(str(path)) as handle:
        assert handle.pages[0].photometric == 6       # YCbCr really is what was written
    assert brightfield.detect_image_type(path).verdict == "brightfield"
    assert brightfield.is_rgb_layout(path) is True


# -- 8. a pyramid that steps by 4 ---------------------------------------------


def _write_by_four(path, data):
    with tifffile.TiffWriter(path, bigtiff=True) as writer:
        writer.write(data, subifds=2, photometric="minisblack", tile=(256, 256), metadata=None)
        for step in (4, 16):
            writer.write(data[:, ::step, ::step], subfiletype=1, photometric="minisblack",
                         tile=(256, 256), metadata=None)


def test_a_pyramid_stepping_by_four_is_served_on_a_halving_chain(app):
    client, source = app
    data = _smooth()
    path = source / "byfour.ome.tif"
    _write_by_four(path, data)

    with tiff_series.ImageTiff(str(path)) as handle:
        series = tiff_series.channel_series(handle)
        assert [tuple(level.shape) for level in series.levels] == \
            [(3, 2048, 2304), (3, 512, 576), (3, 128, 144)]
        assert tiff_series.steps_unevenly(series) is True

    geometry = image_geometry(str(path))
    shapes = geometry["level_shapes"]
    assert shapes[0] == [2048, 2304]
    assert len(shapes) >= 3
    for finer, coarser in zip(shapes, shapes[1:]):
        assert coarser == [-(-finer[0] // 2), -(-finer[1] // 2)]
    assert geometry["num_channels"] == 3

    channels, overview, _ = _open(path)
    assert len(channels) == len(shapes)
    # Level 0 is the file's own pixels, exactly.
    assert np.array_equal(_read(channels, 1, 0, 1, 1, 512), data[1, 512:1024, 512:1024])
    # Level 1 is half size: compare against a 2x2 mean of level 0.
    tile = _read(channels, 1, 1, 0, 0, 512).astype(np.float64)
    expected = data[1, :1024, :1024].astype(np.float64).reshape(512, 2, 512, 2).mean(axis=(1, 3))
    assert tile.shape == expected.shape
    assert abs(tile.mean() - expected.mean()) < 0.01 * expected.mean()
    assert np.corrcoef(tile.ravel(), expected.ravel())[0, 1] > 0.99
    # Level 2 (1/4) is the file's own 1/4 level.
    level2 = _read(channels, 0, 2, 0, 0, 1024)
    assert level2.shape == (512, 576)
    assert np.corrcoef(level2.ravel().astype(float),
                       data[0, ::4, ::4].ravel().astype(float))[0, 1] > 0.999

    _, _, entry = _register(client, path, "byfour")
    assert entry["maxLevel"] == len(shapes)
    assert (entry["width"], entry["height"]) == (2304, 2048)
    step = entry["tileWidth"]
    routed = _tile(client, entry["imageData"][1]["src"], 1, 0, 0).astype(np.float64)
    want = data[1, :2 * routed.shape[0], :2 * routed.shape[1]].astype(np.float64)
    want = want.reshape(routed.shape[0], 2, routed.shape[1], 2).mean(axis=(1, 3))
    assert np.corrcoef(routed.ravel(), want.ravel())[0, 1] > 0.99
    level0 = _tile(client, entry["imageData"][0]["src"], 0, 1, 0)
    assert np.array_equal(level0, data[0, :level0.shape[0], step:step + level0.shape[1]])


def test_a_halving_pyramid_is_served_from_its_own_levels(tmp_path):
    data = _smooth(height=1024, width=1152)
    path = tmp_path / "halving.ome.tif"
    with tifffile.TiffWriter(path, bigtiff=True) as writer:
        writer.write(data, subifds=2, photometric="minisblack", tile=(256, 256), metadata=None)
        for step in (2, 4):
            writer.write(data[:, ::step, ::step], subfiletype=1, photometric="minisblack",
                         tile=(256, 256), metadata=None)
    with tiff_series.ImageTiff(str(path)) as handle:
        series = tiff_series.channel_series(handle)
        assert tiff_series.steps_unevenly(series) is False
        assert tiff_series.needs_region_reader(series) is False

    import zarr

    channels, _, _ = _open(path)
    assert isinstance(channels, zarr.Group)
    for level, step in enumerate((1, 2, 4)):
        own = data[:, ::step, ::step]
        tile = _read(channels, 2, level, 0, 0, 256)
        assert np.array_equal(tile, own[2, :256, :256]), level


def test_steps_unevenly_tolerates_odd_rounding():
    levels = [SimpleNamespace(shape=(3, 1001, 999)), SimpleNamespace(shape=(3, 500, 500)),
              SimpleNamespace(shape=(3, 250, 250))]
    assert tiff_series.steps_unevenly(SimpleNamespace(levels=levels, shape=(3, 1001, 999))) is False
    single = SimpleNamespace(levels=[], shape=(3, 10, 10))
    assert tiff_series.steps_unevenly(single) is False


# -- 9. channels stored as interleaved samples -----------------------------


def test_four_interleaved_samples_open_as_four_channels(app):
    client, source = app
    data = _panel(channels=4)
    interleaved = np.moveaxis(data, 0, -1).copy()          # (600, 700, 4)
    path = source / "multispectral.tif"
    tifffile.imwrite(path, interleaved, photometric="minisblack", planarconfig="contig",
                     metadata=None, extrasamples=None)
    with tiff_series.ImageTiff(str(path)) as handle:
        series = tiff_series.channel_series(handle)
        assert str(series.axes).endswith("S")

    geometry = image_geometry(str(path))
    assert geometry["num_channels"] == 4
    assert (geometry["height"], geometry["width"]) == (600, 700)

    channels, overview, _ = _open(path)
    assert overview.shape[0] == 4
    for c in range(4):
        assert np.array_equal(_read(channels, c, 0, 0, 0, 512), data[c, :512, :512])

    _, _, entry = _register(client, path, "msi")
    assert entry["num_channels"] == 4
    assert (entry["width"], entry["height"]) == (700, 600)
    tile = _tile(client, entry["imageData"][3]["src"], 0, 0, 0)
    assert np.array_equal(tile, data[3, :tile.shape[0], :tile.shape[1]])


def test_sixteen_bit_rgb_opens_as_eight_bit_colour(app):
    client, source = app
    rng = np.random.default_rng(2)
    rgb = rng.integers(0, 65535, size=(600, 700, 3), dtype=np.uint16)
    path = source / "rgb48.tif"
    tifffile.imwrite(path, rgb, photometric="rgb", metadata=None, tile=(256, 256))
    assert brightfield.is_rgb_layout(path) is True

    _, _, entry = _register(client, path, "rgb48")
    assert entry["image_kind"] == "brightfield"
    src = entry["imageData"][0]["src"]
    response = client.get(f"{src}0/0_0.png?q=hd")
    assert response.status_code == 200, response.data[:300]
    tile = np.asarray(PILImage.open(io.BytesIO(response.data)).convert("RGB"))
    assert tile.dtype == np.uint8
    h, w = tile.shape[:2]
    assert np.array_equal(tile, (rgb[:h, :w] >> 8).astype(np.uint8))
