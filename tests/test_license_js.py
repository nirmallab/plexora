"""The browser half of licensing, run in node against the shipped scripts.

The checks live in tests/js/license_probe.mjs (the page's Pro surfaces, and
the website's verifier against the biocognia vectors) and
tests/js/license_client_probe.mjs (the website's licence client); this pins
their lines, so a check that is deleted or renamed fails here rather than
silently reducing what is covered.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "license_probe.mjs"

CHECKS = (
    "loading proFeature.js fetches nothing, stores nothing, shows nothing",
    "the page hint is hierarchical and false on Free",
    "the modal names the feature, says Free keeps working, and offers a trial on Free",
    "Video Studio is a Pro feature like any other, with a trial on Free",
    "what is off by default is never called Pro and offers no trial or price",
    "a third-party plugin comes from its maker, never from Pro",
    "a lapsed licence says so",
    "Start a Trial opens the portal's trial page in the browser",
    "Enter License goes to Settings > License",
    "with no BioCognia platform, a trial explains itself instead of opening nothing",
    "the badge says Pro",
    "a locked tool opens nothing, explains nothing and mounts nothing",
    "a quiet (restore) load of a locked tool opens nothing",
    "the platform's checkSigned reads every biocognia vector as the platform's own test does",
    "every vector verifies, or is refused with the code the biocognia package gives it",
    "canonical JSON is byte for byte the package's",
    "a certificate binds to the vectors' device secret, by its binding and by its commitment",
    "the browser trusts the shipped production keys and never a test key",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def _vectors() -> str:
    """The vector file the installed `biocognia` package ships (the platform's
    contract test holds its own copy equal to it)."""
    import biocognia

    return str(Path(biocognia.__file__).parent / "vectors" / "biocognia-vectors.json")


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "BIOCOGNIA_VECTORS": _vectors()})
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS


CONNECT_PROBE = REPO_ROOT / "tests" / "js" / "license_connect_probe.mjs"
CONNECT_CHECKS = (
    "Connect this device shows the code and the link, opens the portal, and polls",
    "a declined code says so and stops polling",
    "Cancel stops polling and puts the button back",
    "Account shows who is signed in: initials, name, email, organisation and Manage account",
    "on Free the account says how to connect, and neither the account nor the balance is asked",
    "an account that cannot be read says why",
    "AI credits show the balance and the member's limit; Add credits opens the portal; "
    "coming back to the window asks again, and a failed ask keeps the number",
)


def test_connect_this_device_probe():
    done = subprocess.run([node, str(CONNECT_PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CONNECT_CHECKS


CLIENT_PROBE = REPO_ROOT / "tests" / "js" / "license_client_probe.mjs"
CLIENT_CHECKS = (
    "the device flow saves the certificate, writes the worker's files, and keeps no code, key or email",
    "a licence key activates, and the key itself is never kept",
    "a bv 2 certificate binds by its commitment, and another browser's certificate is invalid",
    "expiry moves to grace, then expired, and a clock set back does not undo it",
    "the heartbeat is due after 7 days or inside 21 days of the end, never within 6 hours of a try, "
    "and a failure changes nothing",
    "renewed and revoked answers are applied, and a renewal that does not verify is rejected",
    "two tabs share one licence: one refreshes under the lock, the other hears of a change and re-reads",
    "Forget releases the device, then clears the certificate, the secret and the install id; "
    "a release that fails asks first",
    "the AI token is exchanged once, held in memory, renewed at two thirds of its life, and never kept",
    "the routes answer in license_routes.py's shapes, and other paths are not the licence's",
    "the platform's address must be https, unless it is this machine",
)


def test_licence_client_probe():
    done = subprocess.run([node, str(CLIENT_PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CLIENT_CHECKS


@pytest.mark.parametrize("source", ["services/proFeature.js", "views/settingsPage.js",
                                    "views/toolLoader.js", "views/helpMenu.js",
                                    "services/licenseClient.js", "services/licenseVerify.js"])
def test_syntax(source):
    path = REPO_ROOT / "plexora" / "client" / "src" / "js" / source
    done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_no_licence_secret_is_ever_in_browser_code():
    """The page is told plan, state and grants. Nothing in the client may
    reach for a certificate, a seat key or a token, or keep one in storage."""
    for path in (REPO_ROOT / "plexora" / "client" / "src" / "js").rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        if "PlexoraPro" not in text and "license" not in text.lower():
            continue
        assert "PLEXORA1." not in text and "BIOC1.ey" not in text, path
        assert "localStorage.setItem(\"license" not in text, path
        assert "sessionStorage.setItem(\"license" not in text, path
