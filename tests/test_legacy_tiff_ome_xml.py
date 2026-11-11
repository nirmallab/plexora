"""OME-XML of every schema vocabulary, read for what a viewer needs.

Regression tests for `plexora/server/utils/ome_xml.py` and its callers: channel
names and pixel size out of documents written under 2003-FC to 2016-06, by
local tag name, with the first Image the image; channel names out of the
non-OME flavours (PerkinElmer/Akoya QPI pages, ImageJ slice labels); and the
pixel-size rules of `brightfield.physical_metadata` (first Image wins, units
to microns, TIFF resolution placeholders are not a calibration).

Every expectation is what the hand-written document states, not what the
reader happens to return.
"""

import io
import time

import numpy as np
import pytest
import tifffile
from PIL import Image as PILImage

import plexora
from plexora.datasource import derive_image_channel_names
from plexora.server.utils import brightfield, ome_xml, tiff_series
from tests.helpers import use_data_root

NS_2016 = "http://www.openmicroscopy.org/Schemas/OME/2016-06"
NS_2010 = "http://www.openmicroscopy.org/Schemas/OME/2010-06"
NS_2008 = "http://www.openmicroscopy.org/Schemas/OME/2008-02"
NS_FC = "http://www.openmicroscopy.org/XMLschemas/OME/FC/ome.xsd"

H, W = 600, 700


def doc_2016(names=("DAPI", "CD3", "Ki67"), size="325", unit="nm", height=H, width=W):
    channels = "".join(
        f'<Channel ID="Channel:0:{i}" Name="{name}" SamplesPerPixel="1"/>'
        for i, name in enumerate(names))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<OME xmlns="{NS_2016}" UUID="urn:uuid:0000-2016">'
        '<Image ID="Image:0" Name="scan">'
        f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" SizeX="{width}" '
        f'SizeY="{height}" SizeC="{len(names)}" SizeZ="1" SizeT="1" '
        f'PhysicalSizeX="{size}" PhysicalSizeXUnit="{unit}" '
        f'PhysicalSizeY="{size}" PhysicalSizeYUnit="{unit}">'
        f'{channels}'
        '</Pixels></Image></OME>')


def doc_2010(names=("DAPI", "FITC", "TRITC"), size="0.5", height=H, width=W):
    channels = "".join(f'<Channel ID="Channel:0:{i}" Name="{name}"/>' for i, name in enumerate(names))
    return (
        f'<OME xmlns="{NS_2010}">'
        '<Image ID="Image:0" Name="old">'
        f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" SizeX="{width}" '
        f'SizeY="{height}" SizeC="{len(names)}" SizeZ="1" SizeT="1" '
        f'PhysicalSizeX="{size}" PhysicalSizeY="{size}">'
        f'{channels}'
        '</Pixels></Image></OME>')


def doc_2008(names=("Hoechst", "GFP", "mCherry"), size="0.4", height=H, width=W, order=None):
    order = list(range(len(names))) if order is None else order
    logical = "".join(
        f'<LogicalChannel ID="LogicalChannel:{i}" Name="{names[i]}">'
        f'<ChannelComponent Pixels="Pixels:0" Index="{i}" ColorDomain="foo"/>'
        '</LogicalChannel>' for i in order)
    return (
        f'<OME xmlns="{NS_2008}">'
        '<Image ID="Image:0" Name="older">'
        f'{logical}'
        f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" PixelType="uint16" SizeX="{width}" '
        f'SizeY="{height}" SizeC="{len(names)}" SizeZ="1" SizeT="1" '
        f'PhysicalSizeX="{size}" PhysicalSizeY="{size}" BigEndian="false">'
        '<TiffData/></Pixels></Image></OME>')


