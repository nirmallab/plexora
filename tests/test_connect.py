"""`plexora connect` -- the ssh commands it builds and the order it runs them in.

No real ssh is spawned here and none should be: what is worth pinning is the
argv, which is the part that has to be exactly right on a cluster nobody can
reach from CI, and the two-process choreography of `--srun`, where the tunnel
cannot be built until the job has said where it landed.

The module is loaded straight off disk, the same way tests/test_cli.py loads
cli.py, because it is required to stay importable without the plexora package.
"""

import importlib.util
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "plexora_connect_under_test", ROOT / "plexora" / "connect.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


connect_mod = _load()


# -- fakes ----------------------------------------------------------------


class FakeProcess:
    """Enough of Popen for _Watched: output to drain, and a liveness answer."""

    def __init__(self, lines=(), dead_with=None):
        self.stdout = iter([line + "\n" for line in lines])
        self.returncode = dead_with
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
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
def rig(monkeypatch):
    """The module with every outside edge replaced, plus a record of the calls."""
    calls = types.SimpleNamespace(spawned=[], opened=[], echoed=[], healthy=True)

    def popen(argv, **kwargs):
        calls.spawned.append(argv)
        return calls.queue.pop(0) if calls.queue else FakeProcess()

    def urlopen(url, timeout=None):
        if not calls.healthy:
            raise OSError("refused")
        return FakeResponse()

    calls.queue = []
    monkeypatch.setattr(connect_mod, "_popen", popen)
    monkeypatch.setattr(connect_mod, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(connect_mod, "_urlopen", urlopen)
    monkeypatch.setattr(connect_mod, "_open_browser", calls.opened.append)
    monkeypatch.setattr(connect_mod, "_sleep", lambda seconds: None)
    # Same numbers on both ends, so an assertion can name one port.
    monkeypatch.setattr(connect_mod, "pick_ports",
                        lambda local=None, remote=None, **kw: (local or 9999,
                                                               remote or 9999))
    calls.echo = calls.echoed.append
    return calls


# -- pure command construction -------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [("me@host", ("me", "host")), ("host", (None, "host"))],
)
def test_split_target(target, expected):
    assert connect_mod.split_target(target) == expected


def test_remote_command_line_adds_the_flags_the_local_side_depends_on():
    line = connect_mod.remote_command_line("plexora", 8123)
    assert line == "plexora --remote --no-browser --port 8123"


def test_remote_command_line_leaves_the_command_itself_unquoted():
    """--remote-command is the escape hatch for environments where reaching
    Plexora is a shell expression, so it must survive intact."""
    line = connect_mod.remote_command_line("conda run -n imaging plexora", 8123)
    assert line.startswith("conda run -n imaging plexora --remote")


def test_remote_command_line_quotes_what_came_from_argv():
    line = connect_mod.remote_command_line("plexora", 8123, datasource="Tonsil 2")
    assert "'Tonsil 2'" in line


def test_remote_command_line_passes_through_the_optional_flags():
    line = connect_mod.remote_command_line(
        "plexora", 8123, bind_node=True, data_dir="/scratch/me", plugins=""
    )
    assert "--bind-node" in line
    assert "--data-dir /scratch/me" in line
    assert "--plugins ''" in line


def test_an_empty_plugins_value_survives_as_an_empty_string():
    """"" means core-only and must reach the remote as such, not vanish."""
    assert "--plugins" in connect_mod.remote_command_line("plexora", 1, plugins="")
    assert "--plugins" not in connect_mod.remote_command_line("plexora", 1)


def test_srun_command_line_wraps_the_launch():
    line = connect_mod.srun_command_line(
        "-p interactive -t 1:00:00", "plexora --remote --port 8123"
    )
    assert line == "srun -p interactive -t 1:00:00 plexora --remote --port 8123"


def test_direct_ssh_carries_both_the_forward_and_the_command():
    argv = connect_mod.direct_ssh_argv("me@host", 9000, 8123, "plexora --remote")
    assert argv == ["ssh", "-t", "-L", "9000:127.0.0.1:8123", "me@host",
                    "plexora --remote"]


