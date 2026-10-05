"""Suite-wide fixtures.

At the repository root rather than under tests/, because `testpaths` spans two
trees -- tests/ and plexora/plugins/*/tests/ -- and both load datasources.
"""

import os
import threading
import time

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
    # A developer who exported the suggestion variable to reproduce a connect
    # problem would otherwise have every test resolve against it -- quietly,
    # because a suggestion that loses says nothing.
    monkeypatch.delenv("PLEXORA_DATA_PATH_DEFAULT", raising=False)
    # Telemetry is pinned off for the whole suite, belt and braces: it also
    # refuses to run under pytest, but a developer who exported
    # PLEXORA_TELEMETRY=diagnostics to try it must not have every test count
    # into their live queue. The tests of telemetry itself opt back in with
    # the `telemetry_enabled` fixture.
    monkeypatch.setenv("PLEXORA_TELEMETRY", "off")
    monkeypatch.delenv("PLEXORA_TELEMETRY_ENDPOINT", raising=False)
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
    # `delenv(raising=False)` above records no undo entry when the variable was
    # already absent, and `cli.main` writes this one straight into os.environ --
    # it has to, because the resolver reads it from there. Left alone it
    # survives the rest of the session, and the first resolution that happens
    # after a teardown has restored the real `settings_path` sees a suggestion,
    # no recorded data_dir, and adopts -- into the developer's own settings
    # file. PLEXORA_DATA_PATH escapes this only because `setenv` always records.
    os.environ.pop(paths.ENV_DATA_PATH_DEFAULT, None)
    paths.reset()


@pytest.fixture(autouse=True)
def _no_claude_cli(monkeypatch):
    """`plexora ai setup claude` registers the server through the real `claude`
    CLI when it is installed -- in a test that would rewrite the developer's
    own Claude Code config. Tests see no CLI unless they fake one."""
    from plexora.ai import setup

    monkeypatch.setattr(setup, "_claude_add", lambda *args, **kwargs: False)


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
    # `source` too, and not only for tidiness: a background segmentation job
    # outlives the test that started it, and its completion handler reloads the
    # project when `source` still names it -- against whatever root the NEXT
    # test has installed.
    data_model.source = None
    data_model._providers = data_model.providers.EMPTY
    data_model._remote = False
    data_model._resource_errors.clear()


@pytest.fixture(autouse=True)
def _forget_disconnected_nodes():
    """Drop the "this address was disconnected" set between tests.

    It is process state by necessity -- it exists to stop work that outlives
    the request that started it from reaching a node that has since gone -- and
    the suite reuses a handful of node names and loopback ports across tmp
    roots. Without this, a test that disconnects `hpc-data` at :41000 makes the
    next test's identically named node refuse to answer, and the failure lands
    nowhere near the cause.
    """
    from plexora.server.models import nodes as node_registry

    node_registry._disconnected.clear()
    node_registry._addresses.clear()
    yield
    node_registry._disconnected.clear()
    # The address generations too. A provider records the generation it saw
    # when it resolved, so a counter left at 3 by the previous test is not
    # itself harmful -- but a node registered under a name the suite reuses
    # would start life one behind, and "re-resolved once for no reason" is a
    # confusing thing to find in a trace.
    node_registry._addresses.clear()


