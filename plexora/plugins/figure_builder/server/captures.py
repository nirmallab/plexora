"""The captures bin: what a capture is before it belongs to anything.

Capturing used to demand an answer to "which figure?" at the worst possible
moment -- before the user had decided which of the regions in front of them
were worth keeping. The honest answer, early on, is "I do not know yet", and a
tool that refuses to accept it is a tool people stop capturing with. So a
capture now lands HERE, unconditionally and with nothing asked, and the figure
question is asked once, later, on the way to the canvas.

**One store for the whole bin, not one per capture.** That is the opposite of
`repository.py`'s choice, and the reasoning inverts cleanly: a figure is a
document a user opens, names, exports and deletes as a unit, so a file per
figure is the portable thing and a central index would be a single point of
loss. A capture is a row -- a scene, a caption and a small raster -- with no
identity a user would name, and fifty files holding one row each would make
"show me my bin" fifty opens. A damaged bin loses captures nobody has committed
to a figure, which is the cheapest thing in this plugin to lose; a damaged
figure is a day of work.

**The preview is a BLOB and it is the only pixel data here.** A capture in the
bin has no figure directory to sit beside, and a loose file per capture in a
dot-directory is a garbage-collection problem the moment a row and its file
disagree. A WebP crop is tens of kilobytes; SQLite carries it and the row and
the delete in one transaction.

**Rollback journal, not WAL**, for exactly the reason `repository.py` gives:
`data_path` is very often inside Dropbox, OneDrive or a network share, and
WAL's shared-memory file does not survive those.

A capture leaves the bin only by being adopted into a figure -- see
`transfer_previews`, which is the one path that moves the raster across and
deletes the row -- or by being deleted outright. Nothing else ages it out: a
bin that quietly emptied itself would be a bin nobody could leave anything in.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from plexora.plugins.figure_builder.server import repository, schema

DB_FILENAME = "captures.db"

#: Biggest preview accepted, per capture. The same limit a figure's previews
#: get, and for the same reason: a WebP of a canvas crop is tens of kilobytes,
#: and anything past this is a client sending the wrong thing.
MAX_PREVIEW_BYTES = 8 * 1024 * 1024

#: How long to wait for another thread's write. Waitress is multi-threaded and
#: a burst of captures really does land in several threads at once.
BUSY_TIMEOUT = 10.0

#: How many rows one listing returns, newest first. A bin is meant to
#: accumulate, and a grid of two thousand thumbnails is not a browse -- it is a
#: page that never finishes painting. The cap is high enough that nobody
#: reaches it by working and low enough that the page stays usable if they do.
LIST_LIMIT = 500

#: Ids are minted by the client, like panel ids, so a capture appears in the
#: strip the instant the shutter closes rather than after a round trip. Checked
#: on the way in because this value arrives from a URL.
CAPTURE_ID_PATTERN = re.compile(r"^cap_[A-Za-z0-9][A-Za-z0-9_.:-]{0,59}$")

#: Stood in for the source_id a stored source does not have yet.
#:
#: A capture records WHICH IMAGE it came from -- dimensions, channel keys,
#: fingerprint, pixel size -- because the strip has to caption it and the
#: adoption has to register it, and neither can ask a datasource that may not
#: be loaded any more. What it cannot know is the id that image will have
#: INSIDE some figure, because no figure has been chosen. `normalize_source`
#: requires one, so the bin supplies this and the client replaces it with a
#: fresh `src_...` when the capture is adopted.
PLACEHOLDER_SOURCE_ID = "src_unassigned"


class UnknownCapture(Exception):
    """No capture with that id in the bin."""


class CapturesUnavailable(Exception):
    """The bin itself cannot be opened -- damaged, or on a filesystem that has
    stopped answering.

    Separate from the read paths' behaviour, which is to report an empty bin:
    a capture that cannot be READ costs the user a thumbnail, and refusing to
    open the viewer over that would be the worse failure. A capture that cannot
    be WRITTEN is the shutter press not landing, and the user has to be told
    while they are still standing over the region.
    """


#: One lock for the whole bin, held across read-modify-write sequences. There
#: is one database, so there is one lock; contention is a handful of writes a
#: minute at the very most.
_LOCK = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_capture_id() -> str:
    """A fresh id, for the server-side paths (tests, mostly). The client mints
    its own with the same prefix."""
    return "cap_" + uuid.uuid4().hex[:12]


def validate_capture_id(value) -> str:
    if not isinstance(value, str) or not CAPTURE_ID_PATTERN.match(value):
        raise ValueError(f"invalid capture id: {value!r}")
    return value


def captures_root() -> Path:
    """Where the bin lives, resolved on every call.

    Deliberately not captured at import time -- `plexora.paths` resolves the
    root on demand, and a module-level constant here would pin whatever the
    answer happened to be when this module was first imported. The test suite
    repoints the root per test.
    """
    from plexora import paths

    return paths.captures_root()


def db_path() -> Path:
    return captures_root() / DB_FILENAME


_DDL = """
CREATE TABLE IF NOT EXISTS captures (
    capture_id  TEXT PRIMARY KEY,
    datasource  TEXT NOT NULL DEFAULT '',
    source_json TEXT NOT NULL DEFAULT '{}',
    scene_json  TEXT NOT NULL DEFAULT '{}',
    caption     TEXT NOT NULL DEFAULT '',
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    format      TEXT NOT NULL DEFAULT 'webp',
    preview     BLOB,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS captures_by_datasource ON captures (datasource);
"""


def _connect(create=True):
    """A connection to the bin, creating it on first use.

    Unlike a figure, a bin that does not exist yet is not an error -- it is a
    user who has not captured anything. `create=False` is for the read paths,
    which answer "nothing in it" without putting a database on disk just
    because somebody opened the page.

    The schema is applied on every open rather than once at creation, because
    there is no create step to hang it off: the first capture of the user's
    life is an ordinary write. A file that is there but cannot take the schema
    raises CapturesUnavailable rather than an OperationalError from whichever
    statement happened to run next -- the read paths turn that into an empty
    bin and the write paths report it.
    """
    path = db_path()
    if not path.is_file():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT,
                                     isolation_level="DEFERRED")
    except sqlite3.Error as exc:
        raise CapturesUnavailable(f"the captures store cannot be opened: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.executescript(_DDL)
    except sqlite3.Error as exc:
        connection.close()
        raise CapturesUnavailable(f"the captures store cannot be read: {exc}") from exc
    return connection


# -- writing ------------------------------------------------------------


def add(capture_id, datasource, source, scene, caption="", created_at=None) -> dict:
    """Record a capture. Returns its summary row.

    The scene and the source are normalised HERE rather than trusted, so a
    client that sends nonsense cannot poison a bin that every later adoption
    reads from -- and a scene taken by a newer build of Plexora is refused with
    the same UnreadableFigure a figure would refuse it with, rather than stored
    and re-read as a panel that cannot be drawn.

    Re-adding an id that is already there replaces it. That is what makes the
    client's retry-after-a-failed-preview safe, and there is nothing to merge:
    a capture is written once, whole, at the moment the shutter closes.
    """
    validate_capture_id(capture_id)
    scene_clean = schema.normalize_scene(scene if isinstance(scene, dict) else {})
    raw_source = dict(source) if isinstance(source, dict) else {}
    # See PLACEHOLDER_SOURCE_ID: the id this image will carry inside a figure
    # cannot be known until a figure is chosen.
    raw_source.setdefault("source_id", PLACEHOLDER_SOURCE_ID)
    if not schema.clean_id(raw_source.get("source_id")):
        raw_source["source_id"] = PLACEHOLDER_SOURCE_ID
    source_clean = schema.normalize_source(raw_source)

    stamp = schema.clean_text(created_at) or _now()
    row = {
        "capture_id": capture_id,
        "datasource": schema.clean_text(datasource),
        "source_json": json.dumps(source_clean, separators=(",", ":")),
        "scene_json": json.dumps(scene_clean, separators=(",", ":")),
        "caption": schema.clean_text(caption),
        "created_at": stamp,
    }
    with _LOCK:
        connection = _connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO captures (capture_id, datasource, source_json, scene_json, "
                    "caption, created_at) VALUES (:capture_id, :datasource, :source_json, "
                    ":scene_json, :caption, :created_at) "
                    "ON CONFLICT(capture_id) DO UPDATE SET datasource = excluded.datasource, "
                    "source_json = excluded.source_json, scene_json = excluded.scene_json, "
                    "caption = excluded.caption",
                    row)
        finally:
            connection.close()
    return get(capture_id)


def put_preview(capture_id, data, width=0, height=0, fmt="webp") -> bool:
    """Store a capture's raster. False if the capture is not in the bin.

    No render-revision check, unlike a figure's previews: a capture is written
    once and never re-rendered, so there is no race between a slow render and a
    fast one to arbitrate. The raster arrives a moment after the row because
    the blob is produced asynchronously by the browser, which is the only
    reason this is a second call at all.
    """
    validate_capture_id(capture_id)
    if not data:
        raise ValueError("a preview needs image data")
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError(f"preview is larger than {MAX_PREVIEW_BYTES // (1024 * 1024)} MB")

    with _LOCK:
        connection = _connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE captures SET preview = ?, width = ?, height = ?, format = ? "
                    "WHERE capture_id = ?",
                    (sqlite3.Binary(data), int(width or 0), int(height or 0),
                     schema.clean_text(fmt) or "webp", capture_id))
                return cursor.rowcount > 0
        finally:
            connection.close()


def get_preview(capture_id):
    """(bytes, format) or None -- None both for "no such capture" and for one
    whose raster has not arrived yet. The caller renders a placeholder in both
    cases, so telling them apart would buy nothing."""
    validate_capture_id(capture_id)
    try:
        connection = _connect(create=False)
    except CapturesUnavailable:
        return None
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT preview, format FROM captures WHERE capture_id = ?",
            (capture_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None or row["preview"] is None:
        return None
    return bytes(row["preview"]), row["format"] or "webp"


def remove(capture_ids) -> int:
    """Delete captures. Returns how many rows went.

    Bulk by construction: the strip and the bin grid both delete a selection,
    and a loop of single deletes would be a loop of transactions for one thing
    the user did.
    """
    ids = [validate_capture_id(value) for value in (capture_ids or [])]
    if not ids:
        return 0
    with _LOCK:
        connection = _connect(create=False)
        if connection is None:
            return 0
        try:
            with connection:
                cursor = connection.executemany(
                    "DELETE FROM captures WHERE capture_id = ?", [(value,) for value in ids])
                # executemany's rowcount is the total across statements in
                # sqlite3, which is what is wanted here.
                return max(0, cursor.rowcount)
        finally:
            connection.close()


# -- reading ------------------------------------------------------------


def _row(row) -> dict:
    """One capture as the client sees it -- everything but the raster.

    The scene AND the source ride along, because both callers need them and
    neither can get them anywhere else: the dock draws each capture's outline
    from the scene's viewport, and adopting one into a figure registers the
    source it names. Fetching them per capture would be one request per
    thumbnail for data that is a couple of kilobytes.
    """
    try:
        source = json.loads(row["source_json"] or "{}")
    except ValueError:
        source = {}
    try:
        scene = json.loads(row["scene_json"] or "{}")
    except ValueError:
        scene = {}
    return {
        "capture_id": row["capture_id"],
        "datasource": row["datasource"] or "",
        "caption": row["caption"] or "",
        "width": int(row["width"] or 0),
        "height": int(row["height"] or 0),
        "format": row["format"] or "webp",
        "has_preview": bool(row["has_preview"]),
        "created_at": row["created_at"] or "",
        "source": source,
        "scene": scene,
    }


def list_captures(datasource=None) -> list[dict]:
    """The bin, newest first, without the rasters.

    `datasource` filters to one image, which is what the in-viewer strip wants:
    a capture's outline is drawn in the coordinates of the image it came from,
    so captures from another slide have nowhere to be drawn and nothing to say
    on this one. The bin grid passes nothing and gets everything.
    """
    try:
        connection = _connect(create=False)
    except CapturesUnavailable:
        # A bin that cannot be read is a bin with nothing in it as far as every
        # caller here is concerned: the strip renders empty, the next capture
        # still lands, and refusing to open the viewer over it would be the
        # worse failure.
        return []
    if connection is None:
        return []
    query = ("SELECT capture_id, datasource, source_json, scene_json, caption, width, "
             "height, format, created_at, preview IS NOT NULL AS has_preview FROM captures")
    parameters: tuple = ()
    if datasource:
        query += " WHERE datasource = ?"
        parameters = (str(datasource),)
    query += " ORDER BY created_at DESC, capture_id DESC LIMIT ?"
    parameters = parameters + (LIST_LIMIT,)
    try:
        rows = connection.execute(query, parameters).fetchall()
    except sqlite3.Error:
        # A bin that cannot be read is a bin with nothing in it as far as the
        # capture path is concerned: the next capture still lands, and refusing
        # to open the viewer over it would be the worse failure.
        return []
    finally:
        connection.close()
    return [_row(row) for row in rows]


def get(capture_id) -> dict:
    """One capture, or UnknownCapture."""
    validate_capture_id(capture_id)
    connection = _connect(create=False)
    if connection is None:
        raise UnknownCapture(capture_id)
    try:
        row = connection.execute(
            "SELECT capture_id, datasource, source_json, scene_json, caption, width, "
            "height, format, created_at, preview IS NOT NULL AS has_preview "
            "FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise UnknownCapture(capture_id)
    return _row(row)


def count() -> int:
    try:
        connection = _connect(create=False)
    except CapturesUnavailable:
        return 0
    if connection is None:
        return 0
    try:
        return int(connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


# -- leaving the bin ----------------------------------------------------


def transfer_previews(figure_id, pairs) -> dict:
    """Move captures' rasters into a figure's previews, and empty their rows.

    The one path out of the bin. It lives here rather than in `repository`
    because the deciding half is the bin's: the row is deleted, and a delete
    that happened without the raster having landed would lose the only picture
    of a region the user may never be standing over again. So the copy is made
    first, per capture, and only the ones that copied are removed.

    Deliberately NOT the thing that creates the panels. The panel is a document
    edit and belongs in the one batch the client commits -- one undo step for
    "add these captures" -- and duplicating panel construction on the server
    would be a second implementation of `panelFor` to disagree with the first.
    So the client commits the panels and then calls this, and a failure here
    leaves a panel that re-renders from its scene with no cached raster, which
    is a slow panel rather than a lost one.

    `pairs` is `[{capture_id, panel_id, render_revision}]`. Returns
    `{moved: [...], missing: [...]}`.
    """
    schema.validate_figure_id(figure_id)
    if not isinstance(pairs, list):
        raise ValueError("pairs must be a list")
    if not repository.exists(figure_id):
        raise repository.UnknownFigure(figure_id)

    moved, missing = [], []
    for raw in pairs:
        if not isinstance(raw, dict):
            raise ValueError("each pair must be an object")
        capture_id = validate_capture_id(raw.get("capture_id"))
        panel_id = schema.validate_id(raw.get("panel_id"), "panel id")
        revision = raw.get("render_revision", 1)
        if not isinstance(revision, int) or isinstance(revision, bool):
            revision = 1

        found = get_preview(capture_id)
        if found is None:
            # Either the capture is gone or its raster never arrived. Both are
            # "nothing to copy", and the row still goes: the panel exists now,
            # and a bin row for a capture that is already in a figure is the
            # one thing the bin must never show.
            missing.append(capture_id)
            remove([capture_id])
            continue
        data, fmt = found
        try:
            stored = get(capture_id)
        except UnknownCapture:  # pragma: no cover - raced with a delete
            missing.append(capture_id)
            continue
        repository.put_preview(figure_id, panel_id, revision, data,
                               width=stored["width"], height=stored["height"], fmt=fmt)
        remove([capture_id])
        moved.append(capture_id)
    return {"moved": moved, "missing": missing}
