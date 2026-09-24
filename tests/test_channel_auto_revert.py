"""The channel contrast window as one line, and the undo on the end of it.

The control used to take two rows: a header carrying two bordered number boxes
and a text "Auto" button, and the slider under it. Two boxes and their gaps are
about a third of a 300px sidebar, which is why the numbers had been moved off
the slider in the first place -- inline they left almost no track to aim with.
Drawn as plain text until they are clicked they cost three or five characters
each, so the numbers went back to the ends of the track they describe and the
header row is gone.

Auto is the only destructive control in the panel: it replaces whatever window
is on screen, and on a channel tuned by eye that window is the only copy of a
number pressing Auto again will not return. So the pair is taken down first and
the button becomes an undo of itself -- a different icon, the same 20px of
space, no second control on the line.

The behaviour is pinned by tests/js/channel_auto_revert_probe.mjs, which runs
the real methods out of viewerSidebar.js. This module drives it and adds the
source-level checks a running probe cannot make: that the old two-row markup
and its stylesheet rules are actually gone, rather than merely unused.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "channel_auto_revert_probe.mjs"
SIDEBAR = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views" / "viewerSidebar.js"
VIEWER_CSS = REPO_ROOT / "plexora" / "client" / "src" / "css" / "viewer.css"
MAIN_CSS = REPO_ROOT / "plexora" / "client" / "src" / "css" / "main.css"


def test_auto_stores_the_window_it_replaces_and_offers_it_back():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The probe asserts internally, but pin its lines here too so a future edit
    # that quietly drops a check is not mistaken for a passing test.
    for line in (
        "the resting state is a labelless Auto icon with a tooltip",
        "Auto stores the exact window it is about to replace, then offers it back",
        "Revert restores the exact window and returns the icon to Auto",
        "Revert restores where the window came from, not only the window",
        "Revert alters the intensity range and nothing else about the channel",
        "an Auto that changes nothing leaves no Revert behind",
        "the button cannot be pressed again while the fit is in flight",
        "the inline numbers are sized to the domain, not to their contents",
        "Enter returns a typed number to its text appearance; Escape does not",
    ):
        assert line in proc.stdout, proc.stdout


def test_the_window_is_one_row_with_the_numbers_on_the_slider():
    """No header row, and no `fieldsSlot` parking the boxes away from the track.

    `fieldsSlot` is the slider option that reparents its two number boxes
    somewhere else; it exists for exactly this panel and the figure builder's.
    While it was passed, the numbers could be anywhere on the page and the row
    they sat on was a second line by definition.
    """
    source = SIDEBAR.read_text(encoding="utf8")
    assert "slot-detail-header" not in source, (
        "the header row the two number boxes used to sit on is back"
    )
    # The option key, not the word -- the comment beside the slider's options
    # names it to say why it is absent.
    assert "fieldsSlot:" not in source, (
        "the contrast slider is parking its number boxes off the track again"
    )
    assert 'rangeRow.classList.add("slider-auto-row")' in source
    # The order on the line is the order of the sketch: low, track, high, icon.
    row = source[source.index('rangeRow.classList.add("slider-auto-row")'):]
    row = row[: row.index("detail.appendChild(rangeRow)")]
    assert row.index("rangeRow.appendChild(slider)") < row.index("rangeRow.appendChild(auto)"), (
        "the action icon belongs at the far right of the line, after the slider"
    )


def test_the_action_is_an_icon_and_never_a_word():
    """A labelless glyph with its meaning in the tooltip.

    Fifteen channels can have this line open at once. "Auto" as text, in a
    bordered button, is what made the old panel read as a column of buttons
    with sliders between them rather than as a list of channels.
    """
    source = SIDEBAR.read_text(encoding="utf8")
    assert 'auto.textContent = "Auto"' not in source, "the icon grew a text label"
    for icon in ("fa-wand-magic-sparkles", "fa-rotate-left"):
        assert icon in source, f"{icon} is how one of the two states is drawn"
    for tooltip in ('"Auto contrast"', '"Restore previous range"'):
        assert tooltip in source, f"{tooltip} is the only place the state is spelled out"


def test_the_two_numbers_are_text_until_they_are_focused():
    """Borderless and transparent at rest; a field only while it is being typed in.

    Pinned in the stylesheet rather than inferred from a screenshot: these four
    declarations ARE the difference between a number that reads as a label and
    one that reads as an input, and a `.plx-number` rule reaching this panel
    from main.css would otherwise put the box back with nothing else changing.

    A MODIFIER ON THE PRIMITIVE, beside `.plx-number` itself in main.css, not a
    rule of this panel's: the gating threshold is the same control with a
    different domain, and two copies of these declarations drifted the moment
    either was touched. It was an opt-in once; now it is every slider's.
    """
    assert ".slot-detail-header" not in VIEWER_CSS.read_text(encoding="utf8"), (
        "the removed header row still has styling"
    )
    assert "is-plain-numbers" not in SIDEBAR.read_text(encoding="utf8"), (
        "the contrast window still asks for what every slider now has"
    )

    css = MAIN_CSS.read_text(encoding="utf8")
    start = css.index(".plx-slider .plx-number,\n.gradient-range-scale .plx-number {")
    rest = css[start:]
    resting = rest[: rest.index("}")]
    assert "background: transparent;" in resting
    assert "border: 0;" in resting

    focused = rest[rest.index(".gradient-range-scale .plx-number:focus {"):]
    focused = focused[: focused.index("}")]
    # `box-shadow` and not `border`: a border on a fixed-width border-box input
    # takes its pixel out of the text, so the number would step sideways at the
    # moment it was clicked.
    assert "box-shadow: inset" in focused
    assert "border:" not in focused
