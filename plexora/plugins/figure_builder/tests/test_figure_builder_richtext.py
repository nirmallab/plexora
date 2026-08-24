"""Rich text, and the one thing that can silently break it.

A text annotation holds its words twice: `rich` is the lines of styled runs and
`text` is the flat string derived from them. BOTH the browser and the server
normalise that structure -- the browser because the canvas draws from it and an
editor has to hand it back canonical, the server because it is the gate every
write passes through. If the two ever disagree, the canvas shows the user what
they typed and the document stores something else, and they find out on reload.
Nothing else in the suite would notice.

So `tests/js/figure_richtext_probe.mjs` owns a table of deliberately awkward
inputs, emits its own answers alongside the inputs, and the test below pushes
the identical inputs through `schema.normalize_rich_text`. The table lives in
one place rather than being written out twice and drifting -- which is the same
failure it exists to catch.

The rest pins the other half: that a document written before `rich` existed
still opens, that a build without `rich` degrades to the words rather than to
nothing, and that the twelve family variants all resolve to fonts reportlab
actually has -- which is what makes the export path's silent fallback provably
dead code rather than a hiding place.

    node tests/js/figure_richtext_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import compose, schema, textmetrics

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_richtext_probe.mjs"


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


def test_the_browsers_own_arithmetic_is_right(report):
    returncode, data = report
    assert not data["problems"], json.dumps(data["problems"], indent=2)
    assert returncode == 0


def test_the_probe_actually_normalised_something(report):
    """A probe whose case table is empty passes every assertion in it."""
    _, data = report
    assert len(data["cases"]) >= 15


def test_the_client_and_the_server_normalise_rich_text_the_same_way(report):
    """The important one.

    Every case the probe ran, run again here. A mismatch means the canvas and
    the document hold different words, or the same words differently marked up,
    for input a user can actually produce -- a Windows paste, a word processor
    that styles every letter, a colour typed in the wrong case.
    """
    _, data = report
    mismatches = []
    for case in data["cases"]:
        got = schema.normalize_rich_text(case["input"]["flat"], case["input"]["rich"])
        if got != case["rich"] or schema.plain_text(got) != case["text"]:
            mismatches.append({
                "case": case["name"],
                "python": got,
                "browser": case["rich"],
                "python_text": schema.plain_text(got),
                "browser_text": case["text"],
            })
    assert not mismatches, json.dumps(mismatches, indent=2)[:4000]


def test_the_canvas_and_the_exporter_put_the_baseline_in_the_same_place(report):
    """The other important one.

    `FigureCanvas.textLayout` decides where a caption's lines sit on screen and
    `compose._text_layout` decides where they sit in the PDF. They are the same
    arithmetic written twice, so they are compared here the way the arrowhead
    rule already is -- and a drift between them is a caption that is a
    millimetre out only in the export, which nothing else would catch.
    """
    _, data = report
    mismatches = []
    for case in data["layouts"]:
        ours = compose._text_layout(case["annotation"])
        theirs = case["layout"]
        if len(ours["lines"]) != len(theirs["lines"]):
            mismatches.append(f"{case['name']}: line count "
                              f"{len(ours['lines'])} != {len(theirs['lines'])}")
            continue
        if ours["block_h_mm"] != pytest.approx(theirs["block_h_mm"], abs=1e-9):
            mismatches.append(f"{case['name']}: block height "
                              f"{ours['block_h_mm']} != {theirs['block_h_mm']}")
        for index, (mine, yours) in enumerate(zip(ours["lines"], theirs["lines"])):
            for key in ("baseline_mm", "lead_mm"):
                if mine[key] != pytest.approx(yours[key], abs=1e-9):
                    mismatches.append(f"{case['name']} line {index} {key}: "
                                      f"{mine[key]} != {yours[key]}")
            if mine["last_of_paragraph"] != yours["last_of_paragraph"]:
                mismatches.append(f"{case['name']} line {index}: "
                                  "disagree about the end of the paragraph")
    assert not mismatches, "\n".join(mismatches)


def test_the_probe_actually_laid_something_out(report):
    _, data = report
    assert len(data["layouts"]) >= 8
    assert any(len(case["layout"]["lines"]) > 1 for case in data["layouts"])


def test_the_two_sides_agree_on_the_typographic_constants(report):
    """Where a baseline goes is arithmetic over these numbers.

    Cheap to check and easy to break: the line height in particular used to live
    in `figure_builder.css` as `line-height: 1.25`, and a stylesheet is exactly
    the sort of place a constant survives a change nobody connected it to.
    """
    _, data = report
    theirs = data["constants"]
    assert theirs["MM_PER_PT"] == pytest.approx(textmetrics.MM_PER_PT, abs=1e-12)
    assert theirs["LINE_HEIGHT"] == textmetrics.LINE_HEIGHT
    # The size a new text box starts at. It is read on both sides -- the canvas
    # writes it into the style of every box it draws, the schema falls back to
    # it -- so a figure whose caption came from the browser and one whose
    # caption came through the REST surface have to start the same size.
    assert theirs["DEFAULT_SIZE_PT"] == textmetrics.DEFAULT_TEXT_SIZE_PT
    assert theirs["ASCENT"] == textmetrics.ASCENT
    assert theirs["DESCENT"] == textmetrics.DESCENT
    assert theirs["UNDERLINE_OFFSET_EM"] == textmetrics.UNDERLINE_OFFSET_EM
    assert theirs["UNDERLINE_THICKNESS_EM"] == textmetrics.UNDERLINE_THICKNESS_EM
    assert theirs["STRIKE_OFFSET_EM"] == textmetrics.STRIKE_OFFSET_EM
    assert theirs["FAMILIES"] == list(textmetrics.FAMILIES)
    assert theirs["DEFAULT_FAMILY"] == textmetrics.DEFAULT_FAMILY
    assert theirs["CSS_STACK"] == textmetrics.CSS_STACK
    assert theirs["MAX_TEXT_LENGTH"] == schema.MAX_TEXT_LENGTH
    assert theirs["MAX_TEXT_LINES"] == schema.MAX_TEXT_LINES
    assert theirs["MAX_RUNS_PER_LINE"] == schema.MAX_RUNS_PER_LINE
    assert theirs["MAX_TEXT_RUNS"] == schema.MAX_TEXT_RUNS


# -- the round trip, and what an older build sees -------------------------


def _text(**kwargs):
    raw = {"annotation_id": "ann_t1", "type": "text", "page_id": "pg_1"}
    raw.update(kwargs)
    return schema.normalize_annotation(raw)


def test_a_text_annotation_written_before_rich_existed_still_opens():
    """The whole migration, and the reason this needed no SCHEMA_VERSION bump."""
    annotation = _text(text="Fig. 1a\nScale bar 50 um")
    assert annotation["text"] == "Fig. 1a\nScale bar 50 um"
    assert [line["hard"] for line in annotation["rich"]["lines"]] == [True, True]
    assert schema.plain_text(annotation["rich"]) == annotation["text"]


def test_a_rich_annotation_degrades_to_its_words_in_an_older_build():
    """An older build's whitelist drops `rich` and writes the loss back.

    What it costs is the marks. What it must not cost is the words, the line
    breaks or the box -- which is the argument for an additive key rather than
    a version bump, in executable form so nobody has to trust the docstring.
    """
    rich = _text(rich={"lines": [
        {"hard": True, "runs": [{"text": "Fig. 1a", "bold": True},
                                {"text": " DAPI", "italic": True}]},
        {"hard": True, "runs": [{"text": "Scale bar 50 um", "size_pt": 6.0}]},
    ]})

    older = {key: value for key, value in rich.items() if key != "rich"}
    reopened = schema.normalize_annotation(older)

    assert reopened["text"] == rich["text"] == "Fig. 1a DAPI\nScale bar 50 um"
    assert reopened["geometry"] == rich["geometry"]
    assert all(len(line["runs"]) <= 1 for line in reopened["rich"]["lines"])
    assert not any(run.get("bold") or run.get("italic")
                   for line in reopened["rich"]["lines"] for run in line["runs"])


def test_the_flat_string_is_derived_and_never_trusted():
    """`text` is a projection of `rich`. A caller sending a `text` that
    disagrees with the `rich` beside it does not get to store both."""
    annotation = _text(
        text="a lie",
        rich={"lines": [{"hard": True, "runs": [{"text": "the truth"}]}]})
    assert annotation["text"] == "the truth"


def test_only_a_text_annotation_carries_rich():
    """A `rich` key on a rectangle is a field with no meaning, and
    `_update_annotation`'s merge would carry one along forever."""
    for kind in ("rect", "ellipse", "line", "arrow"):
        annotation = schema.normalize_annotation(
            {"annotation_id": "ann_x", "type": kind, "page_id": "pg_1",
             "rich": {"lines": [{"hard": True, "runs": [{"text": "no"}]}]}})
        assert "rich" not in annotation


