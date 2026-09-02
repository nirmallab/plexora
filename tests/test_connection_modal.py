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

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "connection_modal_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


def _without_comments(code):
    """`code` with its comments gone, so a test cannot fire on its own prose.

    A comment explaining why the browser must not splice `-t` into a line
    contains the string `-t`, and a scan that counted it would fail on the
    explanation for the rule it is enforcing.
    """
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"^\s*//.*$", "", code, flags=re.M)


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
    # A DATA NODE, like every other surface. Connect here used to run Plexora
    # on the far machine and tunnel the viewer back, which made this page a
    # place where the host Plexora itself runs on could be redefined from
    # inside the running app -- one concept too many. See test_one_connection
    # _concept below.
    assert "KIND_NODE" in settings
    assert "KIND_VIEWER" not in settings
    # The card keeps its own prompt box on purpose: a question can arrive at a
    # connection somebody opened from another surface entirely, and a page that
    # said "Needs your password" with nowhere to type it would be a dead end.
    assert "paintPrompt" in settings


def test_one_connection_concept_reaches_the_page_that_explains_it():
    """The machine Plexora runs on is Local; anything reached from it over SSH
    is Remote. Running Plexora ITSELF somewhere else is a launch decision made
    on that machine, not a setting inside a running Plexora -- and the page
    says which of those it is doing, because the fields on it serve both."""
    page = source("templates", "settings.html")
    assert "Plexora runs on one machine" in page
    assert "plexora connect you@login.cluster.edu" in page
    # The claim that used to be here, and is not any more.
    assert "opens it in this browser" not in page

    settings = source("src", "js", "views", "settingsPage.js")
    assert "Open remote Plexora" not in settings


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
    assert "adding a server starts from the KIND of machine you use" in probe, probe
    assert "...fetched rather than shipped in every page" in probe, probe
    assert "saving goes to the server, which composes what a preset means" in probe, probe
    assert "...and connecting follows without a second press" in probe, probe
    assert "...ending with the machine the field asked for" in probe, probe


def test_the_first_screen_is_the_machines_anyone_could_have(probe):
    """Five of the seven presets describe a KIND of machine -- any ssh host,
    any Slurm cluster, a workstation, either cloud -- and fit everybody who
    will ever open this. Two name one university's cluster and fit the people
    with an account there. In one grid of seven those two took a seventh of the
    attention from the five, and read to everybody else as evidence that this
    was a tool for somebody else's institution. So the sites are behind a
    disclosure, where the people who recognise the name will look for them.

    The split is the catalogue's own `institution` flag -- see
    test_recipes.test_a_named_institution_is_exactly_a_preset_that_fixes_the_address
    for what keeps that flag honest -- so both surfaces that draw these cards
    split them the same way without either of them deciding anything."""
    assert "adding a server starts from the KIND of machine you use" in probe, probe
    assert "...with a named institution one click further in" in probe, probe
    assert "the disclosure says what is behind it, and that it is shut" in probe, probe
    assert "...and opening it shows them, and says so to a screen reader" in probe, probe

    # And Settings draws the same two grids, because it draws the same cards
    # from the same function -- there is no second catalogue to keep in step.
    modal = source("src", "js", "services", "connectionModal.js")
    assert "recipe.institution" in modal
    settings = source("src", "js", "views", "settingsPage.js")
    assert "institution" not in settings


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
    # No srun arithmetic on this side of the wire. The Advanced box has a
    # placeholder and a value the server sent, and posts whatever is in it
    # verbatim -- what would be arithmetic is splicing a flag into a line, and
    # `_with_flag` lives on the other side of the wire, alone.
    code = _without_comments(modal)
    assert "--mem" not in code
    assert '"-t"' not in code and "'-t'" not in code
    assert "srun_extra" in code, "the split is the server's, not the browser's"


# -- installing Plexora is a step, not a background errand --------------------


def test_installing_is_one_of_the_steps_when_the_profile_asks_for_it(probe):
    """Minutes long, writes to the far machine, and the step most likely to be
    the one that failed. All three are reasons for it to be on the list
    somebody is watching rather than happening beside it."""
    assert "a profile that installs gets the step that says so" in probe, probe
    assert "...before anything is launched, because that is where it runs" in probe, probe
    assert "...and signing in is already behind it" in probe, probe
    assert ("...and a profile that installs nothing is not promised that step "
            "either") in probe, probe


