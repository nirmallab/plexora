"""Where videos live, and who may overwrite whom.

One SQLite file per video under `paths.videos_root()`, Figure Builder's
pattern: the library is a directory scan, so one damaged database is one
unreadable card rather than a lost library, and the revision check means a
stale tab's save is refused instead of reinstating its whole world. These
tests pin both promises, plus the all-or-nothing edit (a refused batch stores
nothing, not even a journal line), duplicate's "copy the document, never the
exports", posters' size cap and the exports index the library counts.

Every test runs against the suite's own data root (the root conftest points
PLEXORA_DATA_PATH at tmp_path); the first test checks that is where the store
resolves, so nothing here can write into a real install.
"""

import json
import sqlite3

import pytest

from plexora.plugins.video_studio.server import repository, schema
from plexora.plugins.video_studio.server.repository import ConflictError, UnknownVideo

SOURCE = {"datasource": "demo", "image_size": [800, 600], "depth": 12, "frames": 1}


def _document(title="Rotation", datasource="demo"):
    return {"title": title, "source": {**SOURCE, "datasource": datasource},
            "duration_s": 4.0,
            "tracks": [{"track_id": "trk_z", "kind": "acquired_z", "keyframes": [
                {"kf_id": "kf_0", "t": 0, "value": 0}, {"kf_id": "kf_1", "t": 2, "value": 5}]}],
            "provenance": {"preset": {"name": "z_sweep", "params": {"to": 11}}}}


def _db_rows(video_id, sql):
    connection = sqlite3.connect(str(repository.video_dir(video_id) / repository.DB_FILENAME))
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_the_store_resolves_inside_this_tests_data_root(tmp_path):
    assert repository.videos_root() == tmp_path / ".videos"
    video_id = repository.create(_document())
    assert (tmp_path / ".videos" / video_id / "video.db").is_file()


# -- create, load, list --------------------------------------------------------------


def test_a_created_video_loads_back_normalised_at_revision_zero():
    video_id = repository.create(_document())
    assert schema.VIDEO_ID_PATTERN.match(video_id)
    doc = repository.load(video_id)
    assert doc["video_id"] == video_id
    assert doc["revision"] == 0
    assert doc["title"] == "Rotation"
    assert doc["created_at"] and doc["created_at"] == doc["updated_at"]
    assert [kf["value"] for kf in doc["tracks"][0]["keyframes"]] == [0, 5]
    assert repository.exists(video_id)


def test_create_takes_a_title_override_and_an_empty_document():
    assert repository.load(repository.create(_document(), title="Override"))["title"] == \
        "Override"
    empty = repository.load(repository.create())
    assert empty["title"] == schema.DEFAULT_TITLE and empty["tracks"] == []


def test_create_refuses_an_interpolated_plane_and_lists_nothing():
    raw = _document()
    raw["tracks"][0]["keyframes"][1]["value"] = 2.5
    with pytest.raises(schema.IntegrityError):
        repository.create(raw)
    assert repository.list_videos() == []


def test_the_library_lists_newest_first():
    first = repository.create(_document("First"))
    second = repository.create(_document("Second"))
    third = repository.create(_document("Third"))
    assert [v["video_id"] for v in repository.list_videos()] == [third, second, first]
    # An edit makes a video the newest.
    repository.apply(first, 0, [{"op": "set_title", "title": "First, edited"}])
    listed = repository.list_videos()
    assert [v["video_id"] for v in listed] == [first, third, second]
    summary = listed[0]
    assert summary["title"] == "First, edited"
    assert summary["readable"] is True
    assert summary["revision"] == 1
    assert summary["datasource"] == "demo"
    assert summary["duration_s"] == 4.0
    assert summary["track_count"] == 1
    assert summary["export_count"] == 0
    assert summary["preset"] == "z_sweep"


def test_the_library_filters_by_datasource():
    demo = repository.create(_document(datasource="demo"))
    repository.create(_document(datasource="other"))
    assert [v["video_id"] for v in repository.list_videos("demo")] == [demo]
    assert len(repository.list_videos()) == 2
    assert repository.list_videos("nobody") == []


def test_an_empty_or_missing_library_is_an_empty_list(tmp_path):
    assert repository.list_videos() == []
    (tmp_path / ".videos").mkdir()
    (tmp_path / ".videos" / "not-a-video").mkdir()
    (tmp_path / ".videos" / "vid_0123456789ab").mkdir()  # no database inside
    assert repository.list_videos() == []


def test_a_damaged_database_is_one_unreadable_card_and_the_rest_list_fine():
    good = repository.create(_document("Good"))
    bad = repository.create(_document("Bad"))
    (repository.video_dir(bad) / repository.DB_FILENAME).write_bytes(b"not a database" * 100)
    listed = {v["video_id"]: v for v in repository.list_videos()}
    assert set(listed) == {good, bad}
    assert listed[good]["readable"] is True and listed[good]["title"] == "Good"
    assert listed[bad]["readable"] is False
    assert listed[bad]["title"] == bad
    with pytest.raises(schema.UnreadableVideo):
        repository.load(bad)


