"""A projection pinned as a layer: exact at full resolution, served as any layer is.

The claim worth testing is the word "exact": every pixel of the pinned
layer's level 0 is the operator over the stack's own level-0 planes, which
the viewer's tile-by-tile projection is not at coarse levels. Then that it is
an ordinary layer (served by the layer route, removable) and that what cannot
be pinned is refused with a reason.
"""

import io
import json
import time

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import layer_jobs
from plexora.server.models.project import Project
from tests import volume_fixtures as vf


@pytest.fixture()
def client():
    return plexora.app.test_client()


def _register(tmp_path, name="stack", **kwargs):
    kwargs.setdefault("channels", 2)
    kwargs.setdefault("depth", 4)
    # Wider than one 1024 tile, so the pyramid has a level to reduce into.
    kwargs.setdefault("height", 1100)
    kwargs.setdefault("width", 1030)
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX",
                                     names=["DNA", "CD8"][:kwargs["channels"]], **kwargs)
    register_image_datasource(name, path)
    return data[0]  # (C, Z, Y, X)


def _settle(name, layer_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = layer_jobs.get(name, layer_id) or {}
        if job.get("status") in ("ready", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"{layer_id} did not finish")


def _hd(client, src, level=0, tile="0_0"):
    from PIL import Image

    response = client.get(f"{src}{level}/{tile}.png?q=hd")
    assert response.status_code == 200, response.get_data(as_text=True)
    return np.asarray(Image.open(io.BytesIO(response.data)))


def _pin(client, name, view, **extra):
    return client.post(f"/volume/{name}/pin", json={"view": view, **extra})


def test_a_pinned_mip_is_exact_at_full_resolution(tmp_path, client):
    data = _register(tmp_path)
    response = _pin(client, "stack", "z0-3max")
    assert response.status_code == 202
    assert response.get_json() == {"layer": "proj-z0-3max", "status": "pending",
                                   "created": True}
    assert _settle("stack", "proj-z0-3max")["status"] == "ready", \
        layer_jobs.get("stack", "proj-z0-3max")

    layer = Project.find("stack").layer("proj-z0-3max")
    assert layer.modality == "projection" and layer.label == "MIP z 0–3"
    assert [c["name"] for c in layer.channels] == ["DNA", "CD8"]
    expected = data.max(axis=1)
    for index, channel in enumerate(layer.channels):
        assert channel["src"].endswith(f"/projection_{index}/")
        tile = _hd(client, channel["src"])
        np.testing.assert_array_equal(tile, expected[index][:1024, :1024])
        corner = _hd(client, channel["src"], tile="1_1")
        np.testing.assert_array_equal(corner, expected[index][1024:, 1024:])
    # The pyramid goes on below level 0, at the size the layer record says.
    assert layer.max_level == 2
    coarse = _hd(client, layer.channels[0]["src"], level=1)
    assert coarse.shape == (550, 515)


def test_the_file_says_how_it_was_made(tmp_path, client):
    _register(tmp_path)
    _pin(client, "stack", "z1-2mean")
    _settle("stack", "proj-z1-2mean")
    layer = Project.find("stack").layer("proj-z1-2mean")
    with tifffile.TiffFile(layer.src) as handle:
        assert len(handle.series[0].levels) == 2
        xml = handle.ome_metadata
    assert 'Name="plexora-projection"' in xml
    import re
    from html import unescape

    description = json.loads(unescape(re.search(r"<Description>(.*?)</Description>",
                                                xml, re.S).group(1)))
    assert description["view"] == "z1-2mean"
    assert (description["op"], description["z0"], description["z1"]) == ("mean", 1, 2)
    assert description["exact"] is True and description["level"] == 0
    assert description["rounded"]


def test_a_mean_and_a_percentile_are_exact_too(tmp_path, client):
    data = _register(tmp_path, height=96, width=80)
    for view in ("z0-3mean", "z0-3p75"):
        _pin(client, "stack", view)
        assert _settle("stack", f"proj-{view}")["status"] == "ready"
    layer = Project.find("stack").layer("proj-z0-3mean")
    mean = _hd(client, layer.channels[1]["src"])
    np.testing.assert_array_equal(mean, np.rint(data[1].astype(np.float64).mean(axis=0)))
    layer = Project.find("stack").layer("proj-z0-3p75")
    pct = _hd(client, layer.channels[0]["src"])
    np.testing.assert_array_equal(pct, np.percentile(data[0], 75, axis=0, method="lower"))


@pytest.mark.parametrize("view, words", [
    ("z2", "only a projection"),
    ("xzy10", "only a projection"),
    ("z0-3sum", "overflows"),
    ("z0-9max", "outside this stack"),
    ("nonsense", ""),
])
def test_what_cannot_be_pinned_is_refused(tmp_path, client, view, words):
    _register(tmp_path, height=96, width=80)
    response = _pin(client, "stack", view)
    assert response.status_code == 400
    assert words in response.get_json()["error"]
    assert Project.find("stack").layer(f"proj-{view}") is None


def test_a_flat_image_has_nothing_to_pin(tmp_path, client):
    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 32, 32), dtype=np.uint16), ome=True)
    register_image_datasource("flat", path)
    response = _pin(client, "flat", "z0-1max")
    assert response.status_code == 400 and "not a z-stack" in response.get_json()["error"]


