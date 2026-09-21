"""Two channel sidebars on one page, and what happens when one is unmounted.

`ViewerSidebar` takes `{root, idPrefix}` so a second instance can drive a
second copy of the channel markup. That was safe while the only second
instance was Figure Builder's Quick Edit, which lives in a modal. A registered
layer's channel panel is not in a modal -- it is a card in the same sidebar,
ABOVE the base image's card -- so the viewer's own instance, whose root is the
whole document, reaches the layer's rows first through any lookup made by CSS
selector.

Three per-slot lookups were made that way. The failure is silent: the wrong
row's colour, marker and enabled state are rewritten and nothing reports it.

The checks are in `tests/js/sidebar_scoping_probe.mjs`, because what they
assert is which element a lookup resolves to -- a DOM question with no Python
answer. This drives the probe and names every check, so one quietly deleted
fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "sidebar_scoping_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "each row carries this instance's own prefixed id",
    "an instance resolves its own row",
    "syncSlotDom writes its own row and not the other instance's",
    "...and the other instance's row keeps its enabled state",
    "applySlotExpansion expands its own row only",
    "the Auto button swapped is its own",
    "an unpinned instance listens for the HD toggle",
    "destroy() takes the HD listener back off",
    "...and the one still mounted is the one that kept listening",
    "destroy() is safe to call twice",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)


def test_the_scoping_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)


def test_no_per_slot_lookup_is_written_as_a_selector():
    """The rule, stated once rather than re-derived per lookup. A fourth
    lookup written as `.channel-slot[data-slot=...]` would reintroduce exactly
    the bug above, and the probe can only see the three that exist."""
    source = (REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
              / "viewerSidebar.js").read_text(encoding="utf-8")
    assert '.channel-slot[data-slot=' not in source
    assert "slotRow(index) {" in source
