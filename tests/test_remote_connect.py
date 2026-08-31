"""Saved remote servers, and connecting to one from the Settings page.

Three properties carry this feature and each is pinned from more than one
side:

**No password is ever written down.** There is no field for one in the stored
profile, and the answer a user types travels through the manager to ssh
without appearing in the status payload, the log tail, or the file. A
regression here would be silent and permanent -- a credential in a JSON file
in a home directory on a shared cluster -- so it is asserted, not assumed.

**A route never blocks.** Connecting starts a thread and answers 202. The
thing being waited for is sometimes a scheduler queue, and a request that
waited with it would hold a Waitress worker for a quarter of an hour.

**A failure says which failure it was.** The three that actually happen -- a
remote `plexora` that is not on a non-interactive PATH, a rejected credential,
a changed host key -- have three different fixes and none of them is "retry".

No real ssh runs here. connect.py's module-level seams are rebound, the same
way tests/test_connect.py does it, so what is exercised is the choreography
rather than anybody's cluster.
"""

import json
import os
import stat
import sys
import threading
import time
import types

import pytest

import plexora
from plexora import connect
from plexora.server.models import remote_sessions
from plexora.server.models import remotes as remote_store


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture(autouse=True)
def _no_sessions_left_running():
    """The registry is module state, and a leaked session outlives its test."""
    yield
    for name in list(remote_sessions.all_sessions()):
        remote_sessions.forget(name)


def a_remote(name="hpc", **kwargs):
    # No node on this machine unless a test asks for one. Every connection
    # starts one in production, and it announces itself in under a second --
    # but the fake ssh here announces nothing ever, so leaving it on would make
    # each of these tests wait out `EMPTY_NODE_TIMEOUT` for a process that was
    # never going to speak. What that default IS is pinned separately, in
    # test_a_saved_server_starts_a_node_on_this_machine_by_default.
    fields = {"target": "me@login.cluster.edu", "local_node": False}
    fields.update(kwargs)
    return remote_store.Remote(name=name, **fields)


# -- the store -------------------------------------------------------------


def test_a_saved_server_round_trips(plexora_data_root):
    saved = remote_store.save(a_remote(
        remote_command="conda run -n imaging plexora",
        datasource="tonsil", srun="-p interactive -t 4:00:00",
        forwards=("8642",), bind_node=True,
    ))
    back = remote_store.get("hpc")

    assert back == saved
    assert back.target == "me@login.cluster.edu"
    assert back.forwards == ("8642",)
    assert back.bind_node is True


def test_the_scheduler_field_keeps_all_three_of_its_answers(plexora_data_root):
    """None, "" and a string are three different instructions: no scheduler,
    srun with the site's defaults, and srun with these arguments. Collapsing
    the first two is how "run it inside a job" quietly stopped happening."""
    remote_store.save(a_remote("none", srun=None))
    remote_store.save(a_remote("bare", srun=""))
    remote_store.save(a_remote("args", srun="-p gpu"))

    assert remote_store.get("none").srun is None
    assert remote_store.get("bare").srun == ""
    assert remote_store.get("args").srun == "-p gpu"


def test_saving_the_same_name_updates_rather_than_duplicates(plexora_data_root):
    remote_store.save(a_remote(target="me@old.edu"))
    remote_store.save(a_remote(target="me@new.edu"))

    assert list(remote_store.load_all()) == ["hpc"]
    assert remote_store.get("hpc").target == "me@new.edu"


def test_there_is_nowhere_to_put_a_password(plexora_data_root):
    """Not a matter of remembering not to write one: the dataclass has no field
    for it, so the mistake is unrepresentable rather than merely avoided."""
    assert not any("pass" in field.lower()
                   for field in remote_store.Remote.__dataclass_fields__)

    remote_store.save(a_remote())
    written = remote_store.remotes_path().read_text(encoding="utf-8")
    assert "password" not in written.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_file_is_written_owner_only(plexora_data_root):
    remote_store.save(a_remote())
    mode = stat.S_IMODE(remote_store.remotes_path().stat().st_mode)
    assert mode == 0o600


def test_a_malformed_entry_is_skipped_rather_than_fatal(plexora_data_root):
    """A hand-edited file must not take the whole section down with it."""
    remote_store.save(a_remote())
    path = remote_store.remotes_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["broken"] = {"remote_command": "plexora"}  # no target
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert sorted(remote_store.load_all()) == ["hpc"]


def test_an_unknown_name_names_the_ones_that_exist(plexora_data_root):
    remote_store.save(a_remote())
    with pytest.raises(KeyError) as excinfo:
        remote_store.get("hcp")
    assert "hpc" in str(excinfo.value)


def test_a_saved_server_starts_a_node_on_this_machine_by_default(plexora_data_root):
    """Including every entry written before the option existed.

    The node on the user's own computer is what makes "Local" mean anything in
    the viewer's data forms, and nothing in a saved record could name the file
    somebody is going to pick from a browser half an hour from now. So it is
    stored as an opt-out, and an absent key is a yes.
    """
    remote_store.save(a_remote())
    path = remote_store.remotes_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["older"] = {"target": "me@login.cluster.edu"}
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert remote_store.get("older").local_node is True
    assert remote_store.get("older").as_session_kwargs()["local_node"] is True
    # And the opt-out survives the round trip, or it would be re-enabled every
    # time somebody edited an unrelated field.
    assert remote_store.get("hpc").local_node is False


def test_a_saved_server_names_the_nodes_it_registers_after_itself(
        plexora_data_root):
    """Two saved connections to the same cluster are two identities.

    The node names, `managed_by`, and the manifest the local node keeps are all
    derived from this -- so falling back to the host would give both profiles
    one shared identity, and would rename everything the day somebody edited
    the target.
    """
    remote_store.save(a_remote("study-a"))
    remote_store.save(a_remote("study-b"))

    assert remote_store.get("study-a").as_session_kwargs()["node_name"] == "study-a"
    assert remote_store.get("study-b").as_session_kwargs()["node_name"] == "study-b"


