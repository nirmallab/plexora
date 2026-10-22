"""The fast `data-tooltip` bubble, and why nothing can cut it off.

It used to be two pseudo-elements on the control, clipped by every overflow
box around it -- HD Mode's was cut in half by the viewer sidebar's edge. It is
one portaled, fixed element now (services/tooltip.js), placed inside the
window. The placement is in tests/js/tooltip_probe.mjs, run in node below;
what is left here is the wiring.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tooltip_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


def test_the_bubble_stays_inside_the_window():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    for line in (
        "above by default, centred on the control, tail at its centre",
        "no room above, so it goes below rather than off the top",
        "asked for below with no room there, so it goes above",
        "at the left edge it slides in, and the tail still points at the control",
        "at the right edge it slides in, the tail kept off the rounded corner",
        "align right hangs it left from the control's right edge",
    ):
        assert line in proc.stdout, proc.stdout


def test_it_is_loaded_on_every_page_after_the_portal():
    base = source("templates", "base.html")
    assert "services/tooltip.js" in base
    assert base.index("views/popoverPortal.js") < base.index("services/tooltip.js")


def test_no_pseudo_element_tooltip_is_left_to_be_clipped():
    """A `[data-tooltip]::after` bubble would come back clipped wherever it is
    inside an overflow box, and would draw a second bubble under the glass."""
    css = "\n".join(
        p.read_text(encoding="utf-8")
        for p in list((CLIENT / "src" / "css").glob("*.css"))
        + list((REPO_ROOT / "plexora" / "plugins").rglob("*.css"))
    )
    assert not re.search(r"\[data-tooltip[^\]]*\][^{]*::(before|after)", css)


def test_it_is_glass_not_a_coloured_slab():
    main = source("src", "css", "main.css")
    start = main.index(".plx-tooltip-bubble,\n.plx-tooltip-tail {")
    block = main[start:main.index("}", start)]
    assert "backdrop-filter: blur(" in block
    assert "--tooltip-bg" not in main
    assert "--tooltip-bg" not in source("src", "css", "tokens.css")
