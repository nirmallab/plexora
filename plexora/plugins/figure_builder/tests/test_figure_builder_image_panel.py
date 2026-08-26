"""The image sidebar, and the floating bar it took six buttons off.

A placed panel's own properties -- Quick Edit, the round trip to the viewer, the
split, the title and label, the scale bar, the legend, the rendering -- used to
be six buttons on the bar that floats over the artwork, each opening a popover
that covered the panel it was about. Text, shapes and lines each moved into the
sidebar strip before this for the same reasons; panels were the last set left.

Two claims are worth pinning on the Python side:

* nothing that moved was LOST. Every one of those actions still has a way in --
  the sidebar, and for the two that were also menu rows, the right-click menu.
  A registry entry deleted rather than moved is a feature that silently stops
  existing, and the bar is the one surface where that is invisible: it changes
  with the selection, so a missing button reads as "not for this one".

* the bar is left with the actions that apply to any object. A panel-specific
  entry still declaring `surface: ["bar"]` is a popover the bar can no longer
  build, which is a button that opens an empty box.

`tests/js/figure_image_panel_probe.mjs` owns the panel's own behaviour.

Run the probe alone:
    node tests/js/figure_image_panel_probe.mjs
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_image_panel_probe.mjs"
STATIC = REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"

#: The panel-specific actions. Each one is either a section of the image
#: sidebar or a row of the right-click menu, and none of them is a bar button.
MOVED = ("quick_edit", "edit")


def test_the_image_panel_behaves():
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


def test_what_left_the_bar_is_still_reachable():
    """Quick Edit and the viewer round trip are the two that were also menu
    rows, so they stay in the registry as menu-only. Everything else that left
    is a section of the sidebar."""
    actions = (STATIC / "figureActions.js").read_text(encoding="utf-8")
    for action_id in MOVED:
        entry = re.search(rf'id: "{action_id}".*?surface: \[([^\]]*)\]',
                          actions, re.S)
        assert entry, f"{action_id} is no longer a registered action"
        surfaces = entry.group(1)
        assert '"menu"' in surfaces, f"{action_id} has no way in at all"
        assert '"bar"' not in surfaces, f"{action_id} is still on the floating bar"

    panel = (STATIC / "figureImagePanel.js").read_text(encoding="utf-8")
    for act in ("quick_edit", "viewer", "split_with_composite",
                "copy_rendering", "apply_rendering", "legend_share"):
        assert f'data-act="{act}"' in panel, f"the sidebar has no {act}"


def test_the_bar_no_longer_declares_popovers_it_cannot_build():
    """The panel popovers were deleted with the buttons that opened them. An
    action left declaring `popover: true` on the bar would be a button that
    opens an empty box -- which looks like a rendering bug, not a missing
    handler."""
    actions = (STATIC / "figureActions.js").read_text(encoding="utf-8")
    bar = (STATIC / "figureContextBar.js").read_text(encoding="utf-8")

    declared = set(re.findall(r'id: "(\w+)",(?:[^{}]|\n)*?popover: true', actions))
    built = set(re.findall(r'act === "(\w+)"', bar))
    # `more`, `align` and friends are built inline in popoverMarkup rather than
    # by a named method, and all of them go through the same `act ===` fork.
    assert declared <= built, f"no popover body for {sorted(declared - built)}"

    for gone in ("titlesPopover", "scalebarPopover", "legendPopover",
                 "pixelSizePopover", "splitPopover"):
        assert gone not in bar, f"{gone} is dead code the sidebar replaced"
