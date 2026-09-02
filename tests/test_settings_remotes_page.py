"""The Settings page's server cards, and the log that is on them.

The behaviour is in tests/js/settings_remotes_probe.mjs, run in node below.
What is left here is what the probe cannot see: which file the terminal comes
from, and that the page no longer claims Plexora can be moved somewhere else
from inside itself.

The connection dialog those cards open is tests/test_connection_modal.py; the
poll they subscribe to is tests/test_remote_state.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "settings_remotes_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def _run_probe(path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(path)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


@pytest.fixture(scope="module")
def probe():
    return _run_probe(PROBE)


@pytest.fixture(scope="module")
def probe_modal():
    """The connection dialog's probe, for the questions this page used to ask.

    Its own form is gone -- the preset's form asks them now -- so the checks
    that pin what a saved server can say are over there. Read from here as
    well, because it is this page's Edit button that opens it."""
    return _run_probe(REPO_ROOT / "tests" / "js" / "connection_modal_probe.mjs")


# -- the log ------------------------------------------------------------------


def test_the_log_keeps_its_place_through_a_poll(probe):
    """The reported bug, and it was worse than it looked. The cards were thrown
    away and rebuilt on every update, so the pane was a NEW element once a
    second -- and a new element starts at the top. Reading anything in a live
    connection's log was impossible for exactly as long as the connection was
    live, which is the only time it matters."""
    assert "the log is on the card, as a terminal" in probe, probe
    assert "the pane survives the poll rather than being replaced" in probe, probe
    assert "...and scrolling up to read stops it yanking itself back down" in probe, probe
    assert "...and coming back to the bottom sets it following again" in probe, probe
    assert "...pinned to the bottom while nobody is reading it" in probe, probe


def test_the_far_machines_own_output_is_in_there_and_marked(probe):
    """ssh relays the remote command's stdout and stderr on one stream (they
    are merged in connect._Watched), and every line of it is already echoed
    into the session log. What was missing was any sign of which lines those
    were -- so the far machine's own words are marked as its own, in the same
    stream and in order, because the interesting moments are exactly where
    Plexora's narration and the machine's output interleave."""
    assert "...with what the far machine said marked as its own" in probe, probe


def test_opening_a_log_asks_for_the_whole_tail(probe):
    """The list payload carries eight lines, which is enough for a card to say
    something and never enough to diagnose anything. Two hundred is what the
    server keeps, and it is fetched for the connection somebody has just said
    they want to read -- not for every card, and not on a timer."""
    assert "a closed log asks for no deep tail" in probe, probe
    assert "opening one asks for that connection's whole log" in probe, probe
    assert "...and asks for it now, rather than at the next tick" in probe, probe
    assert "...and the deeper answer is what is drawn once it arrives" in probe, probe


def test_one_terminal_implementation_for_both_surfaces():
    """The connection modal and these cards show the same log for the same
    connection. Two implementations of "a terminal follows its own output" is
    how one of them ends up not doing it."""
    settings = source("src", "js", "views", "settingsPage.js")
    modal = source("src", "js", "services", "connectionModal.js")
    assert "PlexoraLogTerminal.create" in settings
    assert "PlexoraLogTerminal.create" in modal
    # Neither keeps a second copy of the scroll arithmetic.
    for text in (settings, modal):
        assert "scrollHeight" not in text
    base = source("templates", "base.html")
    assert base.index("services/logTerminal.js") < base.index(
        "services/connectionModal.js")


# -- what a poll must not disturb ---------------------------------------------


def test_a_poll_disturbs_nothing_somebody_is_using(probe):
    """Three things on the card hold state the DOM owns rather than the script:
    the log's scroll position, the password box's contents, and which button
    has the focus. All three used to be destroyed once a second; two of them
    had hand-written workarounds, which this removes rather than improves."""
    assert "a poll repaints the card rather than replacing it" in probe, probe
    assert "...including the button somebody may have tabbed to" in probe, probe
    assert "a poll while the same question stands leaves the box alone" in probe, probe


# -- what a saved server's card is for ----------------------------------------