def test_the_step_names_the_environment_when_there_is_one_to_name(probe):
    """Which is the difference between somebody being able to check, before it
    runs, that it is about to touch the environment they meant."""
    assert "...named plainly when the launch command names no environment" in probe, probe


def test_a_failed_install_is_marked_where_it_happened(probe):
    """"Failed" is not a step -- it is what happened to whichever step was
    running -- and pip's own account is what there is to act on."""
    assert "a failed install is marked against the install step" in probe, probe
    assert "...with the later steps left as never having run" in probe, probe
    assert "...and pip's own account still on screen" in probe, probe


def test_the_environment_the_install_writes_to_is_named_by_the_server():
    """Same rule as the srun split: the name shown to somebody about to press
    Connect comes from the same reading of the same field that builds the pip
    line. A second parser in the browser is how a step ends up promising one
    environment while pip writes to another."""
    modal = _without_comments(source("src", "js", "services", "connectionModal.js"))
    assert "installEnv" in modal
    # The name is READ, never derived. No environment flag is parsed on this
    # side of the wire -- the same rule the srun split follows, and for the
    # same reason. (The one mention of pip in the file is a sentence under the
    # switch saying what turning it on does, which is copy, not a command.)
    assert '"conda run' not in modal
    assert '"-n"' not in modal and "'-n'" not in modal
    assert '"--prefix"' not in modal and '"--name"' not in modal


# -- the dropdowns this form draws itself ------------------------------------


def test_the_dropdowns_are_drawn_rather_than_the_operating_systems(probe):
    """A native `<select>` can be styled shut and not open. The menu it opens
    is drawn by the operating system, in the system's colours, at the system's
    size, and on a dark dialog that is a white rectangle no stylesheet in this
    repository reaches. So the menu is an ordinary element.

    Two things about it are load-bearing and easy to undo. It stays **inside
    the `<dialog>`** -- the top layer carries the dialog's whole subtree, so a
    child of it paints above a fullscreen viewer while anything portalled to
    `document.body` lands behind, which is the reason the app's own
    SearchableSelect cannot be used here. And it is positioned **fixed**, from
    the trigger's own rectangle, because `.connect-modal` and
    `.connect-modal-body` both clip their overflow and an absolute menu would
    be cut off the first time one opened near the bottom of a scrolled form."""
    assert "a dropdown is drawn rather than handed to the operating system" in probe, probe
    assert "...and opens on the control being pressed" in probe, probe
    # A `<label>` forwards a click to the first labelable element inside it.
    # The trigger is a button and the menu lives in the same field, so the
    # fields that hold a dropdown are divs with `aria-labelledby` — otherwise
    # every row click would choose, close, and then be re-opened by the label.
    assert "...from a field that is not a label, so a row click cannot re-open it" in probe, probe
    modal = _without_comments(source("src", "js", "services", "connectionModal.js"))
    assert "menuSelect" in modal
    # No native select is built anywhere on this form any more -- one left
    # behind would be the one white menu on a dark dialog.
    assert 'el("select"' not in modal
    assert "form-select" not in modal


def test_escape_closes_the_menu_without_closing_the_dialog(probe):
    """The dialog closes on Escape too. A dropdown that let the key through
    would take the whole half-filled form with it, which is the single most
    annoying thing a custom menu can do."""
    assert "...and closes on Escape without taking the dialog with it" in probe, probe


# -- what the account already has, offered rather than recalled --------------


def test_the_lists_this_form_fetches_are_visible_as_lists(probe):
    """Both these fields were a `<datalist>` on a text box first, and that was
    the wrong shape. A datalist has no affordance: the buckets were fetched,
    parsed and filled, and the field still looked exactly like an empty box
    somebody had to already know a name to use. The only way to discover the
    list was to start typing the name you had opened the field to look up.

    A `<select>` says it can be opened, and carries the location and the zone
    beside each name -- which is the join this form exists to save somebody
    doing by hand in the console."""
    assert "the account's buckets are a dropdown, each saying where it lives" in probe, probe
    assert "...as a dropdown of what the project has, not a name to remember" in probe, probe


