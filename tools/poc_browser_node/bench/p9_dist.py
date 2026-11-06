"""The Pyodide distribution P9 serves from its own origin, and the wheels it
installs on top.

    python p9_dist.py [--force-wheels]

- Downloads the full Pyodide release once (``pyodide-<V>.tar.bz2``, ~320 MB:
  the core plus every package built for it) into
  ``$PLEXORA_BENCH_HOME/browser_node/pyodide/<V>/pyodide/``. Served same-origin
  by ``serve_static.py --pyodide`` (the ``/pyodide/`` tree), as
  viewer.plexoraapp.com would serve it: timings then measure this origin, not
  jsDelivr. The npm ``pyodide`` package is the core only and fetches every
  package from jsDelivr.
- Builds ``<V>/wheels/``: this checkout's plexora wheel (``pip wheel
  --no-deps``) and the pure-Python wheels Pyodide does not ship, at the
  versions the conda env pins (``WHEELS``). Installed in the page with
  ``micropip.install(url, deps=False)``: plexora declares dependencies with no
  wasm build (numba, onnxruntime, hdf5plugin, imagecodecs) that the gating path
  never imports.
- Strips the plexora wheel of its web client (``plexora/client/``, every
  plugin's ``static/``, source maps): the browser serves those itself, so a
  website build ships Plexora's Python without them (13 MB -> ~5 MB). The
  wheel's RECORD is rewritten to match.
- Writes ``<V>/wheels/manifest.json``: every wheel's name, size and sha256, and
  the sizes of the Pyodide files P9 loads (the transfer a first visit pays).

``--variant pyc --python314 PY`` builds ``<V>-pyc/`` beside it: the packages
P9 loads and its wheels with every module precompiled to bytecode for
Pyodide's Python (Pyodide's own packages ship sources only, so every visit
compiles every module it imports), and pyarrow's declared dependency on
pandas dropped from the lockfile. Served with ``--params '{"dist": "pyc"}'``.

Nothing here runs Python in the browser; the bench driver does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path

VERSION = "314.0.7"
URL = f"https://github.com/pyodide/pyodide/releases/download/{VERSION}/pyodide-{VERSION}.tar.bz2"
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
BENCH = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node"
ROOT = BENCH / "pyodide" / VERSION

#: Pure-Python distributions the gating path imports that Pyodide does not
#: ship, at the conda env's versions (the CPython reference runs on those).
#: S1 grows this list from observed ModuleNotFoundErrors; each is recorded.
WHEELS = (
    "spatialagents", "spatialbridge", "biocognia", "biocognia-markers",
    "tifffile", "flask", "werkzeug", "itsdangerous", "blinker",
    "anndata", "array-api-compat", "legacy-api-wrap", "natsort", "scverse-misc",
    "ome-types", "xsdata", "typing-inspection", "pydantic-extra-types",
)
#: Pyodide ships these at a lower version than the pins above require
#: (pydantic 2.12 against spatialagents' >= 2.13). Recorded, not upgraded:
#: pydantic 2.13 needs pydantic-core 2.46, a compiled wheel with no wasm build.
SHIPPED_OLDER = ("pydantic", "pydantic-core", "polars", "numpy", "scipy",
                 "scikit-learn", "pillow", "h5py", "zarr", "opencv-python")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_release():
    dist = ROOT / "pyodide"
    if (dist / "pyodide-lock.json").exists():
        return dist
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT.parent / f"pyodide-{VERSION}.tar.bz2"
    if not archive.exists():
        part = archive.with_suffix(".part")
        print(f"downloading {URL}", flush=True)
        with urllib.request.urlopen(URL) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
        part.replace(archive)
    print(f"extracting {archive}", flush=True)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(ROOT, filter="data")
    return dist


#: What a browser build of the plexora wheel leaves out: the web client the
#: page already is.
LEAN_DROP = ("plexora/client/",)


def _lean_name(name):
    parts = name.split("/")
    return (name.startswith(LEAN_DROP) or name.endswith(".map")
            or (len(parts) > 3 and parts[0] == "plexora" and parts[1] == "plugins" and parts[3] == "static"))


def lean(wheel: Path):
    """Rewrite the plexora wheel in place without its web client."""
    out = wheel.with_suffix(".lean")
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if _lean_name(info.filename):
                continue
            data = src.read(info)
            if info.filename.endswith(".dist-info/RECORD"):
                data = "".join(line + "\n" for line in data.decode().splitlines()
                               if not _lean_name(line.split(",")[0])).encode()
            dst.writestr(info, data)
    out.replace(wheel)


def build_wheels(force=False):
    out = ROOT / "wheels"
    if force and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    # This checkout, as a wheel: what a release would ship.
    for old in out.glob("plexora-*.whl"):
        old.unlink()
    subprocess.run([*pip, "wheel", "--no-deps", "--no-build-isolation", "-w", str(out),
                    str(REPO)], check=True)
    for wheel in out.glob("plexora-*.whl"):
        lean(wheel)
    pins = []
    for name in WHEELS:
        try:
            pins.append(f"{name}=={metadata.version(name)}")
        except metadata.PackageNotFoundError:
            pins.append(name)
    have = {p.name.split("-")[0].lower().replace("_", "-") for p in out.glob("*.whl")}
    missing = [p for p in pins if p.split("==")[0].lower().replace("_", "-") not in have]
    if missing:
        subprocess.run([*pip, "download", "--no-deps", "--only-binary=:all:",
                        "--python-version", "3.14", "--platform", "any",
                        "--implementation", "py", "-d", str(out), *missing], check=True)
    return out


def manifest(dist: Path, wheels: Path):
    lock = json.loads((dist / "pyodide-lock.json").read_text(encoding="utf-8"))
    record = {
        "pyodide": VERSION, "python": lock["info"]["python"],
        "core": {name: (dist / name).stat().st_size for name in
                 ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip",
                  "pyodide-lock.json") if (dist / name).exists()},
        "packages": {name: {"version": p["version"], "file": p["file_name"],
                            "bytes": (dist / p["file_name"]).stat().st_size,
                            "depends": p.get("depends", [])}
                     for name, p in lock["packages"].items() if (dist / p["file_name"]).exists()},
        "wheels": [{"file": w.name, "bytes": w.stat().st_size, "sha256": sha256(w)}
                   for w in sorted(wheels.glob("*.whl"))],
        "shipped_older": {name: lock["packages"].get(name, {}).get("version") for name in SHIPPED_OLDER},
    }
    (wheels / "manifest.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


#: Files of the release every boot loads, copied into a variant as they are.
SITE = "/lib/python3.14/site-packages"
CORE = ("pyodide.js", "pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip",
        "package.json")
#: The variant's lockfile drops these declared dependencies: pyarrow imports
#: pandas only when asked to convert to or from it, which the gate store never
#: does, and pandas' import is 1.2 s here.
DROP_DEPENDS = {"pyarrow": ("pandas",)}


def _closure(lock, names):
    out, todo = set(), list(names)
    while todo:
        name = todo.pop().lower().replace("_", "-")
        if name in out:
            continue
        out.add(name)
        todo += [d for d in lock["packages"][name].get("depends", [])
                 if d not in DROP_DEPENDS.get(name, ())]
    return out


def _precompile(wheel: Path, out: Path, python314: str):
    """`wheel` rewritten into `out` with every module as sourceless bytecode
    for Pyodide's Python (3.14): compiled ahead with unchecked-hash .pyc, so
    no visit compiles a module again. Data files are kept as they are."""
    with tempfile.TemporaryDirectory(prefix="p9-pyc-") as scratch:
        root = Path(scratch)
        with zipfile.ZipFile(wheel) as src:
            src.extractall(root)
        # -s/-p: tracebacks name the module where Pyodide installs it.
        subprocess.run([python314, "-I", "-m", "compileall", "-q", "-b", "-j", "0",
                        "-s", str(root), "-p", SITE,
                        "--invalidation-mode", "unchecked-hash", str(root)], check=True)
        for source in root.rglob("*.py"):
            if source.with_suffix(".pyc").exists():
                source.unlink()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as dst:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    dst.write(path, path.relative_to(root).as_posix())


def variant_pyc(python314: str):
    """`<V>-pyc/`: the packages P9 loads and its wheels, precompiled
    (`_precompile`), under a lockfile rewritten to match (sha256, and
    `DROP_DEPENDS`). The core is copied as released: its standard library is
    already bytecode."""
    src = ROOT / "pyodide"
    out = ROOT.with_name(f"{VERSION}-pyc")
    (out / "pyodide").mkdir(parents=True, exist_ok=True)
    (out / "wheels").mkdir(parents=True, exist_ok=True)
    for name in CORE:
        shutil.copy2(src / name, out / "pyodide" / name)
    lock = json.loads((src / "pyodide-lock.json").read_text(encoding="utf-8"))
    wanted = json.loads((HERE.parent / "page" / "py" / "p9_packages.json").read_text(encoding="utf-8"))
    for name in sorted(_closure(lock, wanted["pyodide"])):
        entry = lock["packages"][name]
        target = out / "pyodide" / entry["file_name"]
        if entry["file_name"].endswith(".whl"):
            _precompile(src / entry["file_name"], target, python314)
        else:
            shutil.copy2(src / entry["file_name"], target)
        entry["sha256"] = sha256(target)
        if name in DROP_DEPENDS:
            entry["depends"] = [d for d in entry["depends"] if d not in DROP_DEPENDS[name]]
    (out / "pyodide" / "pyodide-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    for wheel in sorted((ROOT / "wheels").glob("*.whl")):
        _precompile(wheel, out / "wheels" / wheel.name, python314)
    manifest(out / "pyodide", out / "wheels")
    return out


def _restore(wheel: Path, out: Path, brotli: str):
    """`wheel` rewritten into `out` with its members stored, not deflated, and
    `out`.br beside it: the CDN's brotli then carries the bytes (polars' wheel
    18 MB deflated is 10 MB this way), and the browser decodes them natively
    rather than Python's zipfile inflating them in wasm."""
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info), compress_type=zipfile.ZIP_STORED)
    _brotli(out, brotli)


