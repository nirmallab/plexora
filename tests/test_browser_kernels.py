"""plexora-kernels on the website (plan L5): the WebAssembly wheel the base
stage ships loads in the worker, every kernel runs compiled there, and what
it computes is what the desktop computes (tests/browser/kernels.mjs, in
Chrome, on tests/kernel_cases.py's `test` inputs, which are the same bits on
both).

The rule, as on the desktop (tests/test_kernels_package.py): the website's
port gives the desktop port's outputs bit for bit -- and so numba's -- except
`segqc.label_stats`, whose `excess` comes from the C library's `expm1f`
(musl's in WebAssembly); there, and against the Python the website would run
otherwise, floats agree to 1e-6 relative.

Skipped without node, playwright-core, a static build, or a bundle holding a
plexora-kernels wheel (tools/browser_build/build.py --kernels-wheel;
PLEXORA_BENCH_HOME names the bundle, as for the other browser tests).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "kernels.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"
BUILD = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"

#: Cases whose float outputs pass through a C library function (expm1f) or
#: numpy's percentile, so may differ from another build in the last bits.
LIBM = {"segqc.label_stats"}
#: ...and those whose Python form rounds differently (tests/test_kernels_package.py).
PYTHON_ROUNDS = {"segqc.label_stats", "cell_features._background_blocks"}


def _bundle_has_kernels():
    import plexora

    manifest = BUILD / plexora.__version__ / "manifest.json"
    if not manifest.exists():
        return False
    return bool(json.loads(manifest.read_text(encoding="utf-8")).get("kernels"))


pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_dir() or not PLAYWRIGHT.is_dir()
    or not _bundle_has_kernels(),
    reason="needs node, playwright-core, a static build and a bundle with plexora-kernels")


@pytest.fixture(scope="module")
def browser():
    done = subprocess.run(["node", str(DRIVER), "--size", "test", "--repeat", "1",
                           "--values", ",".join(sorted(LIBM | PYTHON_ROUNDS))],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert done.returncode == 0, done.stderr[-3000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out.get("error") is None, out["error"]
    return out


def test_the_kernels_load_compiled_in_the_worker(browser):
    package = browser["package"]
    assert package["file"].startswith("/lib/python3.14/site-packages/plexora_kernels/")
    assert set(package["impl"].values()) == {"compiled"}, package["impl"]
    assert package["numba"] is False and package["numba_imported"] is False
    assert browser["fallbacks"] == {}
    assert {entry["compiled"]["impl"] for entry in browser["cases"].values()} == {"compiled"}
    assert {entry["python"]["impl"] for entry in browser["cases"].values()} == {"python"}


def _close(a, b):
    return all(np.allclose(np.asarray(x, dtype=float), np.asarray(y, dtype=float), rtol=1e-6,
                           atol=1e-9, equal_nan=True) for x, y in zip(a, b))


def test_the_website_port_equals_the_python_it_replaces(browser):
    for name, entry in browser["cases"].items():
        if entry["compiled"]["digest"] == entry["python"]["digest"]:
            continue
        assert name in PYTHON_ROUNDS, name
        assert _close(entry["compiled"]["values"], entry["python"]["values"]), name


def test_the_website_port_equals_the_desktops(browser, tmp_path):
    """Same inputs (hashed on both sides), same outputs: so the website runs
    numba's arithmetic (the desktop port equals numba, test_kernels_package)."""
    pytest.importorskip("plexora_kernels")
    saved = tmp_path / "browser.json"
    saved.write_text(json.dumps(browser), encoding="utf-8")
    done = subprocess.run([sys.executable, str(ROOT / "kernels" / "tools" / "compare_browser.py"),
                           str(saved)], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["inputs_differ"] == []
    assert set(report["mismatch"]) <= LIBM, report["mismatch"]