def test_a_document_from_a_newer_build_is_unreadable_on_load():
    video_id = repository.create(_document())
    path = repository.video_dir(video_id) / repository.DB_FILENAME
    connection = sqlite3.connect(str(path))
    with connection:
        connection.execute("UPDATE document SET json = ?",
                           (json.dumps({"schema_version": schema.SCHEMA_VERSION + 1}),))
    connection.close()
    with pytest.raises(schema.UnreadableVideo):
        repository.load(video_id)


def test_unknown_and_invalid_ids():
    with pytest.raises(UnknownVideo):
        repository.load("vid_000000000000")
    with pytest.raises(ValueError):
        repository.load("../../etc")
    assert repository.exists("../../etc") is False
    assert repository.exists("vid_000000000000") is False


# -- edits and the revision -----------------------------------------------------------


def test_apply_bumps_the_revision_and_returns_the_inverse():
    video_id = repository.create(_document())
    result = repository.apply(video_id, 0, [{"op": "set_title", "title": "Renamed"}])
    assert result["revision"] == 1
    assert result["inverse"] == [{"op": "set_title", "title": "Rotation"}]
    assert result["document"]["title"] == "Renamed"
    loaded = repository.load(video_id)
    assert loaded["revision"] == 1 and loaded["title"] == "Renamed"
    assert loaded["updated_at"] > loaded["created_at"]
    # The journal records what was applied, and by whom.
    rows = _db_rows(video_id, "SELECT base_revision, ops_json, origin FROM journal")
    assert rows == [(0, json.dumps([{"op": "set_title", "title": "Renamed"}],
                                   separators=(",", ":")), "ui")]
    # The inverse, applied as the next edit, puts the document back.
    repository.apply(video_id, 1, result["inverse"], origin="undo")
    restored = repository.load(video_id)
    assert restored["title"] == "Rotation" and restored["revision"] == 2


def test_apply_fills_ids_so_the_inverse_can_name_what_it_removes():
    video_id = repository.create(_document())
    result = repository.apply(video_id, 0, [{"op": "add_track", "track": {
        "kind": "hud", "keyframes": [{"t": 1, "value": {"caption": "x"}}]}}])
    added = result["document"]["tracks"][-1]
    assert result["inverse"] == [{"op": "remove_track", "track_id": added["track_id"]}]


def test_a_stale_revision_is_a_conflict_and_nothing_is_stored():
    video_id = repository.create(_document())
    repository.apply(video_id, 0, [{"op": "set_title", "title": "First"}])
    with pytest.raises(ConflictError) as info:
        repository.apply(video_id, 0, [{"op": "set_title", "title": "Stale"}])
    assert info.value.current_revision == 1
    doc = repository.load(video_id)
    assert doc["title"] == "First" and doc["revision"] == 1
    assert len(_db_rows(video_id, "SELECT seq FROM journal")) == 1


def test_a_base_revision_must_be_an_integer():
    video_id = repository.create(_document())
    for bad in ("0", None, True, 0.0):
        with pytest.raises(ValueError):
            repository.apply(video_id, bad, [{"op": "set_title", "title": "x"}])
        with pytest.raises(ValueError):
            repository.replace(video_id, bad, _document())


def test_an_integrity_error_stores_nothing():
    video_id = repository.create(_document())
    with pytest.raises(schema.IntegrityError):
        repository.apply(video_id, 0, [
            {"op": "set_title", "title": "Half"},
            {"op": "update_keyframe", "track_id": "trk_z", "kf_id": "kf_1",
             "changes": {"value": 2.5}}])
    doc = repository.load(video_id)
    assert doc["title"] == "Rotation" and doc["revision"] == 0
    assert _db_rows(video_id, "SELECT seq FROM journal") == []


def test_a_malformed_operation_stores_nothing():
    video_id = repository.create(_document())
    with pytest.raises(ValueError):
        repository.apply(video_id, 0, [{"op": "remove_track", "track_id": "trk_nope"}])
    assert repository.load(video_id)["revision"] == 0


def test_an_operation_that_is_not_an_object_is_a_value_error():
    video_id = repository.create(_document())
    with pytest.raises(ValueError):
        repository.apply(video_id, 0, ["set_title"])


def test_apply_to_an_unknown_video():
    with pytest.raises(UnknownVideo):
        repository.apply("vid_000000000000", 0, [{"op": "set_title", "title": "x"}])


def test_replace_checks_the_revision_and_keeps_the_creation_time():
    video_id = repository.create(_document())
    created = repository.load(video_id)["created_at"]
    revision = repository.replace(video_id, 0, {**_document("Replaced"),
                                                "created_at": "1999-01-01T00:00:00Z"})
    assert revision == 1
    doc = repository.load(video_id)
    assert doc["title"] == "Replaced" and doc["created_at"] == created
    with pytest.raises(ConflictError):
        repository.replace(video_id, 0, _document("Stale"))
    assert repository.load(video_id)["title"] == "Replaced"
    with pytest.raises(schema.IntegrityError):
        bad = _document()
        bad["tracks"][0]["keyframes"][0]["ease"] = "linear"
        repository.replace(video_id, 1, bad)
    assert repository.load(video_id)["revision"] == 1


