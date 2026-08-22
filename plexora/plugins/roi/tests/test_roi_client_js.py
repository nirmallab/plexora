"""The ROI plugin's client logic, run rather than read.

Two probes, both standalone node scripts so they can also be run by hand while
editing:

    node tests/js/roi_geometry_probe.mjs
    node tests/js/roi_state_probe.mjs

They cover the two halves of this plugin that nothing else can see. The Python
suite never executes client JS, `node --check` validates syntax only, and both
failure modes here are invisible on screen: geometry that is subtly wrong still
draws a shape, and a save that never happens leaves a panel that looks exactly
right until the tab is closed.

Each has a companion test below that mutates the source and asserts the probe
notices -- a probe that passes whatever the code does is worth nothing, and the
only way to know is to break the code on purpose.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
STATIC = REPO_ROOT / "plexora" / "plugins" / "roi" / "static"
PROBES = REPO_ROOT / "tests" / "js"


def _run(probe, source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [node, str(PROBES / probe)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
    # The probes report on stderr so their diagnostics never mix with output.
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"{probe} produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _mutate(tmp_path, name, old, new):
    source = (STATIC / name).read_text(encoding="utf-8")
    assert old in source, f"the code this test mutates has moved or been renamed: {old!r}"
    target = tmp_path / name
    target.write_text(source.replace(old, new, 1), encoding="utf-8")
    return target


# -- geometry ------------------------------------------------------------

@pytest.fixture(scope="module")
def geometry_report():
    return _run("roi_geometry_probe.mjs")


def test_the_geometry_holds_up(geometry_report):
    returncode, report = geometry_report
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0


def test_the_geometry_probe_is_actually_checking_something(geometry_report):
    _, report = geometry_report
    assert report["checked"] >= 25


def test_the_geometry_probe_catches_a_wrong_fill_rule(tmp_path):
    """The mutation is one word, and it is the difference between clicking the
    middle of a doughnut selecting it and not. Nonzero winding fills straight
    across an interior ring; even-odd does not."""
    mutated = _mutate(
        tmp_path, "roiGeometry.js",
        "if ((yi > y) !== (yj > y)",
        "if (false && (yi > y) !== (yj > y)",
    )
    returncode, report = _run("roi_geometry_probe.mjs", mutated)
    assert returncode == 1
    assert report["failures"]


def test_the_geometry_probe_catches_over_eager_simplification(tmp_path):
    """A tolerance applied where it should not be flattens real corners -- a
    shape that still looks like a shape, drawn somewhere the user did not."""
    mutated = _mutate(
        tmp_path, "roiGeometry.js",
        "if (farthestDistance > epsilon && farthest > 0)",
        "if (false)",
    )
    returncode, report = _run("roi_geometry_probe.mjs", mutated)
    assert returncode == 1
    assert any("corner" in failure["check"] for failure in report["failures"])


# -- saving --------------------------------------------------------------

@pytest.fixture(scope="module")
def state_report():
    return _run("roi_state_probe.mjs")


def test_edits_reach_the_server_and_failures_keep_them(state_report):
    returncode, report = state_report
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0


def test_the_state_probe_is_actually_checking_something(state_report):
    _, report = state_report
    assert report["checked"] >= 25


def test_the_state_probe_catches_a_dropped_queue(tmp_path):
    """The failure this exists for: clearing the queue regardless of the answer.
    Everything on screen still looks right -- the local copy is the one being
    displayed -- and the regions are simply nowhere else."""
    mutated = _mutate(
        tmp_path, "roiState.js",
        "if (result.ok && result.data.success) {\n            this.queue.splice(0, batch.length);",
        "if (true) {\n            this.queue.splice(0, batch.length);",
    )
    returncode, report = _run("roi_state_probe.mjs", mutated)
    assert returncode == 1
    assert report["failures"]


def test_the_state_probe_catches_a_conflict_treated_as_success(tmp_path):
    """Accepting a 409 means the stale tab's queue is replayed over the other
    session's work -- the silent overwrite the revision exists to stop."""
    mutated = _mutate(
        tmp_path, "roiState.js",
        'if (result.status === 409) {',
        'if (false) {',
    )
    returncode, report = _run("roi_state_probe.mjs", mutated)
    assert returncode == 1
    assert any("stale" in failure["check"] or "conflict" in failure["check"]
               for failure in report["failures"])


# -- pointer handling ----------------------------------------------------