def test_a_list_is_still_only_a_convenience(probe):
    """Listing is a permission of its own. An account can have every right to
    read one bucket and no right to enumerate the project's, and that bucket is
    exactly the one somebody is here to mount -- so naming one the dropdown did
    not offer has to stay possible, and the check that follows is what decides
    whether the name is real."""
    assert "a bucket the list did not cover can still be named" in probe, probe
    assert "a bucket that cannot be read says so in Google's own words" in probe, probe


def test_whose_machine_it_is_decides_whether_install_arrives_on(probe):
    """The switch is off for every other preset because no starting point gets
    to decide that software should be installed into somebody's account on a
    machine Plexora has only read the documentation for.

    A VM Plexora rented has neither that account nor that doubt. Its mount
    chain already pip-installs on first boot, so leaving the switch off would
    mean every *later* connection ran whatever version that first boot happened
    to get — on a machine whose entire existence is Plexora's doing."""
    assert "a VM Plexora rented keeps itself up to date by default" in probe, probe
    # ...and the rule it is an exception to still holds where it belongs.
    assert ("...off on arrival, because no preset gets to decide that software "
            "should be installed into somebody's account") in probe, probe


def test_the_vm_is_given_a_way_out_and_the_box_says_it_is_not_a_way_in(probe):
    """The failure this exists for: a VM created with no public address on a
    default subnet boots, answers the tunnel, looks entirely healthy, and
    cannot reach Google's own apt repository to install Cloud Storage FUSE.
    Cloud NAT is the alternative and costs about $32 a month for the gateway,
    every month, whether or not a VM is running -- which is not a default for a
    preset whose machine is stopped between sessions.

    So the VM gets an address and a firewall rule that refuses every inbound
    connection except Google's tunnel — and the hint has to say the second
    half, because "public IP address" reads as an invitation to the internet
    and this is the opposite of one."""
    assert "a rented VM can reach out to install what it needs" in probe, probe
    assert "...and the box says the door is still shut" in probe, probe
    # And it is not offered for a machine that is not Plexora's to change.
    assert "...or offering to change somebody else's network" in probe, probe


def test_a_switch_is_operated_by_its_label_and_nothing_else_moves(probe):
    """The checkbox is a 1px invisible thing; the label is the control. What
    this pins is that toggling one leaves the form standing and leaves no
    dropdown menu open behind it — an open menu is a fixed, opaque panel with
    a z-index over the dialog, so one left open reads as the modal going
    blank."""
    assert "...and turning it off leaves the form where it was" in probe, probe


def test_the_machine_shortlist_has_both_ends_and_a_way_off_it(probe):
    """Three tiers for three reasons to be here. The default is sized for a
    40-channel pyramid; somebody checking that the bucket mounts and the tunnel
    opens should not have to rent 128 GB of RAM to find out; and the list will
    never be complete, because Compute Engine has hundreds of types and the
    interesting ones are always the ones the person asking already knows the
    name of."""
    assert "the machine type arrives at the size a pyramid actually needs" in probe, probe
    assert "...with something small enough to try the connection on" in probe, probe
    assert "...saying which of them are fractions of a core rather than cores" in probe, probe
    # Short enough to read to the end. It carried sixteen rows once, two of
    # which were 1 GB and 2 GB of RAM -- a shortlist nobody can choose from is
    # not much better than the catalogue it stands in for.
    assert "...and a shortlist short enough to read, not a catalogue" in probe, probe
    assert "...with a way out of it entirely" in probe, probe
    assert "...which is a box, because the type wanted is one nobody listed" in probe, probe


def test_listing_the_buckets_is_not_cancelled_by_checking_one(probe):
    """Two requests with two lifetimes and, for a while, one token between
    them. Changing project starts a list and then re-checks the name still in
    the field; the check bumped the shared token past the list, so the list it
    had just asked for was discarded on arrival and the previous project's
    buckets stayed on screen."""
    modal = _without_comments(source("src", "js", "services", "connectionModal.js"))
    assert "bucketListToken" in modal


