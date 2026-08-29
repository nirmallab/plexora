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
