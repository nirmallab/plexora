"""Subprocess probe: build the app as the website's Python worker would have to.

Run as `python -m tests._browser_imports_probe` (`PLEXORA_PROBE_BLOCK=0`: the
same build with nothing blocked, to compare against). Every package the website's
base build does not carry is made unimportable (`BLOCKED`), then `create_app()`
runs with every plugin, and the probe prints JSON: whether it built, its rule
count, the first failure, and which blocked packages anything asked for.

A subprocess for the reason `_plugin_boundary_probe` is one: `create_app()`
runs once per interpreter, and what it imports is a property of a fresh one.
"""

import importlib.abc
import json
import os
import sys
import tempfile
import traceback

os.environ.setdefault("PLEXORA_DATA_PATH", tempfile.mkdtemp())
os.environ["PLEXORA_TELEMETRY"] = "off"
# As the website: numba is not there, so `jit.njit` must not need it.
os.environ["PLEXORA_NO_NUMBA"] = "1"

#: Not in the website's base build: a later stage brings some of them in when a
#: route needs one, and the rest have no WebAssembly build. Importing any of
#: them while the app is built would fail there.
BLOCKED = (
    "cv2", "skimage", "h5py", "anndata", "pandas", "pyarrow", "reportlab", "numba",
    "onnxruntime", "torch", "imagecodecs", "hdf5plugin", "openslide", "spatialdata",
)

asked = {}


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top in BLOCKED:
            if top not in asked:
                frames = [f"{frame.filename}:{frame.lineno}"
                          for frame in traceback.extract_stack()[:-1]
                          if "plexora" in frame.filename.replace("\\", "/")
                          and "_browser_imports_probe" not in frame.filename]
                asked[top] = frames[-3:]
            raise ModuleNotFoundError(f"No module named {top!r} (blocked: not in the "
                                      f"website's base build)", name=top)
        return None


if os.environ.get("PLEXORA_PROBE_BLOCK", "1") != "0":
    sys.meta_path.insert(0, Blocker())

out = {"built": False, "rules": 0, "error": None}
try:
    import plexora

    app = plexora.app
    out["built"] = True
    out["rules"] = len(list(app.url_map.iter_rules()))
    out["endpoints"] = sorted({rule.endpoint for rule in app.url_map.iter_rules()})
except BaseException as exc:  # noqa: BLE001 -- reported, not raised
    out["error"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    out["trace"] = [f"{frame.filename}:{frame.lineno}"
                    for frame in traceback.extract_tb(exc.__traceback__)][-6:]
out["asked"] = asked
print(json.dumps(out))
