import sqlite3

from plexora.server.models import database_model
from plexora.server.modules.gating.database import GatingList


def test_save_and_get_creates_per_datasource_file(tmp_path, monkeypatch):
    monkeypatch.setattr(database_model, "data_path", tmp_path)

    database_model.save_list(GatingList, datasource="orion2", cells=b"payload")

    db_file = tmp_path / "orion2" / "orion2.db"
    assert db_file.exists()

    row = database_model.get(GatingList, datasource="orion2")
    assert row.cells == b"payload"
    assert row.datasource == "orion2"
    assert row.is_deleted is False


def test_datasources_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database_model, "data_path", tmp_path)

    database_model.save_list(GatingList, datasource="a", cells=b"a-payload")
    database_model.save_list(GatingList, datasource="b", cells=b"b-payload")

    assert database_model.get(GatingList, datasource="a").cells == b"a-payload"
    assert database_model.get(GatingList, datasource="b").cells == b"b-payload"
    assert (tmp_path / "a" / "a.db").exists()
    assert (tmp_path / "b" / "b.db").exists()

    # A model saved for one datasource must not leak into the other model's
    # table within the same datasource's file.
    assert database_model.get(database_model.ChannelList, datasource="a") is None


def test_save_list_updates_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(database_model, "data_path", tmp_path)

    database_model.save_list(database_model.ChannelList, datasource="orion2", cells=b"first")
    database_model.save_list(database_model.ChannelList, datasource="orion2", cells=b"second")

    row = database_model.get(database_model.ChannelList, datasource="orion2")
    assert row.cells == b"second"

    db_file = tmp_path / "orion2" / "orion2.db"
    conn = sqlite3.connect(str(db_file))
    try:
        count = conn.execute('SELECT COUNT(*) FROM "channelList"').fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_get_returns_none_when_nothing_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(database_model, "data_path", tmp_path)

    assert database_model.get(GatingList, datasource="never_saved") is None


def test_legacy_shared_db_is_migrated_on_first_access(tmp_path, monkeypatch):
    monkeypatch.setattr(database_model, "data_path", tmp_path)

    legacy_path = tmp_path / "db.sqlite3"
    legacy_conn = sqlite3.connect(str(legacy_path))
    legacy_conn.execute(
        'CREATE TABLE "gatinglist" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'datasource TEXT NOT NULL, cells BLOB NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0)'
    )
    legacy_conn.execute(
        'INSERT INTO "gatinglist" (datasource, cells, is_deleted) VALUES (?, ?, 0)',
        ("orion2", b"legacy-payload"),
    )
    legacy_conn.commit()
    legacy_conn.close()
    legacy_mtime_before = legacy_path.stat().st_mtime

    row = database_model.get(GatingList, datasource="orion2")

    assert row is not None
    assert row.cells == b"legacy-payload"
    assert (tmp_path / "orion2" / "orion2.db").exists()
    # Legacy shared file must be left untouched (read-only migration source).
    assert legacy_path.stat().st_mtime == legacy_mtime_before


def test_corrupted_db_file_is_recovered(tmp_path, monkeypatch):
    monkeypatch.setattr(database_model, "data_path", tmp_path)

    ds_dir = tmp_path / "orion2"
    ds_dir.mkdir(parents=True)
    db_file = ds_dir / "orion2.db"
    db_file.write_bytes(b"not a real sqlite database")

    row = database_model.save_list(GatingList, datasource="orion2", cells=b"fresh")

    assert row.cells == b"fresh"
    backups = list(ds_dir.glob("orion2.db.corrupt-*"))
    assert len(backups) == 1
