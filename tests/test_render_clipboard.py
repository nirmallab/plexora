"""The Image card's clipboard: what a copy keeps and what a paste would do.

The checks are in `tests/js/render_clipboard_probe.mjs`, which runs the real
services/renderClipboard.js. This drives the probe and names every check, so
one quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "render_clipboard_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    'nothing is copied until something is',
    'the two slots round-trip independently',
    'it survives a page load in the same tab',
    'a document from another version reads as empty',
    'blocked storage is nothing copied, not an exception',
    'an empty copy is refused rather than stored',
    'the same length renames every position',
    'a shorter copy renames the overlap and keeps the rest',
    'a longer copy uses what fits',
    'an identical list changes nothing',
    "a blank copied name keeps the image's own",
    'a name the image keeps elsewhere is skipped',
    'a repeated copied name is used once',
    'a dropped rename that frees a collision is followed through',
    'slots match by name whatever order the image is in',
    'a name the image lacks falls back to its position',
    "a position past the image's channels is skipped",
    'a channel is used at most once',
    'a blank row is skipped and an off slot stays off',
    'a window that is not two numbers is dropped, not pasted',
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
