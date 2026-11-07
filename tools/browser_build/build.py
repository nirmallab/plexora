"""Build the Python the website loads: Pyodide, its packages, and Plexora's.

    python tools/browser_build/build.py [--out DIR] [--python314 PY] [--brotli EXE]

Writes `<out>/py/<plexora version>/`:

- `pyodide/`: the Pyodide core (as released) and every lockfile package a stage
  of plexora/browser/stages.py needs, with `pyodide-lock.json` rewritten to
  match (sha256 of the files written here, `DROP_DEPENDS` applied);
- `wheels/`: this checkout's plexora wheel without its web client (the page
  serves `client/` and every plugin's `static/` itself; templates stay, since
  the app renders pages and panels), and the pure-Python wheels Pyodide does
  not ship, at the versions this checkout's uv.lock pins;
- `ort/`: onnxruntime-web (`ORT_WEB`, MIT), what magic select's model runs on
  in the page (client workers/onnxWorker.js): the WebGPU build and the
  WebAssembly-only one, their licence and third-party notices, and `ort.json`
  naming the version -- here, not in the static site, because the WebGPU
  build's wasm is over Pages' 25 MiB per file;
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
`$PLEXORA_BENCH_HOME/browser_node/pyodide/<V>/` (P9's cache), and
onnxruntime-web's npm package (~38 MB) into `.../onnxruntime-web/<V>/`; each
is checked against its pinned sha256 before anything is taken from it.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path

PYODIDE = "314.0.7"
URL = f"https://github.com/pyodide/pyodide/releases/download/{PYODIDE}/pyodide-{PYODIDE}.tar.bz2"
#: The release archive's sha256, checked before it is unpacked (the archive
#: this machine downloaded from URL on 2026-11-06, which every bundle so far
#: was built from). A new PYODIDE needs a new hash.
PYODIDE_SHA256 = "192b5864e6e6d30ab074861af800cb8b4acb0998ef0f9342c3367448aeb86645"
REPO = Path(__file__).resolve().parents[2]
CACHE = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "pyodide"

#: onnxruntime-web, magic select's runtime in the page: the release the
#: desktop's onnxruntime is (1.30.0), so both run the same kernels' sources.
ORT_WEB = "1.30.0"
ORT_URL = f"https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-{ORT_WEB}.tgz"
#: The npm tarball's sha256 (npm's own integrity is sha512-q0y+JrrtukXS...).
ORT_SHA256 = "d2228df7e4616bc3348bf504ee888f3bec43789a273f0a63f3e68d203ce3bf71"
#: The licence texts, which the npm package does not carry, from the release's tag.
ORT_NOTICES = {
    "LICENSE.txt": (f"https://raw.githubusercontent.com/microsoft/onnxruntime/v{ORT_WEB}/LICENSE",
                    "2f07c72751aed99790b8a4869cf2311df85a860b22ded05fa22803587a48922c"),
    "ThirdPartyNotices.txt": (
        f"https://raw.githubusercontent.com/microsoft/onnxruntime/v{ORT_WEB}/ThirdPartyNotices.txt",
        "143764b952fdb1a7c69ce653bfba74a7744d6a8a573bfb73e235fba356c83de3"),
}
#: What the page loads: the WebGPU build (its WebGPU and WebAssembly execution
#: providers in one module) and the WebAssembly-only build for a browser with
#: no GPU adapter (half the size), each a loader, its glue and its wasm.
ORT_FILES = ("ort.webgpu.min.mjs", "ort-wasm-simd-threaded.asyncify.mjs",
             "ort-wasm-simd-threaded.asyncify.wasm", "ort.wasm.min.mjs",
             "ort-wasm-simd-threaded.mjs", "ort-wasm-simd-threaded.wasm")
SITE = "/lib/python3.14/site-packages"
CORE = ("pyodide.js", "pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip",
        "package.json")

sys.path.insert(0, str(REPO))
from plexora.browser.stages import DROP_DEPENDS, NEWER_THAN_PYODIDE, STAGES  # noqa: E402


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
    found = sha256(archive)
    if found != PYODIDE_SHA256:
        raise SystemExit(f"{archive}: sha256 {found}, not Pyodide {PYODIDE}'s {PYODIDE_SHA256}; "
                         "delete it to download it again")
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(CACHE / PYODIDE, filter="data")
    return dist


def _fetch(url: str, target: Path) -> None:
    part = target.with_name(target.name + ".part")
    print(f"downloading {url}", flush=True)
    with urllib.request.urlopen(url) as response, part.open("wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    part.replace(target)


def ort_release() -> Path:
    """onnxruntime-web's files (`ORT_FILES` and the notices), downloaded once,
    checked against their pins, in the cache."""
    cache = CACHE.parent / "onnxruntime-web" / ORT_WEB
    dist = cache / "dist"
    if all((dist / name).exists() for name in (*ORT_FILES, *ORT_NOTICES)):
        return dist
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"onnxruntime-web-{ORT_WEB}.tgz"
    if not archive.exists():
        _fetch(ORT_URL, archive)
    found = sha256(archive)
    if found != ORT_SHA256:
        raise SystemExit(f"{archive}: sha256 {found}, not onnxruntime-web {ORT_WEB}'s {ORT_SHA256}; "
                         "delete it to download it again")
    notices = {}
    for name, (url, pinned) in ORT_NOTICES.items():
        target = cache / name
        if not target.exists():
            _fetch(url, target)
        if sha256(target) != pinned:
            raise SystemExit(f"{target}: not onnxruntime {ORT_WEB}'s {name}; delete it to download it again")
        notices[name] = target
    dist.mkdir(exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for name in ORT_FILES:
            with tar.extractfile(tar.getmember(f"package/dist/{name}")) as source, \
                    (dist / name).open("wb") as out:
                shutil.copyfileobj(source, out, 1 << 20)
    for name, source in notices.items():
        shutil.copy2(source, dist / name)
    return dist


def ort(out: Path, encoder: str | None) -> Path:
    """onnxruntime-web into the bundle's `ort/` (kept when it is already this
    release, compressed as asked): the files the page loads, a `.br` beside
    each module and wasm when `encoder`, the notices, and `ort.json`
    (`{version, source, files}`), which the page reads first."""
    target = out / "ort"
    record = target / "ort.json"
    if record.exists():
        held = json.loads(record.read_text(encoding="utf-8"))
        if (held.get("version") == ORT_WEB
                and all((target / name).exists() for name in held.get("files", ()))
                and all((target / f"{name}.br").exists() == bool(encoder) for name in ORT_FILES)):
            return target
    dist = ort_release()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in (*ORT_FILES, *ORT_NOTICES):
        shutil.copy2(dist / name, target / name)
        if encoder and name in ORT_FILES:
            brotli(target / name, encoder)
    record.write_text(json.dumps({"version": ORT_WEB, "source": ORT_URL,
                                  "files": [*ORT_FILES, *ORT_NOTICES]}, indent=1), encoding="utf-8")
    return target


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


def locked_versions(lock_file: Path = REPO / "uv.lock") -> dict:
    """{normalized name: {versions}} from this checkout's uv.lock."""
    lock = tomllib.loads(lock_file.read_text(encoding="utf-8"))
    out: dict = {}
    for package in lock.get("package", []):
        out.setdefault(_norm(package["name"]), set()).add(package["version"])
    return out


