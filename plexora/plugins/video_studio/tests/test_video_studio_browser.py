"""Video Studio end to end: a real server, a real Chrome, real exports.

A synthetic z-stack (3 channels x 12 planes, 0.5 µm pixels, 2 µm planes) is
registered in a scratch data root and served by tools/bench_video/
serve_licensed.py -- which installs a TEST licence for `plugin:video_studio`,
signed by a key only that process trusts, into a scratch licence directory.
tests/js/video_studio_export.mjs then drives the page and this checks the
files on disk:

  - a z-sweep at one plane per frame is exactly 12 frames, planes 0..11 in
    order, every frame different, verified against the server's own plan;
  - a 16-frame 360° loop rendered twice is byte-identical frame for frame
    (deterministic rendering), and its last frame differs from its first;
  - Stop after three frames ends the export, leaves no file, and takes the
    render cover away;
  - the viewer is given back exactly after every export.

Skipped without node or Chrome (or WebGL2): the rest of the suite covers the
logic; this covers the browser.
"""

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DRIVER = REPO_ROOT / "tests" / "js" / "video_studio_export.mjs"
LAUNCHER = REPO_ROOT / "tools" / "bench_video" / "serve_licensed.py"
CHROME = os.environ.get("PLEXORA_BENCH_CHROME",
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_stack(path):
    import numpy as np
    import tifffile

    rng = np.random.default_rng(7)
    depth, size = 12, 256
    stack = np.zeros((3, depth, size, size), np.uint16)
    yy, xx = np.mgrid[0:size, 0:size]
    for _ in range(40):
        cx, cy, cz = rng.uniform(20, size - 20), rng.uniform(20, size - 20), rng.uniform(0, depth)
        r = rng.uniform(6, 12)
        for z in range(depth):
            blob = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * r * r) + ((z - cz) ** 2) / 4.0))
            stack[rng.integers(0, 3), z] += (blob * 30000).astype(np.uint16)
    stack += rng.integers(100, 400, stack.shape, dtype=np.uint16)
    md = {"axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0,
          "Channel": {"Name": ["DNA", "CD3", "CD8"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(stack, tile=(128, 128), metadata=md, compression="zlib")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if shutil.which("node") is None or not Path(CHROME).exists():
        pytest.skip("needs node and Chrome")
    root = tmp_path_factory.mktemp("video_browser")
    data = root / "data"
    data.mkdir()
    image = root / "stack.ome.tif"
    _write_stack(image)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PLEXORA_DATA_PATH": str(data),
           "HOME": str(root / "home"), "PLEXORA_TESTING": "1", "PLEXORA_TELEMETRY": "off",
           "PYTHONUNBUFFERED": "1"}
    (root / "home").mkdir()
    register = subprocess.run(
        [sys.executable, "-c",
         "import sys; from plexora.datasource import register_image_datasource as r; "
         "r('stack', sys.argv[1], data_dir=sys.argv[2])", str(image), str(data)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert register.returncode == 0, register.stderr[-2000:]
    port = _free_port()
    log = open(root / "server.log", "w")
    process = subprocess.Popen([sys.executable, str(LAUNCHER), str(REPO_ROOT), str(data), str(port),
                                str(root / "biocognia")], cwd=REPO_ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    else:
        process.kill()
        pytest.fail("the scratch server did not start")
    yield {"base": f"http://127.0.0.1:{port}", "data": data}
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="module")
def run(server):
    proc = subprocess.run(["node", str(DRIVER), server["base"], "stack"], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=600)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, f"no report\nstdout:{proc.stdout[-3000:]}\nstderr:{proc.stderr[-3000:]}"
    report = json.loads(lines[-1])
    if report.get("skip"):
        pytest.skip(report["skip"])
    assert not report.get("error"), report["error"]
    return report


def _export(server, video_id, export_id):
    folder = server["data"] / ".videos" / video_id / "exports" / export_id
    archive = next(p for p in folder.glob("*.zip"))
    manifest = json.loads((folder / "video-manifest.json").read_text())
    with zipfile.ZipFile(archive) as z:
        frames = [z.read(name) for name in sorted(z.namelist())]
    return frames, manifest


def _pixels_hash(png):
    from PIL import Image

    return hashlib.sha1(Image.open(io.BytesIO(png)).convert("RGB").tobytes()).hexdigest()


def test_the_launcher_opens_video_studio(run):
    assert run["launcher"].startswith("Video Studio")
    assert run["ready"] is True
    assert run["console_errors"] == []


def test_a_sweep_shows_every_plane_once_in_order(run, server):
    result = run["sweep"]
    assert result["ok"], result
    frames, manifest = _export(server, result["videoId"], result["exportId"])
    assert len(frames) == 12 == manifest["output"]["frame_count"]
    assert [f["z_acquired"] for f in manifest["frames"]] == list(range(12))
    assert manifest["integrity"]["acquired_verified"] is True
    assert manifest["integrity"]["unresolved_frames"] == []
    assert len({_pixels_hash(f) for f in frames}) == 12
    assert result["restored"] is True


def test_a_loop_renders_identically_twice(run, server):
    a, b = run["orbitA"], run["orbitB"]
    assert a["ok"] and b["ok"], (a, b)
    frames_a, manifest = _export(server, a["videoId"], a["exportId"])
    frames_b, _ = _export(server, b["videoId"], b["exportId"])
    assert len(frames_a) == len(frames_b) == 16
    hashes_a = [_pixels_hash(f) for f in frames_a]
    assert hashes_a == [_pixels_hash(f) for f in frames_b]
    assert hashes_a[0] != hashes_a[-1]
    assert len(set(hashes_a)) == 16
    assert manifest["loop"] is True
    assert a["restored"] and b["restored"]


def test_stop_ends_an_export_and_leaves_no_file(run, server):
    result = run["cancel"]
    assert result.get("stopped") is True and result["ok"] is False, result
    assert result["cover"] is False
    exports = server["data"] / ".videos" / result["videoId"] / "exports"
    finished = {run[name]["exportId"] for name in ("sweep", "orbitA", "orbitB")}
    left = {d.name for d in exports.iterdir() if d.is_dir()} if exports.is_dir() else set()
    # Only finished exports have a directory; the stopped one was removed.
    assert left <= finished, left - finished
