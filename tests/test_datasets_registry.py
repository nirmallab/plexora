"""The dataset registry: a folder that holds project names and nothing else.

What these pin is mostly what a dataset is NOT allowed to do. It is not a
project (so it cannot live in config.json), it does not own the projects in it
(so deleting one releases them), and it does not get to lose membership because
a shared drive was not mounted this morning.
"""

import json

import pytest

from plexora import paths
from plexora.server.models import datasets
from plexora.server.models.data_migration import migratable
from plexora.server.models.project import Project, read_config, write_config
from tests import helpers


@pytest.fixture
def two_projects(tmp_path):
    """Two registered projects, which is the least a cohort can be."""
    write_config(paths.config_path(), {
        "sample1": helpers.entry("sample1"),
        "sample2": helpers.entry("sample2"),
    })
    return ("sample1", "sample2")


def test_a_new_dataset_holds_the_projects_it_was_given(two_projects):
    dataset = datasets.create("Melanoma Cohort", projects=two_projects)

    assert dataset.name == "Melanoma Cohort"
    assert dataset.projects == ("sample1", "sample2")
    assert dataset.id and len(dataset.id) == 12
    assert datasets.get(dataset.id).projects == ("sample1", "sample2")


def test_datasets_live_beside_config_not_inside_it(two_projects):
    """The one structural rule. Every top-level key of config.json is a
    project, so a dataset recorded there would show up on the Open Project
    page as an image-less project nobody could delete."""
    datasets.create("Cohort", projects=two_projects)

    assert set(read_config(paths.config_path())) == {"sample1", "sample2"}
    assert datasets.datasets_path().exists()
    stored = json.loads(datasets.datasets_path().read_text(encoding="utf-8"))
    assert stored["version"] == datasets.VERSION
    assert list(stored["datasets"].values())[0]["name"] == "Cohort"


def test_the_registry_migrates_with_the_root(two_projects):
    """`migratable` copies every top-level name, so a sibling file needs no
    special case to travel with the data directory."""
    datasets.create("Cohort", projects=two_projects)

    assert datasets.FILENAME in migratable(paths.data_root())


def test_a_name_must_be_a_name():
    for bad in ("", "   ", "x" * 200):
        with pytest.raises(datasets.DatasetError):
            datasets.create(bad)


def test_two_datasets_cannot_share_a_name_even_in_different_case():
    datasets.create("Melanoma")

    with pytest.raises(datasets.DatasetError) as caught:
        datasets.create("melanoma")

    assert "Melanoma" in str(caught.value)


def test_a_dataset_cannot_hold_a_project_that_does_not_exist():
    with pytest.raises(datasets.DatasetError) as caught:
        datasets.create("Cohort", projects=["ghost"])

    assert "ghost" in str(caught.value)
    assert not datasets.datasets_path().exists()


def test_the_id_survives_a_rename(two_projects):
    """URLs and drop targets are keyed on the id. Renaming a folder is not
    creating a different one."""
    dataset = datasets.create("Cohort", projects=two_projects)

    renamed = datasets.rename(dataset.id, "Melanoma Cohort")

    assert renamed.id == dataset.id
    assert renamed.name == "Melanoma Cohort"
    assert renamed.projects == dataset.projects


def test_renaming_to_a_taken_name_is_refused_but_to_its_own_is_not(two_projects):
    first = datasets.create("One")
    datasets.create("Two")

    with pytest.raises(datasets.DatasetError):
        datasets.rename(first.id, "two")

    assert datasets.rename(first.id, "One").name == "One"


def test_assigning_moves_a_project_out_of_wherever_it_was(two_projects):
    """Single parent. A project in two folders at once is a state the file
    browser has no way to draw."""
    first = datasets.create("First", projects=["sample1", "sample2"])
    second = datasets.create("Second")

    datasets.assign(["sample1"], second.id)

    assert datasets.get(first.id).projects == ("sample2",)
    assert datasets.get(second.id).projects == ("sample1",)


def test_assigning_to_nothing_unassociates(two_projects):
    dataset = datasets.create("Cohort", projects=two_projects)

    assert datasets.assign(["sample1"], None) is None

    assert datasets.get(dataset.id).projects == ("sample2",)
    assert datasets.membership() == {"sample2": datasets.get(dataset.id)}


def test_assigning_an_unknown_project_is_refused(two_projects):
    dataset = datasets.create("Cohort")

    with pytest.raises(datasets.DatasetError):
        datasets.assign(["ghost"], dataset.id)

    assert datasets.get(dataset.id).projects == ()


def test_assigning_to_an_unknown_dataset_is_refused(two_projects):
    with pytest.raises(datasets.DatasetError):
        datasets.assign(["sample1"], "nosuchid")


def test_deleting_a_dataset_leaves_its_projects_alone(two_projects):
    dataset = datasets.create("Cohort", projects=two_projects)

    released = datasets.remove(dataset.id)

    assert released.projects == ("sample1", "sample2")
    assert datasets.load_all() == {}
    assert set(read_config(paths.config_path())) == {"sample1", "sample2"}
    assert Project.find("sample1") is not None


def test_deleting_a_dataset_that_is_not_there_is_not_an_error():
    assert datasets.remove("nosuchid") is None


def test_forget_project_drops_one_name(two_projects):
    dataset = datasets.create("Cohort", projects=two_projects)

    datasets.forget_project("sample1")

    assert datasets.get(dataset.id).projects == ("sample2",)


