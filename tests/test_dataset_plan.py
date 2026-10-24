"""Grouping a folder's files into projects: identity, confidence, corrections.

The planner is the part of the dataset builder that can be wrong quietly: a
mask given to the wrong slide opens, draws, and is simply someone else's
cells. So the tests pin, per layout, which file went where, how sure the plan
was, and the reason it gives -- and the cases where the right answer is "not
sure, ask": two images with the same generic name, two projects a mask could
equally belong to, a CSV that holds every image's cells.
"""

import os
import random

import pytest

from plexora.server.models import dataset_plan, dataset_scan
from plexora.server.models.dataset_scan import Entry, Inventory
from tests import dataset_fixtures as fx


def _plan(root, overrides=None, config=None):
    inventory = dataset_scan.scan([root])
    return dataset_plan.plan(inventory, overrides, config=config or {})


def _project(plan, name):
    return next(p for p in plan["projects"] if p["name"] == name)


def _rel(slot):
    return slot["rel"] if slot else None


def _entry(rel, kind="raster", **fields):
    root = "/scan"
    path = f"{root}/{rel}"
    return Entry(path=path, real=path, root=root, rel=rel,
                 name=rel.rsplit("/", 1)[-1], kind=kind, inspected=True, **fields)


# -- identity -----------------------------------------------------------------


@pytest.mark.parametrize("rel, expected", [
    ("Sample01.ome.tiff", (("sample", 1), "Sample01")),
    ("sample_1.tif", (("sample", 1), "sample_1")),
    ("SAMPLE01_registered.ome.tif", (("sample", 1), "SAMPLE01")),
    ("LSP11385--unmicst_cell.csv", (("lsp", 11385), "LSP11385")),
    ("X12_cp_masks.tif", (("x", 12), "X12")),
    ("Tumour7_mesmer_whole_cell.tif", (("tumour", 7), "Tumour7")),
    ("Tumour7_nuclear.tif", (("tumour", 7), "Tumour7")),
    # Generic stems take the nearest folder that names something.
    ("Sample01/registered/image.ome.tiff", (("sample", 1), "Sample01")),
    ("segmentation/unmicst-LSP2/cell.ome.tif", (("lsp", 2), "LSP2")),
    ("run3/cell_mask.tif", ((3,), "3")),
])
def test_identity_strips_role_words_and_climbs_past_generic_names(rel, expected):
    identity = dataset_plan.identity_of(_entry(rel))
    assert (identity.key, identity.name) == expected


@pytest.mark.parametrize("rel", ["image.ome.tiff", "images/image.ome.tiff",
                                 "masks/cell_mask.tif", "nuclei_mask.tif",
                                 "outs/segmentation/labels.tif"])
def test_a_generic_name_with_no_naming_folder_has_no_identity(rel):
    assert dataset_plan.identity_of(_entry(rel)) is None


@pytest.mark.parametrize("name, role", [
    ("segmentation", "mask"), ("quantification", "table"), ("registration", "image"),
    ("outs", "structural"), ("s3seg", "mask"), ("Sample01", None), ("run3", None),
    ("cell_masks", "mask"),
])
def test_dir_role(name, role):
    assert dataset_plan.dir_role(name) == role


def test_numbers_compare_as_numbers():
    left = dataset_plan.identity_of(_entry("Sample01.tif"))
    right = dataset_plan.identity_of(_entry("masks/sample_1_mask.tif"))
    other = dataset_plan.identity_of(_entry("masks/sample_10_mask.tif"))
    assert left.key == right.key != other.key


def test_pattern_of_writes_the_identity_as_a_placeholder():
    identity = dataset_plan.identity_of(_entry("Sample01.tif"))
    assert dataset_plan.pattern_of(_entry("Sample01_nuc.tiff"), identity) == "{id}_nuc.tiff"
    assert dataset_plan.pattern_of(
        _entry("seg/Sample01/Sample01_ws.tif"), identity) == "seg/{id}/{id}_ws.tif"


@pytest.mark.parametrize("best, runner, expected", [
    (0.85, 0.0, "high"), (0.6, 0.3, "high"), (0.6, 0.35, "medium"),
    (0.45, 0.0, "medium"), (0.45, 0.4, "low"), (0.35, 0.0, "low"),
])
def test_confidence_is_about_the_competition(best, runner, expected):
    assert dataset_plan.confidence(best, runner) == expected


# -- the five layouts -------------------------------------------------------------