# -- duplicate and delete --------------------------------------------------------------


def test_duplicate_copies_the_document_and_posters_but_not_the_exports():
    source = repository.create(_document())
    repository.apply(source, 0, [{"op": "set_loop", "loop": True}])
    repository.set_poster(source, "kf_1", b"poster-bytes", fmt="png")
    repository.record_export(source, "exp_000000000001", status="done",
                             output={"format": "png"})
    copy_id = repository.duplicate(source)
    assert copy_id != source
    original, duplicate = repository.load(source), repository.load(copy_id)
    assert duplicate["title"] == "Rotation copy"
    assert duplicate["revision"] == 0
    assert duplicate["video_id"] == copy_id
    ignore = ("video_id", "title", "revision", "created_at", "updated_at")
    assert {k: v for k, v in duplicate.items() if k not in ignore} == \
        {k: v for k, v in original.items() if k not in ignore}
    assert repository.list_exports(copy_id) == []
    assert len(repository.list_exports(source)) == 1
    assert repository.get_poster(copy_id, "kf_1") == ("png", b"poster-bytes")
    assert _db_rows(copy_id, "SELECT seq FROM journal") == []
    summary = {v["video_id"]: v for v in repository.list_videos()}[copy_id]
    assert summary["revision"] == 0 and summary["title"] == "Rotation copy"


def test_duplicate_takes_a_title_and_refuses_an_unknown_video():
    source = repository.create(_document())
    assert repository.load(repository.duplicate(source, title="Variant"))["title"] == "Variant"
    with pytest.raises(UnknownVideo):
        repository.duplicate("vid_000000000000")


def test_delete_removes_the_video_and_its_files():
    video_id = repository.create(_document())
    exports = repository.exports_dir(video_id)
    exports.mkdir(parents=True)
    (exports / "movie.mp4").write_bytes(b"x")
    repository.delete(video_id)
    assert not repository.video_dir(video_id).exists()
    assert repository.exists(video_id) is False
    with pytest.raises(UnknownVideo):
        repository.load(video_id)
    with pytest.raises(UnknownVideo):
        repository.delete(video_id)
    assert repository.list_videos() == []


# -- posters ---------------------------------------------------------------------------


def test_posters_are_stored_replaced_and_read_back():
    video_id = repository.create(_document())
    assert repository.get_poster(video_id, "kf_0") is None
    repository.set_poster(video_id, "kf_0", b"first", width=10, height=8)
    assert repository.get_poster(video_id, "kf_0") == ("webp", b"first")
    repository.set_poster(video_id, "kf_0", b"second", fmt="png")
    assert repository.get_poster(video_id, "kf_0") == ("png", b"second")


def test_a_poster_over_eight_megabytes_or_empty_is_refused():
    video_id = repository.create(_document())
    with pytest.raises(ValueError):
        repository.set_poster(video_id, "kf_0", b"x" * (repository.MAX_POSTER_BYTES + 1))
    with pytest.raises(ValueError):
        repository.set_poster(video_id, "kf_0", b"")
    repository.set_poster(video_id, "kf_0", b"x" * repository.MAX_POSTER_BYTES)
    assert len(repository.get_poster(video_id, "kf_0")[1]) == repository.MAX_POSTER_BYTES
    with pytest.raises(UnknownVideo):
        repository.set_poster("vid_000000000000", "kf_0", b"x")


# -- the exports index -------------------------------------------------------------------


def test_an_export_row_is_recorded_updated_and_listed(tmp_path):
    video_id = repository.create(_document())
    repository.record_export(video_id, "exp_000000000001", status="running",
                             output={"format": "png", "width": 64})
    rows = repository.list_exports(video_id)
    assert rows == [{"export_id": "exp_000000000001", "status": "running",
                     "output": {"format": "png", "width": 64}, "name": None, "bytes": None,
                     "available": False, "created_at": rows[0]["created_at"]}]
    assert repository.export_manifest(video_id, "exp_000000000001") is None

    target = repository.exports_dir(video_id) / "exp_000000000001"
    target.mkdir(parents=True)
    movie = target / "movie.zip"
    movie.write_bytes(b"12345")
    manifest = {"manifest_version": 1, "frames": [{"k": 0}]}
    # The finishing update names no output: the one recorded at start is kept.
    repository.record_export(video_id, "exp_000000000001", status="done", manifest=manifest,
                             path=movie, size=5)
    (row,) = repository.list_exports(video_id)
    assert row["status"] == "done"
    assert row["output"] == {"format": "png", "width": 64}
    assert (row["name"], row["bytes"], row["available"]) == ("movie.zip", 5, True)
    assert repository.export_manifest(video_id, "exp_000000000001") == manifest
    assert repository.export_manifest(video_id, "exp_nope") is None
    # Only finished exports count in the library.
    repository.record_export(video_id, "exp_000000000002", status="failed")
    summary = repository.list_videos()[0]
    assert summary["export_count"] == 1
    movie.unlink()
    assert {r["export_id"]: r["available"] for r in repository.list_exports(video_id)} == {
        "exp_000000000001": False, "exp_000000000002": False}