@pytest.fixture(autouse=True)
def _window_scans_run_inline():
    """Run node window scans to completion inside the request, in tests.

    Production queues the full-resolution scan on a background thread and
    answers with a provisional window, because a request thread waiting on a
    plane read is what made a node deaf on a cluster. In a test that
    asynchrony is nothing but nondeterminism: the images are kilobytes, and
    nearly every existing assertion was written against the synchronous
    behaviour ("the second request read nothing"). Inline mode gives every
    test the exact window immediately; the handful of tests about the
    asynchrony itself flip the flag back and drive the queue by hand.
    """
    from plexora.server.node import api as node_api

    node_api._WINDOW_SCANS_INLINE = True
    node_api._window_scan_queue.clear()
    node_api._window_scan_pending.clear()
    yield
    node_api._WINDOW_SCANS_INLINE = False
    node_api._window_scan_queue.clear()
    node_api._window_scan_pending.clear()


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
def _no_remote_warm(monkeypatch):
    """Do not start fetching a remote image's coarse levels on every load.

    The same reasoning as `_no_background_cache_warmup`: the warm job is an
    optimisation that runs on a thread, and in a test it is only a source of
    extra requests in a request log somebody is counting. The tests of the
    warm job itself restore it.
    """
    from plexora.server.models import remote_sources

    monkeypatch.setattr(remote_sources, "start_warm", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _forget_remote_stores():
    """Drop the process's remote store objects and cache index after a test.

    Both are process-wide by design -- one store object per URL, one index per
    cache root -- and a test's cache root is gone once its tmp_path is.
    """
    yield
    import sys

    remote_store = sys.modules.get("plexora.server.utils.remote_store")
    if remote_store is not None:
        remote_store._reset_for_tests()


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
    import sys

    # Core's shared shelf too (an agent's rendered evidence reads through it),
    # and only if something imported it -- importing it here would not be free.
    core = sys.modules.get("plexora.server.utils.source_image")
    if core is not None:
        core.close_readers()
    agent_render = sys.modules.get("plexora.agent.render")
    if agent_render is not None:
        agent_render.close_masks()
    try:
        from plexora.plugins.figure_builder.server import pixels
    except ImportError:  # pragma: no cover - the plugin is not installed
        return
    pixels.close_readers()


@pytest.fixture(autouse=True)
def _finish_layer_builds(plexora_data_root):
    """Let a layer build finish before the root is repointed under it.

    `layer_jobs.start` runs the build on a daemon thread, and a Xenium run's
    transcripts take longer to tile than the test that imported it takes to
    end. When the thread outlives the test, its `Project.mutate` and its tile
    cache resolve `paths.data_root()` at whatever moment they get to it -- and
    after teardown has undone `PLEXORA_DATA_PATH`, that is the developer's own
    install. It really happened: a suite run left a `run` project and a dozen
    `run_0042` copies in `~/Library/Application Support/plexora`, written from
    tmp_paths that no longer existed.

    Declared against `plexora_data_root` so it tears down FIRST -- the join has
    to happen while the environment still points at this test's tmp_path, which
    is the whole point.

    The segmentation conversion thread (`segmentation-<name>`) is joined too:
    `_forget_the_loaded_datasource` stops its completion handler reloading
    into the next test, but its own mask writes resolve the root when they
    happen, so it has to finish while this test's root is still in force.
    """
    yield
    deadline = time.monotonic() + 30
    for thread in threading.enumerate():
        if (not thread.name.startswith(("layer-", "segmentation-"))
                or not thread.is_alive()):
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():  # pragma: no cover - a build that hung
            raise RuntimeError(
                f"{thread.name} was still building after 30s; it would have "
                f"written into the real data root once this test's root went "
                f"away")
    from plexora.server.models import layer_jobs

    layer_jobs.forget()


@pytest.fixture(autouse=True)
def _finish_agent_jobs(plexora_data_root):
    """Let an agent job finish before the root is repointed under it.

    The same hazard as `_finish_layer_builds`: a job runs on a daemon thread
    and writes its record, receipts and audit lines as it goes. Its store
    resolved the jobs directory at submit, but its handler's own writes resolve
    `paths.data_root()` when they happen -- so the join has to happen while the
    environment still points at this test's tmp_path.
    """
    yield
    import sys

    jobs = sys.modules.get("plexora.agent.jobs")
    if jobs is None:
        return
    alive = jobs.drain(30)
    jobs._reset_for_tests()
    if alive:  # pragma: no cover - a job that hung
        raise RuntimeError(f"{[t.name for t in alive]} still running after 30s; it would "
                           f"have written into the real data root")


@pytest.fixture(autouse=True)
def _forget_serving_flag():
    """`PLEXORA_SERVING` marks the one app object as a running server (see
    cli._announce_server), which makes agent viewer tools act in-process. A test
    that runs a launch path with `serve` patched out would otherwise leave it
    set for every test after it."""
    yield
    import sys

    module = sys.modules.get("plexora")
    app = getattr(module, "app", None) if module is not None else None
    if app is not None:
        app.config.pop("PLEXORA_SERVING", None)
    sessions = sys.modules.get("plexora.server.models.viewer_sessions")
    if sessions is not None:
        sessions._reset_for_tests()


@pytest.fixture(autouse=True)
def _finish_telemetry_threads(plexora_data_root):
    """Stop telemetry's writer and uploader before the root is repointed.

    The same hazard as `_finish_layer_builds`: the writer resolves
    `paths.data_root()` when it opens its queue, and a thread that outlives
    the test would open one in the developer's own install.
    """
    yield
    import sys

    module = sys.modules.get("plexora.telemetry.client")
    if module is not None:
        module.telemetry.reset_for_tests()
    deadline = time.monotonic() + 5
    for thread in threading.enumerate():
        if thread.name.startswith("plexora-telemetry") and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


@pytest.fixture(scope="session", autouse=True)
def _no_developer_licence(tmp_path_factory):
    """Keep the developer's own licence out of the whole session.

    `_isolated_license` below is per test, so it runs after module- and
    session-scoped fixtures -- and some of those start fresh interpreters
    (the plugin boundary probe) that would otherwise read a real Paid licence
    from the per-user config directory and render every page as Paid.
    """
    from plexora.licensing import store

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(store.ENV_DIR, str(tmp_path_factory.mktemp("license")))
        for name in (store.ENV_TOKEN, store.ENV_FILE, store.ENV_JOB_CERT, store.ENV_SERVER):
            patch.delenv(name, raising=False)
        patch.setenv(store.ENV_OFFLINE, "1")
        patch.setenv(store.ENV_NO_HEARTBEAT, "1")
        yield


@pytest.fixture(autouse=True)
def _isolated_license(tmp_path, monkeypatch):
    """A licence directory of this test's own, and no licensing network.

    The licence lives in the per-user config directory, which the data-root
    fixture above does not move, so without this a developer's own Paid
    licence would unlock every entitled capability under test -- and every
    "denied on Free" assertion would pass or fail by whose laptop ran it.
    Offline and heartbeat-free, like telemetry is pinned off: the tests of the
    licensing client opt back in by deleting the variable.
    """
    from plexora.licensing import store

    monkeypatch.setenv(store.ENV_DIR, str(tmp_path / ".plexora-license"))
    for name in (store.ENV_TOKEN, store.ENV_FILE, store.ENV_JOB_CERT, store.ENV_SERVER):
        monkeypatch.delenv(name, raising=False)
    # The production service is never a test's server: tests that talk to
    # one point PLEXORA_LICENSE_SERVER at their own fake.
    monkeypatch.setattr(store, "DEFAULT_SERVER", "")
    monkeypatch.setenv(store.ENV_OFFLINE, "1")
    monkeypatch.setenv(store.ENV_NO_HEARTBEAT, "1")
    from plexora import licensing
    from plexora.licensing import tokens

    licensing.reset_for_tests()
    tokens.reset_for_tests()
    yield
    licensing.reset_for_tests()
    tokens.reset_for_tests()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "paid: run with a valid Paid test licence installed (AI capabilities "
                   "unlocked). The default is Free, as it is for a real install.")