def test_layout_a_role_folders(tmp_path):
    plan = _plan(fx.layout_a(tmp_path / "cohort"))
    assert [p["name"] for p in plan["projects"]] == ["A", "B", "C"]
    a = _project(plan, "A")
    assert _rel(a["mask"]) == "masks/A_mask.ome.tiff"
    assert a["mask"]["confidence"] == "high"
    assert "named A" in a["mask"]["reasons"]
    assert _rel(a["table"]) == "tables/A.csv"
    assert a["status"] == "matched"
    c = _project(plan, "C")
    assert c["mask"] is None and c["status"] == "missing_mask"
    assert plan["conventions"]["layout"] == "role-folders"
    assert plan["conventions"]["mask_pattern"] == "masks/{id}_mask.ome.tiff"
    assert plan["unassigned"] == []


def test_layout_b_generic_names_in_sample_folders(tmp_path):
    plan = _plan(fx.layout_b(tmp_path / "cohort"))
    assert [p["name"] for p in plan["projects"]] == ["Sample01", "Sample02", "Sample03"]
    for name in ("Sample01", "Sample02"):
        project = _project(plan, name)
        assert _rel(project["mask"]) == f"{name}/mask.ome.tiff"
        assert project["mask"]["confidence"] == "high"
        assert f"in folder {name}/" in project["mask"]["reasons"]
        assert _rel(project["table"]) == f"{name}/cells.csv"
    assert _project(plan, "Sample03")["status"] == "missing_mask"
    assert plan["conventions"]["layout"] == "per-sample"


def test_layout_c_mixed(tmp_path):
    plan = _plan(fx.layout_c(tmp_path / "cohort"))
    one, two = _project(plan, "Sample01"), _project(plan, "Sample02")
    assert _rel(one["image"]) == "Sample01/registered/image.ome.tiff"
    assert _rel(one["mask"]) == "Sample01/segmentation/nuclei_mask.ome.tiff"
    assert one["mask"]["confidence"] == "high"
    assert _rel(one["table"]) == "quantification/Sample01_cells.csv"
    assert _rel(two["mask"]) == "Sample02/Sample02_cell_mask.ome.tiff"
    assert _rel(two["table"]) == "quantification/Sample02_cells.csv"
    assert {one["status"], two["status"]} == {"matched"}


def test_layout_d_one_table_for_the_cohort(tmp_path):
    plan = _plan(fx.layout_d(tmp_path / "cohort"))
    a, b = _project(plan, "A"), _project(plan, "B")
    assert _rel(a["table"]) == _rel(b["table"]) == "data/combined.h5ad"
    assert a["table"]["subset"] == {"column": "imageid", "value": "A"}
    assert b["table"]["subset"] == {"column": "imageid", "value": "B"}
    assert a["table"]["shared"] and a["table"]["confidence"] == "high"
    assert "rows where imageid == A" in a["table"]["reasons"]


def test_layout_d_reports_values_that_match_no_image(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "images" / "A.ome.tiff")
    fx.cohort_h5ad(root / "data" / "combined.h5ad", ["A", "Z9"])
    plan = _plan(root)
    assert _project(plan, "A")["table"]["subset"]["value"] == "A"
    assert any("Z9" in warning for warning in plan["warnings"])


def test_layout_e_uneven(tmp_path):
    plan = _plan(fx.layout_e(tmp_path / "cohort"))
    s1 = _project(plan, "S1")
    # Whole-cell before nuclei, and the other offered as an alternative.
    assert _rel(s1["mask"]) == "S1_cell.tif"
    assert [alt["rel"] for alt in s1["mask"]["alternatives"]] == ["S1_nuclei.tif"]
    assert s1["mask"]["confidence"] == "high"
    s2 = _project(plan, "S2")
    assert s2["mask"] is None and s2["status"] == "missing_mask"
    assert [u["rel"] for u in plan["unassigned"]] == ["other/image_mask.ome.tiff"]


def test_mcmicro(tmp_path):
    plan = _plan(fx.mcmicro(tmp_path / "run"))
    assert [p["name"] for p in plan["projects"]] == ["LSP1", "LSP2"]
    lsp1 = _project(plan, "LSP1")
    assert _rel(lsp1["mask"]) == "segmentation/unmicst-LSP1/cell.ome.tif"
    assert [alt["rel"] for alt in lsp1["mask"]["alternatives"]] == [
        "segmentation/unmicst-LSP1/nuclei.ome.tif"]
    assert _rel(lsp1["table"]) == "quantification/LSP1--unmicst_cell.csv"
    assert lsp1["channels"]["names"] == ["DNA", "CD3", "CD8"]
    # Probability maps and QC are a pipeline's working files, not samples.
    skipped = {os.path.basename(s["path"]) for s in plan["skipped"]}
    assert {"probability-maps", "qc"} <= skipped
    assert plan["unassigned"] == []


# -- the traps --------------------------------------------------------------------


