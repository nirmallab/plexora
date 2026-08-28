"""Registering a node, and pointing a project at it, through the app's own routes.

The Python API (`plexora.nodes`) is covered by test_node_table.py; this is the
surface a user reaches without writing any. Both go through a real node process,
because the interesting failures here are the ones where the browser is told
something the server cannot back up -- a node listed as reachable that is not, a
select showing "this machine" for a project that is bound elsewhere.
"""

from __future__ import annotations

import polars as pl
import pytest
import tifffile
import numpy as np

from tests.helpers import ALL_CONFIRMED, csv_spec, project
from tests.node_harness import node_process  # noqa: F401 - fixture


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _table_file(directory):
    pl.DataFrame({
        "CellID": [1, 2, 3],
        "X_centroid": [1.0, 2.0, 3.0],
        "Y_centroid": [1.0, 2.0, 3.0],
        "CD3": [0.5, 1.5, 2.5],
    }).write_csv(directory / "cells.csv")
    return directory / "cells.csv"


def _image_file(directory):
    path = directory / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


def _project(tmp_path, name="demo"):
    path = _table_file(tmp_path)
    record = project(
        name,
        dataset=csv_spec(path, cell_id="CellID", x="X_centroid", y="Y_centroid",
                         markers=("CD3",),
                         metadata=("CellID", "X_centroid", "Y_centroid")),
        confirmed=ALL_CONFIRMED, src=str(_image_file(tmp_path)))
    record.save()
    return record, path


def test_the_settings_page_lists_no_nodes_to_begin_with(client):
    answer = client.get("/settings/nodes").get_json()
    assert answer["nodes"] == []


def test_registering_a_node_checks_that_it_answers(client, tmp_path, node_process):
    node = node_process(f"table:cells={_table_file(tmp_path)}")

    added = client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token,
    }).get_json()
    assert added["node"]["name"] == "hpc"
    # Never the token. A settings page that showed it would put it in the first
    # screenshot anybody sent asking for help.
    assert "token" not in added["node"]
    assert added["node"]["has_token"] is True

    listed = client.get("/settings/nodes").get_json()["nodes"]
    assert [entry["name"] for entry in listed] == ["hpc"]
    assert listed[0]["reachable"] is True
    assert [r["id"] for r in listed[0]["resources"]] == ["cells"]


def test_an_address_that_is_not_a_node_is_refused_at_the_form(client):
    answer = client.post("/settings/nodes", json={
        "name": "nowhere", "endpoint": "http://127.0.0.1:1", "token": "x",
    })
    assert answer.status_code == 400
    # Named, so the user can tell a typo from a node that is not running.
    assert "127.0.0.1:1" in answer.get_json()["error"]
    assert client.get("/settings/nodes").get_json()["nodes"] == []


def test_a_missing_name_is_refused_before_anything_is_contacted(client):
    answer = client.post("/settings/nodes", json={"endpoint": "http://x"})
    assert answer.status_code == 400
    assert "name" in answer.get_json()["error"]


def test_the_edit_page_reports_every_resource_as_local_by_default(client, tmp_path):
    _project(tmp_path)
    answer = client.get("/project/demo/resources").get_json()

    kinds = {entry["kind"]: entry for entry in answer["resources"]}
    assert set(kinds) == {"image", "segmentation", "table"}
    assert kinds["table"]["provider"] == "local"
    assert kinds["table"]["present"] is True
    # No mask on this project, so the section will not draw a row for it.
    assert kinds["segmentation"]["present"] is False


