import os
import socket
import sys
import types
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "plexora" / "cli.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("plexora_cli_under_test", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()


def test_pyproject_exposes_friendly_console_entry_point():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
        import tomli as tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    assert config["project"]["scripts"]["plexora"] == "plexora.cli:main"
    assert config["project"]["scripts"]["plexora-server"] == "plexora.server_cli:main"


def test_browser_url_targets_localhost_when_binding_all_interfaces():
    assert cli.browser_url("0.0.0.0", "8000") == "http://127.0.0.1:8000/"


def test_browser_url_can_open_a_datasource_under_a_base_url():
    assert (
        cli.browser_url("127.0.0.1", 9000, "/proxy/9000/", "my dataset")
        == "http://127.0.0.1:9000/proxy/9000/my%20dataset"
    )


@pytest.mark.parametrize(
    ("env", "system", "expected"),
    [
        ({}, "Linux", False),
        ({"DISPLAY": ":0"}, "Linux", True),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "Linux", True),
        ({"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 55555", "DISPLAY": ":0"}, "Linux", False),
        ({"SLURM_JOB_ID": "123"}, "Linux", False),
        ({}, "Windows", True),
        ({}, "Darwin", True),
    ],
)
def test_should_open_browser_auto_detects_interactive_desktop(env, system, expected):
    assert cli.should_open_browser(env=env, system=system) is expected


def test_browser_flags_override_environment_detection():
    assert cli.should_open_browser(env={"SLURM_JOB_ID": "123"}, system="Linux", preference="yes")
    assert not cli.should_open_browser(env={"DISPLAY": ":0"}, system="Linux", preference="no")


def test_open_browser_waits_on_health_url_but_opens_requested_url():
    waited = []
    opened = []

    cli._open_browser_when_ready(
        "http://127.0.0.1:8000/demo",
        "http://127.0.0.1:8000/",
        wait_fn=lambda url, token=None: waited.append(url) or True,
        open_fn=opened.append,
    )

    assert waited == ["http://127.0.0.1:8000/"]
    assert opened == ["http://127.0.0.1:8000/demo"]


def _fake_serve(monkeypatch, served):
    fake_waitress = types.SimpleNamespace(
        serve=lambda app, **kwargs: served.update({"app": app, **kwargs})
    )
    fake_plexora = types.SimpleNamespace(
        # A real config mapping, because main() writes the base URL and the
        # auth token onto it after the import -- create_app() has already run
        # by the time main() sees an argument, so the environment alone would
        # be too late.
        app=types.SimpleNamespace(config={}),
        paths=types.SimpleNamespace(first_run_notice=lambda: None),
        _clean_base_url=lambda base_url: "" if not base_url else "/" + str(base_url).strip("/"),
    )
    monkeypatch.setitem(sys.modules, "waitress", fake_waitress)
    monkeypatch.setitem(sys.modules, "plexora", fake_plexora)
    monkeypatch.setattr(cli, "should_open_browser", lambda **kwargs: False)


def test_main_leaves_the_data_path_for_the_resolver_and_serves(monkeypatch):
    """No --data-dir means the CLI decides nothing.

    It used to write a default into PLEXORA_DATA_PATH here, because every
    module bound the root at import and somebody had to get in first. That made
    `plexora` the only entry point that reached the right directory --
    `plexora-server`, `run.py` and a plain `import plexora` all fell through to
    a CWD-relative path. Resolution is plexora.paths' job now, so the CLI must
    leave the variable alone or it would pin the answer for the sidecar and the
    notebook too.
    """
    served = {}
    _fake_serve(monkeypatch, served)
    monkeypatch.delenv("PLEXORA_DATA_PATH", raising=False)

    cli.main(["--no-browser", "--port", "8765"])

    assert "PLEXORA_DATA_PATH" not in os.environ
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8765


def test_main_pins_the_data_path_when_asked(monkeypatch, tmp_path):
    """--data-dir is still an explicit instruction, and still wins."""
    served = {}
    _fake_serve(monkeypatch, served)
    monkeypatch.delenv("PLEXORA_DATA_PATH", raising=False)

    cli.main(["--no-browser", "--data-dir", str(tmp_path)])

    assert os.environ["PLEXORA_DATA_PATH"] == str(tmp_path)


