"""`plexora connect` -- the ssh commands it builds and the order it runs them in.

No real ssh is spawned here and none should be: what is worth pinning is the
argv, which is the part that has to be exactly right on a cluster nobody can
reach from CI, and the two-process choreography of `--srun`, where the tunnel
cannot be built until the job has said where it landed.

The module is loaded straight off disk, the same way tests/test_cli.py loads
cli.py, because it is required to stay importable without the plexora package.
"""

import importlib.util
import json
import types
import urllib.error
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
    calls = types.SimpleNamespace(spawned=[], spawn_envs=[], opened=[],
                                  echoed=[], healthy=True)

    def popen(argv, **kwargs):
        calls.spawned.append(argv)
        calls.spawn_envs.append(kwargs.get("env"))
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


def test_an_environment_prefix_becomes_the_program_inside_it():
    """What a user knows is where they made the environment. `conda env list`
    prints prefixes; nothing prints the path to the entry point."""
    line = connect_mod.remote_command_line(
        "/home/ajn16/miniconda3/envs/plexora", 8123)
    assert line.startswith(
        "env PYTHONUNBUFFERED=1 /home/ajn16/miniconda3/envs/plexora/bin/plexora "
        "--remote")


def test_a_trailing_slash_on_a_prefix_does_not_double_up():
    line = connect_mod.remote_command_line("/opt/envs/plexora/", 8123)
    assert "/opt/envs/plexora/bin/plexora --remote" in line
    assert "//" not in line


def test_an_executable_path_is_not_given_a_second_bin_plexora():
    line = connect_mod.remote_command_line(
        "/home/ajn16/miniconda3/envs/plexora/bin/plexora", 8123)
    assert line.startswith(
        "env PYTHONUNBUFFERED=1 /home/ajn16/miniconda3/envs/plexora/bin/plexora "
        "--remote")


def test_a_wrapper_script_is_left_alone():
    """A dot in the last component is how a path says it is a program."""
    line = connect_mod.remote_command_line("/home/me/run-plexora.sh", 8123)
    assert line.startswith("env PYTHONUNBUFFERED=1 /home/me/run-plexora.sh --remote")


def test_a_posix_path_is_unbuffered_because_srun_gives_it_a_pipe():
    """Under --srun the task's stdout is not a tty, so a remote old enough to
    print its announce line without flushing would never deliver it."""
    line = connect_mod.remote_command_line("/opt/plexora/bin/plexora", 8123)
    assert line.startswith("env PYTHONUNBUFFERED=1 ")


def test_a_shell_expression_is_never_prefixed():
    """`env` cannot exec a shell builtin, a function, or an && expression."""
    for command in ("conda run -n imaging plexora",
                    "module load python && plexora"):
        line = connect_mod.remote_command_line(command, 8123)
        assert line.startswith(command + " --remote"), line


def test_a_bare_name_is_left_alone_for_windows_remotes():
    """`env` is not a program on Windows, and a bare name is no evidence the
    far side is POSIX."""
    line = connect_mod.remote_command_line("plexora", 8123)
    assert line == "plexora --remote --no-browser --port 8123"


def test_node_command_line_resolves_a_prefix_the_same_way():
    line = connect_mod.node_command_line(
        "/home/ajn16/miniconda3/envs/plexora", 8642, ["table:cells=/data/c.h5ad"])
    assert line.startswith(
        "env PYTHONUNBUFFERED=1 /home/ajn16/miniconda3/envs/plexora/bin/plexora "
        "node serve")


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


#: What `_ssh_options` puts on every ssh before anything the caller asked for.
#: Spliced into the argv assertions below so they keep pinning the shape of the
#: command rather than re-stating the keepalive policy; the policy itself is
#: pinned once, in the three tests under "-- keepalive --".
KEEPALIVE = ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]


def test_direct_ssh_carries_both_the_forward_and_the_command():
    argv = connect_mod.direct_ssh_argv("me@host", 9000, 8123, "plexora --remote")
    assert argv == ["ssh", "-t", *KEEPALIVE, "-L", "9000:127.0.0.1:8123",
                    "me@host", "plexora --remote"]


def test_direct_ssh_accepts_a_jump_host_and_options():
    argv = connect_mod.direct_ssh_argv(
        "me@host", 9000, 8123, "plexora", jump="me@gate",
        ssh_opts=["Compression=yes"],
    )
    assert argv[:10] == ["ssh", "-t", *KEEPALIVE, "-o", "Compression=yes",
                         "-J", "me@gate"]


# -- keepalive -------------------------------------------------------------


def test_every_ssh_is_told_to_notice_when_the_far_end_stops_answering():
    """Without this a dead tunnel is a hang, not a failure.

    A slept laptop, a dropped VPN or a job that ended leaves ssh holding a TCP
    connection nothing will ever answer: the process stays up, the forward
    stays open, and every request through it waits forever instead of failing
    somewhere the page can report it.
    """
    for argv in (connect_mod.direct_ssh_argv("me@host", 9000, 8123, "plexora"),
                 connect_mod.job_ssh_argv("me@login", "srun plexora"),
                 connect_mod.tunnel_ssh_argv("me@login", 9000, "n1", 8123,
                                             user="me")):
        assert "ServerAliveInterval=30" in argv
        assert "ServerAliveCountMax=3" in argv


def test_a_caller_who_set_the_interval_themselves_is_not_overruled():
    """And not passed twice: ssh honours the FIRST of a repeated option, so a
    default appended alongside the user's would silently win."""
    argv = connect_mod.direct_ssh_argv(
        "me@host", 9000, 8123, "plexora",
        ssh_opts=["ServerAliveInterval=120"],
    )
    assert argv.count("ServerAliveInterval=120") == 1
    assert "ServerAliveInterval=30" not in argv
    # The other half of the policy is untouched -- overriding the interval is
    # not asking for the count to be dropped as well.
    assert "ServerAliveCountMax=3" in argv


def test_the_job_ssh_carries_no_forward():
    """It cannot: -L is set up when the connection opens, and at that moment
    the scheduler has not said which node to point it at."""
    argv = connect_mod.job_ssh_argv("me@login", "srun -p x plexora --remote")
    assert "-L" not in argv


def test_the_default_tunnel_goes_through_the_login_node_into_the_compute_node():
    argv = connect_mod.tunnel_ssh_argv("me@login", 9000, "compute-a-16", 8123,
                                       user="me")
    assert argv == ["ssh", "-N", *KEEPALIVE, "-J", "me@login", "me@compute-a-16",
                    "-L", "9000:127.0.0.1:8123"]


def test_bind_node_forwards_from_the_login_node_instead():
    argv = connect_mod.tunnel_ssh_argv("me@login", 9000, "compute-a-16", 8123,
                                       user="me", bind_node=True)
    assert argv == ["ssh", "-N", *KEEPALIVE, "-L", "9000:compute-a-16:8123",
                    "me@login"]


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


def test_a_step_that_knows_what_failed_says_so_and_is_not_overruled():
    """`looks_like_missing_command` reads substrings out of whatever the
    remote printed, and "not found" appears in a great deal of text that has
    nothing to do with a PATH. When the gcloud mount step started printing
    apt's own log on failure, that log contained `gpg: not found` -- and a
    connection whose VM was missing a package began telling people to go and
    edit a remote-command setting that was entirely correct.

    So an error built by a step that had the output in front of it carries a
    flag, and the guessing upstream defers to it."""
    plain = connect_mod.ConnectError("something went wrong")
    assert plain.diagnosed is False
    known = connect_mod.ConnectError("Preparing the data failed", diagnosed=True)
    assert known.diagnosed is True
    # The marker that caused it is still a marker -- the fix is precedence,
    # not making the heuristic blind.
    assert connect_mod.looks_like_missing_command(["sh: 1: gpg: not found"])


# -- what the scheduler said ----------------------------------------------


#: What HMS O2 actually sent back for a job submitted with no partition, in
#: the order it arrived. The four banner lines are the point: they are what a
#: user saw, and the two that matter were in the middle of them.
O2_NO_PARTITION = [
    "Use your lower case HMS ID, like abc123, not ABC123.",
    "If locked out, see: https://it.hms.harvard.edu/i-want/"
    "reset-password-or-unlock-your-hms-account",
    "srun: error: Job not submitted: please specify partition with -p.",
    "srun: error: Unable to allocate resources: Invalid partition name specified",
    "Connection to o2.hms.harvard.edu closed.",
]