# -- the session manager ---------------------------------------------------


class FakeProcess:
    """Enough of Popen for connect._Watched."""

    def __init__(self, lines=(), dead_with=None, block=False):
        self._lines = list(lines)
        self.returncode = dead_with
        self._block = block
        self.stdout = self._stream()
        self.terminated = False

    def _stream(self):
        for line in self._lines:
            yield line + "\n"
        # A live ssh does not close its stdout; without this the reader thread
        # finishes and _Watched sets saw_announce, which means "gave up".
        while self._block and self.returncode is None:
            time.sleep(0.01)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        while self._block and self.returncode is None:
            time.sleep(0.01)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()


@pytest.fixture
def ssh(monkeypatch):
    """connect.py with every outside edge replaced, plus what it was asked."""
    rig = types.SimpleNamespace(spawned=[], envs=[], queue=[], healthy=True)

    def popen(argv, **kwargs):
        rig.spawned.append(argv)
        rig.envs.append(kwargs.get("env"))
        rig.detach = kwargs.get("start_new_session")
        return rig.queue.pop(0) if rig.queue else FakeProcess(block=True)

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url, timeout=None):
        if not rig.healthy:
            raise OSError("refused")
        return Response()

    monkeypatch.setattr(connect, "_popen", popen)
    monkeypatch.setattr(connect, "_urlopen", urlopen)
    monkeypatch.setattr(connect, "_which", lambda name: "/usr/bin/ssh")
    # Short but real: the health poll is a loop, and a no-op sleep turns the
    # tests that deliberately never become healthy into three spinning threads.
    monkeypatch.setattr(connect, "_sleep", lambda seconds: time.sleep(0.01))
    return rig


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_connecting_reaches_connected_without_blocking_the_caller(ssh):
    session = remote_sessions.start(a_remote(), askpass_url=None)

    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)
    assert session.url.startswith("http://127.0.0.1:")
    assert ssh.spawned and ssh.spawned[0][0] == "ssh"


def test_a_connection_opens_the_tunnel_for_the_project_it_was_saved_with(ssh):
    session = remote_sessions.start(a_remote(datasource="tonsil"),
                                    askpass_url=None)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)
    assert session.url.endswith("/tonsil")


# -- the other thing a saved connection is for -----------------------------


def test_a_saved_connection_can_open_a_data_node_instead_of_a_viewer(ssh):
    """Same profile, same login, opposite arrangement: Plexora stays here with
    the project and the browser, and only the far side's files are reached.

    This is what a data field's Remote option opens, and it is why the two
    kinds cannot share a slot -- both can be live for one profile at once and
    they mean different things."""
    recorded = {}

    def register(name, endpoint, token, **extra):
        recorded.update({"name": name, "endpoint": endpoint}, **extra)
        return name

    ssh.queue.append(FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"],
        block=True))

    session = remote_sessions.start(
        a_remote(), askpass_url=None, kind=remote_sessions.KIND_NODE,
        allow_origin="http://127.0.0.1:8000", register=register)

    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)
    assert recorded["name"] == "hpc"
    assert recorded["managed_by"] == "connect:hpc"
    # One ssh with a forward, and no viewer asked for anywhere on it.
    launched = " ".join(ssh.spawned[0])
    assert "node serve" in launched and "--dynamic" in launched
    assert "--remote" not in launched

    # And the field is told which node to address, which is the one thing it
    # needs out of all of this.
    status = session.status()
    assert status["kind"] == "node" and status["node"] == "hpc"


def test_the_two_kinds_of_connection_do_not_share_a_slot(ssh):
    """A viewer session and a node session for one profile are different
    arrangements of the same login. Keying them together would let opening one
    report the other as already connected."""
    viewer = remote_sessions.start(a_remote(), askpass_url=None)
    assert wait_for(lambda: viewer.state == remote_sessions.STATE_CONNECTED)

    assert remote_sessions.get("hpc") is viewer
    assert remote_sessions.get("hpc", remote_sessions.KIND_NODE) is None


def test_a_missing_remote_command_is_named_as_such(ssh):
    ssh.healthy = False
    ssh.queue.append(FakeProcess(
        ["bash: plexora: command not found"], dead_with=127))

    session = remote_sessions.start(
        a_remote(remote_command="plexora"), askpass_url=None)

    assert wait_for(lambda: session.state == remote_sessions.STATE_FAILED)
    assert "PATH" in session.error


def test_a_rejected_login_is_named_as_such(ssh):
    ssh.healthy = False
    ssh.queue.append(FakeProcess(
        ["me@login.cluster.edu: Permission denied (publickey,password)."],
        dead_with=255))

    session = remote_sessions.start(a_remote(), askpass_url=None)

    assert wait_for(lambda: session.state == remote_sessions.STATE_FAILED)
    assert "rejected the login" in session.error


def test_a_changed_host_key_is_named_as_such(ssh):
    ssh.healthy = False
    ssh.queue.append(FakeProcess(
        ["@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@",
         "Host key verification failed."], dead_with=255))

    session = remote_sessions.start(a_remote(), askpass_url=None)

    assert wait_for(lambda: session.state == remote_sessions.STATE_FAILED)
    assert "administrator" in session.error


def test_a_queued_job_reports_the_wait_rather_than_looking_stuck(ssh):
    """The longest wait in the whole feature is the one that is not a problem,
    and an unlabelled spinner is what made it read as a hang."""
    session = remote_sessions.start(a_remote(srun="-p interactive"),
                                    askpass_url=None)

    assert wait_for(
        lambda: session.state == remote_sessions.STATE_WAITING_FOR_JOB)
    assert "scheduler" in session.status()["phase"]
    session.stop()


