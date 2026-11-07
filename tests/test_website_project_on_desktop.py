"""A project made on the website opens in the desktop app, unasked.

End to end through Plexora's own loading: a project registered the way the
website registers one -- in the website's folder of projects, its image named
as a web page knows it (`/picked/<id>/<name>`) with the file's identity --
is opened by the desktop's code path. The image is found on this computer,
the project loads, and the website's own record of it is left as it was.
"""

import json

import numpy as np
import pytest
import tifffile

from plexora import paths
from plexora.server.models import sources
from plexora.server.utils import relink

WEB_PATH = "/picked/ab12/slide.ome.tif"


def _pyramid(path):
    data = (np.arange(2 * 256 * 320) % 4000).astype(np.uint16).reshape(2, 256, 320)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, tile=(128, 128), subifds=1, photometric="minisblack", metadata={"axes": "CYX"})
        writer.write(data[:, ::2, ::2], tile=(128, 128), subfiletype=1, photometric="minisblack")
    return path


@pytest.fixture
def website_project(tmp_path, monkeypatch):
    own = tmp_path / "own"
    library = tmp_path / "Documents" / "Plexora" / "projects"
    own.mkdir()
    library.mkdir(parents=True)
    image = _pyramid(tmp_path / "Downloads" / "run7" / "slide.ome.tif")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(own))
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(library))
    paths.reset()
    import plexora

    plexora.register_image_datasource("web_slide", str(image), data_dir=str(library))
    # As the website writes it: its own name for the file, and the file's identity.
    config_file = library / "config.json"
    text = config_file.read_text(encoding="utf-8").replace(json.dumps(str(image))[1:-1], WEB_PATH)
    config = json.loads(text)
    config["web_slide"]["sources"] = {WEB_PATH: {**sources.file_identity(image), "at": {"website": WEB_PATH}}}
    config_file.write_text(json.dumps(config), encoding="utf-8")
    assert str(image) not in config_file.read_text(encoding="utf-8")
    monkeypatch.setattr(sources, "app_key", lambda: "host:TEST")
    monkeypatch.setattr(relink, "_index", lambda name: [])
    monkeypatch.setattr(relink, "_user_folders", lambda: [tmp_path / "Downloads"])
    yield image, config_file
    paths.reset()


def test_it_opens_with_its_image_found(website_project):
    image, config_file = website_project
    from plexora.server.models import data_model

    data_model.load_datasource("web_slide", reload=True)
    assert data_model.config["web_slide"]["channelFile"] == str(image)
    level0 = data_model.channels["0"] if hasattr(data_model.channels, "array_keys") else data_model.channels
    assert tuple(level0.shape) == (2, 256, 320)
    stored = json.loads(config_file.read_text(encoding="utf-8"))["web_slide"]
    assert stored["channelFile"] == WEB_PATH                                  # still the website's
    assert stored["sources"][WEB_PATH]["at"]["host:TEST"] == str(image)       # and now this computer's


def test_listed_as_the_users_own(website_project):
    from plexora.server.models.project import Project

    project = Project.load("web_slide")
    assert project is not None and not project.is_shared