# -- argv shapes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], (None, [])),
        (["tonsil"], (None, ["tonsil"])),
        (["where"], ("where", [])),
        (["config", "set", "data-dir", "/x"], ("config", ["set", "data-dir", "/x"])),
        (["connect", "me@host"], ("connect", ["me@host"])),
        # Only argv[0] counts, so a flag value that happens to spell a
        # subcommand is left alone.
        (["--base-url", "config"], (None, ["--base-url", "config"])),
    ],
)
def test_split_command_recognises_subcommands_only_in_front(argv, expected):
    assert cli.split_command(argv) == expected


def test_a_bare_datasource_still_parses():
    """The regression that adding subcommands introduced.

    An argparse subparsers action is a positional, so sharing a parser with the
    optional `datasource` positional made `plexora tonsil` die with "invalid
    choice: 'tonsil'" -- the single most basic invocation the command has.
    """
    command, rest = cli.split_command(["tonsil"])
    args = cli.build_parser(command).parse_args(rest)
    assert args.datasource == "tonsil"


def test_connect_keeps_its_own_datasource():
    """The other half of the same bug: when a subparser DID match, the parent's
    trailing positional then ran with nothing left and reset datasource to
    None, throwing away what had just been parsed."""
    command, rest = cli.split_command(["connect", "me@host", "tonsil"])
    args = cli.build_parser(command).parse_args(rest)
    assert (args.target, args.datasource) == ("me@host", "tonsil")


def test_version_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0


def test_host_defaults_to_the_environment_override(monkeypatch):
    monkeypatch.setenv("PLEXORA_HOST", "0.0.0.0")
    assert cli.build_parser().parse_args([]).host == "0.0.0.0"


# -- port selection ------------------------------------------------------


@pytest.fixture
def taken_port():
    """A port that really is bound for the duration of the test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def test_a_free_port_is_used_as_asked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert cli._resolve_port("127.0.0.1", free, explicit=True) == free


def test_an_explicit_busy_port_is_an_error_naming_it(taken_port):
    with pytest.raises(SystemExit) as excinfo:
        cli._resolve_port("127.0.0.1", taken_port, explicit=True)
    assert str(taken_port) in str(excinfo.value)


def test_an_unrequested_busy_port_moves_aside_and_says_so(taken_port):
    said = []
    chosen = cli._resolve_port("127.0.0.1", taken_port, explicit=False, log=said.append)
    assert chosen != taken_port
    assert said and str(chosen) in said[0]


def test_port_zero_means_pick_one():
    assert cli._resolve_port("127.0.0.1", 0, explicit=True) > 0


def test_main_moves_off_a_busy_default_port(monkeypatch, taken_port):
    served = {}
    _fake_serve(monkeypatch, served)
    monkeypatch.setattr(cli, "DEFAULT_PORT", taken_port)

    cli.main(["--no-browser"])

    assert served["port"] != taken_port


# -- the `python -m plexora --plugins` re-exec ---------------------------


def _reexec(argv, modules, environ):
    calls = []
    result = cli.maybe_reexec_for_plugins(
        argv, modules=modules, environ=environ, relaunch=calls.append
    )
    return result, calls


def test_plugins_relaunches_once_through_a_program_that_sets_the_variable_first():
    environ = {}
    result, calls = _reexec(["--plugins", "gating"], {"plexora": object()}, environ)

    assert result is True
    assert environ[cli.REEXEC_ENV_VAR] == "1"
    assert calls[0][1] == "-c"
    assert calls[0][3:] == ["--plugins", "gating"]
    # The env write must come before anything that could import the package.
    program = calls[0][2]
    assert program.index("PLEXORA_PLUGINS") < program.index("plexora.cli")


def test_the_relaunch_target_has_not_imported_the_package():
    """`-c`, not `-m plexora` and not the console script. Both of those import
    the package to reach their entry point, which is the trap this exists to
    escape -- Blueprints are registered during that import."""
    _result, calls = _reexec(["--plugins", "x"], {"plexora": object()}, {})
    assert "-m" not in calls[0]


@pytest.mark.parametrize("value", ["", "gating", "a,b", "it's", 'say "hi"', "back\\slash"])
def test_the_bootstrap_embeds_any_value_as_a_safe_literal(value):
    """`--plugins ""` cannot travel in the child's environment -- on Windows
    setting a variable to "" DELETES it, so the child would read "unset", which
    means activate EVERYTHING, the exact opposite of a core-only build. So it
    travels as a Python literal instead, which must survive whatever is in it
    and must not be able to break out of it."""
    import ast

    program = cli.bootstrap_program(value)
    literal = program.split("os.environ['PLEXORA_PLUGINS'] = ", 1)[1].split("; from", 1)[0]

    assert ast.literal_eval(literal) == value


def test_connect_does_not_relaunch_for_its_remote_plugins_flag(monkeypatch):
    """`plexora connect host --plugins gating` names plugins for the REMOTE
    host; this process's own Blueprints are beside the point."""
    calls = []
    monkeypatch.setattr(cli, "maybe_reexec_for_plugins",
                        lambda argv: calls.append(argv) or True)
    monkeypatch.setattr(cli, "_run_connect", lambda args: 0)

    assert cli.main(["connect", "me@host", "--plugins", "gating"]) == 0
    assert calls == []


