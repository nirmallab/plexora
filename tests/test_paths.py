"""Where Plexora decides its data lives.

The rules this file pins are the ones a pip-installed package depends on: a
root that never depends on the working directory, never sits inside the
installed package, and can always be named back to the user.

Several tests here deliberately avoid `paths.data_root()` and call
`paths._candidate_data_root()` instead. The public function creates the
directory it resolves and probes it for writes; asking it what the platform
default *would* be would therefore create that directory on the machine running
the suite. The private one is pure.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from platformdirs import user_data_dir

from plexora import paths


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No inherited answer, and a settings file of this test's own.

    The suite-wide fixture pins PLEXORA_DATA_PATH so that nothing touches the
    developer's projects; these tests are about the resolution itself, so they
    start from nothing and opt into each rule in turn.
    """
    monkeypatch.delenv("PLEXORA_DATA_PATH", raising=False)
    monkeypatch.delenv("PLEXORA_SHARED_PATH", raising=False)
    settings = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(paths, "settings_path", lambda: settings)
    paths.reset()
    yield settings
    paths.reset()


# -- resolution order ----------------------------------------------------


def test_the_default_is_the_platform_directory(clean_env):
    """No env var, no settings, not frozen: the OS convention wins.

    The rule that makes `pip install plexora && plexora` work from any
    directory. What it replaced resolved to `Path("plexora/data")` -- relative
    to the working directory, so importing the package from somewhere else
    silently started a second, empty installation.
    """
    resolution = paths._candidate_data_root()

    assert resolution.path == Path(user_data_dir("plexora", appauthor=False)).resolve()
    assert resolution.rule == "platform default"


def test_the_default_does_not_double_the_app_name(clean_env):
    """appauthor=False, not the default.

    platformdirs otherwise treats the app name as a vendor as well and returns
    `AppData\\Local\\plexora\\plexora` on Windows -- which is what the old
    appdirs call did.
    """
    path = paths._candidate_data_root().path

    assert path.name == "plexora"
    assert path.parent.name != "plexora"


def test_the_environment_variable_wins(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path / "from-env"))
    paths.reset()

    resolution = paths._candidate_data_root()

    assert resolution.path == (tmp_path / "from-env").resolve()
    assert "PLEXORA_DATA_PATH" in resolution.rule


def test_a_recorded_setting_beats_the_platform_default(clean_env, tmp_path):
    clean_env.parent.mkdir(parents=True, exist_ok=True)
    clean_env.write_text(json.dumps({"data_dir": str(tmp_path / "chosen")}),
                         encoding="utf-8")
    paths.reset()

    resolution = paths._candidate_data_root()

    assert resolution.path == (tmp_path / "chosen").resolve()
    assert "data_dir" in resolution.rule


def test_the_environment_variable_beats_a_recorded_setting(clean_env, tmp_path,
                                                           monkeypatch):
    """An instruction for this run outranks a stored preference.

    Which is what lets one shell open a colleague's directory without
    disturbing what every other shell on the machine opens.
    """
    clean_env.parent.mkdir(parents=True, exist_ok=True)
    clean_env.write_text(json.dumps({"data_dir": str(tmp_path / "stored")}),
                         encoding="utf-8")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path / "explicit"))
    paths.reset()

    assert paths._candidate_data_root().path == (tmp_path / "explicit").resolve()


