"""Which encoders this server has, and the writers that turn frames into files.

Nothing in core writes H.264, so `encoders` finds out what a machine can do by
trying: ffmpeg from PLEXORA_FFMPEG, the `plexora[video]` extra or PATH, and
OpenCV probed by an actual write (its VideoWriter reports success for codecs it
then silently fails to write). The Export dialog shows a format no path can
write disabled WITH the reason, so a row's reason is part of the contract.

Availability differs between machines, so these tests assert consistency, not
a particular answer: a row that says available has what it needs, PNG frames
and the Pillow GIF are always there, and with discovery forced empty every
ffmpeg format says how to get one. The ffmpeg writers themselves are exercised
only when an ffmpeg is discoverable here (set PLEXORA_FFMPEG to run them);
each output is decoded again and its frames counted.
"""

import io
import re
import subprocess
import sys
import zipfile

import pytest
from PIL import Image

from plexora.plugins.video_studio.server import encoders, writers
from plexora.plugins.video_studio.server.writers import (Cv2Writer, FfmpegWriter, PillowGifWriter,
                                                          PngZipWriter, WriterError)

W, H = 64, 48


@pytest.fixture(autouse=True)
def fresh_probe():
    """The report is cached per process; each test probes for itself."""
    encoders.reset_for_tests()
    yield
    encoders.reset_for_tests()


def _no_ffmpeg(monkeypatch):
    monkeypatch.setattr(encoders, "_ffmpeg_candidates", lambda: iter(()))


def _frame(i, width=W, height=H, alpha=255):
    """A frame whose colour encodes its index, so frames never repeat."""
    r, g, b = (i * 40) % 256, (255 - i * 30) % 256, (i * 90 + 17) % 256
    return bytes([r, g, b, alpha]) * (width * height)


def _rows(report=None):
    return {(row["format"], row["writer"]): row for row in (report or encoders.report())["formats"]}


# -- the report ---------------------------------------------------------------------------


def test_the_report_has_one_row_per_format_and_writer():
    report = encoders.report()
    assert set(report) == {"ffmpeg", "cv2", "formats"}
    rows = _rows(report)
    assert set(rows) == {("mp4", "ffmpeg"), ("webm", "ffmpeg"), ("mov", "ffmpeg"),
                         ("gif", "ffmpeg"), ("mp4", "cv2"), ("gif", "pillow"),
                         ("png", "pngzip")}
    for row in rows.values():
        assert set(row) == {"format", "writer", "label", "available", "reason", "alpha",
                            "codec", "side"}
        assert row["side"] == "server" and row["label"]
        # A disabled format always says why; an enabled one says nothing.
        assert bool(row["reason"]) is not row["available"], row
    assert isinstance(report["ffmpeg"]["available"], bool)
    assert isinstance(report["ffmpeg"]["encoders"], list)


def test_png_frames_and_the_pillow_gif_are_always_available(monkeypatch):
    _no_ffmpeg(monkeypatch)
    monkeypatch.setattr(encoders, "_probe_cv2", lambda: {"available": False, "reason": "no"})
    rows = _rows(encoders.report(refresh=True))
    assert rows[("png", "pngzip")]["available"] and rows[("png", "pngzip")]["alpha"]
    assert rows[("gif", "pillow")]["available"]


def test_the_cv2_row_reflects_an_actual_probe():
    report = encoders.report()
    cv2 = report["cv2"]
    row = _rows(report)[("mp4", "cv2")]
    assert row["available"] is cv2["available"]
    if cv2["available"]:
        assert cv2["fourcc"] in ("avc1", "mp4v")
        assert cv2["codec"] in ("H.264", "MPEG-4 Part 2") and row["codec"] == cv2["codec"]
    else:
        assert cv2["reason"] and row["reason"] == cv2["reason"]


def test_the_report_is_cached_until_refreshed(monkeypatch):
    calls = []
    monkeypatch.setattr(encoders, "_probe_ffmpeg",
                        lambda: calls.append(1) or {"available": False, "encoders": []})
    monkeypatch.setattr(encoders, "_probe_cv2", lambda: {"available": False, "reason": "x"})
    first = encoders.report()
    assert encoders.report() is first
    assert len(calls) == 1
    encoders.report(refresh=True)
    assert len(calls) == 2


