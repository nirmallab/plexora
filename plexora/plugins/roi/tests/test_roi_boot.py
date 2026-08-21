"""The plugin's client comes up when loaded the way the server loads it.

Nothing else here runs this plugin's JavaScript. The Python suite renders the
panel's HTML and stops; `node --check` sees syntax only. So the entire client
can be broken -- a file missing from `PLUGIN.scripts`, a constructor that throws
the moment it runs, a registration that never happens -- while every server-side
test passes, and the only symptom is a panel that appears and does nothing.

The file list is read off the descriptor rather than restated here, so what gets
exercised is what the server will actually send.

One thing deliberately not claimed: that the ORDER of that tuple matters. It
does not for this plugin -- these files reference each other from inside methods
and constructors, which run after toolLoader has awaited every script, so the
bindings resolve whatever sequence they arrived in. Asserting an order-dependence
that is not there would be a test that passes for the wrong reason. A file left
OUT is a genuine failure, and is what the can-fail case below uses.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.roi import PLUGIN

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "roi_boot_probe.mjs"


def _run(scripts):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE), *scripts],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


@pytest.fixture(scope="module")
def report():
    return _run(list(PLUGIN.scripts))


def test_the_declared_order_loads(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0
    assert data["loaded"] == list(PLUGIN.scripts)


def test_the_plugin_registers_itself(report):
    """Core never names a plugin -- it activates whatever registered. A client
    that loads without registering is a tool whose panel appears and does
    nothing."""
    _, data = report
    assert len(data["registered"]) == 1
    assert data["registered"][0]["name"] == "roi"
    assert data["registered"][0]["ownsCellLayer"] is False


def test_a_controller_can_be_built_from_a_plugin_context(report):
    """Loading without throwing is not the same as working: a class can define
    fine and still name something that does not exist when it is used."""
    _, data = report
    assert data["controller"] == {
        "tool": "select", "state": "idle.select", "status": "saved", "ready": False,
    }


def test_every_declared_script_exists(report):
    static = REPO_ROOT / "plexora" / "plugins" / "roi" / "static"
    for name in PLUGIN.scripts:
        assert (static / name).is_file(), f"{name} is declared but not shipped"
    for name in PLUGIN.styles:
        assert (static / name).is_file(), f"{name} is declared but not shipped"


def test_the_order_of_the_declared_scripts_does_not_matter(report):
    """Stated as a test rather than assumed, because the descriptor's comment
    orders them as if it did. Every cross-file reference here is inside a method
    or a constructor, and toolLoader awaits all six before anything activates --
    so a reordering is harmless, and knowing that is what makes the omission
    case below the failure worth guarding."""
    returncode, data = _run(list(reversed(PLUGIN.scripts)))
    assert returncode == 0, "\n".join(data["problems"])
    assert data["registered"][0]["name"] == "roi"


def test_the_probe_catches_a_file_dropped_from_the_descriptor(report):
    """The real failure, produced on purpose: a file the others depend on left
    out of PLUGIN.scripts, so the browser never fetches it. Everything
    server-side is unaffected -- the panel still renders."""
    scripts = [name for name in PLUGIN.scripts if name != "roiGeometry.js"]

    returncode, data = _run(scripts)
    assert returncode == 1
    assert any("RoiGeometry" in problem for problem in data["problems"])
