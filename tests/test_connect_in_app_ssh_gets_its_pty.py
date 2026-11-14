"""A connection the app opens still gives the far side its pty.

`-t` is what ties a POSIX remote's life to its ssh: with a pty, sshd hangs
it up when the connection ends, so Disconnect stops `plexora --remote` (or
`plexora node serve`) and a queued or running `srun` is hung up with it. But
OpenSSH honours a single `-t` only when its own stdin is a terminal --
otherwise it prints "Pseudo-terminal will not be allocated because stdin is
not a terminal." and runs the command without one.

A connection opened from Settings is spawned with `detach=True` by
`remote_sessions.RemoteSession._build`, inside a server whose stdin is never
the user's terminal on the desktop (it is the shell's lifeline pipe) and is
the user's terminal when `plexora` runs in one. So the ssh that carries the
remote Plexora must (a) force its pty whatever its stdin is (`-tt`), and (b)
not be handed the server's own stdin: an inherited terminal would be put in
raw mode by ssh, and Ctrl+C typed at the local server would go down the
channel instead of stopping it.

The module is loaded straight off disk, the way tests/test_connect.py loads it.
"""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "plexora_connect_in_app_pty_under_test",
        ROOT / "plexora" / "connect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


connect_mod = _load()


class FakeProcess:
    """Enough of Popen for _Watched: output to drain, and a liveness answer."""

    def __init__(self, lines=()):
        self.stdout = iter([line + "\n" for line in lines])
        self.stdin = None
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def spawns(monkeypatch):
    """Every ssh the connection starts: its argv and the stdin it was given."""
    record = []
    queue = []

    def popen(argv, **kwargs):
        record.append((argv, kwargs.get("stdin")))
        return queue.pop(0) if queue else FakeProcess()

    monkeypatch.setattr(connect_mod, "_popen", popen)
    monkeypatch.setattr(connect_mod, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(connect_mod, "_urlopen",
                        lambda url, timeout=None: FakeResponse())
    monkeypatch.setattr(connect_mod, "_open_browser", lambda url: None)
    monkeypatch.setattr(connect_mod, "_sleep", lambda seconds: None)
    monkeypatch.setattr(connect_mod, "_ACTIVE", [])
    monkeypatch.setattr(connect_mod, "pick_ports",
                        lambda local=None, remote=None, **kw: (local or 9999,
                                                               remote or 9999))
    return record, queue


def _forces_a_pty(argv):
    """OpenSSH's own rule: a pty regardless of stdin needs `-tt` (two `-t`)
    or `RequestTTY=force`; one `-t` is dropped when stdin is not a tty."""
    before_target = argv[:argv.index("me@host")] if "me@host" in argv else argv
    count = 0
    for i, word in enumerate(before_target):
        if word == "-t":
            count += 1
        elif word == "-tt":
            count += 2
        elif word == "-o" and i + 1 < len(before_target) and \
                before_target[i + 1].replace(" ", "").lower() \
                == "requesttty=force":
            count += 2
    return count >= 2


def _the_remote_plexora_ssh(record, label_word):
    """The spawn whose command line starts Plexora over there."""
    for argv, stdin in record:
        if any(label_word in word for word in argv):
            return argv, stdin
    raise AssertionError(f"no ssh launched {label_word!r}: {record!r}")


def _check(argv, stdin):
    assert _forces_a_pty(argv), (
        "an in-app ssh asks for its pty with a single -t, which ssh drops "
        "when its stdin is not a terminal (the desktop's lifeline pipe): "
        f"{argv!r}")
    assert stdin is not None, (
        "an in-app ssh inherits the server's own stdin -- the desktop's "
        "lifeline pipe, or the terminal `plexora` was started in")


def test_an_in_app_viewer_connection_gives_the_remote_its_pty(spawns):
    record, _queue = spawns
    session = connect_mod.Session("me@host", detach=True, local_node=False,
                                  echo=lambda line: None)
    session.establish()
    try:
        argv, stdin = _the_remote_plexora_ssh(record, "--remote")
        _check(argv, stdin)
    finally:
        session.stop()


def test_an_in_app_queued_viewer_job_gives_srun_its_pty(spawns):
    record, queue = spawns
    queue.append(FakeProcess(["[plexora-remote] node=compute-b-16 port=9999"]))
    session = connect_mod.Session("me@host", detach=True, local_node=False,
                                  srun="-p short", echo=lambda line: None)
    session.establish()
    try:
        argv, stdin = _the_remote_plexora_ssh(record, "srun")
        _check(argv, stdin)
    finally:
        session.stop()


def test_an_in_app_data_node_gives_the_remote_its_pty(spawns):
    record, queue = spawns
    queue.append(FakeProcess([
        "[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"]))
    session = connect_mod.NodeSession(
        "me@host", node_name="hpc", local_port=9100, remote_port=41000,
        detach=True, echo=lambda line: None,
        register=lambda *a, **k: "hpc")
    session.establish()
    try:
        argv, stdin = _the_remote_plexora_ssh(record, "serve")
        _check(argv, stdin)
    finally:
        session.stop()
