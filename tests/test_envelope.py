"""What a browser tab handles well, and the project facts it is judged on.

The website offers the desktop app only when a project is more than a tab
holds (client services/handoffNotice.js, limits in client/src/envelope.json,
measured: P9's worker heap at 1.26 M x 29 cells). ORION CRC02's table opens on
the website and must not be told otherwise; the same cells with 90 columns,
or ORION on a 4 GB device, are offered the desktop app.

Three more moments, each once per project per tab, its x remembered 30 days,
its buttons on one row (at most three): a batch longer than ten minutes with
the tab open (images x the measured seconds an image before it starts, its
own pace once it has done a step); magic select's model about to load with
no WebGPU (the measured second a click on WebAssembly); and tiles read
slowly (the rolling p95 of 50 over 150 ms, after the open's own 50 and never
while a job holds the worker), which offers a node beside the data.
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "envelope_probe.mjs"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_rule():
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    verdicts = json.loads(done.stdout)
    assert verdicts["orion"]["verdict"] == "website"
    assert verdicts["wide"]["verdict"] == "desktop"
    assert "1.3 M cells × 90 columns" in verdicts["wide"]["note"]
    assert verdicts["orionOnFourGb"]["verdict"] == "desktop"
    assert verdicts["noTable"]["verdict"] == "website"
    assert verdicts["toasts"] == [{"title": "Large for a browser tab",
                                   "actions": ["Open in Desktop App", "Continue here"]}]
    assert verdicts["remembered"] == ["plexora-notice:big:desktop"]


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_limits_are_measured_and_dated(probe):
    limits = json.loads((ROOT / "plexora" / "client" / "src" / "envelope.json").read_text(encoding="utf-8"))
    assert probe["limitsDated"]
    assert limits["batch"]["limit_s"] == 600
    assert limits["model"]["wasm_click_s"] == 0.95
    assert (limits["stall"]["tiles"], limits["stall"]["p95_ms"]) == (50, 150)


def test_a_long_batch_is_said_before_it_runs_and_by_its_pace(probe):
    prior, pace = probe["batchPrior"], probe["batchPace"]
    # 0.75 s an image: 500 images are 6 min (nothing said), 1000 are 13.
    assert prior["fits"] == {"verdict": "website", "seconds": 375}
    assert prior["over"]["verdict"] == "desktop" and prior["over"]["seconds"] == 750
    assert prior["over"]["note"] == "1000 images: about 13 min here, with this tab open."
    assert prior["unmeasured"]["verdict"] == "website"  # no prior: wait for its pace
    assert pace["slow"]["verdict"] == "desktop" and pace["slow"]["seconds"] == 3000
    assert pace["slow"]["note"] == "About 50 min here at this pace, with this tab open."
    assert pace["quick"]["verdict"] == "website" and pace["finished"]["verdict"] == "website"
    assert probe["quietBatch"] == 0
    assert probe["batch"] == [{"title": "A long run for a browser tab",
                               "note": "2000 images: about 25 min here, with this tab open.",
                               "actions": ["*Desktop app", "Connect a node", "Continue here"]}]


def test_a_model_without_webgpu_says_what_a_click_costs(probe):
    assert probe["model"]["gpu"] == {"verdict": "website"}
    assert probe["modelToasts"] == [{
        "title": "Magic select without a GPU here",
        "note": "No WebGPU here: about 1 s a click (0.95 s measured).",
        "actions": ["*Connect a node", "Desktop app", "Continue here"]}]


def test_slow_tiles_offer_a_node(probe):
    verdicts = probe["stallVerdicts"]
    assert verdicts["fast"]["verdict"] == "website" and verdicts["short"]["verdict"] == "website"
    assert verdicts["slow"]["verdict"] == "node" and verdicts["slow"]["p95"] == 300
    assert verdicts["p95"] == 500
    assert probe["duringJob"] == 0           # a job holds the worker: not counted
    assert probe["firstVerdictAt"] == 100    # 50 of the open's own, then a full window
    assert probe["stall"] == [{"title": "Slow to read here",
                               "note": "Tiles take 400 ms here; a node beside the data: ~35 ms.",
                               "actions": ["*Connect a node", "Continue here"]}]
    assert probe["rememberedStall"] == [{"key": "plexora-notice:slides:stall", "days": 30}]


def test_every_notice_is_short(probe):
    for notice in probe["batch"] + probe["modelToasts"] + probe["stall"]:
        assert len(notice["title"]) <= 60 and "\n" not in notice["note"] and len(notice["note"]) <= 60
        assert len(notice["actions"]) <= 3
        assert not any(word in (notice["title"] + notice["note"]).lower() for word in ("upgrade", "unlock", "pro "))


def test_the_size_of_an_open_project(tmp_path):
    import plexora

    image = tmp_path / "slide.ome.tif"
    data = np.zeros((3, 256, 320), np.uint16)
    with tifffile.TiffWriter(image, bigtiff=True, ome=True) as writer:
        writer.write(data, tile=(128, 128), subifds=1, photometric="minisblack", metadata={"axes": "CYX"})
        writer.write(data[:, ::2, ::2], tile=(128, 128), subfiletype=1, photometric="minisblack")
    plexora.register_image_datasource("slide", str(image))
    answer = plexora.app.test_client().get("/project/slide/size").get_json()
    assert answer["rows"] is None
    assert answer["image"] == {"shape": [3, 256, 320], "dtype": "uint16", "levels": 2}
