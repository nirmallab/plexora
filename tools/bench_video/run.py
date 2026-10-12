"""Video Studio benchmarks: render real exports, measure, check every frame.

    python tools/bench_video/run.py [--label NAME] [--only SCENARIO ...] [--ims PATH]
                                    [--meso PATH] [--keep DIR]

Builds on tools/bench3d: the same fixtures (small, flat), the same GPU Chrome
(Metal through ANGLE; a software renderer is refused), the same data root
under PLEXORA_BENCH_HOME. Starts a scratch server with a test licence
(serve_licensed.py), runs each scenario of `SCENARIOS` through bench.mjs, runs
check_frames.py on every file it can reach, and writes one JSON record to
docs/internal/video/perf/<sha>-<label>.json. Never alongside pytest.

Real stacks are optional: --ims (an Imaris file) and --meso (a light-sheet
OME-Zarr) or PLEXORA_BENCH_IMS / PLEXORA_BENCH_MESO; nothing machine-specific
lives in the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "bench3d"))
from fixtures import make_flat, make_small, out_dir, register  # noqa: E402
from serve import bench_home, server_env  # noqa: E402

sys.path.insert(0, str(HERE))
from check_frames import check  # noqa: E402

OVERLAYS = {"scalebar": True, "label": True, "legend": True, "axes": True}

SCENARIOS = [
    {"name": "zsweep_small_1080_png", "dataset": "small", "preset": "z_sweep",
     "params": {"ms_per_plane": 1000 / 30, "hold_ends_s": 0},
     "settings": {"width": 1920, "height": 1080, "fps": 30, "format": "png", "quality": "good",
                  "overlays": OVERLAYS}, "route": "server"},
    {"name": "orbit_small_1080_browser", "dataset": "small", "preset": "orbit",
     "params": {"duration_s": 4, "loop": True}, "preview_s": 3,
     "settings": {"width": 1920, "height": 1080, "fps": 30, "format": "mp4", "quality": "good",
                  "overlays": OVERLAYS}, "route": "browser"},
    {"name": "orbit_small_1080_ffmpeg", "dataset": "small", "preset": "orbit",
     "params": {"duration_s": 4, "loop": True},
     "settings": {"width": 1920, "height": 1080, "fps": 30, "format": "mp4", "quality": "good",
                  "overlays": OVERLAYS}, "route": "ffmpeg"},
    {"name": "orbit_small_4k_browser", "dataset": "small", "preset": "orbit",
     "params": {"duration_s": 2, "loop": True},
     "settings": {"width": 3840, "height": 2160, "fps": 30, "format": "mp4", "quality": "good",
                  "overlays": OVERLAYS}, "route": "browser"},
    {"name": "zoom_flat_1080_browser", "dataset": "flat", "preset": "zoom",
     "params": {"factor": 8, "duration_s": 3, "hold_end_s": 0.5}, "preview_s": 3,
     "settings": {"width": 1920, "height": 1080, "fps": 30, "format": "mp4", "quality": "good",
                  "overlays": {"scalebar": True}}, "route": "browser"},
    {"name": "rock_small_gif", "dataset": "small", "preset": "rock",
     "params": {"period_s": 2, "cycles": 1, "amplitude_deg": 30},
     "settings": {"width": 640, "height": 360, "fps": 15, "format": "gif", "quality": "good",
                  "overlays": {"scalebar": True}}, "route": "ffmpeg"},
    {"name": "orbit_ims_1080_browser", "dataset": "ims", "preset": "orbit",
     "params": {"duration_s": 8, "loop": True, "axis": "screen_y"}, "preview_s": 3,
     "settings": {"width": 1920, "height": 1080, "fps": 30, "format": "mp4", "quality": "high",
                  "overlays": OVERLAYS}, "route": "browser", "needs": "ims"},
    {"name": "zsweep_meso_1080_browser", "dataset": "meso", "preset": "z_sweep",
     "params": {"from": 800, "to": 1100, "ms_per_plane": 1000 / 30, "hold_ends_s": 0},
     "settings": {"width": 1920, "height": 1080, "fps": 30, "format": "mp4", "quality": "good",
                  "overlays": OVERLAYS}, "route": "browser", "needs": "meso"},
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_up(port, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def git_sha():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True).stdout.strip() or "nogit"
    except OSError:
        return "nogit"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="local")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--ims", default=os.environ.get("PLEXORA_BENCH_IMS"))
    parser.add_argument("--meso", default=os.environ.get("PLEXORA_BENCH_MESO"))
    parser.add_argument("--keep", default=str(bench_home() / "video" / "files"))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    home = bench_home() / "video"
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    fixtures = out_dir()
    entries = {"small": make_small(fixtures), "flat": make_flat(fixtures)}
    if args.ims:
        entries["ims"] = Path(args.ims)
    if args.meso:
        entries["meso"] = Path(args.meso)
    config = data / "config.json"
    known = json.loads(config.read_text()) if config.is_file() else {}
    missing = {k: v for k, v in entries.items() if k not in known}
    if missing:
        print("registering", ", ".join(missing), flush=True)
        register(data, missing)

    scenarios = [s for s in SCENARIOS if (not args.only or s["name"] in args.only)
                 and (s.get("needs") is None or s["needs"] in entries)]
    port = free_port()
    env = server_env(data, root=ROOT)
    log = open(home / f"server-{port}.log", "w")
    server = subprocess.Popen([sys.executable, str(HERE / "serve_licensed.py"), str(ROOT), str(data), str(port),
                               str(home / "biocognia")], cwd=str(ROOT), env=env, stdout=log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    try:
        if not wait_up(port):
            raise SystemExit(f"the server did not start; see {log.name}")
        plan = home / "plan.json"
        plan.write_text(json.dumps({"scenarios": scenarios}))
        raw = home / "raw.json"
        subprocess.run(["node", str(HERE / "bench.mjs"), "--base", f"http://127.0.0.1:{port}", "--plan", str(plan),
                        "--out", str(raw), "--keep", args.keep] + (["--headed"] if args.headed else []),
                       check=True, cwd=str(ROOT))
        results = json.loads(raw.read_text())
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
    for record in results["scenarios"]:
        path = _file_of(record, data, Path(args.keep))
        if path:
            report = check(path)
            record["frame_check"] = {"file": path.name, "frames": report["frames"],
                                     "distinct": report["distinct"],
                                     "failing": report["failing"][:10]}
    results.update(sha=git_sha(), label=args.label, machine=os.uname().machine)
    out = ROOT / "docs" / "internal" / "video" / "perf" / f"{results['sha']}-{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"wrote {out}")
    for r in results["scenarios"]:
        timing = r.get("timing") or {}
        print(f"{r['name']:32s} ok={r.get('ok')} frames={r.get('frames')} wall={r.get('wall_s')}s "
              f"per_frame={timing.get('per_frame_ms')} unresolved={r.get('unresolved')} "
              f"restored={r.get('restored')} preview={r.get('preview')} "
              f"check={len((r.get('frame_check') or {}).get('failing', []))}")


def _file_of(record, data, keep):
    """The exported file: saved by the browser into keep/<scenario>/, or
    written by the server into the video's exports/<export id>/."""
    if record.get("kind") == "blob" and record.get("file"):
        path = keep / record["name"] / record["file"]
        return path if path.is_file() else None
    if record.get("video_id") and record.get("exportId"):
        folder = data / ".videos" / record["video_id"] / "exports" / record["exportId"]
        files = [p for p in folder.glob("*") if p.name != "video-manifest.json"] if folder.is_dir() else []
        return files[0] if files else None
    return None


if __name__ == "__main__":
    main()