def test_the_login_banner_is_not_the_reason_the_job_failed():
    said = connect_mod.scheduler_lines(O2_NO_PARTITION)
    assert len(said) == 2
    assert all(line.startswith("srun: error:") for line in said)
    assert not any("abc123" in line for line in said)
    assert not any("reset-password" in line for line in said)


def test_a_missing_partition_is_named_ahead_of_the_refusal_it_caused():
    """Two refusals arrive: no `-p`, and then the empty partition that was
    submitted because there was no `-p`. Only the first has a fix."""
    message = connect_mod.scheduler_refusal(O2_NO_PARTITION)
    assert "no default partition" in message
    assert "-p <partition>" in message
    assert "sinfo -s" in message
    # Both of srun's lines are still shown; it is the advice that picks one.
    assert "please specify partition" in message
    assert "Invalid partition name" in message
    # And the banner is gone from the message entirely.
    assert "abc123" not in message


def test_a_partition_that_does_not_exist_is_a_different_fix():
    message = connect_mod.scheduler_refusal([
        "srun: error: Unable to allocate resources: Invalid partition name "
        "specified",
    ])
    assert "does not exist here" in message
    assert "no default partition" not in message


def test_a_job_too_big_for_its_partition_points_at_the_boxes_that_size_it():
    message = connect_mod.scheduler_refusal([
        "srun: error: Unable to allocate resources: Requested node "
        "configuration is not available",
    ])
    assert "cores or the memory" in message


def test_a_refusal_nobody_has_a_table_entry_for_still_beats_a_raw_tail():
    message = connect_mod.scheduler_refusal([
        "Use your lower case HMS ID, like abc123, not ABC123.",
        "srun: error: Unable to allocate resources: Something new in 2031",
    ])
    assert "Something new in 2031" in message
    assert "abc123" not in message
    assert "scheduler arguments" in message


def test_srun_narrating_the_queue_is_not_srun_refusing():
    """The normal case prints on the same prefix. A job that queued and then
    hit the timeout must not be reported as a job the scheduler turned down."""
    assert connect_mod.scheduler_refusal([
        "srun: job 9182734 queued and waiting for resources",
        "srun: job 9182734 has been allocated resources",
    ]) is None


def test_nothing_from_the_scheduler_means_no_scheduler_diagnosis():
    assert connect_mod.scheduler_refusal([]) is None
    assert connect_mod.scheduler_refusal(
        ["bash: plexora: command not found"]) is None


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

    code = connect_mod.connect("me@host", echo=rig.echo, browser=True,
                               local_node=False)

    assert code == 0
    assert len(rig.spawned) == 1
    assert rig.spawned[0][:2] == ["ssh", "-t"]
    assert rig.opened == ["http://127.0.0.1:9999/"]


def test_a_datasource_is_appended_to_the_url_that_is_opened(rig):
    rig.queue = [FakeProcess([])]

    connect_mod.connect("me@host", "tonsil", echo=rig.echo, local_node=False)

    assert rig.opened == ["http://127.0.0.1:9999/tonsil"]


def test_no_browser_still_sets_the_tunnel_up(rig):
    rig.queue = [FakeProcess([])]

    assert connect_mod.connect("me@host", echo=rig.echo, browser=False,
                                   local_node=False) == 0
    assert rig.opened == []


def test_srun_mode_waits_for_the_announce_then_tunnels_to_that_node(rig):
    """The whole point of the two-process design: the node is not knowable
    until the scheduler has granted it."""
    rig.queue = [
        FakeProcess(["srun: job 4242 queued and waiting for resources",
                     "[plexora-remote] node=compute-a-16 port=9999"]),
        FakeProcess([]),
    ]

    code = connect_mod.connect("me@login", srun="-p interactive", echo=rig.echo,
                               local_node=False)

    assert code == 0
    job_argv, tunnel_argv = rig.spawned
    assert "srun -p interactive plexora --remote --no-browser --port 9999" in job_argv
    assert tunnel_argv == ["ssh", "-N", *KEEPALIVE, "-J", "me@login",
                           "me@compute-a-16", "-L", "9999:127.0.0.1:9999"]


def test_srun_with_bind_node_passes_the_flag_through_and_forwards_from_login(rig):
    rig.queue = [
        FakeProcess(["[plexora-remote] node=compute-a-16 port=9999"]),
        FakeProcess([]),
    ]

    connect_mod.connect("me@login", srun="-p x", bind_node=True, echo=rig.echo,
                        local_node=False)

    job_argv, tunnel_argv = rig.spawned
    assert "--bind-node" in job_argv[-1]
    assert tunnel_argv == ["ssh", "-N", *KEEPALIVE,
                           "-L", "9999:compute-a-16:9999", "me@login"]


def test_a_child_that_dies_before_answering_is_retried_on_a_new_port(rig):
    """A remote port collision is plausible -- the number was picked blind --
    and entirely recoverable."""
    rig.queue = [FakeProcess([], dead_with=1), FakeProcess([])]

    code = connect_mod.connect("me@host", echo=rig.echo, local_node=False)

    assert code == 0
    assert len(rig.spawned) == 2


def test_giving_up_names_the_printed_instructions_as_the_fallback(rig):
    rig.queue = [FakeProcess([], dead_with=1) for _ in range(3)]

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", echo=rig.echo, local_node=False)

    assert "plexora --remote" in str(excinfo.value)
    assert len(rig.spawned) == 3


def test_a_missing_remote_plexora_is_reported_with_the_flag_that_fixes_it(rig):
    rig.queue = [FakeProcess(["bash: plexora: command not found"], dead_with=127)]

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", echo=rig.echo, local_node=False)

    message = str(excinfo.value)
    assert "--remote-command" in message
    assert "command not found" in message
    # Reported rather than retried: another port would fail identically.
    assert len(rig.spawned) == 1


def test_a_pinned_remote_port_is_not_retried(rig):
    """--remote-port is an instruction; retrying would try the same thing."""
    rig.queue = [FakeProcess([], dead_with=1)]

    with pytest.raises(SystemExit):
        connect_mod.connect("me@host", remote_port=8123, echo=rig.echo,
                            local_node=False)

    assert len(rig.spawned) == 1


def test_both_processes_are_torn_down_tunnel_first(rig):
    job = FakeProcess(["[plexora-remote] node=n1 port=9999"])
    tunnel = FakeProcess([])
    rig.queue = [job, tunnel]

    connect_mod.connect("me@login", srun="-p x", echo=rig.echo, local_node=False)

    assert tunnel.terminated
    assert connect_mod._ACTIVE == []


def test_a_health_check_that_never_answers_times_out_with_advice(rig):
    rig.queue = [FakeProcess([])]
    rig.healthy = False

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", timeout=0.05, attempts=1, echo=rig.echo,
                            local_node=False)

    assert "--timeout" in str(excinfo.value)


def test_no_ssh_on_the_path_says_how_to_get_one(monkeypatch, rig):
    monkeypatch.setattr(connect_mod, "_which", lambda name: None)

    with pytest.raises(SystemExit) as excinfo:
        connect_mod.connect("me@host", echo=rig.echo, local_node=False)

    assert "ssh" in str(excinfo.value).lower()


# -- what counts as "Plexora answered" ------------------------------------


def _health(monkeypatch, raises=None, **kwargs):
    """One health poll against a far side that answers `raises`, or 204."""
    def urlopen(url, timeout=None):
        if raises is not None:
            raise raises
        return FakeResponse()

    monkeypatch.setattr(connect_mod, "_urlopen", urlopen)
    monkeypatch.setattr(connect_mod, "_sleep", lambda seconds: None)
    return connect_mod._wait_for_health(
        "http://127.0.0.1:9999/", connect_mod._now() + 0.2, (), **kwargs)


def _http_error(code):
    return urllib.error.HTTPError("http://127.0.0.1:9999/", code, "no", {}, None)


def test_a_viewer_that_refuses_us_is_still_a_viewer_that_is_up(monkeypatch):
    """A remote viewer started with its own auth token answers 403.

    This side cannot know that token -- the announce line carries a node and a
    port and nothing else -- and it does not need to: the question this poll
    asks is whether Plexora is listening at the far end of the tunnel, and a
    refusal answers it. Without this the connection times out against a viewer
    that came up perfectly.
    """
    assert _health(monkeypatch, raises=_http_error(403), any_answer=True) is True


