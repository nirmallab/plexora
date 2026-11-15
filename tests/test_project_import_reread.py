"""Import Project: changing "Save to" while an earlier preview is in flight.

tests/js/project_import_reread_probe.mjs lifts moveTo() out of
views/projectImport.js and runs it with replies landing out of order: only the
latest place's preview may be shown, re-reading (which holds Import disabled)
lasts until that one lands, and a reply arriving after an import started does
not redraw the preview over it. Skipped without node.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "project_import_reread_probe.mjs"

CHECKS = (
    "a preview asked for an earlier place, landing last, does not replace the chosen place's",
    "the earlier reply does not clear re-reading while the later one is still out",
    "...and the later one, when it lands, is the preview shown",
    "a preview that lands after an import started does not redraw over it",
)


def test_only_the_latest_places_preview_is_shown():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    out = proc.stdout + proc.stderr
    for label in CHECKS:
        assert any(line.startswith(f"PASS {label}") for line in proc.stdout.splitlines()), out
    assert proc.returncode == 0, out