def doc_fc(names=("FITC", "Cy3"), size="0.2", height=H, width=W):
    info = "".join(
        f'<ChannelInfo ID="ChannelInfo:{i}" Name="{name}">'
        f'<ChannelComponent Pixels="Pixels:0" Index="{i}" ColorDomain="foo"/>'
        '</ChannelInfo>' for i, name in enumerate(names))
    return (
        f'<OME xmlns="{NS_FC}">'
        f'<Image ID="Image:0" Name="fc" PixelSizeX="{size}" PixelSizeY="{size}" PixelSizeZ="1.0">'
        '<CreationDate>2004-01-01T00:00:00</CreationDate>'
        f'{info}'
        f'<Pixels ID="Pixels:0" DimensionOrder="XYZCT" PixelType="uint16" BigEndian="true" '
        f'SizeX="{width}" SizeY="{height}" SizeZ="1" SizeC="{len(names)}" SizeT="1">'
        '<TiffData/></Pixels></Image></OME>')


def two_images(first=("A", "B"), second=("C", "D", "E"), sizes=("0.325", "0.65")):
    def image(index, names, size):
        channels = "".join(f'<Channel ID="Channel:{index}:{i}" Name="{n}"/>' for i, n in enumerate(names))
        return (f'<Image ID="Image:{index}" Name="img{index}">'
                f'<Pixels ID="Pixels:{index}" DimensionOrder="XYCZT" Type="uint16" '
                f'SizeX="{W // (index + 1)}" SizeY="{H // (index + 1)}" SizeC="{len(names)}" '
                f'SizeZ="1" SizeT="1" PhysicalSizeX="{size}" PhysicalSizeY="{size}">'
                f'{channels}</Pixels></Image>')
    return (f'<OME xmlns="{NS_2016}">' + image(0, first, sizes[0])
            + image(1, second, sizes[1]) + '</OME>')


# -- ome_xml.read across schema vocabularies -------------------------------


def test_2016_06_channels_size_and_unit():
    document = ome_xml.read(doc_2016())
    assert document is not None and document.schema == "2016-06"
    assert document.uuid == "urn:uuid:0000-2016"
    (image,) = document.images
    assert (image.size_x, image.size_y, image.size_c) == (W, H, 3)
    assert image.pixel_type == "uint16"
    assert image.physical_size["x"] == (325.0, "nm")
    assert ome_xml.channel_names(doc_2016()) == ["DAPI", "CD3", "Ki67"]
    pixels = ome_xml.pixels(doc_2016())
    assert pixels["physical_size_x"] == 325.0 and pixels["physical_size_x_unit"] == "nm"
    assert [c["name"] for c in pixels["channels"]] == ["DAPI", "CD3", "Ki67"]


def test_2010_06_type_attribute_and_no_units_means_microns():
    document = ome_xml.read(doc_2010())
    assert document.schema == "2010-06"
    image = document.images[0]
    assert image.pixel_type == "uint16"
    assert image.physical_size["x"] == (0.5, "µm")
    assert ome_xml.channel_names(doc_2010()) == ["DAPI", "FITC", "TRITC"]
    assert ome_xml.pixels(doc_2010())["physical_size_x"] == 0.5


def test_2008_02_logical_channels_by_component_index():
    xml = doc_2008()
    document = ome_xml.read(xml)
    assert document.schema == "2008-02"
    assert document.images[0].pixel_type == "uint16"
    assert document.images[0].big_endian is False
    assert ome_xml.channel_names(xml) == ["Hoechst", "GFP", "mCherry"]


def test_2008_02_logical_channels_listed_out_of_order_land_at_their_index():
    # The document lists channel 2 first; its ChannelComponent says Index=2.
    xml = doc_2008(order=[2, 0, 1])
    assert ome_xml.channel_names(xml) == ["Hoechst", "GFP", "mCherry"]


def test_2003_fc_channel_info_and_image_pixel_size():
    xml = doc_fc()
    document = ome_xml.read(xml)
    assert document.schema == "2003-FC"
    image = document.images[0]
    assert image.physical_size["x"] == (0.2, "µm")
    assert image.size_c == 2
    assert ome_xml.channel_names(xml) == ["FITC", "Cy3"]
    assert ome_xml.pixels(xml)["physical_size_x"] == 0.2