def pin(name: str, locked: dict) -> str:
    """The version of a pure wheel the bundle takes: the one uv.lock pins, as
    the desktop app ships it (the installed one where the lock holds several,
    for different platforms). Never unpinned: a wheel the lock does not know
    and this environment does not have stops the build."""
    versions = locked.get(_norm(name), set())
    try:
        installed = metadata.version(name)
    except metadata.PackageNotFoundError:
        installed = None
    if len(versions) == 1:
        version = next(iter(versions))
        if installed and installed != version:
            print(f"note: {name} {version} as uv.lock pins it (this environment has {installed})",
                  flush=True)
        return version
    if installed and (not versions or installed in versions):
        return installed
    raise SystemExit(f"{name}: uv.lock pins {sorted(versions) or 'nothing'} and this environment has "
                     f"{installed or 'none'}: no version to take (add it to pyproject.toml and uv lock)")


def wheels(scratch: Path) -> dict:
    """{distribution: wheel path}: plexora built from this checkout, the rest
    downloaded as pure wheels at the versions uv.lock pins."""
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check", "-q"]
    out = scratch / "wheels"
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([*pip, "wheel", "--no-deps", "--no-build-isolation", "-w", str(out),
                    str(REPO)], check=True)
    for wheel in out.glob("plexora-*.whl"):
        lean(wheel)
    wanted = sorted(({name for stage in STAGES.values() for name in stage["wheels"]} - {"plexora"})
                    | set(NEWER_THAN_PYODIDE))
    locked = locked_versions()
    pins = [f"{name}=={pin(name, locked)}" for name in wanted]
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

