"""The website's pyramid builder writes the copy Python writes.

On the website a flat image's pyramidized copy is built by the page's own
workers (client src/js/node/pyramid.js, tiffWriter.js, reduce.js): Python in
the page has one thread and a 32-bit heap, and a copy it wrote would sit in
the browser's memory. Python then reads that copy exactly as it reads its
own, so it must BE its own: `pyramidize_image`'s file, level for level and
pixel for pixel, with the same OME metadata (channel names, pixel size, and
the source key that lets a later import adopt the copy).

Each case writes a flat TIFF, builds a copy with `pyramidize_image` and one
with the page's builder (tests/js/pyramid_probe.mjs, run in Node over the
same modules the browser loads), given exactly what Python asks the page for
(`image_pyramid.page_build`), and compares what tifffile reads from both.
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

from plexora.server.utils import image_pyramid

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "pyramid_probe.mjs"
NODE = shutil.which("node")
GEOTIFF = ROOT / "plexora" / "client" / "node_modules" / "geotiff"

pytestmark = pytest.mark.skipif(not NODE or not GEOTIFF.is_dir(),
                                reason="needs node and plexora/client/node_modules (npm ci)")

TILE = 256


def _pixels(dtype, shape, seed):
    rng = np.random.default_rng(seed)
    dtype = np.dtype(dtype)
    if dtype.kind == "f":
        return rng.normal(100, 40, size=shape).astype(dtype)
    info = np.iinfo(dtype)
    # The full range, so every rounding case of the 2x2 mean occurs, and a
    # gradient so the levels are not noise alone.
    noise = rng.integers(info.min, info.max, size=shape, endpoint=True, dtype=np.int64)
    ramp = np.linspace(info.min, info.max, shape[-1], dtype=np.float64)
    return np.where(rng.random(shape) < 0.5, noise, ramp.astype(np.int64)).astype(dtype)


CASES = {
    # name: (dtype, (C, H, W), tifffile write options)
    "uint16_lzw_strips": ("uint16", (3, 1037, 1529), {"compression": "lzw", "rowsperstrip": 37}),
    "uint8_tiled_zlib": ("uint8", (1, 700, 1100), {"compression": "zlib", "tile": (128, 128)}),
    "int16_raw_strips": ("int16", (2, 513, 769), {}),
    "float32_zlib_strips": ("float32", (2, 600, 520), {"compression": "zlib", "rowsperstrip": 64}),
    "uint32_one_channel": ("uint32", (1, 600, 777), {"rowsperstrip": 100}),
    "int32_tiled_raw": ("int32", (2, 530, 610), {"tile": (64, 64)}),
}


def _write_source(path, dtype, shape, options, seed):
    data = _pixels(dtype, shape, seed)
    tifffile.imwrite(path, data, photometric="minisblack", metadata={"axes": "CYX"}, **options)
    return data


def _page_copy(source, spec, out, job_bytes):
    request = {"source": str(source), "out": str(out), "dtype": spec["dtype"], "channels": spec["shape"][0],
               "factors": spec["factors"], "tile": spec["tile"], "description": spec["description"],
               "kind": spec.get("kind", "image"), "jobBytes": job_bytes}
    spec_file = out.with_suffix(".json")
    spec_file.write_text(json.dumps(request), encoding="utf-8")
    done = subprocess.run([NODE, str(PROBE), str(spec_file)], capture_output=True, text=True,
                          encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _levels(path):
    with tifffile.TiffFile(path) as tiff:
        series = tiff.series[0]
        return [np.asarray(level.asarray()) for level in series.levels], series.axes


def _ome(path):
    with tifffile.TiffFile(path) as tiff:
        meta = tifffile.xml2dict(tiff.ome_metadata)["OME"]
    meta.pop("UUID", None)
    meta.pop("Creator", None)
    return meta


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_page_writes_the_copy_python_writes(tmp_path, name):
    dtype, shape, options = CASES[name]
    source = tmp_path / f"{name}.tif"
    _write_source(source, dtype, shape, options, seed=len(name))
    names = [f"{name}-{c}" for c in range(shape[0])]
    physical = {"physical_size_x": 0.325, "physical_size_x_unit": "µm",
                "physical_size_y": 0.325, "physical_size_y_unit": "µm"}

    python_copy = tmp_path / "python.ome.tiff"
    image_pyramid.pyramidize_image(source, python_copy, channel_names=names, physical=physical,
                                   tile_size=TILE)
    spec = image_pyramid.page_build(source, channel_names=names, physical=physical, tile_size=TILE)
    assert spec is not None
    page_copy = tmp_path / "page.ome.tiff"
    # Small bands, so a stripped source is cut into many and every cut is checked.
    record = _page_copy(source, spec, page_copy, job_bytes=96 * 1024)

    want, want_axes = _levels(python_copy)
    got, got_axes = _levels(page_copy)
    assert got_axes == want_axes  # YX for one channel, as tifffile reads it
    assert len(got) == len(want) == len(spec["factors"])
    for level, (a, b) in enumerate(zip(want, got)):
        assert b.dtype == a.dtype and b.shape == a.shape, level
        assert np.array_equal(a, b), f"level {level}: {int((a != b).sum())} pixels differ"
    assert _ome(page_copy) == _ome(python_copy)
    assert image_pyramid._description(page_copy) == image_pyramid._description(python_copy)
    # A later import adopts either copy (a single channel's too: tifffile reads
    # its copy as YX, which once made every one-channel copy be rebuilt).
    assert image_pyramid.is_adoptable_image(page_copy, source)
    assert image_pyramid.is_adoptable_image(python_copy, source)
    assert record["tiles"] > 0


def test_the_tiles_are_the_layout_python_writes(tmp_path):
    """Square deflated tiles, edge tiles padded, one main IFD per channel with
    its levels as SubIFDs: what node/tiffReader.js and the viewer expect."""
    dtype, shape, options = CASES["uint16_lzw_strips"]
    source = tmp_path / "s.tif"
    _write_source(source, dtype, shape, options, seed=3)
    spec = image_pyramid.page_build(source, tile_size=TILE)
    page_copy = tmp_path / "page.ome.tiff"
    _page_copy(source, spec, page_copy, job_bytes=16 << 20)
    with tifffile.TiffFile(page_copy) as tiff:
        assert tiff.is_bigtiff and tiff.byteorder == "<"
        assert len(tiff.pages) == shape[0]
        for page in tiff.pages:
            assert page.is_tiled and (page.tilewidth, page.tilelength) == (TILE, TILE)
            assert page.compression == 8 and page.photometric == 1
            assert len(page.subifds) == len(spec["factors"]) - 1


@pytest.mark.parametrize("make, why", [
    (lambda p: tifffile.imwrite(p, np.zeros((40, 50, 3), np.uint8), photometric="rgb"), "RGB samples"),
    (lambda p: tifffile.imwrite(p, np.zeros((40, 50), np.complex64)), "complex samples"),
])
def test_what_the_page_would_read_differently_is_left_to_python(tmp_path, make, why):
    source = tmp_path / "s.tif"
    make(source)
    assert image_pyramid.page_build(source) is None, why


# -- a flat segmentation mask's label pyramid ---------------------------------
#
# The same builder with `kind: "labels"`: one plane, each level sampled from
# level 0 (segmentation_pyramid's FILLED mode), the samples written as they
# are. Held to `pyramidize_segmentation_mask(outline=False)` the same way, and
# to what `_is_adoptable`, `generated_mask_kind` and `generated_mask_scale`
# read back, since a later load finds the pyramid by those alone.

def _labels(dtype, shape, seed):
    """Cells as blocks of one id over noise of single-pixel ids: sampling at
    the wrong pixel shows in the noise, a wrong block edge in the blocks."""
    rng = np.random.default_rng(seed)
    dtype = np.dtype(dtype)
    info = np.iinfo(dtype)
    low = max(int(info.min), -(2 ** 31))
    out = rng.integers(low, int(info.max), size=shape, endpoint=True, dtype=np.int64)
    height, width = shape
    for _ in range(60):
        y, x = int(rng.integers(0, height)), int(rng.integers(0, width))
        h, w = int(rng.integers(3, 90)), int(rng.integers(3, 90))
        out[y:y + h, x:x + w] = int(rng.integers(low, int(info.max), endpoint=True))
    out[rng.random(shape) < 0.25] = 0  # background
    if dtype.kind == "u" and dtype.itemsize == 4:
        out[0, 0] = info.max  # the largest id a mask can hold
        out[-1, -1] = 2 ** 24 + 1  # past float32's whole numbers
    return out.astype(dtype)


MASKS = {
    # name: (dtype, (H, W), tifffile write options)
    "uint32_tiled_zlib": ("uint32", (1037, 1529), {"compression": "zlib", "tile": (128, 128)}),
    "uint32_one_strip_raw": ("uint32", (700, 1100), {"rowsperstrip": 700}),
    "uint16_lzw_strips": ("uint16", (777, 601), {"compression": "lzw", "rowsperstrip": 37}),
    "uint16_zlib_predictor": ("uint16", (530, 610), {"compression": "zlib", "predictor": True,
                                                     "rowsperstrip": 64}),
    "int32_tiled_raw": ("int32", (513, 769), {"tile": (64, 64)}),
    "uint8_packbits_strips": ("uint8", (300, 1301), {"compression": "packbits", "rowsperstrip": 16}),
    "int16_raw_strips": ("int16", (257, 255), {}),
}


def _write_mask(path, dtype, shape, options, seed):
    data = _labels(dtype, shape, seed)
    tifffile.imwrite(path, data, photometric="minisblack", **options)
    return data


def _mask_copies(tmp_path, name, job_bytes=96 * 1024):
    from plexora.server.utils import segmentation_pyramid

    dtype, shape, options = MASKS[name]
    source = tmp_path / f"{name}.tif"
    data = _write_mask(source, dtype, shape, options, seed=len(name))
    python_copy = tmp_path / "python.labels.pyramid.ome.tiff"
    segmentation_pyramid.pyramidize_segmentation_mask(source, python_copy, tile_size=TILE,
                                                      outline=False, overwrite=True)
    spec = segmentation_pyramid.page_build(source, tile_size=TILE)
    assert spec is not None and spec["kind"] == "labels"
    assert spec["shape"] == [1, *shape] and spec["dtype"] == dtype
    page_copy = tmp_path / "page.labels.pyramid.ome.tiff"
    record = _page_copy(source, spec, page_copy, job_bytes=job_bytes)
    return source, data, python_copy, page_copy, spec, record


@pytest.mark.parametrize("name", sorted(MASKS))
def test_the_page_writes_the_label_pyramid_python_writes(tmp_path, name):
    from plexora.server.utils import segmentation_pyramid as sp

    source, data, python_copy, page_copy, spec, record = _mask_copies(tmp_path, name)
    want, want_axes = _levels(python_copy)
    got, got_axes = _levels(page_copy)
    assert got_axes == want_axes == "YX"
    assert len(got) == len(want) == len(spec["factors"]) > 1
    for level, (a, b) in enumerate(zip(want, got)):
        assert b.dtype == a.dtype == data.dtype and b.shape == a.shape, level
        assert np.array_equal(a, b), f"level {level}: {int((a != b).sum())} pixels differ"
        # Every id at a level is an id of level 0's, at the sampled pixel.
        factor = spec["factors"][level]
        assert np.array_equal(b, data[::factor, ::factor]), level
    assert _ome(page_copy) == _ome(python_copy)
    # What a later load reads back to find it, adopt it and draw it.
    assert sp.generated_mask_kind(page_copy) == sp.generated_mask_kind(python_copy) == sp.MODE_FILLED
    assert sp.generated_mask_scale(page_copy) is sp.generated_mask_scale(python_copy) is None
    assert sp.plane_size(page_copy) == sp.plane_size(python_copy) == (data.shape[1], data.shape[0])
    for copy in (page_copy, python_copy):
        assert sp._is_adoptable(copy, source, sp.MODE_FILLED)
        assert sp.is_adoptable_mask(copy, source)
        assert not sp._is_adoptable(copy, source, sp.MODE_OUTLINES)
        assert sp.is_servable_label_pyramid(copy)
    assert record["tiles"] > 0


def test_the_label_tiles_are_the_layout_python_writes(tmp_path):
    """One plane, square deflated tiles, its levels as SubIFDs, the samples'
    own format: what node/tiffReader.js and the browser node's labels expect."""
    source, data, python_copy, page_copy, spec, _ = _mask_copies(tmp_path, "int32_tiled_raw",
                                                                 job_bytes=16 << 20)
    with tifffile.TiffFile(page_copy) as page, tifffile.TiffFile(python_copy) as python:
        assert page.is_bigtiff and page.byteorder == "<"
        assert len(page.pages) == len(python.pages) == 1
        mine, theirs = page.pages[0], python.pages[0]
        assert mine.is_tiled and (mine.tilewidth, mine.tilelength) == (TILE, TILE)
        assert mine.compression == 8 and mine.photometric == 1
        assert (mine.sampleformat, mine.bitspersample) == (theirs.sampleformat, theirs.bitspersample)
        assert len(mine.subifds) == len(theirs.subifds) == len(spec["factors"]) - 1


