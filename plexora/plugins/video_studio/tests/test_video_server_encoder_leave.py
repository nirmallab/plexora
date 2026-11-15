"""Video Studio: a server export abandoned by leaving the page is cancelled.

The probe (tests/js/video_server_encoder_leave_probe.mjs) runs the real
ServerEncoder and videoProjectApi under node: on `pagehide` mid-export it sends
one keepalive DELETE for the export, and none once the finish was posted or the
export was cancelled. Without it the server's job kept ffmpeg open for its
15-minute idle timeout and the agent's export_video refused the video.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "video_server_encoder_leave_probe.mjs"


def test_leaving_the_page_cancels_a_running_server_export():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
