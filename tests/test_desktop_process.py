"""The desktop server as the shell sees it: a real child process.

What the shell does is exactly this -- spawn, read one line, talk HTTP with
the token, close stdin -- so this is the contract test for `plexora
--desktop`. Slow (the scientific stack imports take seconds), hence one
launch per behaviour and generous deadlines.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
READY_TIMEOUT = 120


def _spawn(tmp_path, *extra, program=None):
    env = dict(os.environ)
    env["PLEXORA_DATA_PATH"] = str(tmp_path / "data")
    env.pop("PLEXORA_AUTH_TOKEN", None)
    env.pop("PLEXORA_PLUGINS", None)
    argv = ([sys.executable, "-c", program] if program
            else [sys.executable, "-m", "plexora", "--desktop", *extra])
    return subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")


def _read_ready(process):
    box = {}

    def read():
        box["line"] = process.stdout.readline()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(READY_TIMEOUT)
    if not box.get("line"):
        process.kill()
        raise AssertionError("no ready line; stderr:\n" + process.stderr.read()[-3000:])
    return json.loads(box["line"])


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def server(tmp_path):
    process = _spawn(tmp_path, "--port", "0")
    try:
        yield process, _read_ready(process)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)


def test_the_handshake_the_token_and_a_clean_exit_on_stdin_close(server, tmp_path):
    process, ready = server
    origin, token = ready["origin"], ready["token"]
    assert ready["event"] == "ready"
    assert Path(ready["data_root"]) == (tmp_path / "data").resolve()

    assert _get(f"{origin}/health?token={token}")[0] == 204
    assert _get(f"{origin}/health")[0] == 403

    status, body = _get(f"{origin}/desktop/info?token={token}")
    info = json.loads(body)
    assert status == 200 and info["desktop"] is True
    assert token not in body.decode()

    started = time.time()
    process.stdin.close()
    assert process.wait(15) == 0
    assert time.time() - started < 12
    assert process.stdout.read() == "", "nothing but the ready line on stdout"


def test_quit_from_the_page_runs_the_atexit_teardown(tmp_path):
    marker = tmp_path / "atexit-ran"
    program = (
        "import atexit, sys, pathlib\n"
        f"atexit.register(lambda: pathlib.Path({str(marker)!r}).write_text('yes'))\n"
        "sys.argv = ['plexora', '--desktop', '--port', '0']\n"
        "from plexora.cli import main\n"
        "raise SystemExit(main())\n")
    process = _spawn(tmp_path, program=program)
    try:
        ready = _read_ready(process)
        request = urllib.request.Request(
            f"{ready['origin']}/shutdown?token={ready['token']}", method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 204
        assert process.wait(20) == 0
        assert marker.read_text() == "yes"
    finally:
        if process.poll() is None:
            process.kill()


def test_a_second_server_never_shares_the_first_ones_port(server, tmp_path):
    _process, first = server
    second = _spawn(tmp_path, "--port", str(first["port"]))
    try:
        assert second.wait(READY_TIMEOUT) == 2
        assert second.stdout.read() == ""
    finally:
        if second.poll() is None:
            second.kill()
