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
import sys
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


def write_main_chain(path, stack, levels=4, names=None, **options):
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
            + "".join(f'<Channel ID="Channel:0:{i}" Name="{(names or {}).get(i, f"C{i}")}" SamplesPerPixel="1"/>'
                      for i in range(channels))
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


def test_the_first_view_names_the_files_channels(tmp_path):
    """The frame's channel list before the viewer has one: SizeC and each
    Channel's Name from the OME-XML, entities decoded, in both layouts."""
    chain = tmp_path / "named-chain.ome.tif"
    write_main_chain(chain, _stack(3, 128, 128, np.uint16, seed=3), levels=2,
                     names={0: "01_Nucleus_Hoechst", 1: "CD45 &amp; co"})
    subifds = tmp_path / "named-subifds.ome.tif"
    pyramid = _levels(_stack(2, 128, 128, np.uint16, seed=4), 2)
    with tifffile.TiffWriter(subifds, bigtiff=True, ome=True) as tif:
        tif.write(pyramid[0], subifds=1, tile=(64, 64),
                  metadata={"axes": "CYX", "Channel": {"Name": ["DNA", "Ki-67"]}})
        tif.write(pyramid[1], subfiletype=1, tile=(64, 64))
    got_chain, got_subifds = _probe(chain, subifds)
    assert got_chain["channels"] == {"count": 3, "names": ["01_Nucleus_Hoechst", "CD45 & co", "C2"]}
    assert got_subifds["channels"] == {"count": 2, "names": ["DNA", "Ki-67"]}


# -- slides and OME-Zarr -----------------------------------------------------------