def test_a_card_leads_with_the_machine_rather_than_with_its_address(probe):
    """These cards led with `aj@o2.hms.harvard.edu`, then the machine type, the
    bucket, the operating system and the environment pip writes to. That is the
    answer to a question nobody asks of a list of their own machines: by the
    time a profile is saved, the person reading it chose all of that months ago
    and wants to know which machine this is and whether it is up.

    So the face carries the recipe's own words for the kind of machine and
    stops there. Not even on a tooltip: a hover that shows configuration is
    still configuration on the card, it is just configuration nobody can find.
    All of it is asked for, and read back, on the form behind the pencil."""
    assert "...saying what kind of machine it is, in words" in probe, probe
    assert ("...with the address nowhere on the card, tooltip included"
            in probe), probe
    assert "the settings a profile carries stay off its card" in probe, probe


def test_the_saved_servers_are_a_grid_rather_than_a_stack():
    """Three across. A saved server is a small object now -- a name, a kind of
    machine, a dot and one button -- and a column of full-width cards each
    carrying four words was a list pretending to be a stack of panels, with the
    fourth machine below the fold on a laptop."""
    styles = source("src", "css", "settings.css")
    block = styles[styles.index(".settings-remotes {"):]
    block = block[:block.index("}")]
    assert "display: grid" in block
    assert "repeat(3, minmax(0, 1fr))" in block
    # Stretched, not `start`. `start` was right for the card that grows a
    # password prompt and wrong for the other ninety-nine percent of the time,
    # where it left three cards holding identical content at three heights
    # because one description wrapped.
    assert "align-items: stretch" in block


def test_the_saved_servers_sit_in_a_box_of_their_own():
    """Two objects on the page, not one long run of cards. The saved half and
    the catalogue were a grid, a heading and another grid on one flat
    background, so the presets read as more saved servers with the wrong
    buttons on them."""
    page = source("templates", "settings.html")
    assert "settings-remotes-box" in page
    assert ">Saved connections</h3>" in page
    assert "then reconnect by name" in page
    # Labelled by its own title rather than by an aria-label repeating it: a
    # heading that is on the page is the accessible name.
    assert 'aria-labelledby="settings_saved_title"' in page

    styles = source("src", "css", "settings.css")
    block = styles[styles.index(".settings-remotes-box {"):]
    block = block[:block.index("}")]
    assert "border: 1px solid var(--border-subtle)" in block
    assert "background: var(--surface-1)" in block
    # And the cards inside it are a step lighter than the box, which is what
    # makes them read as cards in a container rather than panels on a page.
    card = styles[styles.index(".settings-remote-card {"):]
    assert "background: var(--surface-2)" in card[:card.index("}")]


def test_a_long_description_cannot_make_one_card_taller_than_its_row():
    """Equal cards or the grid is a stack with gaps in it. The clamp is what
    stops a wrapped description setting the height of the row, and the
    two-line minimum is what lines a card in the second row up with a card in
    the first."""
    styles = source("src", "css", "settings.css")
    block = styles[styles.index(".settings-remote-description {"):]
    block = block[:block.index("}")]
    assert "-webkit-line-clamp: 2" in block
    assert "min-height" in block
    # .settings-meta sets `pre-line`, which a clamped box must not honour.
    assert "white-space: normal" in block

    # And Connect on the bottom edge of all of them: `auto` above the actions
    # row, not padding below the description, which is what leaves one card's
    # button stranded in the middle of a taller neighbour.
    actions = styles[styles.index(".settings-remote-actions {"):]
    assert "margin-top: auto" in actions[:actions.index("}")]


def test_the_state_is_a_dot_that_can_still_be_read_out(probe):
    """A word per card, in the head, in capitals, was the loudest thing on a
    panel whose cards mostly say "Not connected". A dot says the same thing at
    a glance and says it in colour -- the same colour, from the same classes,
    as every other machine on this page. What a dot cannot do is be read
    aloud, so the word is its accessible name and its tooltip."""
    assert "...and its state as a dot that is still named" in probe, probe
    page = source("src", "css", "settings.css")
    # The colour has one definition. A second palette for the dot is how a
    # connecting card ends up amber here and grey four inches away.
    assert "settings-remote-dot" in page
    assert "background: currentColor" in page