def test_installing_is_a_state_the_page_can_read_and_a_step_it_can_draw(ssh):
    """A pip pulling numpy and zarr onto a cold shared home directory looks
    exactly like a hung login unless the page says which of the two it is."""
    # `block=True`: this one process is now install AND launch, so after the
    # marker it has to stay up the way a live ssh would.
    ssh.queue.append(FakeProcess(
        ["Collecting plexora", "Successfully installed plexora-1.4.2",
         "PLEXORA_INSTALL_DONE"], block=True))
    session = remote_sessions.start(
        a_remote(install=True, remote_command="conda run -n imaging plexora"),
        askpass_url=None)

    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)
    # One ssh, one login: the pip line rides the launch's own command, chained
    # ahead of it, in the environment the launch names.
    remote = ssh.spawned[0][-1]
    assert remote.startswith("conda run --no-capture-output -n imaging "
                             "pip install --progress-bar off --upgrade plexora && echo ")
    assert "--remote" in remote
    # ...and everything it said is in the log the terminal panel shows.
    log = "\n".join(session.status(200)["log"])
    assert "Collecting plexora" in log
    assert "Successfully installed plexora-1.4.2" in log
    assert "Plexora 1.4.2 is installed" in log
    session.stop()


def test_the_install_is_an_opening_state_so_a_second_press_is_refused(ssh):
    """It has to be in OPENING_STATES or every surface reading that tuple --
    the cap, the poller, the chooser -- treats a connection that is minutes
    into a pip as settled."""
    assert (remote_sessions.STATE_INSTALLING
            in remote_sessions.OPENING_STATES)
    assert remote_sessions.PHRASES[remote_sessions.STATE_INSTALLING]


def test_a_failed_install_fails_the_connection_with_pips_own_words(ssh):
    """Launching anyway would run the copy the upgrade was meant to replace,
    and the actionable line is always in the output."""
    ssh.queue.append(FakeProcess(
        ["ERROR: Could not find a version that satisfies the requirement "
         "plexora"], dead_with=1))
    session = remote_sessions.start(a_remote(install=True), askpass_url=None)

    assert wait_for(lambda: session.state == remote_sessions.STATE_FAILED)
    assert "pip exited 1" in session.error
    assert "Could not find a version" in session.error
    # Nothing was launched: one ssh, and it was the install.
    assert len(ssh.spawned) == 1


def test_only_so_many_connections_may_be_opening_at_once(ssh):
    """The cap is on connections being OPENED, not on connections. Each one
    holds two ssh processes and a thread while it waits, and the way to get
    forty of them is a page retrying rather than anybody's intent."""
    ssh.healthy = False  # so they stay mid-establishment
    for index in range(remote_sessions.MAX_CONNECTING):
        remote_sessions.start(a_remote(f"host{index}"), askpass_url=None,
                              timeout=30)
    with pytest.raises(remote_sessions.ConnectionRefused):
        remote_sessions.start(a_remote("one-too-many"), askpass_url=None)


def test_connecting_something_already_connected_is_refused(ssh):
    remote_sessions.start(a_remote(), askpass_url=None)
    with pytest.raises(remote_sessions.ConnectionRefused):
        remote_sessions.start(a_remote(), askpass_url=None)


def test_disconnecting_stops_the_ssh_processes(ssh):
    process = FakeProcess(block=True)
    ssh.queue.append(process)
    session = remote_sessions.start(a_remote(), askpass_url=None)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    session.stop()

    assert process.terminated is True
    assert session.state == remote_sessions.STATE_EXITED


# -- a session that ends on its own tidies up after itself ------------------


def test_a_node_session_that_dies_on_its_own_takes_its_node_off_the_map(ssh):
    """Nobody presses Disconnect on a walltime. The session's own teardown is
    the only thing left that knows the node it registered ran inside the job
    that just ended -- and while the entry stood, /resource_routing kept
    offering the dead address to browsers and /resource_status called a
    project reading from it fine."""
    forgotten = []
    process = FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"],
        block=True)
    ssh.queue.append(process)
    session = remote_sessions.start(
        a_remote(), askpass_url=None, kind=remote_sessions.KIND_NODE,
        allow_origin="http://127.0.0.1:8000",
        register=lambda name, *args, **extra: name,
        unregister=forgotten.append)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    process.returncode = 0  # the job ends: walltime, a drop, a crash

    assert wait_for(lambda: session.state == remote_sessions.STATE_EXITED)
    assert wait_for(lambda: forgotten == ["hpc"])


def test_a_session_that_dies_on_its_own_stops_its_sibling_processes(ssh):
    """Under srun the tunnel is a second ssh. The job leg exiting is what ends
    `wait()`, and on a cluster that does not adopt compute-side ssh into the
    job nothing else ends the tunnel: dead session, live listener, forwarding
    into refused connections until the app exits."""
    job = FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t "
         "hostname=compute-9"],
        block=True)
    tunnel = FakeProcess(block=True)
    ssh.queue.extend([job, tunnel])
    session = remote_sessions.start(
        a_remote(srun="-t 1:00:00"), askpass_url=None,
        kind=remote_sessions.KIND_NODE,
        allow_origin="http://127.0.0.1:8000",
        register=lambda name, *args, **extra: name)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)
    assert tunnel.terminated is False

    job.returncode = 0  # Slurm's walltime, as this side sees it

    assert wait_for(lambda: session.state == remote_sessions.STATE_EXITED)
    assert wait_for(lambda: tunnel.terminated)


def test_a_deliberate_disconnect_leaves_the_forgetting_to_the_route(ssh):
    """stop() is somebody acting, and the route that serves them forgets the
    node itself with its own managed_by check. The session teardown running a
    second, competing forget on that path would be two owners of one entry."""
    forgotten = []
    process = FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"],
        block=True)
    ssh.queue.append(process)
    session = remote_sessions.start(
        a_remote(), askpass_url=None, kind=remote_sessions.KIND_NODE,
        allow_origin="http://127.0.0.1:8000",
        register=lambda name, *args, **extra: name,
        unregister=forgotten.append)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    session.stop()

    assert wait_for(lambda: not session._thread.is_alive())
    assert forgotten == []


