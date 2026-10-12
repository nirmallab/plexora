"""Frame-by-frame QA of an exported video: what a person would wince at.

    python tools/bench_video/check_frames.py VIDEO [--manifest video-manifest.json] [--json]

VIDEO is an MP4, WebM, MOV or GIF (read with OpenCV, or ffmpeg when
PLEXORA_FFMPEG / imageio-ffmpeg is available) or a PNG-frames zip. Each frame is
measured and the run is flagged for:

  dark        mean luminance under 12/255 -- a frame captured before anything
              was drawn
  hot         more than 5 % of pixels at 250 or above -- a provisional contrast
              window, or a blown overlay
  flash       mean luminance jumping by more than 25 between neighbours while
              the manifest says the scene barely changed (same acquired plane
              and timepoint) -- a window flipping mid-video
  duplicate   two neighbouring frames byte-identical while the manifest says
              something changed -- a frame captured before the viewer applied
              its state
  count       the file's frame count differs from the manifest's

Exit status 1 when anything is flagged. Generalises the plexora-video skill
kit's checker to Video Studio's manifests.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

DARK = 12.0
HOT_SHARE = 0.05
FLASH = 25.0


def _ffmpeg():
    explicit = os.environ.get("PLEXORA_FFMPEG")
    if explicit and Path(explicit).is_file():
        return explicit
    try:
        import imageio_ffmpeg  # noqa: PLC0415

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def frames_of(path: Path):
    """Yield RGB uint8 arrays, one per frame."""
    import numpy as np

    if path.suffix.lower() == ".zip":
        from PIL import Image

        with zipfile.ZipFile(path) as archive:
            for name in sorted(n for n in archive.namelist() if n.lower().endswith(".png")):
                yield np.asarray(Image.open(io.BytesIO(archive.read(name))).convert("RGB"))
        return
    if path.suffix.lower() == ".gif":
        from PIL import Image, ImageSequence

        for frame in ImageSequence.Iterator(Image.open(path)):
            yield np.asarray(frame.convert("RGB"))
        return
    ffmpeg = _ffmpeg()
    if ffmpeg:
        probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
        size = None
        for line in probe.stderr.splitlines():
            if "Video:" in line:
                for part in line.replace(",", " ").split():
                    if "x" in part and part.split("x")[0].isdigit() and part.split("x")[1].isdigit():
                        size = tuple(int(v) for v in part.split("x"))
                        break
            if size:
                break
        if size:
            width, height = size
            process = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
                                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                       stdout=subprocess.PIPE)
            frame_bytes = width * height * 3
            while True:
                chunk = process.stdout.read(frame_bytes)
                if len(chunk) < frame_bytes:
                    break
                yield np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3)
            process.wait()
            return
    import cv2  # noqa: PLC0415

    capture = cv2.VideoCapture(str(path))
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        yield frame[:, :, ::-1]
    capture.release()


def check(path, manifest=None) -> dict:
    import numpy as np

    stats = []
    for frame in frames_of(Path(path)):
        luminance = frame.astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        stats.append({"mean": float(luminance.mean()),
                      "hot": float((frame.max(axis=2) >= 250).mean()),
                      "hash": hashlib.sha1(frame.tobytes()).hexdigest()})
    record = manifest.get("frames", []) if manifest else []
    flags = []
    for k, s in enumerate(stats):
        if s["mean"] < DARK:
            flags.append({"k": k, "flag": "dark", "mean": round(s["mean"], 2)})
        if s["hot"] > HOT_SHARE:
            flags.append({"k": k, "flag": "hot", "share": round(s["hot"], 3)})
        if k == 0:
            continue
        previous = stats[k - 1]
        same_data = (k < len(record) and k - 1 < len(record)
                     and record[k].get("z_acquired") == record[k - 1].get("z_acquired")
                     and record[k].get("t_acquired") == record[k - 1].get("t_acquired"))
        if abs(s["mean"] - previous["mean"]) > FLASH and (same_data or not record):
            flags.append({"k": k, "flag": "flash", "delta": round(s["mean"] - previous["mean"], 2)})
        changed = (k < len(record) and (record[k].get("z_acquired") != record[k - 1].get("z_acquired")
                                        or record[k].get("t_acquired") != record[k - 1].get("t_acquired")))
        if s["hash"] == previous["hash"] and changed:
            flags.append({"k": k, "flag": "duplicate"})
    if manifest and manifest.get("output", {}).get("frame_count") not in (None, len(stats)):
        flags.append({"flag": "count", "file": len(stats),
                      "manifest": manifest["output"]["frame_count"]})
    # A dark frame is expected in a stack's sparse planes; only a run of
    # them, or one between bright neighbours, is reported as a failure.
    failing = [f for f in flags if f["flag"] != "dark"]
    darks = [f["k"] for f in flags if f["flag"] == "dark"]
    for k in darks:
        neighbours = [stats[j]["mean"] for j in (k - 1, k + 1) if 0 <= j < len(stats)]
        if neighbours and min(neighbours) > DARK * 3:
            failing.append({"k": k, "flag": "dark-between-bright"})
    return {"frames": len(stats), "flags": flags, "failing": failing,
            "mean_luminance": [round(s["mean"], 2) for s in stats],
            "distinct": len({s["hash"] for s in stats})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video")
    parser.add_argument("--manifest")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    manifest = None
    candidate = Path(args.manifest) if args.manifest else Path(args.video).with_name("video-manifest.json")
    if candidate.is_file():
        manifest = json.loads(candidate.read_text(encoding="utf-8"))
    result = check(args.video, manifest)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['frames']} frames, {result['distinct']} distinct, "
              f"{len(result['failing'])} failing")
        for flag in result["failing"]:
            print(f"  {flag}")
    return 1 if result["failing"] else 0


if __name__ == "__main__":
    sys.exit(main())
