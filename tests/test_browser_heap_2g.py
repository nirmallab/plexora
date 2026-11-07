"""The website's Python past 2 GiB of heap (tests/browser/heap_2g.mjs).

Pyodide 314.0.7's own glue read four addresses as signed numbers, so past
2 GiB a buffer copied into Python threw and bytes or long strings handed to
the page came from the wrong end of memory; a 60 M-value table then answered
every JSON route with its own column values. tools/browser_build/build.py
patches them (GLUE_FIXES); this pushes the heap past 2 GiB and checks each
crossing at an address above 2^31 (all four failed on the unpatched glue).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "heap_2g.mjs"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"
BENCH = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not PLAYWRIGHT.is_dir() or not BENCH.is_dir(),
    reason="needs node, playwright-core and the website's Python bundle (tools/browser_build/build.py)")


def test_python_past_2_gib_hands_data_to_and_from_the_page_whole():
    done = subprocess.run(["node", str(DRIVER)], capture_output=True, text=True, encoding="utf-8",
                          timeout=600, cwd=ROOT)
    assert done.stdout.strip(), done.stderr[-2000:]
    report = json.loads(done.stdout)
    failed = [check for check in report["checks"] if not check["ok"]]
    assert not failed, json.dumps(failed, indent=1)[:3000]
    assert len(report["checks"]) == 7