def test_a_node_that_refuses_us_is_a_wrong_token_and_stays_loud(monkeypatch):
    """The opposite call, for the opposite reason: a node poll sends the token
    it was given, so 403 means that token is wrong -- which no amount of
    waiting fixes and must not be reported as a healthy node."""
    assert _health(monkeypatch, raises=_http_error(403)) is False


def test_a_far_side_that_is_broken_rather_than_guarded_is_not_alive(monkeypatch):
    """500 is Plexora failing, not Plexora refusing, either way round."""
    assert _health(monkeypatch, raises=_http_error(500), any_answer=True) is False
    assert _health(monkeypatch, raises=OSError("refused"), any_answer=True) is False


def test_an_ordinary_answer_is_alive_under_both_readings(monkeypatch):
    assert _health(monkeypatch) is True
    assert _health(monkeypatch, any_answer=True) is True


# -- forwarding a data node's port too -----------------------------------


def test_a_bare_port_forwards_to_the_same_number_on_both_ends():
    assert connect_mod.parse_forward("8642") == (8642, 8642)
    assert connect_mod.parse_forward(" 9000:8642 ") == (9000, 8642)


def test_extra_forwards_target_the_remote_loopback():
    # Loopback on the far side, because that is where `plexora node serve`
    # binds by default -- forwarding to the interface it is NOT listening on
    # fails in a way that reads as the node being down.
    assert connect_mod.extra_forwards(["8642", "9000:8643"]) == [
        "-L", "8642:127.0.0.1:8642",
        "-L", "9000:127.0.0.1:8643",
    ]
    assert connect_mod.extra_forwards([]) == []


def test_a_direct_connection_carries_every_forward():
    argv = connect_mod.direct_ssh_argv("me@host", 8000, 8000, "plexora --remote",
                                   forwards=["8642"])
    assert argv.count("-L") == 2
    assert "8642:127.0.0.1:8642" in argv
    # The command still comes last, after every flag.
    assert argv[-1] == "plexora --remote"
    assert argv[-2] == "me@host"


def test_a_job_tunnel_carries_forwards_through_the_compute_node():
    argv = connect_mod.tunnel_ssh_argv("me@login", 8000, "gpu-3", 49200,
                                   user="me", forwards=["8642"])
    assert "8642:127.0.0.1:8642" in argv


def test_a_bind_node_tunnel_points_forwards_at_the_compute_node():
    # In this form the forward is made FROM the login node, whose loopback is a
    # different machine's -- so the node's address is what a second forward has
    # to name.
    argv = connect_mod.tunnel_ssh_argv("me@login", 8000, "gpu-3", 49200,
                                   bind_node=True, forwards=["8642"])
    assert "8642:gpu-3:8642" in argv
    assert argv[-1] == "me@login"


# -- data nodes across two machines ---------------------------------------
#
# Three layouts, and the thing they have in common is that no token ever
# appears in a command line. Every one of them is on a shared login node where
# `ps` is readable by every other account, so the token travels back on the
# node's own stdout, inside the ssh channel, and the registration that uses it
# is POSTed through the tunnel.


def test_a_node_announce_carries_everything_needed_to_register_it():
    found = connect_mod.parse_node_announce(
        "[plexora-node] host=c42 port=8642 node_id=ab12 token=s3cr3t")
    assert found == {"host": "c42", "port": 8642, "node_id": "ab12",
                     "token": "s3cr3t",
                     # Absent from this line, and present as None rather than
                     # missing: every reader gets the same shape of answer.
                     "hostname": None}


def test_a_line_that_is_not_a_node_announce_is_not_one():
    assert connect_mod.parse_node_announce("[plexora-remote] node=c42 port=1") is None
    assert connect_mod.parse_node_announce("starting up") is None


def test_reverse_forwards_point_back_at_this_machine():
    """-R, not -L: in this layout the viewer is the far side and the data is
    here, so it is the far side that needs a port to open."""
    assert connect_mod.reverse_forwards([(41000, 8642)]) == [
        "-R", "41000:127.0.0.1:8642"]
    assert connect_mod.reverse_forwards([]) == []


def test_a_reverse_forward_rides_the_ssh_that_carries_the_viewer():
    argv = connect_mod.direct_ssh_argv("me@host", 9999, 9999, "plexora",
                                       reverse=[(41000, 8642)])
    assert "-R" in argv and "41000:127.0.0.1:8642" in argv


def test_a_reverse_forward_rides_the_tunnel_in_srun_mode():
    """The job ssh cannot carry it -- that connection is opened before the
    scheduler has said which node to point at."""
    argv = connect_mod.tunnel_ssh_argv("me@login", 9999, "c42", 8123,
                                       user="me", reverse=[(41000, 8642)])
    assert argv[argv.index("-R") + 1] == "41000:127.0.0.1:8642"


def test_the_remote_launch_line_asks_for_a_node_by_port_and_never_by_token():
    line = connect_mod.remote_command_line(
        "plexora", 8123, also_serve=["table:cells=/scratch/c.h5ad"],
        node_port=41000, node_allow_origin="http://127.0.0.1:9999")
    assert "--also-serve table:cells=/scratch/c.h5ad" in line
    assert "--node-port 41000" in line
    assert "--node-allow-origin" in line
    assert "token" not in line


def test_a_node_only_host_runs_node_serve_rather_than_a_viewer():
    line = connect_mod.node_command_line(
        "plexora", 8642, ["image:t=/scratch/t.ome.tif"])
    assert line == ("plexora node serve --port 8642 --host 127.0.0.1 "
                    "--serve image:t=/scratch/t.ome.tif")


def test_a_node_opened_from_a_data_field_starts_empty_and_takes_files_later():
    """The whole point of choosing a location when the data is added: at the
    moment this command line is built, nobody has picked a file yet. Without
    --dynamic the node would come up serving nothing, forever."""
    line = connect_mod.node_command_line(
        "plexora", 8642, (), dynamic=True, node_id="connect-hpc-data",
        allow_origin="http://127.0.0.1:8000")
    assert "--dynamic" in line
    # A stable id, because it names the manifest on the far side -- same id
    # next session, same resource ids, and a project reopens without being
    # repointed.
    assert "--node-id connect-hpc-data" in line
    # And the browser's exact origin, or every direct tile fetch fails CORS and
    # silently falls back to being proxied through Plexora.
    assert "--allow-origin http://127.0.0.1:8000" in line
    assert "--serve" not in line and "token" not in line


