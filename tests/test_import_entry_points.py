"""One engine under every way into Plexora.

Plexora registers data from a lot of places: the import dialog, the edit page,
the requirements modal, the Cells control, the Python API, the CLI, a data node.
The risk in adding a new one is not that it fails -- it is that it SUCCEEDS
differently, and a project imported through the dialog ends up shaped unlike one
imported through `create_project`, so half the app works on each.

So these tests are not about any one route. They take the same files in through
different doors and assert the records come out the same, and that the same
per-resource writers ran. If somebody later gives `/import/sample` its own
registration path, these fail -- which is the only way to keep "one engine" a
fact rather than a claim in a docstring.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import import_sample as importer
from plexora.server.models.project import Project

from tests.helpers import use_data_root

#: Keys that are a fact about WHEN, not about what was imported. Two records of
#: the same data differ here and nowhere else.
TIMESTAMPS = ("createdAt", "lastOpenedAt")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    rng = np.random.default_rng(0)
    tifffile.imwrite(source / "slide.ome.tif",
                     rng.integers(0, 3000, (2, 128, 128)).astype(np.uint16))
    tifffile.imwrite(source / "slide_mask.tif",
                     (np.arange(128 * 128).reshape(128, 128) % 90).astype(np.uint32))
    (source / "cells.csv").write_text(
        "CellID,X_centroid,Y_centroid,DNA\n1,10,10,5\n2,20,20,7\n",
        encoding="utf-8")
    return source


def comparable(entry):
    """A project record with the facts that are about time taken out.

    Paths are kept: two imports of the same files must point at the same files,
    and a copy made in one and not the other is exactly the divergence this
    file exists to catch.
    """
    return {key: value for key, value in entry.items() if key not in TIMESTAMPS}


def test_the_dialog_and_the_python_api_register_the_same_sample(workspace):
    """`/import/sample` and `plexora.import_sample` are the same call.

    The route is a thin translation of the request into arguments -- if it ever
    grows a registration of its own, this is what says so.
    """
    client = plexora.app.test_client()
    response = client.post("/import/sample", json={
        "paths": [str(workspace / "slide.ome.tif")],
        "name": "viaroute",
    })
    assert response.status_code == 200, response.data

    plexora.import_sample(workspace / "slide.ome.tif", name="viaapi")

    through_route = comparable(Project.load("viaroute").to_entry())
    through_api = comparable(Project.load("viaapi").to_entry())
    # The project directory is in the channel addresses, so those differ by
    # name and by nothing else.
    through_api = json.loads(json.dumps(through_api).replace("viaapi", "viaroute"))
    assert through_route == through_api


def _settled(name, timeout=60.0):
    """The project once its mask conversion is no longer pending."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        project = Project.load(name)
        if project.segmentation.status != "pending" or time.monotonic() > deadline:
            return project
        time.sleep(0.1)


def test_a_mask_lands_the_same_wherever_it_was_attached(workspace):
    """The Cells control, the edit page and "+ Add Layer" all attach masks.

    They must reach `attach_segmentation` -- the one function that fingerprints
    the source, inserts the "Area" placeholder into `imageData` and starts the
    pyramid job. Three copies of that would be three ways for the mask to end
    up at a different channel index.
    """
    from plexora.server.routes.import_routes import attach_segmentation

    plexora.import_sample(workspace / "slide.ome.tif", name="direct")
    attach_segmentation("direct", str(workspace / "slide_mask.tif"))

    plexora.import_sample(workspace / "slide.ome.tif", name="viaimport")
    importer.add_layers("viaimport", [str(workspace / "slide_mask.tif")])

    # Each attach starts the mask's conversion in the background, so a status
    # read straight away is a snapshot of two jobs at different points -- the
    # first one routinely finishes while the second project is being made.
    # What must match is where they end up.
    direct = _settled("direct")
    through = _settled("viaimport")
    assert direct.segmentation.source == through.segmentation.source
    assert direct.segmentation.status == through.segmentation.status
    # The mask's tiles are `imageData[0]` -- the "Area" placeholder -- and every
    # channel index in the app is counted from that.
    assert [c["fullname"] for c in direct.image.channels] == \
           [c["fullname"] for c in through.image.channels]
    assert direct.image.channels[0]["fullname"] == "Area"


