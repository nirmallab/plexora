"""Escape in the caption editor commits, in every browser.

It used to remove the editor element and rely on the browser firing ``blur``
on a removed focused element: Chromium does, so Escape committed; Firefox does
not, so the typed caption was dropped and a fresh text box stayed on the page
as an empty ghost. Escape now blurs the editor, which every engine reports.

`tests/js/figure_text_editor_escape_probe.mjs` drives the real class against a
stub element in both behaviours; this runs it.

Run the probe alone:
    node tests/js/figure_text_editor_escape_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_text_editor_escape_probe.mjs"

CHECKS = (
    "Chromium: Escape commits the caption once",
    "Firefox: Escape commits it too, rather than discarding it",
)


def test_escape_commits_the_caption_in_every_engine():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    probe_text = PROBE.read_text(encoding="utf-8")
    for name in CHECKS:
        assert name in probe_text, f"probe no longer checks: {name}"

    proc = subprocess.run([node, str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert not report["problems"], json.dumps(report["problems"], indent=2)[:4000]
    assert proc.returncode == 0
