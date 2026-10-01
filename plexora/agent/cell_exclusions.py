"""Cells left out of estimation: the seam between a QC layer and its consumers.

Automatic gating fits mixtures, samples strata and draws validation fields
from a table's values. Cells QC has called failures -- inside a fold, a blurred
field, a misregistered patch, a bad segmentation -- would bias every one of
those, so the estimates are made on the QC-passed cells only. The gates that
come out still apply to every cell: QC is an annotation layer, never a removal,
and what is left out here is left out of *estimation and evidence*, nothing
else.

Core cannot name the plugin that knows which cells failed (a gating build must
not import QC), so a plugin offers it through `Plugin.cell_exclusions_factory`:
a zero-argument factory returning `provider(ds, mode) -> ExclusionRecord |
None`. `current(ds)` asks every installed provider and merges their answers.

The mode is a context, not an argument threaded through forty functions:

- ``strict`` (the default): cells whose call is exclude or warn, and for one
  marker the cells QC flagged that marker unreliable in;
- ``exclude``: exclude calls and the per-marker flags, warn calls kept;
- ``off``: nothing left out.

A capability whose input carries a ``qc`` field runs under that mode (the
registry and the job runner open the scope); a gating session's handlers open
the one its options chose. On a data node there is no QC store to ask, so the
caller resolves the record and sends it in the payload (`encode`/`decode`), and
the node runs under `using(record)`.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import threading
import zlib
from dataclasses import dataclass, field

import numpy as np

#: The modes, strictest first. One vocabulary for every input that takes `qc`.
MODES = ("strict", "exclude", "off")
DEFAULT_MODE = "strict"

#: A validation field (or any window of tissue shown as evidence) whose cells
#: are more than this fraction QC-excluded is not used: it sits on a fold or a
#: blurred patch, and its counts say nothing about the gate.
FIELD_QC_MAX_FRACTION = 0.25

#: Above this fraction of the image left out, the result says so and the
#: confidence of anything decided on what remains is capped.
HEAVY_EXCLUSION_FRACTION = 0.5

_MODE: contextvars.ContextVar = contextvars.ContextVar("plexora_qc_mode", default=None)
_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("plexora_qc_record",
                                                           default=None)
_NONE = object()


@dataclass
class ExclusionRecord:
    """Which cells are left out, by id.

    `whole` are left out for every marker; `by_marker[m]` only when estimating
    marker `m`. Both are sorted int64 id arrays (the ids `label_overlay.
    cell_ids` reads, which is the rule QC writes its calls under).
    `fingerprint` changes whenever the calls do, and is part of every cache
    key over masked values. `source` says where the calls came from.
    """

    whole: np.ndarray
    by_marker: dict = field(default_factory=dict)
    fingerprint: str = ""
    mode: str = DEFAULT_MODE
    source: dict = field(default_factory=dict)

    def ids_for(self, marker=None) -> np.ndarray:
        extra = self.by_marker.get(marker) if marker is not None else None
        if extra is None or not extra.size:
            return self.whole
        return np.union1d(self.whole, extra)

    @property
    def empty(self) -> bool:
        return not self.whole.size and not any(v.size for v in self.by_marker.values())


def _ids(values) -> np.ndarray:
    return np.unique(np.asarray(list(values) if not isinstance(values, np.ndarray)
                                else values, dtype=np.int64))


def make_record(whole, by_marker=None, *, fingerprint="", mode=DEFAULT_MODE,
                source=None) -> ExclusionRecord:
    return ExclusionRecord(_ids(whole), {str(k): _ids(v) for k, v in (by_marker or {}).items()},
                           fingerprint, mode, dict(source or {}))


# -- the mode -----------------------------------------------------------------


def current_mode() -> str:
    return _MODE.get() or DEFAULT_MODE


@contextlib.contextmanager
def mode(value):
    """Run the block under one exclusion mode. None leaves the current one."""
    if value is None:
        yield current_mode()
        return
    if value not in MODES:
        raise ValueError(f"qc must be one of {MODES}, not {value!r}")
    token = _MODE.set(value)
    try:
        yield value
    finally:
        _MODE.reset(token)


@contextlib.contextmanager
def using(record):
    """Run the block with this record (a node answering an operation): no
    provider is asked. `None` means "nothing is left out"."""
    token = _OVERRIDE.set(_NONE if record is None else record)
    try:
        yield record
    finally:
        _OVERRIDE.reset(token)


def scope_for(inp):
    """The scope a capability's input asks for (its `qc` field), if any."""
    return mode(getattr(inp, "qc", None))