def test_attaching_and_detaching_a_table_through_the_route(client, tmp_path, node_process):
    _record, path = _project(tmp_path)
    node = node_process(f"table:cells={path}")
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})

    attached = client.post("/project/demo/resources/table",
                           json={"node": "hpc", "resource_id": "cells"}).get_json()
    table = next(e for e in attached["resources"] if e["kind"] == "table")
    assert table["provider"] == "node"
    assert table["node"] == "hpc"
    # No path, because there is no file at any path on this machine -- see
    # ResourceLocator.
    assert table["path"] is None

    # Detaching without saying where the file is HERE is refused, and the
    # refusal names the field to use: a project whose table is on a node has no
    # local copy by construction, so "bring it back" without an answer would
    # leave it pointing at nothing.
    refused = client.post("/project/demo/resources/table", json={})
    assert refused.status_code == 400
    assert "Data field" in refused.get_json()["error"]

    detached = client.post("/project/demo/resources/table",
                           json={"path": str(path)}).get_json()
    assert "error" not in detached, detached
    table = next(e for e in detached["resources"] if e["kind"] == "table")
    assert table["provider"] == "local"
    assert table["path"] == str(path)
    # And the project keeps every answer it had recorded about the table.
    from plexora.server.models.project import Project

    assert Project.load("demo").roles.cell_id == "CellID"


def test_attaching_to_a_node_that_is_not_registered_says_which_are(client, tmp_path):
    _project(tmp_path)
    answer = client.post("/project/demo/resources/table",
                         json={"node": "ghost", "resource_id": "cells"})
    assert answer.status_code == 400
    message = answer.get_json()["error"]
    assert "ghost" in message and "known nodes" in message


def test_a_node_that_stops_answering_is_still_shown_as_this_project_s_source(
        client, tmp_path, node_process):
    _record, path = _project(tmp_path)
    node = node_process(f"table:cells={path}")
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})
    client.post("/project/demo/resources/table",
                json={"node": "hpc", "resource_id": "cells"})

    node.stop()
    answer = client.get("/project/demo/resources").get_json()

    table = next(e for e in answer["resources"] if e["kind"] == "table")
    # The binding is a fact about the project and does not evaporate because a
    # laptop closed its lid. A page that reported "this machine" here would be
    # one save away from making that true.
    assert table["provider"] == "node" and table["node"] == "hpc"
    hpc = next(entry for entry in answer["nodes"] if entry["name"] == "hpc")
    assert hpc["reachable"] is False


def test_forgetting_a_node_names_the_projects_that_were_using_it(
        client, tmp_path, node_process):
    _record, path = _project(tmp_path)
    node = node_process(f"table:cells={path}")
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})
    client.post("/project/demo/resources/table",
                json={"node": "hpc", "resource_id": "cells"})

    answer = client.delete("/settings/nodes/hpc").get_json()
    assert answer["projects_affected"] == ["demo"]
    assert client.get("/settings/nodes").get_json()["nodes"] == []


# -- importing a project whose image is not on this machine ---------------


def _big_image(directory):
    """A pyramidal image, the kind that is on a node because it is too large to
    be anywhere else."""
    rng = np.random.default_rng(5)
    data = rng.integers(0, 3000, (2, 1024, 1024), dtype=np.uint16)
    path = directory / "slide.ome.tif"
    tifffile.imwrite(path, data, photometric="minisblack", tile=(512, 512))
    return path


def test_the_import_form_accepts_a_node_address_for_the_image(
        client, tmp_path, node_process):
    """The whole reason an image is on a node is that it is too large to copy,
    so a form that insists on a local path is a form that cannot be used for
    the case data nodes exist for."""
    from plexora.server.models.project import Project

    node = node_process(f"image:slide={_big_image(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "o2", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/import", data={
        "name": "remote-slide",
        "image_file": "node://o2/slide",
    })
    assert answer.status_code == 302, answer.get_data(as_text=True)

    record = Project.load("remote-slide")
    assert record.resource("image").node == "o2"
    assert record.image.width == 1024 and record.image.num_channels == 2
    # The geometry the viewer needs before it can ask for a tile, recorded
    # centrally -- the node is not asked again per request.
    assert record.image.tile_width == 512


