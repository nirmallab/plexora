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


# -- config set ----------------------------------------------------------


def _config_args(key, value):
    return types.SimpleNamespace(config_command="set", key=key, value=value)


def _settings_at(monkeypatch, tmp_path):
    from plexora import paths

    path = tmp_path / "settings.json"
    monkeypatch.setattr(paths, "settings_path", lambda: path)
    return paths, path


def test_mask_output_is_recorded(monkeypatch, tmp_path):
    paths, _ = _settings_at(monkeypatch, tmp_path)

    assert cli._run_config(_config_args("mask-output", "project")) == 0
    assert paths.read_settings()["mask_output"] == "project"
    assert paths.mask_output_preference() == "project"


def test_a_misspelt_mask_output_is_refused_before_anything_is_written(monkeypatch,
                                                                     tmp_path):
    """The value is one of two words, so a typo is the likely mistake -- and a
    settings file holding one would read back as the default, leaving somebody
    looking at a recorded setting that does nothing."""
    paths, path = _settings_at(monkeypatch, tmp_path)

    assert cli._run_config(_config_args("mask-output", "beside-the-mask")) == 2
    assert not path.exists()
    assert paths.mask_output_preference() == "beside"


def test_mask_output_is_beside_until_it_is_set(monkeypatch, tmp_path):
    paths, _ = _settings_at(monkeypatch, tmp_path)
    monkeypatch.delenv(paths.ENV_MASK_OUTPUT, raising=False)

    assert paths.mask_output_preference() == "beside"


def test_config_set_takes_only_the_keys_it_knows():
    parser = cli.build_parser("config")
    with pytest.raises(SystemExit):
        parser.parse_args(["set", "mask-outputs", "project"])
    assert parser.parse_args(["set", "mask-output", "beside"]).key == "mask-output"


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


# -- working out where we are --------------------------------------------
#
# The bare `plexora` command used to print a localhost URL wherever it ran,
# which on a hub or a cluster is not a bad URL so much as a false one -- it
# names the user's own laptop, where nothing is listening. These pin the
# ladder that fills in the flags they would otherwise have had to know.


def _resolved(kind, server_base="", display="", bind_host="127.0.0.1"):
    """A stand-in for notebook_env.Resolved, so no Jupyter is needed here."""
    return types.SimpleNamespace(
        kind=kind, server_base=server_base, display=display or server_base,
        bind_host=bind_host,
    )


def test_detection_helpers_match_notebook_env():
    """Two copies of one list, kept honest.

    cli.py has to load without the plexora package (see _clean_base_url), so it
    cannot import the module that owns these. A drift would mean the CLI and
    `plexora.view()` disagreed about what "remote" means.
    """
    from plexora import notebook_env

    assert cli.REMOTE_ENV_VARS == notebook_env.REMOTE_ENV_VARS
    assert cli.PORT_PLACEHOLDER == notebook_env.PORT_PLACEHOLDER
    assert cli.OOD_MOUNT.strip("/") == "rnode"


def test_detection_runs_for_a_bare_invocation():
    assert cli.should_detect([], env={}) is True
    assert cli.should_detect(["tonsil", "--port", "9000"], env={}) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--ood"],
        ["--remote"],
        ["-r"],
        ["--bind-node"],
        ["--base-url", "/proxied"],
        ["--base-url=/proxied"],
        ["--host", "0.0.0.0"],
        ["--host=0.0.0.0"],
        ["--login-host", "l1"],
    ],
)
def test_a_flag_that_answers_the_question_turns_detection_off(argv):
    assert cli.should_detect(argv, env={}) is False


def test_the_docker_host_variable_turns_detection_off():
    """The image sets PLEXORA_HOST=0.0.0.0 and means it. A container that
    started proxying itself under a Jupyter prefix it happened to find inside
    itself would be a mystifying regression."""
    assert cli.should_detect([], env={"PLEXORA_HOST": "0.0.0.0"}) is False


def test_no_detect_turns_detection_off():
    assert cli.should_detect([], env={}, disabled=True) is False


def test_detection_that_raises_is_learning_nothing_not_a_crash():
    """Every failure in here has to end in a working local viewer."""
    def explode(echo=print):
        raise RuntimeError("no jupyter_server, half-installed environment")

    assert cli.detect_environment(explode) is None


def _args():
    return types.SimpleNamespace(ood=False, remote=False, base_url=None)


def test_open_ondemand_is_applied_as_the_ood_flag():
    args = _args()
    assert cli.apply_detection(args, _resolved("ood", "/rnode/c42/{port}")) == "ood"
    assert args.ood is True
    assert args.remote is False


