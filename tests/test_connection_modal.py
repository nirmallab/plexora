"""One dialog for connecting to another machine, wherever you came from.

The behaviour is in tests/js/connection_modal_probe.mjs, run in node below.
What is left here is the wiring -- that every entry point now leads to it, and
that the two surfaces it replaced no longer do the job themselves:

  settingsPage.js   Connect opens the modal (a viewer connection)
  placePicker.js    Connect opens the modal (a data node) and has no prompt
                    box of its own left
  dataLocation.js   a field flipped to Remote with nothing connected opens it
  base.html         loads it before all three

The server halves are tests/test_remote_connect.py (states, prompts, the log
tail and the ?log= knob) and tests/test_remote_state.py (the shared poll the
modal subscribes to).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "connection_modal_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


# -- what it shows while it is slow ------------------------------------------


def test_the_steps_are_the_servers_own_states(probe):
    """Five things happen and they take wildly different amounts of time. A
    spinner says the same thing about a two-second login and a quarter-hour
    queue, and it was the queue that made the old flow feel broken when it was
    merely slow."""
    assert "the step being waited for is the one that is active" in probe, probe
    assert "...and everything before it is already done" in probe, probe
    assert "the server's own sentence is what is shown, not a translation" in probe, probe


def test_a_step_that_will_never_happen_is_not_drawn(probe):
    """Only a profile that runs Plexora inside a job waits for a scheduler.
    Promising that wait to somebody connecting to a plain ssh host is a
    sentence they will spend the connection wondering about."""
    assert "a plain ssh host is not promised a scheduler wait" in probe, probe
    assert "a profile that waits in a queue gets the step that says so" in probe, probe


def test_a_data_node_is_not_described_as_a_viewer(probe):
    """Same login, same password, different thing at the far end -- and the
    last step is the only one where that difference is visible."""
    assert "a data node's last step is not called starting Plexora" in probe, probe


# -- the question ssh asked --------------------------------------------------


def test_a_redraw_does_not_eat_a_half_typed_password(probe):
    """The dialog re-renders every second and the box is inside it."""
    assert "a redraw while the same question stands leaves the box alone" in probe, probe
    assert "sending hands the answer over and clears the box" in probe, probe


def test_a_redraw_does_not_throw_away_the_log_being_read(probe):
    """The other thing in this dialog whose state the DOM owns rather than the
    script. Rebuilding the pane every second put a reader back wherever a fresh
    element starts -- which looked like it worked, because that position was
    usually near the one they had just scrolled to."""
    assert "the log pane survives a redraw rather than being replaced" in probe, probe
    assert "scrolling up to read stops it yanking itself back down" in probe, probe
    assert "...and scrolling back to the bottom sets it following again" in probe, probe


def test_a_redraw_does_not_take_the_focus_off_a_button(probe):
    """Rebuilding the action row every tick blurred whichever button somebody
    had tabbed to -- once a second, for the whole of a queued job."""
    assert "the buttons are not rebuilt while they are still the same buttons" in probe, probe


def test_masking_follows_the_question_not_the_fact_of_one(probe):
    """A fingerprint typed into a row of dots is unanswerable, next to the
    fingerprint it is being checked against."""
    assert "a password question is masked" in probe, probe
    assert "...and the question is shown exactly as ssh asked it" in probe, probe
    assert "a host-key question is answerable in the clear" in probe, probe
    assert "...with the two answers ssh accepts, as buttons" in probe, probe
    assert "...which send what ssh expects" in probe, probe


# -- when it goes wrong ------------------------------------------------------


def test_a_failure_is_drawn_where_it_happened_with_the_log_intact(probe):
    """"failed" is not a step -- it is what happened to whichever step was
    running -- and the actionable line is almost always in the log."""
    assert "the step that was running is the one marked failed" in probe, probe
    assert "...and what went wrong is said in words" in probe, probe
    assert "...with the log still on screen, where the reason usually is" in probe, probe
    assert "a failure offers the two things that can help" in probe, probe
    assert "trying again starts a new connection rather than a new dialog" in probe, probe


# -- closing is not cancelling -----------------------------------------------


def test_the_window_and_the_connection_are_different_things(probe):
    """The ssh belongs to the server and a queued job is a real fifteen
    minutes. Exactly one button here ends it, and it says so."""
    assert "while opening, the way out says it leaves the connection running" in probe, probe
    assert "...and the button that ends it says that instead" in probe, probe
    assert "closing the window disconnects nothing" in probe, probe
    assert "stopping ends the connection and the errand together" in probe, probe


def test_joining_a_connection_somebody_else_started_is_not_an_error(probe):
    """Another data field, or Settings in another tab, may have opened it --
    and watching it is exactly what this dialog is for. The server refuses the
    second POST with a 409, which used to be reported as a failed connection."""
    assert "a connection already on its way is watched, not restarted" in probe, probe
    assert ("a refusal from a connection that IS opening is not shown as failure"
            in probe), probe
    assert "a machine already connected resolves immediately" in probe, probe


def test_it_resolves_with_what_the_caller_has_to_do_next(probe):
    """A data field addresses a NODE, whose name is not necessarily the
    profile's."""
    assert "connecting resolves with the node a field has to address" in probe, probe
    assert "with no machine named, the dialog asks which" in probe, probe
    assert "...saying which of them is already up" in probe, probe
    assert "choosing a connected machine takes it as the answer" in probe, probe


