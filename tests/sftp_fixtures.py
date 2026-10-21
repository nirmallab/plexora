"""A real SFTP server over a directory, for the files-only server tests.

OpenSSH's own `sftp-server`, run directly: it speaks SFTP on stdin and stdout
exactly as it does at the far end of `ssh -s host sftp`, so everything above
the transport -- the protocol client, the registry, the fsspec filesystem and
the chunk cache over it -- is exercised against the real thing with no
network, no sshd and no login. Skipped where the binary is not installed
(Windows, a minimal container).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

#: Where distributions put it. Not on PATH anywhere: it is sshd's helper.
SERVER_CANDIDATES = (
    "/usr/libexec/sftp-server",               # macOS
    "/usr/lib/openssh/sftp-server",           # Debian, Ubuntu
    "/usr/libexec/openssh/sftp-server",       # Fedora, RHEL
    "/usr/lib/ssh/sftp-server",               # Arch
)

TARGET = "tester@sftp.test"


def sftp_server_binary():
    for candidate in SERVER_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("sftp-server")


needs_sftp_server = pytest.mark.skipif(
    sftp_server_binary() is None, reason="OpenSSH sftp-server is not installed")


def spawn_server(cwd):
    """An `sftp-server` whose working directory -- its "home" -- is `cwd`."""
    return subprocess.Popen([sftp_server_binary()], cwd=str(cwd),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)


class SFTPHost:
    """One served directory, connected and registered under `TARGET`."""

    def __init__(self, root: Path, target: str = TARGET):
        from plexora.server.utils import sftp

        self.root = root.resolve()
        self.target = target
        self.process = spawn_server(self.root)
        self.client = sftp.SFTPClient(self.process, label=target)
        self.client.handshake(timeout=10)
        self.client.home = self.client.realpath(".")
        sftp.register(target, self.client)

    def url(self, relative="") -> str:
        from plexora.server.utils import sftp

        return sftp.url_for(self.target, str(self.root / relative) if relative
                            else str(self.root))

    @property
    def base(self) -> str:
        return f"sftp://{self.target}"

    def disconnect(self):
        from plexora.server.utils import sftp

        sftp.unregister(self.target, self.client)
        self.client.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture
def sftp_host(tmp_path, cache_root):
    """`make(root=None)` -> an `SFTPHost` over `tmp_path / "served"`."""
    if sftp_server_binary() is None:
        pytest.skip("OpenSSH sftp-server is not installed")
    served = tmp_path / "served"
    served.mkdir(exist_ok=True)
    hosts = []

    def make(root=None, target=TARGET):
        host = SFTPHost(root or served, target)
        hosts.append(host)
        return host

    yield make
    for host in hosts:
        host.disconnect()
