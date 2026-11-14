"""A capture's pixel size is recorded in microns, whatever unit the file uses.

`/get_ome_metadata` serves the file's own unit: an OME-Zarr whose axes say
"nanometer" arrives as `physical_size_x_unit: "nm"`, an OME-TIFF can say "mm".
Core's viewer converts it before drawing its bar. Everything in Figure Builder
-- `FigureSchema.physicalWidthUm`, `compose.scale_bar`, the canvas bar -- reads
`source.pixel_size.value` as microns per pixel, so `readPixelSize` has to hand
back microns, or a scale bar comes out 1000x wrong and still looks right.

The probe (tests/js/figure_pixel_size_unit_probe.mjs) runs the plugin's real
figureBuilderApi.js and figureSchema.js with a stubbed `/get_ome_metadata`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_pixel_size_unit_probe.mjs"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=60)
    try:
        data = json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")
    assert not data["problems"], "\n".join(data["problems"])
    return data


def test_a_size_stated_in_nanometres_is_read_as_microns(report):
    assert report["nm"]["unit"] == "µm"
    assert report["nm"]["value"] == pytest.approx(0.325)


def test_a_size_stated_in_millimetres_is_read_as_microns(report):
    assert report["mm"]["unit"] == "µm"
    assert report["mm"]["value"] == pytest.approx(0.325)


def test_a_size_already_in_microns_is_unchanged(report):
    assert report["um"] == {"value": 0.325, "unit": "µm", "source": "metadata"}


def test_a_unit_nobody_knows_is_no_calibration_rather_than_a_guess(report):
    """The module's own rule: absent stays absent. A unit that cannot be
    converted is not a number of microns, and treating it as one is a scale bar
    that is wrong and looks right."""
    assert report["unknown"] is None


def test_a_capture_of_a_nanometre_image_knows_its_width_in_microns(report):
    """End to end: 1000 pixels at 325 nm per pixel is 325 µm across, which is
    what the tray prints and what the export's scale bar is measured against."""
    assert report["captured"]["unit"] == "µm"
    assert report["widthUm"] == pytest.approx(325.0)
