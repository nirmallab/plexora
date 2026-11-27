"""Notebooks on disk: one SQLite file per notebook, under `paths.scimap_root()`.

Figure Builder's arrangement, for its reason: a notebook is not datasource
state (a dataset notebook spans several projects), so `plexora.api.store`,
which is scoped to one datasource, cannot hold it.

A notebook is a JSON document -- its title, its context (a project or a
dataset), its results folder and its steps ("cells") with their settings --
plus the record of every run and a cache of each run's result summary (the
numbers its plots are drawn from). The document carries a revision; a write
that names a stale one is refused with `ConflictError`, so two tabs cannot
silently overwrite each other.

The results themselves are NOT here. They live in the working AnnData in the
results folder, where Scimap Pro wrote them, and an exported script reads them
from there. This file is the notebook's memory of what was asked and what came
back.

Where it lives: beside the data, in the results folder the notebook writes to
(`scimap_analysis/` by default), as

    <results>/notebooks/<notebook>/notebook.db
    <results>/notebooks/<notebook>/<NN-step>/<vN>/   params.json, results.parquet

so an analysis travels with its data. Plexora keeps only an index of where each
notebook is (`index.json` under `paths.scimap_root()`), so notebooks reopen
after a restart. Notebooks made before this layout stay in
`paths.scimap_root()/<id>/` and are found there.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 1
_ID = re.compile(r"^nb_[0-9a-f]{12}$")
_CELL_ID = re.compile(r"^c_[0-9a-f]{8}$")
_lock = threading.RLock()


class ConflictError(Exception):
    def __init__(self, current_revision):
        super().__init__("stale revision")
        self.current_revision = current_revision


class UnknownNotebook(KeyError):
    pass


def root() -> Path:
    from plexora import paths

    return paths.scimap_root()


def new_id() -> str:
    return "nb_" + secrets.token_hex(6)


def new_cell_id() -> str:
    return "c_" + secrets.token_hex(4)


def valid_id(notebook_id) -> bool:
    return bool(_ID.match(str(notebook_id or "")))


def valid_cell_id(cell_id) -> bool:
    return bool(_CELL_ID.match(str(cell_id or "")))


# -- where each notebook lives -------------------------------------------------

_index_lock = threading.RLock()


def _index_path() -> Path:
    return root() / "index.json"


def _read_index() -> dict:
    try:
        data = json.loads(_index_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(index: dict) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(index, indent=1, sort_keys=True))
    tmp.replace(path)


def notebook_dir(notebook_id) -> Path:
    """The notebook's own folder: its database, selections and versions."""
    if not valid_id(notebook_id):
        raise UnknownNotebook(notebook_id)
    entry = _read_index().get(notebook_id)
    if entry and entry.get("path"):
        return Path(entry["path"])
    return root() / notebook_id


def _db_path(notebook_id) -> Path:
    return notebook_dir(notebook_id) / "notebook.db"


def slug(text, fallback="notebook") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return cleaned[:60] or fallback


def place_for(results_dir, title) -> Path | None:
    """A new notebook's folder under `results_dir`, or None when that folder
    cannot be written (the notebook then lives in Plexora's own folder)."""
    if not results_dir:
        return None
    base = Path(results_dir) / "notebooks"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if not os.access(base, os.W_OK):
        return None
    name, i = slug(title), 2
    candidate = base / name
    while candidate.exists():
        candidate, i = base / f"{name}-{i}", i + 1
    return candidate


