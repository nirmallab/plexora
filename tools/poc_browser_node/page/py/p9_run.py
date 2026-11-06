"""P9's gating session, the same code under CPython (bench/truth_p9.py) and
Pyodide (the page's worker, bench/p9_node.mjs).

`session(data_root, info)` runs tests/test_gating_session.py's oracle session
on the synthetic project and returns a dump: every packet (normalized: ids,
times and paths out; images as their bytes' hash plus the bytes), the
answers, the final units, the gates and per-phase times. `compare_p9.py`
reads two dumps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import time

PROJECT = "gsynth"
#: tests/test_gating_session.py's `start`, unchanged: engine v1, the defaults.
OPTIONS = {"engine": "v1", "scope": "project", "project": PROJECT}
#: Keys whose values name this run rather than what it computed.
VOLATILE = {"session_id", "packet_id", "operation_id", "job_id", "artifact_id", "receipt_id",
            "guide_version", "started", "finished", "elapsed_ms", "duration_ms", "ms", "seconds",
            "path", "src", "folder", "pid", "resource", "token", "trace_id", "as_in",
            "provenance_revision", "session_run"}
VOLATILE_SUFFIXES = ("_at", "_ms", "_s", "_id", "_path")
#: What S1 imports: the gating session's modules, as registry.discover loads them.
MODULES = (
    "plexora.paths", "plexora.agent", "plexora.agent.registry", "plexora.agent.jobs",
    "plexora.agent.session", "plexora.agent.render", "plexora.agent.evidence.crops",
    "plexora.server.providers.memory", "plexora.server.models.project",
    "plexora.plugins.gating.capabilities_session",
    "plexora.plugins.gating.server.autogate.engine",
    "plexora.plugins.gating.server.autogate.bulk",
    "plexora.plugins.gating.server.autogate.packets",
    "plexora.plugins.gating.server.autogate.cellcalls",
    "plexora.ai.bench", "plexora.licensing", "spatialagents", "spatialbridge",
)


def import_check():
    """S1: each module's import time or error, and what discover registered."""
    out = {"modules": {}, "python": sys.version.split()[0]}
    for name in MODULES:
        t0 = time.perf_counter()
        try:
            __import__(name)
            out["modules"][name] = round((time.perf_counter() - t0) * 1000, 1)
        except BaseException as exc:  # noqa: BLE001 -- the record names it
            out["modules"][name] = f"{type(exc).__name__}: {exc}"
    from plexora.agent import registry

    t0 = time.perf_counter()
    try:
        names = registry.discover(["gating"])
        out["discover"] = {"ms": round((time.perf_counter() - t0) * 1000, 1),
                           "count": len(names),
                           "gating": sorted(n for n in names if n.startswith("gating."))}
    except BaseException as exc:  # noqa: BLE001
        out["discover"] = f"{type(exc).__name__}: {exc}"
    try:
        from PIL import features

        out["webp"] = bool(features.check("webp"))
    except Exception as exc:  # noqa: BLE001
        out["webp"] = str(exc)
    try:
        import sqlite3

        out["sqlite"] = sqlite3.sqlite_version
    except Exception as exc:  # noqa: BLE001
        out["sqlite"] = str(exc)
    out["versions"] = versions()
    return out


#: What the worker runs before it says "ready": every module imported and the
#: gating capabilities registered, so the first AI call pays for none of it.
warm = import_check


def versions():
    from importlib import metadata

    names = ("plexora", "numpy", "scipy", "scikit-learn", "polars", "pillow", "pandas",
             "opencv-python", "opencv-python-headless", "h5py", "zarr", "pydantic",
             "pydantic-core", "tifffile", "spatialagents", "spatialbridge", "biocognia",
             "anndata")
    out = {}
    for name in names:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return out


def _image(entry):
    """A packet image names its artifact (`agent/artifacts.py`, a PNG the
    session rendered): its role, size, the PNG's hash and the PNG itself."""
    from plexora.agent import artifacts

    out = {k: entry.get(k) for k in ("role", "width", "height")}
    art = entry.get("artifact_id")
    if art:
        data, _ = artifacts.get(art)
        out.update({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                    "b64": base64.b64encode(data).decode("ascii")})
    return out


