"""Cell ids past 2^24 on the website (tests/browser/high_ids.mjs).

Two projects from tools/bench_envelope/high_ids.py, one whose ids straddle
2^24 (float32's last exact integer) and one whose ids sit near 2^32, imported
on a static build in Chrome: the label tiles carry every id whole (their high
byte was dropped until 2026-11, so 16,777,216 drew as background and
16,777,217 as cell 1), the viewer's id list, the nearest-cell pick and a gate
name the table's cells exactly, and a gate past 2^24 is drawn on the CPU
without building an id-indexed table as long as the largest id (4 GB for ids
near 2^32). tests/test_high_cell_ids.py checks the desktop's server.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "high_ids.mjs"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"
BENCH = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not PLAYWRIGHT.is_dir() or not BENCH.is_dir(),
    reason="needs node, playwright-core and the website's Python bundle (tools/browser_build/build.py)")


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    out = tmp_path_factory.mktemp("highids")
    subprocess.run([sys.executable, str(ROOT / "tools" / "bench_envelope" / "high_ids.py"), str(out)],
                   check=True, capture_output=True, cwd=ROOT)
    return out


@pytest.mark.parametrize("name", ["hi24", "hi32"])
def test_ids_past_2_24_read_exactly_on_the_website(fixtures, name):
    done = subprocess.run(["node", str(DRIVER), "--dir", str(fixtures / name)], capture_output=True,
                          text=True, encoding="utf-8", timeout=900, cwd=ROOT)
    assert done.stdout.strip(), done.stderr[-2000:]
    report = json.loads(done.stdout)
    failed = [check for check in report["checks"] if not check["ok"]]
    assert not failed, json.dumps(failed, indent=1)[:3000]
    assert len(report["checks"]) == 8, [check["name"] for check in report["checks"]]