def test_a_node_session_forwards_a_port_and_registers_what_answers(rig):
    """Plexora stays here; only the far side's files move. That makes this the
    mirror image of a viewer session, and the difference that matters is which
    machine ends up holding the project."""
    recorded = {}

    def register(name, endpoint, token, **extra):
        recorded.update({"name": name, "endpoint": endpoint, "token": token},
                        **extra)
        return name

    rig.queue.append(FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"]))
    rig.healthy = True
    session = connect_mod.NodeSession(
        "me@login", node_name="hpc", local_port=9100, remote_port=41000,
        echo=rig.echo, register=register, allow_origin="http://127.0.0.1:8000")
    session.establish()

    argv = rig.spawned[0]
    assert "-L" in argv and "9100:127.0.0.1:41000" in argv
    # No srun anywhere: a data node wants the filesystem, not an allocation,
    # and a wait in a queue in the middle of a form is not a trade worth making.
    assert not any("srun" in str(part) for part in argv)
    assert recorded["name"] == "hpc"
    assert recorded["endpoint"] == "http://127.0.0.1:9100"
    # The browser is on this machine too, so the direct path IS the only path.
    assert recorded["browser_endpoint"] == "http://127.0.0.1:9100"
    assert recorded["managed_by"] == "connect:hpc"
    assert recorded["token"] == "s3cr3t"


def test_an_old_plexora_on_the_far_side_is_named_as_that(rig):
    """The symptom points at the wrong machine. What comes back is an argparse
    usage dump, which reads as "you typed something wrong" -- and the user
    typed none of it. Nothing on this end can be adjusted to fix it."""
    rig.healthy = False
    rig.queue.append(FakeProcess([
        "usage: plexora node [-h] {serve,connect,prepare} ...",
        "plexora node: error: unrecognized arguments: --dynamic",
    ], dead_with=2))

    session = connect_mod.NodeSession("me@login", node_name="hpc",
                                      echo=rig.echo, timeout=1)
    with pytest.raises(connect_mod.ConnectError) as raised:
        session.establish()

    message = str(raised.value)
    assert "too old" in message and "--dynamic" in message
    assert "me@login" in message
    # And not the PATH advice: argparse's refusal contains "usage:" and a list
    # of subcommands, which trips the missing-command markers.
    assert "PATH" not in message


def test_a_node_that_announced_and_never_answered_shows_what_it_printed(rig):
    """The announce is printed BEFORE the node binds, so "it started" and "it
    is listening" are different facts -- and when the second one never arrives,
    the node's own output is the entire evidence. It used to be discarded, so
    a node that died of a port collision reported only a timeout."""
    rig.healthy = False
    rig.queue.append(FakeProcess([
        "[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t",
        "OSError: [Errno 48] Address already in use",
    ], dead_with=1))

    session = connect_mod.NodeSession("me@login", node_name="hpc",
                                      echo=rig.echo, timeout=1)
    with pytest.raises(connect_mod.ConnectError) as raised:
        session.establish()

    message = str(raised.value)
    assert "Address already in use" in message
    # And it says what that means, because the port was never probed -- it is
    # picked at random out of the ephemeral range.
    assert "try again" in message


def test_a_node_still_loading_is_not_reported_as_a_dead_one(rig):
    """A live process that has not answered yet is a slow start, and the fix
    for it is nothing like the fix for a crash."""
    rig.healthy = False
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"]))

    session = connect_mod.NodeSession("me@login", node_name="hpc",
                                      echo=rig.echo, timeout=1)
    with pytest.raises(connect_mod.ConnectError) as raised:
        session.establish()

    message = str(raised.value)
    assert "before it binds" in message
    # The two things it actually is, both named, both with something to do.
    assert "node serve" in message and "AllowTcpForwarding" in message
    assert "stopped" not in message


def test_a_node_gets_longer_than_a_viewer_to_start():
    """60 seconds is measured against a machine that has run Plexora before.
    A node started on demand, mid-form, usually has not -- and the wait is a
    cold shared filesystem, not a network round trip."""
    assert connect_mod.NODE_START_TIMEOUT > connect_mod.DEFAULT_TIMEOUT
    assert connect_mod.NodeSession("me@login").timeout == connect_mod.NODE_START_TIMEOUT
    # Still overridable, for somebody who knows their own cluster.
    assert connect_mod.NodeSession("me@login", timeout=5).timeout == 5


def test_the_flag_an_old_remote_rejected_is_read_back_off_its_output():
    lines = ["plexora node: error: unrecognized arguments: --manifest"]
    assert connect_mod.unsupported_remote_flag(lines) == "--manifest"
    assert connect_mod.unsupported_remote_flag(["all fine here"]) is None


def test_a_saved_profile_is_the_source_of_truth_for_reaching_the_host():
    """Everything about how the machine is REACHED crosses over, `srun` most of
    all. Somebody who wrote "this is a login node -- run Plexora inside a job"
    said something about the machine, not about one feature of it, and a data
    node is sustained read I/O: exactly what such a rule is about.

    What stays behind is only what configures a viewer that is not being
    started -- and `serve`, which is the question the switch on every data
    field exists to stop asking in advance."""
    from plexora.server.models.remotes import Remote

    remote = Remote(name="hpc", target="me@login.edu", remote_command="/env/plexora",
                    jump="bastion", ssh_opts=("Compression=yes",),
                    srun="-p gpu", bind_node=True, plugins="roi",
                    datasource="tonsil", data_dir="/scratch/plexora",
                    forwards=("8642",), serve=("image:t=/scratch/t.tif",))
    kwargs = remote.as_node_kwargs()

    assert kwargs == {"remote_command": "/env/plexora", "srun": "-p gpu",
                      "bind_node": True, "jump": "bastion",
                      "ssh_opts": ("Compression=yes",), "plugins": "roi",
                      "node_name": "hpc", "install": False}


def test_a_profile_that_asks_for_a_job_gets_the_data_node_in_one(rig):
    """Two processes, exactly as the viewer does it: the job first, because at
    the moment it opens there is no compute node to point a forward at, then
    the tunnel to wherever the scheduler put it."""
    rig.queue.append(FakeProcess([
        "[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t "
        "hostname=compute-a-16",
    ]))
    rig.healthy = True

    session = connect_mod.NodeSession(
        "me@login", node_name="hpc", srun="-p interactive -c 16",
        local_port=9100, remote_port=41000, echo=rig.echo,
        register=lambda *a, **k: "hpc")
    session.establish()

    job_argv, tunnel_argv = rig.spawned
    # The job carries the launch line and no forward -- there is nothing to
    # forward to yet.
    assert "srun" in " ".join(job_argv) and "-p interactive -c 16" in " ".join(job_argv)
    assert "node serve" in " ".join(job_argv) and "--dynamic" in " ".join(job_argv)
    assert "-L" not in job_argv
    # The tunnel goes through the login node INTO the compute node the
    # scheduler named, which is knowable only from the announce.
    assert session.node == "compute-a-16"
    assert "-J" in tunnel_argv and "me@compute-a-16" in tunnel_argv
    assert "9100:127.0.0.1:41000" in tunnel_argv


def test_a_job_node_binds_where_its_tunnel_can_reach_it(rig):
    """The two ways of building the last hop need opposite bind addresses, and
    getting it backwards fails silently -- the forward opens onto an interface
    nothing is listening on."""
    def launched(**kwargs):
        rig.spawned.clear()
        rig.queue.append(FakeProcess([
            "[plexora-node] host=h port=41000 node_id=ab token=s3cr3t "
            "hostname=compute-a-16"]))
        connect_mod.NodeSession("me@login", srun="", local_port=9100,
                                remote_port=41000, echo=rig.echo,
                                register=lambda *a, **k: None,
                                **kwargs).establish()
        return " ".join(rig.spawned[0])

    # Into the compute node: it listens on its own loopback, where nothing else
    # on the cluster can reach it.
    assert "--host 127.0.0.1" in launched(bind_node=False)
    # Forwarded from the login node: the login node's loopback is a different
    # machine's, so the node has to be reachable on the internal network.
    assert "--host 0.0.0.0" in launched(bind_node=True)


def test_a_job_node_that_cannot_say_where_it_landed_is_refused(rig):
    """`hostname=` is what names the compute node, and an older Plexora does
    not send it. Tunnelling to a guess would be worse than saying so."""
    rig.queue.append(FakeProcess([
        "[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t"]))

    session = connect_mod.NodeSession("me@login", srun="-p gpu", echo=rig.echo,
                                      timeout=1)
    with pytest.raises(connect_mod.ConnectError) as raised:
        session.establish()

    assert "did not say which machine it landed on" in str(raised.value)
    assert len(rig.spawned) == 1  # and no tunnel was opened to nowhere


def test_the_node_announce_carries_the_machine_as_well_as_the_bind_address():
    """`host` is where it bound; `hostname` is which machine that loopback
    belongs to. Under a scheduler only the second one is useful, and it is the
    one thing the launching side cannot know."""
    found = connect_mod.parse_node_announce(
        "[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t "
        "hostname=compute-a-16")
    assert found["host"] == "127.0.0.1" and found["hostname"] == "compute-a-16"
    assert found["token"] == "s3cr3t" and found["port"] == 41000

    # An older node sends no such field, and still parses.
    older = connect_mod.parse_node_announce(
        "[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t")
    assert older["hostname"] is None and older["token"] == "s3cr3t"


def test_one_pipe_carries_two_announcements_in_either_order(rig):
    """The viewer's ssh and the data node beside it write to the same pipe, and
    which lands first is not something either end controls."""
    process = FakeProcess([
        "[plexora-node] host=c42 port=41000 node_id=ab token=s3cr3t",
        "[plexora-remote] node=c42 port=8123",
    ])
    rig.queue.append(process)
    watched = connect_mod._Watched(
        ["ssh"], "ssh", echo=rig.echo,
        matchers={"announce": connect_mod.parse_announce,
                  "node": connect_mod.parse_node_announce})
    watched.drain(timeout=2)

    assert watched.announce == ("c42", 8123)
    assert watched.found["node"]["token"] == "s3cr3t"


def test_registration_goes_to_the_viewer_s_own_settings_route(rig, monkeypatch):
    posted = {}

    class Response:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=None):
        posted["url"] = request.full_url
        posted["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(connect_mod, "_urlopen", urlopen)
    connect_mod.register_node_through(
        "http://127.0.0.1:9999/", "hpc-node", "http://127.0.0.1:41000",
        "s3cr3t", browser_endpoint="http://127.0.0.1:41001")

    assert posted["url"] == "http://127.0.0.1:9999/settings/nodes"
    assert posted["body"] == {
        "name": "hpc-node",
        "endpoint": "http://127.0.0.1:41000",
        "token": "s3cr3t",
        "browser_endpoint": "http://127.0.0.1:41001",
    }


def test_a_refused_registration_is_reported_rather_than_swallowed(rig, monkeypatch):
    class Response:
        def read(self):
            return b'{"error": "could not reach a data node there"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(connect_mod, "_urlopen",
                        lambda request, timeout=None: Response())
    with pytest.raises(connect_mod.ConnectError):
        connect_mod.register_node_through("http://127.0.0.1:9999", "n",
                                          "http://127.0.0.1:1", "t")


