"""tools/browser_build/build.py: the parts that decide what the website's
Python bundle holds, without building one (that takes Pyodide's 337 MB
release and ~15 minutes of brotli: web.yml's bundle job).

The stage check (`unmet`): a stage's pure-Python wheels are fetched without
their dependencies, so a requirement no stage lists is a module missing at its
first import on the website -- anndata's scverse-misc[settings] was, and every
.h5ad failed to open. The build refuses such a stage. No network: the
lockfile and the wheels are made here."""

import hashlib
import importlib.util
import json
import sys
import zipfile
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


def test_a_pyodide_release_that_is_not_the_pinned_one_is_never_unpacked(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "CACHE", tmp_path)
    archive = tmp_path / f"pyodide-{build.PYODIDE}.tar.bz2"
    archive.write_bytes(b"not pyodide")
    with pytest.raises(SystemExit, match="sha256"):
        build.release()
    assert not (tmp_path / build.PYODIDE).exists()


LOCK = {"info": {"python": "3.14.2"},
        "packages": {"numpy": {"depends": []}, "h5py": {"depends": ["numpy"]},
                     "pandas": {"depends": ["numpy", "python-dateutil"]},
                     "python-dateutil": {"depends": []}}}


def wheel(tmp_path, name, requires):
    path = tmp_path / f"{name.replace('-', '_')}-1.0-py3-none-any.whl"
    metadata = "".join(f"Requires-Dist: {line}\n" for line in requires)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name.replace('-', '_')}-1.0.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n{metadata}")
    return path


def wheels(tmp_path, extra=()):
    found = {
        "plexora": wheel(tmp_path, "plexora", ["anndata>=0.10", "numba", "waitress"]),
        "anndata": wheel(tmp_path, "anndata", [
            "numpy", "h5py", "pandas", "scverse-misc[settings]", "zarr; extra == 'zarr'"]),
        "scverse-misc": wheel(tmp_path, "scverse-misc", [
            "session-info2", "typing-extensions; python_version < '3.13'",
            "pydantic-settings; extra == 'settings'", "ipywidgets; extra == 'jupyter'"]),
    }
    for name in extra:
        found[name] = wheel(tmp_path, name, [])
    return found


def stages(h5ad_wheels):
    return {"base": {"pyodide": ["numpy"], "wheels": ["plexora"], "modules": []},
            "h5ad": {"pyodide": ["h5py", "pandas"], "wheels": h5ad_wheels, "modules": ["h5py"]}}


def test_a_dependency_nobody_listed_is_refused_with_who_needs_it(tmp_path):
    missing = build.unmet(LOCK, wheels(tmp_path), stages(["anndata", "scverse-misc"]))
    # The extra anndata asks for counts; one nobody asks for, or a marker that
    # is false in Pyodide (python 3.14), does not.
    assert missing == {"h5ad": {"session-info2": ["scverse-misc"],
                                "pydantic-settings": ["scverse-misc"]}}


def test_a_stage_that_lists_them_passes(tmp_path):
    names = ["anndata", "scverse-misc", "session-info2", "pydantic-settings"]
    found = wheels(tmp_path, extra=["session-info2", "pydantic-settings"])
    assert build.unmet(LOCK, found, stages(names)) == {}


def test_the_base_wheels_are_not_held_to_it(tmp_path):
    """plexora requires numba and waitress, which the website never loads."""
    missing = build.unmet(LOCK, wheels(tmp_path), stages(["anndata", "scverse-misc"]))
    assert "base" not in missing
    assert not {"numba", "waitress"} & set(missing["h5ad"])


def test_the_real_h5ad_stage_lists_scverse_miscs_settings_extra():
    from plexora.browser.stages import STAGES

    assert {"session-info2", "pydantic-settings", "python-dotenv"} <= set(STAGES["h5ad"]["wheels"])
