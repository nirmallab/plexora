"""Build the Python the website loads: Pyodide, its packages, and Plexora's.

    python tools/browser_build/build.py [--out DIR] [--python314 PY] [--brotli EXE]

Writes `<out>/py/<plexora version>/`:

- `pyodide/`: the Pyodide core (as released) and every lockfile package a stage
  of plexora/browser/stages.py needs, with `pyodide-lock.json` rewritten to
  match (sha256 of the files written here, `DROP_DEPENDS` applied);
- `wheels/`: this checkout's plexora wheel without its web client (the page
  serves `client/` and every plugin's `static/` itself; templates stay, since
  the app renders pages and panels), and the pure-Python wheels Pyodide does
  not ship, at the versions this environment has installed;
- `stages.json`: per stage, the lockfile packages to `loadPackage` and the
  wheel files to unpack, with their bytes;
- `manifest.json`: every file with its size and sha256, the Pyodide and Python
  versions, and the first visit's transfer (the base stage).

With `--python314`, every module in every wheel is compiled ahead to sourceless
bytecode for Pyodide's Python (`unchecked-hash`), so no visit compiles a module
(P9: 16 s to ready as published, 7 s precompiled). With `--brotli`, each wheel
is rewritten stored (not deflated) and a `.br` written beside it and beside the
core's wasm and JavaScript: the bucket serves those with `Content-Encoding: br`,
and the browser inflates natively rather than Python's zipfile in wasm (P9:
46 MB instead of 88).

The Pyodide release (~320 MB) is downloaded once into
`$PLEXORA_BENCH_HOME/browser_node/pyodide/<V>/` (P9's cache).
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

PYODIDE = "314.0.7"
URL = f"https://github.com/pyodide/pyodide/releases/download/{PYODIDE}/pyodide-{PYODIDE}.tar.bz2"
REPO = Path(__file__).resolve().parents[2]
CACHE = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "pyodide"
SITE = "/lib/python3.14/site-packages"
CORE = ("pyodide.js", "pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip",
        "package.json")

sys.path.insert(0, str(REPO))
from plexora.browser.stages import DROP_DEPENDS, STAGES  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release() -> Path:
    """The extracted Pyodide release, downloaded once."""
    dist = CACHE / PYODIDE / "pyodide"
    if (dist / "pyodide-lock.json").exists():
        return dist
    archive = CACHE / f"pyodide-{PYODIDE}.tar.bz2"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        part = archive.with_suffix(".part")
        print(f"downloading {URL}", flush=True)
        with urllib.request.urlopen(URL) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
        part.replace(archive)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(CACHE / PYODIDE, filter="data")
    return dist


#: What the website's plexora wheel leaves out: what the page serves itself.
LEAN_DROP = ("plexora/client/src/", "plexora/client/dist/", "plexora/client/external/")


def _dropped(name: str) -> bool:
    parts = name.split("/")
    return (name.startswith(LEAN_DROP) or name.endswith(".map")
            or (len(parts) > 3 and parts[:2] == ["plexora", "plugins"] and parts[3] == "static"))


def lean(wheel: Path) -> None:
    out = wheel.with_suffix(".lean")
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if _dropped(info.filename):
                continue
            data = src.read(info)
            if info.filename.endswith(".dist-info/RECORD"):
                data = "".join(line + "\n" for line in data.decode().splitlines()
                               if not _dropped(line.split(",")[0])).encode()
            dst.writestr(info, data)
    out.replace(wheel)


def wheels(scratch: Path) -> dict:
    """{distribution: wheel path}: plexora built from this checkout, the rest
    downloaded as pure wheels at the installed versions."""
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check", "-q"]
    out = scratch / "wheels"
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([*pip, "wheel", "--no-deps", "--no-build-isolation", "-w", str(out),
                    str(REPO)], check=True)
    for wheel in out.glob("plexora-*.whl"):
        lean(wheel)
    wanted = sorted({name for stage in STAGES.values() for name in stage["wheels"]} - {"plexora"})
    pins = []
    for name in wanted:
        try:
            pins.append(f"{name}=={metadata.version(name)}")
        except metadata.PackageNotFoundError:
            pins.append(name)
    subprocess.run([*pip, "download", "--no-deps", "--only-binary=:all:", "--python-version",
                    "3.14", "--platform", "any", "--implementation", "py", "-d", str(out), *pins],
                   check=True)
    found = {}
    for wheel in out.glob("*.whl"):
        found[_norm(wheel.name.split("-")[0])] = wheel
    missing = [name for name in [*wanted, "plexora"] if _norm(name) not in found]
    if missing:
        raise SystemExit(f"no wheel for {missing}")
    return found


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def requires(wheel: Path) -> list:
    """A wheel's `Requires-Dist` lines, from its own METADATA."""
    import email.parser

    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        message = email.parser.BytesParser().parsebytes(archive.read(name))
    return message.get_all("Requires-Dist") or []


