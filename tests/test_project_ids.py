"""A project's id: given once, kept for good, beside a name that stays the key.

Every registry gains ids on its next write, with no migration pass: an entry
written before ids existed reads with none, and the write that next saves it
gives it one. From then on it is the same id through every read and write, a
registration of the same name again keeps it, and nothing else in the entry
moves -- the keys this version does not model least of all.
"""

import json
import re

import pytest

from plexora import paths
from plexora.server.models import datasets
from plexora.server.models.project import ImageSpec, Project, ensure_id, read_config, write_config

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _stored(name, root=None):
    return json.loads(paths.config_path(root).read_text(encoding="utf-8"))[name]


def _old_entry(name, **extra):
    """An entry as a Plexora before ids wrote it."""
    write_config(paths.config_path(), {**read_config(paths.config_path()),
                                       name: {"channelFile": f"/data/{name}.tif",
                                              "createdAt": "2026-01-01T00:00:00", **extra}})


def test_a_new_project_is_given_an_id_once():
    Project(name="s01", image=ImageSpec()).save()
    ident = _stored("s01")["id"]
    assert HEX32.match(ident)
    loaded = Project.load("s01")
    assert loaded.id == ident
    loaded.with_title("Lymph node").save()
    Project.mutate("s01", lambda p: p.patch(created_at="2026-02-02T00:00:00"))
    assert _stored("s01")["id"] == ident == Project.load("s01").id


def test_an_entry_from_before_ids_gains_one_on_its_next_write():
    _old_entry("old")
    assert Project.load("old").recorded_id is None        # reading writes nothing
    assert "id" not in _stored("old")
    pending = Project.load("old")
    pending.with_title("Renamed").save()
    ident = _stored("old")["id"]
    assert HEX32.match(ident) and ident == pending.id     # the one it reported
    assert Project.load("old").recorded_id == ident
    Project.load("old").with_title("Again").save()
    assert _stored("old")["id"] == ident


def test_unmodelled_keys_are_untouched():
    _old_entry("future", someFutureKey={"a": 1}, anotherOne=[1, 2])
    Project.load("future").save()
    stored = _stored("future")
    assert stored["someFutureKey"] == {"a": 1} and stored["anotherOne"] == [1, 2]
    assert "id" not in Project.load("future").extra


def test_an_unsaved_record_reports_one_id_however_it_is_copied():
    record = Project(name="draft", image=ImageSpec())
    first = record.to_entry()["id"]
    assert record.to_entry()["id"] == first == record.id
    assert record.with_title("Copy").to_entry()["id"] == first
    assert Project.from_entry("draft", record.to_entry()).to_entry() == record.to_entry()
    # Two reads of one entry are one record, whatever ids they were given.
    _old_entry("twice")
    assert Project.load("twice") == Project.load("twice")


def test_an_id_somebody_names_is_kept():
    _old_entry("named")
    Project(name="named", image=ImageSpec()).save()
    given = Project.load("named").patch(id="f" * 32)
    assert given.recorded_id == "f" * 32
    given.save()
    assert _stored("named")["id"] == "f" * 32


def test_registering_a_name_again_keeps_its_id():
    Project(name="s02", image=ImageSpec()).save()
    ident = _stored("s02")["id"]
    Project(name="s02", image=ImageSpec(width=64, height=64)).save()
    assert _stored("s02")["id"] == ident


def test_a_stored_id_that_is_not_one_is_replaced():
    _old_entry("odd", id=42)
    assert Project.load("odd").recorded_id is None
    Project.load("odd").save()
    assert HEX32.match(_stored("odd")["id"])
    # One another app wrote, of a sane shape, is kept as it is.
    _old_entry("foreign", id="web-0123456789ab")
    Project.load("foreign").save()
    assert _stored("foreign")["id"] == "web-0123456789ab"


def test_ensure_id_assigns_and_persists_only_the_id():
    _old_entry("bundle_me", someFutureKey={"a": 1})
    before = _stored("bundle_me")
    ident = ensure_id("bundle_me")
    assert HEX32.match(ident)
    after = _stored("bundle_me")
    assert after.pop("id") == ident and after == before
    assert ensure_id("bundle_me") == ident == Project.load("bundle_me").id
    with pytest.raises(KeyError):
        ensure_id("nobody")


def test_ensure_id_on_a_root_nobody_here_can_write(tmp_path, monkeypatch):
    shared = tmp_path / "site"
    shared.mkdir()
    write_config(shared / "config.json", {"site_project": {"channelFile": "/x.tif"}})
    monkeypatch.setattr(paths, "is_writable", lambda root: False)
    first = ensure_id("site_project", data_root=shared)
    assert HEX32.match(first) and ensure_id("site_project", data_root=shared) == first
    assert "id" not in _stored("site_project", shared)


def test_each_root_keeps_its_own_projects_ids(tmp_path, monkeypatch):
    """Read across roots, first root wins as it always has; ids follow the
    entry that is read."""
    lib = tmp_path / "lib"
    lib.mkdir()
    write_config(lib / "config.json", {"both": {"channelFile": "/b.tif", "id": "b" * 32},
                                       "web": {"channelFile": "/w.tif", "id": "c" * 32}})
    _old_entry("both", id="a" * 32)
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(lib))
    paths.reset()
    assert Project.load("both").id == "a" * 32
    assert Project.load("web").id == "c" * 32
    assert Project.load_all()["both"]["id"] == "a" * 32


# -- datasets record members by id as well as name ------------------------------


def test_a_dataset_records_its_members_ids():
    Project(name="m1", image=ImageSpec()).save()
    Project(name="m2", image=ImageSpec()).save()
    cohort = datasets.create("Cohort", projects=["m1"])
    datasets.assign(["m2"], cohort.id)
    stored = datasets._read()[cohort.id]
    assert stored["projects"] == ["m1", "m2"]
    assert stored["projectIds"] == {"m1": Project.load("m1").id, "m2": Project.load("m2").id}
    datasets.assign(["m1"], None)
    assert datasets._read()[cohort.id]["projectIds"] == {"m2": Project.load("m2").id}


def test_a_member_is_found_by_its_id():
    """Recorded under a name that is not visible here, or by id alone."""
    Project(name="here_now", image=ImageSpec()).save()
    Project(name="by_id", image=ImageSpec()).save()
    ident, other = Project.load("here_now").id, Project.load("by_id").id
    write_config(datasets.datasets_path(), {"version": 1, "datasets": {"d1": {
        "name": "Imported", "projects": ["named_elsewhere"],
        "projectIds": {"named_elsewhere": ident, "": [other]}}}})
    known = set(Project.load_all())
    assert datasets.get("d1", known=known).projects == ("here_now", "by_id")
    assert datasets.membership(known=known)["by_id"].id == "d1"
    # A write keeps what it could not resolve to a name, as it was.
    datasets.rename("d1", "Renamed")
    stored = datasets._read()["d1"]
    assert stored["projects"] == ["named_elsewhere"]
    assert stored["projectIds"] == {"named_elsewhere": ident, "": [other]}


def test_an_old_datasets_file_still_reads():
    Project(name="p1", image=ImageSpec()).save()
    write_config(datasets.datasets_path(), {"version": 1, "datasets": {
        "d1": {"name": "Old", "projects": ["p1"]}}})
    assert datasets.get("d1").projects == ("p1",)
    assert datasets.membership(known={"p1"})["p1"].name == "Old"