def test_a_plexora_ffmpeg_that_does_not_exist_is_ignored(monkeypatch, tmp_path):
    missing = tmp_path / "no-such-ffmpeg"
    monkeypatch.setenv("PLEXORA_FFMPEG", str(missing))
    # Nothing else to fall back on, so the answer is certain on any machine.
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    monkeypatch.setattr(encoders.shutil, "which", lambda name: None)
    candidates = list(encoders._ffmpeg_candidates())
    assert candidates == [(str(missing), "PLEXORA_FFMPEG")]
    ffmpeg = encoders.report(refresh=True)["ffmpeg"]
    assert ffmpeg == {"available": False, "encoders": []}


def test_a_plexora_ffmpeg_that_is_not_ffmpeg_is_skipped(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PLEXORA_FFMPEG", str(fake))
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    monkeypatch.setattr(encoders.shutil, "which", lambda name: None)
    assert encoders.report(refresh=True)["ffmpeg"]["available"] is False


def test_without_ffmpeg_every_ffmpeg_format_says_how_to_get_it(monkeypatch):
    _no_ffmpeg(monkeypatch)
    rows = _rows(encoders.report(refresh=True))
    for fmt in ("mp4", "webm", "mov", "gif"):
        row = rows[(fmt, "ffmpeg")]
        assert row["available"] is False
        assert "plexora[video]" in row["reason"], row
    assert encoders.pick("webm") is None
    assert encoders.pick("mov") is None
    assert encoders.pick("png")["writer"] == "pngzip"
    assert encoders.pick("gif")["writer"] == "pillow"
    mp4 = encoders.pick("mp4")
    assert mp4 is None or mp4["writer"] == "cv2"


def test_an_ffmpeg_without_an_encoder_names_the_missing_encoder(monkeypatch):
    monkeypatch.setattr(encoders, "_probe_ffmpeg", lambda: {
        "available": True, "path": "/x/ffmpeg", "origin": "PATH", "version": "7",
        "encoders": ["gif"]})
    rows = _rows(encoders.report(refresh=True))
    assert rows[("mp4", "ffmpeg")]["reason"] == "This ffmpeg has no H.264 encoder."
    assert rows[("webm", "ffmpeg")]["reason"] == "This ffmpeg has no VP9 encoder."
    assert rows[("mov", "ffmpeg")]["reason"] == "This ffmpeg has no ProRes encoder."
    assert rows[("gif", "ffmpeg")]["available"] is True
    # The best writer for a format comes first.
    assert encoders.pick("gif")["writer"] == "ffmpeg"
    assert encoders.pick("gif", "pillow")["writer"] == "pillow"


def test_videotoolbox_counts_as_h264(monkeypatch):
    monkeypatch.setattr(encoders, "_probe_ffmpeg", lambda: {
        "available": True, "path": "/x/ffmpeg", "encoders": ["h264_videotoolbox"]})
    assert encoders.pick("mp4")["writer"] == "ffmpeg"


def test_pick_with_a_named_writer_returns_exactly_that_writer_or_nothing():
    assert encoders.pick("png", "pngzip")["writer"] == "pngzip"
    assert encoders.pick("png", "ffmpeg") is None
    assert encoders.pick("avi") is None


# -- PNG frames -----------------------------------------------------------------------------


def test_png_frames_are_numbered_and_opaque_by_default(tmp_path):
    writer = PngZipWriter(tmp_path, "clip", W, H, 10, {})
    for i in range(5):
        writer.write(_frame(i, alpha=128))
    path = writer.close()
    assert path == tmp_path / "clip.zip" and writer.frames == 5
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert names == [f"clip/frame_{i:06d}.png" for i in range(5)]
        image = Image.open(io.BytesIO(archive.read(names[3])))
        assert image.mode == "RGB" and image.size == (W, H)
        assert image.getpixel((0, 0)) == tuple(_frame(3)[:3])
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())