def test_a_failed_connection_releases_what_it_spawned(ssh):
    """Establishment spawns real ssh before it can fail. A failed connection's
    children serve nobody, and replacing the record without stopping them left
    their watcher entries in connect._ACTIVE for the life of the app (and,
    under srun, held the job and its walltime bill). The process here is
    already dead -- _Watched.stop() rightly skips the terminate -- so what is
    pinned is the release itself."""
    ssh.healthy = False
    process = FakeProcess(["bash: plexora: command not found"], dead_with=127)
    ssh.queue.append(process)
    session = remote_sessions.start(a_remote(), askpass_url=None)

    assert wait_for(lambda: session.state == remote_sessions.STATE_FAILED)
    assert wait_for(lambda: session.session is not None
                    and all(watched not in connect._ACTIVE
                            for watched in session.session.watchers))


def test_connecting_again_over_a_dead_session_reaps_it_first(ssh):
    """start() replaces a failed or exited session's record. Doing so without
    stopping it first leaked its watcher entries into connect._ACTIVE for the
    life of the app -- and any child process that survived its session."""
    ssh.healthy = False
    ssh.queue.append(FakeProcess(
        ["bash: plexora: command not found"], dead_with=127))
    first = remote_sessions.start(a_remote(), askpass_url=None)
    assert wait_for(lambda: first.state == remote_sessions.STATE_FAILED)

    reaped = []
    first.stop = lambda: reaped.append(True)
    ssh.healthy = True
    second = remote_sessions.start(a_remote(), askpass_url=None)

    assert reaped == [True]
    assert wait_for(lambda: second.state == remote_sessions.STATE_CONNECTED)
    assert remote_sessions.get("hpc") is second


def test_the_connect_route_wires_both_halves_of_registration(
        client, monkeypatch):
    """The route supplies register AND unregister: recording a node when it
    announces, and taking it off the map when the session dies on its own.
    Wiring only the first is how a dead address stayed on offer."""
    captured = {}

    def refuse(remote, **kwargs):
        captured.update(kwargs)
        raise remote_sessions.ConnectionRefused("not now")

    monkeypatch.setattr(remote_sessions, "start", refuse)
    monkeypatch.setattr(remote_store, "find", lambda name: a_remote(name))

    response = client.post("/settings/remotes/hpc/connect?kind=node")

    assert response.status_code == 409
    assert callable(captured.get("register"))
    assert callable(captured.get("unregister"))


def test_forget_node_entry_only_removes_what_a_connection_wrote(
        plexora_data_root):
    """The same managed_by proof the disconnect route makes: an entry somebody
    registered by hand points at an address they maintain, and no session's
    teardown gets to speak for it."""
    from plexora import nodes as node_api
    from plexora.server.models import nodes as node_registry
    from plexora.server.routes.settings_routes import _forget_node_entry

    node_api.register_node("mine", "http://127.0.0.1:1", token="t",
                           verify=False, managed_by="connect:mine")
    node_api.register_node("hand", "http://127.0.0.1:2", token="t",
                           verify=False)

    _forget_node_entry("mine")
    _forget_node_entry("hand")

    left = node_registry.load_all()
    assert "mine" not in left
    assert "hand" in left


# -- the askpass relay -----------------------------------------------------


def test_ssh_is_spawned_with_the_relay_wired_in_and_no_terminal(ssh):
    """SSH_ASKPASS_REQUIRE is what makes ssh use the helper; start_new_session
    is what stops it finding the server's own console and prompting into a
    window nobody is looking at."""
    session = remote_sessions.start(
        a_remote(), askpass_url="http://127.0.0.1:8000/settings/remotes/_askpass")
    assert wait_for(lambda: bool(ssh.envs and ssh.envs[0]))

    env = ssh.envs[0]
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert env["PLEXORA_ASKPASS_NONCE"] == session.nonce
    assert env["PLEXORA_ASKPASS_URL"].endswith("/_askpass")
    assert os.path.exists(env["SSH_ASKPASS"])
    assert ssh.detach is True


def test_a_prompt_travels_to_the_page_and_the_answer_back_to_ssh(ssh):
    session = remote_sessions.start(a_remote(), askpass_url="http://x/_askpass")
    prompt = session.open_prompt("Password:")

    assert session.state == remote_sessions.STATE_AUTHENTICATING
    assert session.status()["prompt"] == {"id": prompt.id, "text": "Password:"}
    # Nothing to collect until somebody types something.
    assert session.collect(prompt.id) is None

    assert session.answer("hunter2", prompt.id) is True
    assert session.collect(prompt.id) == "hunter2"


def test_answering_returns_the_page_to_the_wait_it_interrupted(ssh):
    """On a cluster the password is asked for after the job has been submitted,
    so the state underneath the prompt is already "queued". Going back to
    "connecting" instead left the page claiming to be opening an SSH connection
    for the entire wait in the queue -- the longest part, and the one the
    wording exists to explain."""
    session = remote_sessions.start(a_remote(srun="-p interactive"),
                                    askpass_url="http://x/_askpass")
    assert wait_for(
        lambda: session.state == remote_sessions.STATE_WAITING_FOR_JOB)

    prompt = session.open_prompt("Password:")
    assert session.state == remote_sessions.STATE_AUTHENTICATING

    session.answer("hunter2", prompt.id)

    assert session.state == remote_sessions.STATE_WAITING_FOR_JOB
    assert "scheduler" in session.status()["phase"]
    session.stop()


def test_an_answer_is_handed_over_exactly_once(ssh):
    session = remote_sessions.start(a_remote(), askpass_url="http://x/_askpass")
    prompt = session.open_prompt("Password:")
    session.answer("hunter2", prompt.id)

    assert session.collect(prompt.id) == "hunter2"
    assert session.collect(prompt.id) == ""


