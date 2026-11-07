"""An .ims reads the same without hdf5plugin (plan L4).

hdf5plugin is C with no WebAssembly build, so the website's Python has none,
and a desktop install may lack it too. Without it HDF5 has no LZ4 or zstd
filter, and h5py cannot read an Imaris 10 file's LZ4 chunks -- but it still
hands over a chunk's stored bytes, which ims.py decodes itself (imagecodecs'
lz4h5_decode and zstd_decode; on the website imagecodecs_shim's).

Pinned, in a Python where `import hdf5plugin` fails (a subprocess, since a
filter once registered stays registered for the process): every level of
every channel, a plane pyramid's window, an LZ4 mask's ids and the import's
verdict on it read exactly what they read with hdf5plugin; and h5py alone
really cannot read those files there, so the reads went through ims.py's
own decode.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

from tests import volume_fixtures as vf

hdf5plugin = pytest.importorskip("hdf5plugin")
ROOT = Path(__file__).resolve().parents[1]


def _sha(array) -> str:
    array = np.ascontiguousarray(array)
    return f"{array.dtype}:{array.shape}:" + hashlib.sha256(array.tobytes()).hexdigest()


def _write_mask(path, compression):
    """One channel of ids (uint16, two levels), padded and chunked as Imaris
    writes, through `compression`."""
    cells = np.zeros((7, 61, 77), np.uint16)
    cells[:, 5:30, 5:30] = 1
    cells[1:5, 35:58, 10:50] = 2
    cells[0:3, 10:55, 60:75] = 3
    with h5py.File(str(path), "w") as handle:
        info = handle.create_group("DataSetInfo/Image")
        for key, value in (("X", 77), ("Y", 61), ("Z", 7), ("Unit", "um"), ("ExtMin0", 0),
                           ("ExtMin1", 0), ("ExtMin2", 0), ("ExtMax0", 38.5), ("ExtMax1", 30.5),
                           ("ExtMax2", 14.0)):
            info.attrs[key] = vf._attr(value)
        handle.create_group("DataSetInfo/Channel 0").attrs["Name"] = vf._attr("(name not specified)")
        for level in range(2):
            volume = cells[:, ::2 ** level, ::2 ** level]
            d, h, w = volume.shape
            padded = np.zeros((d, h + 5, w + 3), np.uint16)
            padded[:, :h, :w] = volume
            group = handle.create_group(f"DataSet/ResolutionLevel {level}/TimePoint 0/Channel 0")
            group.create_dataset("Data", data=padded, chunks=(4, 32, 32), **compression)
            for key, value in (("ImageSizeX", w), ("ImageSizeY", h), ("ImageSizeZ", d)):
                group.attrs[key] = vf._attr(value)
    return path


#: Read by both Pythons: what a viewer, an import and a mask conversion read.
READS = r'''
import hashlib, json, sys
import numpy as np


def sha(array):
    array = np.ascontiguousarray(array)
    return f"{array.dtype}:{array.shape}:" + hashlib.sha256(array.tobytes()).hexdigest()


def reads(stacks, masks):
    import h5py
    from plexora.server.volume.sources import ims

    out = {"lz4_filter": bool(h5py.h5z.filter_avail(32004)),
           "zstd_filter": bool(h5py.h5z.filter_avail(32015)), "files": {}}
    for path in stacks:
        source = ims.open_ims_volume(path)
        got = {"levels": [(lv["width"], lv["height"], lv["depth"]) for lv in source._levels],
               "dtype": str(source.volume.dtype)}
        for level, info in enumerate(source._levels):
            for channel in range(source.volume.channels):
                got[f"L{level}C{channel}"] = sha(source.read_block(
                    level, 0, channel, 0, info["depth"] - 1, slice(0, info["height"]),
                    slice(0, info["width"])))
        got["plane"] = sha(np.asarray(source.plane_pyramid(0, 3)[0][1, 7:41, 5:33]))
        source.close()
        with h5py.File(path, "r") as handle:
            try:
                handle["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0/Data"][0, :4, :4]
                got["h5py_alone"] = "reads"
            except OSError:
                got["h5py_alone"] = "cannot"
        out["files"][path] = got
    for path in masks:
        labels = ims.ImsLabels(path)
        got = {"shape": list(labels.shape), "verdict": ims.mask_verdict(path),
               "planes": [sha(labels.plane(0, z)) for z in range(labels.depth)],
               "window": sha(labels.plane(0, 2, slice(3, 40), slice(9, 70)))}
        labels.close()
        out["files"][path] = got
    return out
'''


def _without_plugin(stacks, masks) -> dict:
    code = ("import sys\nsys.modules['hdf5plugin'] = None\n" + READS
            + "\nprint(json.dumps(reads(json.loads(sys.argv[1]), json.loads(sys.argv[2]))))\n")
    done = subprocess.run([sys.executable, "-c", code, json.dumps(stacks), json.dumps(masks)],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _with_plugin(stacks, masks) -> dict:
    scope = {}
    exec(READS, scope)  # noqa: S102 -- the same reads, in this (hdf5plugin) Python
    return json.loads(json.dumps(scope["reads"](stacks, masks)))


def test_an_ims_reads_the_same_without_hdf5plugin(tmp_path):
    stacks = []
    for name, compression, shuffle, dtype in (
            ("lz4.ims", hdf5plugin.LZ4(), True, np.uint16),
            ("lz4_u8.ims", hdf5plugin.LZ4(), False, np.uint8),
            ("zstd.ims", hdf5plugin.Zstd(), True, np.uint16),
            ("deflate.ims", "gzip", True, np.uint16)):
        path, _ = vf.write_ims(tmp_path / name, channels=2, depth=9, height=70, width=53,
                               levels=3, compression=compression, shuffle=shuffle,
                               chunks=(4, 32, 16), z_levels=True)
        if dtype == np.uint8:
            # The same layout with 8-bit samples: rewritten from the 16-bit one.
            with h5py.File(path, "a") as handle:
                for level in range(3):
                    for channel in range(2):
                        group = handle[f"DataSet/ResolutionLevel {level}/TimePoint 0/Channel {channel}"]
                        data = (group["Data"][...] % 251).astype(np.uint8)
                        chunks = group["Data"].chunks
                        del group["Data"]
                        group.create_dataset("Data", data=data, chunks=chunks, compression=compression)
        stacks.append(str(path))
    masks = [str(_write_mask(tmp_path / "mask_lz4.ims", {"compression": hdf5plugin.LZ4()}))]

    want = _with_plugin(stacks, masks)
    got = _without_plugin(stacks, masks)

    assert want["lz4_filter"] and want["zstd_filter"]
    assert not got["lz4_filter"] and not got["zstd_filter"], "hdf5plugin was loaded after all"
    for path in stacks + masks:
        mine, theirs = got["files"][path], want["files"][path]
        if "h5py_alone" in mine:
            name = Path(path).name
            # Without the plugin h5py cannot read LZ4 or zstd: ims.py did.
            assert mine.pop("h5py_alone") == ("reads" if name == "deflate.ims" else "cannot"), name
            assert theirs.pop("h5py_alone") == "reads"
        assert mine == theirs, path
    assert want["files"][masks[0]]["verdict"] is True


def test_zstd_is_decoded_here_only_without_the_plugin():
    """A desktop with hdf5plugin reads zstd through h5py, as it always did."""
    from plexora.server.volume.sources import ims

    assert h5py.h5z.filter_avail(ims._ZSTD)
    assert not ims._zstd_here()
    assert not ims._readable([ims._SHUFFLE, ims._ZSTD])
    assert ims._readable([ims._SHUFFLE, ims._DEFLATE]) and ims._readable([])
    assert not ims._readable([ims._DEFLATE, ims._DEFLATE])
