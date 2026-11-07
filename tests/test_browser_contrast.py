"""The same image opens with the same channel contrast on the website and on
the desktop, and the automatic contrast always lands (tests/browser/contrast.mjs).

Each case opens one image in both apps -- the static site with the website's
Python bundle, and a CPython Plexora -- and checks, per app: the statistics'
hint and then the fit landed on every channel (each switched on in turn), a
viewer left 300 ms after it opened reopens on its fit, a reopen gets its fits
from the store beside the project and settles on them, and on a z-stack the
3-D view draws every channel at its fit; and between the apps: the same
windows, fits and Log windows for every channel, on the first open, after the
early leave and in the 3-D view.

What it caught: an unseeded, float32 mixture fit (the same stack's DNA at 267
or 1593, and 268 on the website against 267 on the desktop), a failed fit kept
as the answer, and a row saved before its fit landed restored as levelled for
good (tests/test_contrast_fit_store.py, tests/test_contrast_landing.py).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "contrast.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static" / "index.html"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_file() or not PLAYWRIGHT.is_dir(),
    reason="needs node, a static build (python -m plexora.client.static_build), "
           "the website's Python bundle (tools/browser_build/build.py) and playwright-core")


def _stack(tmp_path_factory):
    sys.path.insert(0, str(ROOT / "tests" / "browser"))
    try:
        import volume_web
    finally:
        sys.path.pop(0)
    out = tmp_path_factory.mktemp("contrast-stack")
    data = volume_web.cells(*volume_web.SIZES["test"])[0]
    return str(volume_web.write_ome_tiff(out / "stack.ome.tif", data, names=["DNA", "Membrane"]))


@pytest.fixture(scope="module", params=["plane", "volume"])
def run(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"contrast-{request.param}")
    extra = ["--files", _stack(tmp_path_factory), "--volume"] if request.param == "volume" else []
    done = subprocess.run(["node", str(DRIVER), "--out", str(out), "--reopen", "1", *extra],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=1800,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail(f"contrast.mjs wrote no record: {done.stderr[-3000:]}")
    return request.param, record


def test_every_check_passes(run):
    case, record = run
    failed = {name: check.get("detail") for name, check in record["checks"].items() if not check["ok"]}
    assert not failed, (case, failed)


def test_both_apps_were_compared(run):
    case, record = run
    names = set(record["checks"])
    expected = {"both apps: the first open's windows are the same",
                "both apps: every channel's window is the same",
                "both apps: the reopen after leaving early has the same windows"}
    if case == "volume":
        expected |= {"both apps: the 3-D view draws the same windows",
                     "website: the 3-D view draws every channel at its fit",
                     "desktop: the 3-D view draws every channel at its fit"}
    assert expected <= names, sorted(names)