def test_adding_a_mask_is_the_one_case_that_asks_for_a_reload(workspace):
    """Because it shifts every channel index, which is wired into the GL pass.

    Everything else is adopted in place, which is what makes "+ Add Layer" not
    cost the user their pan, zoom, gates and open tools.
    """
    plexora.import_sample(workspace / "slide.ome.tif", name="sample")
    with_mask = importer.add_layers("sample", [str(workspace / "slide_mask.tif")])
    assert with_mask["reload"] is True


def test_the_table_goes_through_the_same_writer_as_the_edit_page(workspace):
    from plexora.server.routes.import_routes import replace_project_data

    plexora.import_sample(workspace / "slide.ome.tif", name="edited")
    replace_project_data("edited", str(workspace / "cells.csv"))

    plexora.import_sample(workspace / "slide.ome.tif",
                          workspace / "cells.csv", name="imported")

    edited = Project.load("edited")
    imported = Project.load("imported")
    assert edited.dataset.type == imported.dataset.type == "csv"
    assert edited.roles.x == imported.roles.x
    assert edited.roles.cell_id == imported.roles.cell_id
    # Both copied the CSV into their own project directory, which is what
    # `replace_project_data` does and what makes a moved source file harmless.
    assert Path(edited.dataset.src).parent.name == "edited"
    assert Path(imported.dataset.src).parent.name == "imported"


def test_the_cli_takes_the_detection_route_only_when_the_paths_ask(workspace,
                                                                  tmp_path):
    """`--data` is a caller SAYING what a file is. Re-detecting it would be
    second-guessing somebody who already answered."""
    from types import SimpleNamespace

    from plexora.cli import _wants_detection

    named = SimpleNamespace(segmentation=None, data="cells.csv", spec_file=None)
    assert _wants_detection([str(workspace / "slide.ome.tif")], named) is False

    bare = SimpleNamespace(segmentation=None, data=None, spec_file=None)
    assert _wants_detection([str(workspace / "slide.ome.tif")], bare) is False
    assert _wants_detection([str(workspace / "slide.ome.tif"),
                             str(workspace / "cells.csv")], bare) is True

    run = tmp_path / "run"
    run.mkdir()
    (run / "morphology.ome.tif").write_bytes(b"")
    (run / "transcripts.parquet").write_bytes(b"")
    assert _wants_detection([str(run)], bare) is True


def test_inspect_and_inspect_data_agree_about_a_csv(workspace):
    """The edit page and the requirements modal still ask `/inspect_data`.

    Both surfaces have to reach the same conclusion about the same file, or a
    table that imports cleanly through the dialog reports as unreadable on the
    page that would fix it.
    """
    client = plexora.app.test_client()
    old = client.post("/inspect_data",
                      json={"path": str(workspace / "cells.csv")}).get_json()
    new = client.post("/import/inspect",
                      json={"paths": [str(workspace / "cells.csv")]}).get_json()
    assert old["ok"] is True and old["data_type"] == "csv"
    layer = new["samples"][0]["layers"][0]
    assert layer["role"] == "table"
    assert layer["render"]["detail"] == "csv"


def test_registering_the_same_image_twice_offers_the_existing_sample(workspace):
    """The quick-view rule, kept. A second copy of one slide is almost never
    what somebody meant, and the two would then diverge."""
    plexora.import_sample(workspace / "slide.ome.tif", name="first")
    client = plexora.app.test_client()
    proposal = client.post("/import/inspect", json={
        "paths": [str(workspace / "slide.ome.tif")]}).get_json()
    assert proposal["samples"][0]["existing"] == "first"
