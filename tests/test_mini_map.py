"""The viewer's overview lens, run rather than read.

    node tests/js/mini_map_probe.mjs

Three things it covers that nothing else can. The colour arithmetic is a second
implementation of frag.glsl's u8_r_range, in a different language, and a drift
between them looks like "the mini-map is a bit dark" rather than like a bug.
The circle geometry still draws a perfectly plausible map when it is wrong --
it just points somewhere else. And the lifecycle failures are all invisible:
a handler that outlives a collapse costs work on every animation frame for the
rest of the session, and a request fired while the lens is shut costs the user
nothing they can see and everything the feature promised not to.

Each half has a mutation test below. A probe that passes whatever the code does
is worth nothing, and breaking the code on purpose is the only way to know.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VIEWS = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views"
PROBE = REPO_ROOT / "tests" / "js" / "mini_map_probe.mjs"
TEMPLATE = REPO_ROOT / "plexora" / "client" / "templates" / "base.html"


def _run(source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [node, str(PROBE)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
    # The probe reports on stderr so its diagnostics never mix with output.
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"the probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _mutate(tmp_path, old, new):
    name = "miniMap.js"
    source = (VIEWS / name).read_text(encoding="utf-8")
    assert old in source, f"the code this test mutates has moved or been renamed: {old!r}"
    target = tmp_path / name
    target.write_text(source.replace(old, new, 1), encoding="utf-8")
    return target


@pytest.fixture(scope="module")
def report():
    return _run()


def test_the_mini_map_holds_up(report):
    returncode, result = report
    assert not result["failures"], json.dumps(result["failures"], indent=2)
    assert returncode == 0


def test_the_probe_is_actually_checking_something(report):
    _, result = report
    assert result["checked"] >= 40


# -- geometry ------------------------------------------------------------

def test_the_probe_catches_the_wrong_fit_inside_the_circle(tmp_path):
    """max() spans the long side across the diameter and lets the corners be
    clipped; hypot() is the strict no-clip fit, which on a square image spends
    29% of the diameter protecting corners that are background. Either is a
    defensible choice and both draw a map -- so only a test can hold the one
    the geometry is actually written against."""
    mutated = _mutate(tmp_path, "size / Math.max(width, height)", "size / Math.hypot(width, height)")
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


def test_the_probe_catches_an_indicator_that_ignores_aspect(tmp_path):
    """OSD viewport y runs over [0, height/width], not [0, 1]. Dropping the
    normalisation leaves an indicator that is right on a square image and
    silently wrong on every other one."""
    mutated = _mutate(tmp_path, "bounds.y / geom.aspect", "bounds.y")
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


def test_the_probe_catches_an_unclamped_indicator(tmp_path):
    """Zoomed out past the image, OSD reports bounds outside [0, 1]. Unclamped,
    the rectangle spills into the dead space around the image instead of
    reading "you can see all of it"."""
    mutated = _mutate(
        tmp_path,
        "const left = MiniMap._clamp01(bounds.x);",
        "const left = bounds.x;",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


# -- colour --------------------------------------------------------------

def test_the_probe_catches_a_brightness_that_drifts_from_the_shader(tmp_path):
    """0.9 is the alpha frag.glsl emits and canvas `lighter` multiplies in.
    Any other value makes the lens a different brightness from the viewer it
    is supposed to be an overview of."""
    mutated = _mutate(tmp_path, "static TILE_ALPHA = 0.9;", "static TILE_ALPHA = 1.0;")
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


def test_the_probe_catches_a_missing_contrast_clamp(tmp_path):
    """Without the clamp, values below the window go negative and values above
    it keep climbing -- the window stops being a window."""
    mutated = _mutate(
        tmp_path,
        "const clamped = Math.min(Math.max((value / 255 - low) / span, 0), 1);",
        "const clamped = (value / 255 - low) / span;",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


def test_the_probe_catches_hd_ranges_left_in_raw_units(tmp_path):
    """In HD, slot.range is in raw 16-bit units while the overview bytes are
    quantized into [0, 255]. Skipping the conversion collapses almost every
    channel to black -- and only in HD, which is the mode nobody has open
    while developing."""
    mutated = _mutate(
        tmp_path,
        "range = sidebar.rawToByteRange(range, packet);",
        "range = range;",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


def test_the_probe_catches_a_zero_width_window_dividing_by_zero(tmp_path):
    """GLSL's clamp absorbs 0/0. JS gives NaN, which a Uint8ClampedArray stores
    as 0 -- a black map with nothing in the console to explain it."""
    mutated = _mutate(tmp_path, "Math.max(high - low, 1 / 255)", "high - low")
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


# -- lifecycle -----------------------------------------------------------

def test_the_probe_catches_work_done_while_the_lens_is_shut(tmp_path):
    """The entire performance argument for this feature is that it costs
    nothing until opened. A guard that stops guarding gives that away silently:
    everything still looks and behaves correct."""
    mutated = _mutate(
        tmp_path,
        "    invalidate(options) {\n        if (!this.expanded) {\n            return;\n        }",
        "    invalidate(options) {\n        if (false) {\n            return;\n        }",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


def test_the_probe_catches_an_animation_handler_that_stops_being_guarded(tmp_path):
    """Registered once and guarded, rather than added on expand and removed on
    collapse, because OSD's removeHandler needs the identical reference and an
    add/remove pair leaks a closure per cycle. If the guard goes, the closure
    runs getBounds() on every frame with the lens shut."""
    mutated = _mutate(
        tmp_path,
        "this._onAnimation = () => {\n            if (this.expanded) {",
        "this._onAnimation = () => {\n            if (true) {",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


def test_the_probe_catches_a_cache_that_stops_caching(tmp_path):
    """Reopening the lens, or changing a colour, must not go back to the
    server. Refetching is invisible on a fast local connection and is the
    difference between this feature being free and being a request storm every
    time a contrast slider moves."""
    mutated = _mutate(
        tmp_path,
        "(channel) => !this._gray.has(channel.srcIdx) && !this._pending.has(channel.srcIdx)",
        "(channel) => true",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert result["failures"]


# -- wiring --------------------------------------------------------------

def test_the_mini_map_is_loaded_before_the_viewer_that_constructs_it():
    """ImageViewer's constructor reaches for MiniMap, so the script has to be
    on the page. Both are plain <script>s, and `class` in a classic script is a
    lexical binding rather than a property of window -- which is why the call
    site tests `typeof`, and why a missing tag would be a silent no-op rather
    than an error."""
    markup = TEMPLATE.read_text(encoding="utf-8")
    assert "views/miniMap.js?v=" in markup
    viewer = (VIEWS / "imageViewer.js").read_text(encoding="utf-8")
    assert 'typeof MiniMap === "undefined"' in viewer
    assert "if (window.MiniMap" not in viewer


def test_the_channel_mutators_tell_the_mini_map():
    """Three call sites, and the distinction between them matters: only a
    change to WHICH channels are on can need a request. A colour or a contrast
    change that refetched would put the network on a slider drag."""
    viewer = (VIEWS / "imageViewer.js").read_text(encoding="utf-8")

    def body(name):
        return viewer.split(f"    {name}(", 1)[1].split("\n    }", 1)[0]

    assert "this.miniMap?.invalidate({ refetch: true });" in body("updateActiveChannels")
    for name in ("updateChannelRange", "updateChannelColors"):
        assert "this.miniMap?.invalidate();" in body(name), name
        assert "refetch" not in body(name), name


# -- the empty state -----------------------------------------------------

def test_the_probe_catches_a_silent_failure(tmp_path):
    """The bug this exists for: a black circle is indistinguishable from dark
    tissue, so a mini-map that fails to load anything and says nothing reads as
    working. That is exactly what a Plexora updated underneath a live waitress
    process does -- server_cli has no reloader and Flask binds routes at import,
    so /generated/overview 404s while the new templates and static JS load
    normally."""
    mutated = _mutate(
        tmp_path,
        "if (channels.length && !cached && !pending && failed.length) {",
        "if (false) {",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert any("says so" in f["name"] for f in result["failures"]), result["failures"]


def test_the_probe_catches_the_note_naming_the_wrong_cause(tmp_path):
    """404 means one thing here -- the running server predates the route -- and
    saying "restart the server" for a 500 would send people to reboot a healthy
    process instead of reading the traceback it just logged."""
    mutated = _mutate(tmp_path, "const missingRoute = failed.every(", "const missingRoute = true || failed.every(")
    returncode, result = _run(mutated)
    assert returncode != 0
    assert any("blame the server version" in f["name"] for f in result["failures"]), result["failures"]


def test_the_probe_catches_a_note_that_cries_wolf_on_a_partial_load(tmp_path):
    """One channel failing while another works is a missing colour, not a broken
    map. Keying the note on "something failed" rather than "nothing loaded"
    would cover a perfectly good overview with an error message."""
    mutated = _mutate(
        tmp_path,
        "if (channels.length && !cached && !pending && failed.length) {",
        "if (channels.length && !pending && failed.length) {",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert any("stays silent" in f["name"] for f in result["failures"]), result["failures"]


def test_the_probe_catches_a_note_that_flashes_mid_load(tmp_path):
    """A channel that fails early while a slower one is still in flight is not
    a failed map yet. Without the in-flight guard every slow load that ends up
    working would flash an error first."""
    mutated = _mutate(
        tmp_path,
        "if (channels.length && !cached && !pending && failed.length) {",
        "if (channels.length && !cached && failed.length) {",
    )
    returncode, result = _run(mutated)
    assert returncode != 0
    assert any("still loading" in f["name"] for f in result["failures"]), result["failures"]


def test_the_failure_record_is_read_across_every_channel_not_just_one(tmp_path):
    """The reason failures are a per-channel map and not one last-error field.

    With a scalar, two channels failing with different statuses leave whichever
    finished last in the field, so the same breakage says "restart the server"
    or "something else is wrong" depending on network timing. `every` is what
    makes the answer depend on all of them; `some` is the plausible-looking
    version that tells a user with a 500 to go reboot a healthy process."""
    mutated = _mutate(
        tmp_path,
        "const missingRoute = failed.every(",
        "const missingRoute = failed.some(",
    )
    returncode, result = _run(mutated)
    assert returncode != 0, "a scalar-equivalent failure record went undetected"
    assert any("mixed with a 500" in f["name"] for f in result["failures"]), result["failures"]