def _probe_args(*args):
    done = subprocess.run(["node", str(PROBE), *map(str, args)], cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_slide_is_previewed_from_its_thumbnail_never_its_label(tmp_path):
    """SVS's directories: the baseline, a thumbnail of its shape, a label of
    another shape that is smaller still, a macro. The preview is the smallest
    of the image's own shape -- the thumbnail -- read as tifffile reads it."""
    from tests.slide_fixtures import write_svs_like

    path = tmp_path / "slide.svs"
    pictures = write_svs_like(path, jpeg=False)
    [got] = _probe(path)
    assert "error" not in got, got
    assert (got["height"], got["width"], got["samples"]) == pictures["thumbnail"].shape
    assert got["sha256"] == _sha(pictures["thumbnail"])


def test_a_jpeg_slides_tiles_are_handed_to_the_decoder_whole(tmp_path):
    """Aperio's JPEG tiles are abbreviated -- the tables are in JPEGTables --
    and RGB with no colour marker. The stream the page hands the browser's
    decoder has both: decoded (here by Pillow, as libjpeg does in the
    browser) it is the strip's own pixels, not a YCbCr reading of them."""
    from PIL import Image

    from tests.slide_fixtures import write_svs_like

    path = tmp_path / "slide.svs"
    pictures = write_svs_like(path, jpeg=True)
    got = _probe_args("--jpeg", path)
    assert "error" not in got, got
    assert (got["compression"], got["photometric"]) == (7, 2)
    assert (got["height"], got["width"]) == pictures["thumbnail"].shape[:2]
    import base64
    from io import BytesIO

    stream = base64.b64decode(got["stream"])
    assert stream[:4] == b"\xff\xd8\xff\xee"          # the Adobe marker, first
    with Image.open(BytesIO(stream)) as decoded:
        strip = np.asarray(decoded.convert("RGB")).astype(int)
    expected = pictures["thumbnail"][:strip.shape[0]].astype(int)
    assert strip.shape == expected.shape
    assert np.abs(strip - expected).mean() < 4
    # Without the page's marker the same tables and scan read as YCbCr.
    bare = b"\xff\xd8" + stream[2 + 16:]
    with Image.open(BytesIO(bare)) as decoded:
        misread = np.asarray(decoded.convert("RGB")).astype(int)
    assert np.abs(misread - expected).mean() > 20


def test_a_qptiff_is_previewed_from_its_reduced_level_with_its_biomarkers(tmp_path):
    from tests.slide_fixtures import write_qptiff_like

    path = tmp_path / "panel.qptiff"
    _, reduced = write_qptiff_like(path, names=("DAPI", "CD8", "PanCK"))
    [got] = _probe(path)
    assert "error" not in got, got
    assert (got["height"], got["width"]) == reduced.shape[1:]
    assert got["sha256"] == _sha(reduced[0])
    assert got["channels"] == {"count": 3, "names": ["DAPI", "CD8", "PanCK"]}


@pytest.mark.parametrize("version", ["0.4", "0.5"])
def test_an_ome_zarr_folder_names_its_smallest_level_and_its_channels(tmp_path, version):
    """What the page reads of a picked OME-Zarr folder before the reader
    worker reads the level: the path and shape of the smallest level at
    least 512 a side (the largest when none is), the axes, the omero
    labels -- zarr v2 and v3, and bioformats2raw's layout."""
    from tests.ngff_fixtures import write_bioformats2raw_like, write_ngff

    store = write_ngff(tmp_path / "image.ome.zarr", shape=(3, 256, 256), levels=3, version=version,
                       labels=["DAPI", "CD3", "CD20"])
    got = _probe_args("--zarr", store)
    # No level reaches 512 a side: the largest, 256.
    assert got == {"path": "0", "shape": [3, 256, 256], "format": 2 if version == "0.4" else 3,
                   "axes": ["c", "y", "x"], "channels": {"count": 3, "names": ["DAPI", "CD3", "CD20"]}}

    raw = write_bioformats2raw_like(tmp_path / "raw.zarr", version=version, shape=(2, 128, 128), levels=2,
                                    axes="cyx")
    got = _probe_args("--zarr", raw)
    assert got["path"] == "0/0" and got["shape"] == [2, 128, 128]
    assert got["channels"]["count"] == 2

    # A pyramid down to 64 a side: the level above it that reaches 512.
    deep = write_ngff(tmp_path / "deep.ome.zarr", shape=(1, 2048, 2048), levels=6, version=version)
    got = _probe_args("--zarr", deep)
    assert got["path"] == "2" and got["shape"] == [1, 512, 512]

    assert _probe_args("--zarr", tmp_path / "missing.zarr") is None


needs_sparse = pytest.mark.skipif(sys.platform == "win32",
                                  reason="a 4 GB file is sparse here only where the file system makes it so")


@needs_sparse
def test_an_ndpi_past_4_gb_is_read_at_its_64_bit_offsets(tmp_path):
    """NDPI keeps 64-bit offsets in a classic TIFF: the high half of each
    entry's value after the directory. A reduced level and its directory past
    4 GB (a sparse hole before them) are read where tifffile reads them."""
    import base64
    from io import BytesIO

    from PIL import Image

    from tests.slide_fixtures import write_ndpi_like

    raw = tmp_path / "raw.ndpi"
    pictures = write_ndpi_like(raw, beyond=2**32 + 999, jpeg=False)
    with tifffile.TiffFile(raw) as tif:
        assert tif.is_ndpi and tif.pages[2].dataoffsets[0] > 2**32
    [got] = _probe(raw)
    assert "error" not in got, got
    assert (got["height"], got["width"], got["samples"]) == pictures["level1"].shape
    assert got["sha256"] == _sha(pictures["level1"])

    jpeg = tmp_path / "jpeg.ndpi"
    write_ndpi_like(jpeg, beyond=2**32 + 12345, jpeg=True)
    got = _probe_args("--jpeg", jpeg)
    assert "error" not in got, got
    with tifffile.TiffFile(jpeg) as tif:
        expected = tif.pages[2].asarray().astype(int)
    with Image.open(BytesIO(base64.b64decode(got["stream"]))) as decoded:
        strip = np.asarray(decoded.convert("RGB")).astype(int)
    assert strip.shape == expected.shape
    assert np.abs(strip - expected).mean() < 2


def test_an_ome_zarr_folder_is_one_whatever_its_name(tmp_path):
    from tests.ngff_fixtures import write_ngff

    store = write_ngff(tmp_path / "sample_7", shape=(2, 128, 128), levels=2, labels=["DAPI", "CD8"])
    got = _probe_args("--zarr", store)
    assert got["shape"] == [2, 128, 128] and got["channels"]["names"] == ["DAPI", "CD8"]


def test_a_volumes_level_is_named_with_its_depth(tmp_path):
    """The reader worker is asked for the middle plane of a z-stack (the plane
    the viewer opens on); what the page reads of the store says its depth."""
    from tests.ngff_fixtures import write_ngff

    store = write_ngff(tmp_path / "volume.ome.zarr", shape=(2, 9, 64, 64), axes="czyx", levels=1)
    got = _probe_args("--zarr", store)
    assert got["axes"] == ["c", "z", "y", "x"] and got["shape"] == [2, 9, 64, 64]


def test_a_remote_stores_keys_are_fetched_where_the_websites_python_reads_them():
    got = _probe_args("--address",
                      "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4/idr0062A/6001240.zarr/.zattrs",
                      "s3://lin-2023-orion-crc/data/P37 S29/image.ome.zarr/0/.zarray",
                      "gs://idc-open-data/x/y.zarr/zarr.json",
                      "https://host/a.zarr/.zattrs?X-Amz-Signature=1",
                      "sftp://host/a.zarr/.zattrs")
    assert got == [
        "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4/idr0062A/6001240.zarr/.zattrs",
        "https://lin-2023-orion-crc.s3.amazonaws.com/data/P37%20S29/image.ome.zarr/0/.zarray",
        "https://storage.googleapis.com/storage/v1/b/idc-open-data/o/x%2Fy.zarr%2Fzarr.json?alt=media",
        None,
        None,
    ]