@pytest.fixture(autouse=True)
def _paid_when_marked(request, _isolated_license, monkeypatch):
    """Install a Paid test licence for tests marked `paid`.

    After `_isolated_license`, so it lands in this test's own licence
    directory, signed by a throwaway key only this test trusts.
    """
    if request.node.get_closest_marker("paid") is not None:
        from tests.license_fixtures import PAID_TEST_GRANTS, Issuer

        issuer = Issuer(monkeypatch)
        issuer.install(issuer.issue(entitlements=list(PAID_TEST_GRANTS)))
    yield


@pytest.fixture
def license_issuer(monkeypatch):
    """An issuer whose throwaway key is the only one trusted for this test."""
    from tests.license_fixtures import Issuer

    return Issuer(monkeypatch)


@pytest.fixture
def paid_license(license_issuer):
    """A valid Paid licence (`ai` and `mcp`, PAID_TEST_GRANTS) installed for this test."""
    from tests.license_fixtures import PAID_TEST_GRANTS

    license_issuer.install(license_issuer.issue(entitlements=list(PAID_TEST_GRANTS)))
    return license_issuer


@pytest.fixture
def license_service(license_issuer, monkeypatch):
    """A stand-in licence service, with the network switched back on."""
    from plexora.licensing import store
    from tests.license_fixtures import FakeLicenseService

    service = FakeLicenseService(license_issuer).start()
    monkeypatch.delenv(store.ENV_OFFLINE, raising=False)
    monkeypatch.setenv(store.ENV_SERVER, service.url)
    try:
        yield service
    finally:
        service.stop()


@pytest.fixture
def telemetry_enabled(tmp_path, monkeypatch):
    """Telemetry switched on, in diagnostics, against a fake ingest server.

    Yields `(telemetry, fake)`. Nothing is started: a test calls
    `telemetry.start(...)` itself, so it decides whether an uploader runs.
    """
    from plexora.telemetry import config
    from plexora.telemetry.client import telemetry
    from tests.telemetry_fixtures import FakeIngest

    fake = FakeIngest().start()
    telemetry.reset_for_tests()
    monkeypatch.setattr(config, "_testing_override", False)
    monkeypatch.setenv("PLEXORA_TELEMETRY", "diagnostics")
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("PLEXORA_TELEMETRY_ENDPOINT", fake.url)
    try:
        yield telemetry, fake
    finally:
        telemetry.shutdown(0.5)
        telemetry.reset_for_tests()
        fake.stop()
