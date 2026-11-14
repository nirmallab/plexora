"""Video Studio's burned-in scale bar matches the zoom a turned 2-D frame is drawn at.

A 2-D video frame turned 90 or 270 degrees (a slide rotated to orient the
tissue) is fitted by its TURNED extent, so on a 16:9 output the frame's zoom
differs from the upright one by the aspect ratio. The scale bar burned into
every exported frame must still be exact in 2-D: its µm per output pixel is
the image's pixel size over the zoom the viewer really applied, and the bar it
draws spans the length its label says.

The probe (tests/js/video_studio_scalebar_rotation_probe.mjs) runs the real
viewerScene.js, videoSceneState.js and videoOverlays.js against a stand-in
OpenSeadragon viewer the size of the output.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "tests" / "js" / "video_studio_scalebar_rotation_probe.mjs"


def test_the_scale_bar_matches_the_zoom_of_a_turned_2d_frame():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert report["checked"] >= 31, report
    assert not report["failures"], json.dumps(report["failures"], indent=2, ensure_ascii=False)
    assert proc.returncode == 0