@pytest.fixture(scope="module")
def interaction_report():
    return _run("roi_interaction_probe.mjs")


def test_clicking_and_dragging_do_the_right_things(interaction_report):
    returncode, report = interaction_report
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0


def test_the_interaction_probe_is_actually_checking_something(interaction_report):
    _, report = interaction_report
    assert report["checked"] >= 35


def test_the_interaction_probe_catches_a_panel_that_never_hears_the_image_arrive(tmp_path):
    """Whether anything can be drawn depends on the viewer's world holding an
    image, which is not a fact about the annotations -- so no store change ever
    announces it.

    Opened with `?tool=roi` the panel renders while the first tile is still on
    its way, so the toolbar paints disabled; and on a project that already has
    categories no edit ever follows to repaint it. The tools stay dead with
    nothing anywhere saying why."""
    mutated = _mutate(
        tmp_path, "roiTools.js",
        'this.viewer.world?.addHandler("add-item", this._onWorldChange);',
        "",
    )
    returncode, report = _run("roi_interaction_probe.mjs", mutated)
    assert returncode == 1
    assert any("world" in failure["check"] or "arriving" in failure["check"]
               for failure in report["failures"]), report["failures"]


def test_the_interaction_probe_catches_drawing_without_a_category(tmp_path):
    """A project starts with no categories -- the user names their own -- so
    the draw tools have nowhere to put a shape until one exists.

    Losing the gate does not fail loudly: a shape gets made, the server refuses
    it as an unknown category, and the panel says "Save failed" about a region
    that is already on screen."""
    mutated = _mutate(
        tmp_path, "roiTools.js",
        "return this.ready && Boolean(this.store.activeCategory);",
        "return this.ready;",
    )
    returncode, report = _run("roi_interaction_probe.mjs", mutated)
    assert returncode == 1
    assert any("nothing can be drawn" in failure["check"] for failure in report["failures"])


def test_the_interaction_probe_catches_a_gesture_that_outlives_its_drag(tmp_path):
    """Esc, a tool shortcut and a panel switch all cancel mid-drag, and the
    mouse keeps sending movements until the button comes up. The state string
    survives the gesture it described, so three of the drag cases would read a
    `drag` that is no longer there -- a null dereference inside a pointer
    handler, which takes the rest of the interaction down with it."""
    mutated = _mutate(
        tmp_path, "roiTools.js",
        "if (needsGesture && !this.drag) return;",
        "if (false) return;",
    )
    returncode, report = _run("roi_interaction_probe.mjs", mutated)
    assert returncode == 1
    assert any("does not throw" in failure["check"] for failure in report["failures"])


def test_the_interaction_probe_catches_a_press_that_commits_too_early(tmp_path):
    """The bug this probe was written for, found by driving the real app and
    reinstated here exactly: deciding the gesture at press time.

    OpenSeadragon fires `canvas-press` for a click as well as for a drag, so
    committing to `editing.move` there left the machine stuck in it -- and
    `canvas-click`, guarded on `idle.select`, then never ran again. Selecting a
    second shape and deselecting both stopped working, while everything looked
    right because drawing a shape selects it as a side effect.
    """
    mutated = _mutate(
        tmp_path, "roiTools.js",
        """            this.drag = null;
            this.pending = { hit: this.hitTest(point), point };
            this.state = "idle.select";
            return;""",
        """            const hit = this.hitTest(point);
            this.drag = null;
            if (hit && hit.vertex) {
                this.state = "editing.vertex";
                this.drag = { id: hit.feature.id, vertex: hit.vertex, before: hit.feature.geometry };
            } else if (hit) {
                this.state = "editing.move";
                this.drag = { id: hit.feature.id, origin: point, before: hit.feature.geometry };
            } else { this.state = "idle.select"; }
            return;""",
    )
    returncode, report = _run("roi_interaction_probe.mjs", mutated)
    assert returncode == 1
    names = [failure["check"] for failure in report["failures"]]
    assert any("SECOND shape" in name for name in names), names


# -- the Map to cells gate -----------------------------------------------

@pytest.fixture(scope="module")
def map_button_report():
    return _run("roi_map_button_probe.mjs")


def test_the_map_button_appears_only_when_it_can_work(map_button_report):
    returncode, report = map_button_report
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0


def test_the_map_button_probe_is_actually_checking_something(map_button_report):
    _, report = map_button_report
    assert report["checked"] >= 12


