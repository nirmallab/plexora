"""What a browser tab handles well, and the project facts it is judged on.

The website offers the desktop app only when a project is more than a tab
holds (client services/handoffNotice.js, limits in client/src/envelope.json,
measured: P9's worker heap at 1.26 M x 29 cells). ORION CRC02's table opens on
the website and must not be told otherwise; the same cells with 90 columns,
or ORION on a 4 GB device, are offered the desktop app.

Four more moments, each once per project per tab, its x remembered 30 days,
its buttons on one row (at most three): a batch longer than ten minutes with
the tab open (images x the measured seconds an image before it starts, its
own pace once it has done a step); a flat image's pyramid longer than two
minutes to build here, or bigger than the room the browser has left for a
copy kept in it; magic select's model about to load with
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
                               "actions": ["*Desktop app", "Continue here"]}]


def test_a_flat_image_too_long_or_too_big_to_build_here_is_said_before_it_starts(probe):
    """23 MB/s a band worker (a 38.4 GB flat image in 136-144 s with 12; P7's
    9.6 GB gave 29), against two minutes; a copy kept in the browser against
    the room it says is left."""
    builds = probe["builds"]
    assert builds["p7"]["verdict"] == "website" and round(builds["p7"]["seconds"]) == 35
    # 40 GB: 145 s with 12 workers -- measured 136-144 s for 38.4 GB.
    assert builds["forty"]["verdict"] == "desktop"
    assert builds["forty"]["seconds"] == pytest.approx(144.9, abs=0.1)
    assert builds["eighty"] == {"verdict": "desktop", "seconds": pytest.approx(289.9, abs=0.1),
                                "title": "A long build for a browser tab",
                                "note": "80 GB of pixels: about 5 min here, with this tab open."}
    assert builds["p7FourCores"]["verdict"] == "desktop"  # 2 workers: 209 s
    assert builds["noRoom"]["title"] == "No room for this pyramid here"
    assert builds["noRoom"]["note"] == "About 5.8 GB to keep; 3.0 GB of room left here."
    assert builds["mask"]["note"].startswith("80 GB of labels:")
    assert builds["noShape"] == {"verdict": "website"}
    # A copy into a folder on disk is judged on time only; one kept in the
    # browser on the room too, said once.
    assert probe["buildVerdicts"] == ["website", "desktop"]
    assert probe["buildToasts"] == [{"title": "No room for this pyramid here",
                                     "note": "About 5.8 GB to keep; 4.0 GB of room left here.",
                                     "actions": ["*Desktop app", "Continue here"]}]


def test_a_model_without_webgpu_says_what_a_click_costs(probe):
    assert probe["model"]["gpu"] == {"verdict": "website"}
    assert probe["modelToasts"] == [{
        "title": "Magic select without a GPU here",
        "note": "No WebGPU here: about 1 s a click (0.95 s measured).",
        "actions": ["*Desktop app", "Continue here"]}]


def test_slow_tiles_are_judged_but_never_said(probe):
    """No notice offers a node (the website's own tiles tripped the stall
    limit on a large image): the stall is measured and returned only."""
    verdicts = probe["stallVerdicts"]
    assert verdicts["fast"]["verdict"] == "website" and verdicts["short"]["verdict"] == "website"
    assert verdicts["slow"]["verdict"] == "node" and verdicts["slow"]["p95"] == 300
    assert verdicts["p95"] == 500
    assert probe["duringJob"] == 0           # a job holds the worker: not counted
    assert probe["firstVerdictAt"] == 100    # 50 of the open's own, then a full window
    assert probe["stall"] == []
    assert probe["rememberedStall"] == []


def test_slow_reads_are_said_when_one_cause_held_the_page(probe):
    """Waiting on the network or a slow drive is said when the reads' own
    waiting -- not the work between them, not tiles read side by side counted
    twenty times -- held 10 of the last 15 s at under the cause's rate."""
    v = probe["slowVerdicts"]
    assert v["network"]["verdict"] == "node" and round(v["network"]["rate"] / 1e6, 1) == 3.0
    assert v["network"]["note"] == "3.0 MB/s from transfer.rc.hms.harvard.edu."
    assert v["fastNetwork"]["verdict"] == "website"
    assert v["shortWait"]["verdict"] == "website"
    assert v["workBetween"]["verdict"] == "website" and v["workBetween"]["waited"] < 3000
    assert v["parallelTiles"]["verdict"] == "copy" and 11000 <= v["parallelTiles"]["waited"] <= 12000
    assert v["localDisk"]["verdict"] == "website"
    assert v["stale"]["verdict"] == "website"


def test_a_slow_network_offers_a_data_node_once(probe):
    assert probe["paceWired"]
    assert probe["networkToasts"] == [{
        "title": "Waiting on the network", "note": probe["networkToasts"][0]["note"],
        "actions": ["*Set up a data node", "Close"]}]
    assert "transfer.rc.hms.harvard.edu" in probe["networkToasts"][0]["note"]
    assert probe["rememberedSlow"] == ["plexora-notice:slow-network:transfer.rc.hms.harvard.edu"]


def test_a_slow_drive_says_to_copy_the_file_here(probe):
    [toast] = probe["driveToasts"]
    assert toast["title"] == "Reading a slow drive" and toast["actions"] == ["*OK"]
    assert "MB/s" in toast["note"] and "copy it here" in toast["note"]


def test_every_notice_is_short(probe):
    for notice in (probe["batch"] + probe["buildToasts"] + probe["modelToasts"] + probe["stall"]
                   + probe["networkToasts"] + probe["driveToasts"] + probe["refusedToasts"]):
        assert len(notice["title"]) <= 60 and "\n" not in notice["note"] and len(notice["note"]) <= 60
        assert len(notice["actions"]) <= 3
        assert not any(word in (notice["title"] + notice["note"]).lower() for word in ("upgrade", "unlock", "pro "))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_desktop_says_slow_reads_the_websites_way():
    """The desktop page asks its server how long reads waited (GET
    /read_pace), its clock lined up with the server's, and says the same two
    notices; a fast host says nothing."""
    done = subprocess.run(["node", str(ROOT / "tests" / "js" / "desktop_pace_probe.mjs")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    desktop = json.loads(done.stdout)
    assert desktop["said"] == [
        {"title": "Waiting on the network", "note": "3.0 MB/s from transfer.rc.hms.harvard.edu.",
         "actions": ["*Set up a data node", "Close"]},
        {"title": "Reading a slow drive", "note": "melanoma.ome.tif reads at 4.0 MB/s; copy it here.",
         "actions": ["*OK"]},
    ]
    assert desktop["opened"] and desktop["opened"][0]["kind"] == "node"
    assert desktop["every"] == 2000
    # Under the server's own base, the limits fetched once.
    assert desktop["asked"][0] == "http://127.0.0.1:5000/base/read_pace?after=0"
    assert desktop["asked"].count("http://127.0.0.1:5000/base/client/src/envelope.json") == 1
    assert "http://127.0.0.1:5000/base/read_pace?after=12" in desktop["asked"]
    for notice in desktop["said"]:
        assert len(notice["note"]) <= 60


def test_only_the_desktop_page_loads_the_notices_itself(monkeypatch):
    """The website's host page has its own copy (services/shell.js); a second
    in the app's frame would say everything twice."""
    import plexora

    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    page = plexora.app.test_client().get("/").get_data(as_text=True)
    assert 'handoffNotice.js?v=' in page and 'data-reads="desktop"' in page
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    page = plexora.app.test_client().get("/").get_data(as_text=True)
    assert "handoffNotice.js" not in page


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
