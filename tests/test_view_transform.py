"""The view transform -- core's Rotate and Flip -- run rather than read.

    node tests/js/view_transform_probe.mjs
    node tests/js/view_transform_tools_probe.mjs
    node tests/js/overlay_transform_probe.mjs

Three probes, because the failures live in three places and all of them draw a
perfectly plausible picture:

  * **The service** (services/viewTransform.js). Our state flips on the
    screen's axes; OpenSeadragon has one horizontal flip. The mapping between
    them is checked as matrices at several angles, and every helper against
    where OSD's canvas drawer actually puts a point. A wrong mapping shows the
    tissue the wrong way round with nothing on screen saying so.
  * **The cards** (views/viewTransformTools.js). Three widgets for one number
    must stay one number, and a card opened fresh must show the live state --
    once the card holds nothing, that is what "reopening restores it" means.
  * **Everything we draw ourselves** -- the overlay canvas and the transcript
    shader -- has to land on the tile pixel it annotates.

Each has mutation tests below. A probe that passes whatever the code does is
worth nothing, and breaking the code on purpose is the only way to know.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"
SERVICE = CLIENT / "src" / "js" / "services" / "viewTransform.js"
TOOLS = CLIENT / "src" / "js" / "views" / "viewTransformTools.js"
OVERLAY = CLIENT / "external" / "openseadragon-bin-2.4.0" / "canvas-overlay-hd.js"
POINTS = REPO_ROOT / "plexora" / "plugins" / "transcripts" / "static" / "transcriptPoints.js"
TEMPLATES = CLIENT / "templates" / "tools"

SERVICE_PROBE = REPO_ROOT / "tests" / "js" / "view_transform_probe.mjs"
TOOLS_PROBE = REPO_ROOT / "tests" / "js" / "view_transform_tools_probe.mjs"
OVERLAY_PROBE = REPO_ROOT / "tests" / "js" / "overlay_transform_probe.mjs"


def _run(probe, *extra):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(probe), *map(str, extra)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        return proc.returncode, {"checked": 0, "failures": [proc.stderr or proc.stdout]}


def _mutate(tmp_path, source: Path, old: str, new: str) -> Path:
    text = source.read_text(encoding="utf-8")
    assert old in text, f"the code this test mutates has moved or been renamed: {old!r}"
    target = tmp_path / source.name
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target


def _fails(probe, flag, mutated):
    returncode, result = _run(probe, flag, mutated)
    return returncode != 0 and result["failures"]


# -- the probes pass ---------------------------------------------------------

@pytest.mark.parametrize("probe, minimum", [
    (SERVICE_PROBE, 120), (TOOLS_PROBE, 40), (OVERLAY_PROBE, 60)])
def test_the_probe_passes_and_checks_something(probe, minimum):
    returncode, result = _run(probe)
    assert not result["failures"], json.dumps(result["failures"], indent=2)
    assert returncode == 0
    assert result["checked"] >= minimum


# -- the service -------------------------------------------------------------

@pytest.mark.parametrize("old, new", [
    # A vertical flip is a horizontal one plus half a turn; drop either half
    # and the tissue is shown mirrored the wrong way or upside down.
    ("flipped: flipH !== flipV,", "flipped: flipH,"),
    ("degrees: normalize(flipV ? degrees + 180 : degrees),", "degrees: normalize(degrees),"),
    # OSD's pointFromPixel ignores the flip: a click on a mirrored view picks
    # the cell on the other side of the screen.
    ("unflipped = point(containerSize(viewer).width - pixel.x, pixel.y);", "unflipped = pixel;"),
    ("return point(containerSize(viewer).width - pixel.x, pixel.y);", "return pixel;"),
    # Mirror and turn in the drawer's order, not the other way round.
    ("context.rotate(degrees * Math.PI / 180);", "context.rotate(-degrees * Math.PI / 180);"),
    # One save per burst, and none for what boot adopts.
    ("if (save) this._scheduleSave();", "if (save) this._save();"),
    ("return this.set(clean(saved), { immediately: true, save: false });",
     "return this.set(clean(saved), { immediately: true, save: true });"),
    # A flip applied under an animated turn spins the picture half a turn
    # behind a mirror that already happened.
    ("this.apply({ immediately: immediately || flipChanged });",
     "this.apply({ immediately });"),
    # Home fills at right angles and contains otherwise.
    ("const viewWidth = (wider !== fill) ? boxWidth : boxHeight * aspect;",
     "const viewWidth = (wider === fill) ? boxWidth : boxHeight * aspect;"),
    ("const rightAngle = degrees % 90 === 0;", "const rightAngle = true;"),
])
def test_the_service_probe_catches(tmp_path, old, new):
    assert _fails(SERVICE_PROBE, "--source", _mutate(tmp_path, SERVICE, old, new))


# -- the cards ---------------------------------------------------------------

@pytest.mark.parametrize("old, new", [
    # A card that does not paint on setup opens upright over a turned view.
    ("                unsubscribe = service.subscribe(paint);\n                paint(service.get());",
     "                unsubscribe = service.subscribe(paint);"),
    # ...or never hears a change made anywhere else.
    ("                unsubscribe = service.subscribe(paint);\n                paint(service.get());",
     "                paint(service.get());"),
    # A closed card that keeps listening.
    ("                    unsubscribe?.();\n                    unsubscribe = null;\n                    slider?.destroy?.();",
     "                    slider?.destroy?.();"),
    # The quick-select that lights a stale angle.
    ("const on = Number(button.dataset.rotateTo) === degrees;", "const on = false;"),
    # A flip that toggles the other flip, or sets rather than toggles.
    ("service.set({ [key]: !service.get()[key] });", "service.set({ flipH: !service.get().flipH });"),
    ("service.set({ [key]: !service.get()[key] });", "service.set({ [key]: true });"),
    # The eye and the lazy boot are declared, not inferred.
    ('            lazy: true,\n            hasLayer: false,\n            help: {\n                summary: "Turns',
     '            hasLayer: false,\n            help: {\n                summary: "Turns'),
])
def test_the_tools_probe_catches(tmp_path, old, new):
    assert _fails(TOOLS_PROBE, "--source", _mutate(tmp_path, TOOLS, old, new))


def test_the_probe_builds_the_panels_the_templates_ship():
    """The tools probe builds its panels by hand. These are the ids and
    attributes it relies on, read off the real templates, so the two cannot
    drift apart while both keep passing."""
    rotate = (TEMPLATES / "rotate_panel.html").read_text(encoding="utf-8")
    flip = (TEMPLATES / "flip_panel.html").read_text(encoding="utf-8")
    for needle in ('id="rotate_panel_section"', 'id="rotate_slider"', 'id="rotate_reset_button"',
                   'class="cell-mode-control is-compact"', 'role="radiogroup"'):
        assert needle in rotate, needle
    assert re.findall(r'data-rotate-to="(\d+)"', rotate) == ["0", "90", "180", "270"]
    for needle in ('id="flip_panel_section"', 'id="flip_horizontal_button"',
                   'id="flip_vertical_button"', 'data-flip="flipH"', 'data-flip="flipV"',
                   'aria-pressed="false"'):
        assert needle in flip, needle
    # No heading, close or extras of their own: the card core builds has them.
    for panel in (rotate, flip):
        body = re.sub(r"\{#.*?#\}", "", panel, flags=re.S)
        assert "section-heading" not in body
        assert "data-tool-extras" not in body


def test_the_panels_use_icons_the_bundle_has():
    bundle = (CLIENT / "dist" / "vendor_bundle.js").read_text(encoding="utf-8", errors="ignore")
    for template in TEMPLATES.glob("*.html"):
        for name in re.findall(r'class="fas fa-([a-z-]+)"', template.read_text(encoding="utf-8")):
            assert (f'"{name}":[' in bundle
                    or re.search(r"[,{]" + re.escape(name) + r":\[", bundle)), (template.name, name)


# -- what we draw ourselves --------------------------------------------------

@pytest.mark.parametrize("old, new", [
    ("transform.orientContext(context, this._viewer);", ""),
    ("p = this._viewer.viewport.pixelFromPointNoRotate(vp, true);",
     "p = this._viewer.viewport.pixelFromPoint(vp, true);"),
])
def test_the_overlay_probe_catches_the_host(tmp_path, old, new):
    assert _fails(OVERLAY_PROBE, "--overlay", _mutate(tmp_path, OVERLAY, old, new))


@pytest.mark.parametrize("old, new", [
    ("const corner = viewport.pixelFromPointNoRotate(", "const corner = viewport.pixelFromPoint("),
    ("flipped: !!viewport.getFlip?.(),", "flipped: false,"),
    ("const radians = (viewport.getRotation(true) || 0) * Math.PI / 180;", "const radians = 0;"),
])
def test_the_overlay_probe_catches_the_transcripts(tmp_path, old, new):
    assert _fails(OVERLAY_PROBE, "--points", _mutate(tmp_path, POINTS, old, new))
