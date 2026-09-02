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
