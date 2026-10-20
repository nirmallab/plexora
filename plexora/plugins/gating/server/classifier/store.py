"""Where the classifier keeps what it knows, in gating's own store.

- **Examples** -- a store table (`classifier_examples`): marker, cell id (the
  mask label, never a row), label 1/0, when, and whether a click or an import
  put it there.
- **State** -- one JSON document (`classifier/state.json`): the panel's mode,
  and per marker which source is active, the baseline captured at its first
  run, the current run's record, and the layer a gating session made at its
  gate (`session_run`: drawn and exported, never deciding until adopted).
- **Arrays** -- one `.npz` per run (`classifier/<marker>/run_<id>.npz`), row
  aligned to the table: calls (1/0/-1), scores, the uncertain band; a
  session's run adds the classifier's own 0.5 call (`raw_calls`) and the
  gate's (`gate_calls`), so where the two part ways stays on record.

Every write is atomic -- a `.part` file replaced into place -- and made under
a per-project lock, so a run finishing while the eraser is pressed leaves one
consistent answer, never half of each.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import threading
from pathlib import Path

import numpy as np
import polars as pl

from plexora import api

PLUGIN = "gating"
EXAMPLES_TABLE = "classifier_examples"
FOLDER = "classifier"
STATE_FILE = "state.json"

_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def lock(project) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project, threading.RLock())


def _store(project):
    return api.store(project, PLUGIN)


def folder(project) -> Path:
    path = Path(_store(project).directory()) / FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(marker) -> str:
    """A file name for a marker: readable, and never two markers the same."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(marker)).strip("._") or "marker"
    return f"{text[:48]}-{hashlib.sha1(str(marker).encode('utf-8')).hexdigest()[:8]}"


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with open(part, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, path)


# -- state ----------------------------------------------------------------


def read_state(project) -> dict:
    path = folder(project) / STATE_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = {}
    document.setdefault("mode", "threshold")
    document.setdefault("markers", {})
    return document


def write_state(project, document) -> None:
    write_atomic(folder(project) / STATE_FILE,
                 json.dumps(document, indent=1, sort_keys=True).encode("utf-8"))


@contextlib.contextmanager
def editing(project):
    """The state document to change in place, written back on success."""
    with lock(project):
        document = read_state(project)
        yield document
        write_state(project, document)


def marker_state(document, marker) -> dict:
    entry = document["markers"].setdefault(marker, {})
    entry.setdefault("active", "threshold")
    entry.setdefault("baseline", None)
    entry.setdefault("run", None)
    entry.setdefault("session_run", None)
    return entry


def run_record(project, marker) -> dict | None:
    return (read_state(project)["markers"].get(marker) or {}).get("run")


def session_run_record(project, marker) -> dict | None:
    """The layer a gating session made for the marker at its gate, or None."""
    return (read_state(project)["markers"].get(marker) or {}).get("session_run")


def kept_run_ids(entry) -> set:
    """The run files a marker's state still points at."""
    return {r["run_id"] for r in (entry.get("run"), entry.get("session_run"))
            if isinstance(r, dict) and r.get("run_id")}


def active(project, marker) -> str:
    entry = read_state(project)["markers"].get(marker) or {}
    if entry.get("active") == "classifier" and entry.get("run"):
        return "classifier"
    return "threshold"


# -- examples -------------------------------------------------------------

_EXAMPLE_SCHEMA = {"marker": pl.Utf8, "cell_id": pl.Int64, "label": pl.Int8,
                   "created": pl.Utf8, "origin": pl.Utf8}


def all_examples(project) -> pl.DataFrame:
    frame = _store(project).get_table(EXAMPLES_TABLE)
    if frame is None or frame.height == 0:
        return pl.DataFrame(schema=_EXAMPLE_SCHEMA)
    return frame.select([pl.col(k).cast(v) for k, v in _EXAMPLE_SCHEMA.items()])


def put_examples(project, frame: pl.DataFrame) -> None:
    frame = frame.select([pl.col(k).cast(v) for k, v in _EXAMPLE_SCHEMA.items()])
    _store(project).put_table(EXAMPLES_TABLE, frame.unique(["marker", "cell_id"], keep="last",
                                                           maintain_order=True))


def examples(project, marker) -> tuple[list, list]:
    """(positive ids, negative ids) for one marker, oldest first."""
    frame = all_examples(project).filter(pl.col("marker") == marker)
    pos = frame.filter(pl.col("label") == 1)["cell_id"].to_list()
    neg = frame.filter(pl.col("label") == 0)["cell_id"].to_list()
    return [int(v) for v in pos], [int(v) for v in neg]


def origins(project, marker) -> dict:
    """{cell id: origin} for one marker's examples ("click", "import", "auto",
    "agent")."""
    frame = all_examples(project).filter(pl.col("marker") == marker)
    return {int(c): str(o) for c, o in zip(frame["cell_id"].to_list(),
                                           frame["origin"].to_list())}


def set_auto(project, marker, pos, neg) -> None:
    """The wand's picks for one marker: its earlier picks are replaced, and
    the examples a person gave are kept (and win over a pick of the same
    cell)."""
    with lock(project):
        frame = all_examples(project)
        frame = frame.filter(~((pl.col("marker") == marker) & (pl.col("origin") == "auto")))
        kept = set(frame.filter(pl.col("marker") == marker)["cell_id"].to_list())
        stamp = now()
        rows = ([(marker, int(v), 1, stamp, "auto") for v in pos if int(v) not in kept]
                + [(marker, int(v), 0, stamp, "auto") for v in neg if int(v) not in kept])
        if rows:
            frame = pl.concat([frame, pl.DataFrame(rows, schema=_EXAMPLE_SCHEMA, orient="row")])
        put_examples(project, frame)