def test_two_generic_images_are_two_projects_never_one(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "patientA" / "image.ome.tiff")
    fx.image(root / "patientB" / "image.ome.tiff")
    fx.mask(root / "patientA" / "mask.ome.tiff")
    plan = _plan(root)
    assert [p["name"] for p in plan["projects"]] == ["patientA", "patientB"]
    assert _rel(_project(plan, "patientA")["mask"]) == "patientA/mask.ome.tiff"
    assert _project(plan, "patientB")["mask"] is None


def test_a_mask_two_projects_could_own_is_left_unassigned(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "Sample01" / "dapi_scan.ome.tiff")
    fx.image(root / "Sample01" / "he_scan.ome.tiff")
    fx.mask(root / "Sample01" / "mask.ome.tiff")
    plan = _plan(root)
    assert len(plan["projects"]) == 2
    assert all(p["mask"] is None for p in plan["projects"])
    [item] = plan["unassigned"]
    assert item["rel"] == "Sample01/mask.ome.tiff"
    assert len(item["suggestions"]) == 2
    assert any("equally well" in reason for reason in item["reasons"])


def test_a_different_size_vetoes_a_name_match(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "images" / "A.ome.tiff", size=256)
    fx.mask(root / "masks" / "A_mask.ome.tiff", size=512)
    plan = _plan(root)
    assert _project(plan, "A")["mask"] is None
    [item] = plan["unassigned"]
    assert any("different size" in reason for reason in item["reasons"])


def test_a_lone_same_size_mask_in_the_images_folder_is_a_suggestion_to_review(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "image.ome.tiff", size=256)
    fx.mask(root / "labels.tif", size=256)
    plan = _plan(root)
    [project] = plan["projects"]
    assert _rel(project["mask"]) == "labels.tif"
    assert project["mask"]["confidence"] == "medium"
    assert project["status"] == "review"


def test_a_shared_csv_is_not_split(tmp_path):
    root = tmp_path / "cohort"
    for name in ("A", "B"):
        fx.image(root / "images" / f"{name}.ome.tiff")
    fx.csv(root / "all_cells.csv", extra=["imageid"])
    plan = _plan(root)
    assert all(p["table"] is None for p in plan["projects"])
    [item] = plan["unassigned"]
    assert any("CSV rows are not filtered" in reason for reason in item["reasons"])


def test_duplicates_are_counted_once(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "images" / "A.ome.tiff")
    os.symlink(root / "images" / "A.ome.tiff", root / "A_link.ome.tiff")
    plan = _plan(root)
    assert len(plan["projects"]) == 1
    [duplicate] = plan["duplicates"]
    assert duplicate["rel"] == "images/A.ome.tiff" or duplicate["rel"] == "A_link.ome.tiff"


def test_an_image_already_imported_is_adopted(tmp_path):
    import plexora
    from plexora import get_config

    root = fx.layout_a(tmp_path / "cohort")
    plexora.create_project(str(root / "images" / "A.ome.tiff"), name="Already")
    plan = _plan(root, config=get_config())
    a = next(p for p in plan["projects"] if p["image"]["rel"] == "images/A.ome.tiff")
    assert a["name"] == "Already"
    assert a["existing"] == "Already" and a["status"] == "existing"
    assert plan["counts"]["existing"] == 1 and plan["counts"]["to_create"] == 2


