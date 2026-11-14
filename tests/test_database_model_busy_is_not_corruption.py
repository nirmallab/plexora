"""A busy per-datasource database is busy, not corrupt.

`database_model._connect` recovers from a corrupt file by moving it aside to
`<name>.db.corrupt-<ts>` and starting an empty one. "database is locked"
(another connection holding the write lock past the busy timeout) is a
`sqlite3.OperationalError`, which is a `sqlite3.DatabaseError` too -- and it is
not corruption. The file holds every gate, ROI, channel list and plugin table of
the project, so mistaking a busy file for a corrupt one empties the project.

The busy timeout is shortened through `sqlite3.connect` so the tests do not
wait out the real 10 s; a second connection holds the write lock.
"""

import logging
import sqlite3

from plexora.server.models import database_model


class SavedState:
    """Stand-in for state the user already saved (gates, ROIs, ...)."""

    __tablename__ = "savedState"


class FirstTouchedTable:
    """Stand-in for a table this datasource has never used yet."""

    __tablename__ = "firstTouchedTable"


_REAL_CONNECT = sqlite3.connect


def _short_busy_timeout(monkeypatch):
    def connect(*args, **kwargs):
        kwargs["timeout"] = 0.1
        return _REAL_CONNECT(*args, **kwargs)

    monkeypatch.setattr(database_model.sqlite3, "connect", connect)


def _hold_write_lock(db_file):
    blocker = _REAL_CONNECT(str(db_file), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001 -- the test asserts on its type
        return None, exc


def test_a_locked_database_raises_and_is_not_moved_aside(tmp_path, monkeypatch, caplog):
    """Another connection holding the write lock makes the call fail with the
    lock error; the project's file stays where it is and is not called corrupt."""
    database_model.save_list(SavedState, datasource="busyproj", cells=b"gates")
    db_file = tmp_path / "busyproj" / "busyproj.db"
    _short_busy_timeout(monkeypatch)
    caplog.set_level(logging.WARNING, logger=database_model.__name__)

    blocker = _hold_write_lock(db_file)
    try:
        _, raised = _call(database_model.get, FirstTouchedTable, datasource="busyproj")
    finally:
        blocker.rollback()
        blocker.close()

    corrupt_warnings = [r.getMessage() for r in caplog.records if "corrupt" in r.getMessage()]
    assert corrupt_warnings == [], f"a locked database was treated as corrupt: {corrupt_warnings}"
    assert list(db_file.parent.glob("busyproj.db.corrupt-*")) == []
    assert isinstance(raised, sqlite3.OperationalError), (
        f"expected the lock error to reach the caller, got {raised!r}")
    assert db_file.exists()
    assert database_model.get(SavedState, datasource="busyproj").cells == b"gates"


def test_a_lock_that_clears_does_not_empty_the_project(tmp_path, monkeypatch):
    """The writer that held the lock finishes just as the busy timeout fires
    (here: as soon as the engine logs anything). Whatever the call returns, the
    state saved before it must still be in the project's database afterwards."""
    database_model.save_list(SavedState, datasource="clearproj", cells=b"gates")
    db_file = tmp_path / "clearproj" / "clearproj.db"
    _short_busy_timeout(monkeypatch)

    blocker = _hold_write_lock(db_file)

    class _ReleaseOnLog(logging.Handler):
        def emit(self, record):
            if blocker is not None:
                _release()

    def _release():
        nonlocal blocker
        if blocker is not None:
            blocker.rollback()
            blocker.close()
            blocker = None

    handler = _ReleaseOnLog(level=logging.DEBUG)
    logger = logging.getLogger(database_model.__name__)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        _call(database_model.get, FirstTouchedTable, datasource="clearproj")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        _release()

    row = database_model.get(SavedState, datasource="clearproj")
    assert row is not None and row.cells == b"gates", (
        "state saved before a transient lock is gone from the project's database; "
        f"backups now: {sorted(p.name for p in db_file.parent.glob('clearproj.db.corrupt-*'))}")
    assert list(db_file.parent.glob("clearproj.db.corrupt-*")) == []
