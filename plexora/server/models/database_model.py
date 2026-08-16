from plexora import data_path

import sqlite3
import threading
import time


class ChannelList:
    __tablename__ = 'channelList'


class _Row:
    __slots__ = ("id", "datasource", "cells", "is_deleted")

    def __init__(self, id, datasource, cells, is_deleted):
        self.id = id
        self.datasource = datasource
        self.cells = cells
        self.is_deleted = bool(is_deleted)


# Table name always comes from a model class's __tablename__ (ChannelList
# here; plugin tables go through plexora.api.store, which namespaces and
# validates them) -- trusted code, never user input, so safe to
# interpolate.
_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS "{table}" ('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, '
    'datasource TEXT NOT NULL UNIQUE, '
    'cells BLOB NOT NULL, '
    'is_deleted INTEGER NOT NULL DEFAULT 0)'
)

_migration_locks = {}
_migration_locks_guard = threading.Lock()


def _migration_lock_for(datasource_name):
    with _migration_locks_guard:
        if datasource_name not in _migration_locks:
            _migration_locks[datasource_name] = threading.Lock()
        return _migration_locks[datasource_name]


def _db_path_for_datasource(datasource_name):
    db_dir = data_path / datasource_name
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / f"{datasource_name}.db"


def _create_table(conn, model):
    # Only the table for the model actually being get/save_list'd is
    # created -- not every known model's table eagerly -- so a datasource's
    # .db file never grows tables for feature modules that aren't installed
    # in this build. get(SomeOtherModel, ...) on a datasource that's never
    # used that model still works fine (table gets created empty by that
    # call's own _connect, then queried, per the isolation test in
    # tests/test_database_model.py) -- no pre-creation is required.
    conn.execute(_SCHEMA.format(table=model.__tablename__))


def _ensure_healthy(db_file):
    """Reactive recovery only, invoked after an actual sqlite3.DatabaseError --
    not a proactive integrity_check on every call, since that would add cost
    to every get/save_list without adding safety (a check that passes right
    before a query says nothing about corruption from that query's own write)."""
    if not db_file.exists():
        return
    backup_path = db_file.with_name(f"{db_file.name}.corrupt-{int(time.time())}")
    print(f"WARNING: {db_file} appears corrupted; moving it to {backup_path} "
          f"and recreating a fresh database.")
    db_file.rename(backup_path)


def _connect(db_file, model):
    conn = sqlite3.connect(str(db_file), timeout=10)
    try:
        _create_table(conn, model)
    except sqlite3.DatabaseError:
        # Close the failed connection first -- on Windows, _ensure_healthy's
        # rename-aside fails with PermissionError while a handle is still open.
        conn.close()
        _ensure_healthy(db_file)
        conn = sqlite3.connect(str(db_file), timeout=10)
        _create_table(conn, model)
    return conn


def _migrate_legacy_row(db_file, datasource_name, table):
    """Best-effort, one-time: copy this datasource's row out of the old
    shared data_path/db.sqlite3 into its new per-datasource file. Read-only
    against the legacy file -- it is never deleted or written to."""
    legacy_path = data_path / "db.sqlite3"
    if not legacy_path.exists():
        return
    try:
        legacy_conn = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True, timeout=10)
        try:
            row = legacy_conn.execute(
                f'SELECT cells, is_deleted FROM "{table}" WHERE datasource = ? '
                f'ORDER BY id DESC LIMIT 1', (datasource_name,),
            ).fetchone()
        finally:
            legacy_conn.close()
    except sqlite3.DatabaseError:
        return
    if row is None:
        return
    cells, is_deleted = row
    conn = sqlite3.connect(str(db_file), timeout=10)
    try:
        conn.execute(_SCHEMA.format(table=table))
        conn.execute(
            f'INSERT OR IGNORE INTO "{table}" (datasource, cells, is_deleted) '
            f'VALUES (?, ?, ?)', (datasource_name, cells, is_deleted),
        )
        conn.commit()
    finally:
        conn.close()


def _prepare(model, datasource_name):
    db_file = _db_path_for_datasource(datasource_name)
    if not db_file.exists():
        with _migration_lock_for(datasource_name):
            if not db_file.exists():
                _migrate_legacy_row(db_file, datasource_name, model.__tablename__)
    return db_file


def get(model, datasource):
    db_file = _prepare(model, datasource)
    conn = _connect(db_file, model)
    try:
        row = conn.execute(
            f'SELECT id, datasource, cells, is_deleted FROM "{model.__tablename__}" '
            f'WHERE datasource = ?', (datasource,),
        ).fetchone()
    finally:
        conn.close()
    return _Row(*row) if row else None


def save_list(model, datasource, cells):
    db_file = _prepare(model, datasource)
    conn = _connect(db_file, model)
    try:
        conn.execute(
            f'INSERT INTO "{model.__tablename__}" (datasource, cells, is_deleted) '
            f'VALUES (?, ?, 0) ON CONFLICT(datasource) DO UPDATE SET cells = excluded.cells',
            (datasource, cells),
        )
        conn.commit()
        row = conn.execute(
            f'SELECT id, datasource, cells, is_deleted FROM "{model.__tablename__}" '
            f'WHERE datasource = ?', (datasource,),
        ).fetchone()
    finally:
        conn.close()
    return _Row(*row)