def test_transparent_png_frames_keep_their_alpha(tmp_path):
    writer = PngZipWriter(tmp_path, "clip", W, H, 10, {"transparent": True})
    writer.write(_frame(1, alpha=77))
    with zipfile.ZipFile(writer.close()) as archive:
        image = Image.open(io.BytesIO(archive.read("clip/frame_000000.png")))
        assert image.mode == "RGBA"
        assert image.getpixel((5, 5))[3] == 77


def test_png_bytes_from_the_browser_are_stored_untouched(tmp_path):
    writer = PngZipWriter(tmp_path, "clip", W, H, 10, {})
    writer.write_png(b"\x89PNG-as-sent")
    with zipfile.ZipFile(writer.close()) as archive:
        assert archive.read("clip/frame_000000.png") == b"\x89PNG-as-sent"


# -- frame size ------------------------------------------------------------------------------


def _writers(tmp_path):
    yield PngZipWriter(tmp_path, "a", W, H, 10, {})
    yield PillowGifWriter(tmp_path, "b", W, H, 10, {})
    if encoders.report()["cv2"]["available"]:
        yield Cv2Writer(tmp_path, "c", W, H, 10, {},
                        fourcc=encoders.report()["cv2"]["fourcc"])


def test_a_frame_of_the_wrong_size_is_refused_by_every_writer(tmp_path):
    for writer in _writers(tmp_path):
        with pytest.raises(WriterError) as info:
            writer.write(b"\x00" * (W * H * 4 - 4))
        assert f"{W}x{H}x4" in str(info.value)
        assert writer.frames == 0
        writer.abort()


# -- GIF without ffmpeg -------------------------------------------------------------------------


def test_a_pillow_gif_has_one_frame_per_frame_written(tmp_path):
    writer = PillowGifWriter(tmp_path, "clip", W, H, 10, {})
    for i in range(4):
        writer.write(_frame(i))
    path = writer.close()
    with Image.open(path) as gif:
        assert gif.format == "GIF" and gif.size == (W, H)
        assert gif.n_frames == 4


def test_a_pillow_gif_is_capped_in_size_and_length(tmp_path, monkeypatch):
    with pytest.raises(WriterError):
        PillowGifWriter(tmp_path, "big", writers.PILLOW_GIF_MAX_SIDE + 2, 64, 10, {})
    monkeypatch.setattr(writers, "PILLOW_GIF_MAX_FRAMES", 3)
    writer = PillowGifWriter(tmp_path, "long", W, H, 10, {})
    for i in range(3):
        writer.write(_frame(i))
    with pytest.raises(WriterError) as info:
        writer.write(_frame(3))
    assert "3 frames" in str(info.value)
    with pytest.raises(WriterError):
        PillowGifWriter(tmp_path, "empty", W, H, 10, {}).close()


# -- OpenCV ---------------------------------------------------------------------------------


def _count_cv2_frames(path):
    import cv2

    capture = cv2.VideoCapture(str(path))
    count = 0
    while True:
        ok, _ = capture.read()
        if not ok:
            break
        count += 1
    capture.release()
    return count


def test_the_opencv_writer_writes_a_readable_mp4_with_every_frame(tmp_path):
    cv2_report = encoders.report()["cv2"]
    if not cv2_report["available"]:
        pytest.skip(f"OpenCV cannot write an MP4 here: {cv2_report['reason']}")
    writer = Cv2Writer(tmp_path, "clip", W, H, 10, {}, fourcc=cv2_report["fourcc"])
    for i in range(6):
        writer.write(_frame(i))
    path = writer.close()
    assert path.suffix == ".mp4" and path.stat().st_size > 0
    assert _count_cv2_frames(path) == 6


# -- open_writer -------------------------------------------------------------------------------


def test_open_writer_builds_the_writer_a_row_names_and_creates_its_directory(tmp_path):
    target = tmp_path / "exports" / "exp_1"
    writer = writers.open_writer({"writer": "pngzip", "format": "png"}, target, "clip", W, H,
                                 10, {})
    assert isinstance(writer, PngZipWriter) and target.is_dir()
    writer.abort()
    assert isinstance(writers.open_writer({"writer": "pillow", "format": "gif"}, target, "g",
                                          W, H, 10, {}), PillowGifWriter)
    with pytest.raises(WriterError):
        writers.open_writer({"writer": "telepathy", "format": "png"}, target, "x", W, H, 10, {})