def test_a_name_already_taken_is_suffixed(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    plan = _plan(root, config={"A": {"channelFile": "/elsewhere/a.tif"}})
    assert [p["name"] for p in plan["projects"]] == ["A_2", "B", "C"]


# -- corrections -------------------------------------------------------------------


def test_assign_moves_a_file_and_says_so(tmp_path):
    root = fx.layout_e(tmp_path / "cohort")
    stray = str(root / "other" / "image_mask.ome.tiff")
    plan = _plan(root, {"assign": [{"path": stray, "project": "p:s_2", "slot": "mask"}]})
    s2 = _project(plan, "S2")
    assert s2["mask"]["path"] == stray
    assert s2["mask"]["confidence"] == "user" and s2["status"] == "matched"
    assert plan["unassigned"] == []


def test_exclude_a_folder_drops_everything_in_it(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    plan = _plan(root, {"exclude": [str(root / "tables")]})
    assert all(p["table"] is None for p in plan["projects"])
    assert plan["excluded"] == [{"path": str(root / "tables"), "count": 3,
                                 "folder": True}]


def test_detach_returns_a_file_to_unassigned(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    path = str(root / "masks" / "A_mask.ome.tiff")
    plan = _plan(root, {"detach": [path]})
    assert _project(plan, "A")["mask"] is None
    [item] = plan["unassigned"]
    assert item["path"] == path and "removed by you" in item["reasons"]
    assert item["suggestions"][0]["project"] == "p:a"


def test_new_project_from_a_file_read_as_a_mask(tmp_path):
    root = fx.layout_e(tmp_path / "cohort")
    stray = str(root / "other" / "image_mask.ome.tiff")
    plan = _plan(root, {"new_project": [{"image": stray, "name": "Extra"}]})
    extra = _project(plan, "Extra")
    assert extra["image"]["path"] == stray and extra["id"] == f"n:{stray}"


def test_rename(tmp_path):
    plan = _plan(fx.layout_a(tmp_path / "cohort"), {"rename": {"p:a": "Tumour A"}})
    assert plan["projects"][0]["name"] == "Tumour A"


def test_a_learned_rule_applies_to_every_sample(tmp_path):
    """`{id}_ws.tif` is a mask suffix nothing knows; one correction teaches it."""
    root = tmp_path / "cohort"
    for name in ("Alpha1", "Alpha2", "Alpha3"):
        fx.image(root / "images" / f"{name}.ome.tiff")
        fx.mask(root / "elsewhere" / f"{name}_final_ws.tif")
    # Different sizes, so nothing but the name could tie them.
    fx.image(root / "images" / "Beta.ome.tiff", size=300)
    first = _plan(root)
    # `ws` is not a role word, so these names do not reduce to the samples'.
    assert all(p["mask"] is None or p["mask"]["confidence"] != "high"
               for p in first["projects"])
    taught = str(root / "elsewhere" / "Alpha1_final_ws.tif")
    plan = _plan(root, {"assign": [{"path": taught, "project": "p:alpha_1", "slot": "mask"}],
                        "learn": [{"path": taught, "project": "p:alpha_1"}]})
    for name in ("Alpha2", "Alpha3"):
        mask = _project(plan, name)["mask"]
        assert mask["rel"] == f"elsewhere/{name}_final_ws.tif"
        assert "follows elsewhere/{id}_final_ws.tif" in mask["reasons"]
    [learned] = plan["conventions"]["learned"]
    assert learned["applied_to"] == 2


def test_bad_overrides_are_refused():
    with pytest.raises(ValueError):
        dataset_plan.clean_overrides({"assign": [{"path": "x"}]})
    with pytest.raises(ValueError):
        dataset_plan.clean_overrides({"assign": [{"path": "x", "project": "p", "slot": "bogus"}]})
    with pytest.raises(ValueError):
        dataset_plan.clean_overrides({"exclude": "x"})


# -- determinism and verification ----------------------------------------------------


def test_the_plan_does_not_depend_on_listing_order(tmp_path):
    root = fx.layout_c(tmp_path / "cohort")
    inventory = dataset_scan.scan([root])
    expected = dataset_plan.plan(inventory, config={})
    shuffled = Inventory(roots=list(inventory.roots), counts=dict(inventory.counts))
    entries = list(inventory.entries.values())
    random.Random(4).shuffle(entries)
    for entry in entries:
        shuffled.add(entry)
    assert dataset_plan.plan(shuffled, config={}) == expected


def test_verify_clears_a_file_that_vanished(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    plan = _plan(root)
    os.remove(root / "masks" / "A_mask.ome.tiff")
    os.remove(root / "images" / "B.ome.tiff")
    checked = dataset_plan.verify(plan)
    assert _project(checked, "A")["mask"] is None
    assert any("no longer there" in w for w in _project(checked, "A")["warnings"])
    assert _project(checked, "B")["status"] == "unavailable"
    assert {u["slot"] for u in checked["unavailable"]} == {"mask", "image"}
    # The plan it was given is not changed.
    assert _project(plan, "A")["mask"] is not None


def test_an_ambiguous_mask_is_settled_by_its_name_folder_and_size(tmp_path):
    """One 8-bit plane with one region reads as an image or a mask; named
    `_mask`, filed under `masks/` and exactly the image's size, it is a mask
    and the match is not left for review."""
    from tests.test_import_proposal import _ambiguous

    root = tmp_path / "cohort"
    for name in ("A", "B"):
        fx.image(root / "images" / f"{name}.ome.tiff")
        (root / "masks").mkdir(parents=True, exist_ok=True)
        _ambiguous(root / "masks" / f"{name}_mask.tif", size=256)
    plan = _plan(root)
    assert {p["mask"]["confidence"] for p in plan["projects"]} == {"high"}


def test_a_folder_name_is_never_read_as_a_path_containing_a_sample(tmp_path):
    """`example_data/raw/tonsil.tif` takes `example_data` as a folder key for
    generic files beside it; a table elsewhere under `example_data/` is not
    the image's for having that word in its path."""
    root = tmp_path / "download"
    fx.image(root / "example_data" / "raw" / "tonsil_tma.tif")
    fx.csv(root / "example_data" / "processed" / "spleen_cells.csv")
    plan = _plan(root)
    assert plan["projects"][0]["table"] is None
    assert [u["rel"] for u in plan["unassigned"]] == ["example_data/processed/spleen_cells.csv"]