def test_a_co_located_node_is_forwarded_and_registered(rig, monkeypatch):
    """Layout one: viewer and node both on the cluster, browser here. The
    viewer reaches the node over the far side's loopback; the browser reaches
    it through a second forward on this one."""
    registered = {}
    monkeypatch.setattr(
        connect_mod, "register_node_through",
        lambda base, name, endpoint, token, **kw: registered.update(
            base=base, name=name, endpoint=endpoint, token=token, **kw))
    monkeypatch.setattr(connect_mod, "pick_remote_port", lambda randint=None: 41000)
    monkeypatch.setattr(connect_mod, "_free_local_port", lambda: 41001)
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=c42 port=41000 node_id=ab token=s3cr3t"]))

    session = connect_mod.Session(
        "me@host", echo=rig.echo, node_name="hpc", local_node=False,
        also_serve=["table:cells=/scratch/c.h5ad"])
    session.establish()

    argv = rig.spawned[0]
    assert "-L" in argv and "41001:127.0.0.1:41000" in argv
    assert "--also-serve" in " ".join(argv)
    assert registered["name"] == "hpc-node"
    assert registered["endpoint"] == "http://127.0.0.1:41000"
    assert registered["browser_endpoint"] == "http://127.0.0.1:41001"
    assert registered["token"] == "s3cr3t"
    assert session.data_nodes == [{
        "name": "hpc-node", "endpoint": "http://127.0.0.1:41000",
        "browser_endpoint": "http://127.0.0.1:41001"}]
    session.stop()


def test_a_laptop_side_node_is_reverse_forwarded_and_registered(rig, monkeypatch, tmp_path):
    """Layout three: images on the cluster, cell table never left this laptop.
    The remote viewer reaches back down the same ssh connection."""
    table = tmp_path / "cells.h5ad"
    table.write_bytes(b"")
    registered = {}
    monkeypatch.setattr(
        connect_mod, "register_node_through",
        lambda base, name, endpoint, token, **kw: registered.update(
            name=name, endpoint=endpoint, token=token, **kw))
    monkeypatch.setattr(connect_mod, "pick_remote_port", lambda randint=None: 41000)
    monkeypatch.setattr(connect_mod, "_free_local_port", lambda: 8642)
    # The local node is spawned first, so it is the first process handed out.
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=8642 node_id=cd token=l0cal"]))

    session = connect_mod.Session(
        "me@host", echo=rig.echo, node_name="study",
        node_manifest=str(tmp_path / "manifest.json"),
        local_serve=[f"table:cells={table}"])
    session.establish()

    node_argv, ssh_argv = rig.spawned[0], rig.spawned[1]
    assert node_argv[1:5] == ["-m", "plexora", "node", "serve"]
    assert "--token" not in node_argv
    # A pipe means block buffering, and a block-buffered announce sits
    # invisible while the parent waits its whole deadline for a node that is
    # up and serving. Cost a real afternoon before it was pinned here.
    assert rig.spawn_envs[0]["PYTHONUNBUFFERED"] == "1"
    assert "-R" in ssh_argv and "41000:127.0.0.1:8642" in ssh_argv
    assert registered["name"] == "study-local"
    # From the remote viewer, the node is a port on its own loopback...
    assert registered["endpoint"] == "http://127.0.0.1:41000"
    # ...and from the browser, which is on this machine, it is simply local.
    assert registered["browser_endpoint"] == "http://127.0.0.1:8642"
    # And the viewer is told this one is the user's own computer, which is the
    # only way it can offer "Local" and mean anything by it -- from over there,
    # a node beside the viewer and a node on somebody's desk are both
    # http://127.0.0.1:<port>.
    assert registered["role"] == "client"
    session.stop()


def test_a_node_runs_on_this_machine_even_with_nothing_to_serve(
        rig, monkeypatch, tmp_path):
    """The empty laptop node is what makes a Local/Remote toggle possible.

    The file the user is going to pick is chosen from a browser, minutes into
    the session, and nothing on this command line could have named it. So the
    node starts empty and `--dynamic` lets the viewer hand it files later.
    """
    registered = {}
    monkeypatch.setattr(
        connect_mod, "register_node_through",
        lambda base, name, endpoint, token, **kw: registered.update(
            name=name, endpoint=endpoint, token=token, **kw))
    monkeypatch.setattr(connect_mod, "pick_remote_port", lambda randint=None: 41000)
    monkeypatch.setattr(connect_mod, "_free_local_port", lambda: 8642)
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=8642 node_id=cd token=l0cal"]))

    manifest = tmp_path / "manifest.json"
    session = connect_mod.Session("me@host", echo=rig.echo, node_name="study",
                                  node_manifest=str(manifest))
    session.establish()

    node_argv = rig.spawned[0]
    assert "--serve" not in node_argv
    assert "--dynamic" in node_argv
    # A stable id per saved connection, because it is what names the manifest
    # -- and the manifest is how a project reopened next week finds the files
    # that were shared from here last time.
    assert node_argv[node_argv.index("--node-id") + 1] == "connect-study-local"
    assert node_argv[node_argv.index("--manifest") + 1] == str(manifest)
    assert registered["role"] == "client"
    session.stop()


def test_the_local_node_is_told_the_origin_the_browser_will_use(
        rig, monkeypatch, tmp_path):
    """Without this every tile takes the long way round.

    The browser probes a node directly and falls back to proxying through the
    viewer when the probe fails. For a node on THIS machine, proxying means
    laptop -> cluster -> back down the reverse tunnel -> laptop -> cluster ->
    browser, per tile. The only thing standing between those two outcomes is a
    CORS header the node emits for origins it was told about -- and this end is
    the only end that knows what the browser's origin will be.
    """
    monkeypatch.setattr(connect_mod, "register_node_through",
                        lambda *a, **kw: None)
    monkeypatch.setattr(connect_mod, "pick_remote_port", lambda randint=None: 41000)
    monkeypatch.setattr(connect_mod, "_free_local_port", lambda: 8642)
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=8642 node_id=cd token=l0cal"]))

    session = connect_mod.Session("me@host", echo=rig.echo,
                                  node_manifest=str(tmp_path / "m.json"))
    session.establish()

    node_argv = rig.spawned[0]
    assert node_argv[node_argv.index("--allow-origin") + 1] == \
        "http://127.0.0.1:9999"
    session.stop()


def test_a_node_beside_the_viewer_is_told_the_same_origin(rig, monkeypatch):
    """The same fix, for the layout where the node is on the cluster: the
    browser reaches it through a forward on this machine and its Origin header
    still says this machine."""
    monkeypatch.setattr(connect_mod, "register_node_through",
                        lambda *a, **kw: None)
    monkeypatch.setattr(connect_mod, "pick_remote_port", lambda randint=None: 41000)
    monkeypatch.setattr(connect_mod, "_free_local_port", lambda: 41001)
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=c42 port=41000 node_id=ab token=s3cr3t"]))

    session = connect_mod.Session(
        "me@host", echo=rig.echo, local_node=False,
        also_serve=["table:cells=/scratch/c.h5ad"])
    session.establish()

    launched = " ".join(rig.spawned[0])
    assert "--node-allow-origin http://127.0.0.1:9999" in launched
    session.stop()


def test_no_local_node_leaves_this_machine_out_of_it(rig):
    """For somebody who wants the tunnel and nothing else."""
    session = connect_mod.Session("me@host", echo=rig.echo, local_node=False)
    session.establish()

    assert len(rig.spawned) == 1
    assert "node" not in rig.spawned[0]
    # And no reverse forward, since there is nothing on this end to reach.
    assert "-R" not in rig.spawned[0]
    session.stop()