@pytest.mark.parametrize("make, why", [
    (lambda p: tifffile.imwrite(p, np.zeros((40, 50), np.float32)), "float labels: Python refuses them"),
    (lambda p: tifffile.imwrite(p, np.zeros((40, 50), np.uint64)), "64-bit ids: Python narrows them"),
    (lambda p: tifffile.imwrite(p, np.zeros((40, 50, 3), np.uint8), photometric="rgb"), "a colour picture"),
    (lambda p: tifffile.imwrite(p, np.zeros((2, 40, 50), np.uint16)), "two planes: Python refuses them"),
    (lambda p: p.write_bytes(b"not a tiff"), "not a TIFF at all"),
])
def test_a_mask_the_page_would_read_differently_is_left_to_python(tmp_path, make, why):
    from plexora.server.utils import segmentation_pyramid

    source = tmp_path / "m.tif"
    make(source)
    assert segmentation_pyramid.page_build(source) is None, why


def test_a_label_pyramid_is_described_as_tifffile_describes_it(tmp_path):
    """The page writes the OME-XML Python's writer would: only the UUID that
    tifffile makes afresh for every file differs."""
    import re

    from plexora.server.utils import segmentation_pyramid

    source = tmp_path / "m.tif"
    _write_mask(source, "uint32", (600, 700), {}, seed=1)
    python_copy = tmp_path / "python.labels.pyramid.ome.tiff"
    segmentation_pyramid.pyramidize_segmentation_mask(source, python_copy, tile_size=TILE,
                                                      outline=False, overwrite=True)
    with tifffile.TiffFile(python_copy) as tiff:
        written = tiff.pages[0].description
    spec = segmentation_pyramid.page_build(source, tile_size=TILE)
    drop = re.compile(r' UUID="[^"]*"')
    assert drop.sub("", spec["description"]) == drop.sub("", written)


