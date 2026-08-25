"""What bounds the contrast slider in HD mode.

HD serves raw 16-bit tiles, so the slider works in raw units and its domain has
to reach the brightest pixel those tiles contain. It took that ceiling from
`image_max`, which get_image_channel_stats derives from `zarray` -- the
mean-pooled overview. Pooling dilutes single/few-pixel peaks, so image_max sits
far below the real maximum: a channel reporting 1313 could not have its window
moved above 1313, and every raw value above it clamped to full brightness.

get_channel_quantization_window already records this exact trap ("using it as a
max-based ceiling under-clips real data... it caused whole channels to saturate
to a single solid color") and reads full-resolution data for `qmax` because of
it. The sidebar just wasn't using that field.

image_max is deliberately left alone: it is the companion statistic to
image_histogram and only means anything plotted against it, so redefining it
server-side would desynchronize the curve from its own axis. The fix is at the
consumer, which wanted qmax all along.

The probe extracts the real methods from viewerSidebar.js, and runs them
alongside the same methods with the one-expression fix taken back out, so it
measures the change against shipped code rather than a reimplementation.

It used to take "before" from `git show HEAD:viewerSidebar.js`. That is only a
regression test until the fix is committed -- at which point before and after
are the same source and the probe fails BECAUSE the code it guards had shipped.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "hd_slider_domain_probe.mjs"


def test_hd_slider_domain_reaches_the_full_resolution_maximum():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The probe asserts internally, but pin its lines here too so a future edit
    # that quietly drops a check is not mistaken for a passing test.
    for line in (
        "the HD slider ceiling is the full-resolution max, not the pooled one",
        "toggling HD on a fully-open channel keeps the upper handle inside the domain",
        "a channel whose stats have not been fetched is unchanged",
        "default mode still uses the fixed [0, 255] byte domain",
        "a packet with no qmax falls back to the previous ceiling",
    ):
        assert line in proc.stdout, proc.stdout


def test_stats_packet_carries_both_the_pooled_and_full_resolution_ceilings():
    """The fix depends on qmax riding along in the packet the sidebar already
    has -- if it ever stopped being sent, getRawImageRange would silently fall
    back to the pooled image_max and the bug would return with no other sign."""
    source = (REPO_ROOT / "plexora" / "server" / "models" / "data_model.py").read_text(
        encoding="utf8"
    )
    start = source.index("def get_image_channel_stats(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    for key in ("'image_max'", "'qmax'"):
        assert key in body, f"{key} is no longer part of the stats packet"