#: Sources a package reads as files rather than imports, kept beside their
#: bytecode: OpenCV's loader (cv2/__init__.py) execs cv2/config.py and
#: cv2/config-3.py by path and refuses to import without them ("OpenCV
#: loader: missing configuration file"), which took magic select's
#: preprocessing, and every QC check that uses cv2, off the website.
KEEP_SOURCES = ("cv2/config.py", "cv2/config-*.py")


def kept_source(relative: str) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in KEEP_SOURCES)


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
            if source.with_suffix(".pyc").exists() and not kept_source(source.relative_to(root).as_posix()):
                source.unlink()
        with zipfile.ZipFile(out, "w", compression) as dst:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    dst.write(path, path.relative_to(root).as_posix(), compress_type=compression)


def brotli(path: Path, encoder: str) -> None:
    subprocess.run([encoder, "-q", "11", "-w", "24", "-f", "-o", f"{path}.br", str(path)],
                   check=True)


def newer(lock: dict, dist: Path, found: dict) -> dict:
    """{lockfile name: the file to ship}: Pyodide's own, except for each
    `NEWER_THAN_PYODIDE` package, whose lockfile entry is pointed at the wheel
    uv.lock pins (file name, version), so Pyodide loads that one wherever it
    is a dependency."""
    files = {}
    for name, entry in lock["packages"].items():
        if name in NEWER_THAN_PYODIDE:
            wheel = found[_norm(name)]
            entry["file_name"] = wheel.name
            entry["version"] = wheel.name.split("-")[1]
            files[name] = wheel
        else:
            files[name] = dist / entry["file_name"]
    return files


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
    ort(out, encoder)

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
    with tempfile.TemporaryDirectory(prefix="pxweb-wheels-") as scratch:
        found = wheels(Path(scratch))
        sources = newer(lock, dist, found)
        for name in sorted(shipped):
            entry = lock["packages"][name]
            source, target = sources[name], out / "pyodide" / entry["file_name"]
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

    core_bytes = account(out, lock, stages)
    (out / "stages.json").write_text(json.dumps(stages, indent=1), encoding="utf-8")
    manifest = {"plexora": __version__, "pyodide": PYODIDE, "python": lock["info"]["python"],
                "precompiled": python314 is not None, "brotli": encoder is not None,
                "first_visit_bytes": core_bytes + stages["base"]["bytes"],
                "stages": {name: stage["bytes"] for name, stage in stages.items()},
                "files": listing(out)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return out


def _stored(path: Path) -> int:
    """A file's bytes as the bucket stores it: its `.br` where there is one."""
    compressed = path.with_name(path.name + ".br")
    return (compressed if compressed.exists() else path).stat().st_size


def account(out: Path, lock: dict, stages: dict) -> int:
    """Each stage's bytes as stored, written into `stages`; returns the core's."""
    for stage in stages.values():
        stage["bytes"] = (sum(_stored(out / "pyodide" / lock["packages"][n]["file_name"])
                              for n in stage["pyodide"])
                          + sum(_stored(out / "wheels" / w) for w in stage["wheels"]))
    return sum(_stored(out / "pyodide" / name) for name in CORE)


def listing(out: Path) -> dict:
    """Every file of the bundle but the manifest itself, with its size and sha256."""
    return {path.relative_to(out).as_posix(): {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(out.rglob("*")) if path.is_file() and path != out / "manifest.json"}


def plexora_only(out_root: Path, python314: str | None, encoder: str | None) -> Path:
    """Rebuild the plexora wheel inside an existing bundle (development: a
    change to Plexora's Python without the 15 minutes of brotli the packages
    take). stages.json and manifest.json are rewritten to match, the stage
    sizes and the first visit included."""
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
    # A bundle built before the page ran magic select's model gains it here.
    ort(out, encoder)
    lock = json.loads((out / "pyodide" / "pyodide-lock.json").read_text(encoding="utf-8"))
    core_bytes = account(out, lock, stages)
    (out / "stages.json").write_text(json.dumps(stages, indent=1), encoding="utf-8")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest.update({"plexora": __version__, "first_visit_bytes": core_bytes + stages["base"]["bytes"],
                     "stages": {name: stage["bytes"] for name, stage in stages.items()},
                     "files": listing(out)})
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