@pytest.mark.parametrize("recorded, current, same", [
    ("1000-1730000000123456700", "1000-1730000000123456700", True),   # the same machine
    ("1000-1730000000123000000", "1000-1730000000123456700", True),   # the website (ms) vs the desktop (100 ns)
    ("1000-1730000000124000000", "1000-1730000000123456700", True),   # rounded rather than truncated
    ("1000-1730000000125000000", "1000-1730000000123456700", False),  # a later save
    ("1001-1730000000123456700", "1000-1730000000123456700", False),  # another size
    (None, "1000-1", False),
])
def test_a_copy_made_on_the_website_is_the_desktops_and_back(recorded, current, same):
    """A page sees a file's time in whole milliseconds: a copy made by the
    website from a file is still a copy of that file to the desktop app."""
    assert image_pyramid._same_source(recorded, current) is same


# -- a pool of the page's readers opens a file once ----------------------------
#
# services/browserNode.js opens a project's file in several workers; the first
# reads the header and IFDs from the file (on the desktop app, a chain of byte
# ranges through the shell's scheme) and records them, and the rest open from
# that record (node/tiffReader.js `seed`). Held here to: the seeded open reads
# nothing, finds the same geometry, and every tile it reads is the same.

SEED_PROBE = ROOT / "tests" / "js" / "tiff_seed_probe.mjs"