# -- providers ----------------------------------------------------------------

_PROVIDERS = None
_PROVIDERS_LOCK = threading.Lock()


def _load_providers() -> list:
    from plexora.server import plugins as plugin_registry

    names = plugin_registry.requested()
    if names is None:
        names = plugin_registry.available_names()
    found = []
    for name in names:
        if name not in plugin_registry.available_names():
            continue
        plugin = plugin_registry.load(name)
        factory = getattr(plugin, "cell_exclusions_factory", None) if plugin else None
        if factory is None:
            continue
        try:
            provider = factory()
        except Exception as exc:  # pragma: no cover - a broken third-party plugin
            print(f"WARNING: plugin {name!r} cell exclusions failed to load: {exc}")
            continue
        if provider is not None:
            found.append((name, provider))
    return found


def providers() -> list:
    global _PROVIDERS
    with _PROVIDERS_LOCK:
        if _PROVIDERS is None:
            _PROVIDERS = _load_providers()
        return list(_PROVIDERS)


def _reset_for_tests():
    global _PROVIDERS
    with _PROVIDERS_LOCK:
        _PROVIDERS = None


def _merge(records) -> ExclusionRecord | None:
    records = [r for r in records if r is not None]
    if not records:
        return None
    if len(records) == 1:
        return records[0]
    by_marker = {}
    for r in records:
        for marker, ids in r.by_marker.items():
            by_marker[marker] = np.union1d(by_marker.get(marker, np.empty(0, np.int64)), ids)
    whole = records[0].whole
    for r in records[1:]:
        whole = np.union1d(whole, r.whole)
    fingerprint = hashlib.sha1("|".join(r.fingerprint for r in records).encode()).hexdigest()[:16]
    return ExclusionRecord(whole, by_marker, fingerprint, records[0].mode,
                           {"providers": [r.source for r in records]})


def for_project(ds, *, mode_value=None) -> ExclusionRecord | None:
    """Ask every provider about `ds` under a mode; None when nothing answers."""
    wanted = mode_value or current_mode()
    if wanted == "off":
        return None
    answers = []
    for name, provider in providers():
        try:
            record = provider(ds, wanted)
        except Exception as exc:
            # A QC store that cannot be read must not stop gating; it is
            # reported in the result instead (`describe`).
            record = ExclusionRecord(np.empty(0, np.int64), {}, f"error:{name}", wanted,
                                     {"provider": name, "error": str(exc)})
        if record is not None:
            record.source.setdefault("provider", name)
            answers.append(record)
    return _merge(answers)


def current(ds) -> ExclusionRecord | None:
    """The record every estimate over `ds` uses right now."""
    override = _OVERRIDE.get()
    if override is _NONE:
        return None
    if override is not None:
        return override
    if not getattr(getattr(ds, "table", None), "available", False):
        return None
    return for_project(ds)


def fingerprint(record) -> str:
    """Part of a cache key: "" when nothing is left out."""
    if record is None or record.empty:
        return ""
    return f"{record.mode}:{record.fingerprint}"


# -- masks --------------------------------------------------------------------


def keep_mask(record, ids, marker=None) -> np.ndarray:
    """Row-aligned: True for the rows estimation may use."""
    ids = np.asarray(ids, dtype=np.int64)
    if record is None:
        return np.ones(ids.shape[0], dtype=bool)
    left = record.ids_for(marker)
    if not left.size:
        return np.ones(ids.shape[0], dtype=bool)
    return ~np.isin(ids, left, assume_unique=False)


def row_ids(ds) -> np.ndarray:
    """The table's cell ids, row-aligned (-1 where a row has none). Cached."""
    def compute():
        from plexora.server.utils.label_overlay import cell_ids

        frame = ds.table.geometry()
        n = int(frame.height) if frame is not None else 0
        out = np.full(n, -1, dtype=np.int64)
        if frame is not None:
            raw, keep = cell_ids(frame, ds.schema.cell_id if ds.schema else None)
            out[keep] = raw.astype(np.int64)
        return out

    return ds.cached(("qc.row_ids",), compute)


