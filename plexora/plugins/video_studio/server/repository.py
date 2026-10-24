"""Where videos live, and who is allowed to overwrite whom.

Figure Builder's pattern, for Figure Builder's reasons: a video is not
datasource state (it may outlive the project it was made on, and a Storyboard
may one day span several), so each video is its own directory and its own
single-file SQLite database:

    data_path/.videos/<video_id>/video.db     the document, journal, posters, exports index
    data_path/.videos/<video_id>/exports/     finished files and their manifests
    data_path/.videos/<video_id>/cache/       scratch an export may leave behind

**The library is a directory scan** -- no central index whose corruption loses
every video at once; a database that cannot be opened is one unreadable card.

**Rollback journal, not WAL**, because `data_path` is so often inside Dropbox or
a network share, and WAL's shared-memory file does not survive those.

**The revision is checked here.** Two tabs on one video both hold a copy;
without the check the stale tab's next save reinstates its whole world.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from plexora.plugins.video_studio.server import operations, schema

DB_FILENAME = "video.db"
EXPORTS_DIRNAME = "exports"
CACHE_DIRNAME = "cache"
BUSY_TIMEOUT = 10.0
JOURNAL_LIMIT = 500
MAX_POSTER_BYTES = 8 * 1024 * 1024


class ConflictError(Exception):
    def __init__(self, current_revision):
        super().__init__("this video changed in another session")
        self.current_revision = current_revision


class UnknownVideo(Exception):
    """No video with that id, or its directory has gone."""


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(video_id):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(video_id)
        if lock is None:
            lock = _LOCKS[video_id] = threading.Lock()
        return lock


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def videos_root() -> Path:
    """Resolved on every call, never at import: the data root can change under
    a running process (tests, a remote switch)."""
    from plexora import paths

    return paths.videos_root()


def video_dir(video_id) -> Path:
    return videos_root() / schema.validate_video_id(video_id)


def exports_dir(video_id) -> Path:
    return video_dir(video_id) / EXPORTS_DIRNAME


_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL,
    json     TEXT    NOT NULL,
    saved_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    base_revision INTEGER,
    ops_json      TEXT,
    origin        TEXT,
    applied_at    TEXT
);
CREATE TABLE IF NOT EXISTS posters (
    kf_id      TEXT PRIMARY KEY,
    format     TEXT NOT NULL DEFAULT 'webp',
    width      INTEGER,
    height     INTEGER,
    data       BLOB NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS exports (
    export_id     TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    output_json   TEXT,
    manifest_json TEXT,
    path          TEXT,
    bytes         INTEGER,
    created_at    TEXT
);
"""


def _connect(path: Path):
    connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    return connection


def _open(video_id):
    """A connection to an existing video, or UnknownVideo -- never a freshly
    conjured empty database for a path that has gone."""
    path = video_dir(video_id) / DB_FILENAME
    if not path.is_file():
        raise UnknownVideo(video_id)
    return _connect(path)


# -- creating and listing ----------------------------------------------------


def create(document=None, *, title=None) -> str:
    """A new video from `document` (a preset's output, say) or empty. Returns
    its id."""
    video_id = schema.new_video_id()
    directory = video_dir(video_id)
    stamp = _now()
    raw = dict(document or {})
    raw.update(video_id=video_id, revision=0, created_at=stamp, updated_at=stamp)
    if title:
        raw["title"] = title
    # Validated before anything is written: a refused document leaves no
    # empty directory behind.
    normalized = schema.normalize_project(raw, video_id=video_id)
    directory.mkdir(parents=True, exist_ok=True)
    connection = _connect(directory / DB_FILENAME)
    try:
        with connection:
            connection.executescript(_DDL)
            _write_document(connection, normalized, stamp)
    finally:
        connection.close()
    return video_id


def list_videos(datasource=None) -> list[dict]:
    """Every video on this machine (or made on `datasource`), newest first."""
    root = videos_root()
    if not root.is_dir():
        return []
    videos = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / DB_FILENAME).is_file():
            continue
        try:
            schema.validate_video_id(directory.name)
        except ValueError:
            continue
        summary = _summary(directory)
        if datasource and summary.get("readable") and summary.get("datasource") != datasource:
            continue
        videos.append(summary)
    videos.sort(key=lambda entry: entry.get("updated_at") or "", reverse=True)
    return videos