def test_the_secret_is_in_no_status_payload_and_no_log(ssh):
    session = remote_sessions.start(a_remote(), askpass_url="http://x/_askpass")
    prompt = session.open_prompt("Password:")
    session.answer("hunter2", prompt.id)

    body = json.dumps(session.status())
    assert "hunter2" not in body


def test_an_answer_for_the_wrong_prompt_is_refused(ssh):
    session = remote_sessions.start(a_remote(), askpass_url="http://x/_askpass")
    session.open_prompt("Password:")
    assert session.answer("hunter2", "some-other-id") is False


def test_a_failed_connection_releases_the_helper_waiting_on_a_prompt(ssh):
    """Otherwise the askpass process polls until its own timeout, holding an
    ssh open behind a question nobody will ever answer."""
    session = remote_sessions.start(a_remote(), askpass_url="http://x/_askpass")
    prompt = session.open_prompt("Password:")
    session._fail(RuntimeError("gave up"))

    assert session.collect(prompt.id) is False


# -- one password, however many hops ---------------------------------------
#
# Three ssh authentications is the ordinary shape of one cluster connection --
# the job, the login node again as a jump host, then the compute node -- and at
# a site that authenticates by password that was three identical boxes for one
# press of Connect. What these pin is that it is now one box, and the two
# things that keep that from being a way to lock somebody's account.


def establishing(**kwargs):
    """A session part-way through opening, with no thread and no fake ssh.

    The constructor leaves exactly that state, and it is the state the reuse
    window is defined by -- so these reach the prompt logic from ssh's side,
    where it lives, rather than racing a fake connection to `connected`.
    """
    return remote_sessions.RemoteSession(a_remote(**kwargs),
                                         askpass_url="http://x/_askpass")


def ask(session, text, asker="pid:1"):
    """One prompt, and whatever ssh would have collected for it."""
    prompt = session.open_prompt(text, asker=asker)
    return prompt, session.collect(prompt.id)


LOGIN = "me@login.cluster.edu's password: "
NODE = "me@compute-a-01's password: "


def test_one_password_answers_every_hop_of_one_connection():
    """The job ssh asks, the person types, and the two the tunnel makes are
    answered from that -- including the jump hop, whose question is the login
    node's and therefore word for word the one already answered."""
    session = establishing()

    first, nothing = ask(session, LOGIN, asker="pid:100")
    assert first.reused is False
    assert nothing is None                      # nobody has typed yet
    session.answer("hunter2", first.id)
    assert session.collect(first.id) == "hunter2"

    jump, answer = ask(session, LOGIN, asker="pid:201")
    assert (jump.reused, answer) == (True, "hunter2")
    compute, answer = ask(session, NODE, asker="pid:200")
    assert (compute.reused, answer) == (True, "hunter2")

    # And none of it reached the page: an answered question is not pending.
    assert session.status()["prompt"] is None


def test_a_reused_answer_does_not_make_the_page_say_it_is_authenticating():
    """The state is what the page reads to say what it is waiting for. While a
    question is being answered from memory it is not waiting for a person, and
    on a cluster what it is really waiting for is the scheduler."""
    session = establishing()
    first, _ = ask(session, LOGIN, asker="pid:100")
    session.answer("hunter2", first.id)
    session._phase_state = remote_sessions.STATE_WAITING_FOR_JOB
    session.state = remote_sessions.STATE_WAITING_FOR_JOB

    ask(session, NODE, asker="pid:200")

    assert session.state == remote_sessions.STATE_WAITING_FOR_JOB


def test_the_same_ssh_asking_twice_is_a_refusal_and_goes_back_to_the_person():
    """ssh does not report a rejected password, it simply asks again. Offering
    the same one back would spend every attempt the site allows before anybody
    could be told -- which is how a typo becomes a locked account."""
    session = establishing()
    first, _ = ask(session, LOGIN, asker="pid:100")
    session.answer("wrong", first.id)
    session.collect(first.id)

    again, answer = ask(session, LOGIN, asker="pid:100")

    assert (again.reused, answer) == (False, None)
    # And the refused one is not kept for the hops that follow either.
    later, answer = ask(session, NODE, asker="pid:200")
    assert (later.reused, answer) == (False, None)


def test_an_unidentifiable_asker_treats_any_repeat_as_a_refusal():
    """Windows cannot say which ssh is asking (the .bat wrapper cannot exec),
    so there the wording is all there is. Erring towards asking again costs one
    typing; erring the other way replays a secret that was just rejected."""
    session = establishing()
    first, _ = ask(session, LOGIN, asker=None)
    session.answer("hunter2", first.id)
    session.collect(first.id)

    # The jump hop's question is the login node's, so with no asker to tell
    # them apart it reads as a refusal and is put to the person.
    jump, _ = ask(session, LOGIN, asker=None)
    assert jump.reused is False
    session.answer("hunter2", jump.id)
    session.collect(jump.id)

    # The hop after it is still spared: two typings rather than three.
    assert ask(session, NODE, asker=None)[1] == "hunter2"


@pytest.mark.parametrize("prompt", [
    "Duo two-factor login for me\n\nPasscode or option (1-3): ",
    "Verification code: ",
    "Enter your one-time code: ",
    "The authenticity of host 'login (10.0.0.1)' can't be established.\n"
    "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
])
def test_a_one_time_answer_is_never_given_a_second_time(prompt):
    """A code that is true once, and a trust decision about one host key, are
    not secrets that can be reused -- replaying either is worse than asking."""
    session = establishing()
    first, _ = ask(session, prompt, asker="pid:100")
    session.answer("123456", first.id)
    session.collect(first.id)

    assert ask(session, prompt, asker="pid:200")[0].reused is False
    # Nor does answering one leave anything behind for a password prompt.
    assert ask(session, LOGIN, asker="pid:200")[0].reused is False


