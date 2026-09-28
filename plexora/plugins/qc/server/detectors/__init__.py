"""Candidate detectors: the classical ones, and any a package registers.

`run_all` runs every enabled detector that says it can run on this scan and
never raises for one that cannot -- an unavailable detector is reported in
`skipped` with its reason, which the report and the session status show.
"""

from __future__ import annotations

from plexora.plugins.qc.server.detectors.base import (Candidate, DetectorContext,  # noqa: F401
                                                        DetectorRequires, QCDetector)

ENTRY_POINT_GROUP = "plexora.qc_detectors"


def builtin():
    from plexora.plugins.qc.server.detectors.classical import BUILTIN

    return BUILTIN


def discover_external() -> tuple:
    """Detectors third-party packages register under `plexora.qc_detectors`."""
    try:
        from importlib.metadata import entry_points

        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        return ()
    out = []
    for entry in found:
        try:
            detector = entry.load()
            out.append(detector() if isinstance(detector, type) else detector)
        except Exception as exc:  # pragma: no cover - a broken third-party package
            print(f"WARNING: QC detector {entry.name!r} failed to load: {exc}")
    return tuple(out)


def all_detectors() -> tuple:
    return tuple(builtin()) + discover_external()


def versions(detectors=None) -> dict:
    return {d.name: d.version for d in (detectors or all_detectors())}


def run_all(context, *, enabled=None):
    """(candidates, skipped[{name, reason}]) of every enabled detector."""
    candidates, skipped = [], []
    for detector in all_detectors():
        if enabled is not None and detector.name not in enabled:
            continue
        try:
            ok, reason = detector.available(context)
        except Exception as exc:
            ok, reason = False, f"could not check: {exc}"
        if not ok:
            skipped.append({"name": detector.name, "reason": reason})
            continue
        try:
            candidates.extend(detector.run(context))
        except Exception as exc:
            skipped.append({"name": detector.name, "reason": f"failed: {exc}"})
    return candidates, skipped
