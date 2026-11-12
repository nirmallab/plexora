"""Smaller images in a `.plexora` file (server/utils/approximate_image.py, the
reader server/utils/formats/approx.py, and "approximate" in
server/models/bundles.py): a project's 16-bit fluorescence image goes about 85%
smaller with every pixel within sqrt(v) + 0.25 counts of v, its pyramid and
OME-XML with it; a mask never goes smaller; import takes the original when this
computer has it and otherwise places the container, which Plexora reads as it
is -- the website too, whose Python has numpy and zstd but no SZ3."""

import io
import json
import zipfile

import numpy as np
import pytest
import tifffile

from plexora import paths
from plexora.server.models import bundles, sources
from plexora.server.models.project import Project, config_transaction, read_config, write_config
from plexora.server.utils import approximate_image, formats, relink

pytestmark = pytest.mark.skipif(not approximate_image.can_make(), reason="no zstd here")

NAMES = ["DNA", "CD3", "Ki-67"]


def _fluorescence(shape, seed=0):
    """Blobs over a background, with photon noise: what a camera records."""
    rng = np.random.default_rng(seed)
    planes, height, width = shape
    yy, xx = np.mgrid[0:height, 0:width]
    out = np.empty(shape, np.uint16)
    for plane in range(planes):
        signal = np.full((height, width), 300.0)
        for _ in range(20):
            y, x = rng.uniform(0, height), rng.uniform(0, width)
            radius, peak = rng.uniform(5, 30), rng.uniform(500, 30000)
            signal += peak * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * radius * radius))
        out[plane] = np.clip(rng.poisson(signal), 0, 65535)
    return out