def test_wording_nobody_recognises_is_treated_as_unrepeatable():
    """A site whose prompt this does not know is exactly the site where a guess
    about what may be replayed would be wrong."""
    session = establishing()
    first, _ = ask(session, "Answer the security question: ", asker="pid:100")
    session.answer("Fido", first.id)
    session.collect(first.id)

    assert ask(session, "Answer the other one: ", asker="pid:200")[0].reused is False


def test_a_key_passphrase_is_reused_only_for_the_same_key():
    session = establishing()
    one = "Enter passphrase for key '/home/me/.ssh/id_ed25519': "
    other = "Enter passphrase for key '/home/me/.ssh/id_rsa': "
    first, _ = ask(session, one, asker="pid:100")
    session.answer("open sesame", first.id)
    session.collect(first.id)

    assert ask(session, one, asker="pid:200")[1] == "open sesame"
    assert ask(session, other, asker="pid:200")[0].reused is False


def test_nothing_typed_outlives_the_connection_it_was_typed_for(ssh):
    """The reuse exists to get one connection open, not to hold a password for
    the afternoon. Once there is nothing left to open there is nothing left to
    hold one for, so a later prompt -- a rekey, a hop an hour from now -- goes
    to the person again."""
    session = remote_sessions.start(a_remote(), askpass_url="http://x/_askpass")
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    first, _ = ask(session, LOGIN, asker="pid:100")
    session.answer("hunter2", first.id)
    session.collect(first.id)

    assert ask(session, NODE, asker="pid:200")[0].reused is False


def test_a_failed_connection_forgets_what_was_typed_for_it(ssh):
    session = establishing()
    first, _ = ask(session, LOGIN, asker="pid:100")
    session.answer("hunter2", first.id)
    session.collect(first.id)

    session._fail(RuntimeError("gave up"))

    assert ask(session, NODE, asker="pid:200")[0].reused is False


def test_a_reused_answer_is_visible_in_the_log_and_the_secret_is_not(ssh):
    """A credential used somewhere the user did not watch it being used should
    still be findable afterwards -- but the log is served, so the line says
    that it happened and never what was used."""
    session = establishing()
    first, _ = ask(session, LOGIN, asker="pid:100")
    session.answer("hunter2", first.id)
    session.collect(first.id)
    ask(session, NODE, asker="pid:200")

    body = json.dumps(session.status())
    assert "hunter2" not in body
    assert "what you typed a moment ago" in body


def test_the_helper_names_the_ssh_that_is_asking(monkeypatch):
    """`exec` in the POSIX wrapper is what makes this the ssh rather than a
    shell: without it every prompt would look like a different asker, and a
    refused password would be replayed instead of re-asked."""
    from plexora import askpass

    monkeypatch.setattr(os, "getppid", lambda: 4242)
    monkeypatch.setattr(os, "name", "posix")
    assert askpass.asking_process() == "pid:4242"

    monkeypatch.setattr(os, "name", "nt")
    assert askpass.asking_process() is None


def test_a_nonce_finds_its_own_session_and_nothing_else(ssh):
    first = remote_sessions.start(a_remote("one"), askpass_url="http://x/_askpass")
    second = remote_sessions.start(a_remote("two"), askpass_url="http://x/_askpass")

    assert remote_sessions.find_by_nonce(first.nonce) is first
    assert remote_sessions.find_by_nonce(second.nonce) is second
    assert remote_sessions.find_by_nonce("not-a-nonce") is None
    assert remote_sessions.find_by_nonce("") is None


def test_a_token_on_a_log_line_is_redacted_before_it_is_served():
    """The Stage-C node announce prints a token on stdout, which is safe
    inside the ssh channel and not safe in a page anyone can screenshot."""
    line = "[plexora-node] host=c42 port=8642 node_id=abc token=s3cr3tvalue"
    assert "s3cr3tvalue" not in remote_sessions.redact(line)
    assert "node_id=abc" in remote_sessions.redact(line)


def test_the_askpass_helper_asks_and_waits_and_prints_the_answer(monkeypatch):
    """The helper is what ssh actually executes, so its contract is the shape
    of the two requests and the answer landing on stdout."""
    from plexora import askpass

    calls = []
    answers = [{"state": "pending"}, {"state": "answered", "answer": "hunter2"}]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        calls.append(url)
        if "prompt" in url:
            return Response({"id": "p1"})
        return Response(answers.pop(0))

    monkeypatch.setattr(askpass, "_urlopen", urlopen)
    monkeypatch.setattr(askpass, "_sleep", lambda seconds: None)

    answer = askpass.ask("Password:", env={
        askpass.ENV_URL: "http://127.0.0.1:8000/settings/remotes/_askpass",
        askpass.ENV_NONCE: "n0nce",
    })

    assert answer == "hunter2"
    assert calls[0].endswith("/prompt")
    assert "nonce=n0nce" in calls[1] and "id=p1" in calls[1]


def test_the_helper_refuses_to_run_outside_a_connection():
    from plexora import askpass

    with pytest.raises(askpass.AskpassError):
        askpass.ask("Password:", env={})


