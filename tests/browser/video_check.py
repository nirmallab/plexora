"""What tests/browser/video.mjs exported, opened and compared (plan L10).

    python tests/browser/video_check.py OUT_DIR

Reads OUT_DIR/record.json and the files beside it. Every movie is decoded
with FFmpeg (PLEXORA_FFMPEG, the `plexora[video]` extra, or one on PATH) --
container, codec, pixel format, size, frame count, and every frame as RGBA;
the PNG frames are read from their zip and a GIF with Pillow, so those two
need no FFmpeg. Then:

- each export against its own arm's PNG frames (the frames as rendered,
  lossless): how much the encoder changed them;
- each website export against the desktop's of the same format: whether the
  two apps give the same file, frame for frame.

Prints (and returns) {files: {arm-format: facts}, fidelity: {...}, parity: {...}}.
Differences are the mean absolute difference per RGB channel, 0-255, and the
PSNR in dB, over every frame.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageSequence

EXT = {"mp4": "mp4", "webm": "webm", "mov": "mov", "gif": "gif", "png": "zip"}


def ffmpeg_exe() -> str | None:
    explicit = os.environ.get("PLEXORA_FFMPEG")
    if explicit and Path(explicit).is_file():
        return explicit
    try:
        import imageio_ffmpeg  # noqa: PLC0415

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed
        return shutil.which("ffmpeg")


_STREAM = re.compile(r"Stream #0:\d+.*?: Video: (?P<codec>\w+)(?: \((?P<profile>[^)]*)\))?"
                     r"(?: \([^)]*\))*, (?P<pix>\w+)(?:\([^)]*\))?, (?P<w>\d+)x(?P<h>\d+)")


def probe(path: Path, ffmpeg: str) -> dict:
    """FFmpeg's description of the file's first video stream."""
    text = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stderr
    container = re.search(r"Input #0, ([\w,]+), from", text)
    stream = _STREAM.search(text)
    if not stream:
        return {"container": container.group(1) if container else None, "error": text[-400:]}
    return {"container": container.group(1) if container else None, "codec": stream["codec"],
            "profile": stream["profile"], "pix_fmt": stream["pix"],
            "size": [int(stream["w"]), int(stream["h"])]}


def decode(path: Path, ffmpeg: str, width: int, height: int) -> np.ndarray:
    """Every frame as RGBA, (frames, height, width, 4)."""
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba",
                          "-"], capture_output=True, check=True).stdout
    frame = width * height * 4
    return np.frombuffer(raw[:len(raw) - len(raw) % frame], np.uint8).reshape(-1, height, width, 4)


def png_frames(path: Path) -> np.ndarray:
    with zipfile.ZipFile(path) as archive:
        names = sorted(n for n in archive.namelist() if n.endswith(".png"))
        return np.stack([np.asarray(Image.open(io.BytesIO(archive.read(n))).convert("RGBA"))
                         for n in names])


def gif_frames(path: Path) -> tuple[np.ndarray, dict]:
    with Image.open(path) as image:
        info = {"loop": image.info.get("loop"), "version": image.info.get("version", b"").decode()}
        frames = [np.asarray(f.convert("RGBA")) for f in ImageSequence.Iterator(image)]
        durations = []
        image.seek(0)
        for i in range(len(frames)):
            image.seek(i)
            durations.append(image.info.get("duration"))
        info["duration_ms"] = sum(d or 0 for d in durations)
    return np.stack(frames), info


def _over_black(frames: np.ndarray) -> np.ndarray:
    """RGB as seen over black: a transparent export (ProRes 4444) against an
    opaque one drawn on the viewer's black ground."""
    rgb = frames[..., :3].astype(np.float64)
    return rgb * (frames[..., 3:4].astype(np.float64) / 255.0)


def difference(a: np.ndarray, b: np.ndarray) -> dict:
    """Mean absolute difference and PSNR over RGB, frame for frame. When `b`
    has a whole multiple of `a`'s frames (a GIF at 15 fps against PNG frames
    at 30), `a`'s frame k is `b`'s frame k * step: the same moment."""
    step = len(b) // len(a) if len(a) and len(b) > len(a) and len(b) % len(a) == 0 else 1
    b = b[::step]
    n = min(len(a), len(b))
    if not n or a.shape[1:3] != b.shape[1:3]:
        return {"frames": [len(a), len(b)], "error": "nothing to compare"}
    x = _over_black(a[:n])
    y = _over_black(b[:n])
    mse = float(((x - y) ** 2).mean())
    return {"frames": [len(a), len(b)], "step": step, "mean_abs": round(float(np.abs(x - y).mean()), 3),
            "max_frame_mean_abs": round(float(np.abs(x - y).mean(axis=(1, 2, 3)).max()), 3),
            "psnr_db": round(10 * np.log10(255 ** 2 / mse), 2) if mse > 0 else None}


def read(out: Path, arm: str, fmt: str, row: dict, ffmpeg: str | None, size) -> tuple[dict, np.ndarray | None]:
    path = out / (row.get("file") or f"{arm}-{fmt}.{EXT[fmt]}")
    facts = {"bytes": path.stat().st_size if path.is_file() else None}
    if not path.is_file():
        return {**facts, "missing": True}, None
    head = path.read_bytes()[:12]
    facts["magic"] = head[:6].hex()
    frames = None
    if fmt == "png":
        facts["zip"] = head[:2] == b"PK"
        frames = png_frames(path) if facts["zip"] else None
    elif fmt == "gif":
        facts["gif"] = head[:6]
        facts["gif"] = facts["gif"].decode("latin-1")
        if facts["gif"].startswith("GIF8"):
            frames, info = gif_frames(path)
            facts.update(info)
    elif ffmpeg:
        facts.update(probe(path, ffmpeg))
        if "codec" in facts:
            frames = decode(path, ffmpeg, *facts["size"])
    if frames is not None:
        facts["frames"] = int(len(frames))
        facts["frame_size"] = [int(frames.shape[2]), int(frames.shape[1])]
        alpha = frames[..., 3]
        facts["alpha"] = {"min": int(alpha.min()), "max": int(alpha.max()),
                          "transparent_share": round(float((alpha < 255).mean()), 4)}
    return facts, frames


def check(out_dir) -> dict:
    out = Path(out_dir)
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    ffmpeg = ffmpeg_exe()
    size = record["size"]
    files, frames = {}, {}
    for arm, data in record["arms"].items():
        for fmt, row in (data.get("exports") or {}).items():
            if not row.get("ok"):
                continue
            files[f"{arm}-{fmt}"], frames[(arm, fmt)] = read(out, arm, fmt, row, ffmpeg, size)
    fidelity, parity = {}, {}
    for (arm, fmt), got in frames.items():
        truth = frames.get((arm, "png"))
        if got is not None and truth is not None and fmt != "png":
            fidelity[f"{arm}-{fmt}"] = difference(got, truth)
    for (arm, fmt), got in frames.items():
        other = frames.get(("desktop", fmt))
        if arm == "website" and got is not None and other is not None:
            parity[fmt] = difference(got, other)
    return {"ffmpeg": bool(ffmpeg), "files": files, "fidelity": fidelity, "parity": parity}


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1]), indent=1))
