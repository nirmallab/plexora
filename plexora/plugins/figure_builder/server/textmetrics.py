"""Typographic constants, in one table, for three renderers.

The canvas, the PDF writer and the raster writer all have to put a baseline in
the same place. They cannot share code -- one of them is JavaScript -- so they
share a TABLE instead, and `figureRichText.js` carries a copy that
`test_the_two_sides_agree_on_the_typographic_constants` holds equal to this one.
That is the same arrangement as `strokegeom.py` and
`figureStrokeGeometry.js`: one rule, two languages, pinned by a test.

Only the twelve Adobe core fonts are offered, and that is a deliberate ceiling
rather than a first step. reportlab knows their metrics without a font file, so
nothing has to ship, nothing has to be licensed, and no export can fail because
a font did not load. The three families also have metric-compatible substitutes
on every desktop platform -- Arial for Helvetica, Liberation Serif for Times,
Courier New for Courier -- so a browser measuring a string gets the same answer
the PDF writer will, which is what makes "wrap as you type" safe to store.

Sizes here are in POINTS and positions in MILLIMETRES, because that is the split
the rest of the plugin already uses: type is specified in points and the page is
measured in millimetres.
"""

from __future__ import annotations

#: Millimetres in one typographic point. Computed rather than written out:
#: `FigureCanvas.PT_PER_MM` is the rounded literal 2.8346 and the two must not
#: be confused. Rounding here would put a floor under how exactly the canvas and
#: the exporter can be asserted to agree.
MM_PER_PT = 25.4 / 72.0

#: Line box height as a multiple of the type size. 1.2 is the usual default and
#: replaces the 1.25 that `.fb-annotation-text` used to set in CSS -- the canvas
#: no longer leaves leading to the browser, so the number has to live somewhere
#: both languages can read it.
LINE_HEIGHT = 1.2

#: The type size a text box starts at, in points.
#:
#: NOT `settings.style.font_size_pt`, which is 8 and has to stay 8: that one is
#: the size of the furniture drawn ON a panel -- legend rows and the scale bar's
#: caption, both of which sit inside the image and have to be small enough to
#: stay out of it (see compose._panel_furniture). A caption BESIDE a figure is
#: read at a normal reading size, and it was inheriting the panel-label size
#: instead -- `FigureCanvas.drawStyle` asked for `label_size_pt`, which is the
#: size of the letter "A" in the corner of a panel and has nothing to do with a
#: sentence.
#:
#: Overridable on every box, so this is only where a new one starts.
DEFAULT_TEXT_SIZE_PT = 14.0

#: Ascent and descent as a fraction of the em, from the Adobe core AFM files.
#: Used to centre a line inside its line box, which is what makes vertical
#: alignment and multi-size lines land in the same place on all three renderers.
ASCENT = {
    "Helvetica": 0.718,
    "Times-Roman": 0.683,
    "Courier": 0.629,
}
DESCENT = {
    "Helvetica": 0.207,
    "Times-Roman": 0.217,
    "Courier": 0.157,
}

#: Underline and strike-through, as a fraction of the em from the baseline.
#: Drawn explicitly by every renderer rather than delegated to `text-decoration`
#: or whatever the PDF viewer would do: SVG takes its underline position from
#: the font file actually in use, so a browser showing Arial and a PDF holding
#: Helvetica would put the rule in two different places.
UNDERLINE_OFFSET_EM = 0.12
UNDERLINE_THICKNESS_EM = 0.06
STRIKE_OFFSET_EM = 0.26

#: The families a document may name. These are PostScript base names rather than
#: abstract keys like "sans", because `settings.style.font_family` already holds
#: "Helvetica" and hands it straight to reportlab's `setFont`. Keeping that
#: vocabulary means a figure written by this build still renders in the right
#: typeface in a build that predates this module.
FAMILIES = ("Helvetica", "Times-Roman", "Courier")
DEFAULT_FAMILY = "Helvetica"

_POSTSCRIPT = {
    ("Helvetica", False, False): "Helvetica",
    ("Helvetica", True, False): "Helvetica-Bold",
    ("Helvetica", False, True): "Helvetica-Oblique",
    ("Helvetica", True, True): "Helvetica-BoldOblique",
    ("Times-Roman", False, False): "Times-Roman",
    ("Times-Roman", True, False): "Times-Bold",
    ("Times-Roman", False, True): "Times-Italic",
    ("Times-Roman", True, True): "Times-BoldItalic",
    ("Courier", False, False): "Courier",
    ("Courier", True, False): "Courier-Bold",
    ("Courier", False, True): "Courier-Oblique",
    ("Courier", True, True): "Courier-BoldOblique",
}

#: What the browser should ask for, per family. Every stack names substitutes
#: that are metric-compatible with the core font, in the order they are likely
#: to exist: Arial (Windows/macOS) and Liberation Sans (Linux) were both drawn
#: to Helvetica's widths, so a line that fits on screen fits in the PDF.
#:
#: This lives here and not in the stylesheet so that the family list is one
#: table. `figureRichText.js` carries the same three strings.
CSS_STACK = {
    "Helvetica": 'Helvetica, Arial, "Liberation Sans", sans-serif',
    "Times-Roman": '"Times New Roman", Times, "Liberation Serif", serif',
    "Courier": '"Courier New", Courier, "Liberation Mono", monospace',
}


def family(value):
    """A family this build can draw, or the default.

    Never raises. An unknown family is a style problem, and the document it
    arrived in still has words in it worth showing.
    """
    return value if value in FAMILIES else DEFAULT_FAMILY


def postscript_name(name, bold=False, italic=False):
    """The PostScript font for one family and weight.

    Raises `KeyError` on a family this module does not know, deliberately: by
    the time a run reaches here it has been through `schema.normalize_annotation`
    and its family is whitelisted, so a miss is a typo in `_POSTSCRIPT` or a
    family added to the UI and not to this table. The export path catches it,
    substitutes the default and says so on the provenance page rather than
    silently shipping the wrong typeface.
    """
    return _POSTSCRIPT[(name, bool(bold), bool(italic))]


def em_mm(size_pt):
    """One em of this type size, in millimetres."""
    return size_pt * MM_PER_PT


def line_metrics(runs, line_height=LINE_HEIGHT):
    """Height and baseline offset of the line box holding these runs.

    The tallest run sets the line box, and the line is centred inside it -- half
    the leading above, half below -- so that a line mixing an 8 pt caption with
    a 6 pt superscript sits where a reader expects rather than riding on the
    bottom of its box.

    Returns `(lead_mm, ascent_mm, descent_mm)`, where the baseline of the line
    is `lead_mm - ascent_mm - descent_mm` halved, plus `ascent_mm`, below the top
    of the box. `text_layout` does that sum; this function only measures.
    """
    if not runs:
        return (0.0, 0.0, 0.0)
    tallest = max(runs, key=lambda run: run["size_pt"])
    em = em_mm(tallest["size_pt"])
    name = family(tallest.get("family"))
    return (em * line_height, em * ASCENT[name], em * DESCENT[name])
