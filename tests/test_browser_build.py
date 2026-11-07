"""tools/browser_build/build.py: the parts that decide what the website's
Python bundle holds, without building one (that takes Pyodide's 337 MB
release and ~15 minutes of brotli: web.yml's bundle job)."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("web_build", ROOT / "tools" / "browser_build" / "build.py")
build = importlib.util.module_from_spec(spec)
sys.modules["web_build"] = build
spec.loader.exec_module(build)


def test_every_pure_wheel_a_stage_takes_is_pinned_by_the_lock():
    """The bundle takes the versions uv.lock pins, as the desktop app ships
    them: a wheel added to a stage must be in the lock, once."""
    locked = build.locked_versions()
    wanted = {name for stage in build.STAGES.values() for name in stage["wheels"]} - {"plexora"}
    assert not [name for name in wanted if len(locked.get(build._norm(name), ())) != 1]


def test_the_lock_pins_over_what_is_installed(tmp_path, monkeypatch, capsys):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "flask"\nversion = "3.1.3"\n'
        '[[package]]\nname = "twice"\nversion = "1.0"\n[[package]]\nname = "twice"\nversion = "2.0"\n',
        encoding="utf-8")
    locked = build.locked_versions(tmp_path / "uv.lock")
    installed = {"flask": "3.1.4", "twice": "2.0", "loose": "0.5"}

    def version(name):
        if name.lower() not in installed:
            raise build.metadata.PackageNotFoundError(name)
        return installed[name.lower()]

    monkeypatch.setattr(build.metadata, "version", version)
    assert build.pin("Flask", locked) == "3.1.3"
    assert "this environment has 3.1.4" in capsys.readouterr().out
    assert build.pin("twice", locked) == "2.0"  # several in the lock: the installed one of them
    assert build.pin("loose", locked) == "0.5"  # not in the lock: what is installed
    with pytest.raises(SystemExit, match="no version"):
        build.pin("nowhere", locked)  # never an unpinned download
    installed["twice"] = "3.0"
    with pytest.raises(SystemExit, match="no version"):
        build.pin("twice", locked)


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_sizes_count_what_the_bucket_stores_and_the_listing_leaves_out_the_manifest(tmp_path):
    for name in build.CORE:
        _write(tmp_path / "pyodide" / name, 10)
    _write(tmp_path / "pyodide" / "pyodide.asm.wasm.br", 4)
    _write(tmp_path / "pyodide" / "numpy.whl", 100)
    _write(tmp_path / "pyodide" / "numpy.whl.br", 30)
    _write(tmp_path / "wheels" / "plexora-1-py3-none-any.whl", 50)
    _write(tmp_path / "manifest.json", 2)
    lock = {"packages": {"numpy": {"file_name": "numpy.whl"}}}
    stages = {"base": {"pyodide": ["numpy"], "wheels": ["plexora-1-py3-none-any.whl"], "bytes": 0},
              "qc": {"pyodide": [], "wheels": [], "bytes": 0}}
    core = build.account(tmp_path, lock, stages)
    assert core == 10 * (len(build.CORE) - 1) + 4
    assert (stages["base"]["bytes"], stages["qc"]["bytes"]) == (30 + 50, 0)
    listed = build.listing(tmp_path)
    assert "manifest.json" not in listed
    assert listed["pyodide/numpy.whl.br"] == {"bytes": 30, "sha256": hashlib.sha256(b"x" * 30).hexdigest()}


def test_precompiling_keeps_the_sources_a_package_reads_by_path(tmp_path):
    """OpenCV's loader execs cv2/config.py and cv2/config-3.py: compiled
    away, `import cv2` fails on the website."""
    import zipfile

    wheel = tmp_path / "opencv_python-4-cp314-cp314-any.whl"
    with zipfile.ZipFile(wheel, "w") as handle:
        for name in ("cv2/__init__.py", "cv2/config.py", "cv2/config-3.py", "cv2/version.py"):
            handle.writestr(name, "x = 1\n")
    out = tmp_path / "out.whl"
    build.precompile(wheel, out, sys.executable, stored=True)
    names = set(zipfile.ZipFile(out).namelist())
    assert {"cv2/config.py", "cv2/config-3.py"} <= names
    assert "cv2/__init__.py" not in names and "cv2/__init__.pyc" in names and "cv2/version.pyc" in names


def test_a_pyodide_release_that_is_not_the_pinned_one_is_never_unpacked(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "CACHE", tmp_path)
    archive = tmp_path / f"pyodide-{build.PYODIDE}.tar.bz2"
    archive.write_bytes(b"not pyodide")
    with pytest.raises(SystemExit, match="sha256"):
        build.release()
    assert not (tmp_path / build.PYODIDE).exists()