def test_the_console_script_never_relaunches():
    """`plexora` has not imported the package when main() starts, so setting
    the variable in-process is still in time."""
    result, calls = _reexec(["--plugins", "gating"], {}, {})
    assert result is False and calls == []


def test_a_relaunched_child_does_not_relaunch_again():
    environ = {cli.REEXEC_ENV_VAR: "1"}
    result, calls = _reexec(["--plugins", "gating"], {"plexora": object()}, environ)
    assert result is False and calls == []


def test_an_environment_that_already_agrees_is_left_alone():
    environ = {"PLEXORA_PLUGINS": "gating"}
    result, calls = _reexec(["--plugins", "gating"], {"plexora": object()}, environ)
    assert result is False and calls == []


def test_no_plugins_flag_means_nothing_to_do():
    result, calls = _reexec(["--port", "9000"], {"plexora": object()}, {})
    assert result is False and calls == []


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--plugins", "a,b"], "a,b"),
        (["--plugins=a,b"], "a,b"),
        (["--plugins="], ""),
        (["--port", "1"], None),
        (["--plugins"], None),
    ],
)
def test_plugins_argument_scanner(argv, expected):
    assert cli._plugins_argument(argv) == expected


# -- --remote instructions -----------------------------------------------


def test_remote_prints_a_single_hop_tunnel_off_a_cluster():
    lines = cli.remote_instructions("aj", "workstation.lab.edu", 8000)
    text = "\n".join(lines)

    assert "ssh -N -L 8000:127.0.0.1:8000 aj@workstation.lab.edu" in text
    assert "http://localhost:8000/" in text
    assert "-J" not in text


def test_remote_inside_a_slurm_job_prints_the_two_hop_form():
    """The O2 case. A single -L to the compute node cannot work from a laptop
    -- only the login node accepts connections from outside."""
    _scheduler, node, login = cli.scheduler_topology(
        env={
            "SLURM_JOB_ID": "4242",
            "SLURMD_NODENAME": "compute-a-16",
            "SLURM_SUBMIT_HOST": "login01.o2.hms.harvard.edu",
        }
    )
    text = "\n".join(
        cli.remote_instructions("aj", "compute-a-16", 8123, node=node, login_host=login)
    )

    assert ("ssh -N -J aj@login01.o2.hms.harvard.edu aj@compute-a-16 "
            "-L 8123:127.0.0.1:8123") in text
    assert "http://localhost:8123/" in text


def test_bind_node_prints_a_login_node_forward_and_warns():
    text = "\n".join(
        cli.remote_instructions("aj", "compute-a-16", 8123, node="compute-a-16",
                                login_host="login01", bind_node=True)
    )

    assert "ssh -N -L 8123:compute-a-16:8123 aj@login01" in text
    assert "0.0.0.0:8123" in text
    assert "internal network" in text


