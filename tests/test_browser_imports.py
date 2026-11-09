"""The website's Python builds the whole app without what its base build lacks.

On the website Plexora's Flask app runs in Pyodide, in a worker beside the
viewer, and every route and plugin panel answers from the same Python as the
desktop's. Its base build carries no pandas, pyarrow, OpenCV, scikit-image,
h5py, anndata, reportlab, numba, ONNX Runtime, imagecodecs and the rest of
`BLOCKED`: some arrive later as stages, when a route needs them; others have no
WebAssembly build at all. So building the app must not import any of them --
which is also what keeps the desktop's own start-up from paying for them.

The first browser proof found the cost of getting this wrong silently: a
missing pure-Python package made a calibrated image read as uncalibrated, with
no error anywhere (docs/internal/bench/browser_node/P9.md). This builds the app
in a subprocess with those packages made unimportable, and asks for exactly the
routes a build with everything installed has.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _probe(tmp_path, block):
    env = {**os.environ, "PLEXORA_DATA_PATH": str(tmp_path / ("blocked" if block else "open")),
           "PLEXORA_PROBE_BLOCK": "1" if block else "0"}
    env.pop("PLEXORA_PLUGINS", None)  # every plugin, as the website ships
    done = subprocess.run([sys.executable, "-m", "tests._browser_imports_probe"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def builds(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("browser_imports")
    return _probe(tmp_path, block=True), _probe(tmp_path, block=False)


def test_the_app_builds_without_what_the_website_lacks(builds):
    blocked, _open = builds
    assert blocked["built"], (blocked["error"], blocked.get("trace"))


def test_it_has_every_route_the_full_build_has(builds):
    blocked, full = builds
    assert full["built"], full["error"]
    assert blocked["endpoints"] == full["endpoints"]
    assert blocked["rules"] == full["rules"]


def test_only_optional_imports_ask_for_them(builds):
    """Asking is allowed where the import is guarded (`fast_png`'s codec, a
    library probing for an optional companion); what this pins is that nothing
    new starts asking. A new entry here is a module-scope import of a package
    the website loads later, or never: make it lazy, or guard it."""
    blocked, _open = builds
    plexora_askers = {name for name, frames in blocked["asked"].items()
                      if any("plexora" in frame.replace("\\", "/").split("site-packages")[0]
                             and "site-packages" not in frame for frame in frames)}
    assert plexora_askers <= {"imagecodecs"}, blocked["asked"]


def test_nothing_uses_a_deferred_module_while_the_app_is_built(builds):
    """polars is a stage of its own on the website, loaded at its first use
    (plexora/browser/stages.py `DEFERRED`): the modules that build the app
    import it at their top, but reading any of it -- a module-level constant
    built from `pl.Int64`, an annotation CPython evaluates -- would load the
    stage inside every visit's start. Read it inside a function instead."""
    blocked, _open = builds
    assert blocked["deferred"] == {}, blocked["deferred"]