def test_a_hub_prefix_is_applied_as_a_proxy_mount():
    args = _args()
    assert cli.apply_detection(args, _resolved("proxy", "/user/me/proxy/{port}")) == "proxy"
    # Nothing is written yet: the mount has to name the port that gets taken.
    assert args.base_url is None
    assert args.ood is False


def test_colab_is_recognised_but_not_configured():
    """Only the notebook frontend knows Colab's proxy origin, and a shell has
    no frontend to ask."""
    args = _args()
    assert cli.apply_detection(args, _resolved("colab", "")) == "colab"
    assert args.base_url is None
    assert args.ood is False
    assert args.remote is False


def test_a_plain_machine_reached_over_ssh_becomes_remote():
    args = _args()
    assert cli.apply_detection(args, _resolved("direct"), remote_env=True) == "remote"
    assert args.remote is True


def test_a_plain_local_machine_is_left_entirely_alone():
    args = _args()
    assert cli.apply_detection(args, _resolved("direct"), remote_env=False) is None
    assert (args.ood, args.remote, args.base_url) == (False, False, None)


def test_the_mount_names_the_port_that_was_actually_taken():
    assert cli.detected_base_url(_resolved("proxy", "/user/me/proxy/{port}"),
                                 8123) == "/user/me/proxy/8123"


def test_the_hub_prefix_is_recoverable_from_the_mount():
    assert cli.jupyter_prefix_from_mount("/user/me/proxy/8123") == "/user/me/"
    # A named server called "proxy" is legal; only the last occurrence is ours.
    assert cli.jupyter_prefix_from_mount("/user/me/proxy/proxy/8123") == "/user/me/proxy/"
    assert cli.jupyter_prefix_from_mount("/rnode/c42/8123") is None


def test_the_ood_node_comes_from_the_prefix_the_portal_routes():
    """Not from the scheduler: OOD put that host spelling into the notebook's
    own prefix, and a node whose $SLURMD_NODENAME differs from it would produce
    a URL the portal cannot map."""
    assert cli.ood_node_from_mount("/rnode/compute-42.cluster/8123") == "compute-42.cluster"
    assert cli.ood_node_from_mount("/user/me/proxy/8123") is None
    assert cli.ood_node_from_mount("") is None


def test_hub_instructions_print_a_path_and_never_guess_the_host():
    lines = cli.hub_instructions("/user/me/proxy/8123", "tonsil")
    assert "  /user/me/proxy/8123/tonsil" in lines
    assert any("jupyter-server-proxy" in line for line in lines)
    assert not any("127.0.0.1" in line for line in lines)


def test_hub_instructions_use_a_known_origin_when_there_is_one():
    lines = cli.hub_instructions("/user/me/proxy/8123", None,
                                 origin="https://hub.example.edu/")
    assert "  https://hub.example.edu/user/me/proxy/8123/" in lines


def _detects(monkeypatch, resolved):
    monkeypatch.setattr(cli, "detect_environment", lambda *a, **k: resolved)


def test_a_detected_hub_serves_on_loopback_under_the_proxy_mount(monkeypatch):
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch, node=None)
    _detects(monkeypatch, _resolved("proxy", "/user/me/proxy/{port}"))

    cli.main(["--port", "8765"])

    assert served["host"] == "127.0.0.1"
    assert os.environ["PLEXORA_BASE_URL"] == "/user/me/proxy/8765"
    assert sys.modules["plexora"].app.config["PLEXORA_BASE_URL"] == "/user/me/proxy/8765"


def test_a_detected_hub_does_not_open_a_browser_here(monkeypatch, capsys):
    """The URL that works is a path on the hub's origin, which webbrowser
    cannot open, and the machine holding the screen is somewhere else."""
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch, node=None)
    _detects(monkeypatch, _resolved("proxy", "/user/me/proxy/{port}"))
    monkeypatch.setattr(cli, "should_open_browser",
                        lambda **kwargs: kwargs.get("preference") != "no")
    opened = []
    monkeypatch.setattr(cli, "_schedule_browser_open",
                        lambda *a, **k: opened.append(a))

    cli.main(["--port", "8765"])

    assert opened == []
    printed = capsys.readouterr().out
    assert "/user/me/proxy/8765/" in printed
    assert "--no-detect" in printed


