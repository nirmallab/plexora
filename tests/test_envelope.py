"""What a browser tab handles well, and the project facts it is judged on.

The website offers the desktop app only when a project is more than a tab
holds (client services/handoffNotice.js, limits in client/src/envelope.json,
measured: P9's worker heap at 1.26 M x 29 cells). ORION CRC02's table opens on
the website and must not be told otherwise; the same cells with 90 columns,
or ORION on a 4 GB device, are offered the desktop app.
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