def test_a_namespace_prefixed_root_is_read():
    xml = (f'<ome:OME xmlns:ome="{NS_2016}">'
           '<ome:Image ID="Image:0"><ome:Pixels ID="Pixels:0" DimensionOrder="XYCZT" '
           f'Type="uint8" SizeX="{W}" SizeY="{H}" SizeC="2" SizeZ="1" SizeT="1" '
           'PhysicalSizeX="0.25" PhysicalSizeY="0.25">'
           '<ome:Channel ID="Channel:0:0" Name="DNA"/><ome:Channel ID="Channel:0:1" Name="Actin"/>'
           '</ome:Pixels></ome:Image></ome:OME>')
    document = ome_xml.read(xml)
    assert document is not None, "a prefixed <ome:OME> root is OME-XML by local name"
    assert ome_xml.channel_names(xml) == ["DNA", "Actin"]
    assert ome_xml.pixels(xml)["physical_size_x"] == 0.25


def test_only_the_first_image_by_default_and_both_on_request():
    xml = two_images()
    assert len(ome_xml.read(xml).images) == 1
    both = ome_xml.read(xml, images=2).images
    assert [c["name"] for c in both[0].channels] == ["A", "B"]
    assert [c["name"] for c in both[1].channels] == ["C", "D", "E"]
    assert ome_xml.channel_names(xml) == ["A", "B"]
    assert ome_xml.pixels(xml)["physical_size_x"] == 0.325
    # Asking for more than there are is just all of them.
    assert len(ome_xml.read(xml, images=5).images) == 2


def test_tiffdata_file_names_and_binary_only():
    xml = (f'<OME xmlns="{NS_2016}" UUID="urn:uuid:set">'
           f'<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" '
           f'SizeX="{W}" SizeY="{H}" SizeC="3" SizeZ="1" SizeT="1">'
           + "".join(f'<Channel ID="Channel:0:{c}" Name="c{c}"/>' for c in range(3))
           + "".join(f'<TiffData FirstC="{c}" FirstZ="0" FirstT="0" IFD="0" PlaneCount="1">'
                     f'<UUID FileName="set_c{c}.ome.tif">urn:uuid:f{c}</UUID></TiffData>'
                     for c in range(3))
           + '</Pixels></Image></OME>')
    image = ome_xml.read(xml).images[0]
    assert image.files == ["set_c0.ome.tif", "set_c1.ome.tif", "set_c2.ome.tif"]
    assert image.tiff_data[1] == (1, 0, 0, 0, 1, "set_c1.ome.tif", "urn:uuid:f1")

    binary = (f'<OME xmlns="{NS_2016}" UUID="urn:uuid:me">'
              '<BinaryOnly MetadataFile="set.companion.ome" UUID="urn:uuid:meta"/></OME>')
    document = ome_xml.read(binary)
    assert document is not None
    assert document.metadata_file == "set.companion.ome"
    assert document.images == []
    assert document.uuid == "urn:uuid:me"


def test_files_are_empty_when_every_plane_is_in_this_file():
    xml = doc_2016().replace("</Pixels>", '<TiffData IFD="0" PlaneCount="3"/></Pixels>')
    assert ome_xml.read(xml).images[0].files == []


def test_a_utf16_declaration_on_a_str_is_read():
    xml = '<?xml version="1.0" encoding="utf-16"?>' + doc_2010().split("?>", 1)[-1]
    assert ome_xml.channel_names(xml) == ["DAPI", "FITC", "TRITC"]
    assert ome_xml.is_ome_xml(xml)
    assert ome_xml.pixels(xml)["physical_size_x"] == 0.5


def test_a_truncated_document_answers_for_the_images_that_closed():
    full = two_images()
    cut = full[:full.index('<Image ID="Image:1"') + 60]     # inside the second Image
    assert ome_xml.channel_names(cut) == ["A", "B"]
    document = ome_xml.read(cut, images=2)
    assert document is not None
    assert len(document.images) == 1
    # Cut inside the FIRST image: nothing closed, nothing to answer for.
    early = full[:full.index('<Channel ID="Channel:0:1"')]
    assert ome_xml.read(early) is None
    assert ome_xml.channel_names(early) == []
    assert ome_xml.pixels(early) == {}


