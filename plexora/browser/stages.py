"""What the website's Python loads, and when.

The page loads the **base** stage with Pyodide, before the app is built:
everything `plexora.app` and a gating session import. Every other stage is
loaded the first time something imports one of its `modules` (`StageFinder`,
installed by the boot): the first QC check imports cv2 and the `qc` stage
arrives; an .h5ad imports anndata and `h5ad` does, an .ims h5py and `hdf5`
does. Loading by import rather than
by route means a stage can never be missing for a route nobody thought of,
and costs nothing for a session that never needs it.

`pyodide` names packages in Pyodide's lockfile (their dependencies follow,
less `DROP_DEPENDS`); `wheels` names pure-Python wheels the build fetches at
the versions this checkout's lock pins; `platform` names WebAssembly wheels
Plexora builds itself and the build is handed (plexora-kernels: kernels/, the
compiled kernels, `build.py --kernels-wheel`). Those are fetched without their
dependencies, so each one's own must be listed too: the build refuses a stage
whose wheels need a package neither it nor the base ships (`build.py`
`unmet`). `tools/browser_build/build.py` turns this into the site's
`stages.json`, with every file and its size.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys

STAGES = {
    "base": {
        "pyodide": ["numpy", "scipy", "scikit-learn", "polars", "pillow", "zarr", "numcodecs",
                    "pydantic", "pyyaml", "cryptography", "orjson", "msgpack", "jinja2",
                    "markupsafe", "packaging", "platformdirs", "click", "fsspec",
                    "typing-extensions", "zstandard", "pyodide-http", "narwhals"],
        "wheels": ["plexora", "flask", "werkzeug", "itsdangerous", "blinker", "tifffile",
                   "universal-pathlib", "pathlib-abc", "biocognia", "biocognia-markers",
                   "spatialagents", "spatialbridge", "ome-types", "xsdata",
                   "pydantic-extra-types"],
        # Every kernel numba would compile on the desktop, as WebAssembly
        # (plan L5): 105 kB, loaded with the app, not when a kernel is first
        # called -- a stage arriving mid-request would cost more than it saves.
        "platform": ["plexora-kernels"],
        "modules": [],
    },
    "qc": {
        "pyodide": ["opencv-python", "shapely"],
        "wheels": [],
        "modules": ["cv2", "shapely"],
    },
    "skimage": {
        # scikit-image on its own: a surface (marching cubes, the threshold
        # suggestions) needs it and not OpenCV, which is 8 MB of qc's 19.8.
        # A QC check that imports both loads both.
        "pyodide": ["scikit-image"],
        "wheels": [],
        "modules": ["skimage"],
    },
    "hdf5": {
        # h5py alone, for an Imaris file (and anything else HDF5 that is not
        # a table): an .ims open fetches 1.15 MB stored, not the 5.7 MB of
        # h5ad's pandas and anndata, which nothing reading a volume imports.
        # Pyodide's h5py decodes gzip and lzf itself (spike S-4); lz4 (56 kB)
        # is imagecodecs_shim's LZ4H5, Imaris 10's LZ4 chunks without
        # hdf5plugin (volume/sources/ims.py), and arrives with h5py, which
        # every .ims needs first.
        "pyodide": ["h5py", "lz4"],
        "wheels": [],
        "modules": ["h5py", "lz4"],
    },
    "h5ad": {
        # h5py again as a package (loading it twice is a no-op): anndata
        # imports it at module level, where `StageFinder` loads no stage.
        "pyodide": ["h5py", "pandas"],
        # anndata asks for scverse-misc[settings]: without pydantic-settings
        # and python-dotenv, `scverse_misc.Settings` is missing and anndata
        # fails at import (found by tests/browser/import_check.mjs).
        # session-info2 is scverse-misc's own requirement (build.py `unmet`).
        "wheels": ["anndata", "array-api-compat", "legacy-api-wrap", "natsort", "scverse-misc",
                   "pydantic-settings", "python-dotenv", "session-info2"],
        "modules": ["anndata", "pandas"],
    },
    "dicom": {
        # A DICOM whole-slide image, read as the desktop reads it
        # (server/utils/dicom_wsi.py: pydicom for headers, wsidicom for the
        # slide; plan L3), brightfield and fluorescence alike. Frames decode
        # through imagecodecs_shim: JPEG by Pillow, JPEG 2000 by the page's
        # OpenJPEG. wsidicom imports its DICOMweb source (dicomweb-client,
        # requests, retrying) at its own import, so those come too; cachetools
        # as uv.lock pins it (wsidicom asks for 7.1.3 or later, Pyodide has 7.0.5).
        "pyodide": ["requests", "retrying"],
        "wheels": ["pydicom", "wsidicom", "dicomweb-client", "marshmallow", "cachetools"],
        "modules": ["pydicom", "wsidicom"],
    },
    "parquet": {
        "pyodide": ["pyarrow"],
        "wheels": [],
        "modules": ["pyarrow"],
    },
    "export": {
        "pyodide": [],
        "wheels": ["reportlab", "charset-normalizer"],
        "modules": ["reportlab"],
    },
    # A data node paired with this browser (services/connectorClient.js):
    # server/providers/http.py talks to it through urllib3, whose Emscripten
    # backend sends each request from the worker as a synchronous
    # XMLHttpRequest (CORS from the node's allowed origins). Loaded at the
    # first node call.
    "nodes": {
        "pyodide": ["urllib3"],
        "wheels": [],
        "modules": ["urllib3"],
    },
}

#: Pure packages Pyodide ships older than a stage's wheels need. The build
#: ships each at the version uv.lock pins, under Pyodide's own name for it, so
#: every package that depends on it loads that one: anndata 0.13 imports
#: typing_extensions.sentinel (new in 4.16), and Pyodide 314.0.7 has 4.15.0 --
#: `import anndata` failed on the website (tests/browser/import_check.mjs).
NEWER_THAN_PYODIDE = ("typing-extensions",)

#: Lockfile dependencies a stage does not take: imported only by code Plexora
#: never reaches (pyarrow's pandas conversions; networkx's drawing).
DROP_DEPENDS = {"pyarrow": ("pandas",), "networkx": ("matplotlib",)}


def stage_of(module: str) -> str | None:
    """The stage whose import brings `module` (its top-level package), if any."""
    top = module.split(".")[0]
    for name, stage in STAGES.items():
        if top in stage["modules"]:
            return name
    return None


class StageFinder(importlib.abc.MetaPathFinder):
    """Loads a stage the first time one of its modules is imported, then lets
    the ordinary finders find it. `load(stage)` is the page's loader: it
    fetches and installs the stage's packages (waiting through JSPI), or
    raises; a stage that failed to load is not asked for again in this
    session, and the import fails as a missing package would."""

    def __init__(self, load, loaded=("base",)):
        self._load = load
        self.loaded = set(loaded)
        self.failed = {}
        #: Which module's import loaded each stage (for the page's record).
        self.importers = {}

    def find_spec(self, name, path=None, target=None):
        if path is not None:  # a submodule: its package was found already
            return None
        stage = stage_of(name)
        if stage is None or stage in self.loaded or stage in self.failed:
            return None
        if _probing() or not _wanted():
            # A library asking whether an optional companion is there --
            # `importlib.util.find_spec("pandas")` (polars), or a module-level
            # `try: import pandas` (scikit-learn, pandas' own pyarrow check) --
            # as it is imported. Not there yet is the true answer, and loading a
            # stage for a question would load every stage at boot.
            return None
        self.importers[stage] = _importer()
        try:
            self._load(stage)
        except Exception as exc:  # noqa: BLE001 -- the import says it, below
            self.failed[stage] = str(exc)
            return None
        self.loaded.add(stage)
        importlib.invalidate_caches()
        from plexora.browser import boot

        boot.stage(stage)
        return None  # the ordinary finders find what was just installed


def _probing() -> bool:
    """Whether this lookup is `importlib.util.find_spec` asking, not an import."""
    frame = sys._getframe(2)
    for _ in range(8):
        if frame is None:
            return False
        if frame.f_code.co_name in ("_find_and_load_unlocked", "_find_and_load"):
            return False
        if frame.f_code.co_name == "find_spec" and frame.f_globals.get("__name__") == "importlib.util":
            return True
        frame = frame.f_back
    return False


def _importing_frame():
    """The frame whose import statement this lookup serves."""
    frame = sys._getframe(3)
    while frame is not None and frame.f_globals.get("__name__", "").startswith(
            ("importlib", "_frozen_importlib")):
        frame = frame.f_back
    return frame


def _importer() -> str:
    frame = _importing_frame()
    return frame.f_globals.get("__name__", "?") if frame is not None else "?"


def _wanted() -> bool:
    """Whether this import is one a stage should load for: Plexora's own, or
    a library function asking at run time (`frame.to_pandas()`) -- not a
    library's module-level optional import while it is itself imported."""
    frame = _importing_frame()
    if frame is None:
        return False
    if frame.f_globals.get("__name__", "").split(".")[0] == "plexora":
        return True
    if frame.f_code.co_name == "<module>":
        return False
    up = frame.f_back
    while up is not None:
        if up.f_code.co_name in ("_find_and_load", "_find_and_load_unlocked"):
            return False  # inside another module's import
        up = up.f_back
    return True


def install(load, loaded=("base",)) -> StageFinder:
    finder = StageFinder(load, loaded)
    sys.meta_path.insert(0, finder)
    return finder
