"""The image sidebar, and the floating bar it took six buttons off.

A placed panel's own properties -- Quick Edit, the round trip to the viewer, the
split, the panel's letter, the scale bar, the colour bar, the captions, the
rendering -- used to be six buttons on the bar that floats over the artwork,
each opening a popover that covered the panel it was about. Text, shapes and
lines each moved into the sidebar strip before this for the same reasons; panels
were the last set left.

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
    # a `data-act` typed into the panel. What is left is what only the panel
    # has: the pixel size, the three eyes, the caption rows.
    registry = actions
    for action_id, section in (("quick_edit", "actions"), ("edit", "actions"),
                               ("split_with_composite", "split"),
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

    # Not one of these is a literal attribute to grep for. The bars' on/off is
    # one helper taking the answer and the act (all three eyes are the same
    # button, and one of them -- the scale bar's caption -- is not a `visible`
    # flag at all); Add and Update are the icon-button helper, which builds the
    # attribute from its argument; and a caption row's buttons carry the row
    # index after a colon. They are pinned where they are DISPATCHED instead,
    # which is the half that can go missing: a button whose act nothing handles
    # is a control that does nothing and says nothing, which is what the
    # reorder was.
    for act in ("pixel_size", "scalebar_visible", "scalebar_label",
                "colorbar_visible", "add_label", "label_delete"):
        assert re.search(rf"^\s+{act}: \(\) =>", panel, re.M), \
            f"the sidebar dispatches nothing for {act}"

    # Reordering a caption is a DRAG, so it is not an act at all: no button, no
    # dispatch entry, and the two arrows that were both are gone. It still has
    # to be doable without a pointer, which is what the handle's arrow keys are
    # -- a list that can only be reordered by dragging is a list some people
    # cannot reorder.
    assert "label_up" not in panel and "label_down" not in panel, \
        "the sidebar still carries the reorder arrows a drag replaced"
    assert "moveRowTo(Number(grip)" in panel, \
        "the drag handle has no keyboard route"
    assert "data-grip=" in panel, "the caption rows have no drag handle"

    # SortableJS, and not a hand-rolled HTML5 drag. That API needs a `dragstart`
    # that survives every ancestor's pointer handling -- this panel floats over
    # a canvas binding pointerdown for panning, marquee and tool arming -- and a
    # `dragover` that preventDefaults on every frame or the drop is refused
    # silently. It reordered nothing. `toolLoader.ensureSortable` restacks the
    # tool cards from a grip with six lines, and `vendor.js` puts Sortable on
    # `window` for every page that extends base.html, which this one does.
    assert "window.Sortable" in panel and 'handle: ".fb-grip"' in panel, \
        "caption reordering no longer runs on the library core already uses"
    # The listeners, not the words: the docstring on `bindSorting` names the API
    # it replaced and why, which is the half of this worth keeping.
    for gone in ('addEventListener("dragstart"', 'addEventListener("dragover"',
                 'addEventListener("drop"', 'draggable="true"'):
        assert gone not in panel, \
            f"the hand-rolled HTML5 drag left {gone} behind to compete with it"

    # "Channels only" removed the composite and kept the rest. It read as two
    # settings rather than as one either/or, and the row the remaining split
    # leaves selected is one press of Delete away from the same result. Gone from
    # the registry rather than hidden, so no surface can still offer it.
    assert "split_channels_only" not in actions, \
        "the sidebar can still offer a split mode nothing implements"
    assert "channels_only" not in (STATIC / "figureCanvas.js").read_text(
        encoding="utf-8"), "the canvas still has a split mode nothing calls"


def test_numbering_belongs_to_the_document():
    """Panel numbering -- A/B/C, a/b/c, A1/A2/A3 -- runs in reading order across
    every page of the figure. It was a select inside the image sidebar's section
    for one selected panel's own properties, so a document-wide setting was
    reachable only by selecting an image, looked like a property of that image,
    and needed a sentence under it explaining that it was not.

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


def test_a_panel_carries_one_kind_of_word_and_not_four():
    """A picture could hold a word four ways: its letter, its `title`, its
    channel `legend`, and its captions. Three of those were fixed in place --
    a title under the panel, a legend white and top-left -- and only the captions
    carried their own corner, size and colour, so the captions are what survived.

    Checked against the sources rather than through the probe because the point
    is that nothing anywhere still reads the removed keys: a renderer that kept
    a `panel.title` branch would draw a word the sidebar has no way to edit.
    """
    schema_source = (STATIC.parent / "server" / "schema.py").read_text(encoding="utf-8")
    body = schema_source[schema_source.index("def normalize_panel(raw):"):]
    body = body[:body.index("\ndef normalize_scalebar")]
    assert '"title"' not in body
    assert '"legend"' not in body

    for name in ("figureCanvas.js", "figureWorkspace.js", "figureImagePanel.js",
                 "figureQuickEdit.js", "figureSidebarController.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert "panel.title" not in source, f"{name} still reads a panel title"
        assert "panel.legend" not in source, f"{name} still reads a panel legend"

    compose_source = (STATIC.parent / "server" / "compose.py").read_text(
        encoding="utf-8")
    assert "legend_rows" not in compose_source
    assert 'panel["title"]' not in compose_source