def test_an_unknown_login_host_becomes_a_placeholder_to_replace():
    text = "\n".join(
        cli.remote_instructions("aj", "compute-a-16", 8123, node="compute-a-16")
    )
    assert "<login-host>" in text
    assert "--login-host" in text


def test_every_shape_emits_the_machine_readable_line():
    """`plexora connect --srun` has no other way to learn which node the
    scheduler picked."""
    plain = cli.remote_instructions("aj", "host.edu", 8000)
    job = cli.remote_instructions("aj", "host.edu", 8000, node="compute-a-16")

    assert plain[0] == "[plexora-remote] node=host.edu port=8000"
    assert job[0] == "[plexora-remote] node=compute-a-16 port=8000"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, (None, None, None)),
        ({"SLURM_JOB_ID": "1", "SLURMD_NODENAME": "n1", "SLURM_SUBMIT_HOST": "l1"},
         ("slurm", "n1", "l1")),
        ({"SLURM_JOB_ID": "1", "SLURMD_NODENAME": "n1"}, ("slurm", "n1", None)),
        ({"PBS_JOBID": "1", "PBS_O_HOST": "l1"}, ("pbs", "hostfallback", "l1")),
        ({"LSB_JOBID": "1", "LSB_HOSTS": "n1 n2", "LSB_SUB_HOST": "l1"},
         ("lsf", "n1", "l1")),
    ],
)
def test_scheduler_topology(env, expected):
    assert cli.scheduler_topology(env=env, hostname="hostfallback") == expected


def test_remote_does_not_open_a_browser_but_still_prints_instructions(monkeypatch, capsys):
    served = {}
    _fake_serve(monkeypatch, served)
    # The real one would answer "no" on this machine anyway; forcing "yes"
    # proves --remote is what suppresses it.
    monkeypatch.setattr(cli, "should_open_browser",
                        lambda **kwargs: kwargs.get("preference") == "yes")
    opened = []
    monkeypatch.setattr(cli, "_schedule_browser_open",
                        lambda url, health, token=None: opened.append(url))

    cli.main(["--remote", "--port", "0"])

    assert opened == []
    assert "[plexora-remote]" in capsys.readouterr().out


def test_remote_with_an_explicit_browser_flag_still_opens(monkeypatch):
    served = {}
    _fake_serve(monkeypatch, served)
    monkeypatch.setattr(cli, "should_open_browser",
                        lambda **kwargs: kwargs.get("preference") == "yes")
    opened = []
    monkeypatch.setattr(cli, "_schedule_browser_open",
                        lambda url, health, token=None: opened.append(url))

    cli.main(["--remote", "--browser", "--port", "0"])

    assert opened


def test_bind_node_actually_binds_every_interface(monkeypatch):
    served = {}
    _fake_serve(monkeypatch, served)

    cli.main(["--remote", "--bind-node", "--port", "0"])

    assert served["host"] == "0.0.0.0"


# -- --ood ----------------------------------------------------------------


def test_ood_instructions_name_the_stripping_door_and_the_token():
    """`/node/` forwards the path unstripped and is right for Jupyter, which
    mounts under it; Plexora serves at root, so it needs the door that strips."""
    text = "\n".join(
        cli.ood_instructions("compute-a-16", 8123, "tok123",
                             cli.ood_mount("compute-a-16", 8123), "tonsil")
    )

    assert "https://<your-OnDemand-host>/rnode/compute-a-16/8123/tonsil?token=tok123" in text
    assert "0.0.0.0:8123" in text
    assert "internal network" in text


def test_ood_instructions_say_which_placeholder_to_replace():
    """The portal's public hostname is genuinely unknowable from a compute
    node, and a guess would fail in a way nobody could debug."""
    text = "\n".join(
        cli.ood_instructions("compute-a-16", 8123, "tok", "/rnode/compute-a-16/8123")
    )

    assert "/rnode/compute-a-16/8123/?token=tok" in text
    assert "Replace <your-OnDemand-host>" in text


