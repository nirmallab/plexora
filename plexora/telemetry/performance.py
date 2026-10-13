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
    __slots__ = ("kind", "start", "last", "spans", "notes", "parts")

    def __init__(self, kind):
        self.kind = kind
        self.start = self.last = perf_counter()
        #: `[(name, ms)]`, in the order the phases ended. A name that ends
        #: twice (two reads for one tile) adds up.
        self.spans = []
        self.notes = {}
        #: `[(name, ms)]`: what a phase was made of (`part`) -- a read's
        #: opening, its chunk-cache lookups, raw I/O and inflating. Added from
        #: whichever thread did the work, so parts done in parallel may add up
        #: to more than the phase they divide.
        self.parts = []

    def total_ms(self) -> float:
        return (self.last - self.start) * 1000.0

    def ms(self, name) -> float | None:
        found = [ms for span, ms in self.spans if span == name]
        return sum(found) if found else None

    def part_ms(self, name) -> float | None:
        found = [ms for part, ms in list(self.parts) if part == name]
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


def current() -> Phases | None:
    """This thread's recorder, or None: for a read that hands its work to other
    threads, which add their `part`s to it."""
    return getattr(_local, "phases", None)


def part(name: str, ms: float, phases: Phases | None = None) -> None:
    """Add `ms` to the part `name` of the phase under way, without ending it.

    The phase stays whole -- `read` is still the read, which is what every
    reader of the header already parses -- and its parts are reported beside
    it. `phases` is a recorder from `current()`, for work done on a thread
    other than the request's (a list append, so safe from any thread)."""
    if phases is None:
        phases = getattr(_local, "phases", None)
    if phases is not None:
        phases.parts.append((name, ms))


def active() -> bool:
    return getattr(_local, "phases", None) is not None


def end() -> Phases | None:
    phases = getattr(_local, "phases", None)
    _local.phases = None
    if phases is not None:
        phases.last = max(phases.last, perf_counter())
    return phases


_HEADER_NAMES = {"read": "read", "node": "node", "lut": "lut", "enc": "enc",
                 # the volume routes' phases (server/routes/volume_routes.py):
                 # chunk decode outside the reader's lock, time waiting for a
                 # gate or a lock, z max-pooling, quantization, the contrast
                 # hint, a brick manifest
                 "dec": "dec", "wait": "wait", "pool": "pool", "quant": "quant",
                 "hint": "hint", "manifest": "manifest"}

#: Parts of a tile's `read` (`part`), in the order the header lists them:
#: resolving the plane's pyramid (and a reader's dataset), the decoded-chunk
#: cache (`desc` hit when every chunk was held, miss when any was read), the
#: chunks' raw bytes, and decompressing them. A reader reports the parts it
#: can tell apart: an Imaris file all four, an OME-Zarr store `open` and
#: `chunk` (zarr reads and decodes in one call), a TIFF plane `open`.
_PART_NAMES = ("open", "chunk", "io", "inflate")


def server_timing_header(phases: Phases | None, total_ms: float | None = None) -> str:
    """`read;dur=7.9, lut;dur=1.1, enc;dur=21.0, total;dur=30.5, cache;desc=miss`,
    with the read's parts after it when it has any (`open;dur=0.2,
    chunk;dur=6.9;desc=miss, io;dur=4.1, inflate;dur=2.2`)."""
    if phases is None:
        return ""
    parts = []
    seen = set()
    for name, _ms in phases.spans:
        if name in seen or name not in _HEADER_NAMES:
            continue
        seen.add(name)
        parts.append(f"{_HEADER_NAMES[name]};dur={phases.ms(name):.1f}")
    if phases.parts:
        for name in _PART_NAMES:
            ms = phases.part_ms(name)
            if ms is None:
                continue
            entry = f"{name};dur={ms:.1f}"
            if name == "chunk" and phases.notes.get("chunk") in ("hit", "miss"):
                entry += f";desc={phases.notes['chunk']}"
            parts.append(entry)
    total = phases.total_ms() if total_ms is None else total_ms
    parts.append(f"total;dur={total:.1f}")
    cache = phases.notes.get("cache")
    if cache in ("hit", "miss"):
        parts.append(f"cache;desc={cache}")
    return ", ".join(parts)