def test_direct_ssh_accepts_a_jump_host_and_options():
    argv = connect_mod.direct_ssh_argv(
        "me@host", 9000, 8123, "plexora", jump="me@gate",
        ssh_opts=["ServerAliveInterval=30"],
    )
    assert argv[:6] == ["ssh", "-t", "-o", "ServerAliveInterval=30", "-J", "me@gate"]


def test_the_job_ssh_carries_no_forward():
    """It cannot: -L is set up when the connection opens, and at that moment
    the scheduler has not said which node to point it at."""
    argv = connect_mod.job_ssh_argv("me@login", "srun -p x plexora --remote")
    assert "-L" not in argv


def test_the_default_tunnel_goes_through_the_login_node_into_the_compute_node():
    argv = connect_mod.tunnel_ssh_argv("me@login", 9000, "compute-a-16", 8123,
                                       user="me")
    assert argv == ["ssh", "-N", "-J", "me@login", "me@compute-a-16",
                    "-L", "9000:127.0.0.1:8123"]


def test_bind_node_forwards_from_the_login_node_instead():
    argv = connect_mod.tunnel_ssh_argv("me@login", 9000, "compute-a-16", 8123,
                                       user="me", bind_node=True)
    assert argv == ["ssh", "-N", "-L", "9000:compute-a-16:8123", "me@login"]


def test_the_second_hop_reuses_the_username_from_the_target():
    """The local account name is not the cluster account name, especially on
    Windows where it may contain a space."""
    argv = connect_mod.tunnel_ssh_argv("aj123@login", 9000, "n1", 8123, user="aj123")
    assert "aj123@n1" in argv


# -- the announce line ----------------------------------------------------


def test_parse_announce_reads_the_line_plexora_remote_prints():
    assert connect_mod.parse_announce(
        "[plexora-remote] node=compute-a-16 port=8123"
    ) == ("compute-a-16", 8123)


def test_parse_announce_survives_a_pty_prefix():
    """srun banners and shell noise share the line; -t adds carriage returns."""
    assert connect_mod.parse_announce(
        "srun: job 42 queued\t[plexora-remote] node=n1 port=1"
    ) == ("n1", 1)


@pytest.mark.parametrize("line", ["", "hello", "[plexora-remote] node=n1"])
def test_parse_announce_returns_none_for_anything_else(line):
    assert connect_mod.parse_announce(line) is None


def test_missing_command_is_recognised_from_the_shell_message():
    assert connect_mod.looks_like_missing_command(["bash: plexora: command not found"])
    assert not connect_mod.looks_like_missing_command(["Serving Plexora at ..."])


# -- port pairing ---------------------------------------------------------


def test_ports_match_on_both_ends_when_they_can():
    local, remote = connect_mod.pick_ports(randint=lambda a, b: 50000,
                                           is_free=lambda port: True)
    assert (local, remote) == (50000, 50000)


def test_a_busy_local_port_falls_back_without_changing_the_remote_one():
    local, remote = connect_mod.pick_ports(randint=lambda a, b: 50000,
                                           is_free=lambda port: False,
                                           free_port=lambda: 41111)
    assert (local, remote) == (41111, 50000)


def test_an_explicit_local_port_is_honoured():
    local, remote = connect_mod.pick_ports(local_port=7000,
                                           randint=lambda a, b: 50000,
                                           is_free=lambda port: True)
    assert (local, remote) == (7000, 50000)


# -- orchestration --------------------------------------------------------


def test_direct_mode_spawns_one_ssh_and_opens_the_local_url(rig):
    rig.queue = [FakeProcess(["[plexora-remote] node=host port=9999"])]

    code = connect_mod.connect("me@host", echo=rig.echo, browser=True)

    assert code == 0
    assert len(rig.spawned) == 1
    assert rig.spawned[0][:2] == ["ssh", "-t"]
    assert rig.opened == ["http://127.0.0.1:9999/"]


