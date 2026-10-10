"""The agent's volume tools: what a z-stack is, which planes to look at, and pictures that say which.

Driven through `invoke`, the way an MCP call or the harness reaches them, over
volumes registered the real way (register_image_datasource). None of it may
load the project into the viewer's slot (tests/test_agent_architecture.py
checks that in general; the last test here checks it for these tools).
"""

import io

import numpy as np
import pytest
from PIL import Image

from plexora.agent import AgentSession, invoke, registry
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from tests import volume_fixtures as vf


@pytest.fixture(autouse=True)
def _discovered():
    registry.discover([])
    yield


def _volume(tmp_path, name="vol", **kwargs):
    kwargs.setdefault("axes", "CZYX")
    kwargs.setdefault("levels", 2)
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", channels=3, depth=8,
                                     height=128, width=112, names=["DNA", "CD3", "CD8"],
                                     **kwargs)
    register_image_datasource(name, path)
    return data


def _ok(result):
    assert result["ok"], result
    return result["result"] if "result" in result else result


def test_inspect_volume_describes_the_stack(tmp_path):
    _volume(tmp_path)
    out = _ok(invoke(AgentSession(), "inspect_volume", {"project": "vol"}))
    assert out["volumetric"] is True
    assert out["depth"] == 8 and out["channels"] == 3
    assert out["spacing"]["z"]["value"] == pytest.approx(2.0)
    assert out["anisotropy"] == pytest.approx(4.0)
    assert out["default_view"] == "z4"
    assert out["warnings"] == []
    assert any(view["view"] == "z0-7max" for view in out["suggested_views"])


def test_inspect_volume_on_a_flat_image_says_so(tmp_path):
    import tifffile

    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 64, 64), dtype=np.uint16), ome=True)
    register_image_datasource("flat", path)
    out = _ok(invoke(AgentSession(), "inspect_volume", {"project": "flat"}))
    assert out["volumetric"] is False


def test_a_missing_z_step_is_a_warning_and_can_be_set(tmp_path):
    path, _ = vf.write_ome_volume(tmp_path / "noz.ome.tif", axes="ZYX", depth=6,
                                  physical_z=None)
    register_image_datasource("noz", path)
    session = AgentSession()
    out = _ok(invoke(session, "inspect_volume", {"project": "noz"}))
    assert any("No z step" in warning for warning in out["warnings"])
    _ok(invoke(session, "set_z_spacing", {"project": "noz", "microns_per_plane": 1.5}))
    again = _ok(invoke(session, "inspect_volume", {"project": "noz"}))
    assert again["spacing"]["z"] == {"value": 1.5, "unit": "µm", "source": "manual"}
    assert again["warnings"] == []


def test_a_stated_z_step_is_not_overridden(tmp_path):
    _volume(tmp_path)
    result = invoke(AgentSession(), "set_z_spacing", {"project": "vol", "microns_per_plane": 3})
    assert not result["ok"] and result["error"]["code"] == "conflict"


def test_sample_z_planes_picks_spread_out_planes_with_reasons(tmp_path):
    _volume(tmp_path)
    out = _ok(invoke(AgentSession(), "sample_z_planes",
                     {"project": "vol", "channel": "DNA", "strategy": "uniform", "n": 3}))
    assert [plane["z"] for plane in out["planes"]] == [0, 4, 7]
    assert all(plane["view"] == f"z{plane['z']}" for plane in out["planes"])
    assert len(out["profile"]) == 8
    edges = _ok(invoke(AgentSession(), "sample_z_planes",
                       {"project": "vol", "strategy": "edges", "n": 3}))
    assert [plane["z"] for plane in edges["planes"]] == [0, 4, 7]


def test_intensity_sampling_finds_the_brightest_plane(tmp_path):
    _volume(tmp_path)
    out = _ok(invoke(AgentSession(), "sample_z_planes",
                     {"project": "vol", "channel": "CD8", "strategy": "intensity", "n": 1}))
    # Plane value rises with z in the fixture (tens digit), so the last is brightest.
    assert out["planes"][0]["z"] == 7


def test_axis_profile_reports_every_plane(tmp_path):
    _volume(tmp_path)
    out = _ok(invoke(AgentSession(), "axis_profile", {"project": "vol", "channel": "CD3"}))
    assert out["z"] == list(range(8))
    assert len(out["median"]) == 8
    assert out["median"][-1] > out["median"][0]


def test_render_region_draws_the_plane_it_is_asked_for(tmp_path):
    _volume(tmp_path)
    session = AgentSession()
    base = {"project": "vol", "channels": [{"name": "CD8", "color": "#ffffff",
                                            "window": [2000, 3100]}],
            "segmentation": "none", "scale_bar": False, "output": {"width": 112}}
    low = _ok(invoke(session, "render_region", dict(base, view="z0")))
    high = _ok(invoke(session, "render_region", dict(base, view="z7")))
    assert low["manifest"]["view"] == "z0" and high["manifest"]["view"] == "z7"
    assert low["manifest"]["view_source"] == "explicit"
    assert low["manifest"]["z_label"] == "z 0 / 8"
    default = _ok(invoke(session, "render_region", base))
    assert default["manifest"]["view"] == "z4"
    assert default["manifest"]["view_source"] == "default"


def test_render_projection_is_labelled_as_one(tmp_path):
    _volume(tmp_path)
    out = _ok(invoke(AgentSession(), "render_projection", {
        "project": "vol", "view": "z0-7max", "segmentation": "none",
        "channels": [{"name": "DNA", "color": "#ffffff", "window": [1000, 1100]}]}))
    manifest = out["manifest"]
    assert manifest["view"] == "z0-7max"
    assert manifest["z_label"].startswith("MIP z 0–7")
    assert any("not colocalization" in warning for warning in manifest["warnings"])
    refused = invoke(AgentSession(), "render_projection",
                     {"project": "vol", "view": "z3"})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"


def test_a_view_on_a_flat_image_is_refused(tmp_path):
    import tifffile

    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 64, 64), dtype=np.uint16), ome=True)
    register_image_datasource("flat", path)
    result = invoke(AgentSession(), "render_region", {"project": "flat", "view": "z1"})
    assert not result["ok"] and result["error"]["code"] == "invalid_input"


def test_a_planar_render_manifest_has_no_view_fields(tmp_path):
    import tifffile

    path = tmp_path / "flat.ome.tif"
    tifffile.imwrite(path, np.arange(2 * 64 * 64, dtype=np.uint16).reshape(2, 64, 64),
                     ome=True)
    register_image_datasource("flat", path)
    out = _ok(invoke(AgentSession(), "render_region", {"project": "flat",
                                                        "segmentation": "none"}))
    for key in ("view", "view_source", "z_label", "representation"):
        assert key not in out["manifest"]


def test_the_volume_tools_never_load_the_viewers_project(tmp_path):
    _volume(tmp_path)
    data_model._loaded_source = None
    session = AgentSession()
    for name, args in [("inspect_volume", {"project": "vol"}),
                       ("sample_z_planes", {"project": "vol", "n": 2}),
                       ("axis_profile", {"project": "vol"})]:
        _ok(invoke(session, name, args))
    assert data_model._loaded_source is None