def test_the_two_things_you_can_do_TO_a_server_are_not_buttons(probe):
    """Connect is what a card is for. Edit and Delete are housekeeping, and as
    three equal buttons in a row -- four on a cloud profile, six with the VM
    controls -- the card had no primary action at all. They are icons in the
    head now, named for a screen reader and for a hovering mouse both."""
    assert ("...with Edit and Delete as named icons above, not buttons below"
            in probe), probe
    assert "editing a server opens the preset it was described in" in probe, probe


def test_deleting_a_server_asks_first(probe):
    """It did not use to, for anything but a rented machine. That was
    defensible while this was a button in a row of buttons with the word
    "Forget" written on it. It is not defensible for a bin icon sitting eight
    pixels from a pencil with no words on either."""
    assert ("deleting a server asks first, and says what is not being deleted"
            in probe), probe
    assert "...and no means no" in probe, probe
    assert "...and yes drops the profile" in probe, probe


def test_a_forgotten_server_leaves_no_stale_card(probe):
    """The other half of keeping the cards: they have to go when the profile
    does, or a card outlives the thing it describes."""
    assert ("forgetting the last server leaves the empty note, not a stale card"
            in probe), probe


# -- one kind of connection ---------------------------------------------------


def test_connect_here_means_what_it_means_everywhere(probe):
    """A data node on the other machine, through the dialog every other surface
    opens. See test_connection_modal.test_one_connection_concept."""
    assert "Connect opens the shared dialog, for a DATA NODE" in probe, probe
    assert "...and nothing here offers to move Plexora itself" in probe, probe
    assert "...and sending it answers the NODE's prompt, not a viewer's" in probe, probe
    assert "...which ends the data node, not somebody's viewer" in probe, probe


def test_the_presets_are_how_a_server_is_added(probe):
    """They shipped reachable only by flipping a data field to Remote with
    nothing saved -- the one place somebody adding their first server was not
    looking. Then they were a button beside a hand-written form that asked
    seven of the same questions. Now they ARE the way in: the catalogue is on
    the page, where that form used to be."""
    assert "the presets are on the page, where a second form used to be" in probe, probe
    assert "...drawn by the dialog's own card, not a second copy of it" in probe, probe
    assert "choosing one opens that preset's form, not the catalogue again" in probe, probe

    # The catalogue's own titles were invisible here and legible in the dialog,
    # from the same markup: `.connect-recipe` is a <button> that says
    # `color: inherit`, which reaches the dialog's `--text-primary` on one
    # surface and a <body> that sets no colour at all on the other -- so the
    # label fell back to the browser's default button text, which is black, on
    # a near-black card. The blurb survived because it names its colour.
    styles = source("src", "css", "settings.css")
    block = styles[styles.index(".settings-recipes {"):]
    assert "color: var(--text-primary);" in block[:block.index("}")]

    page = source("templates", "settings.html")
    assert 'id="settings_remote_catalogue"' in page
    # Nothing of the form it replaced is left behind: not the boxes, not the
    # Save button, not the "Use preset..." shortcut that pointed at what is
    # now the only way in.
    for gone in ("settings_remote_name", "settings_remote_target",
                 "settings_remote_advanced", "settings_remote_save",
                 "settings_remote_preset"):
        assert gone not in page, gone
    settings = source("src", "js", "views", "settingsPage.js")
    assert "settings_remote_save" not in settings


# -- the job a server asks the scheduler for ----------------------------------
#
# The same three numbers the connection dialog's presets fill in, on the form
# that edits a saved server afterwards. Both surfaces read them from
# recipes.defaults(), which is what stops them drifting apart.


def test_the_job_line_is_shown_rather_than_left_to_a_site_default(probe_modal):
    """A default nobody can see is a default nobody can correct. An empty Cores
    box does not mean "no cores" -- it means whatever the cluster does, which
    on most of them is one core and a couple of gigabytes: enough to start
    Plexora and not enough to open a multiplexed pyramid in it.

    Asked on the preset's form now rather than on a form of this page's own --
    which is the point, since the preset is what knows what the site expects."""
    assert "the walltime a job asks for is on screen, filled in" in probe_modal
    assert "...and so is the number of cores" in probe_modal
    assert "...and the memory, which is what a pyramid actually runs out of" in probe_modal
    assert "...taken from the server rather than written into the browser" in probe_modal

    # Filled in, not offered as placeholder text over an empty box: the
    # fourth argument to `field` is what puts a value in `.value`. On an edit
    # the same box holds the profile's own number instead -- and an empty one
    # stays empty, because a profile with no `-t` said "whatever the site
    # does", which is an answer and not a gap to fill.
    modal = source("src", "js", "services", "connectionModal.js")
    assert 'saved ? (job.walltime || "") : recipeDefaults.walltime' in modal