def test_megabytes_of_trailing_annotations_cost_nothing():
    annotation = ('<XMLAnnotation ID="Annotation:{i}"><Value><OriginalMetadata>'
                  '<Key>Some vendor key {i}</Key><Value>' + "x" * 200
                  + '</Value></OriginalMetadata></Value></XMLAnnotation>')
    trailer = "".join(annotation.format(i=i) for i in range(80_000))     # ~25 MB
    xml = doc_2016().replace("</Image></OME>",
                             "</Image><StructuredAnnotations>" + trailer
                             + "</StructuredAnnotations></OME>")
    assert len(xml) > 20_000_000

    from xml.etree import ElementTree

    started = time.perf_counter()
    ElementTree.fromstring(xml.split("?>", 1)[1].encode())
    full_parse = time.perf_counter() - started

    started = time.perf_counter()
    names = ome_xml.channel_names(xml)
    pixels = ome_xml.pixels(xml)
    elapsed = time.perf_counter() - started

    assert names == ["DAPI", "CD3", "Ki67"]
    assert pixels["physical_size_x"] == 325.0
    # Two reads, each stopping at the end of the first Image.
    assert elapsed < max(0.5, full_parse / 2), (elapsed, full_parse)


@pytest.mark.parametrize("text", [
    "ImageJ=1.53t\nimages=3\nchannels=3\n",
    "Aperio Image Library v11.2.1\n700x600 |MPP = 0.25",
    '<?xml version="1.0"?><PerkinElmer-QPI-ImageDescription><Name>DAPI</Name>'
    '</PerkinElmer-QPI-ImageDescription>',
    '<Root><OMEGA/></Root>',
    "",
    None,
])
def test_non_ome_text_is_none(text):
    assert ome_xml.read(text) is None
    assert ome_xml.channel_names(text) == []
    assert ome_xml.pixels(text) == {}


# -- channel names reach the app ------------------------------------------


def _panel(height=H, width=W, channels=3):
    rng = np.random.default_rng(7)
    return rng.integers(1, 4000, size=(channels, height, width), dtype=np.uint16)


def _write_with_description(path, data, description):
    # utf-8 bytes, as an OME writer stores a µ (tifffile refuses non-ASCII str).
    tifffile.imwrite(path, data, description=description.encode("utf-8"), metadata=None,
                     photometric="minisblack", tile=(256, 256))
    return path


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


@pytest.mark.parametrize("schema,writer,expected", [
    ("2008-02", doc_2008, ["Hoechst", "GFP", "mCherry"]),
    ("2010-06", doc_2010, ["DAPI", "FITC", "TRITC"]),
    ("2003-FC", lambda: doc_fc(names=("FITC", "Cy3", "Cy5")), ["FITC", "Cy3", "Cy5"]),
])
def test_old_schema_channel_names_register(app, schema, writer, expected):
    client, source = app
    data = _panel()
    path = _write_with_description(source / "old.ome.tif", data, writer())
    assert derive_image_channel_names(str(path), 3) == (expected, "image metadata")

    _, names, entry = _register(client, path, "old")
    assert names == expected
    assert [c["name"] for c in entry["imageData"]] == expected
    assert entry["num_channels"] == 3
    tile = _tile(client, entry["imageData"][1]["src"], 0, 0, 0)
    assert np.array_equal(tile, data[1, :256, :256])


def test_old_schema_with_fewer_names_than_channels_falls_back_to_generic(tmp_path):
    path = _write_with_description(tmp_path / "x.ome.tif", _panel(),
                                   doc_2010(names=("DAPI", "FITC")))
    names, source = derive_image_channel_names(str(path), 3)
    assert source == "generic"


def _qpi_description(marker):
    return ('<?xml version="1.0" encoding="utf-16"?>'
            '<PerkinElmer-QPI-ImageDescription><DescriptionVersion>2</DescriptionVersion>'
            '<ImageType>FullResolution</ImageType>'
            f'<Name>{marker}</Name>'
            '<Responsivity><Band><Name>DAPI</Name><Response>1</Response></Band></Responsivity>'
            '</PerkinElmer-QPI-ImageDescription>')


