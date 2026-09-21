"""The one slider: PlexoraSlider, which every range input in Plexora now is.

The design half of this change is a stylesheet and cannot be tested here. What
can, and what matters more, is the arithmetic a shared primitive does on its
way between a panel and a file. Nineteen sliders built five different ways each
had their own clamping, their own rounding and their own idea of when a drag
was finished; folding them into one file means one place can now be wrong about
all of them at once.

Four things are load-bearing:

* the step grid, because `0 + 12 * 0.01` is 0.12000000000000001 in binary
  floating point and a panel would persist that;
* the two ends of a range, which must not cross and, where a caller asks,
  must stay a step apart -- a zero-width window divides by zero downstream;
* the log scale, where the handle holds a position on a 1000-step grid and
  the value must not be read back off it, or a channel window displayed as
  1234 is saved as 1231.7;
* the split between `onInput` and `onChange`, which is how every consumer
  separates a cheap repaint from an expensive commit. A primitive that fired
  the second one per pixel would put a hundred entries in the figure builder's
  undo history for one drag of an opacity slider.

Run in node against the real `views/slider.js`, because nothing else can: the
Python suite renders templates and stops, and `node --check` sees syntax.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "slider_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def test_a_slider_is_a_row_and_not_a_box(probe):
    """The visual half that is checkable: no wrapper, no border, a track with a
    rail and a fill, and the number box in the same row rather than under it."""
    assert "a slider is a row of a track and a number box, and no box of its own" in probe
    assert "the track holds a rail, a fill and one range input" in probe
    assert "a range gets two inputs, two boxes and the lower box first" in probe
    assert "fields can be turned off without turning off the slider" in probe


def test_the_fill_ends_where_the_thumb_is(probe):
    """Rail and fill are inset by half a thumb because that is where a native
    thumb's centre travels. Get it wrong and the fill is a few pixels past the
    handle at one end and short of it at the other, which reads as a bug in the
    value rather than in the geometry."""
    assert "a single slider's fill starts at the left edge and never moves" in probe
    assert "the tick moved the fill and the box with it" in probe
    assert "the fill sits where the thumb is, on a log scale too" in probe


def test_values_are_clamped_and_snapped_without_float_dust(probe):
    assert "a value outside the extent is clamped at construction, both ways" in probe
    assert "a value off the step grid snaps onto it with no float dust" in probe
    assert "step 'any' keeps what it was given" in probe
    assert "decimals follow the step unless they are given" in probe


def test_two_events_two_costs(probe):
    """The contract every consumer depends on. `onInput` is per tick and cheap;
    `onChange` is once, on release, and is where the PATCH, the save, the tile
    reload and the undo entry live."""
    assert "a drag tick is one onInput and no onChange" in probe
    assert "a hundred ticks are still no commit" in probe
    assert "release is the one commit" in probe


def test_set_is_a_commit_and_silent_set_is_not(probe):
    """The distinction d3-simple-slider spelled `value()` versus
    `silentValue()`, which the channel window and the gate both relied on: Auto
    Threshold sets and commits, a readout refresh sets and says nothing."""
    assert "set() without silent is a commit: both callbacks, once each" in probe
    assert "a silent set tells nobody" in probe
    assert "a silent set still repaints and rewrites the box" in probe


def test_the_number_box_is_editable_and_commits_on_blur(probe):
    """A slider cannot express "0.5 exactly" and half the values in this app are
    read off a paper. Commit is blur or Enter and not every keystroke, because
    "25" is typed one character at a time and the first of them is 2 -- a
    different bin size, and in the panels behind these boxes a tile request."""
    assert "a typed number commits once and moves the handle" in probe
    assert "a typed number past the end clamps and the box says so" in probe
    assert "a typed number snaps onto the step grid on commit" in probe
    assert "a keystroke previews: the handle moves, nothing commits" in probe
    assert "Escape puts back the number that was there on focus" in probe


def test_the_box_can_be_typed_past_the_end_of_the_track(probe):
    """A track is sized for the values worth dragging through, which is not
    always every value that is legal. Transcript point size drags between 1
    and 20 pixels -- a dusting to dots that touch -- and a track that ran to a
    hundred would spend four fifths of its length on sizes nobody wants; but
    the one figure that needs a 40-pixel dot at print size still has to be
    able to ask for it. Above the track the thumb pins, the value stays the
    typed one, and `aria-valuetext` reports it because `aria-valuenow` is
    stuck at the pinned position."""
    assert "the box is bounded by the ceiling and the track by the track" in probe
    assert "a typed number past the end of the track is kept, not clamped" in probe
    assert "the thumb pins at the end of its travel and the fill fills" in probe
    assert "aria-valuetext carries the number a pinned thumb cannot report" in probe
    assert "the ceiling is still a ceiling" in probe
    assert "a value above the track survives a silent set, which is a reload" in probe
    assert "touching the rail takes the rail's answer" in probe
    assert "without a ceiling the box clamps at the track, as it always did" in probe


def test_an_emptied_box_does_not_commit_zero(probe):
    """`Number("")` is 0, and 0 is a legal opacity, a legal Q-score and a legal
    threshold. A box cleared on the way to typing something else must not
    commit zero as it passes."""
    assert "an emptied box puts back what was there and commits nothing" in probe


def test_the_two_ends_never_cross(probe):
    assert "the low end cannot be dragged past the high end" in probe
    assert "the high end cannot be dragged past the low end" in probe
    assert "a typed low end past the high end clamps and commits once" in probe
    assert "a range reports which end moved" in probe
    assert "a range hands its caller a pair, not a number" in probe


def test_a_minimum_gap_holds_them_apart(probe):
    """The gradient range asks for one step of clearance: a window with no width
    has no ramp to draw and divides by zero on the way to whatever reads it."""
    assert "with a minimum gap the two ends are held apart, not met" in probe
    assert "and held apart from the other side too" in probe


def test_the_reachable_handle_is_the_one_on_top(probe):
    """Two inputs stacked over one track; where the thumbs meet, the one with
    somewhere left to go has to be the one taking the click, or an end of the
    range is unreachable by mouse."""
    assert "when the two thumbs meet exactly one of them is on top" in probe


def test_a_log_slider_stores_the_value_and_derives_the_position(probe):
    """A channel window runs 1..65535 on a 1000-step grid. Reading the value
    back off the grid would mean a panel that displayed 1234 saved 1231.7 --
    the number on screen and the number on disk would differ, and the one the
    user typed would be the one that lost."""
    assert "a log handle holds a position, not a value" in probe
    assert "a set value survives the round trip through the position exactly" in probe
    assert "and the box shows the value, not the position" in probe
    assert "a drag lands within one grid step of where the value was" in probe
    assert "a log handle spells the real number out for a screen reader" in probe


def test_new_bounds_re_clamp_without_an_event(probe):
    """HD mode changes a channel's domain and a new column changes a gate's.
    That is the app telling the slider what it is measuring, not the user
    setting a value, so nothing is emitted -- a save fired from it would
    persist a window nobody chose."""
    assert "a new extent re-clamps the values it no longer contains" in probe
    assert "a new extent is the app talking, so it tells nobody" in probe
    assert "the boxes learn the new extent as well" in probe
    assert "an empty extent disables the control rather than drawing a lie" in probe
    assert "and a real extent brings it back" in probe


def test_disabled_and_unit_reach_every_part(probe):
    assert "disabling reaches the input and the box" in probe
    assert "and undisabling reaches both back" in probe
    assert "a unit can be set after the fact" in probe
    assert "and taken away again, hiding the span rather than leaving a gap" in probe


def test_identity_survives(probe):
    """Eleven of the sliders this replaces are staged in a template or an
    innerHTML string, with an id a golden file records, a `<label for>` pointing
    at it and, in two cases, a test matching its attributes. Adoption is what
    keeps all of that true."""
    assert "a single slider keeps the id the template gave it and labels it" in probe
    assert "a range keeps four ids and names each end for a screen reader" in probe
    assert "two inputs cannot share one <label for>, so the row is a group" in probe
    assert "adoption reads the extent off the element it found" in probe
    assert "adoption keeps the staged id and its place in the row" in probe
    assert "the adopted element ends up inside the track" in probe


def test_the_keyboard_ring_is_only_for_the_keyboard(probe):
    """Chromium matches `:focus-visible` on a range input after a plain mouse
    click, and the rounded rectangle that draws around the whole track is the
    box this redesign exists to remove. The class set on pointerdown is what
    tells the two apart; clearing it on pointerup would put the box straight
    back the moment the button came up."""
    assert "a pointer press marks the drag and marks the focus as the mouse's" in probe
    assert "letting go ends the drag but leaves the focus as the mouse's" in probe
    assert "an arrow key hands focus back to the keyboard, and the ring with it" in probe
    assert "blur clears both" in probe


def test_the_number_box_can_be_had_on_its_own(probe):
    """Two controls want the box without the track: the gradient range writes
    its ends under the colour bar, and the transcript bin row pairs a four-rung
    ladder with a box that takes any micron count."""
    assert "the box can be had without a track" in probe
    assert "the box takes any number, so that 0.37 can be typed into a 0.5 grid" in probe
    assert "a keystroke in a standalone box previews too" in probe
    assert "and blur commits it once" in probe
    assert "a standalone box clamps through the constraint it was given" in probe


def test_the_boxes_can_be_put_in_a_row_the_caller_owns(probe):
    """A 300px sidebar cannot spare the ~100px that two inline boxes and their
    gaps cost; the channel contrast window and the gate both put theirs on the
    line above, beside the Auto button that was already there. They stay the
    slider's own boxes -- and `destroy()` has to reach out of the root to take
    them back, which is the part that would otherwise leak listeners."""
    assert "fields can be built into a row the caller owns" in probe
    assert "and then the slider's own row is nothing but the track" in probe
    assert "a relocated box still drives the handle it belongs to" in probe
    assert "destroy takes the relocated boxes out of the caller's row too" in probe


def test_destroy_gives_back_what_adoption_borrowed(probe):
    """Adoption is a loan, and `destroy()` has to repay it.

    Eleven sliders adopt an `<input type="range">` that a template staged:
    the id is what a `<label for>` points at, what a golden file records and
    what the panel looks up on its next bind. Adoption moves that element
    inside the slider's root, so a destroy that only drops the root deletes
    the page's own markup -- and the next `bind()` finds nothing, returns
    early, and the control never comes back.

    This emptied three rows of the transcripts panel in the browser while
    every test here stayed green: `paintTree()`, the gene-list rebuild, was
    destroying the sliders, and it runs on load and on every gene added.
    """
    assert "adoption puts the root where the element stood" in probe
    assert "destroy hands the adopted element back to the page" in probe
    assert "and the slider's own root is gone with it" in probe
    assert "so the panel can find its element and bind again" in probe


def test_it_can_be_taken_away_again(probe):
    """The figure builder rebuilds its panels from an innerHTML string on every
    edit. Instances have to be destroyable, and destroy has to be complete --
    which it is only because every listener is on a node inside the root."""
    assert "a mount gets the slider appended to it" in probe
    assert "destroy takes the whole control out of the document" in probe
