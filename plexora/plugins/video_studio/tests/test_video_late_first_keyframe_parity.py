"""A step track whose first keyframe comes after t=0.

The browser renders every exported frame from static/videoAnimation.js, where a
keyframed track "does nothing before its first keyframe": the plane, timepoint
and view mode stay the base scene's (or an earlier sweep's) until then. The
manifest's per-frame record comes from server/schema.py's acquired_plan. The
two must agree on every frame, or the manifest -- the reproducibility record
of which acquired plane each frame shows -- describes a video that was not the
one encoded. The preset fixtures in test_video_studio_js.py all key at t=0, so
they never reach the frames before a late first keyframe.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.video_studio.server import manifest, schema

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "video_studio_math_probe.mjs"

STACK = {"datasource": "demo", "image_size": [800, 600], "depth": 12, "frames": 5,
         "channels": [{"key": "DNA", "name": "DNA"}, {"key": "CD3", "name": "CD3"}]}


def _project(tracks, *, duration=6.0, base=None):
    raw = {"video_id": "vid_0123456789ab", "source": STACK, "duration_s": duration,
           "loop": False, "tracks": list(tracks)}
    if base is not None:
        raw["base"] = base
    return schema.normalize_project(raw)


def _late_plane():
    """A plane keyed only at 3 s, as 'Capture viewer state' with the playhead at
    3 s or add_video_keyframe(kind='acquired_z', t=3, value=10) makes it."""
    return _project([{"kind": "acquired_z", "keyframes": [{"t": 3.0, "value": 10}]}],
                    base={"view": {"z": 2}})


def _late_mode_and_time():
    return _project([{"kind": "acquired_t", "keyframes": [{"t": 2.5, "value": 4}]},
                     {"kind": "view_mode", "keyframes": [{"t": 2.0, "value": "volume"}]}],
                    base={"view": {"z": 0, "t": 1, "mode": "plane"}})


def _sweep_then_late_plane():
    """A keyframed plane track beside a sweep: before its key, the sweep shows."""
    return _project([
        {"kind": "acquired_z", "mode": "procedural",
         "procedural": {"generator": "sweep", "params": {"from": 0, "to": 11}}},
        {"kind": "acquired_z", "keyframes": [{"t": 4.0, "value": 5}]},
    ])


def _run_probe(fixtures_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE), str(fixtures_path)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    return proc.returncode, report


def test_a_late_first_keyframe_gives_the_same_acquired_data_in_browser_and_manifest(tmp_path):
    """The browser's evaluator and the server's acquired_plan agree on every
    frame of a video whose step tracks are first keyed after t=0."""
    fixtures = []
    for name, build in (("late plane", _late_plane),
                        ("late mode and timepoint", _late_mode_and_time),
                        ("sweep then late plane", _sweep_then_late_plane)):
        document = build()
        fixtures.append({"name": name, "fps": 10, "document": document,
                         "plan": schema.acquired_plan(document, 10), "loop_check": False})
    path = tmp_path / "late_keyframe_fixtures.json"
    path.write_text(json.dumps(fixtures), encoding="utf-8")
    returncode, report = _run_probe(path)
    parity = [f for f in report["failures"] if "matches Python" in f["name"]]
    assert not parity, json.dumps(parity, indent=2)
    assert returncode == 0, json.dumps(report["failures"], indent=2)


def test_the_manifest_records_the_base_plane_before_a_plane_tracks_first_keyframe():
    """Frames before a plane track's first keyframe show the base view's plane
    (the browser renders them so), and the manifest says so."""
    document = _late_plane()
    out = manifest.build(document, {}, output={"fps": 10}, writer={}, frames=0,
                         complete=False)
    early = [f for f in out["frames"]
             if schema.content_time(f["k"], len(out["frames"]), 6.0, False) < 3.0 - 1e-9]
    assert early, "the video has frames before 3 s"
    assert {f["z_acquired"] for f in early} == {2}, [f["z_acquired"] for f in early][:5]