def test_bitrate_scales_with_pixels_and_rate():
    assert writers.bitrate_mbps("good", 1920, 1080, 30) == 8
    assert writers.bitrate_mbps("good", 3840, 2160, 60) == 64
    assert writers.bitrate_mbps("unknown", 1920, 1080, 30) == 8


# -- ffmpeg (only where one is discoverable) ------------------------------------------------------


def _ffmpeg():
    report = encoders.report()["ffmpeg"]
    if not report["available"]:
        pytest.skip("no ffmpeg discoverable here (set PLEXORA_FFMPEG to run these)")
    return report


def _decoded_frames(ffmpeg, path):
    """Decode the file with ffmpeg itself and read the frame counter."""
    done = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-progress", "pipe:1",
                           "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    counts = re.findall(r"^frame=(\d+)$", done.stdout, re.MULTILINE)
    assert counts, done.stdout[-2000:]
    return int(counts[-1])


FFMPEG_FORMATS = {"mp4": ("libx264", "h264_videotoolbox"), "webm": ("libvpx-vp9",),
                  "mov": ("prores_ks",), "gif": ("gif",)}


@pytest.mark.parametrize("fmt", sorted(FFMPEG_FORMATS))
@pytest.mark.parametrize("transparent", [False, True])
def test_ffmpeg_writes_every_frame_in_each_format(tmp_path, fmt, transparent):
    report = _ffmpeg()
    if not set(FFMPEG_FORMATS[fmt]) & set(report["encoders"]):
        pytest.skip(f"this ffmpeg has no encoder for {fmt}")
    writer = FfmpegWriter(tmp_path, "clip", W, H, 10,
                          {"quality": "draft", "transparent": transparent},
                          ffmpeg=report["path"], encoders=report["encoders"], fmt=fmt)
    for i in range(7):
        writer.write(_frame(i))
    path = writer.close()
    assert path == tmp_path / f"clip.{fmt}" and path.stat().st_size > 0
    # The fragmented intermediate is gone once the MP4 is finished.
    assert not (tmp_path / "partial.mp4").exists()
    assert _decoded_frames(report["path"], path) == 7
    described = writer.describe()
    assert described["encoder"] == "ffmpeg" and described["frames"] == 7
    assert described["codec"] in FFMPEG_FORMATS[fmt]
    if fmt == "webm":
        assert ("yuva420p" if transparent else "yuv420p") in writer.args


def test_ffmpeg_honours_an_explicit_bitrate(tmp_path):
    report = _ffmpeg()
    if "libx264" not in report["encoders"]:
        pytest.skip("this ffmpeg has no libx264")
    writer = FfmpegWriter(tmp_path, "clip", W, H, 10, {"bitrate_mbps": 2},
                          ffmpeg=report["path"], encoders=report["encoders"], fmt="mp4")
    assert writer.args[writer.args.index("-b:v") + 1] == "2M"
    writer.abort()


def test_an_ffmpeg_failure_raises_writer_error_with_its_last_lines(tmp_path, monkeypatch):
    report = _ffmpeg()
    monkeypatch.setattr(FfmpegWriter, "_codec_args",
                        lambda self: ["-c:v", "no_such_codec_for_this_test"])
    writer = FfmpegWriter(tmp_path, "clip", W, H, 10, {}, ffmpeg=report["path"],
                          encoders=report["encoders"], fmt="webm")
    with pytest.raises(WriterError) as info:
        for i in range(40):
            writer.write(_frame(i))
        writer.close()
    assert "no_such_codec_for_this_test" in info.value.tail


def test_ffmpeg_that_cannot_start_is_a_writer_error(tmp_path):
    with pytest.raises(WriterError):
        FfmpegWriter(tmp_path, "clip", W, H, 10, {}, ffmpeg=str(tmp_path / "missing"),
                     encoders=("libx264",), fmt="mp4")
