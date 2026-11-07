"""The website's self-check page, /selfcheck (plan Phase 5's preview).

tests/browser/selfcheck.mjs opens it on a static build in Chrome and runs
it: every step measured (the browser, its storage, the download rate as
sent, Python's boot in a scratch worker, a flat image's pyramid built by the
website's own builder with its rate beside the estimate, WebAssembly
memory, WebGPU), a row each with what it means on that computer, nothing
left in the site's storage, no CSP violation, and "Download results" saving
the same results -- the file a person sends from a computer only they have
(a Mac, an 8 GB laptop).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "selfcheck.mjs"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"
BENCH = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not PLAYWRIGHT.is_dir() or not BENCH.is_dir(),
    reason="needs node, playwright-core and the website's Python bundle (tools/browser_build/build.py)")


@pytest.fixture(scope="module")
def run():
    done = subprocess.run(["node", str(DRIVER)], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", timeout=900, env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        pytest.fail(f"selfcheck.mjs printed no record: {done.stderr[-3000:]}")


def test_every_check_passes(run):
    failed = [c for c in run["checks"] if not c["ok"]]
    assert not failed, (failed, run["errors"])


def test_the_results_say_what_each_number_means_here(run):
    steps = run["results"]["steps"]
    assert steps["pyramid"]["here"].startswith("Flat images up to about ")
    assert steps["memory"]["here"].startswith("Tables up to about ")
    assert steps["download"]["here"].startswith("A first visit downloads ")
    assert run["results"]["version"] and run["results"]["envelope"] == 2
