"""Where figures live, and who is allowed to overwrite whom.

**Not `plexora.api.store`.** That store is scoped to one datasource by
construction -- `store(datasource, plugin)` writes a blob into that project's
own database -- and a figure legitimately spans several: a composite from one
slide, an H&E from another, a schematic from no project at all. There is no
datasource that owns such a document, and picking one arbitrarily would mean
deleting a project silently deletes figures that merely mentioned it.

So each figure is its own single-file SQLite database:

    data_path/.figures/<figure_id>/figure.db      the document, previews, journal
    data_path/.figures/<figure_id>/assets/        imported PNG/JPEG/TIFF
    data_path/.figures/<figure_id>/exports/       generated PDFs, swept freely

Dot-prefixed so a project literally named "figures" cannot collide with it --
projects are directories under the same `data_path`. The per-figure DIRECTORY,
not just the file, is the unit: it is already the layout a future `.plexfig`
zips up, and deleting a figure is one `rmtree` that cannot take a project with
it.

Three things follow from one-file-per-figure and are worth stating:

**The library is a directory scan.** There is no central index, because a
central index is a single file whose corruption loses every figure at once.
Scanning costs one small read per figure and degrades one figure at a time: a
db that cannot be opened becomes one "unreadable" card, and the other
forty-nine open fine.

**Summary counts are denormalised into `meta`.** Listing fifty figures must not
parse fifty JSON documents. They are written inside the same transaction as the
document they describe, so they cannot drift from it.

**Rollback journal, not WAL.** `data_path` is very often inside Dropbox,
OneDrive or a network share, and WAL's shared-memory file does not survive
those. The write rate here is a handful per minute; the throughput WAL buys is
not worth a mode that fails on the filesystem most of these users have.

The revision lives INSIDE the document and is checked here, exactly as ROI's
is, and for the same reason: two tabs on one figure both hold a full copy and
both autosave, and last-writer-wins means the stale one's next save silently
reinstates its whole world.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from plexora.plugins.figure_builder.server import operations, schema

#: Directory under data_path holding every figure. Dot-prefixed: see module
#: docstring.
FIGURES_DIRNAME = ".figures"

DB_FILENAME = "figure.db"
ASSETS_DIRNAME = "assets"

#: Biggest preview or thumbnail accepted, per panel. A WebP of a canvas crop is
#: tens of kilobytes; anything past this is a client sending the wrong thing.
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024

#: How long to wait for another thread's write before giving up. Waitress is
#: multi-threaded and two tabs really do land in two threads; a saved figure is
#: worth waiting ten seconds for.
BUSY_TIMEOUT = 10.0

#: Extensions an imported asset may have. Checked rather than sniffed because
#: this becomes a filename on disk that is later served back.
ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".ome.tif", ".ome.tiff"}


class ConflictError(Exception):
    """Somebody else wrote since the caller last read.

    Carries the current revision so the client can say what happened rather
    than just failing.
    """

    def __init__(self, current_revision):
        super().__init__("this figure changed in another session")
        self.current_revision = current_revision


class UnknownFigure(Exception):
    """No figure with that id, or its directory has gone."""


#: One lock per figure, held across the read-modify-write in `apply`. SQLite's
#: own busy timeout serialises the WRITES, but not the read-check-write
#: sequence: without this, two threads can both read revision 41, both pass the
#: check, and both write revision 42 -- one of them vanishing with nothing said.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(figure_id):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(figure_id)
        if lock is None:
            lock = _LOCKS[figure_id] = threading.Lock()
        return lock


def _now():
    """A timestamp, microseconds included.

    Kept rather than rounded to the second, unlike the stamps inside the
    document: this one is the library's sort key, and two figures created in
    the same second -- which is what "duplicate, then open the copy" is -- would
    otherwise come back in an order that changes between listings.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def figures_root() -> Path:
    """Where figures live, resolved on every call.

    Always this user's own root, never a shared one: a figure can draw on
    several datasources or none, so no project owns it and there is nothing for
    a site-managed root to hold.

    Deliberately not captured at import time -- `plexora.paths` resolves the
    root on demand, and a module-level constant here would pin whatever the
    answer happened to be when this module was first imported.
    """
    from plexora import paths

    return paths.figures_root()


