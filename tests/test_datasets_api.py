"""Datasets and projects from Python.

Two things are being pinned. The first is that a cohort registers in a loop --
which is the whole reason the module exists. The second is subtler and matters
more: **an answer given here is an answer, and a value worked out is a guess**.
A bulk registration that marked every prediction as a decision would propagate
one bad heuristic across forty slides with nothing ever asking about it again.
"""

import subprocess
import sys
import textwrap

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora import datasets as api
from plexora.server.models import datasets as registry
from plexora.server.models.project import Project


def _image(tmp_path, name="slide.ome.tif"):
    """256px, not smaller: load_datasource walks down to the last pyramid level
    with every dimension >= 200, and a smaller image makes that walk raise."""
    path = tmp_path / name
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _csv(tmp_path, name="cells.csv"):
    path = tmp_path / name
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(1, 4, 4),
        "Y_centroid": np.linspace(1, 4, 4),
        "CD3": np.linspace(0, 3, 4),
        "area": np.linspace(10, 40, 4),
    }).write_csv(path)
    return path


# --------------------------------------------------------------------------
# An image alone is a project
# --------------------------------------------------------------------------

def test_an_image_is_enough(tmp_path):
    name = plexora.create_project(_image(tmp_path))

    assert name == "slide"
    project = Project.load(name)
    assert project.image.src
    assert project.has_table is False
    assert api.project_manifest(name)["summary"]["needsSetup"] is False


def test_a_cohort_registers_in_one_call(tmp_path):
    dataset = plexora.create_dataset("Melanoma Cohort", images=[
        _image(tmp_path, "s1.ome.tif"),
        _image(tmp_path, "s2.ome.tif"),
    ])

    assert dataset.name == "Melanoma Cohort"
    assert dataset.projects == ("s1", "s2")
    assert len(dataset) == 2
    assert "s1" in dataset
    assert set(Project.load_all()) == {"s1", "s2"}


def test_a_dataset_can_be_made_empty(tmp_path):
    """Somewhere to drag things into is a reasonable thing to want."""
    dataset = plexora.create_dataset("Cohort")

    assert dataset.projects == ()
    assert api.list_datasets()[0].id == dataset.id


def test_a_richer_spec_gives_each_project_its_own_files(tmp_path):
    dataset = plexora.create_dataset("Cohort", projects=[
        {"image": str(_image(tmp_path, "s1.ome.tif")),
         "data": str(_csv(tmp_path)), "cell_id": "CellID"},
        str(_image(tmp_path, "s2.ome.tif")),
    ])

    assert dataset.projects == ("s1", "s2")
    assert Project.load("s1").has_table is True
    assert Project.load("s2").has_table is False


# --------------------------------------------------------------------------
# An answer is an answer; a guess is a guess
# --------------------------------------------------------------------------

def test_a_named_column_is_recorded_as_answered(tmp_path):
    plexora.create_project(_image(tmp_path), data=_csv(tmp_path),
                           cell_id="CellID", name="s1")

    manifest = api.project_manifest("s1")["manifest"]

    assert manifest["role:cell_id"]["status"] == "present"
    assert manifest["role:cell_id"]["confirmed"] is True
    assert manifest["role:cell_id"]["value"] == "CellID"


def test_a_column_nobody_named_stays_a_guess(tmp_path):
    """The predictor finds X_centroid and Y_centroid on its own. Recording that
    as a decision is how one bad heuristic reaches a whole cohort silently."""
    plexora.create_project(_image(tmp_path), data=_csv(tmp_path), name="s1")

    manifest = api.project_manifest("s1")["manifest"]

    assert manifest["role:x"]["status"] == "guessed"
    assert manifest["role:x"]["confirmed"] is False


def test_naming_the_markers_answers_the_split(tmp_path):
    plexora.create_project(_image(tmp_path), data=_csv(tmp_path), name="s1",
                           markers=["CD3"], metadata=["CellID", "X_centroid",
                                                      "Y_centroid", "area"])

    project = Project.load("s1")
    assert project.columns.markers == ("CD3",)
    assert api.project_manifest("s1")["manifest"]["markers"]["confirmed"] is True


def test_naming_the_metadata_is_enough(tmp_path):
    """One side implies the other. Naming the morphology columns and nothing
    else used to leave the marker list empty, which reads as unclassified --
    so the answer put the classification question back rather than settling
    it."""
    plexora.create_project(_image(tmp_path), data=_csv(tmp_path), name="s1",
                           metadata=["CellID", "X_centroid", "Y_centroid",
                                     "area"])

    project = Project.load("s1")
    assert project.columns.markers == ("CD3",)
    assert api.project_manifest("s1")["manifest"]["markers"]["confirmed"] is True