def test_only_one_place_quotes_the_three_numbers(client):
    """There used to be two: a template rendering `job_defaults` and a dialog
    fetching `defaults`, with a test whose whole job was keeping them in step.
    The form that needed the first one is gone, so the question is now whether
    anything ELSE has started writing them down."""
    from plexora.server.models import recipes as recipe_store

    numbers = (recipe_store.DEFAULT_CORES, recipe_store.DEFAULT_MEMORY,
               recipe_store.DEFAULT_WALLTIME)

    served = client.get("/settings/recipes").get_json()["defaults"]
    assert served == recipe_store.defaults()

    # The page renders none of them any more -- it offers presets, and a
    # preset's numbers arrive with the preset.
    page = client.get("/settings").get_data(as_text=True)
    for number in numbers:
        assert number not in page, number

    # The page writes none of them: it has no box to put one in any more.
    settings = source("src", "js", "views", "settingsPage.js")
    for number in numbers:
        assert number not in settings, number

    # The dialog carries the three literals once, as the fallback for a server
    # too old to send `defaults` -- which is a different thing from a second
    # opinion, and is why what it does with them is read here too.
    modal = source("src", "js", "services", "connectionModal.js")
    assert "if (payload.defaults) recipeDefaults = payload.defaults;" in modal
    for number in numbers:
        assert modal.count('"%s"' % number) == 1, number


# -- what the form asks, and in what order ------------------------------------


def test_the_questions_a_machine_has_come_before_the_ones_a_cluster_has():
    """Name it, say who you are on it, say where the data is -- and that is the
    whole form for a workstation. Everything only a cluster needs is real and
    is one disclosure below it, so that adding a lab server is not nine
    decisions.

    On the preset's form now. The order is the order the code appends in, so
    it is read out of the source the way the template's was."""
    modal = source("src", "js", "services", "connectionModal.js")
    body = modal.index('"data_dir", "Remote data directory (optional)"')
    advanced = modal.index('const advanced = el("details", "connect-advanced");')
    assert modal.index('["name", "Name this connection"') < body
    assert body < advanced
    for late in ('"srun", "Other job options"',
                 '"remote_command", "Plexora command or environment"',
                 '"install", "Install or update Plexora"',
                 '"bind_node", "Forward from the login node"',
                 '"forwards", "Additional port forwarding"'):
        assert modal.index(late) > advanced, late


def test_the_form_no_longer_ties_a_machine_to_one_project_on_it():
    """"Open project" asked, while saving a MACHINE, which of the things on it
    to open -- a question with a different lifetime from the answer it was
    stored beside. Every data field now has a Local/Remote switch that asks it
    when it actually comes up."""
    page = source("templates", "settings.html")
    assert "settings_remote_datasource" not in page
    assert "Open project" not in page
    settings = source("src", "js", "views", "settingsPage.js")
    assert "settings_remote_datasource" not in settings


def test_a_project_the_form_stopped_asking_about_is_not_thereby_deleted(tmp_path):
    """The form dropped the box; a profile written by `plexora connect --save`
    may still carry the field. Editing an address in Settings is not somebody
    asking for it to be erased."""
    from plexora.server.routes.settings_routes import _remote_payload
    from plexora.server.models.remotes import Remote

    existing = Remote(name="hpc", target="me@old", datasource="/n/data/study")
    saved = _remote_payload({"target": "me@new"}, "hpc", existing)
    assert saved.datasource == "/n/data/study"
    assert saved.target == "me@new"