def test_asking_to_share_a_file_overrides_no_local_node(rig, tmp_path):
    """`--local-serve` names files on this machine, so switching the node off
    would silently drop what the user explicitly asked to share."""
    table = tmp_path / "cells.h5ad"
    table.write_bytes(b"")

    session = connect_mod.Session(
        "me@host", echo=rig.echo, local_node=False,
        node_manifest=str(tmp_path / "m.json"),
        local_serve=[f"table:cells={table}"])

    assert session.local_node is True


def test_a_forward_into_a_compute_node_names_bind_node_when_it_says_nothing(rig):
    """A cluster that refuses ssh into a compute node does not refuse it -- it
    drops it, and the forward opens, and nothing ever comes back. Fifteen
    minutes later the only message must name the thing that fixes it."""
    rig.healthy = False
    rig.queue = [FakeProcess(["[plexora-remote] node=compute-b-16 port=9999"])]

    session = connect_mod.Session("me@host", echo=rig.echo, srun="-p short",
                                  timeout=0.1, local_node=False)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    assert "--bind-node" in str(caught.value)


def test_a_direct_connection_is_not_told_to_try_bind_node(rig):
    """--bind-node is meaningless without a job to forward into, and advice
    that cannot apply is worse than none: it sends someone off to change a
    setting that was never the reason."""
    rig.healthy = False
    rig.queue = [FakeProcess([])]

    session = connect_mod.Session("me@host", echo=rig.echo, timeout=0.1,
                                  local_node=False)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    assert "--bind-node" not in str(caught.value)


def test_ssh_s_own_account_of_a_dead_forward_is_quoted_with_the_way_out(rig):
    """`channel open failed` is the only direct evidence this timeout ever
    has, so it is quoted -- and the way out of a silently dropped hop is the
    OTHER forwarding mode, so the message names it."""
    rig.healthy = False
    rig.queue = [
        FakeProcess(["[plexora-remote] node=compute-b-16 port=9999"]),
        FakeProcess(["channel 2: open failed: connect failed: "
                     "Connection timed out"]),
    ]

    session = connect_mod.Session("me@host", echo=rig.echo, srun="-p short",
                                  timeout=0.3, local_node=False)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    message = str(caught.value)
    assert "Connection timed out" in message
    assert "--bind-node" in message and "ON" in message


def test_a_bind_node_timeout_points_back_at_the_default_mode(rig):
    """The cluster that broke --bind-node allowed the very same connection
    from a shell, so no probe run by hand rules this out -- the message has
    to offer the other mode. Observed live: login-node forwards to two
    different compute nodes silently dropped while ssh into them was open."""
    rig.healthy = False
    rig.queue = [FakeProcess(["[plexora-remote] node=compute-a-16 port=9999"])]

    session = connect_mod.Session("me@host", echo=rig.echo, srun="-p short",
                                  bind_node=True, timeout=0.1, local_node=False)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    assert "OFF" in str(caught.value)
    assert "compute node itself" in str(caught.value)


def test_the_failing_health_poll_backs_off(rig, monkeypatch):
    """Every abandoned probe leaves an ssh channel pending on the far side for
    a TCP connect timeout; a fast poll stacks them to ssh's cap, after which
    even a recovered path cannot be seen."""
    rig.healthy = False
    rig.queue = [FakeProcess([])]
    delays = []
    clock = [0.0]
    monkeypatch.setattr(connect_mod, "_now", lambda: clock[0])

    def sleep(seconds):
        delays.append(seconds)
        clock[0] += 10

    monkeypatch.setattr(connect_mod, "_sleep", sleep)

    session = connect_mod.Session("me@host", echo=rig.echo, timeout=200,
                                  local_node=False)
    with pytest.raises(connect_mod.ConnectError):
        session.establish()

    assert delays[0] == 0.5
    assert delays[-1] == connect_mod.HEALTH_POLL_MAX_DELAY
    assert delays == sorted(delays)


def test_the_health_wait_says_it_is_still_waiting(rig, monkeypatch):
    """Silence is the normal appearance of the commonest misconfiguration, so
    the wait has to narrate itself or it cannot be told from a slow start."""
    rig.healthy = False
    rig.queue = [FakeProcess([])]
    clock = [0.0]
    monkeypatch.setattr(connect_mod, "_now", lambda: clock[0])
    monkeypatch.setattr(connect_mod, "_sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + 10))

    session = connect_mod.Session("me@host", echo=rig.echo, timeout=45,
                                  local_node=False)
    with pytest.raises(connect_mod.ConnectError):
        session.establish()

    assert any("still waiting for Plexora" in line for line in rig.echoed)


def test_a_path_that_is_not_there_is_caught_before_the_queue_is(rig, tmp_path):
    """The local node is spawned before ssh but its death is not noticed until
    registration -- which on a cluster is after the job has queued, run and
    allocated. A typo in a path on THIS machine must not cost that wait."""
    session = connect_mod.Session(
        "me@host", echo=rig.echo,
        local_serve=[f"table:cells={tmp_path / 'not-here.h5ad'}"])

    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    assert "not-here.h5ad" in str(caught.value)
    assert rig.spawned == []  # no node, and above all no ssh


def test_a_path_someone_quoted_out_of_habit_still_works(rig, tmp_path):
    """Everywhere else a path with a space in it is typed -- a shell, mostly --
    it has to be quoted. Here it must not be, and the failure that caused read
    as `there is nothing at '<the correct path>'`."""
    table = tmp_path / "a study" / "cells.h5ad"
    table.parent.mkdir()
    table.write_bytes(b"")

    assert connect_mod.missing_local_paths([f"table:cells='{table}'"]) == []
    assert connect_mod.missing_local_paths([f'table:cells="{table}"']) == []
    assert connect_mod.missing_local_paths([f"table:cells={table}"]) == []


def test_the_two_unquoters_agree(tmp_path):
    """connect.py is stdlib-only and cannot import the node's parser, so it
    carries its own copy of the same rule. This is the parity check."""
    from plexora.server.node.resources import parse_serve

    for raw in ["/a/b.h5ad", "'/a/b.h5ad'", '"/a b/c.h5ad"', "  /a/b.h5ad  ",
                "/a/it's.h5ad", "'"]:
        entry = f"table:cells={raw}"
        node_says = parse_serve(entry)[2]
        # The connect-side copy reports what is missing; on a path it agrees is
        # absent, the name it reports is the one the node would have opened.
        assert connect_mod.missing_local_paths([entry]) in ([], [node_says])


def test_a_node_that_never_announces_leaves_the_viewer_working(rig, monkeypatch):
    """A missing layer is not a missing viewer. Refusing to open the images
    that ARE reachable would be the worse of the two failures."""
    monkeypatch.setattr(connect_mod, "pick_remote_port", lambda randint=None: 41000)
    monkeypatch.setattr(connect_mod, "_free_local_port", lambda: 41001)
    monkeypatch.setattr(connect_mod, "_now", lambda: 0.0)
    rig.queue.append(FakeProcess([]))  # says nothing, then closes its pipe

    session = connect_mod.Session(
        "me@host", echo=rig.echo, local_node=False,
        also_serve=["table:cells=/scratch/c.h5ad"])
    session.establish()

    assert session.data_nodes == []
    assert session.node_errors
    assert session.url == "http://127.0.0.1:9999/"
    session.stop()


def test_node_connect_forwards_registers_and_blocks(rig, monkeypatch):
    """Layout two: viewer here, only the pixels over the wire."""
    registered = {}
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=c42 port=9999 node_id=ef token=s3cr3t"]))

    code = connect_mod.connect_node(
        "me@host", ["image:tonsil=/scratch/t.ome.tif"], name="hpc",
        echo=rig.echo,
        register=lambda name, endpoint, token: registered.update(
            name=name, endpoint=endpoint, token=token))

    assert code == 0
    argv = rig.spawned[0]
    assert "9999:127.0.0.1:9999" in argv
    assert "node serve" in argv[-1]
    assert registered == {"name": "hpc", "endpoint": "http://127.0.0.1:9999",
                          "token": "s3cr3t"}


def test_node_connect_names_the_node_after_the_host_by_default(rig):
    rig.queue.append(FakeProcess(
        ["[plexora-node] host=c42 port=9999 node_id=ef token=s3cr3t"]))
    seen = {}
    connect_mod.connect_node("me@hpc.edu", ["image:t=/a.tif"], echo=rig.echo,
                             register=lambda name, *a: seen.update(name=name))
    assert seen["name"] == "hpc.edu-node"


# -- installing Plexora on the far side -----------------------------------
#
# The one thing a connection does that WRITES to somebody else's machine, so
# what is pinned here is both halves of that: which pip runs in which
# environment, and that nothing runs at all unless the profile asked.


@pytest.mark.parametrize(
    ("remote_command", "expected"),
    [
        # The documented default, for a `plexora` already on PATH.
        ("", "pip install --progress-bar off --upgrade plexora"),
        ("plexora", "pip install --progress-bar off --upgrade plexora"),
        # An environment prefix: its own pip, which is what "inside that
        # environment" means with no shell to activate anything in.
        ("/home/you/envs/imaging",
         "/home/you/envs/imaging/bin/pip install --progress-bar off --upgrade plexora"),
        ("/home/you/envs/imaging/",
         "/home/you/envs/imaging/bin/pip install --progress-bar off --upgrade plexora"),
        # The executable rather than the prefix -- same environment, and the
        # pip is one name along from it.
        ("/opt/conda/envs/img/bin/plexora",
         "/opt/conda/envs/img/bin/pip install --progress-bar off --upgrade plexora"),
        # A conda environment by name. `conda run -n X pip install` IS
        # activating X and installing inside it, and it is the form that works
        # over an ssh whose shell has sourced no rc file.
        ("conda run -n imaging plexora",
         "conda run --no-capture-output -n imaging pip install --progress-bar off --upgrade plexora"),
        ("conda run --no-capture-output -n imaging plexora",
         "conda run --no-capture-output -n imaging pip install --progress-bar off --upgrade plexora"),
        ("conda run -p /opt/envs/img plexora",
         "conda run --no-capture-output -p /opt/envs/img pip install "
         "--progress-bar off --upgrade plexora"),
        # Any other shell expression: the environment is however you get to the
        # program, and the program is the last word.
        ("module load python && plexora",
         "module load python && pip install --progress-bar off --upgrade plexora"),
        ("python -m plexora", "python -m pip install --progress-bar off --upgrade plexora"),
        # A wrapper script says nothing about where a Python lives, so PATH is
        # the only honest answer.
        ("/opt/run-plexora.sh", "pip install --progress-bar off --upgrade plexora"),
    ],
)
def test_the_install_runs_in_the_environment_the_launch_command_names(
        remote_command, expected):
    assert connect_mod.install_command_line(remote_command) == expected


def test_conda_run_is_told_to_stream_so_a_long_pip_is_watchable():
    """Without it conda buffers the child's output and prints it at the end,
    which for a six-minute install means six minutes of nothing."""
    line = connect_mod.install_command_line("conda run -n img plexora")
    assert line.startswith("conda run --no-capture-output -n img ")
    # Not twice, for somebody who already wrote it.
    assert line.count("--no-capture-output") == 1


@pytest.mark.parametrize(
    ("remote_command", "expected"),
    [
        ("", None),
        ("plexora", None),
        ("conda run -n imaging plexora", "imaging"),
        ("conda run -p /opt/envs/img plexora", "img"),
        ("/home/you/miniconda3/envs/imaging", "imaging"),
        ("/home/you/miniconda3/envs/imaging/bin/plexora", "imaging"),
        # Nothing here can name what `module load` did, and inventing a name
        # would be worse than a step that says only "Installing Plexora".
        ("module load python && plexora", None),
    ],
)
def test_the_environment_is_named_only_when_the_command_names_one(
        remote_command, expected):
    assert connect_mod.environment_label(remote_command) == expected


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["Successfully installed numpy-2.1.0 plexora-1.4.2"], "1.4.2"),
        # The commonest outcome for somebody who reconnects every morning, and
        # the one that never prints "Successfully installed" at all.
        (["Requirement already satisfied: plexora in /opt/envs/img/lib (1.3.9)"],
         "1.3.9"),
        (["Collecting plexora", "Downloading plexora-1.4.2-py3-none-any.whl"],
         None),
        ([], None),
    ],
)
def test_the_installed_version_is_read_from_pips_own_output(lines, expected):
    assert connect_mod.parse_installed_version(lines) == expected


