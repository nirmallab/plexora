"""A capture adopted into a figure leaves the viewer's strip.

Adopting takes captures out of the bin on the server; reading the bin back
after the round trip kept every capture taken this session that the answer did
not list, so adopted ones stayed in the strip, ticked and outlined, and could be
added to a figure a second time. Only a capture whose write is still in flight,
or was refused, is held now.

`tests/js/figure_capture_strip_adopted_probe.mjs` drives the real controller's
bin read; this runs it.

Run the probe alone:
    node tests/js/figure_capture_strip_adopted_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_capture_strip_adopted_probe.mjs"

CHECKS = (
    "an adopted capture leaves the strip; one still being written or refused stays",
    "keep() marks a capture whose write landed",
)


def test_an_adopted_capture_leaves_the_strip():
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
