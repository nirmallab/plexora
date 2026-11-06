"""`window.PlexoraBackend` (client services/backend.js) in server mode.

Every page a Plexora serves loads it, and in that mode it must change nothing:
its `fetch` is `window.fetch`, its kind and target are "server", `supports`
answers for what the server can do and `verify` has nothing to check. The
checks run in node against the shipped file (tests/js/backend_shim_probe.mjs);
this also pins where base.html loads it -- not deferred, right before
appStatus.js, so it exists before that file wraps `window.fetch` and before
anything issues a request.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "backend_shim_probe.mjs"
BASE = REPO_ROOT / "plexora" / "client" / "templates" / "base.html"


def test_the_backend_is_a_pass_through_in_server_mode():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "backend shim probe OK" in done.stdout


def test_base_loads_the_backend_right_before_app_status():
    scripts = re.findall(r"<script\b[^>]*\bsrc=\"([^\"]+)\"[^>]*>", BASE.read_text(encoding="utf-8"))
    names = [src.split("?")[0].rsplit("/", 1)[-1] for src in scripts]
    assert names.count("backend.js") == 1
    at = names.index("backend.js")
    assert names[at + 1] == "appStatus.js", names[at - 1:at + 2]
    tag = re.search(r"<script\b[^>]*services/backend\.js[^>]*>", BASE.read_text(encoding="utf-8"))
    assert "defer" not in tag.group(0) and "async" not in tag.group(0)
    assert re.search(r"backend\.js\?v=\d{8}_\w+", tag.group(0))