def test_oversized_text_is_truncated_rather_than_refused():
    """`normalize_document` skips an annotation whose normaliser raises, so a
    ValueError here would silently DELETE the user's text box on the next
    read."""
    annotation = _text(rich={"lines": [{"runs": [{"text": "z" * 9000}]}]})
    assert len(annotation["text"]) == schema.MAX_TEXT_LENGTH


def test_a_run_cap_costs_the_marks_and_never_the_words():
    """A hundred distinctly styled spans on one line is past anything a caption
    needs, but the words on it are still the user's."""
    annotation = _text(rich={"lines": [{"runs": [
        {"text": "q", "size_pt": 1.0 + (index % 7)} for index in range(400)]}]})
    assert len(annotation["text"]) == 400
    assert len(annotation["rich"]["lines"][0]["runs"]) == schema.MAX_RUNS_PER_LINE


# -- fonts ----------------------------------------------------------------


def test_every_family_variant_resolves_to_a_font_reportlab_has():
    """The export path falls back to Helvetica on an unmapped family.

    That fallback is right -- a missing font is a style problem and not a reason
    to lose an export -- but it can only ever be reached by a typo in the table
    below, so this test is what keeps it dead code instead of a hiding place.
    """
    from reportlab.pdfbase import pdfmetrics
    for name in textmetrics.FAMILIES:
        for bold in (False, True):
            for italic in (False, True):
                pdfmetrics.getFont(textmetrics.postscript_name(name, bold, italic))


def test_an_unknown_family_falls_back_rather_than_being_stored():
    assert _text(style={"font_family": "Comic Sans"})["style"]["font_family"] == "Helvetica"
    assert textmetrics.family(None) == textmetrics.DEFAULT_FAMILY


def test_a_family_the_table_does_not_know_is_loud():
    """`postscript_name` raises rather than substituting silently. The export
    path catches it, substitutes, and says so on the provenance page."""
    with pytest.raises(KeyError):
        textmetrics.postscript_name("Comic Sans", False, False)