def test_the_install_and_the_launch_share_one_ssh_and_one_login(rig):
    """The install rides the launch's own ssh, chained ahead of it. It used
    to be a separate ssh, which was a separate authentication -- and at a
    site that confirms every login on somebody's phone, "connect" buzzed it
    twice. `&&` keeps the old promise: a failed install launches nothing,
    because the shell never reaches the launch."""
    rig.queue = [FakeProcess(
        ["Successfully installed plexora-1.4.2", "PLEXORA_INSTALL_DONE"])]
    session = connect_mod.Session(
        "me@host", echo=rig.echo, local_node=False, install=True,
        remote_command="conda run -n imaging plexora")
    session.establish()

    assert len(rig.spawned) == 1, "the install must not be its own login"
    remote = rig.spawned[0][-1]
    assert remote.startswith(
        "conda run --no-capture-output -n imaging pip install --progress-bar off --upgrade plexora"
        " && echo PLEXORA_INSTALL_DONE && ")
    assert "--remote" in remote.split("PLEXORA_INSTALL_DONE")[1]
    assert session.installed_version == "1.4.2"
    assert any("Plexora 1.4.2 is installed" in line for line in rig.echoed)
    session.stop()


def test_under_a_scheduler_the_install_still_runs_on_the_login_node(rig):
    """The chain puts pip BEFORE `srun` in the same command: the environment
    is on a shared filesystem, and the allocation should not be spent
    watching pip download wheels."""
    rig.queue = [
        FakeProcess(["Requirement already satisfied: plexora (1.4.2)",
                     "PLEXORA_INSTALL_DONE",
                     "[plexora-node] host=c42 port=9999 node_id=ef "
                     "token=s3cr3t hostname=c42"]),
        FakeProcess([]),
    ]
    session = connect_mod.NodeSession("me@host", echo=rig.echo, install=True,
                                      srun="", remote_command="/opt/envs/img")
    session.establish()

    remote = rig.spawned[0][-1]
    pip_at = remote.index("/opt/envs/img/bin/pip install --progress-bar off --upgrade plexora")
    assert remote.index(" srun") > remote.index("PLEXORA_INSTALL_DONE") > pip_at
    session.stop()


def test_the_finished_install_is_not_left_looking_like_a_dead_connection(rig):
    """One process now carries install and launch, so a finished install is
    simply a primary that has moved on -- nothing to clean out of `watchers`,
    and the pip output stays in the connection's own log."""
    rig.queue = [FakeProcess(["Successfully installed plexora-1.4.2",
                              "PLEXORA_INSTALL_DONE"])]
    session = connect_mod.Session("me@host", echo=rig.echo, local_node=False,
                                  install=True)
    session.establish()

    assert [w.label for w in session.watchers] == ["ssh"]
    assert session.alive
    assert any("Successfully installed" in line for line in session.log())
    assert "Successfully installed plexora-1.4.2" in session.install_log
    session.stop()


def test_a_profile_that_did_not_ask_installs_nothing(rig):
    session = connect_mod.Session("me@host", echo=rig.echo, local_node=False)
    session.establish()

    assert len(rig.spawned) == 1
    assert "pip" not in " ".join(rig.spawned[0])
    session.stop()


# -- a connection carried by gcloud, and the mount ahead of it ---------------
#
# One transport and one extra prerequisite. Everything downstream of the argv
# -- the watcher, the matchers, the askpass relay, the teardown -- is the same
# either way, and these are the tests that say so.


GCLOUD = {"vm": "plexora-gcp", "project": "my-project", "zone": "us-east1-b"}


def test_a_google_cloud_session_is_one_process_carried_over_iap():
    """Not `start-iap-tunnel` plus a plain ssh. gcloud owns the OS Login key,
    the login name Google derived from the account, and the host key -- and
    everything after `--` reaches the underlying ssh untouched, which is what
    makes this a drop-in for `direct_ssh_argv`."""
    argv = connect_mod.gcloud_ssh_argv(GCLOUD, 8000, 9000, "run me",
                                       forwards=["7000:7001"],
                                       reverse=[(6000, 6001)])
    assert argv[:3] == ["gcloud", "compute", "ssh"]
    assert argv[3] == "plexora-gcp"
    assert "--tunnel-through-iap" in argv
    assert argv[argv.index("--command") + 1] == "run me"
    after = argv[argv.index("--") + 1:]
    assert after[0] == "-t"
    assert "-L" in after and "8000:127.0.0.1:9000" in after
    assert "7000:127.0.0.1:7001" in after
    assert "6000:127.0.0.1:6001" in after
    assert "ServerAliveInterval=30" in after