# -- whose machine it is, and what it costs to leave it running --------------


def test_a_rented_machine_is_given_back_by_default(probe):
    """The two mistakes are not the same size. Leaving a 16-core VM running
    costs by the hour for as long as nobody notices; stopping it costs about
    forty seconds on the next connect. So the ending page arrives on Stop, and
    the idle timer -- the one safeguard that survives this laptop dying -- is
    already set."""
    assert "...with the one that stops the bill chosen by default" in probe, probe
    assert "...and switches itself off if it is left with nobody connected" in probe, probe


def test_a_machine_the_user_already_runs_is_not_treated_as_rented(probe):
    """One field decides three things -- whether Plexora may create a VM,
    whether it may stop one, and whether the size questions mean anything --
    and a machine somebody already runs answers no to all three. The Compute
    half of the form is hidden rather than disabled, because none of it is a
    question about their server."""
    assert ("the VM to use is the first question on the page, and a new one "
            "is the default") in probe, probe
    assert "pointing at your own VM asks which one" in probe, probe
    assert "...and stops asking what size to order, or what to bill for" in probe, probe
    assert "...or how to buy a machine that was bought long ago" in probe, probe
    assert "...or where to put one that is already somewhere" in probe, probe
    assert "the button no longer offers to create anything" in probe, probe


def test_bringing_a_vm_still_requires_naming_it(probe):
    """Plexora will not create one to make up for a blank box: being asked to
    connect to a machine is not permission to build a machine by that name."""
    assert "...and refusing to go on until a machine is actually named" in probe, probe
    assert "...and going on once one is" in probe, probe
    assert "bringing your own VM sends which one, and still sends the bucket" in probe, probe


def test_a_vm_that_already_exists_brings_its_own_location(probe):
    """The rest of this form reasons outwards from the data: the bucket picks
    the region, the region picks the zone. A machine that already exists
    inverts that -- it is somewhere, that somewhere is a fact, and refusing it
    would be refusing the only zone that can possibly be right. Being far from
    the data is still worth saying, because it costs egress; it is information
    rather than a refusal, and the offered fix cannot be "move the VM"."""
    assert "choosing a VM takes the zone it is actually in" in probe, probe
    # And a name the list did not cover does NOT inherit the bucket's zone.
    # Sending that would describe an instance in a zone it is not in, and fail
    # with "there is no VM called that" about a VM that plainly exists; cleared,
    # the server looks the machine up by name across the project instead.
    assert ("a VM the list did not cover does not inherit the bucket's zone"
            ) in probe, probe
    assert ("...though the zone stays, because a VM Plexora cannot list has one"
            ) in probe, probe
    assert ("...and says where that is, since it is a fact and not a setting"
            ) in probe, probe
    assert "...in the tense that is true of a machine already running" in probe, probe
    assert "...and without offering to move a VM that cannot be moved" in probe, probe


# -- four questions, one page at a time ---------------------------------------


def test_the_google_cloud_form_is_asked_one_page_at_a_time(probe):
    """It was one form with nineteen controls on it, six of which only meant
    anything for one of the two kinds of VM, and an Advanced panel holding the
    single most consequential question on it -- what happens to the machine
    when the session ends.

    Pages are not decoration here. A question nobody scrolls to is a question
    answered by its default, and the default in that panel was worth money
    every hour. The order is the order the answers depend on each other in:
    who you are, then where the data is, then a machine to read it with, then
    what to do with that machine afterwards."""
    assert "the Google Cloud form asks four questions, one page at a time" in probe, probe
    assert "...which is the data, because the machine is chosen to suit it" in probe, probe
    assert "the machine is asked about third, once the data is known" in probe, probe
    assert "the last page is the one nobody would have scrolled to" in probe, probe


def test_a_page_says_why_it_will_not_let_you_past(probe):
    """A disabled button with no reason beside it is the commonest way a form
    becomes unusable: the control that would explain the refusal is the one
    that has been switched off. So the sentence sits next to the button, and
    it names the field."""
    assert "a page with every answer in it can be left" in probe, probe
    assert "nothing goes past the data page without a bucket" in probe, probe
    assert "...and the reason is beside the button that will not go" in probe, probe
    assert "...and nothing below it can be reached until it is answered" in probe, probe


