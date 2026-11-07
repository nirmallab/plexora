"""tools/browser_build/build.py's stage check: a stage's pure-Python wheels
are fetched without their dependencies, so a requirement no stage lists is a
module missing at its first import on the website -- anndata's
scverse-misc[settings] was, and every .h5ad failed to open. The build refuses
such a stage. No network: the lockfile and the wheels are made here."""

import importlib.util
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("browser_build", ROOT / "tools" / "browser_build" / "build.py")
build = importlib.util.module_from_spec(spec)
sys.modules["browser_build"] = build
spec.loader.exec_module(build)

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