def unmet(lock: dict, found: dict, stages: dict | None = None) -> dict:
    """`{stage: {package: [wheels that need it]}}`: what a stage's wheels
    require (in Pyodide, with the extras their dependents ask for) that neither
    the stage nor the base ships. The wheels are fetched without dependencies,
    so a requirement nobody listed is a module missing at its first import on
    the website (anndata's scverse-misc[settings] was). The base's own wheels
    are not held to it: plexora requires much that the website loads in a
    later stage, or never."""
    from packaging.requirements import Requirement

    stages = STAGES if stages is None else stages
    python = lock["info"]["python"]
    markers = {"python_version": ".".join(python.split(".")[:2]), "python_full_version": python,
               "sys_platform": "emscripten", "platform_system": "Emscripten", "os_name": "posix",
               "implementation_name": "cpython", "platform_python_implementation": "CPython",
               "platform_machine": "wasm32"}
    base = set(closure(lock, stages["base"]["pyodide"])) | {_norm(w) for w in stages["base"]["wheels"]}
    out = {}
    for stage_name, stage in stages.items():
        if stage_name == "base":
            continue
        have = base | set(closure(lock, stage["pyodide"])) | {_norm(w) for w in stage["wheels"]}
        todo = [(_norm(w), ()) for w in stage["wheels"]]
        seen, missing = set(), {}
        while todo:
            dist, extras = todo.pop()
            if (dist, extras) in seen or dist not in found:
                continue
            seen.add((dist, extras))
            for text in requires(found[dist]):
                req = Requirement(text)
                if req.marker is not None and not any(
                        req.marker.evaluate({**markers, "extra": extra}) for extra in ("", *extras)):
                    continue
                name = _norm(req.name)
                if name not in have:
                    if dist not in missing.setdefault(name, []):
                        missing[name].append(dist)
                elif req.extras:
                    todo.append((name, tuple(sorted(req.extras))))
        if missing:
            out[stage_name] = missing
    return out


def closure(lock: dict, names) -> list:
    """`names` and their lockfile dependencies, less `DROP_DEPENDS`."""
    out, todo = [], list(names)
    while todo:
        name = _norm(todo.pop())
        if name in out:
            continue
        out.append(name)
        entry = lock["packages"][name]
        todo += [dep for dep in entry.get("depends", []) if dep not in DROP_DEPENDS.get(name, ())]
    return sorted(out)


#: Run by the CPython 3.14 that compiles for Pyodide: every module under
#: argv[1] to sourceless bytecode, its path in tracebacks the one Pyodide
#: installs it at (argv[2]/<relative path>, POSIX on every build machine --
#: compileall's own -p joins with this machine's separator).
COMPILE = '''
import pathlib, py_compile, sys
root, site = pathlib.Path(sys.argv[1]), sys.argv[2]
for source in root.rglob("*.py"):
    py_compile.compile(str(source), cfile=str(source.with_suffix(".pyc")),
                       dfile=site + "/" + source.relative_to(root).as_posix(), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
'''


def precompile(wheel: Path, out: Path, python314: str | None, stored: bool) -> None:
    """`wheel` into `out`: modules as sourceless bytecode when `python314`
    is given, members stored (for brotli) or deflated."""
    compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    if python314 is None:
        with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out, "w", compression) as dst:
            for info in src.infolist():
                dst.writestr(info.filename, src.read(info), compress_type=compression)
        return
    with tempfile.TemporaryDirectory(prefix="pxweb-pyc-") as scratch:
        root = Path(scratch)
        with zipfile.ZipFile(wheel) as src:
            src.extractall(root)
        subprocess.run([python314, "-I", "-c", COMPILE, str(root), SITE], check=True)
        for source in root.rglob("*.py"):
            if source.with_suffix(".pyc").exists():
                source.unlink()
        with zipfile.ZipFile(out, "w", compression) as dst:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    dst.write(path, path.relative_to(root).as_posix(), compress_type=compression)


def brotli(path: Path, encoder: str) -> None:
    subprocess.run([encoder, "-q", "11", "-w", "24", "-f", "-o", f"{path}.br", str(path)],
                   check=True)


