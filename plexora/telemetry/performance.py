"""Where a tile's time went, on the thread that served it.

A request arms a recorder with `begin`, the tile path drops `mark`s as each
phase ends, and `end` hands back the phases -- which become a `Server-Timing`
header whether or not telemetry is on (the browser's own devtools show it,
and it costs nothing) and, when it is on, histogram observations.

Flask-free and allocation-light on purpose: `mark` on a thread that never
called `begin` is one `getattr`, which is what every non-tile request pays.
"""

from __future__ import annotations

import threading
from time import perf_counter

_local = threading.local()


class Phases:
    __slots__ = ("kind", "start", "last", "spans", "notes")

    def __init__(self, kind):
        self.kind = kind
        self.start = self.last = perf_counter()
        #: `[(name, ms)]`, in the order the phases ended. A name that ends
        #: twice (two reads for one tile) adds up.
        self.spans = []
        self.notes = {}

    def total_ms(self) -> float:
        return (self.last - self.start) * 1000.0

    def ms(self, name) -> float | None:
        found = [ms for span, ms in self.spans if span == name]
        return sum(found) if found else None


def begin(kind: str) -> None:
    _local.phases = Phases(kind)


def mark(name: str) -> None:
    """End the phase called `name` now (it began at the previous mark)."""
    phases = getattr(_local, "phases", None)
    if phases is None:
        return
    now = perf_counter()
    phases.spans.append((name, (now - phases.last) * 1000.0))
    phases.last = now


def skip() -> None:
    """Restart the clock without recording (time that belongs to no phase)."""
    phases = getattr(_local, "phases", None)
    if phases is not None:
        phases.last = perf_counter()


def note(name: str, value: str) -> None:
    phases = getattr(_local, "phases", None)
    if phases is not None:
        phases.notes[name] = value


def active() -> bool:
    return getattr(_local, "phases", None) is not None


def end() -> Phases | None:
    phases = getattr(_local, "phases", None)
    _local.phases = None
    if phases is not None:
        phases.last = max(phases.last, perf_counter())
    return phases


_HEADER_NAMES = {"read": "read", "node": "node", "lut": "lut", "enc": "enc"}


def server_timing_header(phases: Phases | None, total_ms: float | None = None) -> str:
    """`read;dur=7.9, lut;dur=1.1, enc;dur=21.0, total;dur=30.5, cache;desc=miss`."""
    if phases is None:
        return ""
    parts = []
    seen = set()
    for name, _ms in phases.spans:
        if name in seen or name not in _HEADER_NAMES:
            continue
        seen.add(name)
        parts.append(f"{_HEADER_NAMES[name]};dur={phases.ms(name):.1f}")
    total = phases.total_ms() if total_ms is None else total_ms
    parts.append(f"total;dur={total:.1f}")
    cache = phases.notes.get("cache")
    if cache in ("hit", "miss"):
        parts.append(f"cache;desc={cache}")
    return ", ".join(parts)
