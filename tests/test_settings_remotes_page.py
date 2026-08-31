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


def test_the_presets_are_reachable_from_the_page_that_adds_servers(probe):
    """They shipped reachable only by flipping a data field to Remote with
    nothing saved -- the one place somebody adding their first server was not
    looking. The button that says "Add a server" is on Settings."""
    assert ("the presets are reachable from the page that adds servers"
            in probe), probe
    page = source("templates", "settings.html")
    assert 'id="settings_remote_preset"' in page
    assert page.index('id="settings_remote_preset"') < page.index(
        'id="settings_remote_name"')


# -- the job a server asks the scheduler for ----------------------------------
#
# The same three numbers the connection dialog's presets fill in, on the form
# that edits a saved server afterwards. Both surfaces read them from
# recipes.defaults(), which is what stops them drifting apart.


def test_the_job_line_is_shown_rather_than_left_to_a_site_default():
    """A default nobody can see is a default nobody can correct. An empty Cores
    box does not mean "no cores" -- it means whatever the cluster does, which
    on most of them is one core and a couple of gigabytes: enough to start
    Plexora and not enough to open a multiplexed pyramid in it."""
    page = source("templates", "settings.html")
    for key in ("cores", "memory", "walltime", "srun_extra"):
        assert 'data-default="{{ job_defaults.%s }}"' % key in page
        assert 'placeholder="{{ job_defaults.%s }}"' % key in page

    settings = source("src", "js", "views", "settingsPage.js")
    assert 'getAttribute("data-default")' in settings
    # Only into a box nobody has typed in: turning the switch off and on again
    # must not throw away a walltime somebody has already written.
    assert "box.value.trim()) return;" in settings


def test_the_form_and_the_presets_quote_the_same_three_numbers(client):
    """One source, reached two ways: the page renders `job_defaults`, the
    dialog fetches `defaults`. Neither writes the numbers down itself."""
    from plexora.server.models import recipes as recipe_store

    page = client.get("/settings").get_data(as_text=True)
    for number in (recipe_store.DEFAULT_CORES, recipe_store.DEFAULT_MEMORY,
                   recipe_store.DEFAULT_WALLTIME):
        assert number in page

    served = client.get("/settings/recipes").get_json()["defaults"]
    assert served == recipe_store.defaults()

    # Neither file writes a number down. The template reads `job_defaults`, and
    # the script reads the attribute the template rendered.
    settings = source("src", "js", "views", "settingsPage.js")
    for number in (recipe_store.DEFAULT_CORES, recipe_store.DEFAULT_MEMORY,
                   recipe_store.DEFAULT_WALLTIME):
        assert number not in settings


# -- what the form asks, and in what order ------------------------------------


def test_three_questions_come_before_anything_about_a_scheduler():
    """Name it, say where it is, say where the data is -- and that is the whole
    form for a workstation. Everything only a cluster needs is real and is one
    disclosure below it, so that adding a lab server is not nine decisions."""
    page = source("templates", "settings.html")
    advanced = page.index('id="settings_remote_advanced"')
    for early in ("settings_remote_name", "settings_remote_target",
                  "settings_remote_data_dir"):
        assert page.index('id="%s"' % early) < advanced, early
    for late in ("settings_remote_command", "settings_remote_use_srun",
                 "settings_remote_cores", "settings_remote_srun",
                 "settings_remote_bind_node", "settings_remote_forwards"):
        assert page.index('id="%s"' % late) > advanced, late


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

    page = source("templates", "settings.html")
    assert "<textarea" not in page[page.index('id="settings_remote_advanced"'):
                                   page.index('id="settings_remote_save"')]


def test_a_card_says_how_long_its_job_has_left(probe):
    """A meta line rather than a warning, because that is what it is until the
    last ten minutes: the job is doing exactly what it was asked to do. The
    card is repainted on every poll and this page polls once a second while it
    is open, so the number moves without a timer of its own."""
    assert "a connection inside a job says how long it has left" in probe, probe
    assert "...marked once it is nearly up" in probe, probe
    assert "...and a connection with no walltime is told nothing about time" in probe, probe


# -- installing Plexora on the far side ---------------------------------------


def test_the_install_switch_rides_a_row_that_already_existed(probe):
    """Compact, next to the field that names the environment it would write
    to, and on the group's own title row rather than a row of its own -- which
    would put an uppercase section label and a lone toggle on two lines both
    saying "environment"."""
    page = source("templates", "settings.html")
    head = page.index('class="remote-group-head"')
    form = page.index('id="settings_remote_command"')
    assert head < page.index('id="settings_remote_install"') < form
    assert "remote-switch-inline" in page

    assert "...and a server that installs on connect says so, in the open" in probe, probe
    assert "...and said on the card too, before anybody presses Connect" in probe, probe
    assert "...and the install switch as an explicit no until it is turned on" in probe, probe
    assert "...and as a yes once it is" in probe, probe
    assert "...and Add-a-server starts from off again" in probe, probe


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
    assert "a rented machine's card says what the machine is doing" in probe, probe
    assert "a stopped machine says so rather than saying nothing" in probe, probe
    # And the two facts that decide what it costs: how it was bought, and what
    # happens to it when the session ends. Once the form has been submitted,
    # this card is the only place either of them is visible.
    assert "...and what it is, how it was bought, and how it ends" in probe, probe


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
