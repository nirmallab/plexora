"""A flat mask's label pyramid built by the website's build worker
(client src/js/workers/pyramidWorker.js) wherever the copy goes.

services/browserPyramid.js asks the worker for `kind: "labels"` when Python
needs a flat segmentation mask's pyramid. Every level of it must be sampled
from level 0 (segmentation_pyramid's FILLED mode: level L's pixel (y, x) is
level 0's (y * 2^L, x * 2^L)), never a 2x2 mean, which makes ids of cells that
are not there. That holds whether the copy goes to a folder the user granted,
the site's storage, or -- where the browser gives no sync access handle (a
private window, WebKit on Windows) -- is put together in memory and kept in
IndexedDB.

The node probe (tests/js/pyramid_worker_labels_probe.mjs) runs the real worker
with in-process hands over the mask's samples and writes the copy it posts.
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

from plexora.server.utils import segmentation_pyramid

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "pyramid_worker_labels_probe.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="needs node")

TILE = 256


def _mask(height=530, width=610, seed=5):
    """Cells as blocks of one id over background, and single-pixel ids beside
    them: a 2x2 mean of them is an id no pixel at that place holds."""
    rng = np.random.default_rng(seed)
    out = np.zeros((height, width), dtype=np.uint32)
    for _ in range(80):
        y, x = int(rng.integers(0, height)), int(rng.integers(0, width))
        h, w = int(rng.integers(3, 40)), int(rng.integers(3, 40))
        out[y:y + h, x:x + w] = int(rng.integers(1, 100_000))
    speckle = rng.random(out.shape) < 0.2
    out[speckle] = rng.integers(1, 100_000, size=int(speckle.sum()), dtype=np.int64).astype(np.uint32)
    return out


def _page_copy(tmp_path, mode):
    data = _mask()
    source = tmp_path / "cell.ome.tif"
    tifffile.imwrite(source, data, photometric="minisblack", rowsperstrip=64)
    spec = segmentation_pyramid.page_build(source, tile_size=TILE)
    assert spec is not None and spec["kind"] == "labels" and len(spec["factors"]) > 1
    raw = tmp_path / "cell.raw"
    raw.write_bytes(data.tobytes())
    out = tmp_path / f"{mode}.labels.pyramid.ome.tiff"
    request = {"mode": mode, "raw": str(raw), "out": str(out), "name": out.name, "kind": spec["kind"],
               "shape": spec["shape"], "dtype": spec["dtype"], "factors": spec["factors"],
               "tile": spec["tile"], "description": spec["description"]}
    spec_file = tmp_path / f"{mode}.json"
    spec_file.write_text(json.dumps(request), encoding="utf-8")
    done = subprocess.run([NODE, str(PROBE), str(spec_file)], capture_output=True, text=True,
                          encoding="utf-8", cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    record = json.loads(done.stdout.strip().splitlines()[-1])
    assert "error" not in record, record
    return data, spec, out, record


def _levels(path):
    with tifffile.TiffFile(path) as tiff:
        return [np.asarray(level.asarray()) for level in tiff.series[0].levels]


def _assert_sampled(data, spec, copy):
    levels = _levels(copy)
    assert len(levels) == len(spec["factors"])
    for level, got in enumerate(levels):
        factor = spec["factors"][level]
        want = data[::factor, ::factor]
        assert got.shape == want.shape, level
        differ = int((got != want).sum())
        assert differ == 0, (f"level {level}: {differ} of {want.size} pixels are not level 0's id at the "
                             f"sampled pixel; {int((~np.isin(got, data)).sum())} hold ids no cell has")


def test_a_label_pyramid_built_into_a_granted_folder_samples_every_level(tmp_path):
    data, spec, copy, record = _page_copy(tmp_path, "folder")
    assert record["done"] and record["kept"] is None
    _assert_sampled(data, spec, copy)


def test_a_label_pyramid_built_in_memory_samples_every_level(tmp_path):
    """No folder and no sync access handle: the copy put together in memory
    is a label pyramid too, not a 2x2 mean of cell ids."""
    data, spec, copy, record = _page_copy(tmp_path, "memory")
    assert record["done"] and record["kept"] == "memory"
    _assert_sampled(data, spec, copy)