def test_a_node_image_and_a_local_table_import_together(
        client, tmp_path, node_process):
    """The flagship split: the slide is on the cluster, the table came back to
    the laptop."""
    from plexora.server.models.project import Project

    node = node_process(f"image:slide={_big_image(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "o2", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/import", data={
        "name": "split",
        "image_file": "node://o2/slide",
        "data_file": str(_table_file(tmp_path)),
    })
    assert answer.status_code == 302, answer.get_data(as_text=True)

    record = Project.load("split")
    assert record.resource("image").node == "o2"
    # The table stayed here, and went through the same inspection every other
    # import uses -- its roles are guessed, not left blank.
    assert record.resource("table") is None
    assert record.dataset.src.endswith("cells.csv")
    assert record.roles.x == "X_centroid" and record.roles.y == "Y_centroid"


def test_a_malformed_node_address_says_what_the_shape_is(client, tmp_path):
    answer = client.post("/import", data={
        "name": "bad", "image_file": "node://onlyanode",
    })
    # 400 and the form back with what was typed, like every other refusal here.
    assert answer.status_code == 400
    assert "node://&lt;node&gt;/&lt;resource&gt;" in answer.get_data(as_text=True)


def test_a_failed_node_import_leaves_no_half_project(client, tmp_path):
    """A half-registered project is worse than none: it appears in the picker,
    opens onto an error, and the name is taken so the user cannot import over
    it."""
    from plexora.server.models.project import Project

    answer = client.post("/import", data={
        "name": "ghosted", "image_file": "node://nosuchnode/slide",
    })
    assert answer.status_code == 400
    assert "nosuchnode" in answer.get_data(as_text=True)
    assert Project.find("ghosted") is None


