"""The desktop shell waits for the server's own teardown on Quit.

Quit closes the server's stdin (desktop/src-tauri/src/server.rs `shutdown`);
the server stops like Ctrl+C and runs its atexit teardown -- ssh sessions,
gcloud VM stop or delete, the remote-store index, nodes.json -- with
`plexora._lifetime.DEFAULT_GRACE` seconds before its own backstop. The shell
killed it (and its job object or process group) after 5 s, partway through.
Its grace must outlast the server's.
"""

import re
from pathlib import Path

from plexora import _lifetime

ROOT = Path(__file__).resolve().parents[1]
SERVER_RS = ROOT / "desktop" / "src-tauri" / "src" / "server.rs"


def _shell_grace_seconds():
    source = SERVER_RS.read_text(encoding="utf-8")
    found = re.search(r"const STOP_GRACE: Duration = Duration::from_secs\((\d+)\);", source)
    assert found, "server.rs has no STOP_GRACE in whole seconds"
    return int(found.group(1))


def test_the_shell_waits_longer_than_the_server_teardown_takes():
    assert _shell_grace_seconds() > _lifetime.DEFAULT_GRACE
