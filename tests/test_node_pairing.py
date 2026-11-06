"""Pairing a data node with a page, and the origins a node allows.

A page served from elsewhere -- the hosted viewer, a lab gateway -- reads a
node directly, so the node must allow that page's origin and the page must
learn the node's address and token. `plexora node serve --pair` does both: it
allows the origin and prints a link whose fragment carries the endpoint and
the token. A fragment never reaches a server, so the token crosses no network;
the one place it is in clear is the terminal that asked for it, which is why
these tests also look for it everywhere else the node writes.
"""

from __future__ import annotations

import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from plexora import cli, connect
from plexora.server.node import app as node_app

TOKEN = "0123456789abcdef0123456789abcdef"
HOSTED = "https://viewer.plexoraapp.com"


def _fragment(link):
    parts = urlsplit(link)
    return parts, {key: values[0] for key, values in parse_qs(parts.fragment).items()}


def _link_in(text):
    found = [word for line in text.splitlines() for word in line.split()
             if "/connect#" in word]
    assert len(found) == 1, text
    return found[0]


# -- the link -------------------------------------------------------------


def test_both_modules_default_to_the_same_page():
    """cli.py and connect.py each load without the package, so each keeps a
    copy; a drift would make `--pair` and the link it prints disagree."""
    assert cli.DEFAULT_PAIR_ORIGIN == connect.DEFAULT_PAIR_ORIGIN == HOSTED


def test_the_link_carries_endpoint_and_token_in_its_fragment_only():
    link = connect.pairing_url(True, "http://127.0.0.1:8642/", TOKEN)
    parts, fragment = _fragment(link)

    assert (parts.scheme, parts.netloc, parts.path) == (
        "https", "viewer.plexoraapp.com", "/connect")
    # Nothing a server would see: no query, and the token only after the '#'.
    assert parts.query == ""
    assert TOKEN not in link.split("#", 1)[0]
    assert fragment == {"v": "1", "e": "http://127.0.0.1:8642", "t": TOKEN}
    # The endpoint is percent-encoded whole, so its own ':' and '/' cannot be
    # read as the fragment's separators.
    assert "e=http%3A%2F%2F127.0.0.1%3A8642&" in link


@pytest.mark.parametrize(("page", "base", "origin"), [
    (None, HOSTED, HOSTED),
    (True, HOSTED, HOSTED),
    ("https://Viewer.PlexoraApp.com:443/", HOSTED, HOSTED),
    ("http://127.0.0.1:8642/app/", "http://127.0.0.1:8642/app", "http://127.0.0.1:8642"),
    ("http://localhost:8000", "http://localhost:8000", "http://localhost:8000"),
    ("http://[::1]:9000", "http://[::1]:9000", "http://[::1]:9000"),
])
def test_the_origin_allowed_is_the_one_a_browser_sends(page, base, origin):
    """Lower-case, no default port, no path: an Origin header never has one,
    and an allowed origin that differs by a byte is not allowed at all."""
    assert connect.pair_target(page) == (base, origin)


@pytest.mark.parametrize("page", [
    "tonsil", "ftp://example.org", "https://example.org/?a=1",
    "https://me@example.org", "https://example.org/#x", "http://:80",
])
def test_something_that_is_not_a_page_is_refused_by_name(page):
    """`--pair` takes an optional value, so a server name typed after it is
    read as the page; the refusal has to say what went where."""
    with pytest.raises(ValueError) as raised:
        connect.pair_target(page)
    assert "--pair" in str(raised.value)


# -- origins ---------------------------------------------------------------


def test_environment_origins_join_the_command_line_ones():
    environ = {"PLEXORA_NODE_ORIGINS":
               " https://viewer.plexoraapp.com/, http://localhost:8000\n"
               "http://127.0.0.1:8000  "}
    origins = node_app.node_origins(["http://127.0.0.1:8000/"], None, environ)
    # Command line first, then the variable; normalised as `--allow-origin`
    # always was, repeats dropped -- and localhost kept apart from 127.0.0.1,
    # because to a browser they are two origins.
    assert origins == ["http://127.0.0.1:8000", HOSTED, "http://localhost:8000"]


