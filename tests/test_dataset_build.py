"""From a plan to registered projects, through `create_dataset`.

What matters here is that the dialog makes the same projects a script would --
the specs are the CLI's vocabulary and go through the one creation service --
and that a cohort with one bad sample still lands the other thirty-nine and
names the one that failed.
"""

import json
import threading
import time

import pytest

import plexora
from plexora import cli
from plexora import datasets as api
from plexora.server.models import dataset_build, dataset_plan, dataset_scan, layer_jobs
from plexora.server.models.project import Project
from tests import dataset_fixtures as fx


@pytest.fixture(autouse=True)
def _forget():
    dataset_scan.forget()
    dataset_build.forget()
    yield
    for owner, task_id in layer_jobs.running_tasks():
        if owner in (dataset_build.TASK_OWNER, dataset_scan.TASK_OWNER):
            layer_jobs.stop_task(owner, task_id)
    dataset_build.forget()


def _plan(root, overrides=None):
    from plexora import get_config

    return dataset_plan.plan(dataset_scan.scan([root]), overrides, config=get_config())


def test_specs_speak_the_cli_vocabulary(tmp_path):
    plan = _plan(fx.mcmicro(tmp_path / "run"))
    pairs = dataset_build.specs_for(plan)
    assert [project_id for project_id, _spec in pairs] == ["p:lsp_1", "p:lsp_2"]
    for _id, spec in pairs:
        assert set(spec) <= set(api.PROJECT_SPEC_KEYS)
        api._as_spec(spec)
    spec = pairs[0][1]
    assert spec["segmentation"].endswith("unmicst-LSP1/cell.ome.tif")
    assert spec["data"].endswith("LSP1--unmicst_cell.csv")
    assert spec["channel_names"] == ["DNA", "CD3", "CD8"]
    assert spec["name"] == "LSP1"


def test_a_shared_table_is_read_per_image(tmp_path):
    plan = _plan(fx.layout_d(tmp_path / "cohort"))
    specs = dict(dataset_build.specs_for(plan))
    assert specs["p:a"]["subset"] == {"column": "imageid", "value": "A"}
    assert specs["p:b"]["data"] == specs["p:a"]["data"]