def counts(project) -> dict:
    """{marker: (n positive, n negative)} for every marker with examples."""
    frame = all_examples(project)
    out = {}
    for row in frame.group_by(["marker", "label"]).len().iter_rows(named=True):
        pos, neg = out.get(row["marker"], (0, 0))
        if int(row["label"]) == 1:
            pos = int(row["len"])
        else:
            neg = int(row["len"])
        out[row["marker"]] = (pos, neg)
    return out


def set_label(project, marker, cell_id, label, origin="click") -> str | None:
    """Label one cell: `label` 1, 0 or None (remove). Returns the label it
    had before ("positive", "negative" or None)."""
    with lock(project):
        frame = all_examples(project)
        hit = frame.filter((pl.col("marker") == marker) & (pl.col("cell_id") == int(cell_id)))
        before = None
        if hit.height:
            before = "positive" if int(hit["label"][0]) == 1 else "negative"
        frame = frame.filter(~((pl.col("marker") == marker)
                               & (pl.col("cell_id") == int(cell_id))))
        if label is not None:
            frame = pl.concat([frame, pl.DataFrame(
                {"marker": [marker], "cell_id": [int(cell_id)], "label": [int(label)],
                 "created": [now()], "origin": [origin]}, schema=_EXAMPLE_SCHEMA)])
        put_examples(project, frame)
        return before


def replace_markers(project, by_marker: dict, origin="import", merge=False) -> None:
    """Set the examples of the markers in `by_marker` ({marker: (pos, neg)}).
    `merge` keeps a marker's other examples, the new labels winning; without
    it a named marker's examples are exactly the new ones. Markers not named
    are never touched."""
    with lock(project):
        frame = all_examples(project)
        stamp = now()
        for marker, (pos, neg) in by_marker.items():
            new_ids = {int(v) for v in pos} | {int(v) for v in neg}
            if merge:
                frame = frame.filter(~((pl.col("marker") == marker)
                                       & pl.col("cell_id").is_in(sorted(new_ids))))
            else:
                frame = frame.filter(pl.col("marker") != marker)
            rows = ([(marker, int(v), 1, stamp, origin) for v in pos]
                    + [(marker, int(v), 0, stamp, origin) for v in neg])
            if rows:
                frame = pl.concat([frame, pl.DataFrame(rows, schema=_EXAMPLE_SCHEMA,
                                                       orient="row")])
        put_examples(project, frame)


def clear_examples(project, markers=None) -> int:
    """Forget the examples of `markers` (every marker when None); how many."""
    with lock(project):
        frame = all_examples(project)
        gone = frame if markers is None else frame.filter(pl.col("marker").is_in(list(markers)))
        kept = frame.head(0) if markers is None else frame.filter(
            ~pl.col("marker").is_in(list(markers)))
        put_examples(project, kept)
        return int(gone.height)


# -- arrays ---------------------------------------------------------------


def run_path(project, marker, run_id) -> Path:
    return folder(project) / safe_name(marker) / f"run_{run_id}.npz"


def write_arrays(project, marker, run_id, *, calls, scores, uncertain, agreement=None,
                 raw_calls=None, gate_calls=None) -> Path:
    """`agreement`: the share of resampled models agreeing with each call,
    kept as a whole percent (learner.py). `raw_calls`, `gate_calls`: a
    session run's classifier-alone and gate calls (1/0/-1)."""
    import io

    buffer = io.BytesIO()
    extra = {}
    if agreement is not None:
        extra["agreement"] = np.round(np.nan_to_num(np.asarray(agreement, dtype=np.float32),
                                                    nan=0.0) * 100).astype(np.uint8)
    for name, values in (("raw_calls", raw_calls), ("gate_calls", gate_calls)):
        if values is not None:
            extra[name] = np.asarray(values, dtype=np.int8)
    np.savez_compressed(buffer, calls=np.asarray(calls, dtype=np.int8),
                        scores=np.asarray(scores, dtype=np.float32),
                        uncertain=np.asarray(uncertain, dtype=bool), **extra)
    path = run_path(project, marker, run_id)
    write_atomic(path, buffer.getvalue())
    return path


def read_arrays(project, marker, run_id) -> dict | None:
    path = run_path(project, marker, run_id)
    try:
        with np.load(path, allow_pickle=False) as held:
            out = {name: np.asarray(held[name]) for name in ("calls", "scores", "uncertain")}
            for name in ("agreement", "raw_calls", "gate_calls"):
                out[name] = np.asarray(held[name]) if name in held.files else None
            return out
    except (OSError, KeyError, ValueError):
        return None


def remove_runs(project, marker, keep=None) -> None:
    """Delete a marker's run files, all but `keep`'s (a run id or a set of
    them)."""
    directory = folder(project) / safe_name(marker)
    if not directory.is_dir():
        return
    keep = set() if keep is None else ({keep} if isinstance(keep, str) else set(keep))
    names = {f"run_{k}.npz" for k in keep}
    for path in directory.glob("run_*.npz"):
        if path.name not in names:
            path.unlink(missing_ok=True)