def _seed_probe(path, ifds=None):
    extra = [json.dumps(ifds)] if ifds else []
    done = subprocess.run([NODE, str(SEED_PROBE), str(path), *extra], capture_output=True, text=True,
                          encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _subifd_pyramid(tmp_path):
    source = tmp_path / "s.tif"
    _write_source(source, "uint16", (3, 1037, 1529), {"compression": "zlib", "tile": (128, 128)}, seed=5)
    out = tmp_path / "pyramid.ome.tiff"
    image_pyramid.pyramidize_image(source, out, channel_names=["a", "b", "c"], tile_size=TILE)
    return out


def _series_pyramid(tmp_path):
    out = tmp_path / "series.tif"
    data = _pixels("uint16", (2, 700, 900), seed=6)
    with tifffile.TiffWriter(out, bigtiff=True) as tiff:
        for level in range(3):
            step = 2 ** level
            for channel in data[:, ::step, ::step]:
                tiff.write(channel, tile=(128, 128), compression="zlib", photometric="minisblack")
    return out


def _flat(tmp_path):
    out = tmp_path / "flat.tif"
    _write_source(out, "uint16", (4, 300, 500), {"compression": "lzw", "rowsperstrip": 16}, seed=7)
    return out


@pytest.mark.parametrize("make, layout", [(_subifd_pyramid, "subifd"), (_series_pyramid, "series"),
                                          (_flat, "flat")])
def test_a_pool_opens_its_file_once(tmp_path, make, layout):
    from plexora.browser import node_spec

    path = make(tmp_path)
    ifds = node_spec.main_ifds(str(path))
    with tifffile.TiffFile(path) as tif:
        assert ifds == [page.offset for page in tif.pages] and ifds[0] == 8 + 8 * tif.is_bigtiff
    found = _seed_probe(path, ifds)
    assert found["layout"] == layout
    assert found["first"]["reads"] > 0 and found["first"]["recorded"] == found["first"]["reads"]
    assert found["seeded"]["reads"] == 0, "the seeded open read the file"
    assert found["geometrySame"]
    assert found["tiles"] > 0 and found["differ"] == 0
    # Python's offsets are the chain, so they are taken; a spoiled list is
    # not, and the chain is walked instead: the same file either way.
    hints = found["hints"]
    assert hints["right"] == {"hinted": True, "geometrySame": True, "tileSame": True}
    assert len(ifds) > 2 and hints["spoiled"]["hinted"] is False
    assert hints["wrongFirst"]["hinted"] is False
    for name in ("spoiled", "wrongFirst"):
        assert hints[name]["geometrySame"] and hints[name]["tileSame"], name


def test_the_ifd_offsets_are_kept_until_the_file_changes(tmp_path):
    from plexora.browser import node_spec

    path = _flat(tmp_path)
    first = node_spec.main_ifds(str(path))
    assert len(first) == 4
    assert node_spec.main_ifds(str(path)) is first
    _write_source(path, "uint16", (2, 300, 500), {"compression": "lzw", "rowsperstrip": 16}, seed=8)
    again = node_spec.main_ifds(str(path))
    assert len(again) == 2
    assert node_spec.main_ifds(str(tmp_path / "missing.tif")) is None
    (tmp_path / "not.tif").write_bytes(b"not a tiff")
    assert node_spec.main_ifds(str(tmp_path / "not.tif")) is None