def _inside_a_job(monkeypatch, node="compute-a-16"):
    """Pretend to be in a scheduler job, and contain the blast radius.

    main() writes the token and the mount path straight into `os.environ` --
    which is right, it is about to hand them to a server -- so without claiming
    both variables here first they outlive the test. They are read by every
    subprocess the rest of the suite starts, and a stray PLEXORA_BASE_URL
    prefixes every route in tests/test_plugin_boundary.py's inventory.
    """
    monkeypatch.setattr(
        cli, "scheduler_topology",
        lambda env=None, hostname=None: (("slurm", node, "l1") if node
                                         else (None, None, None)))
    monkeypatch.setenv("PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setenv("PLEXORA_BASE_URL", "")


def test_ood_binds_every_interface_and_mounts_under_the_port_it_got(monkeypatch):
    """Composed after port resolution, because the mount path has to name the
    port actually taken rather than the one asked for."""
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch)

    cli.main(["--ood", "--port", "0"])

    assert served["host"] == "0.0.0.0"
    assert sys.modules["plexora"].app.config["PLEXORA_BASE_URL"] == (
        f"/rnode/compute-a-16/{served['port']}"
    )


def test_ood_protects_the_open_port_with_a_token(monkeypatch, capsys):
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch)

    cli.main(["--ood", "--port", "0"])

    token = os.environ["PLEXORA_AUTH_TOKEN"]
    assert token
    # Config as well as environment: create_app() ran during `import plexora`,
    # which for the console script is before main() reads a single argument.
    assert sys.modules["plexora"].app.config["PLEXORA_AUTH_TOKEN"] == token
    assert f"?token={token}" in capsys.readouterr().out


def test_ood_falls_back_to_this_hostname_off_a_scheduler(monkeypatch):
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch, node=None)
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "some-node")

    cli.main(["--ood", "--port", "0"])

    assert sys.modules["plexora"].app.config["PLEXORA_BASE_URL"].startswith(
        "/rnode/some-node/"
    )


def test_an_explicit_base_url_wins_over_the_composed_one(monkeypatch):
    """The escape hatch for a site whose portal spells this door differently."""
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch)

    cli.main(["--ood", "--port", "0", "--base-url", "/proxied/here"])

    assert sys.modules["plexora"].app.config["PLEXORA_BASE_URL"] == "/proxied/here"


def test_ood_does_not_open_a_browser_on_a_headless_node(monkeypatch):
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch)
    monkeypatch.setattr(cli, "should_open_browser",
                        lambda **kwargs: kwargs.get("preference") == "yes")
    opened = []
    monkeypatch.setattr(cli, "_schedule_browser_open",
                        lambda url, health, token=None: opened.append(url))

    cli.main(["--ood", "--port", "0"])

    assert opened == []


def test_an_explicit_browser_on_the_node_opens_the_bare_address(monkeypatch):
    """The mount path exists only on the portal's side of the proxy, so a
    browser on this node has to ask for the root -- with the token, since the
    guard exempts nothing, including the health probe it waits on."""
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch)
    monkeypatch.setattr(cli, "should_open_browser",
                        lambda **kwargs: kwargs.get("preference") == "yes")
    opened = []
    monkeypatch.setattr(cli, "_schedule_browser_open",
                        lambda url, health, token=None: opened.append((url, health, token)))

    cli.main(["--ood", "--browser", "--port", "0", "tonsil"])

    url, health, token = opened[0]
    port = served["port"]
    assert url == f"http://127.0.0.1:{port}/tonsil?token={token}"
    assert health == f"http://127.0.0.1:{port}/"
    assert token == os.environ["PLEXORA_AUTH_TOKEN"]


def test_ood_and_remote_are_refused_together(monkeypatch):
    """They answer different questions -- a portal URL and an SSH tunnel -- and
    printing both would be two contradictory sets of directions."""
    served = {}
    _fake_serve(monkeypatch, served)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--ood", "--remote", "--port", "0"])

    assert "--ood" in str(excinfo.value)
    assert served == {}
