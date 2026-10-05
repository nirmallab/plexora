"""The browser half of an agent session, run under node.

Three probes, each the real client file against stand-ins:

  tests/js/agent_bridge_probe.mjs   services/agentBridge.js -- every command,
                                    the viewer lease and `restore_viewer`,
                                    evidence to the panel or a non-modal dialog
  tests/js/agent_panel_probe.mjs    services/orbDriver.js + views/agentPanel.js
                                    -- the panel's phases, controls, Done state,
                                    setup question and reduced motion; the
                                    viewer detached from the agent and back
  tests/js/launch_state_probe.mjs   views/viewerSidebar.js -- a slot rebuild
                                    turns off the channels it drops (the rest
                                    of that probe is tests/test_launch_state.py)

Skipped without node, like every other probe wrapper.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBES = REPO_ROOT / "tests" / "js"

PANEL_CHECKS = [
    "every phase has a label and an orb state the vendored engine draws",
    "started mounts the panel in the sidebar dock (the viewer wrapper, then body, without one), active, "
    "with an orb",
    'issued says "AI agent inspecting · CD45", draws searching and shows the progress',
    "a phase event moves the orb to thinking's state (breathing)",
    "issued shows the narration for the user, never the agent's question",
    "a marker at its limit asks in the panel; Keep going posts the answer and closes it",
    "an answer given elsewhere (the agent, another tab) closes the question here",
    "evidence feeds the thumbnail, the caption is text, and the thumb enlarges through the bridge",
    "issued evidence shows the packet's image from the captures route, and answered narrates the outcome",
    'unit_closed reads as a few words: "4 of 9 markers · CD45 accepted, moderate"',
    'Pause posts {action:"pause"} and flips the label, the button and the orb; '
    "a control event resumes",
    "the chevron minimizes to a bar in the dock that still names the phase, and the bar's chevron opens it again",
    'Stop posts {action:"stop"} and gives the viewer back at once',
    "finished: a summary, only Close, a second finished ignored, report appended, "
    "a collapsed panel reopened",
    "needs_setup opens the requirements form with features to confirm (both payload shapes); "
    "another tab's is ignored",
    "reduced motion paints one still frame per state and never asks for a frame",
    "a new started replaces the panel; with no dock it mounts under the viewer wrapper, and with neither on body",
    "text is typed in a token at a time, keeps a shared prefix, and the live region gets whole lines",
    "by_type.channel drives \"N of M channels\" (not every unit); a running bulk pass "
    "shows its own stage, and checks/cell counts appear once it hands off",
    "Plexora AI on Free: the header AI button is always shown; clicked, it says Plexora AI is under "
    "development and needs a licence, offers Close and Enter License (no trial), and Enter License goes there",
    "Plexora AI on Free: opened anyway, both buttons are disabled with the "
    "reason, nothing is asked of the gateway, and About Plexora AI explains",
    "Plexora AI: the launcher's Chat with Plexora AI closes it and opens the chat panel",
    "Plexora AI: the header sparkle opens the launcher and shuts it again; each button carries the "
    'estimate before a start ("~125 credits · 5 markers"); Gate posts /ai/v1/runs for the open '
    "project, mirrored into this tab; the sparkle stays while the session runs, and there is no corner chip",
    "Plexora AI: Gate opens an optional context step in the launcher (label, field with an example, "
    "a muted scope line, Back, Start gating with the estimate); Back returns to the tools; Enter "
    "starts and the note is sent as typed, trimmed",
    'Plexora AI: the running panel says how the note was read ("Context: melanoma · skin · all 9 '
    'markers"), a restriction names its markers, and the note as typed and what was unclear are its title',
    "Plexora AI: the launcher is a centred modal on a backdrop; Escape and the backdrop close it, "
    "and its key listener goes with it",
    "Plexora AI: the launcher shows the detected modality's tools only -- a multiplexed image with "
    "transcripts shows Multiplexed imaging (gating, QC) and no section for data with no tool; an "
    "H&E-only project shows no tool, says so, and asks the gateway for no estimate",
    "Plexora AI: a run the balance cannot pay for is disabled and says what there is; a tool the "
    'project\'s data cannot run is left out; the balance reads "2,000 credits available"',
    'Plexora AI: ai_usage draws "Plexora AI · 12 packets · 3.4 credits · 87% from cache"; a credit '
    "pause shows the two-button card (Resume, Add credits opens the top-up page); Resume posts the "
    "session to resume and the card closes",
    "Plexora AI: a gateway error shows the same card with Resume and Not now, which dismisses it",
    'Plexora AI: after a reload, a run still going on this project has its card put back at once '
    '(phase, "3 of 9 markers", paused) from GET /ai/v1/runs, not from the next event',
    'Plexora AI: Start gating puts the card up at once ("AI agent starting", "Reading your note", '
    "Pause and Stop held), and the first event takes that same card over",
    'Continue in background posts {action:"detach_viewer"}, gives the viewer back after the post, '
    "minimizes to the bar, and the bar offers Watch in viewer; a refused detach puts the toggle back",
    "Watch in viewer posts {action:\"attach_viewer\"} with this tab's view id, the bar's button goes, "
    "and expanding the bar does not re-attach",
    "the actions are one row (Continue in background, Pause, Stop) with no Take over of the panel's own; "
    "a take-over from the plugin pauses and detaches the card, and Resume keeps the viewer detached",
    "a control event's viewer_attached drives the toggle (another tab's view id does not attach this one); "
    "the reload snapshot restores a detached run; a run first heard mid-way is not assumed attached",
    "with the sidebar collapsed a live session shows the chip under the expand button; clicking it "
    "opens the sidebar and the panel",
]

LAUNCH_CHECKS = [
    "a replacing launch turns off an enabled channel it drops, and keeps the one it re-shows on",
    "...and a channel it re-places switched off is turned off too",
    "restoring a saved list does the same: CD8 off, DAPI left on",
]


def _run(name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBES / name)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)


@pytest.fixture(scope="module")
def bridge_probe():
    return _run("agent_bridge_probe.mjs")


@pytest.fixture(scope="module")
def panel_probe():
    return _run("agent_panel_probe.mjs")


@pytest.fixture(scope="module")
def launch_probe():
    return _run("launch_state_probe.mjs")


def test_the_bridge_probe_passes(bridge_probe):
    assert bridge_probe.returncode == 0, f"{bridge_probe.stdout}\n{bridge_probe.stderr}"
    assert "checks passed" in bridge_probe.stdout


def test_the_bridge_probe_checks_the_lease(bridge_probe):
    """The checks are reported on stderr as JSON; the lease's are in there by
    name only when they fail, so a pass is `failures: []` plus the count
    having grown past what the probe held before the lease existed."""
    import json

    report = json.loads(bridge_probe.stderr[bridge_probe.stderr.rindex('{\n  "checked"'):])
    assert report["failures"] == []
    assert report["checked"] >= 140


def test_the_panel_probe_passes(panel_probe):
    assert panel_probe.returncode == 0, f"{panel_probe.stdout}\n{panel_probe.stderr}"


@pytest.mark.parametrize("line", PANEL_CHECKS)
def test_each_panel_check_ran(panel_probe, line):
    assert f"PASS {line}" in panel_probe.stdout


def test_no_panel_check_was_quietly_dropped(panel_probe):
    assert panel_probe.stdout.count("PASS ") == len(PANEL_CHECKS)


@pytest.mark.parametrize("line", LAUNCH_CHECKS)
def test_a_slot_rebuild_turns_off_what_it_drops(launch_probe, line):
    assert launch_probe.returncode == 0, f"{launch_probe.stdout}\n{launch_probe.stderr}"
    assert f"PASS {line}" in launch_probe.stdout
