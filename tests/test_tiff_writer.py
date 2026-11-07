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
               "jobBytes": job_bytes}
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
