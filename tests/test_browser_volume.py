"""3-D on the website as on the desktop (plan L4 + L11).

Each z-stack a user brings -- an OME-TIFF stack with its 3-D mask, an
OME-Zarr folder, an Imaris file deflated, and Imaris 10's LZ4 in 16 and 8
bits (no hdf5plugin on the website: ims.py decodes the chunks through
imagecodecs_shim and the `lz4` package of the `hdf5` stage) -- is picked in
Chrome, imported, opened, and shown in the 3-D view, which must have drawn
(its canvas read back). Then every volume route the viewer asks (info,
windows, the brick manifest, whole levels in 8 and 16 bits, bricks, sections,
plane, projection and section tiles, the mask's objects) answers on the
website what the desktop's Plexora answers for the same files: the same
bytes, or for a PNG tile the same pixels (tests/browser/volume.mjs,
tests/browser/volume_web.py).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "volume.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static" / "index.html"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_file() or not PLAYWRIGHT.is_dir(),
    reason="needs node, a static build (python -m plexora.client.static_build), "
           "the website's Python bundle (tools/browser_build/build.py) and playwright-core")

CASES = {
    "tiff_and_mask": ("stack", "mask"),
    "ome_zarr": ("zarr",),
    "ims_deflate": ("ims_deflate",),
    "ims_lz4": ("ims_lz4",),
    "ims_lz4_u8": ("ims_lz4_u8",),
}


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    pytest.importorskip("hdf5plugin")
    sys.path.insert(0, str(ROOT / "tests" / "browser"))
    try:
        import volume_web
    finally:
        sys.path.pop(0)
    return volume_web.make(tmp_path_factory.mktemp("volumes"), "test")


@pytest.fixture(scope="module", params=sorted(CASES))
def run(request, fixtures, tmp_path_factory):
    files = [fixtures[key] for key in CASES[request.param]]
    out = tmp_path_factory.mktemp(request.param)
    done = subprocess.run(["node", str(DRIVER), "--files", ",".join(files), "--out", str(out)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=1800,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail(f"volume.mjs wrote no record: {done.stderr[-3000:]}")
    return request.param, record


def test_every_check_passes(run):
    case, record = run
    failed = {name: check.get("detail") for name, check in record["checks"].items() if not check["ok"]}
    assert not failed, (case, failed)


def test_every_answer_was_compared(run):
    case, record = run
    verdicts = record.get("verdicts") or {}
    # Everything both arms were asked: info, windows, manifest, 5 levels,
    # 2 brick requests, 2 sections, 3 tiles (and the objects of a mask).
    assert sum(verdicts.get(v, 0) for v in ("same", "same_pixels", "same_json")) >= 15, (case, verdicts)


def test_an_ims_needs_only_the_hdf5_stage(run):
    """An .ims brings h5py and lz4 (1.2 MB), not pandas and anndata."""
    case, record = run
    stages = {s["stage"] for s in record["arms"]["website"].get("stages") or []}
    if case.startswith("ims"):
        assert "hdf5" in stages and "h5ad" not in stages, stages
    else:
        assert not {"hdf5", "h5ad"} & stages, stages