def test_editing_through_a_preset_does_not_drop_what_the_preset_never_asked(
        client, tmp_path, monkeypatch):
    """The regression this page's own form used to hide.

    A recipe composes only the keys its form asked about. Three fields were
    read straight off the payload rather than through `kept()` -- the data
    directory, the forwarded ports and the bind-to-node switch -- so the first
    save through a preset erased all three. It did not show while Settings had
    a form of its own, because that form sent all twelve keys every time; it
    became the ONLY way to edit a server the moment the form was retired."""
    from plexora import paths
    from plexora.server.models import recipes as recipe_store
    from plexora.server.models import remotes as remote_store

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    client.post("/settings/recipes/hms-o2", json={
        "user": "aj", "name": "o2", "data_dir": "/n/data",
        "forwards": ["8642"], "bind_node": True,
    })
    # Two more that no form has ever had a box for, as a hand-written profile
    # or `plexora connect --save` would leave them.
    import dataclasses

    stored = remote_store.get("o2", tmp_path)
    remote_store.save(dataclasses.replace(stored,
                                          datasource="/n/data/study",
                                          jump="bastion.hms.edu"), tmp_path)

    # The same preset again, with only the boxes its form actually shows.
    answer = client.post("/settings/recipes/hms-o2",
                         json={"user": "ajn16", "name": "o2"})
    assert answer.status_code == 200, answer.get_json()

    after = remote_store.get("o2", tmp_path)
    assert after.target == "ajn16@o2.hms.harvard.edu"
    assert after.data_dir == "/n/data"
    assert after.forwards == ("8642",)
    assert after.datasource == "/n/data/study"
    assert after.jump == "bastion.hms.edu"
    # And it still knows which preset to reopen.
    assert after.recipe == "hms-o2"

    # `bind_node` is deliberately NOT in that list. It is the one of the three
    # the site has an opinion about, so a compose body always carries an answer
    # for it -- the preset's, when the caller sent none. That is safe because
    # the form always sends one: the switch is drawn, prefilled from the
    # profile, on every preset that has a job to bind to.
    assert after.bind_node == recipe_store.find("hms-o2").bind_node
    client.post("/settings/recipes/hms-o2",
                json={"user": "ajn16", "name": "o2", "bind_node": True})
    assert remote_store.get("o2", tmp_path).bind_node is True


def test_a_saved_profile_says_which_form_edits_it(client, tmp_path,
                                                  monkeypatch):
    """Settings has one form and it is the recipe's, so the payload the card is
    drawn from has to carry both the preset and the address split into that
    preset's boxes. Split on the server, beside `srun_parts`, for the same
    reason: the page that shows a username and the route that stores one must
    not disagree."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store
    from plexora.server.routes import settings_routes

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    client.post("/settings/recipes/hms-o2", json={"user": "aj", "name": "o2"})
    view = settings_routes._remote_view(remote_store.get("o2", tmp_path))
    assert view["recipe"] == "hms-o2"
    assert view["target_parts"] == {"user": "aj",
                                    "host": "o2.hms.harvard.edu"}

    # And a profile written before any of this still gets a form -- inferred
    # from its shape rather than left with an Edit button that does nothing.
    client.post("/settings/remotes", json={"name": "plain",
                                           "target": "me@lab.edu"})
    plain = settings_routes._remote_view(remote_store.get("plain", tmp_path))
    assert plain["recipe"] == "ssh"
    assert plain["target_parts"] == {"user": "me", "host": "lab.edu"}


def test_a_saved_card_is_told_what_kind_of_machine_it_is_showing(
        client, tmp_path, monkeypatch):
    """The card used to lead with `aj@o2.hms.harvard.edu`, which is the answer
    to a question nobody asks of a list of their own machines. It leads with
    the recipe's own two or three words instead -- and those come from here,
    beside the recipe id, rather than from a second fetch of the catalogue in
    the browser purely so a card can say "cluster"."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store
    from plexora.server.routes import settings_routes

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    client.post("/settings/recipes/hms-o2", json={"user": "aj", "name": "o2"})
    view = settings_routes._remote_view(remote_store.get("o2", tmp_path))
    assert view["description"] == "Harvard O2 compute cluster"

    # Including for a profile that never recorded which preset made it: the
    # recipe is inferred, so the sentence is too, and no card is left blank.
    client.post("/settings/remotes", json={"name": "plain",
                                           "target": "me@lab.edu"})
    plain = settings_routes._remote_view(remote_store.get("plain", tmp_path))
    assert plain["description"] == "SSH server"


