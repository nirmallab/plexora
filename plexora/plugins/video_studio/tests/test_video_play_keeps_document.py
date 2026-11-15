"""Video Studio: Play never swaps the user's own video for a fresh preset build.

While no preview is showing, Play re-captures the viewer as the starting scene
and, if the viewer moved, rebuilds the video from its preset. After opening a
saved video, or after moving the viewer to set a camera position in the path
editor, that silently replaced the document, and the next save, edit or export
overwrote the stored one. Only an unedited preset build follows the viewer now.

The probe (tests/js/video_play_keeps_document_probe.mjs) runs the real
controller with the bar, the preview, the scene capture and the server faked.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "video_play_keeps_document_probe.mjs"

CHECKS = (
    "Play after opening a saved video builds nothing",
    "…keeps the opened document",
    "…and plays it",
    "Play after a path edit and a viewer move builds nothing",
    "…keeps the edited document",
    "…and plays it",
    "Play on an unedited preset build after a viewer move rebuilds",
    "…from the new view",
    "…and plays it",
)


def test_play_keeps_an_opened_or_edited_video():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert report["checked"] == len(CHECKS)
    assert not report["failures"], json.dumps(report["failures"], indent=2, ensure_ascii=False)
    assert proc.returncode == 0