def test_build_registers_a_cohort(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    result = dataset_build.build(_plan(root), name="Cohort A")
    assert [p["status"] for p in result["projects"]] == ["created"] * 3
    dataset = result["dataset"]
    assert dataset.name == "Cohort A" and sorted(dataset) == ["A", "B", "C"]
    a = Project.find("A")
    assert a.segmentation.source.endswith("A_mask.ome.tiff")
    assert a.dataset.src.endswith("A.csv")


def test_build_registers_a_shared_table_once_per_image(tmp_path):
    root = fx.layout_d(tmp_path / "cohort")
    result = dataset_build.build(_plan(root), name="Shared")
    assert [p["status"] for p in result["projects"]] == ["created", "created"]
    for name in ("A", "B"):
        data = Project.find(name).dataset
        assert data.src.endswith("combined.h5ad")
        assert name in json.dumps(data.subset, default=str)


def test_build_adopts_an_image_already_imported(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    plexora.create_project(str(root / "images" / "A.ome.tiff"), name="Earlier")
    plan = _plan(root)
    result = dataset_build.build(plan, name="Cohort")
    statuses = {p["name"]: p["status"] for p in result["projects"]}
    assert statuses == {"Earlier": "adopted", "B": "created", "C": "created"}
    assert "Earlier" in list(result["dataset"])
    assert Project.find("Earlier").segmentation.source.endswith("A_mask.ome.tiff")
    earlier = next(p for p in result["projects"] if p["name"] == "Earlier")
    assert any("table is left as it was" in w for w in earlier["warnings"])


def test_one_failure_does_not_stop_the_rest(tmp_path, monkeypatch):
    root = fx.layout_a(tmp_path / "cohort")
    real = api.create_project

    def flaky(image, **kwargs):
        if kwargs.get("name") == "B":
            raise RuntimeError("the disk said no")
        return real(image, **kwargs)

    monkeypatch.setattr(api, "create_project", flaky)
    result = dataset_build.build(_plan(root), name="Cohort")
    statuses = {p["name"]: (p["status"], p["error"]) for p in result["projects"]}
    assert statuses["A"] == ("created", None)
    assert statuses["B"] == ("failed", "the disk said no")
    assert statuses["C"] == ("created", None)
    assert sorted(result["dataset"]) == ["A", "C"]


def test_cancel_between_projects_keeps_what_landed(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    cancel = threading.Event()

    def progress(done, total, name):
        if done == 1:
            cancel.set()

    result = dataset_build.build(_plan(root), name="Cohort", cancel=cancel,
                                 on_progress=progress)
    statuses = [p["status"] for p in result["projects"]]
    assert statuses[0] == "created" and statuses[2] == "skipped"
    assert "A" in list(result["dataset"])


def test_only_registers_the_projects_named(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    result = dataset_build.build(_plan(root), name="Cohort", only=["p:c"])
    assert [p["name"] for p in result["projects"]] == ["C"]
    assert list(result["dataset"]) == ["C"]


def test_a_vanished_image_fails_its_project_only(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    plan = _plan(root)
    (root / "images" / "B.ome.tiff").unlink()
    result = dataset_build.build(dataset_plan.verify(plan), name="Cohort")
    statuses = {p["name"]: p["status"] for p in result["projects"]}
    assert statuses == {"A": "created", "B": "failed", "C": "created"}


def test_a_paths_entry_is_registered_by_detection(tmp_path):
    folder = tmp_path / "S9"
    fx.image(folder / "S9.ome.tiff")
    fx.mask(folder / "S9_mask.tif")
    dataset = plexora.create_dataset("Detected", projects=[
        {"paths": [str(folder / "S9.ome.tiff"), str(folder / "S9_mask.tif")],
         "name": "Nine"}])
    assert list(dataset) == ["Nine"]
    assert Project.find("Nine").segmentation.source.endswith("S9_mask.tif")


def test_a_paths_entry_is_checked_before_anything_registers(tmp_path):
    fx.image(tmp_path / "A.ome.tiff")
    with pytest.raises(ValueError, match="no such file"):
        plexora.create_dataset("Checked", projects=[
            str(tmp_path / "A.ome.tiff"), {"paths": [str(tmp_path / "missing")]}])
    assert plexora.list_datasets() == []
    with pytest.raises(ValueError, match="cannot be combined"):
        plexora.create_dataset("Mixed", projects=[
            {"paths": [str(tmp_path / "A.ome.tiff")], "segmentation": "x.tif"}])
    with pytest.raises(ValueError, match="goes with `paths`"):
        plexora.create_dataset("Answers", projects=[
            {"image": str(tmp_path / "A.ome.tiff"), "answers": {}}])


@pytest.mark.pro
def test_a_run_is_registered_from_a_paths_entry(tmp_path):
    from tests.test_import_proposal import _xenium_run

    run = _xenium_run(tmp_path / "cohort" / "output-XETG_1")
    plan = _plan(tmp_path / "cohort")
    [(_, spec)] = dataset_build.specs_for(plan)
    assert spec["paths"] == [str(run)]
    result = dataset_build.build(plan, name="Runs")
    assert [p["status"] for p in result["projects"]] == ["created"]


def test_the_spec_document_round_trips_through_the_cli(tmp_path):
    root = fx.layout_b(tmp_path / "cohort")
    document = dataset_build.spec_document(_plan(root), "From file", "a note")
    path = tmp_path / "cohort.dataset.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded = cli._load_spec_file(path)
    dataset = plexora.create_dataset(loaded["name"], projects=loaded["projects"],
                                     description=loaded["description"])
    assert sorted(dataset) == ["Sample01", "Sample02", "Sample03"]
    assert Project.find("Sample01").segmentation.source.endswith("Sample01/mask.ome.tiff")


def test_the_build_task_reports_each_project(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    first = dataset_build.start_build("scan-test", _plan(root), name="Task cohort")
    assert first["status"] == "pending"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        found = dataset_build.build_status("scan-test")
        if found["status"] != "pending":
            break
        time.sleep(0.05)
    assert found["status"] == "ready"
    assert found["counts"] == {"created": 3, "adopted": 0, "failed": 0, "skipped": 0}
    assert found["dataset"]["name"] == "Task cohort"
    assert sorted(found["dataset"]["projects"]) == ["A", "B", "C"]


def test_a_taken_dataset_name_is_refused_before_the_task_starts(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    plexora.create_dataset("Taken")
    with pytest.raises(FileExistsError):
        dataset_build.start_build("scan-x", _plan(root), name="Taken")
    with pytest.raises(ValueError):
        dataset_build.start_build("scan-x", _plan(root), name="  ")


def test_the_python_api(tmp_path):
    root = fx.layout_c(tmp_path / "cohort")
    plan = plexora.plan_dataset(root)
    assert [p["status"] for p in plan["projects"]] == ["matched", "matched"]
    dataset = plexora.create_dataset_from_directory("Mixed", root)
    assert sorted(dataset) == ["Sample01", "Sample02"]
    with pytest.raises(ValueError):
        plexora.create_dataset_from_directory("Empty", tmp_path / "cohort" / "quantification")