def test_a_workstations_operating_system_survives_an_edit_in_settings(tmp_path):
    """It rides in `extra`, which is where every key no form has a box for
    lives -- and editing an address is not somebody asking to forget which
    machine is on the other end."""
    from plexora.server.routes.settings_routes import _remote_payload
    from plexora.server.models.remotes import Remote

    existing = Remote(name="box", target="aj@old",
                      extra={"workstation": {"os": "windows"}})
    saved = _remote_payload({"target": "aj@new"}, "box", existing)
    assert saved.workstation == {"os": "windows"}
    assert saved.target == "aj@new"


def test_a_composed_workstation_lands_in_the_profiles_extra(tmp_path):
    """The seam a recipe's answer travels: `compose` puts it at the top level
    of the body, and this is what folds it into the record -- the same
    arrangement the Google Cloud preset uses."""
    from plexora.server.routes.settings_routes import _remote_payload

    saved = _remote_payload(
        {"target": "aj@box", "workstation": {"os": "macos"}}, "box", None)
    assert saved.extra["workstation"] == {"os": "macos"}
    assert saved.workstation == {"os": "macos"}


def test_an_operating_system_nobody_recognises_never_reaches_a_command_line():
    """Stored as written -- this layer keeps what it is given -- but read as no
    record at all, so what is handed to `connect` is the POSIX default rather
    than an unvalidated word."""
    from plexora.server.routes.settings_routes import _remote_payload

    saved = _remote_payload(
        {"target": "aj@box", "workstation": {"os": "plan9"}}, "box", None)
    assert saved.workstation is None
    assert "remote_os" not in saved.as_node_kwargs()


def test_the_card_is_told_which_kind_of_machine_it_is(tmp_path):
    """None for every profile that is not a workstation, which is the shape
    rule the card branches on -- the same one `gcloud` follows."""
    from plexora.server.routes.settings_routes import _remote_view
    from plexora.server.models.remotes import Remote

    plain = _remote_view(Remote(name="hpc", target="me@login"))
    assert plain["workstation"] is None
    box = _remote_view(Remote(name="box", target="aj@box",
                              extra={"workstation": {"os": "linux"}}))
    assert box["workstation"] == {"os": "linux"}


def test_an_address_with_no_username_is_refused_rather_than_sent_to_ssh():
    """`@login.cluster.edu` reaches ssh with no username, and ssh answers that
    with "Permission denied" -- the one error message that sends people looking
    for a key problem they do not have."""
    from plexora.server.routes.settings_routes import _address_error

    assert _address_error("@o2.hms.harvard.edu")
    assert "username" in _address_error("@o2.hms.harvard.edu")
    assert _address_error("")
    assert _address_error("ajn@o2.hms.harvard.edu") is None


# -- one string in the store, four boxes on the form --------------------------


def test_a_job_is_edited_as_boxes_and_stored_as_one_line(client, tmp_path,
                                                         monkeypatch):
    """Cores, Memory and Time are three fields over a store that holds one
    `srun` string. The split and the splice are both in recipes.py, so the page
    that SHOWS a walltime and the route that STORES one cannot disagree about
    which flag carries it."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store
    from plexora.server.routes import settings_routes

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    answer = client.post("/settings/remotes", json={
        "name": "hpc", "target": "me@login.cluster.edu", "use_srun": True,
        "cores": "32", "memory": "256G", "walltime": "8:00:00",
        "srun": "-p gpu --gres=gpu:1",
    })
    assert answer.status_code == 200, answer.get_json()

    stored = remote_store.get("hpc", tmp_path)
    assert stored.srun == "-p gpu --gres=gpu:1 -t 8:00:00 -c 32 --mem 256G"

    # ...and back apart into the same four boxes it was typed into.
    view = settings_routes._remote_view(stored)
    assert view["srun_parts"] == {"walltime": "8:00:00", "cores": "32",
                                  "memory": "256G",
                                  "extra": "-p gpu --gres=gpu:1"}


def test_a_composed_preset_line_is_not_spliced_a_second_time(client, tmp_path,
                                                             monkeypatch):
    """A recipe arrives with the whole line already assembled and sends none of
    the three boxes. Reading the payload for keys it never had is how a field
    gets silently dropped, so membership -- not truthiness -- decides which of
    the two callers is talking."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    answer = client.post("/settings/recipes/hms-o2",
                         json={"user": "ajn16", "name": "o2"})
    assert answer.status_code == 200, answer.get_json()
    assert remote_store.get("o2", tmp_path).srun == (
        "-p interactive -t 4:00:00 -c 16 --mem 128G")


