"""The figure sheet, reached without a pointer.

The page surface used to be a div of anonymous divs: no role on the objects, no
accessible name, and no tab stop anywhere on it. So a keyboard user could not
select a panel -- and every control this workspace has acts on a selection, which
made the canvas unusable rather than merely awkward. WCAG 2.1.1, and the kind of
failure that is invisible to everybody who uses a mouse.

The behaviour lives in the browser, so the probe owns it:

    node tests/js/figure_canvas_a11y_probe.mjs

What is asserted on this side is the MARKUP the page ships with. The probe drives
`FigureCanvas` against a stub surface and never sees the template, so a template
that lost its `tabindex` would leave every probe assertion green and the page
unreachable again.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_canvas_a11y_probe.mjs"
TEMPLATE = (REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "templates"
            / "figure_builder" / "workspace_body.html")


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_the_sheet_can_be_worked_from_the_keyboard(report):
    returncode, data = report
    assert not data["problems"], json.dumps(data["problems"], indent=2)[:4000]
    assert returncode == 0


def test_the_page_surface_ships_as_a_listbox():
    """The tab stop and the role are in the template, not in the renderer.

    `FigureCanvas.render()` replaces the surface's CONTENTS and never the
    surface itself, so if these attributes were written in JavaScript they would
    have to be rewritten on every render -- and the one that forgot would be a
    page nobody could tab into, with no error anywhere."""
    markup = TEMPLATE.read_text(encoding="utf-8")
    surface = re.search(r'<div class="fb-page-surface"[^>]*>', markup)
    assert surface, "the page surface is gone"
    tag = surface.group(0)
    for attribute in ('tabindex="0"', 'role="listbox"', "aria-multiselectable",
                      "aria-label"):
        assert attribute in tag, f"the page surface has no {attribute}"


def test_every_icon_only_control_has_a_name():
    """A `title` is a tooltip, not an accessible name in every browser and
    screen-reader pairing -- and the topbar and status bar are entirely icon
    buttons. One unnamed button in a row of them is "button, button, button"."""
    markup = TEMPLATE.read_text(encoding="utf-8")
    unnamed = []
    for tag in re.findall(r"<button[^>]*>|<a class=\"fb-[^>]*>", markup):
        # A button with its own words does not need a label; one whose only
        # content is a Font Awesome span has nothing else to be read out.
        if "fb-icon-button" not in tag and "fb-zoom-readout" not in tag \
                and "fb-back" not in tag:
            continue
        if "aria-label" not in tag:
            unnamed.append(" ".join(tag.split())[:80])
    assert not unnamed, "icon-only controls with no accessible name:\n" + "\n".join(unnamed)
