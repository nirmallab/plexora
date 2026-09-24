"""One slider, one stylesheet, and nothing else styling a range input.

views/slider.js is loaded by base.html, so a `PlexoraSlider` can be built on any
page and inside any plugin panel -- which means its rules have to live in the
stylesheet base.html itself loads, and nowhere else. A second copy in viewer.css
or in a plugin's stylesheet would be a slider that looked different depending on
which file the page happened to load last, which is the state this change
replaced: nineteen sliders, five treatments, thirteen of them browser-default
grey.

The guard at the bottom is the load-bearing part. It asserts that no stylesheet
outside main.css's slider and gradient blocks styles a range input at all --
because the way this drifts back is not somebody rewriting `.plx-slider`, it is
somebody adding `.my-panel input[type="range"] { accent-color: ... }` next to a
new control and never seeing the one it already had.

WEBKIT AND GECKO TWINS ARE NEVER COMMA-JOINED. A selector list containing a
pseudo-element an engine does not recognise is invalid, and the engine drops the
whole rule -- so one comma between `::-webkit-slider-thumb` and
`::-moz-range-thumb` is a slider with no thumb in Firefox, on every page, with
nothing in the console to say so.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"
MAIN_CSS = CLIENT / "src" / "css" / "main.css"
SLIDER_JS = CLIENT / "src" / "js" / "views" / "slider.js"
BASE_HTML = CLIENT / "templates" / "base.html"

COMMENTS = re.compile(r"/\*.*?\*/", re.S)

#: The slider's own names. `plx-` is Plexora's general prefix and other widgets
#: use it too (openProject.css has `.plx-folder-back`), so this is the list of
#: what main.css owns rather than the namespace.
OWNED = re.compile(r"^\.plx-(slider|range|number)(-[\w-]+)?(?![\w-])")

#: Stylesheets that ship to a browser. The vendor bundle and the test fixtures
#: are somebody else's code.
STYLESHEETS = sorted(
    path
    for path in list((CLIENT / "src" / "css").glob("*.css"))
    + list((REPO_ROOT / "plexora" / "plugins").glob("*/static/*.css"))
)


def stripped(path):
    return COMMENTS.sub("", path.read_text(encoding="utf-8"))


def defines(css, class_name):
    """Does this stylesheet carry a rule for `.class_name`?

    The trailing boundary matters: `.plx-number` must not be answered by
    `.plx-number-group`, which is a different rule.
    """
    return re.search(rf"\.{re.escape(class_name)}(?![\w-])", css) is not None


def slider_block(css):
    """main.css's own slider rules: the `.plx-*` block and the gradient range's
    overlay, which is the one other place a range input is styled."""
    start = css.index(".plx-slider {")
    # The scale's own layout rule, which is the first thing after the overlay.
    # Not its `.plx-number`: that selector also sits in the plain-number rules
    # inside the block, beside `.plx-slider .plx-number`. At the start of a
    # line, or `.gradient-range.is-dimmed .gradient-range-scale {` answers.
    end = css.index("\n.gradient-range-scale {")
    return css[start:end]


def test_base_html_loads_the_slider_before_what_builds_with_it():
    """gradientRange.js's two handles ARE one of these sliders, and every plugin
    panel toolLoader.js fetches may build one."""
    base = BASE_HTML.read_text(encoding="utf-8")
    for name in ("views/slider.js", "views/gradientRange.js", "views/toolLoader.js"):
        assert name in base, f"base.html no longer loads {name}"
    assert (
        base.index("views/slider.js")
        < base.index("views/gradientRange.js")
        < base.index("views/toolLoader.js")
    ), "slider.js has to be parsed before anything that constructs one"
    tag = re.search(r'<script src="[^"]*views/slider\.js[^"]*"([^>]*)>', base)
    assert tag and "defer" in tag.group(1), "slider.js is loaded with defer"


def test_main_css_defines_every_class_the_slider_writes():
    """Whatever slider.js puts on an element has to be defined in the stylesheet
    base.html itself loads, or a plugin panel draws an unstyled range input."""
    css = MAIN_CSS.read_text(encoding="utf-8")
    written = sorted(set(re.findall(r'"([^"]*\bplx-[\w-]+[^"]*)"', SLIDER_JS.read_text(
        encoding="utf-8"))))
    names = sorted({token for value in written for token in value.split()
                    if token.startswith("plx-")})
    assert names, "slider.js writes no plx- classes at all any more"
    missing = [name for name in names if not defines(css, name)]
    assert not missing, f"main.css does not define: {missing}"


def test_no_other_stylesheet_redefines_the_slider():
    """One definition of what a slider IS, so two copies cannot drift apart on a
    page that loads both.

    A consumer scoping it to its own row -- `.cell-point-size > .plx-slider`,
    `.transcripts-row > .plx-slider` -- is layout and is how every shared widget
    in this app is placed. What is refused is a bare `.plx-slider { }` somewhere
    else, which is a second opinion about the same control.
    """
    for path in STYLESHEETS:
        if path == MAIN_CSS:
            continue
        css = stripped(path)
        for selector in re.findall(r"(?:^|[{}])\s*([^{}@]+?)\s*\{", css, re.M):
            for part in selector.split(","):
                part = part.strip()
                assert not OWNED.match(part), (
                    f"{path.name} styles `{part}` unscoped -- main.css owns "
                    "what a slider is; a consumer may only place one")


def test_the_spinner_arrows_are_gone_everywhere():
    """Globally and not per component, because it was previously written out
    twice byte for byte in two stylesheets that could not see each other, and
    the third component to want it would have written it a third time."""
    css = stripped(MAIN_CSS)
    assert re.search(r'input\[type="number"\]\s*{[^}]*appearance:\s*textfield', css)
    assert re.search(
        r'input\[type="number"\]::-webkit-outer-spin-button,\s*'
        r'input\[type="number"\]::-webkit-inner-spin-button\s*{[^}]*appearance:\s*none',
        css,
    )
    for path in (CLIENT / "src" / "css" / "viewer.css",
                 REPO_ROOT / "plexora" / "plugins" / "transcripts" / "static" / "transcripts.css"):
        text = stripped(path)
        assert "-webkit-inner-spin-button" not in text, (
            f"{path.name} still suppresses spinners of its own")


def test_the_box_around_the_track_is_off_in_every_state():
    """Chromium matches `:focus-visible` on a range input after an ordinary
    mouse click, and the rounded rectangle it draws around the whole track is
    the thing this redesign exists to remove. Turning off `:focus` alone leaves
    it."""
    css = stripped(MAIN_CSS)
    rule = re.search(r"\.plx-range:focus,\s*\.plx-range:focus-visible\s*{([^}]*)}", css)
    assert rule and "outline: none" in rule.group(1)
    base = re.search(r"\n\.plx-range\s*{([^}]*)}", css)
    assert base and "outline: none" in base.group(1)


def test_hover_is_inside_a_hover_query():
    """A touch device has no hover state to rest in. Outside the query, a tap
    leaves the halo stuck on behind the thumb until something else is touched."""
    css = stripped(MAIN_CSS)
    query = css.index("@media (hover: hover)")
    hover_rules = [m.start() for m in re.finditer(r"\.plx-range:hover::", css)]
    assert hover_rules, "nothing lights the thumb on hover any more"
    assert all(at > query for at in hover_rules), (
        "a :hover thumb rule sits outside @media (hover: hover)")


def test_every_webkit_thumb_rule_has_a_gecko_twin_and_no_comma_between_them():
    css = stripped(MAIN_CSS)
    webkit = len(re.findall(r"::-webkit-slider-thumb", css))
    gecko = len(re.findall(r"::-moz-range-thumb", css))
    assert webkit == gecko, (
        f"{webkit} WebKit thumb selectors against {gecko} Gecko ones -- one "
        "engine is drawing a thumb the other is not")
    for selector in re.findall(r"([^{}]+){", css):
        if "," not in selector:
            continue
        assert not ("-webkit-slider" in selector and "-moz-range" in selector), (
            f"comma-joined twins in `{selector.strip()}` -- Firefox drops the "
            "whole rule, and the slider loses its thumb")


def test_the_thumb_is_pulled_up_onto_the_line():
    """The thumb sits on the rail only because WebKit's twin pulls it up by
    half a thumb, and Gecko's twin does not.

    With the input and the runnable track both at `--plx-hit`, Blink and
    WebKit leave the thumb's centre exactly `--plx-thumb / 2` below the rail
    -- independent of `--plx-hit`, which is the part no reading of the spec
    suggests. Two plausible expressions were shipped here before this one and
    both were wrong: `(hit - thumb) / 2` left every thumb in the application
    12px under its line, and no margin at all still left 7px. Measured in
    headless Chrome at thumb/hit of 14/24, 18/18, 10/24 and 20/32 -- centred
    to 0.01px in all four.

    Gecko centres the thumb in the track itself, so a margin there would
    break Firefox in the same way this one fixes Chrome.
    """
    css = stripped(MAIN_CSS)
    shape = re.search(
        r"\.plx-slider \.plx-range::-webkit-slider-thumb\s*\{([^}]*)\}", css)
    assert shape, "the WebKit thumb shape rule is gone"
    assert "margin-top: calc(var(--plx-thumb) / -2)" in shape.group(1), (
        "the WebKit thumb must be pulled up by half a thumb, or it hangs "
        "below the rail; re-measure before changing this expression")

    for body in re.findall(r"::-moz-range-thumb[^{]*\{([^}]*)\}", css):
        assert "margin-top" not in body, (
            "Gecko centres the thumb itself -- a margin here pushes it off "
            "the rail in Firefox only, where nobody is looking")


def test_nothing_else_in_the_app_styles_a_range_input():
    """The guard. `.plx-slider` is the only treatment a range input gets, and
    the way that drifts back is a new panel styling its own control rather than
    somebody rewriting this block."""
    needles = ('type="range"', "::-webkit-slider-thumb", "::-webkit-slider-runnable-track",
               "::-moz-range-thumb", "::-moz-range-track", "::-moz-range-progress")
    for path in STYLESHEETS:
        css = stripped(path)
        if path == MAIN_CSS:
            css = css.replace(slider_block(css), "")
        found = [needle for needle in needles if needle in css]
        assert not found, (
            f"{path.name} styles a range input directly ({found}) -- every "
            "slider in Plexora is a PlexoraSlider")


def test_accent_color_is_only_left_on_checkboxes_and_radios():
    """`accent-color` was the app's entire slider design: two of fifteen range
    inputs set it and the other thirteen were browser-default grey. The thumb
    is drawn by `.plx-slider` now; what is left of `accent-color` colours the
    one native control this app still lets the browser draw."""
    for path in STYLESHEETS:
        css = stripped(path)
        for selector in re.findall(r"([^{}]*)\{[^{}]*accent-color", css):
            head = selector.split("}")[-1]
            assert "checkbox" in head or "radio" in head, (
                f"{path.name}: `accent-color` on `{head.strip()}`")
