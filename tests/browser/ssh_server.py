"""An SSH server for the website's remote-server tests (tests/browser/remote.mjs).

Signs in the way HMS O2 does -- a password, then a Duo-style second prompt --
over keyboard-interactive, and runs each command it is given with /bin/sh on
this machine, in a home folder of its own, so the start script the website
builds (server/models/web_remote.py) starts a real `plexora node serve`. Its
SFTP side serves the same home folder, for a files-only server.

    python tests/browser/ssh_server.py [--port 0] [--home DIR]

Prints one JSON line `{"port", "home", "user", "password", "code"}` when
listening. Host keys are ECDSA P-256 and RSA (the website's SSH client has no
Ed25519 host keys), made fresh for each run.

Needs asyncssh (a test tool only: `pip install asyncssh`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import asyncssh

USER = "tester"
PASSWORD = "correct horse"
CODE = "1"
DUO = ("Duo two-factor login for tester\n\nEnter a passcode or select one of the "
       "following options:\n\n 1. Duo Push to XXX-XXX-1234\n")


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return False

    def kbdint_auth_supported(self):
        return True

    def get_kbdint_challenge(self, username, lang, submethods):
        self._step = 0
        return "", "", "", [("Password: ", False)]

    def validate_kbdint_response(self, username, responses):
        if username != USER:
            return False
        if self._step == 0:
            if responses != [PASSWORD]:
                return False
            self._step = 1
            return "", DUO, "", [("Passcode or option (1-1): ", True)]
        return responses == [CODE]


def environment(home: Path) -> dict:
    repo = Path(__file__).resolve().parents[2]
    siblings = [repo.parent / name / "src" for name in
                ("spatialagents", "spatialbridge", "biocognia-markers", "biocognia-intelligence")]
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": ":".join([str(repo), *map(str, siblings)]),
        "PLEXORA_DATA_PATH": str(home / "plexora-data"),
        "PLEXORA_LIBRARY_PATH": "",
    }


async def run(process: asyncssh.SSHServerProcess, home: Path) -> None:
    command = process.command or "true"
    child = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", command, cwd=str(home), env=environment(home),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def pump_in():
        try:
            async for data in process.stdin:
                child.stdin.write(data)
                await child.stdin.drain()
        except Exception:  # noqa: BLE001 - the channel closed
            pass

    async def pump_out():
        while True:
            data = await child.stdout.read(4096)
            if not data:
                break
            process.stdout.write(data)

    feeder = asyncio.ensure_future(pump_in())
    try:
        await pump_out()
        process.exit(await child.wait())
    except Exception:  # noqa: BLE001
        pass
    finally:
        feeder.cancel()
        if child.returncode is None:
            # The channel closed: the script's process group is hung up on,
            # as sshd's closing does. A detached node is not in it.
            try:
                os.killpg(child.pid, 1)
            except ProcessLookupError:
                pass


async def main(argv=None) -> int:
    if os.environ.get("PLEXORA_SSH_DEBUG"):
        import logging

        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
        asyncssh.set_debug_level(2)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--home", default=None)
    args = parser.parse_args(argv)
    home = Path(args.home or tempfile.mkdtemp(prefix="plexora-ssh-home-"))
    home.mkdir(parents=True, exist_ok=True)
    # SFTP's relative paths, `.` (the home folder a client asks for first)
    # among them, are this process's working directory's.
    os.chdir(home)
    keys = [asyncssh.generate_private_key("ecdsa-sha2-nistp256"),
            asyncssh.generate_private_key("ssh-rsa", key_size=2048)]
    server = await asyncssh.create_server(
        Server, "127.0.0.1", args.port, server_host_keys=keys,
        process_factory=lambda process: run(process, home), sftp_factory=True, encoding=None)
    port = server.sockets[0].getsockname()[1]
    print(json.dumps({"port": port, "home": str(home), "user": USER, "password": PASSWORD,
                      "code": CODE}), flush=True)
    await asyncio.Event().wait()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