def test_the_helper_writes_nothing_to_stdout_when_it_fails(monkeypatch, capsys):
    """ssh reads stdout as the answer, so a message written there would be
    tried as a password."""
    from plexora import askpass

    assert askpass.main(["Password:"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "askpass" in captured.err


# -- the routes ------------------------------------------------------------


def test_saving_and_listing_a_server(client, plexora_data_root):
    answer = client.post("/settings/remotes", json={
        "name": "hpc", "target": "me@login.cluster.edu",
        "remote_command": "conda run -n imaging plexora",
        "use_srun": True, "srun": "-p interactive",
    })
    assert answer.status_code == 200

    listed = client.get("/settings/remotes").get_json()["remotes"]
    assert [item["name"] for item in listed] == ["hpc"]
    assert listed[0]["state"] == "idle"
    assert listed[0]["srun"] == "-p interactive"


def test_a_server_with_no_address_is_refused(client, plexora_data_root):
    answer = client.post("/settings/remotes", json={"name": "hpc"})
    assert answer.status_code == 400
    assert "address" in answer.get_json()["error"]


def test_a_server_with_no_name_is_refused(client, plexora_data_root):
    answer = client.post("/settings/remotes", json={"target": "me@host"})
    assert answer.status_code == 400


def test_a_name_that_would_collide_with_the_askpass_route_is_refused(
        client, plexora_data_root):
    answer = client.post("/settings/remotes",
                         json={"name": "_askpass", "target": "me@host"})
    assert answer.status_code == 400


def test_connect_answers_immediately_with_202(client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    answer = client.post("/settings/remotes/hpc/connect")

    assert answer.status_code == 202
    assert answer.get_json()["state"] in (
        "connecting", "tunneling", "connected")


def test_connecting_an_unknown_server_is_a_404(client, plexora_data_root):
    assert client.post("/settings/remotes/nope/connect").status_code == 404


def test_a_second_connect_while_one_is_up_is_refused(client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")
    again = client.post("/settings/remotes/hpc/connect")

    assert again.status_code == 409


def test_status_reports_the_url_once_it_is_connected(client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")

    assert wait_for(lambda: client.get("/settings/remotes/hpc/status")
                    .get_json()["state"] == "connected")
    body = client.get("/settings/remotes/hpc/status").get_json()
    assert body["url"].startswith("http://127.0.0.1:")


def test_forgetting_a_server_disconnects_it_first(client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")
    session = remote_sessions.get("hpc")
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    client.delete("/settings/remotes/hpc")

    assert remote_sessions.get("hpc") is None
    assert remote_store.find("hpc") is None
    assert session.state == remote_sessions.STATE_EXITED


def test_the_askpass_routes_need_the_right_nonce(client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")
    session = remote_sessions.get("hpc")

    refused = client.post("/settings/remotes/_askpass/prompt",
                          json={"nonce": "guessed", "prompt": "Password:"})
    assert refused.status_code == 403

    opened = client.post("/settings/remotes/_askpass/prompt",
                         json={"nonce": session.nonce, "prompt": "Password:"})
    assert opened.status_code == 200
    prompt_id = opened.get_json()["id"]

    pending = client.get("/settings/remotes/_askpass/answer",
                         query_string={"nonce": session.nonce, "id": prompt_id})
    assert pending.get_json()["state"] == "pending"

    client.post("/settings/remotes/hpc/answer",
                json={"id": prompt_id, "answer": "hunter2"})

    delivered = client.get("/settings/remotes/_askpass/answer",
                           query_string={"nonce": session.nonce, "id": prompt_id})
    assert delivered.get_json() == {"state": "answered", "answer": "hunter2"}


def test_one_answer_through_the_routes_serves_the_hop_after_it(
        client, plexora_data_root, ssh):
    """The path the ssh processes actually take, end to end: the helper posts a
    question, the page posts the answer, and the next hop's identical question
    is answered on the way in rather than reaching the page at all.

    An `srun` profile because that is the shape with hops in it, and because
    the fake ssh never announces a node -- which leaves the session where a
    real one spends its longest minutes, still opening."""
    remote_store.save(a_remote(srun="-p interactive"))
    client.post("/settings/remotes/hpc/connect")
    session = remote_sessions.get("hpc")
    assert wait_for(
        lambda: session.state == remote_sessions.STATE_WAITING_FOR_JOB)

    opened = client.post("/settings/remotes/_askpass/prompt",
                         json={"nonce": session.nonce, "prompt": LOGIN,
                               "asker": "pid:100"}).get_json()
    client.post("/settings/remotes/hpc/answer",
                json={"id": opened["id"], "answer": "hunter2"})
    client.get("/settings/remotes/_askpass/answer",
               query_string={"nonce": session.nonce, "id": opened["id"]})

    # The jump hop: the same words, a different ssh.
    again = client.post("/settings/remotes/_askpass/prompt",
                        json={"nonce": session.nonce, "prompt": LOGIN,
                              "asker": "pid:201"}).get_json()
    delivered = client.get("/settings/remotes/_askpass/answer",
                           query_string={"nonce": session.nonce,
                                         "id": again["id"]}).get_json()

    assert delivered == {"state": "answered", "answer": "hunter2"}
    assert session.status()["prompt"] is None
    session.stop()


def test_answering_when_nothing_asked_is_refused(client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")

    answer = client.post("/settings/remotes/hpc/answer", json={"answer": "x"})
    assert answer.status_code == 409


def test_the_status_payload_never_carries_the_answer(client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")
    session = remote_sessions.get("hpc")
    prompt = session.open_prompt("Password:")
    client.post("/settings/remotes/hpc/answer",
                json={"id": prompt.id, "answer": "hunter2"})

    body = client.get("/settings/remotes/hpc/status").get_data(as_text=True)
    assert "hunter2" not in body


# -- the profile survives being edited -------------------------------------


def test_editing_an_address_keeps_the_fields_the_form_has_no_box_for(
        client, plexora_data_root):
    """A Settings save must not amount to "drop everything I set from the CLI".

    `jump`, `ssh_opts`, `plugins`, `local_node` and the node names are set by
    `plexora connect --save` or by hand, and the form sends none of them.
    Reading them straight off the payload -- which is what a missing key looks
    like -- silently erased somebody's bastion host the first time they fixed a
    typo in the address.
    """
    remote_store.save(remote_store.Remote(
        name="hpc",
        target="me@old.cluster.edu",
        jump="me@bastion",
        ssh_opts=("Compression=yes",),
        plugins="roi,gating",
        serve=("image:slide=/data/slide.ome.tif",),
        local_serve=("table:cells=/home/me/cells.csv",),
        node_name="hpc-data",
        local_node=False,
    ))

    saved = client.post("/settings/remotes",
                        json={"name": "hpc", "target": "me@new.cluster.edu"})
    assert saved.status_code == 200

    kept = remote_store.get("hpc")
    assert kept.target == "me@new.cluster.edu"
    assert kept.jump == "me@bastion"
    assert kept.ssh_opts == ("Compression=yes",)
    assert kept.plugins == "roi,gating"
    assert kept.serve == ("image:slide=/data/slide.ome.tif",)
    assert kept.local_serve == ("table:cells=/home/me/cells.csv",)
    assert kept.node_name == "hpc-data"
    assert kept.local_node is False


def test_a_field_the_form_does_send_is_still_changeable(client, plexora_data_root):
    """The other half of the rule: preserved means "when absent", not "always"."""
    remote_store.save(a_remote(jump="me@bastion"))

    client.post("/settings/remotes",
                json={"name": "hpc", "target": "me@login.cluster.edu",
                      "jump": ""})

    assert remote_store.get("hpc").jump is None


def test_an_unknown_key_in_a_hand_edited_file_is_not_lost_on_a_save(
        client, plexora_data_root):
    """`extra` exists so a key this version does not know about survives it."""
    path = remote_store.remotes_path()
    path.write_text(json.dumps({"hpc": {"target": "me@login.cluster.edu",
                                        "from_the_future": "keep me"}}),
                    encoding="utf-8")

    client.post("/settings/remotes",
                json={"name": "hpc", "target": "me@login.cluster.edu"})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["hpc"]["from_the_future"] == "keep me"


# -- the node's name, everywhere it is written down -------------------------


def test_a_node_session_reports_the_name_the_node_is_actually_on_the_map_under(
        ssh, plexora_data_root):
    """A profile with its own `node_name` registers under that, not under the
    profile's name -- so a status that reported the profile name would hand a
    data form an identifier resolving to nothing."""
    recorded = {}

    def register(name, endpoint, token, **extra):
        recorded.update({"name": name}, **extra)
        return name

    ssh.queue.append(FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"],
        block=True))

    session = remote_sessions.start(
        a_remote(node_name="hpc-data"), askpass_url=None,
        kind=remote_sessions.KIND_NODE, allow_origin="http://127.0.0.1:8000",
        register=register)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    assert recorded["name"] == "hpc-data"
    assert recorded["managed_by"] == "connect:hpc-data"
    # The three names agree: what was registered, what `managed_by` records,
    # and what a form waiting to browse it is told to address.
    assert session.status()["node"] == "hpc-data"


def _map_a_node(name, **extra):
    """Put an entry on the map without a handshake. No node is running here."""
    from plexora.server.models import nodes as node_registry

    return node_registry.save(node_registry.Node(
        name=name, endpoint="http://127.0.0.1:41000", token="t", extra=extra))


def test_disconnecting_forgets_the_node_under_its_own_name(
        client, plexora_data_root):
    """The node entry IS the tunnel. Left behind, it offers a machine that
    cannot answer, under a port the next session gives to something else."""
    from plexora.server.models import nodes as node_registry

    remote_store.save(a_remote(node_name="hpc-data"))
    _map_a_node("hpc-data", managed_by="connect:hpc-data")

    client.post("/settings/remotes/hpc/disconnect", query_string={"kind": "node"})

    assert "hpc-data" not in node_registry.load_all()


def test_a_node_registered_by_hand_is_left_alone(client, plexora_data_root):
    """`managed_by` is the proof of ownership. Someone else's node points at an
    address they can fix and is none of this route's business."""
    from plexora.server.models import nodes as node_registry

    remote_store.save(a_remote(node_name="hpc-data"))
    _map_a_node("hpc-data")

    client.post("/settings/remotes/hpc/disconnect", query_string={"kind": "node"})

    assert "hpc-data" in node_registry.load_all()


# -- how much of the log a request may ask for ------------------------------


def test_the_status_route_serves_a_short_tail_by_default_and_more_on_request(
        client, plexora_data_root, ssh):
    """The list of every profile wants the last thing that happened; a modal
    watching one connection wants the whole buffer, because a stack of
    authentication failures is exactly what somebody needs to read."""
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")
    session = remote_sessions.get("hpc")
    for number in range(60):
        session._echo(f"line {number}")

    short = client.get("/settings/remotes/hpc/status").get_json()
    assert len(short["log"]) == 25

    deep = client.get("/settings/remotes/hpc/status",
                      query_string={"log": 200}).get_json()
    assert len(deep["log"]) == 60
    assert deep["log"][0] == "line 0"


def test_the_log_length_is_clamped_and_a_nonsense_value_is_ignored(
        client, plexora_data_root, ssh):
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")
    session = remote_sessions.get("hpc")
    for number in range(400):
        session._echo(f"line {number}")

    asked = client.get("/settings/remotes/hpc/status",
                       query_string={"log": 100000}).get_json()
    assert len(asked["log"]) == remote_sessions.LOG_LINES

    nonsense = client.get("/settings/remotes/hpc/status",
                          query_string={"log": "lots"}).get_json()
    assert len(nonsense["log"]) == 25


def test_a_deep_log_is_redacted_like_the_shallow_one(
        client, plexora_data_root, ssh):
    """The tail is the one place a node announce's token would escape the ssh
    channel it travels in, and asking for more of it is not an exemption."""
    remote_store.save(a_remote())
    client.post("/settings/remotes/hpc/connect")
    remote_sessions.get("hpc")._echo(
        "[plexora-node] host=c42 port=41000 node_id=ab token=s3cr3t")

    body = client.get("/settings/remotes/hpc/status",
                      query_string={"log": 200}).get_data(as_text=True)
    assert "s3cr3t" not in body
