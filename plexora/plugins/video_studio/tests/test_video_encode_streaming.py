"""Video Studio's browser encoder: a streamed WebM export is a whole file.

A large export streams its parts out of the worker instead of holding one
growing buffer. The WebM muxer seeks back when it finalizes, to patch the
Segment size, the Duration and the SeekHead, and once the output has passed
two 16 MiB chunks those patches arrive as writes at earlier positions. The
worker used to append every part in arrival order, so a long or large WebM came
out with a wrong Segment size and stray bytes at its end.

The probe (tests/js/video_encode_stream_probe.mjs) runs the real worker on the
real vendored mediabunny with a fake WebCodecs encoder, and compares each
streamed export with the buffered export of the same frames.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "video_encode_stream_probe.mjs"

CHECKS = (
    "a large streamed WebM is byte-identical to the buffered one",
    "a small streamed WebM is byte-identical to the buffered one",
    "a large streamed MP4 starts with ftyp and its boxes tile the file",
)


def test_a_streamed_export_is_the_buffered_file():
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