def _summary(directory: Path) -> dict:
    video_id = directory.name
    blank = {"video_id": video_id, "title": video_id, "readable": False, "created_at": "",
             "updated_at": "", "revision": 0, "datasource": "", "duration_s": 0.0,
             "track_count": 0, "export_count": 0, "preset": ""}
    try:
        connection = _connect(directory / DB_FILENAME)
        meta = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
        exports = connection.execute(
            "SELECT COUNT(*) FROM exports WHERE status = 'done'").fetchone()[0]
    except sqlite3.Error:
        return blank
    finally:
        try:
            connection.close()
        except UnboundLocalError:  # pragma: no cover - connect itself failed
            pass
    if not meta:
        return blank
    return {
        "video_id": video_id,
        "title": meta.get("title") or video_id,
        "readable": True,
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "revision": int(meta.get("revision") or 0),
        "datasource": meta.get("datasource", ""),
        "duration_s": float(meta.get("duration_s") or 0.0),
        "track_count": int(meta.get("track_count") or 0),
        "export_count": int(exports or 0),
        "preset": meta.get("preset", ""),
    }


def exists(video_id) -> bool:
    try:
        return (video_dir(video_id) / DB_FILENAME).is_file()
    except ValueError:
        return False


def duplicate(video_id, title=None) -> str:
    """A copy under a new id: the document and posters, never the exports --
    a copy is for trying something different, and its files are its own."""
    source_dir = video_dir(video_id)
    if not (source_dir / DB_FILENAME).is_file():
        raise UnknownVideo(video_id)
    document = load(video_id)
    new_id = schema.new_video_id()
    target_dir = videos_root() / new_id
    target_dir.mkdir(parents=True)
    with _lock_for(video_id):
        shutil.copy2(source_dir / DB_FILENAME, target_dir / DB_FILENAME)
    document["video_id"] = new_id
    document["title"] = schema.clean_text(title) or f"{document['title']} copy"
    document["revision"] = 0
    stamp = _now()
    document["created_at"] = stamp
    connection = _connect(target_dir / DB_FILENAME)
    try:
        with connection:
            connection.execute("DELETE FROM journal")
            connection.execute("DELETE FROM exports")
            _write_document(connection, document, stamp)
    finally:
        connection.close()
    return new_id


def delete(video_id) -> None:
    directory = video_dir(video_id)
    if not directory.is_dir():
        raise UnknownVideo(video_id)
    with _lock_for(video_id):
        shutil.rmtree(directory)


# -- the document -------------------------------------------------------------


def load(video_id) -> dict:
    connection = _open(video_id)
    try:
        row = connection.execute("SELECT revision, json FROM document WHERE id = 1").fetchone()
    except sqlite3.Error as exc:
        raise schema.UnreadableVideo(f"this video's database is damaged: {exc}") from exc
    finally:
        connection.close()
    if row is None:
        raise schema.UnreadableVideo("this video holds no document")
    try:
        raw = json.loads(row["json"])
    except ValueError as exc:
        raise schema.UnreadableVideo(f"this video's document could not be read: {exc}") from exc
    try:
        document = schema.normalize_project(raw, video_id=video_id)
    except schema.UnreadableVideo:
        raise
    except ValueError as exc:
        raise schema.UnreadableVideo(f"this video's document is not valid: {exc}") from exc
    document["revision"] = int(row["revision"])
    return document


def apply(video_id, base_revision, ops, *, origin="ui") -> dict:
    """Apply `ops` if the caller was up to date. Returns
    `{revision, inverse, document}`; raises ConflictError, ValueError
    (IntegrityError included) or UnreadableVideo and stores nothing."""
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        raise ValueError("base_revision must be an integer")
    ops = operations.with_ids(ops)
    with _lock_for(video_id):
        document = load(video_id)
        if document["revision"] != base_revision:
            raise ConflictError(document["revision"])
        inverse = operations.invert_operations(document, ops)
        updated = operations.apply_operations(document, ops)
        updated["revision"] = document["revision"] + 1
        stamp = _now()
        updated["updated_at"] = stamp
        connection = _open(video_id)
        try:
            with connection:
                _write_document(connection, updated, stamp)
                _append_journal(connection, base_revision, ops, origin, stamp)
        finally:
            connection.close()
        return {"revision": updated["revision"], "inverse": inverse, "document": updated}


def replace(video_id, base_revision, document) -> int:
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        raise ValueError("base_revision must be an integer")
    with _lock_for(video_id):
        current = load(video_id)
        if current["revision"] != base_revision:
            raise ConflictError(current["revision"])
        updated = schema.normalize_project(document, video_id=video_id)
        updated["revision"] = current["revision"] + 1
        updated["created_at"] = current["created_at"] or updated["created_at"]
        stamp = _now()
        updated["updated_at"] = stamp
        connection = _open(video_id)
        try:
            with connection:
                _write_document(connection, updated, stamp)
        finally:
            connection.close()
        return updated["revision"]