def row_mask(ds, marker=None, record=_NONE) -> np.ndarray | None:
    """Row-aligned keep mask for `ds` under the current record; None when
    nothing is left out (the caller then skips the work entirely)."""
    record = current(ds) if record is _NONE else record
    if record is None or not record.ids_for(marker).size:
        return None
    fp = fingerprint(record)

    def compute():
        return keep_mask(record, row_ids(ds), marker)

    return ds.cached(("qc.row_mask", marker if marker in record.by_marker else None, fp),
                     compute)


def masked(ds, marker, values, record=_NONE):
    """`values` (row-aligned) with the left-out rows set to NaN, as float."""
    keep = row_mask(ds, marker, record)
    values = np.asarray(values)
    if keep is None or keep.shape[0] != values.shape[0]:
        return values
    out = values.astype(np.float32 if values.dtype == np.float32 else np.float64, copy=True)
    out[~keep] = np.nan
    return out


def describe(ds, record=_NONE, marker=None, n_total=None) -> dict:
    """The `qc_exclusion` block every result built on masked values carries."""
    wanted = current_mode()
    record = current(ds) if record is _NONE else record
    if wanted == "off" and record is None:
        return {"mode": "off", "applied": False, "reason": "qc='off' was asked for"}
    if record is None:
        return {"mode": wanted, "applied": False, "reason": "no QC result for this image"}
    if record.source.get("error"):
        return {"mode": record.mode, "applied": False,
                "reason": f"QC calls could not be read: {record.source['error']}"}
    keep = row_mask(ds, marker, record)
    n = int(n_total if n_total is not None else (keep.shape[0] if keep is not None else 0))
    if keep is None:
        left = 0
    else:
        left = int((~keep).sum())
    out = {"mode": record.mode, "applied": True, "n_left_out": left, "n_cells": n,
           "fraction": round(left / n, 4) if n else None,
           "fingerprint": record.fingerprint}
    for key in ("result_id", "origin", "n_exclude", "n_warn", "n_marker", "regions"):
        if key in record.source:
            out[key] = record.source[key]
    if marker is not None:
        out["marker"] = marker
        out["n_marker_only"] = int(record.by_marker.get(marker, np.empty(0)).size)
    if out["fraction"] is not None and out["fraction"] > HEAVY_EXCLUSION_FRACTION:
        out["warning"] = (f"{out['fraction']:.0%} of this image's cells failed QC: what is "
                          "estimated here describes the remainder only")
    return out


def heavy(block) -> bool:
    return bool(block and block.get("applied")
                and (block.get("fraction") or 0) > HEAVY_EXCLUSION_FRACTION)


# -- the wire (a data node has no QC store to ask) -----------------------------


def _pack(ids) -> str:
    ids = np.asarray(ids, dtype=np.int64)
    deltas = np.diff(ids, prepend=np.int64(0)) if ids.size else ids
    return base64.b64encode(zlib.compress(deltas.astype("<i8").tobytes(), 6)).decode("ascii")


def _unpack(text) -> np.ndarray:
    raw = zlib.decompress(base64.b64decode(text.encode("ascii")))
    return np.cumsum(np.frombuffer(raw, dtype="<i8")).astype(np.int64)


def encode(record) -> dict:
    """JSON for an operation payload; `{"off": true}` when nothing is left out."""
    if record is None:
        return {"off": True}
    return {"fp": record.fingerprint, "mode": record.mode, "whole": _pack(record.whole),
            "by_marker": {m: _pack(v) for m, v in record.by_marker.items()},
            "source": {k: v for k, v in record.source.items()
                       if isinstance(v, (str, int, float, bool, type(None)))}}


def decode(payload) -> ExclusionRecord | None:
    if not payload or payload.get("off"):
        return None
    return ExclusionRecord(_unpack(payload["whole"]),
                           {m: _unpack(v) for m, v in (payload.get("by_marker") or {}).items()},
                           payload.get("fp") or "", payload.get("mode") or DEFAULT_MODE,
                           dict(payload.get("source") or {}))


PAYLOAD_KEY = "_qc_exclusion"


def attach(ds, payload) -> dict:
    """`payload` with the current record riding along (for a node)."""
    out = dict(payload or {})
    out[PAYLOAD_KEY] = encode(current(ds))
    return out


@contextlib.contextmanager
def from_payload(payload):
    """On the side that runs an operation: the record the caller sent, or the
    normal lookup when none came (a local call)."""
    if isinstance(payload, dict) and PAYLOAD_KEY in payload:
        with using(decode(payload[PAYLOAD_KEY])) as record:
            yield record
    else:
        yield None