def test_the_pair_origin_is_added_to_the_rest():
    origins = node_app.node_origins(["http://127.0.0.1:8000"], HOSTED,
                                    {"PLEXORA_NODE_ORIGINS": ""})
    assert origins == ["http://127.0.0.1:8000", HOSTED]


def test_one_origin_or_several_are_both_accepted():
    """Every caller before several origins passed a single string."""
    assert connect.origin_list("http://a:1/") == ["http://a:1"]
    assert connect.origin_list(["http://a:1", "", None, "http://a:1/",
                                "http://b:2"]) == ["http://a:1", "http://b:2"]
    assert connect.origin_list(None) == []
    assert connect.split_origins("http://a:1,http://b:2 http://c:3") == [
        "http://a:1", "http://b:2", "http://c:3"]


# -- serve_node, with the server faked -------------------------------------


@pytest.fixture
def fake_server(monkeypatch, capsys):
    """`serve_node` up to the socket: priming, warming and waitress replaced,
    with a record of what was printed before the bind and before serving."""
    import waitress

    from plexora.server.models import data_model

    record = {"events": [], "app": None}

    class Server:
        def print_listen(self, fmt):
            record["events"].append("print_listen")

        def run(self):
            record["events"].append("run")
            record["before_run"] = capsys.readouterr().out

    def create_server(app, **options):
        record["events"].append("bound")
        record["before_bind"] = capsys.readouterr().out
        record["app"] = app
        record["options"] = options
        return Server()

    def serve(app, **options):
        record["events"].append("serve")
        record["app"] = app

    monkeypatch.setattr(data_model, "prime_hot_code", lambda log=None: None)
    monkeypatch.setattr(node_app, "warm_resources", lambda *a, **k: None)
    monkeypatch.setattr(waitress, "create_server", create_server)
    monkeypatch.setattr(waitress, "serve", serve)
    monkeypatch.delenv("PLEXORA_NODE_ORIGINS", raising=False)
    return record


def _serve(tmp_path, lines, **options):
    node_app.serve_node([], token=TOKEN, port=8642, node_id="pair-test",
                        dynamic=True, manifest=tmp_path / "manifest.json",
                        log=lambda line="", **kw: lines.append(str(line)),
                        **options)


@pytest.fixture
def debug_logging(monkeypatch, caplog):
    """Every record Plexora's loggers make, for one test: `serve_node`
    configures logging from PLEXORA_LOG_LEVEL, and the levels it sets are
    process-wide, so they are put back afterwards."""
    import logging

    ours, root = logging.getLogger("plexora"), logging.getLogger()
    levels = ours.level, root.level
    monkeypatch.setenv("PLEXORA_LOG_LEVEL", "debug")
    caplog.set_level("DEBUG")
    yield caplog
    ours.setLevel(levels[0])
    root.setLevel(levels[1])


def test_pair_prints_the_link_once_the_node_listens(fake_server, tmp_path,
                                                     debug_logging):
    lines = []
    caplog = debug_logging
    _serve(tmp_path, lines, pair=True)

    # Bound, then the link, then serving: a browser opened on the link a
    # moment early would meet a refused connection.
    assert fake_server["events"] == ["bound", "print_listen", "run"]
    assert "/connect#" not in fake_server["before_bind"]
    link = _link_in(fake_server["before_run"])
    _parts, fragment = _fragment(link)
    assert link.startswith(f"{HOSTED}/connect#")
    assert fragment == {"v": "1", "e": "http://127.0.0.1:8642", "t": TOKEN}

    # On stdout only: not through `log` (a launcher may point it at a file)
    # and not through `logging`.
    assert not any("/connect#" in line for line in lines)
    assert TOKEN not in caplog.text and "/connect#" not in caplog.text


def test_pair_allows_the_page_it_links_to(fake_server, tmp_path):
    _serve(tmp_path, [], pair=True, allow_origins=["http://127.0.0.1:8000"])
    app = fake_server["app"]
    assert app.config["PLEXORA_NODE_ORIGINS"] == ["http://127.0.0.1:8000", HOSTED]

    client = app.test_client()
    answer = client.get("/node/v1/hello", headers={
        "X-Plexora-Node-Token": TOKEN, "Origin": HOSTED})
    assert answer.status_code == 200
    assert answer.headers["Access-Control-Allow-Origin"] == HOSTED
    # And `features` says this node can do it, so a page can tell a node that
    # cannot from one that was simply not started with --pair.
    assert {"pair", "origins-env"} <= set(answer.get_json()["features"])