def test_a_detected_ood_session_takes_the_portal_s_spelling_of_the_node(monkeypatch):
    served = {}
    _fake_serve(monkeypatch, served)
    # The scheduler says one thing; the portal's own prefix says another, and
    # the portal is the one doing the routing.
    _inside_a_job(monkeypatch, node="c42")
    _detects(monkeypatch, _resolved("ood", "/rnode/c42.cluster.edu/{port}"))

    cli.main(["--port", "0"])

    assert served["host"] == "0.0.0.0"
    assert sys.modules["plexora"].app.config["PLEXORA_BASE_URL"] == (
        f"/rnode/c42.cluster.edu/{served['port']}"
    )
    assert os.environ["PLEXORA_AUTH_TOKEN"]


def test_a_detected_ssh_session_prints_the_tunnel_command(monkeypatch, capsys):
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch, node=None)
    _detects(monkeypatch, _resolved("direct"))
    monkeypatch.setattr(cli, "looks_remote", lambda env=None: True)

    cli.main(["--port", "8765"])

    printed = capsys.readouterr().out
    assert "[plexora-remote]" in printed
    assert "ssh -N -L 8765:127.0.0.1:8765" in printed
    assert served["host"] == "127.0.0.1"


def test_no_detect_serves_plain_localhost_however_exotic_the_machine(monkeypatch):
    served = {}
    _fake_serve(monkeypatch, served)
    _inside_a_job(monkeypatch, node=None)
    called = []
    monkeypatch.setattr(cli, "detect_environment",
                        lambda *a, **k: called.append(1))

    cli.main(["--no-detect", "--port", "8765"])

    assert called == []
    assert served["host"] == "127.0.0.1"
    assert not os.environ.get("PLEXORA_BASE_URL")


# -- saved servers on the command line -----------------------------------


def _connect_args(argv):
    return cli.build_parser("connect").parse_args(argv)


def _profile(**kwargs):
    fields = {
        "name": "hpc", "target": "me@login.cluster.edu",
        "remote_command": "conda run -n imaging plexora",
        "datasource": "tonsil", "data_dir": "/scratch/me", "plugins": None,
        "srun": "-p interactive", "bind_node": True, "jump": None,
        "ssh_opts": ("ServerAliveInterval=30",), "forwards": ("8642",),
    }
    fields.update(kwargs)
    return types.SimpleNamespace(**fields)


def test_the_remote_command_default_matches_connect_py():
    """Two copies again, for the same standalone-loading reason."""
    from plexora import connect

    assert cli.DEFAULT_REMOTE_COMMAND == connect.DEFAULT_REMOTE_COMMAND


def test_connecting_without_a_profile_is_exactly_what_was_typed():
    kwargs = cli.connect_kwargs(_connect_args(["me@host", "tonsil"]))
    assert kwargs["remote_command"] == "plexora"
    assert kwargs["datasource"] == "tonsil"
    assert kwargs["srun"] is None
    assert kwargs["bind_node"] is False
    assert kwargs["forwards"] == []


def test_a_saved_profile_supplies_everything_that_was_not_typed():
    kwargs = cli.connect_kwargs(_connect_args(["hpc"]), _profile())
    assert kwargs["remote_command"] == "conda run -n imaging plexora"
    assert kwargs["datasource"] == "tonsil"
    assert kwargs["srun"] == "-p interactive"
    assert kwargs["bind_node"] is True
    assert kwargs["data_dir"] == "/scratch/me"
    assert kwargs["forwards"] == ["8642"]
    assert kwargs["ssh_opts"] == ["ServerAliveInterval=30"]


def test_a_typed_flag_beats_the_saved_one():
    """The case that decides this is the ordinary one: the same server, a
    different project, every day."""
    args = _connect_args(["hpc", "other-study", "--srun", "-p gpu",
                          "--remote-command", "/opt/plexora/bin/plexora"])
    kwargs = cli.connect_kwargs(args, _profile())

    assert kwargs["datasource"] == "other-study"
    assert kwargs["srun"] == "-p gpu"
    assert kwargs["remote_command"] == "/opt/plexora/bin/plexora"
    # Untouched flags still come from the profile.
    assert kwargs["data_dir"] == "/scratch/me"


def test_a_saved_profile_may_ask_for_srun_with_no_arguments():
    """"" and None are different instructions -- the site's defaults versus no
    scheduler at all -- and `or` would collapse them into one."""
    kwargs = cli.connect_kwargs(_connect_args(["hpc"]), _profile(srun=""))
    assert kwargs["srun"] == ""

    kwargs = cli.connect_kwargs(_connect_args(["hpc"]), _profile(srun=None))
    assert kwargs["srun"] is None


def test_an_unreadable_profile_store_is_not_an_error(monkeypatch):
    """A connection typed out in full must not depend on a registry file."""
    assert cli._saved_remote("anything") is None