def test_extra_ports_are_a_list_rather_than_lines_in_a_textarea(client,
                                                                tmp_path,
                                                                monkeypatch):
    """A textarea asks somebody to know the format before they can type, and
    accepts anything -- including the thing they meant to delete last time."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    client.post("/settings/remotes", json={
        "name": "hpc", "target": "me@host", "forwards": ["8642", "9000"],
    })
    assert remote_store.get("hpc", tmp_path).forwards == ("8642", "9000")

    # And through the preset that composes one, which is the only form that
    # asks for them now.
    client.post("/settings/recipes/ssh", json={
        "user": "me", "host": "lab.edu", "name": "lab",
        "forwards": ["8642", "9000"],
    })
    assert remote_store.get("lab", tmp_path).forwards == ("8642", "9000")

    # One box and an Add button into a list of chips. The control moved from
    # the Settings page to the dialog when the page's form was retired; what
    # must not come back is the textarea it replaced.
    modal = source("src", "js", "services", "connectionModal.js")
    assert "function portsField(" in modal
    assert "<textarea" not in modal
    assert "textarea" not in source("templates", "settings.html")


def test_a_card_says_how_long_its_job_has_left(probe):
    """A meta line rather than a warning, because that is what it is until the
    last ten minutes: the job is doing exactly what it was asked to do. The
    card is repainted on every poll and this page polls once a second while it
    is open, so the number moves without a timer of its own."""
    assert "a connection inside a job says how long it has left" in probe, probe
    assert "...marked once it is nearly up" in probe, probe
    assert "...and a connection with no walltime is told nothing about time" in probe, probe


# -- installing Plexora on the far side ---------------------------------------


def test_the_install_switch_sits_beside_the_environment_it_writes_to(
        probe, probe_modal):
    """Next to the field that names the environment, because that is the
    environment it writes to -- and as one of the form's own grid cells, so it
    sits beside that field wherever there is room rather than taking a row of
    its own.

    On the preset's form now. It is off there on arrival for every preset: no
    starting point gets to decide that software should be installed into
    somebody's account on a machine Plexora has only read the documentation
    for."""
    modal = source("src", "js", "services", "connectionModal.js")
    command = modal.index('"remote_command", "Plexora command or environment"')
    switch = modal.index('"install", "Install or update Plexora"')
    assert command < switch
    assert "connect-switch" in modal

    assert "...with the install switch beside the environment it would write to" in probe_modal
    assert ("...off on arrival, because no preset gets to decide that software"
            in probe_modal)
    assert "...including the install switch, as a boolean rather than a string" in probe_modal
    # And once it is saved it stays on the form: the card is for picking a
    # machine, not for auditing one.
    assert "the settings a profile carries stay off its card" in probe, probe


def test_the_install_switch_is_saved_and_comes_back(client, tmp_path,
                                                    monkeypatch):
    """A switch on the form, a boolean on the record, and the environment it
    would write to derived once -- on the server, by the same function that
    builds the pip line, so a page cannot promise an environment the install
    does not touch."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store
    from plexora.server.routes import settings_routes

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    answer = client.post("/settings/remotes", json={
        "name": "hpc", "target": "me@host", "install": True,
        "remote_command": "conda run -n imaging plexora",
    })
    assert answer.status_code == 200, answer.get_json()

    stored = remote_store.get("hpc", tmp_path)
    assert stored.install is True
    view = settings_routes._remote_view(stored)
    assert view["install"] is True
    assert view["install_env"] == "imaging"