def figure_dir(figure_id) -> Path:
    return figures_root() / schema.validate_figure_id(figure_id)


def new_figure_id() -> str:
    """A fresh id. Random rather than derived from the title, because a figure
    is renamed freely and a directory that has to be renamed with it is a
    rename that can half-fail."""
    return "fig_" + uuid.uuid4().hex[:12]


# -- the database --------------------------------------------------------

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
CREATE TABLE IF NOT EXISTS previews (
    panel_id        TEXT PRIMARY KEY,
    render_revision INTEGER NOT NULL,
    width           INTEGER,
    height          INTEGER,
    format          TEXT NOT NULL DEFAULT 'webp',
    data            BLOB NOT NULL,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS thumbnail (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    format     TEXT NOT NULL DEFAULT 'webp',
    data       BLOB NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    asset_id   TEXT PRIMARY KEY,
    filename   TEXT NOT NULL,
    media_type TEXT,
    bytes      INTEGER,
    added_at   TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    base_revision INTEGER,
    ops_json      TEXT,
    applied_at    TEXT
);
"""

#: How many journal rows one figure keeps. The journal exists so an abnormal
#: exit leaves something to recover from; it is not a permanent history, and an
#: unbounded one turns a 300 KB figure into a 40 MB one over a long session.
JOURNAL_LIMIT = 500


def _connect(path: Path):
    connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _open(figure_id):
    """A connection to an existing figure, or UnknownFigure.

    Checked rather than allowed to create: `sqlite3.connect` happily conjures an
    empty database for a path that does not exist, which would turn "this figure
    was deleted" into "this figure is empty" -- and the next autosave would make
    that true.
    """
    path = figure_dir(figure_id) / DB_FILENAME
    if not path.is_file():
        raise UnknownFigure(figure_id)
    return _connect(path)


# -- creating and listing ------------------------------------------------


def create(title=None) -> str:
    """A new, empty figure. Returns its id."""
    figure_id = new_figure_id()
    directory = figure_dir(figure_id)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = _now()
    document = schema.new_document(figure_id, title=title, created_at=stamp)

    connection = _connect(directory / DB_FILENAME)
    try:
        with connection:
            connection.executescript(_DDL)
            _write_document(connection, document, stamp)
    finally:
        connection.close()
    return figure_id


def list_figures() -> list[dict]:
    """Every figure on this machine, newest first.

    One small read per figure and no shared index -- see the module docstring.
    A figure whose database cannot be opened comes back with
    `readable: False` rather than being omitted: a figure that has gone wrong is
    exactly the one the user needs to see, and a listing that silently skips it
    presents "damaged" as "deleted".
    """
    root = figures_root()
    if not root.is_dir():
        return []

    figures = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / DB_FILENAME).is_file():
            continue
        try:
            schema.validate_figure_id(directory.name)
        except ValueError:
            continue
        figures.append(_summary(directory))

    figures.sort(key=lambda entry: entry.get("updated_at") or "", reverse=True)
    return figures


def _summary(directory: Path) -> dict:
    figure_id = directory.name
    blank = {"figure_id": figure_id, "title": figure_id, "readable": False,
             "created_at": "", "updated_at": "", "revision": 0,
             "page_count": 0, "panel_count": 0, "sources": [],
             "has_thumbnail": False}
    try:
        connection = _connect(directory / DB_FILENAME)
    except sqlite3.Error:
        return blank
    try:
        meta = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
        has_thumbnail = bool(connection.execute(
            "SELECT 1 FROM thumbnail WHERE id = 1").fetchone())
    except sqlite3.Error:
        return blank
    finally:
        connection.close()

    if not meta:
        return blank
    try:
        sources = json.loads(meta.get("sources") or "[]")
    except ValueError:
        sources = []
    return {
        "figure_id": figure_id,
        "title": meta.get("title") or figure_id,
        "readable": True,
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "revision": int(meta.get("revision") or 0),
        "page_count": int(meta.get("page_count") or 0),
        "panel_count": int(meta.get("panel_count") or 0),
        "sources": sources if isinstance(sources, list) else [],
        "has_thumbnail": has_thumbnail,
    }


def duplicate(figure_id, title=None) -> str:
    """A copy of a figure under a new id, previews and assets included.

    The directory is copied and then the identity inside it is rewritten,
    rather than the document being replayed into a fresh figure. Replaying
    would drop every cached preview, and re-rendering them is minutes of work
    to reproduce pixels that are already on disk -- for an action whose whole
    purpose is usually "let me try a different layout without losing this one".

    The copy's title gets " copy" appended unless one is given, because two rows
    in the library both reading "Figure 1" are two rows the user cannot tell
    apart.
    """
    source_dir = figure_dir(figure_id)
    if not (source_dir / DB_FILENAME).is_file():
        raise UnknownFigure(figure_id)

    document = load(figure_id)
    new_id = new_figure_id()
    target_dir = figures_root() / new_id
    with _lock_for(figure_id):
        shutil.copytree(source_dir, target_dir)

    document["figure_id"] = new_id
    document["title"] = schema.clean_text(title) or f"{document['title']} copy"
    stamp = _now()
    document["created_at"] = stamp
    # Revisions restart: the copy is a new document, and inheriting the
    # original's number would let a tab still holding the original's revision
    # write into the copy without ever looking stale.
    document["revision"] = 0

    connection = _connect(target_dir / DB_FILENAME)
    try:
        with connection:
            # The journal describes edits to the ORIGINAL. Carrying it over
            # would offer a recovery that reinstates another figure's history
            # into this one.
            connection.execute("DELETE FROM journal")
            _write_document(connection, document, stamp)
    finally:
        connection.close()
    return new_id


def delete(figure_id) -> None:
    """Remove a figure and everything it owns.

    An rmtree of a directory whose name has already been through
    `validate_figure_id`, which is the only reason that pattern is as narrow as
    it is.
    """
    directory = figure_dir(figure_id)
    if not directory.is_dir():
        raise UnknownFigure(figure_id)
    with _lock_for(figure_id):
        shutil.rmtree(directory)


def exists(figure_id) -> bool:
    try:
        return (figure_dir(figure_id) / DB_FILENAME).is_file()
    except ValueError:
        return False


# -- reading and writing the document ------------------------------------


def load(figure_id) -> dict:
    """The stored document, normalized.

    Raises UnknownFigure if it is not there and UnreadableFigure if it is there
    and cannot be understood. The second case is deliberately loud: handing back
    an empty document presents "your figure cannot be read" as "your figure is
    empty", and the next autosave makes it true.
    """
    connection = _open(figure_id)
    try:
        row = connection.execute(
            "SELECT revision, json FROM document WHERE id = 1").fetchone()
    except sqlite3.Error as exc:
        raise schema.UnreadableFigure(f"this figure's database is damaged: {exc}") from exc
    finally:
        connection.close()

    if row is None:
        raise schema.UnreadableFigure("this figure holds no document")
    try:
        raw = json.loads(row["json"])
    except ValueError as exc:
        raise schema.UnreadableFigure(f"this figure's document could not be read: {exc}") from exc

    document = schema.normalize_document(raw, figure_id=figure_id)
    # The column is the authority on the revision, not the copy inside the
    # JSON: the column is what the conflict check compares and what an index
    # could be built on later.
    document["revision"] = int(row["revision"])
    return document


def apply(figure_id, base_revision, ops) -> int:
    """Apply operations if the caller was up to date, and bump the revision.

    Returns the new revision. Raises ConflictError when somebody else wrote
    first, ValueError when an operation is invalid, UnreadableFigure when the
    stored document cannot be read -- and stores nothing in any of those cases.
    """
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        raise ValueError("base_revision must be an integer")

    with _lock_for(figure_id):
        document = load(figure_id)
        if document["revision"] != base_revision:
            raise ConflictError(document["revision"])

        updated = operations.apply_operations(document, ops)
        updated["revision"] = document["revision"] + 1
        stamp = _now()
        updated["updated_at"] = stamp

        connection = _open(figure_id)
        try:
            with connection:
                _write_document(connection, updated, stamp)
                _append_journal(connection, base_revision, ops, stamp)
        finally:
            connection.close()
        return updated["revision"]


def replace(figure_id, base_revision, document) -> int:
    """Store a whole document, for the paths that build one rather than edit one.

    Same revision check, same lock. Used by import and by the recovery path;
    ordinary editing goes through `apply`, which can be undone and journalled.
    """
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        raise ValueError("base_revision must be an integer")

    with _lock_for(figure_id):
        current = load(figure_id)
        if current["revision"] != base_revision:
            raise ConflictError(current["revision"])

        updated = schema.normalize_document(document, figure_id=figure_id)
        updated["revision"] = current["revision"] + 1
        updated["created_at"] = current["created_at"] or updated["created_at"]
        stamp = _now()
        updated["updated_at"] = stamp

        connection = _open(figure_id)
        try:
            with connection:
                _write_document(connection, updated, stamp)
        finally:
            connection.close()
        return updated["revision"]


def _write_document(connection, document, stamp):
    """The document plus the denormalised summary, in one transaction.

    Both together, always: a `meta` row that disagrees with the document it
    describes is a library card reporting a figure that does not exist.
    """
    connection.execute(
        "INSERT INTO document (id, revision, json, saved_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET revision = excluded.revision, "
        "json = excluded.json, saved_at = excluded.saved_at",
        (document["revision"], json.dumps(document, separators=(",", ":")), stamp),
    )
    sources = sorted({source["display_name"] or source["datasource"]
                      for source in document["sources"].values()
                      if source["display_name"] or source["datasource"]})
    meta = {
        "schema_version": str(schema.SCHEMA_VERSION),
        "figure_id": document["figure_id"],
        "title": document["title"],
        "created_at": document["created_at"],
        "updated_at": stamp,
        "revision": str(document["revision"]),
        "page_count": str(len(document["pages"])),
        "panel_count": str(len(document["panels"])),
        "sources": json.dumps(sources),
    }
    connection.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(meta.items()),
    )


def _append_journal(connection, base_revision, ops, stamp):
    """Record the operations that were applied.

    Written but not yet read back by anything: the recovery UI is a later
    milestone, and a journal that only starts being kept when the UI ships can
    recover nothing from the sessions before it. Rows are cheap; the sessions
    are not repeatable.
    """
    try:
        payload = json.dumps(ops, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    connection.execute(
        "INSERT INTO journal (base_revision, ops_json, applied_at) VALUES (?, ?, ?)",
        (base_revision, payload, stamp),
    )
    connection.execute(
        "DELETE FROM journal WHERE seq <= "
        "(SELECT MAX(seq) FROM journal) - ?", (JOURNAL_LIMIT,),
    )


# -- previews and thumbnails ---------------------------------------------


def put_preview(figure_id, panel_id, render_revision, data, width=0, height=0, fmt="webp"):
    """Store a panel's preview raster, unless a newer one is already there.

    The refusal is the point. Previews are rendered asynchronously and a slow
    one can land after a fast one that was queued later -- so without this, a
    user who changes a channel and then changes it back sees the FIRST render
    overwrite the second and the panel shows a state they have left. Comparing
    render revisions makes a late arrival a no-op instead.

    Returns True if it was stored.
    """
    schema.validate_id(panel_id, "panel id")
    if not isinstance(render_revision, int) or isinstance(render_revision, bool):
        raise ValueError("render_revision must be an integer")
    if not data:
        raise ValueError("a preview needs image data")
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError(f"preview is larger than {MAX_PREVIEW_BYTES // (1024 * 1024)} MB")

    connection = _open(figure_id)
    try:
        with connection:
            row = connection.execute(
                "SELECT render_revision FROM previews WHERE panel_id = ?", (panel_id,)).fetchone()
            if row is not None and int(row["render_revision"]) > render_revision:
                return False
            connection.execute(
                "INSERT INTO previews (panel_id, render_revision, width, height, format, data, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(panel_id) DO UPDATE SET render_revision = excluded.render_revision, "
                "width = excluded.width, height = excluded.height, format = excluded.format, "
                "data = excluded.data, updated_at = excluded.updated_at",
                (panel_id, render_revision, int(width or 0), int(height or 0),
                 fmt, sqlite3.Binary(data), _now()),
            )
        return True
    finally:
        connection.close()


def get_preview(figure_id, panel_id):
    """(bytes, format, render_revision) or None."""
    schema.validate_id(panel_id, "panel id")
    connection = _open(figure_id)
    try:
        row = connection.execute(
            "SELECT data, format, render_revision FROM previews WHERE panel_id = ?",
            (panel_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return bytes(row["data"]), row["format"], int(row["render_revision"])


def drop_previews(figure_id, panel_ids):
    """Forget previews for panels that no longer exist.

    Best-effort housekeeping called after a delete: a stale preview costs disk
    and nothing else, so a failure here must never fail the delete that
    triggered it.
    """
    if not panel_ids:
        return
    try:
        connection = _open(figure_id)
    except UnknownFigure:
        return
    try:
        with connection:
            connection.executemany("DELETE FROM previews WHERE panel_id = ?",
                                   [(panel_id,) for panel_id in panel_ids])
    except sqlite3.Error:
        pass
    finally:
        connection.close()


def put_thumbnail(figure_id, data, fmt="webp"):
    if not data:
        raise ValueError("a thumbnail needs image data")
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError(f"thumbnail is larger than {MAX_PREVIEW_BYTES // (1024 * 1024)} MB")
    connection = _open(figure_id)
    try:
        with connection:
            connection.execute(
                "INSERT INTO thumbnail (id, format, data, updated_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET format = excluded.format, "
                "data = excluded.data, updated_at = excluded.updated_at",
                (fmt, sqlite3.Binary(data), _now()),
            )
    finally:
        connection.close()


def get_thumbnail(figure_id):
    """(bytes, format) or None."""
    connection = _open(figure_id)
    try:
        row = connection.execute("SELECT data, format FROM thumbnail WHERE id = 1").fetchone()
    finally:
        connection.close()
    return (bytes(row["data"]), row["format"]) if row is not None else None


# -- figure-only assets --------------------------------------------------


def import_asset(figure_id, filename, data):
    """Copy an imported image into this figure's own directory.

    Figure-only by design: a schematic or a supporting RGB panel is not a
    project, and making the user create one to drop a PNG into a figure is the
    setup step this whole plugin exists to remove. The bytes live beside the
    database rather than inside it -- a BLOB column would be read whole on
    every open, and the directory is already the portable unit.
    """
    if not data:
        raise ValueError("an imported file needs contents")
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError(f"file is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB")

    name = schema.clean_text(filename, 120)
    suffix = _asset_suffix(name)
    if suffix is None:
        raise ValueError(f"{name or 'this file'} is not an image Plexora can import")

    asset_id = "ast_" + uuid.uuid4().hex[:12]
    directory = figure_dir(figure_id) / ASSETS_DIRNAME
    if not (figure_dir(figure_id) / DB_FILENAME).is_file():
        raise UnknownFigure(figure_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Named by id, never by the user's filename: the original name is data, and
    # data does not belong in a path. The name is kept in the table for display.
    (directory / f"{asset_id}{suffix}").write_bytes(data)

    connection = _open(figure_id)
    try:
        with connection:
            connection.execute(
                "INSERT INTO assets (asset_id, filename, media_type, bytes, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (asset_id, name, _media_type(suffix), len(data), _now()),
            )
    finally:
        connection.close()
    return {"asset_id": asset_id, "filename": name,
            "media_type": _media_type(suffix), "bytes": len(data)}


def asset_path(figure_id, asset_id):
    """Where an imported file physically is, or None."""
    if not schema.clean_id(asset_id) or not asset_id.startswith("ast_"):
        return None
    directory = figure_dir(figure_id) / ASSETS_DIRNAME
    if not directory.is_dir():
        return None
    for path in directory.iterdir():
        if path.is_file() and path.name.startswith(asset_id):
            return path
    return None


def _asset_suffix(filename):
    lowered = (filename or "").lower()
    for extension in sorted(ASSET_EXTENSIONS, key=len, reverse=True):
        if lowered.endswith(extension):
            return extension
    return None


def _media_type(suffix):
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "image/tiff"
