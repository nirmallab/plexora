"""What the website's Python loads, and when.

The page loads the **base** stage with Pyodide, before the app is built:
everything `plexora.app` imports. Every other stage is loaded the first time
something imports one of its `modules` (`StageFinder`, installed by the
boot): the first QC check imports cv2 and the `qc` stage arrives; a project's
cell index imports scipy and `scipy` does, the first mixture fit (a channel's
contrast, a gate) sklearn and `fit`; an .h5ad imports anndata and `h5ad`
does, an .ims h5py and `hdf5` does. Loading by import rather than
by route means a stage can never be missing for a route nobody thought of,
and costs nothing for a session that never needs it.

A stage in `DEFERRED` loads at its module's first *use* instead: polars,
which the modules that build the app import at their top (as they must on
the desktop, where every C extension is imported before a request thread
exists: `data_model.prime_hot_code`). On the website `install` puts a
stand-in (`Deferred`) in `sys.modules` for it, so those imports cost nothing,
and the first attribute anything reads from it loads the stage and makes the
stand-in the real module, in place, as `importlib.util.LazyLoader` does.

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
import importlib.machinery
import importlib.util
import sys
import types

STAGES = {
    "base": {
        "pyodide": ["numpy", "pillow", "zarr", "numcodecs",
                    "pydantic", "pyyaml", "cryptography", "orjson", "msgpack", "jinja2",
                    "markupsafe", "packaging", "platformdirs", "click", "fsspec",
                    "typing-extensions", "zstandard", "narwhals"],
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
    "polars": {
        # Every table Plexora reads (a project's cells, a gate's, the plugin
        # store's). Not in the base any more: 0.7 s of every visit's start
        # (its WebAssembly compiled at load, Chrome 157), and the home page
        # reads no table. Loaded at polars' first use (`DEFERRED`), which the
        # boot asks for as soon as the first page has been answered.
        "pyodide": ["polars"],
        "wheels": [],
        "modules": ["polars"],
    },
    "scipy": {
        # scipy, for whatever of Plexora's imports it: a project's cell index
        # (a cKDTree, server/utils/spatial_index.py) as its table loads, and
        # the analyses after. Not in the base any more: building the app
        # imports none of it (tests/test_browser_imports.py blocks it), and
        # with scikit-learn it was ~2 s of every visit's start (load 1.2 s,
        # import 0.8 s on Chrome 157) before Python could answer anything.
        "pyodide": ["scipy"],
        "wheels": [],
        "modules": ["scipy"],
    },
    "fit": {
        # scikit-learn (scipy, joblib and threadpoolctl come with it): every
        # mixture fit -- a channel's contrast (`data_model.channel_gmm_of`),
        # a gate's (plugins/gating/server/model.py). The first contrast fit
        # waits for it; the channel is drawn by then, levelled from its stats
        # packet's hint.
        "pyodide": ["scikit-learn"],
        "wheels": [],
        "modules": ["sklearn"],
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
        # scipy as a package for the same reason: anndata imports
        # scipy.sparse at module level (the `scipy` stage's otherwise).
        "pyodide": ["h5py", "pandas", "scipy"],
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

#: Modules whose stage loads at their first use, not their first import
#: (`Deferred`): imported at the top of modules that build the app.
DEFERRED = ("polars",)

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
        #: The page's loader for several stages in one go (`preload`), when
        #: it has one: their packages in one `loadPackage`, which compiles
        #: their WebAssembly side by side. None: one stage after another.
        self.load_many = None
        #: The stand-ins `defer` made, by module name.
        self.deferred = {}

    @staticmethod
    def _loaded(stage):
        importlib.invalidate_caches()
        from plexora.browser import boot

        boot.stage(stage)

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
        self._loaded(stage)
        return None  # the ordinary finders find what was just installed

    def preload(self, names) -> list:
        """Load these stages now, as background work rather than inside the
        request that first needs one, together where the page can
        (`load_many`), and run any deferred module of theirs. A stage that
        will not load is left for its first use to ask again and say why.
        Returns the stages loaded."""
        todo = [name for name in names if name not in self.loaded and name not in self.failed]
        if not todo:
            return []
        for name in todo:
            self.importers.setdefault(name, "preload")
        try:
            if self.load_many is not None:
                self.load_many(todo)
            else:
                for name in todo:
                    self._load(name)
        except Exception:  # noqa: BLE001 -- the first use asks again and says why
            return []
        self.loaded.update(todo)
        importlib.invalidate_caches()
        for name in todo:
            waiting_here = [stand_in for module, stand_in in self.deferred.items()
                            if stage_of(module) == name and type(stand_in) is Deferred]
            if waiting_here:
                for stand_in in waiting_here:
                    self.arrive(stand_in)  # runs the stage's hook
            else:
                self._loaded(name)
        return todo

    def defer(self, name: str) -> "Deferred | None":
        """Put a stand-in for `name` in `sys.modules` (`Deferred`), unless its
        stage is loaded already (a browser without JSPI loads every stage at
        boot) or something imported it first."""
        if stage_of(name) in self.loaded or name in sys.modules:
            return None
        stand_in = Deferred(name, self)
        sys.modules[name] = self.deferred[name] = stand_in
        return stand_in

    def arrive(self, module: "Deferred") -> None:
        """Load a deferred module's stage and run the real module in the
        stand-in's place: every module that imported the stand-in holds the
        real one after this."""
        name = object.__getattribute__(module, "__name__")
        stage = stage_of(name)
        # Not a name of the real module's.
        module.__dict__.pop("_plexora_finder", None)
        if stage in self.failed:
            raise ModuleNotFoundError(f"No module named {name!r} (its stage did not load: "
                                      f"{self.failed[stage]})", name=name)
        if stage not in self.loaded:
            self.importers[stage] = _reader()
            try:
                self._load(stage)
            except Exception as exc:  # noqa: BLE001 -- said by the error below
                self.failed[stage] = str(exc)
                raise ModuleNotFoundError(f"No module named {name!r} (its stage did not load: "
                                          f"{exc})", name=name) from exc
            self.loaded.add(stage)
            importlib.invalidate_caches()
        spec = importlib.machinery.PathFinder.find_spec(name)
        if spec is None:
            self.failed[stage] = "installed, but not found"
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        # As `importlib.util.module_from_spec` would set them, on this object.
        module.__class__ = types.ModuleType
        module.__spec__, module.__loader__ = spec, spec.loader
        module.__file__, module.__cached__ = spec.origin, spec.cached
        if spec.submodule_search_locations is not None:
            module.__path__ = spec.submodule_search_locations
        module.__package__ = name if spec.submodule_search_locations is not None else spec.parent
        try:
            spec.loader.exec_module(module)
        except BaseException as exc:
            self.failed[stage] = f"{name} failed to import: {exc}"
            raise
        # The stage's hook, now there is a module to patch (it may have run
        # once already, while this was a stand-in: `boot.parquet` is idempotent).
        self._loaded(stage)


class Deferred(types.ModuleType):
    """A deferred module before its first use (`DEFERRED`): `import polars as
    pl` at a module's top binds this, and the first attribute read from it
    (`pl.DataFrame`, `from polars import col`, a submodule's import asking
    for `__path__`) loads the stage and turns this object into polars itself.

    `__spec__` is a placeholder so `importlib.util.find_spec("polars")` says
    it is there (it is, one stage away) rather than raising."""

    def __init__(self, name, finder):
        super().__init__(name)
        self.__spec__ = importlib.machinery.ModuleSpec(name, None, is_package=True)
        self._plexora_finder = finder

    def __getattr__(self, attr):
        finder = self.__dict__.get("_plexora_finder")
        if finder is None:  # arriving already, or failed: as a missing attribute
            raise AttributeError(f"module {self.__name__!r} has no attribute {attr!r}")
        try:
            finder.arrive(self)  # takes `_plexora_finder` away first
        except BaseException:
            if type(self) is Deferred:
                self._plexora_finder = finder  # asked again, it says the same
            raise
        return getattr(self, attr)


def waiting(name: str) -> bool:
    """Whether `name` is a deferred module not yet used (`Deferred`)."""
    return type(sys.modules.get(name)) is Deferred


def finder() -> StageFinder | None:
    """The stage finder `install` put on the import path, if any (none on the
    desktop)."""
    for entry in sys.meta_path:
        if isinstance(entry, StageFinder):
            return entry
    return None


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


def _reader() -> str:
    """Who read a deferred module's first attribute: the nearest frame outside
    this module and the import machinery, as `module:function`."""
    frame = sys._getframe(2)
    while frame is not None and (frame.f_globals.get("__name__") == __name__
                                 or frame.f_globals.get("__name__", "").startswith(
                                     ("importlib", "_frozen_importlib"))):
        frame = frame.f_back
    if frame is None:
        return "?"
    return f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_name}"


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
    for name in DEFERRED:
        finder.defer(name)
    return finder
