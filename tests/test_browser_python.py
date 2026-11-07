"""The website's Python, booted in a real Chrome (tests/browser/boot.mjs).

Needs what a developer machine has and CI does not yet (plan Phase 5): system
Chrome, the build `tools/browser_build/build.py` writes, and playwright-core
(`npm ci` in tools/poc_browser_node). Skipped, saying which, without them.
Correctness only: the timings it prints are recorded by the bench, never
asserted here.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(os.environ.get("PLEXORA_BENCH_CHROME")
              or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BUILD = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"
#: tests/browser/lib.mjs reads it from here, or from PLEXORA_PLAYWRIGHT_MODULES.
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"


def _version():
    import plexora

    return plexora.__version__


@pytest.fixture(scope="module")
def booted():
    missing = [what for what, there in (("Chrome", CHROME.exists()), ("node", shutil.which("node")),
                                        ("the build", (BUILD / _version() / "stages.json").exists()),
                                        ("playwright-core", PLAYWRIGHT.exists())) if not there]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (see this file's docstring)")
    done = subprocess.run(["node", str(ROOT / "tests" / "browser" / "boot.mjs"), "--py", _version()],
                          cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_app_builds_in_the_page(booted):
    assert booted["info"].get("error") is None, booted["info"]
    assert booted["isolated"] is True
    assert booted["info"]["info"]["rules"] > 300
    assert booted["info"]["store"] == "opfs"


def test_boot_loads_no_stage_it_does_not_need(booted):
    """A library's optional import (scikit-learn's pandas check) must not
    bring a stage in: every stage at boot would cost seconds."""
    assert booted["stages"] == []


def test_routes_answer_through_the_page(booted):
    statuses = {path: answer["status"] for path, answer in booted["paths"].items()}
    assert statuses["/health"] == 204
    assert all(status == 200 for path, status in statuses.items() if path != "/health"), statuses
    assert booted["paths"]["/settings"]["head"].lstrip().startswith("<!doctype html>")
