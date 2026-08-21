"""Two tools, one slot: opening one must not destroy the other.

`#tool_panel_slot` is shared by every plugin that declares it (see
plexora/api/plugin.py's `Plugin.panels`), and toolLoader.js filled it with
`slot.innerHTML = fragment` -- a whole-slot replace. That was correct for as
long as gating was the only plugin. With a second one it deletes the first
tool's panel out of the DOM while its sidebar controller keeps the element
handles it took at setup(), and the re-open path -- which only unhides the slot
-- then shows the wrong markup behind a live controller wired to nothing.

The other half is `onHide()`. A panel can be hidden and left running, and for a
sidebar of widgets that is exactly right: it is what makes reopening instant.
It is wrong for a tool that reaches outside its panel. ROI puts pointer handlers
on the viewer canvas and a keydown listener on the document; left attached
behind another tool's panel, both tools act on the same keypress and the hidden
one draws on the image. So switching away has to tell the tool, not just hide it.

Neither failure is visible to anything else here: the Python suite renders
panels one tool at a time and never runs two, and the existing asset probe opens
a single tool. The failure needs two tools and a switch, which is what
tests/js/tool_switch_probe.mjs does.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tool_switch_probe.mjs"
TOOL_LOADER = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views" / "toolLoader.js"

#: The per-tool mount, and the whole-slot write it replaced. Mutating one into
#: the other reinstates the bug exactly as it stood.
PER_TOOL_MOUNT = """                const mount = mountFor(slotId, toolName, true);
                if (mount) mount.innerHTML = payload.fragments[slotId];"""
SHARED_SLOT = """                const mount = document.getElementById(slotId);
                if (mount) mount.innerHTML = payload.fragments[slotId];"""

#: The boot path, and the hand-rolled version of it that skipped onShow().
BOOT_THROUGH_SHOW = "        show(toolName);\n    }"
BOOT_BY_HAND = "        activeToolName = toolName;\n        paint();\n    }"


def _run_probe(source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [node, str(PROBE)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    # The probe reports on stderr so its own diagnostics never mix with output.
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


@pytest.fixture(scope="module")
def probe_report():
    return _run_probe()


def test_switching_tools_keeps_both_panels(probe_report):
    returncode, report = probe_report
    assert not report["problems"], (
        "switching between two tools left the sidebar in a broken state:\n"
        + "\n".join(f"  - {p}" for p in report["problems"])
    )
    assert returncode == 0


def test_the_tool_switched_away_from_is_told_to_stand_down(probe_report):
    """The half that matters for a tool drawing on the image. Asserted on the
    call order too: telling a tool to hide after the next one is already showing
    leaves a window where both are armed."""
    _, report = probe_report
    lifecycle = report["lifecycle"]
    assert "gating:hide" in lifecycle
    assert lifecycle.index("gating:hide") < lifecycle.index("roi:show")


def test_reopening_a_tool_shows_its_own_markup(probe_report):
    """The re-open path never re-fetches -- it unhides what is already there --
    so it is only correct if the panel it unhides is still the right one."""
    _, report = probe_report
    assert report["after_return"]["gating"]["hidden"] is False
    assert report["after_return"]["roi"]["hidden"] is True
    assert "gate_marker_section" in report["after_return"]["gating"]["html"]


def test_a_server_rendered_tool_is_brought_up_the_same_way(probe_report):
    """`?tool=roi` renders the panel server-side, so the Tools-menu path never
    runs; main.js reports the already-live tool through registerLoaded instead.

    Both routes have to end in the same place. When registerLoaded set the
    active tool by hand, a bookmarked ?tool=roi gave a panel that looked
    entirely correct and had no onShow() behind it -- so ROI's pointer handlers
    and keyboard shortcuts, which are attached there, never went on. Drawing did
    nothing, and there was no error to say why."""
    _, report = probe_report
    assert "boot-roi:show" in report["boot_lifecycle"]


def test_the_probe_can_actually_fail(tmp_path):
    """Run it against the loader writing the shared slot directly -- the code
    exactly as it shipped -- and it must report the loss."""
    mutated = tmp_path / "toolLoader.js"
    source = TOOL_LOADER.read_text(encoding="utf-8")
    assert PER_TOOL_MOUNT in source, "the lines this test mutates have moved or been renamed"
    mutated.write_text(source.replace(PER_TOOL_MOUNT, SHARED_SLOT), encoding="utf-8")

    returncode, report = _run_probe(mutated)

    assert returncode == 1
    assert report["problems"], "the probe cannot see one tool overwriting another's panel"


def test_the_probe_catches_a_boot_path_that_skips_onshow(tmp_path):
    """The exact code registerLoaded used to run, reinstated."""
    mutated = tmp_path / "toolLoader.js"
    source = TOOL_LOADER.read_text(encoding="utf-8")
    assert BOOT_THROUGH_SHOW in source, "the line this test mutates has moved or been renamed"
    mutated.write_text(source.replace(BOOT_THROUGH_SHOW, BOOT_BY_HAND), encoding="utf-8")

    returncode, report = _run_probe(mutated)

    assert returncode == 1
    assert any("onShow" in problem for problem in report["problems"]), report["problems"]
