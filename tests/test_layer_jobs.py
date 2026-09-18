"""One progress vocabulary for everything a sample is still preparing.

There used to be two and they did not agree. The segmentation pyramid reported
percentage bands out of `data_model`; the transcripts plugin reported
`{stage, done, total}` out of a dict of its own, which no other surface could
read and which a restart lost. A third modality would have been a third, and the
viewer would have had to know what a sample held before it could ask what it was
waiting for.

What these pin is the shape of the replacement: one document per sample, a
builder registered by MODALITY rather than branched on in core, and a layer
nobody can build reported as pending with something to act on rather than as an
import that failed.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import data_model, layer_jobs
from plexora.server.models.project import LayerSpec, Project

from tests.helpers import use_data_root


@pytest.fixture
def sample(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    tifffile.imwrite(tmp_path / "slide.ome.tif",
                     np.full((1, 64, 64), 5, dtype=np.uint16))
    plexora.datasource.register_image_datasource("demo", tmp_path / "slide.ome.tif")
    project = Project.mutate("demo", lambda p: p.with_layer(LayerSpec(
        id="tx", kind="points", modality="transcripts",
        label="Transcripts", src=str(tmp_path / "tx.parquet"),
        status="pending")))
    layer_jobs.forget()
    layer_jobs._BUILDERS.clear()
    # `data_model`'s segmentation jobs are keyed by datasource NAME alone --
    # one data root per server is the deployment this app has -- so a project
    # called "demo" in another test's tmp_path leaves a record this one would
    # read as its own.
    data_model._segmentation_jobs.clear()
    yield project
    layer_jobs.forget()
    layer_jobs._BUILDERS.clear()
    data_model._segmentation_jobs.clear()


def _wait(name, layer_id, want, tries=200):
    import time

    for _ in range(tries):
        record = layer_jobs.get(name, layer_id)
        if record and record["status"] == want:
            return record
        time.sleep(0.01)
    raise AssertionError(
        f"{layer_id} never reached {want}: {layer_jobs.get(name, layer_id)}")


def test_one_document_covers_the_mask_and_every_layer(sample):
    """The reason this module exists: the viewer polls once, whatever the
    sample holds and whichever surface started the work."""
    from dataclasses import replace

    document = layer_jobs.status("demo")
    assert set(document["layers"]) == {"tx"}
    assert document["pending"] is True

    # A mask, whose job is `data_model`'s and not this module's, appears in the
    # same document. That composition is the point: the viewer polls one
    # endpoint whichever surface started the work.
    Project.mutate("demo", lambda p: p.patch(
        segmentation=replace(p.segmentation, source="somewhere.tif",
                             status="pending")))
    document = layer_jobs.status("demo")
    assert set(document["layers"]) == {"tx", "__mask__"}
    assert document["layers"]["__mask__"]["status"] == "pending"


def test_a_builder_is_registered_by_modality_not_branched_on(sample):
    """Core knows a layer is pending and how to run a thread. It does not know
    what a transcript file is, and a `if modality == "transcripts"` anywhere in
    core would be the boundary the transcripts plugin exists to keep."""
    ran = []

    def builder(project, layer, stage, report):
        stage("reading")
        report(1, 2)
        ran.append((project.name, layer.id))

    layer_jobs.register_builder("transcripts", builder)
    project = Project.load("demo")
    assert layer_jobs.start_builder(project, project.layer("tx")) is True

    _wait("demo", "tx", "ready")
    assert ran == [("demo", "tx")]
    # And the outcome is on the RECORD, so a restarted server still knows.
    assert Project.load("demo").layer("tx").status == "ready"


def test_a_modality_nobody_can_build_is_pending_with_something_to_do(sample):
    """Not a failed import. A core build with no transcripts plugin can still
    RECORD that a Xenium run has transcripts in it -- refusing the import
    instead would make the plugin a requirement for reading the folder."""
    project = Project.load("demo")
    assert layer_jobs.start_builder(project, project.layer("tx")) is False

    record = layer_jobs.get("demo", "tx")
    assert record["status"] == "pending"
    assert "transcripts plugin" in record["message"]
    # Nothing ran, so the stored status is untouched.
    assert Project.load("demo").layer("tx").status == "pending"


def test_a_failed_build_says_so_and_carries_its_install_line(sample):
    """A dependency this environment has not got is a command to copy, not a
    stack trace shown to somebody who cannot act on one."""
    class Missing(Exception):
        INSTALL = 'pip install "plexora[spatial]"'

    def builder(project, layer, stage, report):
        raise Missing("pyarrow is not importable")

    layer_jobs.register_builder("transcripts", builder)
    project = Project.load("demo")
    layer_jobs.start_builder(project, project.layer("tx"))

    record = _wait("demo", "tx", "failed")
    assert record["install"] == 'pip install "plexora[spatial]"'
    assert "pyarrow" in record["error"]
    assert Project.load("demo").layer("tx").failed is True


def test_a_restarted_server_reports_from_the_record(sample):
    """In-memory records are gone; the project's own `status` is not.

    Which is why `_finish` writes both: the message is for the card on screen
    now, and the status is for the next process to open this project.
    """
    Project.mutate("demo", lambda p: p.with_layer(
        p.layer("tx").__class__(id="tx", kind="points", modality="transcripts",
                                status="failed")))
    layer_jobs.forget()  # the restart

    document = layer_jobs.status("demo")
    assert document["layers"]["tx"]["status"] == "failed"
    assert document["pending"] is False


def test_two_builds_of_one_layer_do_not_race(sample):
    """They would write the same cache directory. The second call gets the
    first's record instead, which is also what makes the panel's build button
    show the importer's progress rather than starting a second one."""
    import threading

    gate = threading.Event()

    def builder(project, layer, stage, report):
        gate.wait(5)

    layer_jobs.register_builder("transcripts", builder)
    project = Project.load("demo")
    first = layer_jobs.start("demo", "tx", lambda p, l, s, r: builder(p, l, s, r))
    second = layer_jobs.start("demo", "tx", lambda p, l, s, r: builder(p, l, s, r))
    assert first["status"] == second["status"] == "pending"
    gate.set()
    _wait("demo", "tx", "ready")


def test_the_status_route_answers_the_same_document(sample):
    client = plexora.app.test_client()
    payload = client.get("/import/status?sample=demo").get_json()
    assert payload == layer_jobs.status("demo")
    assert client.get("/import/status").status_code == 400