def test_a_frozen_build_keeps_its_data_beside_the_executable(clean_env, monkeypatch,
                                                             tmp_path):
    """The portable-app shape, unchanged: the whole thing moves as one unit."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "plexora.exe"))
    paths.reset()

    resolution = paths._candidate_data_root()

    assert resolution.path == (tmp_path / "bin" / "data")
    assert "frozen" in resolution.rule


def test_a_damaged_settings_file_falls_back_rather_than_raising(clean_env):
    """Refusing to start because a preferences file is corrupt would be a worse
    failure than using the default -- every fallback below it still works."""
    clean_env.parent.mkdir(parents=True, exist_ok=True)
    clean_env.write_text("{not json", encoding="utf-8")
    paths.reset()

    assert paths.read_settings() == {}
    assert paths._candidate_data_root().rule == "platform default"


def test_nothing_resolves_relative_to_the_working_directory(clean_env, monkeypatch,
                                                            tmp_path):
    """The regression this module exists for.

    Whatever the process was started from must not change where projects live.
    """
    monkeypatch.chdir(tmp_path)
    paths.reset()
    first = paths._candidate_data_root().path

    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    paths.reset()

    assert paths._candidate_data_root().path == first


# -- preparing the root --------------------------------------------------


def test_an_unwritable_root_is_reported_with_the_flag_that_fixes_it(clean_env,
                                                                    tmp_path,
                                                                    monkeypatch):
    """Named at resolution, not at the first write inside a request."""
    target = tmp_path / "read-only"
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(target))
    paths.reset()

    def refuse(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "write_text", refuse)

    with pytest.raises(paths.DataRootError) as excinfo:
        paths.data_root()

    message = str(excinfo.value)
    assert str(target) in message
    assert "--data-dir" in message


def test_the_first_run_notice_names_the_directory_then_stops(clean_env, tmp_path,
                                                             monkeypatch):
    """Said once, because the platform directory is the right default and also
    the one nobody guesses."""
    root = tmp_path / "fresh"
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(root))
    paths.reset()

    notice = paths.first_run_notice()
    assert notice is not None
    assert str(root) in notice

    # A registry now exists, which is what "this user has used Plexora" means.
    paths.config_path().write_text("{}", encoding="utf-8")
    paths.reset()
    assert paths.first_run_notice() is None


# -- shared roots --------------------------------------------------------


def test_shared_roots_come_from_the_environment_in_order(clean_env, tmp_path,
                                                         monkeypatch):
    first, second = tmp_path / "site-a", tmp_path / "site-b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path / "mine"))
    monkeypatch.setenv("PLEXORA_SHARED_PATH", os.pathsep.join([str(first), str(second)]))
    paths.reset()

    assert paths.shared_roots() == [first.resolve(), second.resolve()]
    # The user's own root leads, which is the precedence rule for a name that
    # exists in more than one place.
    assert paths.roots()[0] == (tmp_path / "mine").resolve()


def test_the_users_own_root_is_never_also_a_shared_one(clean_env, tmp_path,
                                                       monkeypatch):
    """Otherwise a project would look shared to the person who owns it."""
    mine = tmp_path / "mine"
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(mine))
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(mine))
    paths.reset()

    assert paths.shared_roots() == []
    assert paths.roots() == [mine.resolve()]


def test_a_missing_shared_root_is_reported_rather_than_created(clean_env, tmp_path,
                                                               monkeypatch):
    """A shared root is somebody else's to provision. Creating one would turn a
    typo into a directory that exists and holds nothing."""
    absent = tmp_path / "not-there"
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path / "mine"))
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(absent))
    paths.reset()

    assert paths.shared_roots() == [absent.resolve()]
    assert not absent.exists()
    assert any("[missing]" in line for line in paths.describe())


# -- derived artifacts ---------------------------------------------------


def test_derived_output_goes_beside_a_project_whose_root_takes_writes(clean_env,
                                                                      tmp_path,
                                                                      monkeypatch):
    """Two users opening one shared image should share its pyramid rather than
    each spending minutes building an identical one."""
    mine, site = tmp_path / "mine", tmp_path / "site"
    (site / "shared_project").mkdir(parents=True)
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(mine))
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(site))
    paths.reset()

    assert paths.derived_root("shared_project") == site.resolve() / "shared_project"


def test_derived_output_falls_back_to_the_user_when_the_root_refuses(clean_env,
                                                                     tmp_path,
                                                                     monkeypatch):
    """A derived artifact that cannot be written is a feature that does not
    work, so a read-only shared root sends it to the user's own."""
    mine, site = tmp_path / "mine", tmp_path / "site"
    (site / "shared_project").mkdir(parents=True)
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(mine))
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(site))
    paths.reset()
    monkeypatch.setattr(paths, "is_writable",
                        lambda root: Path(root) != site.resolve())

    assert paths.derived_root("shared_project") == mine.resolve() / "shared_project"


def test_state_always_belongs_to_the_user(clean_env, tmp_path, monkeypatch):
    """Whoever owns the project, what this user produces while exploring it is
    theirs -- and has to land somewhere they can write."""
    mine, site = tmp_path / "mine", tmp_path / "site"
    (site / "shared_project").mkdir(parents=True)
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(mine))
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(site))
    paths.reset()

    assert paths.project_state_dir("shared_project") == mine.resolve() / "shared_project"
    assert paths.figures_root() == mine.resolve() / ".figures"
