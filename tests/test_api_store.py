"""Per-plugin storage: namespacing, write-back, and the legacy-read path that
existing users' saved gates depend on.
"""

import sqlite3

import polars as pl
import pytest

from plexora import api
from plexora.server.models import database_model


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(database_model, "data_path", tmp_path)
    return tmp_path


def _tables(data_dir, datasource):
    db_file = data_dir / datasource / f"{datasource}.db"
    if not db_file.exists():
        return []
    conn = sqlite3.connect(str(db_file))
    try:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Namespacing
# --------------------------------------------------------------------------

def test_state_round_trips(isolated_data_dir):
    store = api.store("proj", "gating")
    assert store.get_state() is None
    store.put_state(b"payload")
    assert store.get_state() == b"payload"


def test_tables_are_namespaced_per_plugin(isolated_data_dir):
    api.store("proj", "gating").put_state(b"g")
    api.store("proj", "roi").put_state(b"r")

    tables = _tables(isolated_data_dir, "proj")
    assert "plugin_gating_state" in tables
    assert "plugin_roi_state" in tables


def test_two_plugins_can_use_the_same_table_name(isolated_data_dir):
    """The collision the namespace exists to prevent: both plugins store
    'results' for the same project without overwriting each other."""
    frame_a = pl.DataFrame({"cell": [1, 2], "score": [0.5, 0.25]})
    frame_b = pl.DataFrame({"cell": [9], "score": [1.0]})

    api.store("proj", "gating").put_table("results", frame_a)
    api.store("proj", "roi").put_table("results", frame_b)

    assert api.store("proj", "gating").get_table("results").equals(frame_a)
    assert api.store("proj", "roi").get_table("results").equals(frame_b)


def test_datasources_stay_isolated(isolated_data_dir):
    api.store("a", "gating").put_state(b"a-state")
    api.store("b", "gating").put_state(b"b-state")
    assert api.store("a", "gating").get_state() == b"a-state"
    assert api.store("b", "gating").get_state() == b"b-state"


# --------------------------------------------------------------------------
# Write-back of derived results
# --------------------------------------------------------------------------

def test_result_tables_preserve_dtypes(isolated_data_dir):
    """Parquet rather than CSV so a classification column comes back as the
    type it went in as, not as text."""
    frame = pl.DataFrame(
        {
            "cell_id": pl.Series([1, 2, 3], dtype=pl.Int64),
            "score": pl.Series([0.1, 0.2, 0.3], dtype=pl.Float32),
            "label": pl.Series(["a", "b", "a"], dtype=pl.Utf8),
            "flagged": pl.Series([True, False, True], dtype=pl.Boolean),
        }
    )
    store = api.store("proj", "gating")
    store.put_table("classifications", frame)
    restored = store.get_table("classifications")
    assert restored.equals(frame)
    assert restored.schema == frame.schema


def test_missing_table_is_none_not_an_error(isolated_data_dir):
    assert api.store("proj", "gating").get_table("never_written") is None


def test_put_table_rejects_non_frames(isolated_data_dir):
    with pytest.raises(TypeError):
        api.store("proj", "gating").put_table("results", {"not": "a frame"})


def test_put_state_rejects_non_bytes(isolated_data_dir):
    with pytest.raises(TypeError):
        api.store("proj", "gating").put_state("a string")


# --------------------------------------------------------------------------
# Introspection and uninstall
# --------------------------------------------------------------------------

def test_list_tables_reports_only_this_plugins_tables(isolated_data_dir):
    gating = api.store("proj", "gating")
    gating.put_state(b"s")
    gating.put_table("results", pl.DataFrame({"a": [1]}))
    api.store("proj", "roi").put_table("regions", pl.DataFrame({"b": [2]}))

    assert gating.list_tables() == ["results", "state"]


def test_drop_all_removes_only_this_plugins_data(isolated_data_dir):
    gating = api.store("proj", "gating")
    roi = api.store("proj", "roi")
    gating.put_state(b"s")
    gating.put_table("results", pl.DataFrame({"a": [1]}))
    roi.put_table("regions", pl.DataFrame({"b": [2]}))

    gating.drop_all()

    assert gating.list_tables() == []
    assert gating.get_state() is None
    assert roi.get_table("regions") is not None
    # Core's own table must be untouched by a plugin uninstall.
    database_model.save_list(database_model.ChannelList, datasource="proj", cells=b"channels")
    gating.drop_all()
    assert database_model.get(database_model.ChannelList, datasource="proj").cells == b"channels"


def test_list_tables_on_untouched_datasource_is_empty(isolated_data_dir):
    assert api.store("never_used", "gating").list_tables() == []


# --------------------------------------------------------------------------
# Identifier validation -- table names reach SQL as identifiers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    ['results"; DROP TABLE "channelList', "Results", "1results", "res-ults", "", "res ults"],
)
def test_hostile_or_malformed_table_names_are_refused(isolated_data_dir, bad):
    with pytest.raises(ValueError):
        api.store("proj", "gating").put_table(bad, pl.DataFrame({"a": [1]}))


@pytest.mark.parametrize("bad", ['gating"; DROP TABLE x --', "Gating", "9gating", "gat-ing"])
def test_hostile_or_malformed_plugin_names_are_refused(isolated_data_dir, bad):
    with pytest.raises(ValueError):
        api.store("proj", bad)


# --------------------------------------------------------------------------
# Legacy read path -- existing users' saved gates
# --------------------------------------------------------------------------

def test_state_falls_back_to_the_pre_namespacing_table(isolated_data_dir):
    """Gates saved by a build that predates the plugin API must still load."""
    from plexora.server.modules.gating.database import LEGACY_STATE_TABLE, GatingList

    database_model.save_list(GatingList, datasource="proj", cells=b"old-gates")

    store = api.store("proj", "gating", legacy_state_table=LEGACY_STATE_TABLE)
    assert store.get_state() == b"old-gates"


def test_namespaced_state_wins_over_legacy(isolated_data_dir):
    from plexora.server.modules.gating.database import LEGACY_STATE_TABLE, GatingList

    database_model.save_list(GatingList, datasource="proj", cells=b"old-gates")
    store = api.store("proj", "gating", legacy_state_table=LEGACY_STATE_TABLE)
    store.put_state(b"new-gates")

    assert store.get_state() == b"new-gates"


def test_writes_never_touch_the_legacy_table(isolated_data_dir):
    """The old table is frozen, not kept in sync, so rolling the host back
    still finds the gates it wrote."""
    from plexora.server.modules.gating.database import LEGACY_STATE_TABLE, GatingList

    database_model.save_list(GatingList, datasource="proj", cells=b"old-gates")
    api.store("proj", "gating", legacy_state_table=LEGACY_STATE_TABLE).put_state(b"new-gates")

    assert database_model.get(GatingList, datasource="proj").cells == b"old-gates"
    assert LEGACY_STATE_TABLE in _tables(isolated_data_dir, "proj")


def test_drop_all_leaves_the_legacy_table_alone(isolated_data_dir):
    from plexora.server.modules.gating.database import LEGACY_STATE_TABLE, GatingList

    database_model.save_list(GatingList, datasource="proj", cells=b"old-gates")
    store = api.store("proj", "gating", legacy_state_table=LEGACY_STATE_TABLE)
    store.put_state(b"new-gates")
    store.drop_all()

    # The namespaced table is gone; the pre-namespacing one is not this
    # store's to delete, so state resolves back to the legacy value.
    assert store.get_state() == b"old-gates"
