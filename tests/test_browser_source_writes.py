"""Saving into the user's own files on the website, in a real Chrome
(tests/browser/source_writes.mjs), held to the desktop's bytes.

An image with a CSV cell table and with an .h5ad one, picked on the website
and imported; then, as a user would: with "Save into your files" off, a save
is refused and the file untouched; turned on, the ROI panel's cell columns
(`roi.map_to_cells`) go into the CSV and Save Gates to Source File into the
.h5ad, each written over the picked original through a stand-in File System
Access handle (the profile's own storage, with Chrome's write prompt stood in:
granted only from a click) -- once from the click that saved, once after the
page's "Save into ...?" question. Where the browser gave no handle (a pick
through `<input type=file>`), the updated file comes back as a download. Each
saved file must be byte for byte what Python staged, and the desktop's own
routes, run on copies of the same files (tests/browser/source_writes_desktop.py),
must have written the same bytes: the CSV's are identical; the .h5ad's are the
same gates, X, obs, var and obsm in a different HDF5 layout (Pyodide's h5py and
CPython's are different HDF5 builds), which the check accepts.

Needs system Chrome, the bundle `tools/browser_build/build.py` writes
(PLEXORA_BENCH_HOME), a static build and playwright-core; skipped, saying
which, without them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(os.environ.get("PLEXORA_BENCH_CHROME")
              or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BUILD = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"
SITE = ROOT / "plexora" / "client" / "dist" / "static"

#: What both apps do: the regions the CSV's cells are mapped to, and the gate
#: the .h5ad gets (a range that is not the marker's whole range, so it counts).
STEPS = {
    "regions": {"type": "FeatureCollection",
                "plexora": {"schema_version": 1, "coordinate_space": {"width": 600, "height": 500}},
                "features": [
        {"type": "Feature", "properties": {"name": "left", "category": "Tumor"},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [250, 0], [250, 500], [0, 500], [0, 0]]]}},
        {"type": "Feature", "properties": {"name": "right", "category": "Stroma"},
         "geometry": {"type": "Polygon", "coordinates": [[[350, 0], [600, 0], [600, 500], [350, 500],
                                                          [350, 0]]]}}]},
    "gates": {"CD45": [600.0, 5000.0], "CD3": [250.0, 9000.0]},
}


def _version():
    import plexora

    return plexora.__version__


def _fixture_writers():
    spec = importlib.util.spec_from_file_location("node_fixtures",
                                                  ROOT / "tests" / "browser" / "node_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fixtures(out: Path) -> dict:
    """A two-channel image (three copies: one project each), 300 cells on it as a
    CSV, and 300 as an .h5ad (one image id, `obsm["spatial"]`)."""
    import anndata
    import pandas as pd

    out.mkdir(parents=True, exist_ok=True)
    writers = _fixture_writers()
    image = out / "csvslide.ome.tif"
    writers._pyramid(image, writers._planes(2, 500, 600, np.uint16), tile=256, levels=1,
                     compression="zlib")
    shutil.copyfile(image, out / "h5adslide.ome.tif")
    shutil.copyfile(image, out / "dlslide.ome.tif")
    rng = np.random.default_rng(7)
    cells = 300
    x, y = rng.uniform(5, 595, cells).round(2), rng.uniform(5, 495, cells).round(2)
    markers = rng.gamma(2.0, 300.0, size=(cells, 3)).round(3)
    csv = out / "cells.csv"
    lines = ["CellID,X_centroid,Y_centroid,DAPI,CD45,CD3"]
    lines += [f"{i + 1},{x[i]},{y[i]},{markers[i, 0]},{markers[i, 1]},{markers[i, 2]}" for i in range(cells)]
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    adata = anndata.AnnData(
        X=markers.astype(np.float32),
        obs=pd.DataFrame({"imageid": pd.Categorical(["slide"] * cells)},
                         index=[f"cell_{i}" for i in range(cells)]),
        var=pd.DataFrame(index=["DAPI", "CD45", "CD3"]))
    adata.obsm["spatial"] = np.column_stack([x, y])
    h5ad = out / "cells.h5ad"
    adata.write_h5ad(h5ad)
    steps = out / "steps.json"
    steps.write_text(json.dumps(STEPS), encoding="utf-8")
    return {"image": image, "image2": out / "h5adslide.ome.tif", "image3": out / "dlslide.ome.tif",
            "csv": csv, "h5ad": h5ad, "steps": steps}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    missing = [what for what, there in (("Chrome", CHROME.exists()), ("node", shutil.which("node")),
                                        ("the build", (BUILD / _version() / "stages.json").exists()),
                                        ("the static site", (SITE / "index.html").exists()),
                                        ("playwright-core", PLAYWRIGHT.exists())) if not there]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (see this file's docstring)")
    files = write_fixtures(tmp_path_factory.mktemp("source-writes"))
    done = subprocess.run(["node", str(ROOT / "tests" / "browser" / "source_writes.mjs"),
                           "--image", str(files["image"]), "--image2", str(files["image2"]),
                           "--image3", str(files["image3"]),
                           "--csv", str(files["csv"]),
                           "--h5ad", str(files["h5ad"]), "--steps", str(files["steps"])],
                          capture_output=True, text=True, encoding="utf-8", timeout=1500, cwd=ROOT)
    assert "{" in done.stdout, done.stderr[-3000:]
    return json.loads(done.stdout[done.stdout.index("{"):])


@pytest.mark.parametrize("name", [
    "off: refused, file untouched",
    "csv: replaced from the click that saved",
    "csv: the browser asked once",
    "csv: bytes are what Python staged",
    "csv: bytes are the desktop's",
    "h5ad: asked first, then replaced",
    "h5ad: bytes are what Python staged",
    "h5ad: what the desktop writes",
    "download: no handle, one notice",
    "download: bytes are the desktop's",
    "Python reads what it wrote",
])
def test_saving_into_picked_files(run, name):
    check = run["checks"].get(name)
    assert check is not None, sorted(run["checks"])
    assert check["ok"], json.dumps(check, indent=1)[:3000]
