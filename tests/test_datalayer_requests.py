"""Core's DataLayer methods must actually reach the network.

The regression this pins: `saveGatingList` kept `lassos: lassos` in its request
payload after `lassos` stopped being one of its parameters. Reading an
undeclared name throws ReferenceError, and the method's own
`catch { console.log(...) }` swallowed it -- so every gate the user set failed
to persist while the UI reported success.

No existing check could see it. `node --check` validates syntax only, the rest
of this suite never executes client JS, and the swallowing catch meant even
calling the method raised nothing. The probe therefore asserts that invoking a
method produces an outbound request, which is the one thing every method here
exists to do.

The probe is a standalone script so it can also be run by hand while editing
dataLayer.js:

    node tests/js/datalayer_globals_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "datalayer_globals_probe.mjs"


@pytest.fixture(scope="module")
def probe_result():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    # The probe reports on stderr so its own diagnostics never mix with output.
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    return report


def test_every_plugin_route_method_sends_its_request(probe_result):
    failures = probe_result["failures"]
    assert not failures, (
        "DataLayer methods that never reached the network: "
        + json.dumps(failures, indent=2)
        + "\nA method here exists to make one request; sending none means it died "
        "on the way, and these methods swallow their own exceptions."
    )


def test_the_probe_is_actually_looking_at_something(probe_result):
    """A probe that checks nothing passes everything. These are the methods
    core still holds that call a plugin's routes."""
    assert len(probe_result["checked"]) >= 9
    assert "saveGatingList" in probe_result["checked"]
