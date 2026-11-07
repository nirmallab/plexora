"""Google Cloud on the website without the CLI (plan L9), in a real Chrome.

`tests/browser/gcloud.mjs` against a stand-in Google (tests/browser/mock_google.py:
the OAuth authorization endpoint, userinfo, Resource Manager, Cloud Storage and
Compute Engine, never the real ones): the Google Cloud button on Settings opens
the form; Sign in with Google is a popup that returns to the site's own
google-auth.html with a token held in the tab's memory; the projects and the
buckets fill the form from the worker's Python; Create & Connect saves the
profile, creates a node VM (firewall rules first), follows its startup through
the Compute API and hands over a link to the node's own /app/connect with the
node's token in the fragment and the certificate fingerprint beside it; the
page lists instances and a bucket's folders and stops, resizes and starts the
VM; the button again opens the saved connection; the token is in no storage,
cookie, profile or file; and a token Google refuses is a sign-in.

Needs what a developer machine has and CI does not yet: system Chrome, node,
playwright-core (`npm ci` in tools/poc_browser_node, or PLEXORA_PLAYWRIGHT_MODULES)
and a website bundle built from this checkout (`tools/browser_build/build.py
--plexora-only` after a change under plexora/), found under PLEXORA_BENCH_HOME.
Skipped, saying which, without them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(os.environ.get("PLEXORA_BENCH_CHROME")
              or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BUILD = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"
MODULES = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
               or ROOT / "tools" / "poc_browser_node" / "node_modules")


def _version():
    import plexora

    return plexora.__version__


def test_google_cloud_signs_in_lists_starts_a_node_vm_and_hands_over_on_the_website():
    missing = [what for what, there in (("Chrome", CHROME.exists()), ("node", shutil.which("node")),
                                        ("the build", (BUILD / _version() / "stages.json").exists()),
                                        ("playwright-core", (MODULES / "playwright-core").exists()))
               if not there]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (see this file's docstring)")
    env = {**os.environ, "PLEXORA_BENCH_HOME": str(BUILD.parents[2]),
           "PLEXORA_PLAYWRIGHT_MODULES": str(MODULES)}
    done = subprocess.run(["node", str(ROOT / "tests" / "browser" / "gcloud.mjs")], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=900)
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    out = json.loads("\n".join(lines[start:])) if start is not None else {}
    assert done.returncode == 0 and out.get("ok"), json.dumps(out, indent=1)[-4000:] + done.stderr[-2000:]
    assert len(out["checks"]) == 13 and all(check["ok"] for check in out["checks"])
