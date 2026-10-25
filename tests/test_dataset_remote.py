"""New Dataset over folders that are not on this computer.

A cohort on a cluster is the usual case, not the exception: the run folder
sits on `/n/files` and the viewer runs on a laptop. Two ways to reach it are
covered, each against a real far side rather than a stub -- a files-only
server (`sftp://`, an `sftp-server` process over pipes) and a data node (a
second Plexora process). Pinned: the same mcmicro run plans the same samples
as it does locally; creating it streams the images and masks from where they
are; and on a files-only server, where nothing streams a CSV, each cell table
is copied into its project.
"""

from __future__ import annotations

import time

import pytest

import plexora
from plexora.server.models import dataset_build, dataset_scan, layer_jobs
from plexora.server.models.project import Project
from tests import dataset_fixtures as fx
from tests.helpers import use_data_root
from tests.node_harness import node_process, register  # noqa: F401 - fixture
from tests.sftp_fixtures import SFTPHost, sftp_server_binary


@pytest.fixture
def root(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    dataset_scan.forget()
    dataset_build.forget()
    yield tmp_path
    for owner, task_id in layer_jobs.running_tasks():
        if owner in (dataset_scan.TASK_OWNER, dataset_build.TASK_OWNER):
            layer_jobs.stop_task(owner, task_id)
    dataset_scan.forget()
    dataset_build.forget()


@pytest.fixture
def host(root):
    if sftp_server_binary() is None:
        pytest.skip("no sftp-server on this machine")
    served = SFTPHost(root)
    yield served
    served.disconnect()


def _by_name(plan):
    return {project["name"]: project for project in plan["projects"]}


# -- a files-only server -------------------------------------------------------

def test_an_mcmicro_run_on_a_files_only_server_plans_as_it_does_here(root, host):
    fx.mcmicro(root / "run")
    plan = plexora.plan_dataset(host.url("run"))

    assert plan["place"] == {"kind": "sftp", "name": host.target}
    projects = _by_name(plan)
    assert sorted(projects) == ["LSP1", "LSP2"]
    first = projects["LSP1"]
    assert first["image"]["path"] == host.url("run/registration/LSP1.ome.tif")
    assert first["mask"]["path"].endswith("unmicst-LSP1/cell.ome.tif")
    assert first["mask"]["confidence"] == "high"
    assert first["table"]["path"].endswith("LSP1--unmicst_cell.csv")
    assert first["channels"]["names"] == ["DNA", "CD3", "CD8"]
    assert first["status"] == "matched"
    skipped = " ".join(item["path"] for item in plan["skipped"])
    assert "probability-maps" in skipped and "/qc" in skipped


def test_creating_it_streams_the_images_and_copies_each_table(root, host):
    fx.mcmicro(root / "run")
    dataset = plexora.create_dataset_from_directory("Run", host.url("run"))

    assert sorted(dataset) == ["LSP1", "LSP2"]
    project = Project.load("LSP1")
    assert project.image.src.startswith("sftp://")
    assert project.segmentation.source.endswith("unmicst-LSP1/cell.ome.tif")
    # The table is the project's own copy; nothing is left in the staging area.
    table = project.dataset.src
    assert not table.startswith("sftp://") and table.endswith("LSP1--unmicst_cell.csv")
    assert not list((root / "root" / "uploads").glob("*/*"))
    names = [channel["name"] for channel in plexora.get_config()["LSP1"]["imageData"]]
    assert names[-3:] == ["DNA", "CD3", "CD8"]


def test_the_dialog_scans_a_files_only_server(root, host):
    fx.mcmicro(root / "run")
    client = plexora.app.test_client()
    response = client.post("/datasets/scan", json={"paths": [host.url("run")]})
    assert response.status_code == 202, response.get_json()
    token = response.get_json()["token"]
    deadline = time.monotonic() + 60
    while True:
        body = client.get(f"/datasets/scan/{token}").get_json()
        if not body["pending"] or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert body["status"] == "ready"
    assert [p["status"] for p in body["projects"]] == ["matched", "matched"]


def test_a_server_that_is_not_connected_is_said_plainly(root):
    with pytest.raises(ConnectionError, match="Not connected to nobody@nowhere.test"):
        plexora.plan_dataset("sftp://nobody@nowhere.test/data/run")
    response = plexora.app.test_client().post(
        "/datasets/scan", json={"paths": ["sftp://nobody@nowhere.test/data/run"]})
    assert response.status_code == 400


def test_a_file_or_a_missing_folder_is_refused(root, host):
    fx.mcmicro(root / "run")
    with pytest.raises(ValueError, match="is not a folder"):
        plexora.plan_dataset(host.url("run/markers.csv"))
    with pytest.raises(ValueError, match="is not there"):
        plexora.plan_dataset(host.url("missing"))


def test_one_scan_is_in_one_place(root, host):
    fx.mcmicro(root / "run")
    with pytest.raises(ValueError, match="one place"):
        dataset_scan.place_of([host.url("run"), str(root / "run")])
    with pytest.raises(ValueError, match="web address"):
        dataset_scan.place_of(["https://example.org/data/"])


def test_import_sample_takes_a_csv_from_a_files_only_server(root, host):
    """The same seam outside the builder: picked one by one, the table no
    longer comes back as "nothing Plexora can read"."""
    fx.mcmicro(root / "run")
    picks = [host.url("run/registration/LSP1.ome.tif"),
             host.url("run/quantification/LSP1--unmicst_cell.csv")]
    from plexora.server.models import import_proposal

    proposal = import_proposal.inspect_paths(picks).to_dict()
    assert not proposal["unrecognised"]
    [sample] = proposal["samples"]
    assert {layer["role"] for layer in sample["layers"]} == {"image", "table"}


# -- a data node ---------------------------------------------------------------

@pytest.fixture
def o2(root, node_process):
    node = node_process(dynamic=True)
    register("hms-o2", node)
    return node


def test_an_mcmicro_run_on_a_data_node_plans_and_creates(root, o2):
    # 512 px: the size the node's own detector is known to read (see
    # test_datasets_api_on_a_node).
    fx.mcmicro(root / "run", size=512)
    plan = plexora.plan_dataset(str(root / "run"), node="hms-o2")

    assert plan["place"] == {"kind": "node", "name": "hms-o2"}
    projects = _by_name(plan)
    assert sorted(projects) == ["LSP1", "LSP2"]
    first = projects["LSP1"]
    assert first["mask"]["path"].endswith("unmicst-LSP1/cell.ome.tif")
    assert first["mask"]["confidence"] == "high"
    assert first["table"]["path"].endswith("LSP1--unmicst_cell.csv")
    assert first["channels"]["names"] == ["DNA", "CD3", "CD8"]

    [(_id, spec)] = dataset_build.specs_for(plan, only=[first["id"]])
    assert spec["node"] == "hms-o2"
    dataset = plexora.create_dataset_from_directory("Run", str(root / "run"),
                                                    node="hms-o2")
    assert sorted(dataset) == ["LSP1", "LSP2"]
    project = Project.load("LSP1")
    assert project.resource("image").node == "hms-o2"
    assert project.resource("segmentation").node == "hms-o2"


def test_a_file_on_a_node_is_not_a_folder(root, o2):
    fx.mcmicro(root / "run", size=512)
    with pytest.raises(ValueError, match="is a file"):
        plexora.plan_dataset(str(root / "run" / "markers.csv"), node="hms-o2")


def test_an_older_node_that_sends_no_size_still_plans(root, o2, monkeypatch):
    """A node started from an older checkout answers `/detect` without the
    header's size. The plan matches by name alone then, and a channel list it
    cannot check against the image is not applied -- the image keeps its
    own names, and the row says so."""
    from plexora import nodes as node_api

    real = node_api.detect_on_node

    def older(node, path):
        found = dict(real(node, path))
        found.pop("geometry", None)
        return found

    monkeypatch.setattr(node_api, "detect_on_node", older)
    fx.mcmicro(root / "run", size=512)
    plan = plexora.plan_dataset(str(root / "run"), node="hms-o2")
    first = _by_name(plan)["LSP1"]
    assert first["mask"]["path"].endswith("unmicst-LSP1/cell.ome.tif")
    assert first["table"]["path"].endswith("LSP1--unmicst_cell.csv")
    assert first["channels"]["names"] is None
    assert "Restarting the data node" in first["channels"]["warning"]
    [(_id, spec)] = dataset_build.specs_for(plan, only=[first["id"]])
    assert "channel_names" not in spec


def test_a_folder_on_a_node_is_checked_by_one_stat(root, o2, monkeypatch):
    """Starting a scan used to ask the node to DETECT the folder, which looks
    inside it for DICOM instances -- the slow part, on a cluster's network
    filesystem. A stat of the name says folder or file just as well."""
    from plexora import nodes as node_api

    fx.mcmicro(root / "run", size=512)
    asked = []
    monkeypatch.setattr(node_api, "detect_on_node",
                        lambda *args: asked.append(args) or {"kind": None})
    found, place = dataset_scan.check_roots([str(root / "run") + "/"], node="hms-o2")
    assert found == [str((root / "run").resolve())] and place == ("node", "hms-o2")
    assert not asked
    with pytest.raises(ValueError, match="is not there on hms-o2"):
        dataset_scan.check_roots([str(root / "missing")], node="hms-o2")
