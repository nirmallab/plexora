"""Copying one panel's rendering onto others, and the arithmetic that redraws it.

A figure is routinely eight crops of one slide, and they have to agree about
what colour CD8 is and where its contrast sits. Setting that eight times by
hand is slow and, more to the point, unreliable -- so one panel's channel
settings can be copied onto a selection of others.

Two claims are worth pinning here rather than only in the probe:

* the browser's compositing and the exporter's are the same arithmetic. The
  windows in a figure were chosen by eye against `render.render_panel`, which
  is the transcription of frag.glsl; a preview that added channels differently
  would be a picture of a figure nobody is going to get, and the author would
  only find out from the exported file.

* a source record carries the display name beside the key. Matching channels
  across two images is a question about NAMES -- a key is a path inside one
  file, so "channel 3" of two slides are not the same stain -- and if
  `fullname_at_capture` ever stopped being stored on the source, the match
  would silently fall back to keys and put a nuclear channel's window on
  whatever happened to be third in the other file.

`tests/js/figure_rendering_copy_probe.mjs` owns the case table.

Run the probe alone:
    node tests/js/figure_rendering_copy_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import render, schema

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_rendering_copy_probe.mjs"
STATIC = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"


def test_copying_rendering_between_panels_behaves():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run([node, str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert not report["problems"], json.dumps(report["problems"], indent=2)[:4000]
    assert proc.returncode == 0


def test_the_browser_composites_at_the_exporter_s_alpha():
    """One number in two languages. The probe asserts the browser's; this
    asserts they are the same one, which is the part that can drift."""
    source = (STATIC / "figurePanelCompositor.js").read_text(encoding="utf-8")
    line = next(line for line in source.splitlines() if "CHANNEL_ALPHA()" in line)
    assert f"return {render.CHANNEL_ALPHA}" in line, line


def test_a_source_records_the_display_name_beside_the_key():
    """What the cross-image match is made on. Without it two panels of two
    slides can only be matched by key, which is a path inside one file."""
    normalized = schema.normalize_source({
        "source_id": "src_1", "kind": "plexora_project", "datasource": "demo",
        "channels": [{"key": "ch_0", "fullname_at_capture": "DNA"}],
    })
    assert normalized["channels"] == [{"key": "ch_0", "fullname_at_capture": "DNA"}]