def test_the_import_page_offers_what_the_nodes_are_serving(
        client, tmp_path, node_process):
    node = node_process(f"image:slide={_big_image(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "o2", "endpoint": node.endpoint, "token": node.token})

    page = client.get("/upload_page").get_data(as_text=True)
    # So nobody has to know the `node://` syntax to use it.
    assert "node://o2/slide" in page
    assert "or an image on a data node" in page


def test_a_local_image_and_a_node_table_import_together(
        client, tmp_path, node_process):
    """The inverse split, and the laptop-share layout's flagship: the viewer
    runs beside the images and the cell table never left the user's own
    machine. The import form takes `node://` in the Data field for it, and the
    node's own inspection stands in for the local one -- roles are guessed,
    not left blank."""
    from plexora.server.models.project import Project

    node = node_process(f"table:cells={_table_file(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/import", data={
        "name": "inverse-split",
        "image_file": str(_image_file(tmp_path)),
        "data_file": "node://laptop/cells",
    })
    assert answer.status_code == 302, answer.get_data(as_text=True)

    record = Project.load("inverse-split")
    binding = record.resource("table")
    assert binding is not None and binding.node == "laptop"
    assert record.dataset.src == "node://laptop/cells"
    assert record.roles.x == "X_centroid" and record.roles.y == "Y_centroid"


def test_a_node_table_attaches_to_a_project_that_never_had_one(
        client, tmp_path, node_process):
    """The Edit page's node picker used to refuse a project with no table spec
    ("import the table locally first") -- which for the laptop-share layout is
    exactly the file that CANNOT be imported locally. The node's inspection
    now proposes the spec instead."""
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import _register_image_only

    node = node_process(f"table:cells={_table_file(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})
    _register_image_only("bare", _image_file(tmp_path), None)

    answer = client.post("/project/bare/resources/table",
                         json={"node": "laptop", "resource_id": "cells"})
    assert answer.status_code == 200, answer.get_data(as_text=True)

    record = Project.load("bare")
    assert record.resource("table").node == "laptop"
    assert record.dataset.src == "node://laptop/cells"


def test_inspect_data_answers_for_a_node_address(
        client, tmp_path, node_process):
    """Typing `node://laptop/cells` into the Data field must not paint the
    field invalid: setCustomValidity on that answer BLOCKS the form, so a
    wrong answer here makes a serveable table unimportable."""
    node = node_process(f"table:cells={_table_file(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/inspect_data",
                         json={"path": "node://laptop/cells"}).get_json()
    assert answer["ok"] is True, answer
    assert answer["data_type"] == "csv"


def test_a_failed_node_table_import_leaves_no_half_project(client, tmp_path):
    from plexora.server.models.project import Project

    answer = client.post("/import", data={
        "name": "ghost-table",
        "image_file": str(_image_file(tmp_path)),
        "data_file": "node://nosuchnode/cells",
    })
    assert answer.status_code == 400
    assert "nosuchnode" in answer.get_data(as_text=True)
    assert Project.find("ghost-table") is None


def test_the_import_page_offers_a_node_s_tables_too(
        client, tmp_path, node_process):
    node = node_process(f"table:cells={_table_file(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})

    page = client.get("/upload_page").get_data(as_text=True)
    assert "node://laptop/cells" in page
    assert "or a table on a data node" in page


# -- one modality at a time ------------------------------------------------
#
# Where each of the three resources lives is an independent fact. The form
# parses all three fields for a node address, and until these tests it only
# ACTED on the mask's when the image was on a node too -- so the commonest
# split of all was unrepresentable.


def _mask_file(directory):
    """A label mask, already a servable pyramid so nothing converts in-test."""
    from plexora.server.utils import segmentation_pyramid

    labels = np.zeros((256, 256), dtype=np.uint32)
    for index in range(1, 4):
        top = index * 40
        labels[top:top + 20, top:top + 20] = index
    flat = directory / "mask.tif"
    tifffile.imwrite(flat, labels)
    return segmentation_pyramid.pyramidize_segmentation_mask(
        flat, directory / "mask_pyramid.ome.tif", overwrite=True, outline=False)


def _multi_image_h5ad(directory):
    """A table spanning three slides, at coordinate ranges far enough apart
    that loading the wrong subset is visible rather than merely plausible."""
    import anndata as ad
    import pandas as pd

    rows, spatial, names = [], [], []
    for offset, image_id in ((0.0, "image_01"), (10_000.0, "image_02"),
                             (20_000.0, "image_03")):
        for index in range(5):
            names.append(f"{image_id}_cell_{index}")
            rows.append(image_id)
            spatial.append([offset + index, offset + index * 2])
    adata = ad.AnnData(
        X=np.random.default_rng(1).random((len(names), 3)).astype(np.float32),
        obs=pd.DataFrame({"image_id": rows}, index=names),
        var=pd.DataFrame(index=[f"protein_{i}" for i in range(3)]))
    adata.obsm["spatial"] = np.asarray(spatial, dtype=np.float64)
    path = directory / "cells.h5ad"
    adata.write_h5ad(path)
    return path


def test_a_local_image_and_a_node_mask_import_together(
        client, tmp_path, node_process):
    """The slide is here; the mask stayed beside the job that wrote it.

    This used to be refused with "Provide a valid path to the segmentation
    mask" -- the field was parsed for a node address and then only consulted
    inside the branch where the IMAGE was on a node.
    """
    from plexora.server.models import data_model
    from plexora.server.models.project import Project

    node = node_process(f"segmentation:mask={_mask_file(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "workstation", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/import", data={
        "name": "masked",
        "image_file": str(_image_file(tmp_path)),
        "label_file": "node://workstation/mask",
    })
    assert answer.status_code == 302, answer.get_data(as_text=True)

    record = Project.load("masked")
    assert record.resource("segmentation").node == "workstation"
    assert record.resource("image") is None, "the image never left this machine"
    assert record.segmentation.derived == "node://workstation/mask"
    # The viewer loads imageData[0] as the label layer whenever a project names
    # a mask. Without the placeholder it would load the first real channel --
    # silently, and with no error to fall back from.
    assert record.image.channels[0]["fullname"] == "Area"

    data_model.load_datasource("masked", reload=True)
    encoded, mimetype = data_model.encode_tile("masked", "mask", 0, "0_0", "webp")
    assert mimetype == "image/png" and encoded[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_node_table_is_subset_to_the_chosen_image(
        client, tmp_path, node_process):
    """The one image's worth of rows the user asked for, not all twelve.

    `subset_column`/`subset_value` are on the import form for every table and
    were dropped on the way to a node -- so a file spanning several slides
    loaded all of them, and every coordinate landed somewhere plausible and
    wrong.
    """
    from plexora.server.models import data_model
    from plexora.server.models.project import Project

    node = node_process(f"table:cells={_multi_image_h5ad(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/import", data={
        "name": "one-slide",
        "image_file": str(_image_file(tmp_path)),
        "data_file": "node://laptop/cells",
        "subset_column": "image_id",
        "subset_value": "image_02",
    })
    assert answer.status_code == 302, answer.get_data(as_text=True)

    record = Project.load("one-slide")
    assert dict(record.dataset.subset) == {"column": "image_id",
                                           "value": "image_02"}

    data_model.load_datasource("one-slide", reload=True)
    frame = data_model.get_datasource_df()
    assert frame.height == 5
    # The middle slide's coordinate range, so reading the wrong rows cannot
    # pass as an off-by-one.
    assert 10_000.0 <= frame["X"].min() and frame["X"].max() < 20_000.0


# -- changing a source later ----------------------------------------------


def test_the_edit_page_takes_a_node_address_for_the_mask(
        client, tmp_path, node_process):
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import _register_image_only
    from plexora.server.routes.project_routes import _describe

    node = node_process(f"segmentation:mask={_mask_file(tmp_path)}")
    client.post("/settings/nodes", json={
        "name": "workstation", "endpoint": node.endpoint, "token": node.token})
    _register_image_only("bare", _image_file(tmp_path), None)

    answer = client.post("/project/bare",
                         json={"segmentation": "node://workstation/mask"})
    assert answer.status_code == 200, answer.get_json()
    assert Project.load("bare").resource("segmentation").node == "workstation"

    # The field shows the node address, because that is what it posts back --
    # and saving the page with an unrelated change must not read as "the user
    # cleared the mask", which is what showing a blank local path would mean.
    described = _describe(Project.load("bare"))
    assert described["segmentation"]["src"] == "node://workstation/mask"
    again = client.post("/project/bare",
                        json={"segmentation": described["segmentation"]["src"]})
    assert again.status_code == 200, again.get_json()
    assert Project.load("bare").resource("segmentation") is not None

    # Clearing it really clears it: the binding goes with the mask, or the
    # project keeps reading from a machine its own record no longer names.
    cleared = client.post("/project/bare", json={"segmentation": ""})
    assert cleared.status_code == 200, cleared.get_json()
    record = Project.load("bare")
    assert record.resource("segmentation") is None
    assert record.segmentation.derived is None
    assert all(c["fullname"] != "Area" for c in record.image.channels)


def test_the_edit_page_takes_a_node_address_for_the_data_file(
        client, tmp_path, node_process):
    from plexora.server.models.project import Project

    _record, path = _project(tmp_path)
    node = node_process(f"table:cells={path}")
    client.post("/settings/nodes", json={
        "name": "laptop", "endpoint": node.endpoint, "token": node.token})

    answer = client.post("/project/demo", json={"data": "node://laptop/cells"})
    assert answer.status_code == 200, answer.get_json()
    record = Project.load("demo")
    assert record.resource("table").node == "laptop"
    assert record.dataset.src == "node://laptop/cells"

    # And back. A local file drops the binding, or the project would keep
    # reading through a node while its recorded source says otherwise.
    back = client.post("/project/demo", json={"data": str(path)})
    assert back.status_code == 200, back.get_json()
    record = Project.load("demo")
    assert record.resource("table") is None
    assert record.dataset.src.endswith("cells.csv")