def _ome(path, data, levels=3, tile=(256, 256), compression="lzw"):
    """An OME-TIFF with its pyramid as SubIFDs, channel names and a pixel size
    in µm (not ASCII: the description must go back as it was)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, subifds=levels - 1, tile=tile, compression=compression, photometric="minisblack",
                     metadata={"axes": "CYX", "Channel": {"Name": NAMES[:data.shape[0]]},
                               "PhysicalSizeX": 0.65, "PhysicalSizeXUnit": "µm",
                               "PhysicalSizeY": 0.65, "PhysicalSizeYUnit": "µm"})
        for step in range(1, levels):
            writer.write(data[:, ::2 ** step, ::2 ** step], subfiletype=1, tile=tile, compression=compression,
                         photometric="minisblack")
    return path


def _within(original, back):
    """Every pixel within sqrt(v) + 0.25 of v (q*q, with |sqrt(v) - q| <= 0.5)."""
    original = original.astype(np.float64)
    return float((np.abs(back.astype(np.float64) - original) - (np.sqrt(original) + 0.25)).max()) <= 1e-9


def _container(tmp_path, source, **encode):
    container = tmp_path / (source.name + approximate_image.SUFFIX)
    with open(container, "wb") as out:
        summary = approximate_image.encode(source, out, **encode)
    return summary, container


# -- the codec -----------------------------------------------------------------


def test_a_tile_comes_back_as_the_square_of_its_rounded_root():
    rng = np.random.default_rng(4)
    for shape in ((1024, 1024), (37, 1000), (5, 3), (1, 1), (700, 64)):
        tile = rng.integers(0, 65536, shape, dtype=np.uint32).astype(np.uint16)
        tile.flat[0], tile.flat[-1] = 0, 65535
        back = approximate_image.decode_tile(approximate_image.encode_tile(tile), shape)
        q = np.rint(np.sqrt(tile.astype(np.float32))).astype(np.int64)
        assert back.shape == shape and np.array_equal(back, np.minimum(q * q, 65535))
        assert _within(tile, back)
    flat = np.full((300, 200), 7, np.uint16)       # one value: one plane, a few bytes
    assert len(approximate_image.encode_tile(flat)) < 200
    with pytest.raises(ValueError):
        approximate_image.decode_tile(b"\x0b" + approximate_image.encode_tile(flat)[1:], flat.shape)


def test_an_image_is_read_within_its_noise_with_its_pyramid_names_and_pixel_size(tmp_path):
    data = _fluorescence((3, 1100, 1300))
    source = _ome(tmp_path / "slide.ome.tif", data)
    assert approximate_image.reason(source) is None
    summary, container = _container(tmp_path, source)
    raw = sum(data[:, ::2 ** k, ::2 ** k].nbytes for k in range(3))
    assert summary["size"] == container.stat().st_size and container.stat().st_size < 0.5 * raw
    # Read as it is, through the formats package (the viewer's, renders', QC's path).
    assert formats.handles(container) and formats.format_name(container) == "Plexora smaller image"
    reader = formats.open_reader(container)
    try:
        assert reader.levels == [(1100, 1300), (550, 650), (275, 325)] and reader.channels == 3
        assert reader.channel_names == NAMES and reader.pixel_size == pytest.approx((0.65, 0.65))
        for level in range(3):
            truth = data[:, ::2 ** level, ::2 ** level]
            for channel in range(3):
                whole = reader.read(level, channel, 0, truth.shape[1], 0, truth.shape[2])
                assert _within(truth[channel], whole)
        # A rectangle across tile edges is the same pixels as the whole.
        part = reader.read(0, 1, 1000, 1100, 1000, 1300)
        assert np.array_equal(part, reader.read(0, 1, 0, 1100, 0, 1300)[1000:, 1000:])
    finally:
        reader.close()
    # Bright pixels come back within a few percent.
    bright = data > 10000
    reader = formats.open_reader(container)
    got = np.stack([reader.read(0, c, 0, 1100, 0, 1300) for c in range(3)])[bright].astype(np.float64)
    reader.close()
    assert np.max(np.abs(got - data[bright]) / data[bright]) < 0.02


def test_written_out_as_a_tiff_it_keeps_its_description_and_is_never_made_smaller_twice(tmp_path):
    data = _fluorescence((2, 1100, 1000), seed=1)
    source = _ome(tmp_path / "slide.ome.tif", data)
    _summary, container = _container(tmp_path, source)
    back = approximate_image.decode(container, tmp_path / "back" / source.name)
    with tifffile.TiffFile(source) as a, tifffile.TiffFile(back) as b:
        assert b.pages[0].software == approximate_image.MARK
        assert b.pages[0].description == a.pages[0].description and "µm" in b.pages[0].description
        assert len(b.pages) == len(a.pages) == 2 and b.pages[0].samplesperpixel == 1
        assert [level.shape for level in b.series[0].levels] == [level.shape for level in a.series[0].levels]
        for la, lb in zip(a.series[0].levels, b.series[0].levels):
            assert _within(la.asarray(), lb.asarray())
    assert approximate_image.reason(back) == "approximate already"
    assert approximate_image.estimate(back) is None


def test_a_plain_stripped_grey_image_goes_too(tmp_path):
    data = _fluorescence((1, 1300, 1100), seed=3)[0]
    source = tmp_path / "plain.tif"
    tifffile.imwrite(source, data, photometric="minisblack")  # strips, no pyramid, no OME
    assert approximate_image.reason(source) is None
    _summary, container = _container(tmp_path, source)
    reader = formats.open_reader(container)
    try:
        assert reader.levels == [(1300, 1100)] and reader.channels == 1
        assert reader.channel_names == ["Channel 1"]
        assert _within(data, reader.read(0, 0, 0, 1300, 0, 1100))
    finally:
        reader.close()


def test_what_is_not_one_grey_16_bit_image_goes_as_it_is(tmp_path):
    big = (1100, 1000)
    cases = {"eight.tif": np.zeros(big, np.uint8), "labels.tif": np.zeros(big, np.uint32)}
    for name, data in cases.items():
        tifffile.imwrite(tmp_path / name, data)
        assert approximate_image.reason(tmp_path / name) == "not 16-bit", name
    tifffile.imwrite(tmp_path / "rgb.tif", np.zeros((*big, 3), np.uint16), photometric="rgb")
    assert approximate_image.reason(tmp_path / "rgb.tif") == "not one grey plane per channel"
    tifffile.imwrite(tmp_path / "tiny.tif", np.zeros((64, 64), np.uint16))
    assert approximate_image.reason(tmp_path / "tiny.tif") == "small"
    (tmp_path / "cells.csv").write_text("a,b\n")
    assert approximate_image.reason(tmp_path / "cells.csv") == "not a TIFF"
    assert approximate_image.likely(tmp_path / "eight.tif") and not approximate_image.likely(tmp_path / "tiny.tif")


def test_the_estimate_knows_a_file_compressed_already(tmp_path):
    data = _fluorescence((1, 1100, 1000), seed=2)[0]
    plain, packed = tmp_path / "plain.tif", tmp_path / "packed.tif"
    tifffile.imwrite(plain, data)
    tifffile.imwrite(packed, data, compression="zlib")
    assert approximate_image.estimate(plain) == int(plain.stat().st_size * approximate_image.SHARE)
    assert approximate_image.estimate(packed) == int(packed.stat().st_size * approximate_image.SHARE_COMPRESSED)
    tifffile.imwrite(tmp_path / "eight.tif", np.zeros((1100, 1000), np.uint8))
    assert approximate_image.estimate(tmp_path / "eight.tif") is None


def test_a_cancel_stops_it_and_a_damaged_container_is_refused(tmp_path):
    source = _ome(tmp_path / "slide.ome.tif", _fluorescence((2, 2100, 900), seed=6), levels=1)

    class Stop(Exception):
        pass

    calls = []

    def check():
        calls.append(1)
        if len(calls) > 1:
            raise Stop

    with pytest.raises(Stop):
        approximate_image.encode(source, io.BytesIO(), check=check)
    (tmp_path / "junk.approx").write_bytes(b"not a container at all")
    with pytest.raises(ValueError):
        approximate_image.read_index(tmp_path / "junk.approx")
    with pytest.raises(formats.FormatUnreadable):
        formats.open_reader(tmp_path / "junk.approx")


def test_the_website_reads_them_and_never_makes_them(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    assert approximate_image.can_read() is True and approximate_image.can_make() is False


# -- in a bundle ---------------------------------------------------------------


def _edit_entry(name, change):
    root = Project.root_for(name)
    with config_transaction():
        config = read_config(paths.config_path(root))
        change(config[name])
        write_config(paths.config_path(root), config)


@pytest.fixture
def slide(tmp_path):
    """A project whose image can go smaller, and whose mask is 16-bit too:
    only the role keeps the mask exact."""
    import plexora

    image = _ome(tmp_path / "here" / "slide.ome.tif", _fluorescence((3, 1100, 1000), seed=9))
    mask = tmp_path / "here" / "slide_mask.tif"
    labels = np.zeros((1100, 1000), np.uint16)
    labels[100:200, 100:200], labels[400:480, 300:420] = 7, 4000
    tifffile.imwrite(mask, labels)
    plexora.register_image_datasource("slide", str(image))
    _edit_entry("slide", lambda entry: entry.update(segmentation=str(mask), segmentationSource=str(mask)))
    return image, mask, labels


def _entry(name):
    """The registry entry as this computer reads it (its own paths)."""
    return read_config(paths.config_path(Project.root_for(name)))[name]


def _members(path):
    with zipfile.ZipFile(path) as bundle:
        return bundle.namelist(), json.loads(bundle.read(bundles.MANIFEST))


def _another_computer(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path / "other" / "projects"))
    paths.reset()
    monkeypatch.setattr(relink, "_index", lambda name: [])
    monkeypatch.setattr(relink, "_user_folders", lambda: [])
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")


def _ref(manifest, role):
    return next(ref for ref in manifest["projects"][0]["refs"] if role in (ref.get("roles") or [ref["role"]]))


def _export(tmp_path, include=None):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out, include=include or {"images": True, "approximate": True})
    return out


def test_the_image_goes_smaller_and_the_mask_goes_exactly(slide, tmp_path):
    image, _mask, _labels = slide
    out = _export(tmp_path)
    names, manifest = _members(out)
    picture, masks = _ref(manifest, "channelFile"), _ref(manifest, "segmentation")
    assert picture["included"] is None and picture["approximate"].endswith("/slide.ome.tif.approx")
    assert picture["approximate"] in names
    assert masks["included"] in names and not masks.get("approximate")
    record = {item["path"]: item for item in manifest["media"]}[picture["approximate"]]
    assert record["approximate"] == {"method": "sqrt-grid", "source_size": image.stat().st_size}
    assert record["sha256"] and record["size"] < 0.6 * image.stat().st_size
    with zipfile.ZipFile(out) as bundle:
        assert bundle.getinfo(picture["approximate"]).compress_type == zipfile.ZIP_STORED
    # Without "approximate", the same image goes byte for byte.
    plain = tmp_path / "plain.plexora"
    bundles.export(["slide"], plain, include={"images": True})
    assert _ref(_members(plain)[1], "channelFile")["included"]


def test_import_places_the_smaller_image_when_this_computer_has_no_original(slide, tmp_path, monkeypatch):
    image, _mask, labels = slide
    data = tifffile.imread(image)
    out = _export(tmp_path)
    container = _ref(_members(out)[1], "channelFile")["approximate"]
    _another_computer(tmp_path, monkeypatch)
    monkeypatch.setattr(bundles, "_find_cheap", lambda *args, **kwargs: None)  # no original anywhere
    assert bundles.preview(out)["projects"][0]["refs"] == {"found": 0, "included": 2, "approximate": 1,
                                                            "missing": 0, "node": 0, "nodes": []}
    result = bundles.apply(out, search=False)
    assert result["projects"][0]["refs"]["approximate"] == 1
    placed = paths.project_state_dir("slide").joinpath(*container.split("/"))
    entry = _entry("slide")
    assert entry["channelFile"] == str(placed) and placed.is_file()
    reader = formats.open_reader(placed)
    try:
        assert all(_within(data[c], reader.read(0, c, 0, 1100, 0, 1000)) for c in range(3))
    finally:
        reader.close()
    # The original's identity stays, so another computer still finds the original.
    assert entry["sources"][str(image)]["size"] == image.stat().st_size
    # The mask came byte for byte.
    assert np.array_equal(tifffile.imread(entry["segmentationSource"]), labels)


def test_the_original_wins_over_a_smaller_copy(slide, tmp_path, monkeypatch):
    image, _mask, _labels = slide
    out = _export(tmp_path)
    container = _ref(_members(out)[1], "channelFile")["approximate"]
    _another_computer(tmp_path, monkeypatch)
    assert bundles.preview(out)["projects"][0]["refs"]["found"] == 1
    result = bundles.apply(out, search=False)
    assert result["projects"][0]["refs"]["approximate"] == 0
    assert not paths.project_state_dir("slide").joinpath(*container.split("/")).exists()
    assert _entry("slide")["channelFile"] == str(image)


def test_a_plexora_that_cannot_read_them_looks_for_the_original(slide, tmp_path, monkeypatch):
    out = _export(tmp_path)
    _another_computer(tmp_path, monkeypatch)
    monkeypatch.setattr(bundles, "_find_cheap", lambda *args, **kwargs: None)
    monkeypatch.setattr(approximate_image, "can_read", lambda: False)
    assert bundles.preview(out)["projects"][0]["refs"]["missing"] == 1
    result = bundles.apply(out, search=False)
    assert result["projects"][0]["refs"]["missing"] == 1
    assert not list(paths.project_state_dir("slide").rglob("*.approx"))


def test_the_websites_page_places_a_smaller_image_as_an_included_file(slide, tmp_path, monkeypatch):
    out = _export(tmp_path)
    container = _ref(_members(out)[1], "channelFile")["approximate"]
    _another_computer(tmp_path, monkeypatch)
    targets = {item["path"]: item["target"] for item in bundles.placements(out, at_least=0)}
    assert targets[container] == str(paths.project_state_dir("slide").joinpath(*container.split("/")))


def test_the_estimate_offers_smaller_images(slide):
    image, _mask, _labels = slide
    sized = bundles.estimate(["slide"])
    assert sized["approximate"] == {"available": True, "share": approximate_image.SHARE}
    items = {item["role"]: item for item in sized["rows"]["images"]["items"]}
    assert items["channelFile"]["approximate"] is True
    # LZW already, so a larger share of its file than an uncompressed one.
    assert items["channelFile"]["approximate_size"] == int(image.stat().st_size * approximate_image.SHARE_COMPRESSED)
    assert items["segmentation"]["approximate"] is False
    rows = sized["rows"]["images"]
    assert rows["approximate_size"] == rows["size"] - image.stat().st_size + items["channelFile"]["approximate_size"]
    smaller = bundles.estimate(["slide"], {"images": True, "approximate": True})
    assert smaller["total"] == bundles.estimate(["slide"], {"images": True})["total"] \
        - rows["size"] + rows["approximate_size"]


def test_a_compressed_pyramid_copy_goes_as_it_is(tmp_path, monkeypatch):
    """A flat image's compressed pyramidized copy (`<name>.pyramid.approx`,
    server/utils/image_pyramid.py) is a ready-to-open file: carried byte for
    byte with "derived", stored rather than deflated, and never made smaller
    again -- "approximate" makes the original smaller, not the copy."""
    import plexora
    from plexora.server.utils import image_pyramid

    monkeypatch.setattr(image_pyramid, "FLAT_IMAGE_MAX_SIDE", 300)
    original = tmp_path / "here" / "flat.tif"
    original.parent.mkdir()
    tifffile.imwrite(original, _fluorescence((2, 1100, 1000), seed=3), metadata={"axes": "CYX"})
    plexora.register_image_datasource("flat", str(original))
    copy = tmp_path / "here" / "flat.pyramid.approx"
    assert _entry("flat")["channelFile"] == str(copy)

    out = tmp_path / "flat.plexora"
    bundles.export(["flat"], out, include={"images": True, "derived": True, "approximate": True})
    names, manifest = _members(out)
    carried, source = _ref(manifest, "channelFile"), _ref(manifest, "imageSource")
    assert carried["derived"] is True and not carried.get("approximate")
    assert carried["included"].endswith("/flat.pyramid.approx") and carried["included"] in names
    with zipfile.ZipFile(out) as bundle:
        assert bundle.read(carried["included"]) == copy.read_bytes()
        assert bundle.getinfo(carried["included"]).compress_type == zipfile.ZIP_STORED
    assert source["approximate"].endswith("/flat.tif.approx")


def test_never_made_on_the_website(slide, monkeypatch):
    monkeypatch.setattr(approximate_image, "can_make", lambda: False)
    built = bundles.plan(["slide"], {"images": True, "approximate": True})
    assert not any(member.approximate for member in built.members)
    assert all(not ref.get("approximate") for ref in built.manifest["projects"][0]["refs"])
    assert bundles.estimate(["slide"])["approximate"]["available"] is False