def test_the_environment_variable_reaches_the_running_node(fake_server, tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("PLEXORA_NODE_ORIGINS", "http://localhost:8000, " + HOSTED)
    _serve(tmp_path, [])
    client = fake_server["app"].test_client()
    for origin in ("http://localhost:8000", HOSTED):
        answer = client.get("/node/v1/health", headers={
            "X-Plexora-Node-Token": TOKEN, "Origin": origin})
        assert answer.headers.get("Access-Control-Allow-Origin") == origin
    # Not one it was not given: 127.0.0.1 is not localhost.
    answer = client.get("/node/v1/health", headers={
        "X-Plexora-Node-Token": TOKEN, "Origin": "http://127.0.0.1:8000"})
    assert "Access-Control-Allow-Origin" not in answer.headers


def test_browser_endpoint_is_the_address_the_link_hands_over(fake_server,
                                                             tmp_path):
    """A gateway, or a tunnel whose local end is another port: the browser's
    address for the node is not where the node bound."""
    _serve(tmp_path, [], pair="https://lab.example.org/plexora/",
           browser_endpoint="https://gw.example.org/node/")
    link = _link_in(fake_server["before_run"])
    parts, fragment = _fragment(link)
    assert (parts.netloc, parts.path) == ("lab.example.org", "/plexora/connect")
    assert fragment["e"] == "https://gw.example.org/node"
    assert "https://lab.example.org" in fake_server["app"].config["PLEXORA_NODE_ORIGINS"]


def test_a_node_bound_to_every_interface_still_links_to_loopback():
    assert node_app.pair_endpoint("0.0.0.0", 9000) == "http://127.0.0.1:9000"
    assert node_app.pair_endpoint("10.1.2.3", 9000) == "http://10.1.2.3:9000"


@pytest.mark.parametrize("options", [
    {"pair": "tonsil"},
    {"pair": True, "browser_endpoint": "gw.example.org:8642"},
])
def test_a_bad_pairing_stops_the_node_before_it_starts(fake_server, tmp_path,
                                                       options):
    with pytest.raises(node_app.NodeStartupError):
        _serve(tmp_path, [], **options)
    assert fake_server["events"] == []


def test_without_pair_the_node_starts_as_it_always_did(fake_server, tmp_path,
                                                       capsys):
    _serve(tmp_path, [])
    assert fake_server["events"] == ["serve"]
    assert "/connect#" not in capsys.readouterr().out


# -- a real node -----------------------------------------------------------


def _free_port():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_real_node_answers_by_the_time_its_link_is_printed():
    """`python -m plexora node serve --pair`, in its own process: the link
    arrives on stdout when the node already answers, and nothing the node
    writes elsewhere -- its log on stderr, files under its data root -- holds
    the token or the link."""
    port = _free_port()
    token = secrets.token_hex(16)
    root = tempfile.mkdtemp(prefix="plexora-pair-")
    env = dict(os.environ, PLEXORA_DATA_PATH=root, PLEXORA_LOG_LEVEL="debug",
               PYTHONUNBUFFERED="1")
    env.pop("PLEXORA_NODE_ORIGINS", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "plexora", "node", "serve", "--dynamic",
         "--host", "127.0.0.1", "--port", str(port), "--token", token, "--pair"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    errors = []
    threading.Thread(target=lambda: [lines.put(line) for line in process.stdout],
                     daemon=True).start()
    threading.Thread(target=lambda: errors.extend(process.stderr),
                     daemon=True).start()
    try:
        link = None
        deadline = time.monotonic() + 90
        while link is None and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                assert process.poll() is None, "".join(errors)
                continue
            if "/connect#" in line:
                link = line.strip()
        assert link, "no pairing link within 90 s"

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/node/v1/health",
            headers={"X-Plexora-Node-Token": token, "Origin": HOSTED})
        with urllib.request.urlopen(request, timeout=10) as answer:
            assert answer.status == 200
            assert answer.headers["Access-Control-Allow-Origin"] == HOSTED
        assert _fragment(link)[1] == {
            "v": "1", "e": f"http://127.0.0.1:{port}", "t": token}
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged node
            process.kill()
            process.wait(timeout=15)

    log = "".join(errors)
    assert token not in log and "/connect#" not in log
    for path in Path(root).rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path
    import shutil

    shutil.rmtree(root, ignore_errors=True)


# -- launch command lines --------------------------------------------------


def test_the_side_node_gets_one_flag_per_origin():
    argv = cli.node_serve_argv(["table:t=/x.h5ad"], 8642,
                               ["http://127.0.0.1:8000", "http://localhost:8000"],
                               python="python")
    assert argv.count("--allow-origin") == 2
    assert argv[-4:] == ["--allow-origin", "http://127.0.0.1:8000",
                         "--allow-origin", "http://localhost:8000"]
    # A single string is still one origin, not one per character.
    single = cli.node_serve_argv([], 8642, "http://127.0.0.1:8000", python="python")
    assert single[-2:] == ["--allow-origin", "http://127.0.0.1:8000"]


def test_node_allow_origin_repeats_on_the_main_command_and_on_connect():
    args = cli.build_parser().parse_args([
        "--also-serve", "table:t=/x.h5ad",
        "--node-allow-origin", "http://127.0.0.1:8000",
        "--node-allow-origin", "http://localhost:8000"])
    assert args.node_allow_origin == ["http://127.0.0.1:8000", "http://localhost:8000"]
    assert cli.build_parser().parse_args([]).node_allow_origin is None

    args = cli.build_parser("connect").parse_args([
        "hpc", "--node-allow-origin", "http://localhost:8000", "--pair"])
    kwargs = cli.connect_kwargs(args)
    assert kwargs["allow_origin"] == ["http://localhost:8000"]
    assert kwargs["pair"] == HOSTED
    plain = cli.connect_kwargs(cli.build_parser("connect").parse_args(["hpc"]))
    assert plain["allow_origin"] is None and plain["pair"] is None


def test_node_serve_and_node_connect_take_pair():
    node = cli.build_parser("node")
    args = node.parse_args(["serve", "--dynamic", "--pair"])
    assert args.pair == HOSTED and args.app is False and args.url_prefix is None
    args = node.parse_args(["serve", "--dynamic", "--pair", "http://localhost:9000",
                            "--browser-endpoint", "https://gw.example.org",
                            "--app", "--url-prefix", "/rnode/c42/8642"])
    assert (args.pair, args.browser_endpoint, args.app, args.url_prefix) == (
        "http://localhost:9000", "https://gw.example.org", True, "/rnode/c42/8642")
    args = node.parse_args(["connect", "me@hpc", "--serve", "image:t=/t.tif", "--pair"])
    assert args.pair == HOSTED


def test_the_remote_launch_line_names_every_origin_in_order():
    line = connect.remote_command_line(
        "plexora", 8123, also_serve=["table:cells=/scratch/c.h5ad"],
        node_allow_origin=[HOSTED, "http://127.0.0.1:9999"])
    assert line.count("--node-allow-origin") == 2
    # The browser's own origin last: an older Plexora over there keeps only
    # the last one, and that is the one it has always been sent.
    assert line.index(HOSTED) < line.index("http://127.0.0.1:9999")
    assert line.endswith("--node-allow-origin http://127.0.0.1:9999")
    # Unchanged with one origin, and with none.
    one = connect.remote_command_line(
        "plexora", 8123, also_serve=["table:cells=/scratch/c.h5ad"],
        node_allow_origin="http://127.0.0.1:9999")
    assert one.count("--node-allow-origin") == 1
    assert "--node-allow-origin" not in connect.remote_command_line(
        "plexora", 8123, node_allow_origin=[HOSTED])


def test_a_node_only_launch_line_names_every_origin():
    line = connect.node_command_line(
        "plexora", 8642, (), dynamic=True,
        allow_origin=["http://127.0.0.1:8000", HOSTED])
    assert "--allow-origin http://127.0.0.1:8000 --allow-origin " + HOSTED in line
    assert "token" not in line
