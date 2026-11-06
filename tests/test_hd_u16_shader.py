"""frag.glsl rebuilds an HD tile's 16-bit value exactly, in a real browser.

`tests/js/hd_u16_shader_parity.mjs` builds glInit's own renderer over the
shipped glRenderer.js, vert.glsl and frag.glsl in Chromium, feeds it uint16
rows the way the HD path does (big-endian bytes, RG8UI) and reads the drawn
bytes back: a full-range ramp, a ramp across a tight bright window, values whose
two bytes differ, and 4096 random values in random windows, each within one
8-bit step of round(255 * clamp((v / 65535 - lo) / (hi - lo))).

Until 2026-11 the shader rebuilt r * 255 + g, short by the high byte: up to 11
levels dark across a [0.50, 0.55] window, and a saturated pixel (65535) in a
window at the top of the range drawn at level 7 of 255. The test fails on that
shader (`--frag` draws through another copy of it).

Headless Chromium draws WebGL with SwiftShader; PLEXORA_BROWSER_HEADED=1 also
runs it on the machine's GPU. Skipped where Playwright cannot be found.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tests" / "js" / "hd_u16_shader_parity.mjs"
SHADER = REPO_ROOT / "plexora" / "client" / "src" / "shaders" / "frag.glsl"

MODES = ["headless"] + (["headed"] if os.environ.get("PLEXORA_BROWSER_HEADED") == "1" else [])


@pytest.fixture(scope="module", params=MODES)
def parity(request):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    args = [node, str(SCRIPT)] + (["--headed"] if request.param == "headed" else [])
    proc = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT, timeout=600)
    if proc.stdout.startswith("SKIP"):
        pytest.skip(proc.stdout.strip())
    return proc


def test_every_case_is_within_one_step(parity):
    assert parity.returncode == 0, parity.stdout + parity.stderr
    assert "FAIL" not in parity.stdout, parity.stdout


def test_the_matrix_actually_ran(parity):
    lines = [line for line in parity.stdout.splitlines() if line.startswith("PASS ")]
    assert len(lines) == 4, parity.stdout
    counts = [int(n) for n in re.findall(r"\| n (\d+) \|", parity.stdout)]
    assert counts == [256, 256, 9, 4096], counts


def test_the_shader_rebuilds_with_256():
    """The source says r * 256 + g; a cheap guard beside the browser run."""
    source = SHADER.read_text(encoding="utf-8")
    assert "pixel.r * uint(256) + pixel.g" in source
    assert "uint(255) + pixel.g" not in source
