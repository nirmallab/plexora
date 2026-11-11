"""The page's first view of a picked TIFF (services/firstView.js) reads the
same pixels tifffile does.

The home page shows the first channel's smallest pyramid level the moment a
file is given, read by the page itself in a few reads -- for a pyramid in
SubIFDs from directory 0's list; for one in the main chain by computing each
level's first directory from the stride of the one above and reading it to be
sure. Run in Node (tests/js/first_view_probe.mjs) on files tifffile writes in
both layouts, uncompressed, deflated (with and without the horizontal
predictor) and LZW, 8- and 16-bit; its samples are held against tifffile's
reading of the same level, byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "tests" / "js" / "first_view_probe.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _stack(channels, height, width, dtype, seed):
    rng = np.random.default_rng(seed)
    top = 250 if dtype == np.uint8 else 60000
    return rng.integers(0, top, size=(channels, height, width), dtype=dtype)


def _levels(stack, count):
    out = [stack]
    for _ in range(count - 1):
        out.append(out[-1][:, ::2, ::2].copy())
    return out


def write_subifds(path, stack, levels=3, **options):
    """An OME-TIFF with its pyramid in SubIFDs, as Bio-Formats writes one."""
    pyramid = _levels(stack, levels)
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as tif:
        tif.write(pyramid[0], subifds=levels - 1, tile=(64, 64), metadata={"axes": "CYX"}, **options)
        for level in pyramid[1:]:
            tif.write(level, subfiletype=1, tile=(64, 64), **options)
    return pyramid[-1][0]


def write_main_chain(path, stack, levels=4, **options):
    """Every level's planes one after another in the main chain, the OME-XML
    describing only the first level's (SizeC planes), each directory beside
    its own plane -- the layout of the 106 GB slide that took minutes."""
    pyramid = _levels(stack, levels)
    with tifffile.TiffWriter(path, bigtiff=True) as tif:
        channels, height, width = stack.shape
        description = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
            '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" '
            f'Type="{np.dtype(stack.dtype).name}" SizeX="{width}" SizeY="{height}" '
            f'SizeC="{channels}" SizeZ="1" SizeT="1">'
            + "".join(f'<Channel ID="Channel:0:{i}" SamplesPerPixel="1"/>' for i in range(channels))
            + "</Pixels></Image></OME>")
        for index, level in enumerate(pyramid):
            for channel in range(channels):
                tif.write(level[channel], tile=(64, 64), contiguous=False,
                          description=description if index == 0 and channel == 0 else None,
                          metadata=None, **options)
    return pyramid[-1][0]


def _probe(*paths):
    done = subprocess.run(["node", str(PROBE), *map(str, paths)], cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return [json.loads(line) for line in done.stdout.strip().splitlines()]


def _sha(plane):
    return hashlib.sha256(np.ascontiguousarray(plane).astype(plane.dtype.newbyteorder("<")).tobytes()).hexdigest()


CASES = [
    ("subifds-none-u16", write_subifds, np.uint16, {}),
    ("subifds-deflate-u16", write_subifds, np.uint16, {"compression": "zlib"}),
    ("subifds-deflate-predictor-u16", write_subifds, np.uint16, {"compression": "zlib", "predictor": True}),
    ("subifds-lzw-u16", write_subifds, np.uint16, {"compression": "lzw"}),
    ("subifds-lzw-predictor-u8", write_subifds, np.uint8, {"compression": "lzw", "predictor": True}),
    ("chain-none-u16", write_main_chain, np.uint16, {}),
    ("chain-deflate-u16", write_main_chain, np.uint16, {"compression": "zlib"}),
]


@pytest.mark.parametrize("name,writer,dtype,options", CASES, ids=[case[0] for case in CASES])
def test_the_first_view_reads_what_tifffile_reads(tmp_path, name, writer, dtype, options):
    if options.get("compression") == "lzw":
        pytest.importorskip("imagecodecs")
    path = tmp_path / f"{name}.ome.tif"
    expected = writer(path, _stack(5, 300, 420, dtype, seed=len(name)), **options)
    [got] = _probe(path)
    assert "error" not in got, got
    assert (got["height"], got["width"]) == expected.shape
    assert got["sha256"] == _sha(expected)


def test_a_main_chain_pyramid_is_found_level_by_level_not_plane_by_plane(tmp_path):
    """The stride of each level's first two planes gives the next level's
    first directory: a handful of reads, not one a plane, and every
    directory's offset kept for the worker's prefetch."""
    path = tmp_path / "chain.ome.tif"
    channels, levels = 12, 4
    write_main_chain(path, _stack(channels, 512, 512, np.uint16, seed=7), levels=levels)
    [got] = _probe(path)
    assert got["layout"] == "chain-strided"
    with tifffile.TiffFile(path, is_ome=False) as tif:
        assert len(tif.pages) == channels * levels
        offsets = [page.offset for page in tif.pages]
    # Every directory the file has is among those handed to the worker.
    assert got["offsets"] >= len(offsets)
    # Far fewer 64 KiB pieces than directories walked one by one would need.
    assert got["reads"] < channels * levels


def test_a_file_it_cannot_read_says_so(tmp_path):
    path = tmp_path / "not.tif"
    path.write_bytes(b"this is not a tiff at all")
    [got] = _probe(path)
    assert "error" in got
