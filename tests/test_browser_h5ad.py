"""An .h5ad cell table on the website, in a real Chrome (tests/browser/h5ad.mjs):
an image and an lzf-compressed .h5ad picked, imported, the table loaded by
Plexora's own AnnData adapter in the page's Python (h5py in Pyodide, spike
S-4 in docs/internal/bench/browser_node/PHASE1_SPIKES.md), and the cell
features that read it, each checked against anndata in CPython; the project
record checked against the desktop's import of the same files.

Needs system Chrome, the build `tools/browser_build/build.py` writes and
playwright-core (`npm ci` in tools/poc_browser_node), as
tests/test_browser_python.py does; skipped, saying which, without them.
"""

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
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"
SITE = ROOT / "plexora" / "client" / "dist" / "static"


def _version():
    import plexora

    return plexora.__version__


def _fixture_writers():
    spec = importlib.util.spec_from_file_location("node_fixtures", ROOT / "tests" / "browser" / "node_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(out: Path) -> tuple:
    """A two-channel pyramid and 2,000 cells on it: six markers, obsm
    spatial, a categorical phenotype, written with lzf (what h5wasm could not
    read the strings of)."""
    import anndata
    import pandas as pd

    writers = _fixture_writers()
    image = out / "slide.ome.tif"
    writers._pyramid(image, writers._planes(2, 1000, 1200, np.uint16), tile=256, levels=1,
                     compression="zlib")
    rng = np.random.default_rng(3)
    cells = 2000
    markers = ["DAPI", "CD45", "CD3", "CD8", "PanCK", "Ki67"]
    adata = anndata.AnnData(
        X=rng.gamma(2.0, 300.0, size=(cells, len(markers))).astype(np.float32),
        obs=pd.DataFrame({"phenotype": pd.Categorical(rng.choice(["T cell", "Tumour", "Other"], cells)),
                          "Area": rng.integers(40, 400, cells)},
                         index=[f"cell_{i}" for i in range(cells)]),
        var=pd.DataFrame(index=markers))
    adata.obsm["spatial"] = np.column_stack([rng.uniform(5, 1195, cells), rng.uniform(5, 995, cells)])
    table = out / "cells.h5ad"
    adata.write_h5ad(table, compression="lzf")
    return image, table


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    missing = [what for what, there in (("Chrome", CHROME.exists()), ("node", shutil.which("node")),
                                        ("the build", (BUILD / _version() / "stages.json").exists()),
                                        ("the static site", (SITE / "index.html").exists()),
                                        ("playwright-core", PLAYWRIGHT.exists())) if not there]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (see this file's docstring)")
    image, table = _write(tmp_path_factory.mktemp("h5ad"))
    done = subprocess.run(["node", str(ROOT / "tests" / "browser" / "h5ad.mjs"), "--image", str(image),
                           "--table", str(table), "--marker", "CD45"],
                          cwd=ROOT, capture_output=True, text=True, timeout=900)
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"h5ad.mjs printed no record: {done.stderr[-3000:]}")


def test_every_check_passes(run):
    failed = {name: check.get("detail") for name, check in run["checks"].items() if not check["ok"]}
    assert not run.get("error") and not failed, (run.get("error"), failed)


def test_the_h5ad_stage_arrives_with_the_table_not_at_boot(run):
    assert not {"hdf5", "h5ad"} & set(run["stagesAtBoot"])
    # h5py first (the import's look inside the file: `hdf5`, which an .ims
    # needs alone), then pandas and anndata (`h5ad`, h5py already in).
    assert [stage["stage"] for stage in run["stages"]] == ["hdf5", "h5ad"]


def test_the_table_is_read_where_it_was_picked(run):
    """Referenced in place, as a picked CSV is: the project's folder on the
    website is the browser's storage, and a copy there would be one more
    copy of the table in memory."""
    assert run["tableSrc"].startswith("/picked/") and run["tableSrc"].endswith("/cells.h5ad")
    assert run["rows"] == 2000
