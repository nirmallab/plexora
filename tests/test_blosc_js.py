"""The page's own Blosc and LZ4 decoders (client src/js/node/blosc.js) read
what numcodecs writes.

numcodecs.js -- the Blosc the website's OME-Zarr reader used -- makes
functions from strings, which the website's Content Security Policy refuses,
so no Blosc chunk could be read in the page there. blosc.js reads the frames
OME-Zarr stores hold in plain JavaScript and declines the rest (BloscLZ,
bit-shuffle), which numcodecs.js then reads where it may run. Every chunk
here is numcodecs' (Python) own encoding: each must come out as the bytes it
was, or be declined -- never come out otherwise -- and everything a store's
default writers produce (LZ4, LZ4HC, Zstandard, zlib; no shuffle or byte
shuffle; short last blocks; split and unsplit) must be read.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

numcodecs = pytest.importorskip("numcodecs")

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "blosc_probe.mjs"
ZSTDDEC = ROOT / "plexora" / "client" / "node_modules" / "zstddec"

pytestmark = pytest.mark.skipif(shutil.which("node") is None or not ZSTDDEC.is_dir(),
                                reason="needs node and the client's node_modules")


def _values(n, dtype, rng):
    base = (np.sin(np.arange(n) / 37.0) * 1000 + 1500).astype(dtype)
    base[::7] += rng.integers(0, 50, size=base[::7].shape).astype(dtype)
    return base


def test_blosc_js_reads_what_numcodecs_writes(tmp_path):
    rng = np.random.default_rng(1)
    cases = []
    for cname in ("lz4", "lz4hc", "zstd", "zlib", "blosclz"):
        for shuffle in (numcodecs.Blosc.NOSHUFFLE, numcodecs.Blosc.SHUFFLE, numcodecs.Blosc.BITSHUFFLE):
            for dtype in ("u1", "<u2", "<u4", "<f8"):
                for n, blocksize in ((5, 0), (100, 0), (70000, 0), (300001, 0), (50000, 4096)):
                    values = _values(n, np.dtype(dtype), rng)
                    name = f"c{len(cases)}"
                    codec = numcodecs.Blosc(cname=cname, clevel=5, shuffle=shuffle, blocksize=blocksize)
                    (tmp_path / f"{name}.bin").write_bytes(codec.encode(values))
                    (tmp_path / f"{name}.raw").write_bytes(values.tobytes())
                    cases.append({"name": name, "kind": "blosc", "cname": cname, "shuffle": shuffle})
    for n in (10, 100000):
        values = _values(n, np.dtype("<u2"), rng)
        name = f"c{len(cases)}"
        (tmp_path / f"{name}.bin").write_bytes(numcodecs.LZ4().encode(values))
        (tmp_path / f"{name}.raw").write_bytes(values.tobytes())
        cases.append({"name": name, "kind": "lz4"})
    (tmp_path / "cases.json").write_text(json.dumps(cases))

    done = subprocess.run(["node", str(PROBE), str(tmp_path)], cwd=ROOT, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert not [c for c in cases if got[c["name"]] == "wrong"]
    must = [c for c in cases if c["kind"] == "lz4"
            or (c["cname"] in ("lz4", "lz4hc", "zstd", "zlib") and c["shuffle"] != numcodecs.Blosc.BITSHUFFLE)]
    assert [c for c in must if got[c["name"]] != "ok"] == []
