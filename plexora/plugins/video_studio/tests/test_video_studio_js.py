"""Video Studio's browser modules, run in node.

The animation evaluator exists twice, on purpose: the browser renders every
frame from static/videoAnimation.js, and the server writes each export's
manifest -- which acquired plane and timepoint every frame shows -- from
server/schema.py. If the two ever disagreed, a manifest would describe a
video that was not the one encoded. So the math probe is handed fixtures built
here by the Python presets and checks the JavaScript against them frame by
frame, alongside the camera, easing and loop-seam checks it runs on its own.

The other probes drive the viewer-facing modules against stand-in viewers:
applying a frame, waiting for readiness, the bar's markup and keys.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.video_studio.server import presets, schema

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBES = REPO_ROOT / "tests" / "js"

FACTS = {
    "datasource": "demo", "image_size": [1538, 763], "depth": 24, "frames": 6,
    "spacing": {"x": {"value": 0.5, "unit": "µm"}, "z": {"value": 2.0, "unit": "µm"}},
    "channels": [{"key": "c0", "name": "DAPI"}, {"key": "c1", "name": "CD3"},
                 {"key": "c2", "name": "CD8"}],
}
VOLUME_PRESETS = ("orbit", "rock", "tumble", "fly_through", "slice_reveal", "story_structure")


def _run(probe, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBES / probe), *map(str, args)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)
    try:
        report = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"{probe} produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    return proc.returncode, report


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    out = []
    for name in presets.PRESETS:
        scene = {"view": {"mode": "volume" if name in VOLUME_PRESETS else "plane"}}
        for fps in (15, 24, 30):
            document = presets.build(name, FACTS, scene, {})
            out.append({"name": f"{name}@{fps}", "fps": fps, "document": document,
                        "plan": schema.acquired_plan(document, fps),
                        "loop_check": name == "orbit"})
    path = tmp_path_factory.mktemp("video") / "fixtures.json"
    path.write_text(json.dumps(out), encoding="utf-8")
    return path


def test_the_animation_math_holds_and_agrees_with_the_server(fixtures):
    returncode, report = _run("video_studio_math_probe.mjs", fixtures)
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0


@pytest.mark.parametrize("probe", [
    "video_studio_scene_probe.mjs",
    "video_studio_readiness_probe.mjs",
    "video_studio_bar_probe.mjs",
])
def test_the_viewer_facing_modules(probe):
    if not (PROBES / probe).is_file():  # pragma: no cover - while a probe is being written
        pytest.skip(f"{probe} not present")
    returncode, report = _run(probe)
    assert not report["failures"], json.dumps(report["failures"], indent=2)
    assert returncode == 0
