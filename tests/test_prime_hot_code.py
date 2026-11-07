"""The lazy initializers the request paths hide are paid before traffic.

Regression tests for an interpreter deadlock watched live on a cluster node:
the first `Image.save` of a tile burst imports PIL's format plugins (dlopen,
GIL held, loader lock wanted) while the first `GaussianMixture.fit` of a
contrast warm-up has threadpoolctl walk the shared libraries
(dl_iterate_phdr, loader lock held, GIL wanted). Two threads, one moment,
every thread in the process frozen forever -- /health included, which is what
the panel reported as a healthy tunnel to a machine that was "Not answering".

The cure is not a lock or a retry: it is that no request thread ever performs
a first-time initialization. `data_model.prime_hot_code` pays them all on one
thread, and the node pays them before its announce line -- the announce is
what lets a primary register it, and traffic follows within seconds.
"""

import sys

from PIL import Image


def test_priming_leaves_nothing_lazy(capsys):
    from plexora.server.models import data_model

    data_model.prime_hot_code(log=print)

    # Each step reports its own failure rather than raising; a clean run is
    # therefore a silent one.
    assert "could not pre-load" not in capsys.readouterr().out
    # PIL's two-stage init: 1 after preinit(), 2 only once init() has imported
    # the full plugin set -- the dlopen storm the request path must never run.
    assert Image._initialized == 2
    assert "sklearn.mixture" in sys.modules
    assert "scipy.stats" in sys.modules
    # QC's geometry, imported whole: a half-initialized shapely is what two
    # threads' first imports left behind in a live MCP server.
    assert "shapely.ops" in sys.modules
    assert sys.modules["shapely"].lib is not None
    # Magic select's runtime is a native library like any other: dlopened
    # here, never first inside a request (it is a core dependency).
    import importlib.util

    if importlib.util.find_spec("onnxruntime") is not None:
        assert "onnxruntime" in sys.modules


_WARM_PROBE = r"""
import json
import sys

from plexora.server.models import data_model


def native():
    return {name for name, module in list(sys.modules.items())
            if str(getattr(module, "__file__", "") or "").endswith((".pyd", ".so"))}


data_model.prime_hot_code()
primed = "ome_types" in sys.modules
core = "pydantic_core" in sys.modules
before = native()
thread = data_model.warm_after_ready()
started = thread is not None and thread.daemon
thread.join(120)
print(json.dumps({"primed": primed, "core": core, "started": started,
                  "warmed": "ome_types.model" in sys.modules,
                  "new_native": sorted(native() - before),
                  "again": data_model.warm_after_ready() is None}))
"""


def test_the_ome_xml_model_is_warmed_after_the_announce_off_the_splash():
    """`ome_types` (0.6-0.8 s of imports, needed by every project's first
    open) is not paid in the prime, which holds the desktop app's splash, but
    on a background thread the desktop server starts after its ready line.

    That thread runs beside live requests, so the prime's rule still holds
    for it: it may dlopen nothing. The prime loads ome_types' one native
    library, and this checks the warm-up adds no other -- a new ome_types
    that brought one (lxml, say) would have to be added to the prime.

    In a new interpreter, because this suite has imported ome_types long
    before this test runs.
    """
    import json
    import subprocess
    from pathlib import Path

    result = subprocess.run([sys.executable, "-c", _WARM_PROBE],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-2000:]
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert found == {"primed": False, "core": True, "started": True, "warmed": True,
                     "new_native": [], "again": True}


def test_the_warm_up_waits_for_the_windows_first_page_to_finish():
    """Started at the announce, the import shared the GIL with the window's
    first page and lengthened its load by about 60 ms; so it waits for the
    server to have been busy and then quiet. Never forever: a window that
    never asks, or a server never idle, gets it at the cap."""
    import time

    from plexora.server.models.data_model import _wait_for_quiet

    states = iter([False] * 20 + [True, True, False, True] + [False] * 1000)
    asked = []

    def busy():
        asked.append(time.monotonic())
        return next(states)

    started = time.monotonic()
    _wait_for_quiet(busy, quiet=0.05, cap=5.0, poll=0.01)
    # Quiet before the first request does not count (0.2 s of it here), and
    # a request after a short gap restarts the quiet period: 24 readings,
    # then >= 0.05 s idle.
    assert len(asked) >= 24 + 5
    assert asked[-1] - asked[23] >= 0.05
    assert time.monotonic() - started < 5.0

    started = time.monotonic()
    _wait_for_quiet(lambda: False, quiet=0.01, cap=0.2, poll=0.01)
    assert 0.2 <= time.monotonic() - started < 2.0

    started = time.monotonic()
    _wait_for_quiet(lambda: True, quiet=0.01, cap=0.2, poll=0.01)
    assert 0.2 <= time.monotonic() - started < 2.0

    def broken():
        raise RuntimeError("no counts")

    started = time.monotonic()
    _wait_for_quiet(broken, quiet=0.01, cap=5.0, poll=0.01)
    assert time.monotonic() - started < 1.0


