"""Quick Edit: the session, the fetching, and the panel it is editing.

None of this is arithmetic that has to match Python -- it is what a slide-over
does with a document and with a network. Four claims, each of which ships green
and is wrong somewhere the user only meets later:

* the mini view draws the pixels it holds through the CURRENT framing. Drawing
  them where they were when they arrived is what made panning feel dead: the
  picture did not move until a refetch landed, which looked like a slow server
  rather than like arithmetic.

* a superseded batch of pixels must not land. A pan asks faster than the server
  answers, and without a sequence number the pan ends on whichever framing the
  network happened to finish last.

* switching panels saves the one being left, before it fetches anything for the
  new one. Selection-follow that dropped the session would make clicking the
  next panel the cheapest way to lose ten minutes of channel work.

* the panel on the figure shows the unsaved picture only while the session is
  open -- and keeps showing it until the preview upload has actually landed,
  because the commit re-renders the canvas at a revision the server has no
  picture for yet.

`tests/js/figure_quickedit_probe.mjs` drives the real class against a stub DOM;
this runs it. See the probe for what each case would cost.

Run the probe alone:
    node tests/js/figure_quickedit_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_quickedit_probe.mjs"
STATIC = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"


def test_the_quick_edit_session_behaves():
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


def test_the_held_pixels_remember_where_they_are_in_the_image():
    """The one line the whole pan fix rests on.

    A plane that recorded a position on the CANVAS is pinned to whatever the
    view happened to be when it was fetched, so every frame of a drag draws it
    in the wrong place until the refetch lands. The probe checks the
    projection; this checks that nothing has quietly gone back to storing
    `left`/`top`, which would still pass every projection test while making the
    view immovable again.
    """
    source = (STATIC / "figureQuickEdit.js").read_text(encoding="utf-8")
    body = source[source.index("async refresh("):source.index("\n    activeSlots(")]
    assert "box: { x: clamped.x" in body
    assert "left:" not in body and "top:" not in body
