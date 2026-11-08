"""Remote servers from the website, end to end (tests/browser/remote.mjs).

A saved server connected from the page with nothing installed for the
browser: SSH in the page through the relay (relay/, under `wrangler dev`) to a
test server that signs in as O2 does (tests/browser/ssh_server.py), a data
node started detached, its files listed through the relay; then a reload and
a relay restart, neither of which asks anything again; then Disconnect
(docs/internal/WEB_REMOTE_RELAY.md).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "remote.mjs"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"
BENCH = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"
RELAY = ROOT / "relay" / "node_modules" / ".bin" / "wrangler"

try:
    import asyncssh  # noqa: F401
except ImportError:  # a test tool only
    asyncssh = None

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not PLAYWRIGHT.is_dir() or not BENCH.is_dir() or not RELAY.exists()
    or asyncssh is None,
    reason="needs node, playwright-core, the website's Python bundle (tools/browser_build/build.py), "
           "relay/node_modules and asyncssh")


def test_a_server_connects_from_the_website_and_no_restart_ends_it():
    done = subprocess.run(["node", str(DRIVER)], capture_output=True, text=True, encoding="utf-8",
                          timeout=900, cwd=ROOT)
    assert done.stdout.strip(), done.stderr[-2000:]
    report = json.loads(done.stdout)
    assert report["ok"], json.dumps(report, indent=1)[:4000]
    assert set(report["steps"]) == {"connect", "node", "reload", "restart", "disconnect"}
    # Three questions, each asked once: the key, the password, the code.
    assert len(report["steps"]["connect"]["asked"]) == 3
