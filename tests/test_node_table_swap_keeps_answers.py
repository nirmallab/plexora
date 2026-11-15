"""A node table swap that fails leaves the current table's answers alone
(audit sc-import-8).

`replace_project_data` used to forget the table's answers (roles, marker split,
confirmations) in a saved write BEFORE asking the node to load the new file. A
node that refused or could not be reached left the old table in force with
every answer about it gone, so the requirements modal asked them all again.
"""

import numpy as np
import pytest
import tifffile

from plexora.server.models.project import ImageSpec, Project
from plexora.server.routes import import_routes
from tests.helpers import use_data_root


@pytest.fixture
def answered(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    image = tmp_path / "image.ome.tif"
    tifffile.imwrite(image, np.zeros((1, 64, 64), dtype=np.uint8))
    Project(
        name="proj",
        image=ImageSpec(src=str(image), width=64, height=64, max_level=1,
                        tile_width=64, tile_height=64,
                        channels=({"name": "c0", "fullname": "DNA", "src": "/t/0"},)),
    ).with_confirmed(["markers", "role:cell_id"]).save()
    return Project.load("proj")


def test_a_failed_node_swap_keeps_the_answers(answered, monkeypatch):
    from plexora import nodes as node_api

    def refuse(*args, **kwargs):
        raise ConnectionError("node unreachable")

    monkeypatch.setattr(node_api, "attach_table", refuse)

    with pytest.raises(ConnectionError):
        import_routes.replace_project_data("proj", "node://hpc/cells")

    assert set(Project.load("proj").confirmed) >= {"markers", "role:cell_id"}


def test_a_node_swap_hands_the_node_a_project_without_the_answers(answered, monkeypatch):
    from plexora import nodes as node_api

    seen = {}

    def attach(project, **kwargs):
        seen["project"] = project if isinstance(project, Project) else Project.load(project)
        return seen["project"]

    monkeypatch.setattr(node_api, "attach_table", attach)

    import_routes.replace_project_data("proj", "node://hpc/cells")

    assert "markers" not in seen["project"].confirmed
    assert "role:cell_id" not in seen["project"].confirmed