@pytest.mark.parametrize("software", ["PerkinElmer-QPI", None])
def test_perkinelmer_pages_name_their_channels(app, software):
    client, source = app
    markers = ["DAPI", "CD8 (Opal 540)", "PDL1 (Opal 520)"]
    data = _panel()
    path = source / "component_data.tif"
    with tifffile.TiffWriter(path) as writer:
        for plane, marker in zip(data, markers):
            writer.write(plane, description=_qpi_description(marker), metadata=None,
                         photometric="minisblack", software=software)
    assert derive_image_channel_names(str(path), 3) == (markers, "image metadata")
    _, names, entry = _register(client, path, "qpi")
    assert names == markers
    assert np.array_equal(_tile(client, entry["imageData"][2]["src"], 0, 0, 0),
                          data[2, :entry["tileHeight"], :entry["tileWidth"]])


def test_imagej_slice_labels_name_the_channels(app):
    client, source = app
    data = _panel()
    labels = ["DAPI", "CD45", "PanCK"]
    path = source / "hyper.tif"
    tifffile.imwrite(path, data, imagej=True, metadata={"axes": "CYX", "Labels": labels})
    assert derive_image_channel_names(str(path), 3) == (labels, "image metadata")
    _, names, _ = _register(client, path, "ij")
    assert names == labels


def test_imagej_labels_of_the_wrong_count_are_not_used(tmp_path):
    path = tmp_path / "hyper.tif"
    tifffile.imwrite(path, _panel(height=64, width=64), imagej=True,
                     metadata={"axes": "CYX", "Labels": ["DAPI", "CD45", "PanCK"]})
    with tifffile.TiffFile(str(path)) as handle:
        assert tiff_series.embedded_channel_names(handle, 4) is None


# -- pixel size -------------------------------------------------------------


def _one_image_per_resolution(path, sizes=(0.325, 0.65, 1.3)):
    """bfconvert before the 2019 pyramid spec: one OME Image per level."""
    images = []
    for index, size in enumerate(sizes):
        width, height = W >> index, H >> index
        images.append(
            f'<Image ID="Image:{index}" Name="level{index}"><Pixels ID="Pixels:{index}" '
            f'DimensionOrder="XYCZT" Type="uint16" SizeX="{width}" SizeY="{height}" SizeC="1" '
            f'SizeZ="1" SizeT="1" PhysicalSizeX="{size}" PhysicalSizeXUnit="µm" '
            f'PhysicalSizeY="{size}" PhysicalSizeYUnit="µm">'
            f'<Channel ID="Channel:{index}:0" Name="DAPI"/>'
            f'<TiffData IFD="{index}" PlaneCount="1"/></Pixels></Image>')
    xml = f'<OME xmlns="{NS_2016}">' + "".join(images) + "</OME>"
    data = _panel(channels=1)[0]
    with tifffile.TiffWriter(path) as writer:
        for index in range(len(sizes)):
            writer.write(data[::2 ** index, ::2 ** index],
                         description=xml.encode("utf-8") if index == 0 else None, metadata=None,
                         photometric="minisblack")
    return path


def test_the_first_images_pixel_size_wins(app):
    client, source = app
    path = _one_image_per_resolution(source / "levels.ome.tif")
    found = brightfield.physical_metadata(path)
    assert found["physical_size_x"] == pytest.approx(0.325)
    assert found["physical_size_y"] == pytest.approx(0.325)
    inspected, _, entry = _register(client, path, "levels")
    assert inspected["samples"][0]["layers"][0]["pixelSize"] == pytest.approx(0.325)
    metadata = client.get("/get_ome_metadata?datasource=levels").get_json()
    assert float(metadata["physical_size_x"]) == pytest.approx(0.325)