def _write_document(connection, document, stamp):
    connection.execute(
        "INSERT INTO document (id, revision, json, saved_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET revision = excluded.revision, "
        "json = excluded.json, saved_at = excluded.saved_at",
        (document["revision"], json.dumps(document, separators=(",", ":")), stamp))
    preset = (document.get("provenance") or {}).get("preset") or {}
    meta = {
        "schema_version": str(schema.SCHEMA_VERSION),
        "video_id": document["video_id"],
        "title": document["title"],
        "created_at": document.get("created_at") or stamp,
        "updated_at": stamp,
        "revision": str(document["revision"]),
        "datasource": document["source"]["datasource"],
        "duration_s": str(document["duration_s"]),
        "track_count": str(len(document["tracks"])),
        "preset": preset.get("name") or "",
    }
    connection.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", list(meta.items()))


def _append_journal(connection, base_revision, ops, origin, stamp):
    try:
        payload = json.dumps(ops, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    connection.execute(
        "INSERT INTO journal (base_revision, ops_json, origin, applied_at) VALUES (?, ?, ?, ?)",
        (base_revision, payload, str(origin)[:80], stamp))
    connection.execute("DELETE FROM journal WHERE seq <= (SELECT MAX(seq) FROM journal) - ?",
                       (JOURNAL_LIMIT,))


# -- posters ------------------------------------------------------------------


def set_poster(video_id, kf_id, data: bytes, *, fmt="webp", width=None, height=None) -> None:
    kf_id = schema.clean_id(kf_id, "kf")
    if not data or len(data) > MAX_POSTER_BYTES:
        raise ValueError("a poster is an image of at most 8 MB")
    connection = _open(video_id)
    try:
        with connection:
            connection.execute(
                "INSERT INTO posters (kf_id, format, width, height, data, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(kf_id) DO UPDATE SET "
                "format = excluded.format, width = excluded.width, height = excluded.height, "
                "data = excluded.data, updated_at = excluded.updated_at",
                (kf_id, fmt, width, height, sqlite3.Binary(data), _now()))
    finally:
        connection.close()


def get_poster(video_id, kf_id):
    connection = _open(video_id)
    try:
        row = connection.execute("SELECT format, data FROM posters WHERE kf_id = ?",
                                 (kf_id,)).fetchone()
    finally:
        connection.close()
    return None if row is None else (row["format"], bytes(row["data"]))


# -- the exports index ----------------------------------------------------------


def record_export(video_id, export_id, *, status, output=None, manifest=None, path=None,
                  size=None) -> None:
    """Write (or update) one export's row. The files are in `exports/`; this is
    what the library counts and what a reopened video lists."""
    connection = _open(video_id)
    try:
        with connection:
            connection.execute(
                "INSERT INTO exports (export_id, status, output_json, manifest_json, path, bytes, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(export_id) DO UPDATE SET "
                "status = excluded.status, output_json = COALESCE(excluded.output_json, "
                "output_json), manifest_json = COALESCE(excluded.manifest_json, manifest_json), "
                "path = COALESCE(excluded.path, path), bytes = COALESCE(excluded.bytes, bytes)",
                (export_id, status, json.dumps(output) if output is not None else None,
                 json.dumps(manifest) if manifest is not None else None,
                 str(path) if path is not None else None, size, _now()))
    finally:
        connection.close()


def list_exports(video_id) -> list[dict]:
    connection = _open(video_id)
    try:
        rows = connection.execute(
            "SELECT export_id, status, output_json, path, bytes, created_at FROM exports "
            "ORDER BY created_at DESC").fetchall()
    finally:
        connection.close()
    out = []
    for row in rows:
        try:
            output = json.loads(row["output_json"]) if row["output_json"] else None
        except ValueError:
            output = None
        path = Path(row["path"]) if row["path"] else None
        out.append({"export_id": row["export_id"], "status": row["status"], "output": output,
                    "name": path.name if path else None,
                    "path": str(path) if path else None, "bytes": row["bytes"],
                    "available": bool(path and path.is_file()),
                    "created_at": row["created_at"]})
    return out


def delete_export(video_id, export_id) -> bool:
    """Remove one export: its row and its folder. False when there is none.
    A running export is the caller's to refuse (only the server that runs it
    knows it is running)."""
    if not str(export_id).startswith("exp_") or not str(export_id)[4:].isalnum():
        raise ValueError(f"not an export id: {export_id!r}")
    connection = _open(video_id)
    try:
        with connection:
            removed = connection.execute("DELETE FROM exports WHERE export_id = ?",
                                         (export_id,)).rowcount
    finally:
        connection.close()
    directory = exports_dir(video_id) / export_id
    if directory.is_dir():
        shutil.rmtree(directory)
        removed = removed or 1
    return bool(removed)


def export_manifest(video_id, export_id):
    connection = _open(video_id)
    try:
        row = connection.execute("SELECT manifest_json FROM exports WHERE export_id = ?",
                                 (export_id,)).fetchone()
    finally:
        connection.close()
    if row is None or not row["manifest_json"]:
        return None
    try:
        return json.loads(row["manifest_json"])
    except ValueError:
        return None