def test_pinning_twice_keeps_the_first_and_removing_deletes_the_file(tmp_path, client):
    from pathlib import Path

    _register(tmp_path, height=96, width=80)
    _pin(client, "stack", "z0-3max")
    _settle("stack", "proj-z0-3max")
    again = _pin(client, "stack", "z0-3max")
    assert again.status_code == 200 and again.get_json()["created"] is False
    path = Path(Project.find("stack").layer("proj-z0-3max").src)
    assert path.exists()
    assert client.delete("/project/stack/layers/proj-z0-3max").status_code == 200
    assert not path.exists()
    assert Project.find("stack").layer("proj-z0-3max") is None


def test_removing_a_layer_the_user_added_never_deletes_its_file(tmp_path, client):
    """The projection rule is the exception, keyed on modality AND folder."""
    from plexora import datasets

    _register(tmp_path, height=96, width=80)
    picture = tmp_path / "other.tif"
    # Many distinct values: an image, not something that could be a mask.
    rng = np.random.default_rng(0)
    tifffile.imwrite(picture, rng.integers(0, 60000, (2, 96, 80), dtype=np.uint16))
    datasets.add_layers("stack", picture, wait=True)
    layer = next(l for l in Project.find("stack").spatial_layers if l.src == str(picture))
    assert client.delete(f"/project/stack/layers/{layer.id}").status_code == 200
    assert picture.exists()


def test_the_agent_pins_through_a_job(tmp_path):
    from plexora.agent import AgentSession, invoke, registry

    registry.discover([])
    data = _register(tmp_path, height=96, width=80)
    session = AgentSession()
    started = invoke(session, "pin_projection", {"project": "stack", "view": "z1-3max"})
    assert started["ok"], started
    done = invoke(session, "job_wait", {"job_id": started["result"]["job_id"],
                                        "timeout_s": 60})
    job = done["result"]["job"]
    assert job["status"] == "done", job.get("error")
    result = job["result"]
    assert result["layer"] == "proj-z1-3max" and result["exact"] is True
    assert result["channels"] == ["DNA", "CD8"]
    stored = tifffile.imread(result["path"])
    np.testing.assert_array_equal(stored, data[:, 1:4].max(axis=1))

    refused = invoke(session, "pin_projection", {"project": "stack", "view": "z1"})
    if refused["ok"]:
        refused = invoke(session, "job_wait", {"job_id": refused["result"]["job_id"],
                                               "timeout_s": 30})
    assert "invalid_input" in json.dumps(refused)