def move(notebook_id, results_dir) -> Path:
    """Move a notebook's folder (database and versions) under another results
    folder; returns the new folder."""
    with _lock, _index_lock:
        current = notebook_dir(notebook_id)
        document = load(notebook_id)
        target = place_for(results_dir, document.get("title"))
        if target is None:
            raise OSError(f"{results_dir} cannot be written to")
        if current.resolve() == target.parent.resolve():
            return current
        shutil.move(str(current), str(target))
        index = _read_index()
        index[notebook_id] = {"path": str(target)}
        _write_index(index)
        return target


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _init(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS document (id INTEGER PRIMARY KEY CHECK (id = 1),
                                             body TEXT NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, cell_id TEXT NOT NULL,
                                         started_at REAL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS results (run_id TEXT PRIMARY KEY, body TEXT NOT NULL);
    """)


def create(document: dict) -> dict:
    """Write a new notebook; returns it with its id and revision. It lives
    under its results folder when that can be written to."""
    with _lock:
        notebook_id = document.get("id") or new_id()
        folder = place_for(document.get("results_dir"), document.get("title"))
        if folder is not None:
            with _index_lock:
                index = _read_index()
                index[notebook_id] = {"path": str(folder)}
                _write_index(index)
        path = _db_path(notebook_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        document = {**document, "id": notebook_id, "schema": SCHEMA_VERSION,
                    "created_at": document.get("created_at") or now, "updated_at": now,
                    "revision": 1}
        document.setdefault("cells", [])
        with closing(_connect(path)) as con:
            _init(con)
            con.execute("INSERT OR REPLACE INTO document (id, body, revision) VALUES (1, ?, 1)",
                        (json.dumps(document),))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        return document


def exists(notebook_id) -> bool:
    try:
        return _db_path(notebook_id).is_file()
    except UnknownNotebook:
        return False


def load(notebook_id) -> dict:
    path = _db_path(notebook_id)
    if not path.is_file():
        raise UnknownNotebook(notebook_id)
    with _lock, closing(_connect(path)) as con:
        row = con.execute("SELECT body, revision FROM document WHERE id = 1").fetchone()
    if row is None:
        raise UnknownNotebook(notebook_id)
    document = json.loads(row[0])
    document["revision"] = row[1]
    from plexora.plugins.scimap.server import tools

    return tools.migrate(document)


def save(document: dict, expected_revision=None) -> dict:
    """Replace the document. `expected_revision` None skips the check (the
    runner's own writes, which always start from a fresh load under the lock)."""
    notebook_id = document["id"]
    path = _db_path(notebook_id)
    with _lock, closing(_connect(path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute("SELECT revision FROM document WHERE id = 1").fetchone()
            current = row[0] if row else 0
            if expected_revision is not None and int(expected_revision) != current:
                con.execute("ROLLBACK")
                raise ConflictError(current)
            document = {**document, "revision": current + 1, "updated_at": time.time()}
            con.execute("UPDATE document SET body = ?, revision = ? WHERE id = 1",
                        (json.dumps(document), current + 1))
            con.execute("COMMIT")
        except ConflictError:
            raise
        except Exception:
            con.execute("ROLLBACK")
            raise
    return document


def mutate(notebook_id, change, expected_revision=None) -> dict:
    """Load, apply `change(document)` in place, save -- atomically with respect
    to every other writer in this process."""
    with _lock:
        document = load(notebook_id)
        if expected_revision is not None and int(expected_revision) != document["revision"]:
            raise ConflictError(document["revision"])
        change(document)
        return save(document, document["revision"])


def delete(notebook_id) -> None:
    folder = notebook_dir(notebook_id)
    with _lock, _index_lock:
        shutil.rmtree(folder, ignore_errors=True)
        index = _read_index()
        if index.pop(notebook_id, None) is not None:
            _write_index(index)


def list_all() -> list:
    """Every notebook's summary, newest first. A damaged file is skipped, and a
    notebook whose folder is not reachable now (a drive not mounted) is left
    in the index for when it is."""
    out = []
    ids = set(_read_index())
    base = root()
    if base.is_dir():
        ids |= {c.name for c in base.iterdir() if valid_id(c.name)}
    for notebook_id in ids:
        if not valid_id(notebook_id) or not _db_path(notebook_id).is_file():
            continue
        try:
            document = load(notebook_id)
        except Exception:  # noqa: BLE001 -- one damaged notebook must not hide the rest
            continue
        out.append(summary(document))
    out.sort(key=lambda d: d.get("updated_at") or 0, reverse=True)
    return out


def summary(document) -> dict:
    return {"id": document["id"], "title": document.get("title") or "Untitled notebook",
            "context": document.get("context") or {}, "steps": len(document.get("cells") or []),
            "updated_at": document.get("updated_at")}


# -- runs and result summaries ---------------------------------------------


def put_run(notebook_id, run: dict) -> None:
    path = _db_path(notebook_id)
    with _lock, closing(_connect(path)) as con:
        con.execute("INSERT OR REPLACE INTO runs (run_id, cell_id, started_at, body) "
                    "VALUES (?, ?, ?, ?)",
                    (run["run_id"], run["cell_id"], run.get("started_at"), json.dumps(run)))


def runs(notebook_id, cell_id=None) -> list:
    path = _db_path(notebook_id)
    with _lock, closing(_connect(path)) as con:
        if cell_id:
            rows = con.execute("SELECT body FROM runs WHERE cell_id = ? ORDER BY started_at",
                               (cell_id,)).fetchall()
        else:
            rows = con.execute("SELECT body FROM runs ORDER BY started_at").fetchall()
    return [json.loads(r[0]) for r in rows]


def put_result(notebook_id, run_id, body: dict) -> None:
    path = _db_path(notebook_id)
    with _lock, closing(_connect(path)) as con:
        con.execute("INSERT OR REPLACE INTO results (run_id, body) VALUES (?, ?)",
                    (run_id, json.dumps(body, default=_jsonable)))


def get_result(notebook_id, run_id):
    path = _db_path(notebook_id)
    with _lock, closing(_connect(path)) as con:
        row = con.execute("SELECT body FROM results WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def drop_result_cache(notebook_id, run_id) -> None:
    path = _db_path(notebook_id)
    with _lock, closing(_connect(path)) as con:
        con.execute("DELETE FROM results WHERE run_id = ?", (run_id,))


def _jsonable(value):
    """numpy scalars and NaN-bearing arrays, as JSON can carry them."""
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")