def test_nothing_installs_unless_somebody_asked(client, tmp_path, monkeypatch):
    """The default has to be off in every direction: an absent key, an
    explicit false, and a record written before the field existed. This is the
    one setting on the form that makes connecting WRITE to another machine."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    client.post("/settings/remotes", json={"name": "a", "target": "me@host"})
    assert remote_store.get("a", tmp_path).install is False

    client.post("/settings/remotes",
                json={"name": "b", "target": "me@host", "install": False})
    assert remote_store.get("b", tmp_path).install is False

    # Off is not written at all, so the file stays a record of what somebody
    # chose rather than of every default.
    assert "install" not in remote_store.load_all(tmp_path)["a"].to_dict()

    # And a preset composes a profile that installs nothing: no starting point
    # gets to decide that software should be put into somebody's account on a
    # machine we have only read the documentation for.
    client.post("/settings/recipes/hms-o2", json={"user": "ajn16", "name": "o2"})
    assert remote_store.get("o2", tmp_path).install is False


def test_a_preset_can_turn_the_install_on_before_the_first_connection(
        client, tmp_path, monkeypatch):
    """The switch is on the preset form too -- and it has to survive the trim
    -to-string pass every text answer goes through, where False becomes "" and
    True becomes "True"."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    client.post("/settings/recipes/ssh",
                json={"user": "me", "host": "lab.example.edu",
                      "name": "lab", "install": True})
    assert remote_store.get("lab", tmp_path).install is True


def test_editing_an_address_does_not_turn_the_install_off(client, tmp_path,
                                                          monkeypatch):
    """`plexora connect --save` and a hand-written body send no `install` key.
    Reading it off the payload with truthiness would erase somebody's answer on
    the first save from a caller that never asked the question."""
    from plexora import paths
    from plexora.server.models import remotes as remote_store

    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    client.post("/settings/remotes",
                json={"name": "hpc", "target": "me@host", "install": True})
    client.post("/settings/remotes",
                json={"name": "hpc", "target": "me@other-host"})

    stored = remote_store.get("hpc", tmp_path)
    assert stored.target == "me@other-host"
    assert stored.install is True


# -- the machine a Google Cloud profile rents --------------------------------


def test_the_card_says_what_the_rented_machine_is_doing(probe):
    """Stopping on disconnect is the default, so stopped is where one of these
    profiles rests. A card that could only ever stop things was describing half
    a lifecycle, and describing it blind."""
    assert ("a rented machine's state is on the button that would change it"
            in probe), probe
    assert "a stopped machine says so rather than saying nothing" in probe, probe
    # On the buttons, not on a line of its own. WHICH of Start and Stop is
    # offered already says which way the machine is; the word is there for the
    # tooltip and the screen reader, and it is the same word from the same map
    # as when it was a row. "VM no VM yet" was the shape of the old one, and
    # it reads as debug output rather than as a status.
    assert "...and the card itself says only which machine this is" in probe, probe
    assert "...with the bucket and the machine type nowhere on it" in probe, probe


def test_asking_compute_engine_is_never_part_of_the_poll(probe):
    """The list is re-read every second while anything is happening. A round
    trip inside that loop would be a gcloud subprocess per cloud profile per
    second for as long as the page was open -- not a status display, a bill."""
    assert "...having asked Google exactly once to find out" in probe, probe
    assert "...and never asks again just because the page repainted" in probe, probe
    assert "a connected session is itself proof the VM is running" in probe, probe


def test_the_button_offered_is_the_one_that_would_do_something(probe):
    """Offering Stop on a stopped machine is offering a no-op, and it was the
    only button there was."""
    assert "...and a running one is offered the button that ends the bill" in probe, probe
    assert "...and is offered Start instead of a Stop that would do nothing" in probe, probe
    assert "a profile whose VM is gone is offered neither Start nor Stop" in probe, probe
    assert "...nor Delete, there being nothing left to delete" in probe, probe


def test_starting_a_machine_does_not_hold_the_page_while_it_boots(probe):
    """It takes the better part of a minute, and a request that waited would be
    a page that appears to have frozen doing what it was asked."""
    assert "starting it says so, and does not wait a minute to say it" in probe, probe
    assert "...and the card shows where it is going, not where it was" in probe, probe


def test_somebody_elses_machine_keeps_its_stop_and_loses_its_delete(probe):
    """Stopping is theirs to ask for and reversible. Deleting is neither."""
    assert "a machine the user already runs can still be stopped by hand" in probe, probe
    assert "...but is never offered a button that would delete it" in probe, probe
