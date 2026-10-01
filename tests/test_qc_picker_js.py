"""The QC panel's five categories in the browser, run in node against the
shipped scripts.

The checks live in tests/js/qc_picker_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "qc_picker_probe.mjs"
SCRIPTS = [REPO_ROOT / "plexora" / "plugins" / "qc" / "static" / name
           for name in ("qcTree.js", "qcSidebarController.js")]

CHECKS = (
    "the picker lists Custom, then the five categories in order, each in its colour",
    "a category draws in that category",
    "a subtype typed into Custom draws its class; a category's words the category; "
    "others a custom one",
    "the ? help is folded away until opened, and opening it does not close the menu",
    "each help row unfolds to its one line of what it groups",
    "regions are grouped by category in the five's order, the subtype noted",
    "cells sit under their category, whose eye hides its reasons",
    "the details popup lists the provenance, the threshold's source included",
    "the download menu offers the provenance and the findings",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def test_the_scripts_parse():
    for script in SCRIPTS:
        done = subprocess.run([node, "--check", str(script)], capture_output=True, text=True,
                              timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
