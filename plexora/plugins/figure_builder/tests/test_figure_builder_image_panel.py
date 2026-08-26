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
    """Everything that left the bar is in the registry, on the surfaces that
    replaced it: the sidebar for all of them, and the right-click menu for the
    two that were menu rows before as well.

    The sidebar is a declared surface now rather than a hand-built list. It had
    its own copy of "can this panel be reopened", the copy drifted, and a panel
    whose image had gone offered a Quick Edit that navigated the user off their
    figure. So what is checked here is that the registry still owns each of
    them AND that each names the section the panel draws it in -- an action on
    the sidebar surface with no section is silently dropped."""
    actions = (STATIC / "figureActions.js").read_text(encoding="utf-8")
    for action_id in MOVED:
        entry = re.search(rf'id: "{action_id}".*?surface: \[([^\]]*)\]',
                          actions, re.S)
        assert entry, f"{action_id} is no longer a registered action"
        surfaces = entry.group(1)
        assert '"menu"' in surfaces, f"{action_id} has no way in at all"
        assert '"bar"' not in surfaces, f"{action_id} is still on the floating bar"

    # The sidebar draws its action buttons FROM the registry now, so the ones
    # that come from there are pinned by their `sidebar.section` rather than by
    # a `data-act` typed into the panel. What is left literal in the panel is
    # what only the panel has: the legend's conflict answers, the pixel size,
    # the caption rows.
    registry = actions
    for action_id, section in (("quick_edit", "actions"), ("edit", "actions"),
                               ("split_with_composite", "split"),
                               ("split_channels_only", "split"),
                               ("copy_rendering", "rendering"),
                               ("apply_rendering", "rendering")):
        entry = re.search(rf'id: "{action_id}".*?surface: \[([^\]]*)\]',
                          registry, re.S)
        assert entry, f"{action_id} is no longer a registered action"
        assert '"sidebar"' in entry.group(1), f"{action_id} left the image sidebar"
        block = re.search(rf'id: "{action_id}".*?sidebar: \{{\s*section: "(\w+)"',
                          registry, re.S)
        assert block and block.group(1) == section, \
            f"{action_id} names no sidebar section, or not {section}"

    panel = (STATIC / "figureImagePanel.js").read_text(encoding="utf-8")
    for act in ("legend_share", "legend_keep", "pixel_size", "add_label"):
        assert f'data-act="{act}"' in panel, f"the sidebar has no {act}"


def test_numbering_belongs_to_the_document():
    """Panel numbering -- A/B/C, a/b/c, A1/A2/A3 -- runs in reading order across
    every page of the figure. It was a select inside the image sidebar's section
    for one selected panel's own title, so a document-wide setting was reachable
    only by selecting an image, looked like a property of that image, and needed
    a sentence under it explaining that it was not.

    Asserted against the source rather than through the probe because the page
    menu counts pages, and the probe boots the workspace in the state it really
    boots in: with the document still in flight."""
    workspace = (STATIC / "figureWorkspace.js").read_text(encoding="utf-8")
    entries = re.search(r"pageMenuEntries\(\) \{(.*?)\n    \}", workspace, re.S)
    assert entries, "the page menu has no entries method"
    assert 'act: "numbering"' in entries.group(1), \
        "panel numbering has no home in the page menu"
    assert "openNumbering()" in workspace, "nothing opens the numbering menu"

    panel = (STATIC / "figureImagePanel.js").read_text(encoding="utf-8")
    assert "label_style" not in panel, \
        "the image sidebar still writes the figure's numbering"


def test_the_bar_no_longer_declares_popovers_it_cannot_build():
    """The panel popovers were deleted with the buttons that opened them. An
    action left declaring `popover: true` on the bar would be a button that
    opens an empty box -- which looks like a rendering bug, not a missing
    handler."""
    actions = (STATIC / "figureActions.js").read_text(encoding="utf-8")
    bar = (STATIC / "figureContextBar.js").read_text(encoding="utf-8")

    declared = set(re.findall(r'id: "(\w+)",(?:[^{}]|\n)*?popover: true', actions))
    built = set(re.findall(r'act === "(\w+)"', bar))
    # `more` is built inline in popoverMarkup rather than by a named method, and
    # every one of them goes through the same `act ===` fork.
    assert declared <= built, f"no popover body for {sorted(declared - built)}"

    # A group that FOLDS is a tile with no action of its own: it exists only to
    # open the popover that holds its members, so a fold the bar cannot draw a
    # body for takes five commands off every surface at once.
    for group in re.findall(r'(\w+): \{ collapse:', actions):
        assert f'act === "group:{group}"' in bar, \
            f"the {group} fold has no popover body to open"

    for gone in ("titlesPopover", "scalebarPopover", "legendPopover",
                 "pixelSizePopover", "splitPopover"):
        assert gone not in bar, f"{gone} is dead code the sidebar replaced"