def test_a_later_image_does_not_lend_the_first_its_pixel_size(tmp_path):
    # The first Image (the scan) states no size; the second (a macro) does.
    xml = (f'<OME xmlns="{NS_2016}">'
           f'<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" '
           f'SizeX="{W}" SizeY="{H}" SizeC="1" SizeZ="1" SizeT="1"><Channel ID="Channel:0:0"/>'
           '</Pixels></Image>'
           '<Image ID="Image:1" Name="macro"><Pixels ID="Pixels:1" DimensionOrder="XYCZT" '
           'Type="uint8" SizeX="100" SizeY="80" SizeC="1" SizeZ="1" SizeT="1" '
           'PhysicalSizeX="16.0" PhysicalSizeY="16.0"><Channel ID="Channel:1:0"/>'
           '</Pixels></Image></OME>')
    path = _write_with_description(tmp_path / "x.ome.tif", _panel(channels=1)[0], xml)
    assert ome_xml.pixels(xml).get("physical_size_x") is None
    assert brightfield.physical_metadata(path).get("physical_size_x") is None


@pytest.mark.parametrize("size,unit,microns", [
    ("325", "nm", 0.325),
    ("0.0005", "mm", 0.5),
    ("0.75", "µm", 0.75),
])
def test_a_stated_unit_is_converted_to_microns(tmp_path, size, unit, microns):
    path = _write_with_description(tmp_path / "u.ome.tif", _panel(),
                                   doc_2016(size=size, unit=unit))
    found = brightfield.physical_metadata(path)
    assert found["physical_size_x"] == pytest.approx(microns)
    assert found["physical_size_x_unit"] == "µm"


def test_a_unit_stated_before_its_value_is_still_its_unit(tmp_path):
    # XML attribute order carries no meaning.
    xml = doc_2016(size="325", unit="nm").replace(
        'PhysicalSizeX="325" PhysicalSizeXUnit="nm"', 'PhysicalSizeXUnit="nm" PhysicalSizeX="325"')
    path = _write_with_description(tmp_path / "u.ome.tif", _panel(), xml)
    assert brightfield.physical_metadata(path)["physical_size_x"] == pytest.approx(0.325)


def test_2003_fc_pixel_size_through_physical_metadata(tmp_path):
    path = _write_with_description(tmp_path / "fc.ome.tif", _panel(channels=2),
                                   doc_fc(size="0.2"))
    assert brightfield.physical_metadata(path)["physical_size_x"] == pytest.approx(0.2)


def test_a_picometre_pixel_size_is_ignored(app):
    client, source = app
    path = _write_with_description(source / "tiny.ome.tif", _panel(),
                                   doc_2010(size="1e-12"))
    assert brightfield.physical_metadata(path).get("physical_size_x") is None
    inspected, _, entry = _register(client, path, "tiny")
    assert inspected["samples"][0]["layers"][0]["pixelSize"] is None
    assert not entry.get("pixelSize")


@pytest.mark.parametrize("resolution,unit", [
    ((1, 1), "INCH"),
    ((1, 1), "CENTIMETER"),
    ((10, 1), "CENTIMETER"),
    ((72, 1), "INCH"),
    ((96, 1), "INCH"),
])
def test_resolution_tag_placeholders_are_not_a_pixel_size(app, resolution, unit):
    client, source = app
    path = source / "plain.tif"
    tifffile.imwrite(path, _panel(), photometric="minisblack", metadata=None,
                     resolution=resolution, resolutionunit=unit)
    assert brightfield.physical_metadata(path) == {}
    inspected, _, entry = _register(client, path, "plain")
    assert inspected["samples"][0]["layers"][0]["pixelSize"] is None
    assert not entry.get("pixelSize")


def test_a_real_resolution_calibration_is_reported(app):
    client, source = app
    path = source / "calibrated.tif"
    tifffile.imwrite(path, _panel(), photometric="minisblack", metadata=None,
                     resolution=(20000, 20000), resolutionunit="CENTIMETER")
    found = brightfield.physical_metadata(path)
    assert found["physical_size_x"] == pytest.approx(0.5)
    inspected, _, _ = _register(client, path, "calibrated")
    assert inspected["samples"][0]["layers"][0]["pixelSize"] == pytest.approx(0.5)
