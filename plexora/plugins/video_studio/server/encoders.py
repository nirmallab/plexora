"""Which video encoders this server actually has, found by trying them.

Nothing in Plexora's core dependencies writes H.264. What a given install can
do depends on what is on the machine, so this module finds out rather than
assumes:

* **ffmpeg** -- `PLEXORA_FFMPEG` if set, else the binary the optional extra
  `plexora[video]` installs (`imageio-ffmpeg`, which the desktop app bundles),
  else one on PATH. Its encoder list is read once (`ffmpeg -encoders`), so
  "MP4" is offered only when libx264 or VideoToolbox is really there.
* **OpenCV** -- a core dependency whose bundled FFmpeg may or may not include an
  H.264 encoder depending on the wheel. Probed by writing two frames and
  reading them back, because `cv2.VideoWriter` reports success for codecs it
  then silently fails to write. Offered as "MP4 (basic)", never the default.
* **PNG frames (zip)** and **GIF (Pillow)** need nothing and are always there.

The browser has its own encoder (WebCodecs), probed on the client. The Export
dialog merges both answers, and a format no path can write is shown disabled
with the reason, never hidden.

ffmpeg is only ever run as a separate process. It is never imported, linked or
loaded into Plexora.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from plexora._subprocess import popen_kwargs

#: Encoder names that matter, as `ffmpeg -encoders` prints them.
WANTED = ("libx264", "h264_videotoolbox", "libvpx-vp9", "prores_ks", "gif", "libaom-av1")

_CACHE: dict = {}
_LOCK = threading.Lock()


def _ffmpeg_candidates():
    explicit = os.environ.get("PLEXORA_FFMPEG")
    if explicit:
        yield explicit, "PLEXORA_FFMPEG"
    try:
        import imageio_ffmpeg  # noqa: PLC0415 - optional extra, imported only here

        yield imageio_ffmpeg.get_ffmpeg_exe(), "plexora[video]"
    except Exception:  # noqa: BLE001 - not installed, or its binary missing
        pass
    found = shutil.which("ffmpeg")
    if found:
        yield found, "PATH"


def _probe_ffmpeg() -> dict:
    for path, origin in _ffmpeg_candidates():
        if not path or not Path(path).is_file():
            continue
        try:
            version = subprocess.run([path, "-hide_banner", "-version"], capture_output=True,
                                     text=True, timeout=20, **popen_kwargs())
            listing = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True,
                                     text=True, timeout=20, **popen_kwargs())
        except (OSError, subprocess.SubprocessError):
            continue
        if version.returncode != 0:
            continue
        names = set()
        for line in listing.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
                names.add(parts[1])
        first = (version.stdout.splitlines() or [""])[0]
        return {"available": True, "path": path, "origin": origin,
                "version": first.replace("ffmpeg version", "").strip().split(" ")[0],
                "encoders": sorted(n for n in names if n in WANTED)}
    return {"available": False, "encoders": []}


def _probe_cv2() -> dict:
    """Write two frames with each H.264-ish fourcc and read them back."""
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {"available": False, "reason": "OpenCV is not installed"}
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    frame[16:48, 16:48] = (40, 200, 120)
    with tempfile.TemporaryDirectory(prefix="plexora-cv2-probe-") as scratch:
        for fourcc in ("avc1", "mp4v"):
            path = os.path.join(scratch, f"probe-{fourcc}.mp4")
            try:
                writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), 10, (64, 64))
                if not writer.isOpened():
                    continue
                writer.write(frame)
                writer.write(frame)
                writer.release()
                if not os.path.isfile(path) or os.path.getsize(path) < 64:
                    continue
                capture = cv2.VideoCapture(path)
                ok, _ = capture.read()
                capture.release()
                if ok:
                    return {"available": True, "fourcc": fourcc,
                            "codec": "H.264" if fourcc == "avc1" else "MPEG-4 Part 2",
                            "version": cv2.__version__}
            except Exception:  # noqa: BLE001 - a codec that crashes is a codec we lack
                continue
    return {"available": False, "reason": "OpenCV could not write an MP4 here"}


def report(*, refresh: bool = False) -> dict:
    """What this server can encode. Cached; `refresh` probes again (a user who
    just installed `plexora[video]` should not have to restart)."""
    with _LOCK:
        if refresh or "report" not in _CACHE:
            ffmpeg = _probe_ffmpeg()
            cv2 = _probe_cv2()
            _CACHE["report"] = {"ffmpeg": ffmpeg, "cv2": cv2, "formats": _formats(ffmpeg, cv2)}
        return _CACHE["report"]


def _formats(ffmpeg: dict, cv2: dict) -> list[dict]:
    """One row per (format, writer), best first within a format."""
    have = set(ffmpeg.get("encoders") or [])
    rows = []

    def row(fmt, writer, label, *, available, reason="", alpha=False, codec=""):
        rows.append({"format": fmt, "writer": writer, "label": label, "available": available,
                     "reason": "" if available else reason, "alpha": alpha, "codec": codec,
                     "side": "server"})

    need = "Needs ffmpeg on the server (pip install 'plexora[video]')."
    h264 = "libx264" in have or "h264_videotoolbox" in have
    row("mp4", "ffmpeg", "MP4 (H.264)", available=h264, codec="H.264",
        reason=need if not ffmpeg.get("available") else "This ffmpeg has no H.264 encoder.")
    row("webm", "ffmpeg", "WebM (VP9)", available="libvpx-vp9" in have, alpha=True, codec="VP9",
        reason=need if not ffmpeg.get("available") else "This ffmpeg has no VP9 encoder.")
    row("mov", "ffmpeg", "ProRes (MOV)", available="prores_ks" in have, alpha=True,
        codec="ProRes 422 HQ / 4444",
        reason=need if not ffmpeg.get("available") else "This ffmpeg has no ProRes encoder.")
    row("gif", "ffmpeg", "GIF", available="gif" in have, codec="GIF", reason=need)
    row("mp4", "cv2", "MP4 (basic)", available=bool(cv2.get("available")),
        codec=cv2.get("codec", ""), reason=cv2.get("reason", ""))
    row("gif", "pillow", "GIF (short clips)", available=True, codec="GIF")
    row("png", "pngzip", "PNG frames (zip)", available=True, alpha=True, codec="PNG")
    return rows


def pick(fmt: str, writer: str | None = None) -> dict | None:
    """The best available server writer for `fmt` (or exactly `writer`)."""
    for entry in report()["formats"]:
        if entry["format"] == fmt and entry["available"] and writer in (None, entry["writer"]):
            return entry
    return None


def reset_for_tests():
    with _LOCK:
        _CACHE.clear()