def build(out_root: Path, python314: str | None, encoder: str | None) -> Path:
    from plexora import __version__

    dist = release()
    lock = json.loads((dist / "pyodide-lock.json").read_text(encoding="utf-8"))
    out = out_root / "py" / __version__
    if out.exists():
        shutil.rmtree(out)
    (out / "pyodide").mkdir(parents=True)
    (out / "wheels").mkdir()
    for name in CORE:
        shutil.copy2(dist / name, out / "pyodide" / name)
        if encoder and name.endswith((".wasm", ".mjs", ".js")):
            brotli(out / "pyodide" / name, encoder)

    stored = encoder is not None
    base = closure(lock, STAGES["base"]["pyodide"])
    stages = {}
    shipped = set()
    for stage_name, stage in STAGES.items():
        packages = closure(lock, stage["pyodide"])
        if stage_name != "base":
            packages = [name for name in packages if name not in base]
        stages[stage_name] = {"pyodide": packages, "wheels": [], "bytes": 0}
        shipped.update(packages)
    for name in sorted(shipped):
        entry = lock["packages"][name]
        source, target = dist / entry["file_name"], out / "pyodide" / entry["file_name"]
        if entry["file_name"].endswith(".whl"):
            precompile(source, target, python314, stored)
            if encoder:
                brotli(target, encoder)
        else:
            shutil.copy2(source, target)
        entry["sha256"] = sha256(target)
        if name in DROP_DEPENDS:
            entry["depends"] = [d for d in entry["depends"] if d not in DROP_DEPENDS[name]]
    (out / "pyodide" / "pyodide-lock.json").write_text(json.dumps(lock), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="pxweb-wheels-") as scratch:
        found = wheels(Path(scratch))
        missing = unmet(lock, found)
        if missing:
            raise SystemExit(f"a stage's wheels need packages no stage ships: {missing} "
                             "(list them in plexora/browser/stages.py)")
        for stage_name, stage in STAGES.items():
            for name in stage["wheels"]:
                wheel = found[_norm(name)]
                target = out / "wheels" / wheel.name
                if not target.exists():
                    precompile(wheel, target, python314, stored)
                    if encoder:
                        brotli(target, encoder)
                stages[stage_name]["wheels"].append(wheel.name)

    def size(path: Path) -> int:
        compressed = path.with_name(path.name + ".br")
        return (compressed if compressed.exists() else path).stat().st_size

    for stage_name, stage in stages.items():
        stage["bytes"] = (sum(size(out / "pyodide" / lock["packages"][n]["file_name"])
                              for n in stage["pyodide"])
                          + sum(size(out / "wheels" / w) for w in stage["wheels"]))
    core_bytes = sum(size(out / "pyodide" / name) for name in CORE)
    (out / "stages.json").write_text(json.dumps(stages, indent=1), encoding="utf-8")
    files = {path.relative_to(out).as_posix(): {"bytes": path.stat().st_size, "sha256": sha256(path)}
             for path in sorted(out.rglob("*")) if path.is_file()}
    manifest = {"plexora": __version__, "pyodide": PYODIDE, "python": lock["info"]["python"],
                "precompiled": python314 is not None, "brotli": encoder is not None,
                "first_visit_bytes": core_bytes + stages["base"]["bytes"],
                "stages": {name: stage["bytes"] for name, stage in stages.items()},
                "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return out


def plexora_only(out_root: Path, python314: str | None, encoder: str | None) -> Path:
    """Rebuild the plexora wheel inside an existing bundle (development: a
    change to Plexora's Python without the 15 minutes of brotli the packages
    take). stages.json and manifest.json are rewritten to match."""
    from plexora import __version__

    out = out_root / "py" / __version__
    stages = json.loads((out / "stages.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="pxweb-plexora-") as scratch:
        built = Path(scratch) / "wheels"
        built.mkdir()
        subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "-q",
                        "wheel", "--no-deps", "--no-build-isolation", "-w", str(built),
                        str(REPO)], check=True)
        wheel = next(built.glob("plexora-*.whl"))
        lean(wheel)
        for old in (out / "wheels").glob("plexora-*.whl*"):
            old.unlink()
        target = out / "wheels" / wheel.name
        precompile(wheel, target, python314, encoder is not None)
        if encoder:
            brotli(target, encoder)
    for stage in stages.values():
        stage["wheels"] = [wheel.name if name.startswith("plexora-") else name
                           for name in stage["wheels"]]
    (out / "stages.json").write_text(json.dumps(stages, indent=1), encoding="utf-8")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = {path.relative_to(out).as_posix(): {"bytes": path.stat().st_size,
                                                            "sha256": sha256(path)}
                         for path in sorted(out.rglob("*")) if path.is_file()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(CACHE.parent / "web"),
                        help="where py/<version>/ is written")
    parser.add_argument("--python314", default=os.environ.get("PLEXORA_PYTHON314"),
                        help="a CPython 3.14 to compile Pyodide's bytecode with")
    parser.add_argument("--brotli", default=os.environ.get("PLEXORA_BROTLI") or shutil.which("brotli"),
                        help="the brotli encoder (omit for deflated wheels and no .br)")
    parser.add_argument("--plexora-only", action="store_true",
                        help="rebuild only the plexora wheel in an existing bundle")
    args = parser.parse_args(argv)
    if args.plexora_only:
        print(json.dumps({"out": str(plexora_only(Path(args.out), args.python314, args.brotli))}))
        return 0
    out = build(Path(args.out), args.python314, args.brotli)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"out": str(out), "first_visit_mb": round(manifest["first_visit_bytes"] / 1e6, 1),
                      "stages_mb": {k: round(v / 1e6, 1) for k, v in manifest["stages"].items()}},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
