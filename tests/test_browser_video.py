"""Video Studio's exports on the website, held to the desktop's (plan L10).

tests/browser/video.mjs opens one small calibrated image in each app -- the
static site with the website's Python bundle, and a CPython Plexora with the
desktop's FFmpeg -- makes Video Studio's zoom preset from it with the same
channel windows, and exports every format from the real panel the way its
Export dialog would, each file caught as the user's download.
tests/browser/video_check.py then opens every file (FFmpeg for the movies,
the zip and Pillow for PNG frames and GIF) and compares the frames: each
export against its own app's PNG frames (what the encoder changed), and each
website export against the desktop's (whether the two apps give the same
video).

The desktop arm needs an FFmpeg (PLEXORA_FFMPEG, the `plexora[video]` extra,
or one on PATH); without one only the website is exported and checked.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "video.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static" / "index.html"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_file() or not PLAYWRIGHT.is_dir(),
    reason="needs node, a static build (python -m plexora.client.static_build), "
           "the website's Python bundle (tools/browser_build/build.py) and playwright-core")

sys.path.insert(0, str(ROOT / "tests" / "browser"))
try:
    import video_check  # noqa: E402
finally:
    sys.path.pop(0)

FRAMES = 30          # 1 s at 30 fps
GIF_FRAMES = 15      # 1 s at 15 fps: a GIF's delays are in hundredths of a second
SIZE = [320, 180]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("video")
    arms = "website,desktop" if video_check.ffmpeg_exe() else "website"
    done = subprocess.run(["node", str(DRIVER), "--out", str(out), "--arms", arms,
                           "--size", "x".join(map(str, SIZE)), "--duration", "1", "--fps", "30"],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=1800,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail(f"video.mjs wrote no record: {done.stderr[-3000:]}")
    for arm, data in record["arms"].items():
        assert not data.get("error"), (arm, data.get("error"), done.stderr[-2000:])
    return record, video_check.check(out)


def _exports(record, arm):
    if arm not in record["arms"]:
        pytest.skip(f"no {arm} arm (the desktop's needs an FFmpeg)")
    return record["arms"][arm]["exports"]


def test_the_website_exports_every_format_but_prores_from_the_panel(run):
    record, _ = run
    exports = _exports(record, "website")
    routes = {fmt: (row.get("route") or {}).get("route") + "/" + (row.get("route") or {}).get("writer")
              for fmt, row in exports.items() if row.get("ok")}
    assert routes == {"mp4": "browser/webcodecs", "webm": "browser/webcodecs", "gif": "browser/gif",
                      "png": "server/pngzip"}, exports


def test_each_website_file_is_what_it_says(run):
    _, checked = run
    files = checked["files"]
    gif = files["website-gif"]
    assert gif["gif"] == "GIF89a" and gif["loop"] == 0, gif
    assert (gif["frames"], gif["frame_size"]) == (GIF_FRAMES, SIZE)
    assert abs(gif["duration_ms"] - 1000) <= 20, gif
    png = files["website-png"]
    assert png["zip"] and (png["frames"], png["frame_size"]) == (FRAMES, SIZE), png
    if not checked["ffmpeg"]:
        pytest.skip("no FFmpeg here to open the movies")
    for fmt, codec, container in (("mp4", "h264", "mp4"), ("webm", "vp9", "webm")):
        movie = files[f"website-{fmt}"]
        assert movie["codec"] == codec and container in movie["container"], movie
        assert (movie["size"], movie["frames"]) == (SIZE, FRAMES), movie


def test_prores_on_the_website_is_the_desktop_apps_notice(run):
    """No LGPL FFmpeg for the web writes ProRes: the dialog shows the
    website's hand-off notice, and no file is made."""
    from plexora.browser import routes

    record, _ = run
    notice = routes.FEATURES["video_studio.prores"]
    mov = _exports(record, "website")["mov"]
    assert mov["ok"] is False and mov["unavailable"] and mov["reason"] == notice.why, mov
    shown = record["arms"]["website"]["dialog"]["notice"]
    assert shown["title"] == notice.title and shown["why"] == notice.why, shown
    assert shown["actions"] == list(notice.actions)
    row = next(r for r in record["arms"]["website"]["dialog"]["rows"] if r["format"] == "mov")
    assert row["handoff"] and not row["disabled"] and row["how"] == notice.title, row


def test_the_desktop_writes_prores_4444_with_alpha(run):
    record, checked = run
    exports = _exports(record, "desktop")
    assert exports["mov"]["ok"] and exports["mov"]["route"]["writer"] == "ffmpeg", exports["mov"]
    mov = checked["files"]["desktop-mov"]
    assert mov["codec"] == "prores" and mov["profile"] == "4444", mov
    assert mov["pix_fmt"].startswith("yuva444"), mov
    assert (mov["size"], mov["frames"]) == (SIZE, FRAMES), mov
    # Transparent where nothing is drawn, opaque where the rings are.
    assert mov["alpha"]["min"] == 0 and mov["alpha"]["max"] == 255, mov
    assert 0.05 < mov["alpha"]["transparent_share"] < 0.995, mov


#: Mean absolute difference per RGB channel (0-255), over every frame, from
#: the frames as rendered (the same app's PNG frames): what each encoder may
#: change. Measured 2026-11-07 at 320 x 180 (docs/internal/bench/browser_node/L10.md).
FIDELITY = {"mp4": 4.0, "webm": 4.0, "gif": 8.0, "mov": 1.0}


@pytest.mark.parametrize("arm", ["website", "desktop"])
def test_every_export_keeps_the_rendered_frames(run, arm):
    record, checked = run
    _exports(record, arm)
    if not checked["ffmpeg"]:
        pytest.skip("no FFmpeg here to open the movies")
    for key, diff in checked["fidelity"].items():
        if not key.startswith(arm + "-"):
            continue
        fmt = key.split("-", 1)[1]
        assert diff.get("mean_abs") is not None and diff["mean_abs"] <= FIDELITY[fmt], (key, diff)


#: Website against desktop, the same scene: PNG frames and the WebCodecs
#: movies are the same pipeline on both (the frames render alike, the
#: browser encodes them: measured identical, the WebM byte for byte, on an
#: idle machine); the GIF is FFmpeg's palettegen/paletteuse on the desktop and
#: the browser's own on the website, two quantisers (measured 1.6 apart).
#: The movies' bar is not "identical", though: two encodes, each within its
#: FIDELITY of the frames it was given, may differ by both losses, and Chrome's
#: H.264 encoder (hardware, when the GPU has one free) does not repeat itself
#: under load -- a full run of the browser tests measured MP4 1.55 apart while
#: the PNG frames still matched. The frames' 0.5 is what says the picture is
#: the same; 2.5 says neither encode went further from it than an encoder does.
PARITY = {"png": 0.5, "mp4": 2.5, "webm": 2.5, "gif": 4.0}


def test_the_website_exports_what_the_desktop_exports(run):
    record, checked = run
    _exports(record, "desktop")
    if not checked["ffmpeg"]:
        pytest.skip("no FFmpeg here to open the movies")
    assert set(checked["parity"]) == set(PARITY), checked["parity"]
    for fmt, diff in checked["parity"].items():
        assert diff.get("mean_abs") is not None and diff["mean_abs"] <= PARITY[fmt], (fmt, diff)