def normalize(value, roots=()):
    """`value` without what names this run (VOLATILE), bytes as hashes,
    data-root paths as `<root>`; floats kept for compare_p9's tolerance."""
    if isinstance(value, dict):
        return {k: normalize(v, roots) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
                if str(k) not in VOLATILE and not str(k).endswith(VOLATILE_SUFFIXES)}
    if isinstance(value, (list, tuple)):
        return [normalize(v, roots) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, str):
        for root in roots:
            value = value.replace(root, "<root>")
        return value
    if hasattr(value, "item") and callable(value.item):  # numpy scalars
        return value.item()
    return value


def session(data_root, info, options=None):
    """The oracle session on `info`'s project; returns the dump."""
    from plexora import paths
    from plexora.agent import AgentSession, invoke, jobs, registry

    import p9_oracle as oracle

    times = {}
    roots = [str(data_root), str(paths.data_root()), str(paths.data_root()).replace("\\", "/")]
    t0 = time.perf_counter()
    registry.discover(["gating"])
    times["discover"] = time.perf_counter() - t0

    s = AgentSession()
    agent = oracle.Oracle(info)
    t = time.perf_counter()
    started = oracle.ok(invoke(s, "gating_session_start", {**OPTIONS, **(options or {})}))
    times["start"] = time.perf_counter() - t
    t = time.perf_counter()
    jobs.drain(120)
    times["drain"] = time.perf_counter() - t
    t = time.perf_counter()
    oracle.answer_review(s, started)
    times["review"] = time.perf_counter() - t

    exchanges = []
    last = [time.perf_counter()]

    def on_packet(packet, answer):
        now = time.perf_counter()
        exchanges.append({"kind": packet["kind"],
                          "units": [u.get("marker") for u in packet.get("units") or []],
                          "wait": now - last[0],
                          "images": [_image(i) for i in packet.get("images") or []],
                          "json_bytes": len(json.dumps({k: v for k, v in packet.items()
                                                        if k != "images"}, default=str)),
                          "packet": normalize({k: v for k, v in packet.items() if k != "images"},
                                              roots),
                          "answer": normalize(answer, roots)})
        last[0] = time.perf_counter()

    t = time.perf_counter()
    oracle.drive(s, started["session_id"], agent, on_packet=on_packet)
    times["drive"] = time.perf_counter() - t

    # The session's own background work (the classifier job) settled before
    # the final state is read, as the suite's `_finish_agent_jobs` does: what
    # is compared is what the session computed, not how far a thread got.
    t = time.perf_counter()
    jobs.drain(120)
    times["settle"] = time.perf_counter() - t
    t = time.perf_counter()
    status = oracle.ok(invoke(s, "gating_session_status", {"session_id": started["session_id"],
                                                          "detail": "full"}))
    gates = oracle.ok(invoke(s, "get_all_gates", {"project": PROJECT}))
    finished = oracle.ok(invoke(s, "gating_session_finish", {"session_id": started["session_id"],
                                                             "action": "close"}))
    times["finish"] = time.perf_counter() - t
    times["total"] = time.perf_counter() - t0
    units = {u["marker"]: {"state": u.get("state"), "final": u.get("final"),
                           "confidence": u.get("confidence")} for u in status["units"]}
    excess = {m: agent.excess(m, u["final"]) for m, u in units.items()
              if u.get("final") is not None and m in agent.values}
    return {
        "summary": {"packets": len(exchanges), "kinds": [e["kind"] for e in exchanges],
                    "units": units, "excess": excess, "times": times},
        "started": normalize(started, roots),
        "exchanges": exchanges,
        "units": units,
        "gates": normalize(gates, roots),
        "finished": normalize(finished, roots),
        "versions": versions(),
        # compare_p9 lists what CPython imported that this run did not: an
        # import that failed quietly (a fallback taken) shows here.
        "modules": sorted(m for m in sys.modules if not m.startswith(("_", "p9_"))),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