# -- every entry point leads here --------------------------------------------


def test_the_settings_page_connects_through_the_modal():
    settings = source("src", "js", "views", "settingsPage.js")
    assert "PlexoraConnectionModal.open" in settings
    assert "KIND_VIEWER" in settings
    # The card keeps its own prompt box on purpose: a question can arrive at a
    # connection somebody opened from another surface entirely, and a page that
    # said "Needs your password" with nowhere to type it would be a dead end.
    assert "promptBox" in settings


def test_the_machine_picker_no_longer_asks_for_passwords():
    """It asks one question -- which machine -- and used to answer a second
    one badly, with a state chip, a password box and a poller that all existed
    a second time in Settings."""
    picker = source("src", "js", "services", "placePicker.js")
    assert "promptRow" not in picker
    assert "place-picker-prompt" not in picker
    assert "PlexoraConnectionModal.open" in picker


def test_a_field_with_nothing_connected_opens_the_modal():
    location = source("src", "js", "services", "dataLocation.js")
    assert "PlexoraConnectionModal" in location
    assert "onlyPlace" in location


def test_the_modal_is_loaded_before_everything_that_opens_it():
    base = source("templates", "base.html")
    assert base.index("services/remoteState.js") < base.index("services/connectionModal.js")
    assert base.index("services/connectionModal.js") < base.index("services/placePicker.js")
    assert base.index("services/connectionModal.js") < base.index("services/dataLocation.js")


def test_the_modal_styles_ship_where_every_page_loads_them():
    """It opens over the viewer as well as over the import and settings pages,
    so its CSS cannot live in import.css."""
    main = source("src", "css", "main.css")
    for rule in (".connect-modal", ".connect-steps", ".connect-log-body",
                 ".connect-prompt"):
        assert rule in main
    assert ".connect-modal" not in source("src", "css", "import.css")


# -- adding a server ---------------------------------------------------------


def test_a_machine_can_be_added_without_leaving_the_dialog(probe):
    """The old way out of "no servers saved yet" was a link to Settings, which
    means leaving the import form somebody was halfway through filling in."""
    assert "with nothing saved at all, the dialog still offers a way forward" in probe, probe
    assert "adding a server starts from the machine you use" in probe, probe
    assert "...fetched rather than shipped in every page" in probe, probe
    assert "saving goes to the server, which composes what a preset means" in probe, probe
    assert "...and connecting follows without a second press" in probe, probe
    assert "...ending with the machine the field asked for" in probe, probe


def test_a_preset_we_have_not_verified_says_so_before_it_is_chosen(probe):
    """And a generic shape does not, because it asserts nothing about anybody's
    machine -- a badge there would devalue the ones that need it."""
    assert "a preset we have not connected with says so before it is chosen" in probe, probe
    assert "...and a generic shape carries no badge to devalue that one" in probe, probe


def test_a_poll_does_not_eat_the_form_being_filled_in(probe):
    """The dialog re-renders every second, and the boxes are inside it -- the
    same hazard as the password box, in a place that takes longer to fill."""
    assert "a poll while the form is open leaves what was typed alone" in probe, probe
    assert "a preset asks only for what genuinely differs" in probe, probe
    assert "...and says what the site expects, in sentences" in probe, probe


def test_composing_a_preset_happens_in_one_place():
    """The browser posts the answers; the server turns them into a profile.
    Two implementations of what "HMS O2" means would drift, and the one in the
    browser would be the one nothing could test against a real cluster."""
    modal = source("src", "js", "services", "connectionModal.js")
    assert "settings/recipes/" in modal
    # No srun arithmetic on this side of the wire.
    assert "--mem" not in modal
    assert "-p interactive" not in modal
