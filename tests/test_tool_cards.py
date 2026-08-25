"""Loaded tools as cards: collapse, on/off, remove, and drag to restack.

Three states, deliberately kept apart, because collapsing any two of them is
what made a second plugin unusable: LOADED (record and panel exist, nothing
drawn), VISIBLE (contributes a layer), ACTIVE (the shared controls act on it,
and its panel is expanded). Opening a tool makes it all three and stands the
previous one down to LOADED; turning another card's eye back on is what stacks
them.

None of this is reachable from the Python suite, which renders panels one tool
at a time and never runs two. It needs two tools, a DOM, and a sequence of
clicks -- which is what tests/js/tool_cards_probe.mjs does.

Each mutation below reinstates a real, silent failure: layers stacked upside
down, a second tool left drawing over the one just opened, and a stale mount in
the off-screen slot that breaks re-opening a removed tool. All three look like
rendering bugs rather than logic errors, which is exactly why they are pinned.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tool_cards_probe.mjs"
TOOL_LOADER = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views" / "toolLoader.js"

#: Sidebar order reads downwards from the top layer; core stacks bottom-first.
TOP_CARD_IS_TOP_LAYER = "        names.reverse();"

#: Opening a tool turns the previous one's layer off. Without it, "single active
#: by default" is only a claim about panels. Both lines live in `fold()`, which
#: standDown() and the coexisting-pair teardown share.
STANDS_THE_LAYER_DOWN = """        entry.collapsed = true;
        if (!entry.pinned) applyToolVisible(toolName, false);"""
LEAVES_IT_DRAWING = "        entry.collapsed = true;"

#: Remove clears the tool out of every slot, including the off-screen one.
CLEARS_EVERY_SLOT = "        entry.slotIds.forEach((slotId) => {"
CLEARS_THE_CARD_SLOT = "        [CARD_SLOT].forEach((slotId) => {"

#: The Tools row is a toggle. Its shortcut is a synthetic click on that same row
#: (keyboardShortcuts.js), so a row that only ever opens is a key that only ever
#: opens -- and pressing it again to put the tool away does nothing at all.
ROW_IS_A_TOGGLE = """        if (isOpen(toolName)) closeTool(toolName);
        else openTool(toolName, linkEl);"""
ROW_ONLY_OPENS = "        openTool(toolName, linkEl);"

#: A tool with no card closes through the plugin's own close, because that is
#: the only thing that knows what closing it costs.
ASKS_THE_PLUGIN_TO_CLOSE = """        if (!entry.sidebarController?.close) {
            removeTool(toolName);
            return;
        }"""
REMOVES_IT_REGARDLESS = """        removeTool(toolName);
        return;"""


def _run(source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [node, str(PROBE)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _mutate(tmp_path, old, new):
    """Write a toolLoader.js with one behaviour reverted.

    Bytes rather than write_text: this repo's JS is LF, and Windows' newline
    translation would rewrite the whole file -- harmless for the probe, but it
    makes a diff of the mutation unreadable and would break any marker-based
    slicing added later.
    """
    source = TOOL_LOADER.read_text(encoding="utf-8")
    assert old in source, "the lines this test mutates have moved or been renamed"
    mutated = tmp_path / "toolLoader.js"
    mutated.write_bytes(source.replace(old, new).encode("utf-8"))
    return mutated


@pytest.fixture(scope="module")
def probe_report():
    return _run()


def test_every_card_rule_holds(probe_report):
    returncode, report = probe_report
    assert not report["problems"], (
        "the sidebar cards are not behaving as layers:\n"
        + "\n".join(f"  - {p}" for p in report["problems"])
    )
    assert returncode == 0


def test_opening_a_tool_stands_the_previous_one_down(probe_report):
    """Single active by default. The previous tool folds away AND stops drawing,
    so opening a second tool shows one picture rather than two stacked ones
    nobody asked to compare -- while staying loaded, so one click brings it
    back."""
    _, report = probe_report
    calls = report["coreCalls"]
    assert "layer:gating:false" in calls
    assert calls.index("layer:gating:false") < calls.index("layer:cell_explorer:true")


def test_a_layer_can_be_turned_back_on_without_selecting_the_tool(probe_report):
    """The whole feature. Visible and active are separate, so a background tool's
    layer can be stacked under the one being worked on."""
    _, report = probe_report
    lifecycle = report["lifecycle"]
    assert "gating:layer:true" in lifecycle
    # The eye reaches a controller that has no cell layer of its own (ROI's
    # case), which is the only way its card's toggle can do anything.
    assert lifecycle.count("gating:layer:true") > 1


def test_a_layer_the_user_pinned_survives_switching_cards(probe_report):
    """The single-active default is for the FIRST switch. Once somebody has put a
    second layer back on by hand in order to compare the two, clicking between
    their cards has to stop taking it away again -- otherwise the stack is
    unusable exactly when it is being used. Same shape as a layer's `userMode`:
    an automatic choice fills a gap, an explicit one is kept."""
    _, report = probe_report
    assert not report["problems"]  # the probe asserts the sequence itself


def test_the_top_card_is_the_top_layer(tmp_path):
    """Core stacks bottom-first and the sidebar reads downwards, so the DOM
    order is reversed on the way out. Getting it backwards draws the picture
    upside down, which reads as a rendering bug rather than a list bug."""
    returncode, report = _run(_mutate(tmp_path, TOP_CARD_IS_TOP_LAYER, ""))
    assert returncode == 1
    assert any("TOP layer" in problem for problem in report["problems"]), report["problems"]


def test_the_probe_sees_a_tool_left_drawing_after_a_switch(tmp_path):
    returncode, report = _run(
        _mutate(tmp_path, STANDS_THE_LAYER_DOWN, LEAVES_IT_DRAWING))
    assert returncode == 1
    assert any("layer drawing" in problem for problem in report["problems"]), report["problems"]


def test_the_menu_row_closes_the_tool_it_opened(probe_report):
    """One row, both ways round -- and therefore one key, both ways round.

    keyboardShortcuts.js fires a synthetic click on the row and knows nothing
    about tools, so whatever the row does is what the shortcut does. Closing
    folds the card away and keeps the tool loaded: throwing away a cached column
    and a rebuilt lookup table on a keystroke somebody may have pressed twice by
    accident is what the card's X is for, deliberately.
    """
    _, report = probe_report
    assert not report["problems"]
    # Exactly one teardown across the whole run, and it is the card's X. A
    # second would mean the menu row had unloaded the tool as well.
    assert report["coreCalls"].count("deactivate:gating") == 1
    # And the tool with no card went the other way, through its own close.
    assert "figure_builder:close" in report["lifecycle"]


def test_the_probe_sees_a_row_that_only_ever_opens(tmp_path):
    returncode, report = _run(_mutate(tmp_path, ROW_IS_A_TOGGLE, ROW_ONLY_OPENS))
    assert returncode == 1
    assert any("did not close it" in problem
               for problem in report["problems"]), report["problems"]


def test_the_probe_sees_a_card_less_tool_closed_behind_the_plugin_s_back(tmp_path):
    """Figure Builder has no card, so no X -- its dock carries its own Close, and
    that Close asks before discarding captures that are not in a figure yet.
    Removing the tool from here instead skips the question and loses the work."""
    returncode, report = _run(
        _mutate(tmp_path, ASKS_THE_PLUGIN_TO_CLOSE, REMOVES_IT_REGARDLESS))
    assert returncode == 1
    assert any("own close" in problem
               for problem in report["problems"]), report["problems"]


def test_the_probe_sees_a_mount_left_behind_in_the_off_screen_slot(tmp_path):
    """The one that breaks re-opening a removed tool: mountFor finds the stale
    wrapper, returns it as if it were new, and the freshly fetched fragment is
    written into markup the new controller never saw."""
    returncode, report = _run(
        _mutate(tmp_path, CLEARS_EVERY_SLOT, CLEARS_THE_CARD_SLOT))
    assert returncode == 1
    assert any("off-screen mount survived" in problem
               for problem in report["problems"]), report["problems"]
