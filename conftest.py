"""Suite-wide fixtures.

At the repository root rather than under tests/, because `testpaths` spans two
trees -- tests/ and plexora/plugins/*/tests/ -- and both load datasources.
"""

import pytest

from plexora import paths
from plexora.server.models import data_model


@pytest.fixture(autouse=True)
def plexora_data_root(tmp_path, monkeypatch):
    """Point the whole app at a data directory of this test's own.

    One environment variable covers every module, because nothing snapshots the
    root any more -- `plexora.paths` resolves it per call. This replaces the
    per-module `monkeypatch.setattr(module, "data_path", ...)` loops that every
    test file used to carry, which had to name each module that had imported
    the constant and silently missed any that were added later.

    Autouse, and deliberately so: a test that forgets it would otherwise run
    against the developer's real projects and write into them.
    """
    # tmp_path itself, not a subdirectory of it: that is what the whole suite
    # already assumes when it asserts on `tmp_path / name / f"{name}.db"`, and
    # it is what every test meant when it set `data_path` to tmp_path by hand.
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path))
    monkeypatch.delenv("PLEXORA_SHARED_PATH", raising=False)
    # The settings file is real and per-user, so a developer who has recorded
    # `shared_dirs` on their own machine would otherwise have those roots
    # merged into every test's project listing. A dot-prefixed file rather than
    # a directory, so it cannot be mistaken for a project.
    monkeypatch.setattr(paths, "settings_path",
                        lambda: tmp_path / ".plexora-settings.json")
    # Resolution is cached per process, so the previous test's tmp_path would
    # otherwise still be the answer.
    paths.reset()
    yield tmp_path
    paths.reset()


@pytest.fixture(autouse=True)
def _forget_the_loaded_datasource():
    """Drop data_model's "which datasource is loaded" state between tests.

    `_loaded_source` is keyed on the project's NAME (see `loaded_scope`, which
    only widens that to include the root when shared roots are configured), and
    the suite is full of projects called `demo` in different tmp_paths. Without
    this, a test that opens `demo` inherits the previous test's loaded table --
    and every guard that asks "is it already loaded?" agrees, so nothing
    reloads and nothing says so.

    That was survivable while the stale state was only a DataFrame. It stopped
    being survivable with `_providers`/`_remote`: a test that leaves a
    node-backed project loaded would have the next test's reads dispatched to a
    subprocess that has since been shut down, and the failure would land in
    whichever unrelated test ran next.
    """
    yield
    data_model._loaded_source = None
    data_model._providers = data_model.providers.EMPTY
    data_model._remote = False


@pytest.fixture(autouse=True)
def _no_background_cache_warmup(monkeypatch):
    """Stop load_datasource's cache-warming thread for the duration of a test.

    load_datasource() ends by spawning a DAEMON thread that walks every channel
    calling get_image_channel_stats and get_channel_gmm. In the app that is the
    point: it moves ~17 s of GaussianMixture fitting off the first request.

    Under pytest it is a race. The thread outlives the test that started it,
    and every function it calls goes through _ensure_loaded(), which can
    reassign data_model.config wholesale and bump load_generation. By the time
    it gets there the fixture that pointed config_json_path at a tmp_path has
    already torn down, so it reloads against whatever the *next* test set up
    and overwrites that test's config from underneath it. The symptom is a
    KeyError in some unrelated file, and which file depends on wall-clock
    timing -- so it moves whenever anything is added to the suite. It was
    reached by adding tests/test_channel_overview.py and tests/test_mini_map.py,
    neither of which goes anywhere near segmentation, and it landed in
    tests/test_segmentation_mapping.py.

    Nothing under test asserts that warming happens, and disabling it changes
    no result: every value it precomputes is computed on demand anyway by the
    same functions, just later. It only makes them slower, which in a test is
    free.
    """
    monkeypatch.setattr(data_model, "_warm_datasource_caches", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _close_figure_builder_readers():
    """Let go of any source TIFF Quick Edit held open, at the end of each test.

    `figure_builder.server.pixels` keeps a few `SourceImage` readers open
    between requests -- see its `_reader`. That is a process-global cache of
    open file handles, and on Windows a held handle makes pytest's own tmp_path
    cleanup fail with a PermissionError in a *later* test. The cache already
    reopens when a datasource's path changes, so this is about the files, not
    about correctness.
    """
    yield
    try:
        from plexora.plugins.figure_builder.server import pixels
    except ImportError:  # pragma: no cover - the plugin is not installed
        return
    pixels.close_readers()