def test_the_probe_notices_a_button_shown_without_cells(tmp_path):
    """The gate that matters most. Dropping the table half of the condition
    leaves a button whose only possible outcome is an error, on precisely the
    projects -- image plus regions, no data -- where ROI is most used."""
    source = _mutate(
        tmp_path, "roiSidebarController.js",
        "const canMap = Boolean(this._destination\n                && this._destination.hasTable) && anything;",
        "const canMap = anything;")
    returncode, report = _run("roi_map_button_probe.mjs", source)
    assert returncode == 1
    assert report["failures"]


def test_the_probe_notices_the_wrong_prefix_fallback(tmp_path):
    """Reusing destinationName() here is the easy mistake: it is the name the
    save button already uses, and it silently yields plexora_rois_category on
    every SpatialData project."""
    source = _mutate(
        tmp_path, "roiSidebarController.js",
        "return typed || (this._destination?.remembered || \"\");",
        "return typed || (this._destination?.remembered\n            || this._destination?.default_name || \"\");")
    returncode, report = _run("roi_map_button_probe.mjs", source)
    assert returncode == 1
    assert report["failures"]


# -- hover -----------------------------------------------------------------


@pytest.fixture(scope="module")
def hover_report():
    return _run("roi_hover_probe.mjs")


def test_hovering_a_region_says_which_one_and_where(hover_report):
    """`plexora:roi-hover` is the whole seam between this plugin and Cell
    Explorer's composition card. This plugin owns geometry and answers "which
    region, and where is it on screen"; the card answers "what is inside it".
    Neither can check the other, and a wrong answer on either side still draws
    a perfectly convincing card somewhere slightly wrong."""
    returncode, report = hover_report
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0


def test_the_hover_probe_is_actually_checking_something(hover_report):
    _, report = hover_report
    assert report["checked"] >= 20


def test_the_probe_notices_a_hover_announced_on_every_frame(tmp_path):
    """Deduplication is what makes the card an anchored panel rather than one
    chasing the cursor: moving around INSIDE a region is not a succession of
    hovers, and re-announcing it re-anchors the card sixty times a second."""
    source = _mutate(
        tmp_path, "roiTools.js",
        "        if (id === this.hoverId) return;\n",
        "")
    returncode, report = _run("roi_hover_probe.mjs", source)
    assert returncode == 1
    assert report["failures"]


def test_the_probe_notices_an_anchor_in_the_wrong_space(tmp_path):
    """The anchor is in CLIENT pixels; OSD's pixelFromPoint answers in
    container ones. Forgetting the canvas offset puts the card exactly as far
    from the region as the image is from the window -- which reads as a layout
    problem somewhere else entirely."""
    source = _mutate(
        tmp_path, "roiTools.js",
        "            left: canvas.left + topLeft.x,",
        "            left: topLeft.x,")
    returncode, report = _run("roi_hover_probe.mjs", source)
    assert returncode == 1
    assert report["failures"]


def test_the_probe_notices_a_pan_that_leaves_the_anchor_behind(tmp_path):
    """The anchor is a snapshot in client pixels, so it is wrong the moment the
    picture moves. Not re-sending it is worse than it sounds: no pointer event
    follows a viewport change, so whoever is showing something beside the region
    never hears again, and the region has to be left and re-entered before it
    can be seen -- which reads as a hover the tool missed rather than as a
    stale anchor."""
    source = _mutate(
        tmp_path, "roiTools.js",
        "        const feature = this.store.feature(this.hoverId);\n"
        "        if (feature) this.dispatchHover(feature);\n    }\n\n    /**\n"
        "     * The store changed under a stationary pointer.",
        "    }\n\n    /**\n     * The store changed under a stationary pointer.")
    returncode, report = _run("roi_hover_probe.mjs", source)
    assert returncode == 1
    assert report["failures"]


def test_the_probe_notices_a_re_anchor_that_never_re_tests_the_pointer(tmp_path):
    """Zooming carries shapes out from under a pointer that never moved. Taking
    the standing hover on trust re-anchors a card to a region the pointer is no
    longer in -- and because nothing else will say so, it stays there."""
    source = _mutate(
        tmp_path, "roiTools.js",
        "            if (point) this.setHover(this.hitTest(point)?.feature || null);",
        "")
    returncode, report = _run("roi_hover_probe.mjs", source)
    assert returncode == 1
    assert report["failures"]