def test_forget_project_writes_nothing_when_there_is_nothing_to_forget():
    """Deleting a project on a machine that has never made a dataset must not
    conjure a registry file."""
    assert datasets.forget_project("sample1") is None
    assert not datasets.datasets_path().exists()


def test_a_dangling_member_is_hidden_from_the_view_but_kept_on_disk(two_projects):
    """A shared root that is not mounted today must not permanently erase the
    grouping somebody built out of it."""
    dataset = datasets.create("Cohort", projects=two_projects)

    pruned = datasets.load_all(known=["sample1"])[dataset.id]
    assert pruned.projects == ("sample1",)

    stored = json.loads(datasets.datasets_path().read_text(encoding="utf-8"))
    assert stored["datasets"][dataset.id]["projects"] == ["sample1", "sample2"]


def test_membership_is_the_reverse_index(two_projects):
    first = datasets.create("First", projects=["sample1"])
    datasets.create("Second", projects=["sample2"])

    index = datasets.membership()

    assert index["sample1"].id == first.id
    assert index["sample2"].name == "Second"


def test_a_shared_project_can_be_a_member(tmp_path, monkeypatch):
    """Membership is recorded in the user's root, so a read-only shared
    project joining a cohort writes nothing over there."""
    shared, = helpers.use_shared_roots(monkeypatch, tmp_path / "site")
    write_config(paths.config_path(shared), {"atlas": helpers.entry("atlas")})

    dataset = datasets.create("Cohort", projects=["atlas"])

    assert datasets.get(dataset.id).projects == ("atlas",)
    assert datasets.datasets_path().parent == paths.data_root()
    assert not (shared / datasets.FILENAME).exists()


def test_find_by_name_is_case_insensitive():
    dataset = datasets.create("Melanoma Cohort")

    assert datasets.find_by_name("melanoma cohort").id == dataset.id
    assert datasets.find_by_name("nothing") is None


def test_resolve_takes_an_id_or_a_name():
    dataset = datasets.create("Cohort")

    assert datasets.resolve(dataset.id).id == dataset.id
    assert datasets.resolve("cohort").id == dataset.id
    with pytest.raises(datasets.DatasetError):
        datasets.resolve("other")


def test_get_names_what_does_exist():
    datasets.create("Melanoma")

    with pytest.raises(datasets.DatasetError) as caught:
        datasets.get("nosuchid")

    assert "Melanoma" in str(caught.value)


def test_describe_merges_meta_rather_than_replacing_it():
    """Two screens will write into `meta` -- a palette and a marker mapping --
    and a whole-bag replacement means whichever saves second erases the
    other."""
    dataset = datasets.create("Cohort")

    datasets.describe(dataset.id, description="Trial 7", meta={"palette": "viridis"})
    updated = datasets.describe(dataset.id, meta={"markers": {"CD3": "CD3e"}})

    assert updated.description == "Trial 7"
    assert updated.meta == {"palette": "viridis", "markers": {"CD3": "CD3e"}}

    cleared = datasets.describe(dataset.id, meta={"palette": None})
    assert cleared.meta == {"markers": {"CD3": "CD3e"}}


def test_a_record_with_no_name_is_read_as_no_record():
    """One hand-edited entry must not make the whole page 500."""
    datasets.datasets_path().write_text(json.dumps({
        "version": 1,
        "datasets": {"aaa": {"projects": ["x"]}, "bbb": {"name": "Real"}},
    }), encoding="utf-8")

    assert [ds.name for ds in datasets.load_all().values()] == ["Real"]


def test_the_temp_file_is_gone_after_a_write():
    datasets.create("Cohort")

    leftovers = list(paths.data_root().glob(f"{datasets.FILENAME}.*"))
    assert leftovers == []


def test_order_of_addition_is_kept(two_projects):
    """Not meaningful yet; next/previous sample will want an order somebody
    chose, and a set would have thrown it away by then."""
    dataset = datasets.create("Cohort", projects=["sample2"])

    datasets.assign(["sample1"], dataset.id)

    assert datasets.get(dataset.id).projects == ("sample2", "sample1")


def test_a_write_while_a_root_is_unmounted_keeps_its_members(two_projects):
    """The half that pruning-in-the-view alone does not cover.

    If a mutation re-read the file PRUNED, renaming a dataset while a shared
    drive happened to be unmounted would quietly drop every project on it --
    the rename would succeed and the grouping would be gone. So every mutation
    re-reads unpruned, and a dangling name leaves the file only when the
    project is really deleted.
    """
    dataset = datasets.create("Cohort", projects=two_projects)
    # sample2's root has gone away: it no longer resolves.
    write_config(paths.config_path(), {"sample1": helpers.entry("sample1")})

    datasets.rename(dataset.id, "Melanoma")
    datasets.describe(dataset.id, description="Trial 7")

    stored = json.loads(datasets.datasets_path().read_text(encoding="utf-8"))
    assert stored["datasets"][dataset.id]["projects"] == ["sample1", "sample2"]
    # And it comes back the moment the root is mounted again.
    write_config(paths.config_path(), {
        "sample1": helpers.entry("sample1"), "sample2": helpers.entry("sample2")})
    assert datasets.get(dataset.id).projects == ("sample1", "sample2")
