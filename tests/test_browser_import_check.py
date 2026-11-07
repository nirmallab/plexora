"""The website's Python bundle, booted in Node from disk
(tests/browser/import_check.mjs; web.yml's import_check job runs the same).

Needs node and the build `tools/browser_build/build.py` writes for this
checkout's version; skipped, saying which, without them. The bundle is what is
checked, so after a change to plexora/ or to the stage list rebuild it
(`build.py --plexora-only` for Python alone) before reading a failure here.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"


def _version():
    import plexora

    return plexora.__version__


@pytest.fixture(scope="module")
def checked():
    bundle = BUILD / _version()
    missing = [what for what, there in (("node", shutil.which("node")),
                                        ("the build", (bundle / "stages.json").exists())) if not there]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (see this file's docstring)")
    done = subprocess.run(["node", str(ROOT / "tests" / "browser" / "import_check.mjs"),
                           "--bundle", str(bundle)], cwd=ROOT, capture_output=True, text=True,
                          timeout=900)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


def _norm(name):
    return name.lower().replace("_", "-").replace(".", "-")


def test_the_bundle_was_built_from_this_stage_list(checked):
    """A failure below may only mean the bundle is older than the checkout."""
    from plexora.browser.stages import STAGES

    built = json.loads((BUILD / _version() / "stages.json").read_text(encoding="utf-8"))
    for name, stage in STAGES.items():
        wheels = sorted(_norm(wheel.split("-")[0]) for wheel in built[name]["wheels"])
        assert wheels == sorted(_norm(wheel) for wheel in stage["wheels"]), (
            f"the bundle's {name} stage is not plexora/browser/stages.py's: rebuild it")


def test_the_bundle_boots_imports_and_reads_a_calibration(checked):
    code, out = checked
    assert out["failed"] == [] and code == 0, out["failed"]


def test_the_boot_builds_the_app_without_a_stage(checked):
    _code, out = checked
    boot = out["checks"]["boot"]
    assert boot["error"] is None
    assert boot["info"]["rules"] > 300
    assert boot["asked"] == []


def test_the_calibrated_fixture_reports_its_pixel_size(checked):
    _code, out = checked
    assert out["checks"]["pixels"]["size"]["value"] == pytest.approx(0.5)