def test_naming_the_markers_alone_leaves_the_rest_metadata(tmp_path):
    plexora.create_project(_image(tmp_path), data=_csv(tmp_path), name="s1",
                           markers=["CD3", "area"])

    project = Project.load("s1")
    assert project.columns.markers == ("CD3", "area")
    assert set(project.columns.metadata) == {"CellID", "X_centroid",
                                             "Y_centroid"}


def test_naming_both_lists_still_means_exactly_those(tmp_path):
    """The complement is for the side nobody mentioned. A caller who listed
    both has said that a column in neither belongs in neither -- `area` here
    stays out of the panel rather than being swept into it."""
    plexora.create_project(_image(tmp_path), data=_csv(tmp_path), name="s1",
                           markers=["CD3"],
                           metadata=["CellID", "X_centroid", "Y_centroid"])

    project = Project.load("s1")
    assert project.columns.markers == ("CD3",)
    assert "area" not in project.columns.all


def test_one_image_is_an_answer_not_a_blank(tmp_path):
    plexora.create_project(_image(tmp_path), data=_csv(tmp_path), name="s1",
                           single_image=True)

    manifest = api.project_manifest("s1")["manifest"]
    assert manifest["role:image_id"]["value"] == "Single image"
    assert manifest["role:image_id"]["confirmed"] is True


# --------------------------------------------------------------------------
# Failing before anything has been converted
# --------------------------------------------------------------------------

def test_a_bad_path_fails_before_a_single_pyramid_is_built(tmp_path):
    """A cohort that fails on the last slide because of a typo in its filename
    should fail before the first has spent four minutes converting."""
    with pytest.raises(ValueError) as caught:
        plexora.create_dataset("Cohort", images=[
            _image(tmp_path, "s1.ome.tif"), tmp_path / "ghost.ome.tif"])

    assert "ghost.ome.tif" in str(caught.value)
    assert Project.load_all() == {}
    assert registry.load_all() == {}


def test_an_unknown_option_is_refused_by_name(tmp_path):
    with pytest.raises(ValueError) as caught:
        plexora.create_dataset("Cohort", projects=[
            {"image": str(_image(tmp_path)), "cellid": "CellID"}])

    assert "cellid" in str(caught.value)


def test_images_and_projects_are_the_same_argument(tmp_path):
    with pytest.raises(ValueError) as caught:
        plexora.create_dataset("Cohort", images=["a"], projects=["b"])

    assert "not both" in str(caught.value)


def test_a_failure_part_way_keeps_what_landed(tmp_path):
    """No rollback: undoing an hour of conversion to tidy up after one bad file
    is worse than the mess. What succeeded is named instead."""
    broken = tmp_path / "broken.ome.tif"
    broken.write_bytes(b"not a tiff")

    with pytest.raises(plexora.DatasetCreateError) as caught:
        plexora.create_dataset("Cohort", images=[
            _image(tmp_path, "s1.ome.tif"), broken])

    error = caught.value
    assert error.created == ["s1"]
    assert error.failed["image"].endswith("broken.ome.tif")
    assert error.cause is not None
    # And the ones that worked are in the dataset, not orphaned beside it.
    assert error.dataset.projects == ("s1",)
    assert Project.find("s1") is not None


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def test_a_second_dataset_of_the_same_name_is_refused(tmp_path):
    plexora.create_dataset("Cohort")

    with pytest.raises(ValueError):
        plexora.create_dataset("cohort")


def test_exist_ok_adds_to_the_dataset_that_is_there(tmp_path):
    plexora.create_dataset("Cohort", images=[_image(tmp_path, "s1.ome.tif")])

    dataset = plexora.create_dataset("Cohort", exist_ok=True,
                                     images=[_image(tmp_path, "s2.ome.tif")])

    assert dataset.projects == ("s1", "s2")


def test_a_project_name_already_taken_is_refused(tmp_path):
    plexora.create_project(_image(tmp_path), name="s1")

    with pytest.raises(ValueError) as caught:
        plexora.create_project(_image(tmp_path, "other.ome.tif"), name="s1")

    assert "s1" in str(caught.value)


def test_exist_ok_adopts_a_project_already_pointing_at_the_image(tmp_path):
    """So a batch can be re-run after fixing one bad path without
    re-converting everything before it."""
    image = _image(tmp_path)
    first = plexora.create_project(image)

    second = plexora.create_project(image, exist_ok=True)

    assert second == first
    assert list(Project.load_all()) == [first]