def test_a_google_cloud_node_session_carries_one_forward_and_nothing_else():
    argv = connect_mod.gcloud_node_ssh_argv(GCLOUD, 8000, 9000, "run me")
    after = argv[argv.index("--") + 1:]
    assert after.count("-L") == 1
    assert "-R" not in after


def test_the_mount_runs_outside_the_install_and_both_before_the_launch():
    """Order matters and is not arbitrary: the environment pip would write to
    lives on the machine the mount step prepares, so the mount is outermost."""
    line = connect_mod.mount_prefixed(
        "mount it", connect_mod.install_prefixed("pip it", "launch it"))
    assert line == ("mount it && echo PLEXORA_MOUNT_DONE && pip it "
                    "&& echo PLEXORA_INSTALL_DONE && launch it")


def test_the_mount_markers_are_matched_only_on_their_own_line():
    assert connect_mod.parse_mount_done("PLEXORA_MOUNT_DONE ") is True
    assert connect_mod.parse_mount_done("saying PLEXORA_MOUNT_DONE") is None
    assert connect_mod.parse_mount_readonly("PLEXORA_MOUNT_READONLY") is True


def test_a_google_cloud_connection_spawns_gcloud_and_waits_for_the_mount(rig):
    rig.queue = [FakeProcess(["Mounting gs://b…", "PLEXORA_MOUNT_DONE"])]
    session = connect_mod.Session(
        "plexora-gcp", echo=rig.echo, local_node=False, gcloud=GCLOUD,
        mount_command="mount it", remote_command="~/plexora-venv")
    session.establish()

    assert rig.spawned[0][0] == "gcloud"
    remote = rig.spawned[0][rig.spawned[0].index("--command") + 1]
    assert remote.startswith("mount it && echo PLEXORA_MOUNT_DONE && ")
    assert "--remote" in remote.split("PLEXORA_MOUNT_DONE")[1]
    assert session.mount_readonly is False
    assert any("mounted and readable" in line for line in rig.echoed)
    session.stop()


def test_a_read_only_bucket_warns_rather_than_failing_the_connection(rig):
    """Somebody else's published atlas is an ordinary thing to be given, and
    images open from one perfectly well. What breaks is saving a figure into
    it, which is worth a sentence and is not worth refusing a connection."""
    rig.queue = [FakeProcess(["PLEXORA_MOUNT_READONLY", "PLEXORA_MOUNT_DONE"])]
    session = connect_mod.Session("plexora-gcp", echo=rig.echo,
                                  local_node=False, gcloud=GCLOUD,
                                  mount_command="mount it")
    session.establish()

    assert session.mount_readonly is True
    assert session.alive
    assert any("read-only" in line for line in rig.echoed)
    session.stop()


def test_a_mount_that_fails_launches_nothing_and_names_the_iam_fix(rig):
    """`&&` short-circuits, so a bucket the VM cannot see means the viewer was
    never started -- and the fix is a Google IAM binding rather than anything
    in Plexora, so the message is the command that grants it."""
    rig.queue = [FakeProcess(
        ["gcsfuse: 403 does not have storage.objects.list access"],
        dead_with=1)]
    session = connect_mod.Session("plexora-gcp", echo=rig.echo,
                                  local_node=False, gcloud=GCLOUD,
                                  mount_command="mount it")
    with pytest.raises(connect_mod.ConnectError) as raised:
        session.establish()
    assert "add-iam-policy-binding" in str(raised.value)


def test_the_mount_spends_none_of_the_connections_own_budget(rig, monkeypatch):
    """A first mount installs Cloud Storage FUSE and builds a Python
    environment on a machine that booted a minute ago. Timing that out against
    the 60s a viewer gets to answer would abandon a VM at the moment it was
    about to work, having already paid for it."""
    assert connect_mod.MOUNT_TIMEOUT >= 900

    # A clock that advances a hundred seconds per reading, so anything that
    # waits at all has run past a sixty-second budget by the time it is done.
    ticks = iter(range(0, 1_000_000, 100))
    monkeypatch.setattr(connect_mod, "_now", lambda: next(ticks))
    seen = []
    monkeypatch.setattr(
        connect_mod, "_wait_for_health",
        lambda url, deadline, watchers, **kw: seen.append(deadline) or True)

    rig.queue = [FakeProcess(["PLEXORA_MOUNT_DONE"])]
    session = connect_mod.Session("plexora-gcp", echo=rig.echo,
                                  local_node=False, gcloud=GCLOUD,
                                  mount_command="mount it", timeout=60)
    session.establish()

    # The deadline the health wait was given was taken AFTER the mount: the
    # first reading of the clock was 0, so one that had never been reset would
    # still be 60.
    assert seen[0] > 60
    session.stop()


def test_a_google_cloud_profile_on_a_machine_with_no_gcloud_says_which(rig,
                                                                      monkeypatch):
    """A profile saved on a laptop that had the CLI can be opened on one that
    does not, which is exactly when the message has to say which of the two
    machines is the problem."""
    monkeypatch.setattr(connect_mod, "_which",
                        lambda name: None if name == "gcloud" else "/usr/bin/ssh")
    session = connect_mod.Session("plexora-gcp", echo=rig.echo,
                                  local_node=False, gcloud=GCLOUD)
    with pytest.raises(connect_mod.ConnectError) as raised:
        session.establish()
    assert "cloud.google.com/sdk" in str(raised.value)
    assert rig.spawned == []


def test_a_profile_with_no_gcloud_record_still_uses_plain_ssh(rig):
    session = connect_mod.Session("me@host", echo=rig.echo, local_node=False)
    session.establish()

    assert rig.spawned[0][0] == "ssh"
    assert "PLEXORA_MOUNT_DONE" not in " ".join(rig.spawned[0])
    session.stop()


def test_a_failed_install_stops_the_connection_and_names_the_fix(rig):
    """`&&` short-circuits: pip fails, the marker never prints, the ssh ends,
    and the copy the upgrade was meant to replace never starts."""
    rig.queue = [FakeProcess(
        ["bash: pip: command not found"], dead_with=127)]

    session = connect_mod.Session("me@host", echo=rig.echo, local_node=False,
                                  install=True)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    message = str(caught.value)
    assert "pip exited 127" in message
    assert "command not found" in message
    assert "conda env list" in message or "Plexora command or environment" in message


def test_an_unwritable_environment_is_told_apart_from_a_missing_pip(rig):
    rig.queue = [FakeProcess(
        ["ERROR: Could not install packages due to an OSError: "
         "[Errno 13] Permission denied: '/usr/lib/python3.11/site-packages'"],
        dead_with=1)]

    session = connect_mod.Session("me@host", echo=rig.echo, local_node=False,
                                  install=True)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    assert "site-managed install" in str(caught.value)


def test_a_node_session_installs_too_and_before_its_own_clock_starts(rig):
    """A node runs the same Plexora a viewer would, so "keep it current over
    there" has to mean the same thing whichever half is being opened -- and
    the node's answer-time budget starts at the marker, not at the login."""
    rig.queue = [FakeProcess(
        ["Successfully installed plexora-1.4.2", "PLEXORA_INSTALL_DONE",
         "[plexora-node] host=c42 port=9999 node_id=ef token=s3cr3t"])]
    session = connect_mod.NodeSession("me@host", echo=rig.echo, install=True,
                                      remote_command="/opt/envs/img")
    session.establish()

    assert len(rig.spawned) == 1
    remote = rig.spawned[0][-1]
    assert remote.startswith(
        "/opt/envs/img/bin/pip install --progress-bar off --upgrade plexora && echo PLEXORA_INSTALL_DONE && ")
    assert "node serve" in remote
    assert [w.label for w in session.watchers] == ["node"]
    assert session.installed_version == "1.4.2"
    session.stop()


def test_the_install_reports_its_progress_as_a_phase(rig):
    seen = []
    rig.queue = [FakeProcess(["Successfully installed plexora-1.4.2",
                              "PLEXORA_INSTALL_DONE"])]
    session = connect_mod.Session("me@host", echo=rig.echo, local_node=False,
                                  install=True, on_phase=seen.append)
    session.establish()

    assert seen[0] == "installing"
    assert seen.index("installing") < seen.index("waiting_for_app")
    session.stop()