def _brotli(path: Path, brotli: str):
    subprocess.run([brotli, "-q", "11", "-w", "24", "-f", "-o", f"{path}.br", str(path)], check=True)


def variant_br(brotli: str):
    """`<V>-pycbr/`: `<V>-pyc/` with every wheel stored and served as brotli
    (`_restore`), and the core's wasm and JavaScript likewise."""
    src = ROOT.with_name(f"{VERSION}-pyc")
    out = ROOT.with_name(f"{VERSION}-pycbr")
    (out / "pyodide").mkdir(parents=True, exist_ok=True)
    (out / "wheels").mkdir(parents=True, exist_ok=True)
    for name in CORE:
        shutil.copy2(src / "pyodide" / name, out / "pyodide" / name)
        if name.endswith((".wasm", ".mjs", ".js")):
            _brotli(out / "pyodide" / name, brotli)
    lock = json.loads((src / "pyodide" / "pyodide-lock.json").read_text(encoding="utf-8"))
    for entry in lock["packages"].values():
        wheel = src / "pyodide" / entry["file_name"]
        if wheel.exists():
            target = out / "pyodide" / entry["file_name"]
            if wheel.suffix == ".whl":
                _restore(wheel, target, brotli)
            else:
                shutil.copy2(wheel, target)
            entry["sha256"] = sha256(target)
    (out / "pyodide" / "pyodide-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    for wheel in sorted((src / "wheels").glob("*.whl")):
        _restore(wheel, out / "wheels" / wheel.name, brotli)
    manifest(out / "pyodide", out / "wheels")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force-wheels", action="store_true")
    parser.add_argument("--variant", choices=("pyc", "pycbr"), default=None,
                        help="pyc: also build <V>-pyc/, every module precompiled (needs --python314); "
                             "pycbr: <V>-pyc/ as stored wheels served with brotli (needs --brotli)")
    parser.add_argument("--python314", default=os.environ.get("P9_PYTHON314"),
                        help="a CPython 3.14 to compile Pyodide's bytecode with")
    parser.add_argument("--brotli", default=os.environ.get("P9_BROTLI") or shutil.which("brotli"),
                        help="the brotli command-line encoder")
    args = parser.parse_args(argv)
    dist = fetch_release()
    if args.variant == "pyc":
        if not args.python314:
            raise SystemExit("--variant pyc needs --python314 (a CPython 3.14 interpreter)")
        print(json.dumps({"variant": str(variant_pyc(args.python314))}))
        return
    if args.variant == "pycbr":
        if not args.brotli:
            raise SystemExit("--variant pycbr needs --brotli (the brotli encoder)")
        print(json.dumps({"variant": str(variant_br(args.brotli))}))
        return
    wheels = build_wheels(args.force_wheels)
    record = manifest(dist, wheels)
    print(json.dumps({"dist": str(dist), "wheels": [w["file"] for w in record["wheels"]]}, indent=1))


if __name__ == "__main__":
    main()