def test_going_back_keeps_every_answer(probe):
    """Every control on all four pages is made once and only ever hidden.
    A wizard that rebuilt the page it returns to would have to remember the
    answers separately from the controls holding them, and the two would
    disagree the first time a lookup came back late."""
    assert "going back does not lose what was already chosen" in probe, probe
    assert "...and a page already answered can be jumped straight back to" in probe, probe
    assert "...which is what the strip at the top is for" in probe, probe
    # Forward is through the questions, never around them.
    assert ("...and will not skip ahead to a page whose questions are "
            "unanswered") in probe, probe


def test_how_the_machine_is_bought_is_a_question_on_the_form(probe):
    """Spot is the same hardware at 60-91% off, on the condition that Google
    may take it back. For a long-running server that is a serious risk; here it
    is an interruption -- the data is in the bucket rather than on the machine,
    and Plexora asks for a preempted VM to be stopped rather than deleted, so
    the disk with the environment on it survives and reconnecting starts the
    same machine. What preemption costs is a reconnect. What Standard costs is
    three to ten times the hourly rate, every hour, forever."""
    assert ("a new VM is bought at the spot price unless somebody says "
            "otherwise") in probe, probe
    assert "...with what that trade actually is, under the choice" in probe, probe
    assert "...and the other side of it when the other side is chosen" in probe, probe


def test_the_three_endings_are_a_question_rather_than_a_switch(probe):
    """A boolean could only offer "keep paying for compute" or "keep paying for
    the disk". The third answer -- a VM that is deleted and costs nothing at
    all afterwards -- had no way to be expressed, and it is the right one for
    somebody who connects once a week.

    All three are on screen together because the whole difficulty of the
    question is comparing them, and the explanation under the group follows the
    selection: one sentence at a time rather than a wall of three."""
    assert ("all three endings are on screen together, not hidden in a "
            "dropdown") in probe, probe
    assert "...and what each costs said under the one selected" in probe, probe
    assert "...one sentence at a time, following the choice" in probe, probe
    assert ("...including the two answers that decide what a session costs"
            ) in probe, probe
    # And nothing else on it. The page used to carry the recipe's notes as
    # well -- billing, IAP roles, Spot, what Delete does not touch -- none of
    # which is about the question being asked here, and each of which had
    # already been said on the page it applied to.
    assert "...and the page is the question, not a page of notes about it" in probe, probe


def test_a_spot_refusal_is_a_button_rather_than_an_instruction(probe):
    """A zone with no spare Spot capacity is a price problem, not a broken
    configuration: the zone, the machine and the bucket were all right, and
    the same request at full price very likely succeeds this minute. Acting on
    the sentence alone means leaving the failure, opening Settings, finding
    the profile, reaching the third page of the form and changing one radio.

    The key comes from the server beside the error rather than from reading
    the error here, which is what keeps the button off every OTHER failure --
    a button that offers to change somebody's configuration must never appear
    on a guess."""
    assert ("a spot refusal offers the fix as a button, not a sentence to act "
            "on") in probe, probe
    assert "...beside the two ways out that suit every other failure" in probe, probe
    assert "...which changes what the profile buys, and then retries it" in probe, probe
    assert ("a failure with no named fix offers no button to guess at one"
            ) in probe, probe


def test_delete_is_not_offered_for_a_machine_plexora_did_not_make(probe):
    """Greyed rather than removed, with the reason under it: a row that
    vanished would read as a shorter list, and a row that is simply grey reads
    as a bug. The server refuses the same thing twice more -- on the saved
    record, and on the label written to the instance itself -- but a row that
    is only ever going to be refused should not be pressable here either."""
    assert ("Plexora will not offer to delete a machine it did not create"
            ) in probe, probe
    assert ("...and says why the row is greyed rather than leaving it a "
            "mystery") in probe, probe
    assert "...and never asks for it to be deleted" in probe, probe