def test_a_file_on_a_node_nobody_registered_is_refused_by_name(tmp_path):
    """A node address is now a thing this API takes, so the refusal is about
    the node rather than about the syntax -- and it names what IS registered,
    because "which nodes do I know" is the next question somebody has."""
    with pytest.raises(ValueError) as caught:
        plexora.create_project(_image(tmp_path), data="node://hpc/cells")

    assert "hpc" in str(caught.value)
    assert "known nodes" in str(caught.value)
    assert Project.load_all() == {}


# --------------------------------------------------------------------------
# Configuring afterwards
# --------------------------------------------------------------------------

def test_a_table_can_be_attached_to_an_image_only_project(tmp_path):
    name = plexora.create_project(_image(tmp_path))

    result = plexora.configure_project(name, data=_csv(tmp_path), cell_id="CellID")

    assert result["manifest"]["table"]["status"] == "present"
    assert result["manifest"]["role:cell_id"]["confirmed"] is True


def test_configuring_an_unknown_project_says_so(tmp_path):
    with pytest.raises(KeyError):
        plexora.configure_project("ghost", cell_id="CellID")


def test_configuring_with_an_unknown_option_is_refused(tmp_path):
    name = plexora.create_project(_image(tmp_path))

    with pytest.raises(ValueError) as caught:
        plexora.configure_project(name, cellid="CellID")

    assert "cellid" in str(caught.value)


def test_naming_a_table_with_no_data_file_says_what_is_missing(tmp_path):
    name = plexora.create_project(_image(tmp_path))

    with pytest.raises(ValueError) as caught:
        plexora.configure_project(name, table="cells")

    assert "no data file" in str(caught.value)


# --------------------------------------------------------------------------
# The handle
# --------------------------------------------------------------------------

def test_a_dataset_handle_adds_removes_and_renames(tmp_path):
    plexora.create_project(_image(tmp_path, "s1.ome.tif"))
    plexora.create_project(_image(tmp_path, "s2.ome.tif"))
    dataset = plexora.create_dataset("Cohort")

    dataset = dataset.add("s1", "s2")
    assert dataset.projects == ("s1", "s2")

    dataset = dataset.remove("s1")
    assert dataset.projects == ("s2",)
    assert Project.find("s1") is not None, "removing is not deleting"

    dataset = dataset.rename("Melanoma")
    assert dataset.name == "Melanoma"
    assert plexora.dataset("melanoma").id == dataset.id


def test_deleting_a_dataset_leaves_its_projects(tmp_path):
    dataset = plexora.create_dataset("Cohort",
                                     images=[_image(tmp_path, "s1.ome.tif")])

    dataset.delete()

    assert api.list_datasets() == []
    assert Project.find("s1") is not None


def test_an_unknown_dataset_is_a_key_error_naming_what_exists(tmp_path):
    plexora.create_dataset("Melanoma")

    with pytest.raises(KeyError) as caught:
        plexora.dataset("nothing")

    assert "Melanoma" in str(caught.value)


def test_a_dataset_reports_what_each_of_its_projects_has(tmp_path):
    dataset = plexora.create_dataset("Cohort", projects=[
        {"image": str(_image(tmp_path, "s1.ome.tif")), "data": str(_csv(tmp_path))},
        str(_image(tmp_path, "s2.ome.tif")),
    ])

    manifests = dataset.manifest()

    assert set(manifests) == {"s1", "s2"}
    assert manifests["s1"]["summary"]["table"] == "present"
    assert manifests["s2"]["summary"]["table"] == "missing"


def test_a_project_can_name_its_dataset_at_registration(tmp_path):
    """Including one that does not exist yet, which is plainly what
    `dataset="Cohort"` means."""
    plexora.create_project(_image(tmp_path), dataset="Cohort")

    assert plexora.dataset("Cohort").projects == ("slide",)


def test_describe_merges_rather_than_replacing(tmp_path):
    dataset = plexora.create_dataset("Cohort")

    dataset = dataset.describe("Trial 7", palette="viridis")
    dataset = dataset.describe(markers={"CD3": "CD3e"})

    assert dataset.description == "Trial 7"
    assert dataset.meta == {"palette": "viridis", "markers": {"CD3": "CD3e"}}


# --------------------------------------------------------------------------
# The import boundary
# --------------------------------------------------------------------------

def test_naming_the_dataset_api_does_not_pull_in_anndata():
    """`plexora.datasets` reaches the adapters, and a core build importing
    anndata is exactly what tests/test_plugin_boundary.py exists to prevent.
    The lazy `_PUBLIC_API` map is what keeps it out; this is what says so."""
    script = textwrap.dedent("""
        import sys
        import plexora
        plexora.create_dataset, plexora.create_project, plexora.PROJECT_SPEC_KEYS
        assert "anndata" not in sys.modules, sorted(
            m for m in sys.modules if "anndata" in m)
        print("clean")
    """)
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "clean" in proc.stdout