def test_a_datasource_is_appended_to_the_url_that_is_opened(rig):
    rig.queue = [FakeProcess([])]

    connect_mod.connect("me@host", "tonsil", echo=rig.echo)

    assert rig.opened == ["http://127.0.0.1:9999/tonsil"]


def test_no_browser_still_sets_the_tunnel_up(rig):
    rig.queue = [FakeProcess([])]

    assert connect_mod.connect("me@host", echo=rig.echo, browser=False) == 0
    assert rig.opened == []


def test_srun_mode_waits_for_the_announce_then_tunnels_to_that_node(rig):
    """The whole point of the two-process design: the node is not knowable
    until the scheduler has granted it."""
    rig.queue = [
        FakeProcess(["srun: job 4242 queued and waiting for resources",
                     "[plexora-remote] node=compute-a-16 port=9999"]),
        FakeProcess([]),
    ]

    code = connect_mod.connect("me@login", srun="-p interactive", echo=rig.echo)

    assert code == 0
    job_argv, tunnel_argv = rig.spawned
    assert "srun -p interactive plexora --remote --no-browser --port 9999" in job_argv
    assert tunnel_argv == ["ssh", "-N", "-J", "me@login", "me@compute-a-16",
                           "-L", "9999:127.0.0.1:9999"]


def test_srun_with_bind_node_passes_the_flag_through_and_forwards_from_login(rig):
    rig.queue = [
        FakeProcess(["[plexora-remote] node=compute-a-16 port=9999"]),
        FakeProcess([]),
    ]

    connect_mod.connect("me@login", srun="-p x", bind_node=True, echo=rig.echo)

    job_argv, tunnel_argv = rig.spawned
    assert "--bind-node" in job_argv[-1]
    assert tunnel_argv == ["ssh", "-N", "-L", "9999:compute-a-16:9999", "me@login"]


def test_a_child_that_dies_before_answering_is_retried_on_a_new_port(rig):
    """A remote port collision is plausible -- the number was picked blind --
    and entirely recoverable."""
    rig.queue = [FakeProcess([], dead_with=1), FakeProcess([])]

    code = connect_mod.connect("me@host", echo=rig.echo)

    assert code == 0
    assert len(rig.spawned) == 2


def test_giving_up_names_the_printed_instructions_as_the_fallback(rig):
    rig.queue = [FakeProcess([], dead_with=1) for _ in range(3)]

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", echo=rig.echo)

    assert "plexora --remote" in str(excinfo.value)
    assert len(rig.spawned) == 3


def test_a_missing_remote_plexora_is_reported_with_the_flag_that_fixes_it(rig):
    rig.queue = [FakeProcess(["bash: plexora: command not found"], dead_with=127)]

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", echo=rig.echo)

    message = str(excinfo.value)
    assert "--remote-command" in message
    assert "command not found" in message
    # Reported rather than retried: another port would fail identically.
    assert len(rig.spawned) == 1


def test_a_pinned_remote_port_is_not_retried(rig):
    """--remote-port is an instruction; retrying would try the same thing."""
    rig.queue = [FakeProcess([], dead_with=1)]

    with pytest.raises(SystemExit):
        connect_mod.connect("me@host", remote_port=8123, echo=rig.echo)

    assert len(rig.spawned) == 1


def test_both_processes_are_torn_down_tunnel_first(rig):
    job = FakeProcess(["[plexora-remote] node=n1 port=9999"])
    tunnel = FakeProcess([])
    rig.queue = [job, tunnel]

    connect_mod.connect("me@login", srun="-p x", echo=rig.echo)

    assert tunnel.terminated
    assert connect_mod._ACTIVE == []


def test_a_health_check_that_never_answers_times_out_with_advice(rig):
    rig.queue = [FakeProcess([])]
    rig.healthy = False

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", timeout=0.05, attempts=1, echo=rig.echo)

    assert "--timeout" in str(excinfo.value)


def test_no_ssh_on_the_path_says_how_to_get_one(monkeypatch, rig):
    monkeypatch.setattr(connect_mod, "_which", lambda name: None)

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", echo=rig.echo)

    assert "ssh" in str(excinfo.value).lower()