def test_the_desktop_server_reads_waitress_for_busy():
    """cli._waitress_busy against a real waitress 3 server: idle once it has
    started, busy while a request is in hand, idle after."""
    import threading
    import time
    import urllib.request

    from waitress.server import create_server

    from plexora import cli

    release = threading.Event()

    def app(environ, start_response):
        release.wait(10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    server = create_server(app, host="127.0.0.1", port=0, threads=2)
    busy = cli._waitress_busy(server)
    assert busy is not None
    stopping = threading.Event()

    def serve():
        # Closing the sockets under a running loop can make its select()
        # raise; that is the test stopping it, not a failure.
        try:
            server.run()
        except Exception:
            if not stopping.is_set():
                raise

    runner = threading.Thread(target=serve, daemon=True)
    runner.start()
    try:
        deadline = time.monotonic() + 5
        while busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not busy()
        asking = threading.Thread(target=lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{server.effective_port}/", timeout=10).read())
        asking.start()
        deadline = time.monotonic() + 5
        while not busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert busy()
        release.set()
        asking.join(10)
        deadline = time.monotonic() + 5
        while busy() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not busy()
    finally:
        release.set()
        stopping.set()
        server.close()
        runner.join(5)

    assert cli._waitress_busy(object()) is None


def test_priming_is_idempotent_and_quick_the_second_time():
    import time

    from plexora.server.models import data_model

    data_model.prime_hot_code()
    started = time.monotonic()
    data_model.prime_hot_code()
    # Everything is resident, so the second pass is a handful of tiny
    # encodes and one 96-point fit. The bound is generous: what it guards
    # against is a step that re-does real work every call.
    assert time.monotonic() - started < 5.0


def test_the_node_primes_before_it_announces(monkeypatch, tmp_path):
    """Order is the contract: prime, then announce, then serve.

    The announce line is what `plexora connect` parses to open the tunnel and
    register the node, so anything after it shares the process with live
    traffic. Priming after the announce would reopen the exact race this
    exists to close.
    """
    import waitress

    from plexora.server.models import data_model
    from plexora.server.node import app as node_app

    lines = []
    monkeypatch.setattr(data_model, "prime_hot_code",
                        lambda log=None: lines.append("PRIMED"))
    monkeypatch.setattr(node_app, "warm_resources",
                        lambda *args, **kwargs: lines.append("WARMED"))
    monkeypatch.setattr(waitress, "serve",
                        lambda *args, **kwargs: lines.append("SERVED"))

    node_app.serve_node([], token="tokentokentoken1", port=8642,
                        node_id="prime-order-test", dynamic=True,
                        manifest=tmp_path / "manifest.json",
                        log=lambda line="": lines.append(str(line)))

    announce = next(index for index, line in enumerate(lines)
                    if line.startswith("[plexora-node]"))
    assert lines.index("PRIMED") < announce
    assert announce < lines.index("WARMED") < lines.index("SERVED")


def test_the_mcp_server_primes_before_it_is_built(monkeypatch):
    """`plexora mcp serve` runs QC and gating jobs in its own process, beside
    the threads that answer tool calls, so it primes like the node does:
    before anything that could take a call exists."""
    from plexora.mcp import server as mcp_server
    from plexora.server.models import data_model

    lines = []

    class FakeServer:
        def run(self, *args, **kwargs):
            lines.append("SERVED")

    def fake_build(*args, **kwargs):
        lines.append("BUILT")
        return FakeServer()

    monkeypatch.setattr(data_model, "prime_hot_code",
                        lambda log=None: lines.append("PRIMED"))
    monkeypatch.setattr(mcp_server, "build_server", fake_build)

    mcp_server.serve(transport="stdio")

    assert lines == ["PRIMED", "BUILT", "SERVED"]


def test_the_announce_says_which_operating_system_answered(monkeypatch,
                                                           tmp_path):
    """The machine answering for itself. A workstation profile records which
    of Windows/macOS/Linux somebody picked on a form, and picking the wrong one
    builds every command line for the wrong shell -- so the far side states
    what it actually is and the launching end compares.

    Parsed back with the real parser rather than by matching a substring, so
    the two ends of this cannot drift apart without a test noticing.
    """
    import waitress

    from plexora import connect
    from plexora._resources import PLATFORMS, platform_word
    from plexora.server.models import data_model
    from plexora.server.node import app as node_app

    lines = []
    monkeypatch.setattr(data_model, "prime_hot_code", lambda log=None: None)
    monkeypatch.setattr(node_app, "warm_resources",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(waitress, "serve", lambda *args, **kwargs: None)

    node_app.serve_node([], token="tokentokentoken1", port=8642,
                        node_id="platform-test", dynamic=True,
                        manifest=tmp_path / "manifest.json",
                        log=lambda line="": lines.append(str(line)))

    announced = next(
        found for found in (connect.parse_node_announce(line)
                            for line in lines) if found)
    assert announced["platform"] == platform_word()
    assert announced["platform"] in PLATFORMS
    # And the fields that were already there still parse beside it: a reader
    # too old to know about `platform` must not be broken by its arrival.
    assert announced["token"] == "tokentokentoken1"
    assert announced["hostname"]
