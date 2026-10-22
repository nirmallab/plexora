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

#: Plexora 3D's analysis paths (plexora/licensing/manifest.py `volume`): these
#: tests exercise them, so they run on a Pro test licence.
pytestmark = pytest.mark.pro


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


# -- 3-D objects -----------------------------------------------------------------


def _volume_with_mask(tmp_path, name="obj"):
    import tifffile

    from plexora import datasets

    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                     depth=6, height=64, width=64, names=["DNA", "CD8"])
    labels = np.zeros((6, 64, 64), dtype=np.uint16)
    labels[1:4, 10:20, 10:20] = 5
    labels[3:6, 30:40, 30:46] = 9
    tifffile.imwrite(tmp_path / "mask.tif", labels)
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / "mask.tif", wait=True)
    return data, labels


def test_measure_objects_writes_and_attaches_an_object_table(tmp_path):
    import polars as pl

    from plexora.server.models.project import Project

    data, labels = _volume_with_mask(tmp_path)
    session = AgentSession()
    started = _ok(invoke(session, "measure_objects", {"project": "obj"}))
    done = _ok(invoke(session, "job_wait", {"job_id": started["job_id"], "timeout_s": 60}))
    job = done["job"]
    assert job["status"] == "done", job.get("error")
    result = job["result"]
    assert result["objects"] == 2 and result["attached"] is True
    frame = pl.read_parquet(result["table"])
    assert frame["label"].to_list() == [5, 9]
    assert frame["volume_vox"].to_list() == [3 * 10 * 10, 3 * 10 * 16]
    expected = data[0, 1][labels == 9].mean()
    assert frame.filter(pl.col("label") == 9)["CD8"][0] == pytest.approx(expected)
    project = Project.find("obj")
    assert project.dataset.roles.z == "Z"
    assert project.dataset.roles.cell_id == "label"


def test_measure_objects_refuses_a_2d_mask(tmp_path):
    import tifffile

    from plexora import datasets

    path, _ = vf.write_ome_volume(tmp_path / "flatmask.ome.tif", axes="CZYX", channels=2,
                                  depth=4, height=64, width=64)
    tifffile.imwrite(tmp_path / "mask2d.tif", np.ones((64, 64), dtype=np.uint16))
    register_image_datasource("flatmask", path)
    datasets.add_layers("flatmask", tmp_path / "mask2d.tif", wait=True)
    session = AgentSession()
    started = _ok(invoke(session, "measure_objects", {"project": "flatmask"}))
    done = invoke(session, "job_wait", {"job_id": started["job_id"], "timeout_s": 60})
    text = str(done)
    assert "inference_forbidden" in text or "2-D" in text


def test_check_volume_quality_reports_per_channel(tmp_path):
    _volume(tmp_path)
    out = _ok(invoke(AgentSession(), "check_volume_quality",
                     {"project": "vol", "channels": ["DNA"]}))
    assert out["depth"] == 8
    assert set(out["channels"]) == {"DNA"}
    assert len(out["channels"]["DNA"]["focus"]) == 8
    assert isinstance(out["findings"], list)
    assert out["params"]["focus_deficit"] > 0


def test_render_sections_draws_three_panes_through_a_point(tmp_path):
    data = _volume(tmp_path)
    out = _ok(invoke(AgentSession(), "render_sections", {
        "project": "vol", "x": 56, "y": 64, "z": 3, "size_px": 64, "output_px": 128,
        "channels": [{"name": "CD8", "color": "#ffffff", "window": [3000, 3100]}]}))
    manifest = out["manifest"]
    assert manifest["center"] == {"x": 56, "y": 64, "z": 3, "t": 0}
    panes = manifest["panes"]
    assert panes["xy"]["view"] == "z3" and panes["xz"]["view"] == "xzy64"
    assert panes["yz"]["view"] == "yzx56"
    # 8 planes 2.0 um apart over 0.5 um pixels: 32 rows, at 2 output px per pixel.
    assert panes["xz"]["pixels"][3] - panes["xz"]["pixels"][1] == 64
    assert out["artifact"]["id"]


def test_render_object_views_finds_a_cell_and_outlines_it(tmp_path):
    data, labels = _volume_with_mask(tmp_path)
    session = AgentSession()
    started = _ok(invoke(session, "measure_objects", {"project": "obj"}))
    done = _ok(invoke(session, "job_wait", {"job_id": started["job_id"], "timeout_s": 60}))
    assert done["job"]["status"] == "done", done["job"].get("error")

    out = _ok(invoke(session, "render_object_views", {
        "project": "obj", "object_id": 9, "output_px": 192,
        "channels": [{"name": "DNA", "color": "#ffffff", "window": [0, 4000]}]}))
    manifest = out["manifest"]
    obj = manifest["object"]
    assert obj["id"] == 9 and obj["volume_vox"] == 3 * 10 * 16
    assert obj["n_planes"] == 3 and obj["touches_z_bounds"] in (True, 1)
    # Centred on the cell, at its centre plane, outlines drawn with it in yellow.
    assert manifest["center"]["z"] == round(obj["Z"])
    assert manifest["outlines"]["highlight"] == 9
    pixels = np.asarray(Image.open(io.BytesIO(out["_images"][0])).convert("RGB"))
    assert ((pixels == (255, 214, 10)).all(axis=-1)).any()

    missing = invoke(session, "render_object_views", {"project": "obj", "object_id": 77})
    assert not missing["ok"] and missing["error"]["code"] == "invalid_input"


def test_render_sections_outlines_need_a_3d_mask(tmp_path):
    _volume(tmp_path)
    refused = invoke(AgentSession(), "render_sections", {
        "project": "vol", "segmentation": "outlines", "size_px": 64})
    assert not refused["ok"] and refused["error"]["code"] == "precondition_missing"


def test_object_z_profile_says_where_in_depth_a_cells_signal_sits(tmp_path):
    """CD8 in cell 5 rises to its last plane and carries on past it."""
    import tifffile

    from plexora import datasets

    image = np.full((2, 6, 64, 64), 10, dtype=np.uint16)
    image[0] = 100
    for z, value in ((1, 100), (2, 200), (3, 800), (4, 700)):
        image[1, z, 10:20, 10:20] = value
    labels = np.zeros((6, 64, 64), dtype=np.uint16)
    labels[1:4, 10:20, 10:20] = 5
    path = tmp_path / "prof.ome.tif"
    tifffile.imwrite(path, image, ome=True, metadata={
        "axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0,
        "Channel": {"Name": ["DNA", "CD8"]}})
    tifffile.imwrite(tmp_path / "prof_mask.tif", labels)
    register_image_datasource("prof", path)
    datasets.add_layers("prof", tmp_path / "prof_mask.tif", wait=True)
    session = AgentSession()
    started = _ok(invoke(session, "measure_objects", {"project": "prof"}))
    _ok(invoke(session, "job_wait", {"job_id": started["job_id"], "timeout_s": 60}))

    out = _ok(invoke(session, "object_z_profile", {"project": "prof", "object_ids": [5],
                                                   "channels": ["CD8"]}))
    cd8 = out["objects"][0]["channels"]["CD8"]
    assert [p["mean"] for p in cd8["profile"]] == [None, 100.0, 200.0, 800.0, None]
    assert cd8["profile"][-1]["footprint_mean"] == 700.0
    assert cd8["peak_z"] == 3 and cd8["peak_at_end_plane"] is True
    assert cd8["fwhm_planes"] == 1 and cd8["fwhm_um"] == pytest.approx(2.0)
    assert cd8["beyond"]["after"] == pytest.approx(700 / (1100 / 3), abs=1e-3)
    assert cd8["beyond"]["before"] < 0.05

    views = _ok(invoke(session, "render_object_views", {
        "project": "prof", "object_id": 5, "output_px": 160,
        "channels": [{"name": "CD8", "color": "#00ff00", "window": [0, 1000]}]}))
    assert views["manifest"]["z_profile"]["CD8"]["peak_z"] == 3
    assert views["manifest"]["z_profile"]["CD8"]["means_by_plane"]["3"] == 800.0


def test_render_region_outlines_the_cells_on_the_plane_it_draws(tmp_path):
    """Cell 5 spans planes 1-3 and cell 9 planes 3-5: plane 1 holds only 5,
    plane 5 only 9, the middle plane both."""
    _volume_with_mask(tmp_path)
    session = AgentSession()
    seen = {}
    for view in ("z1", "z5", None):
        args = {"project": "obj", "bounds": {"x": 0, "y": 0, "width": 64, "height": 64},
                "channels": [{"name": "DNA"}], "segmentation": "outlines",
                "output": {"width": 64, "height": 64}, **({"view": view} if view else {})}
        manifest = _ok(invoke(session, "render_region", args))["manifest"]
        seen[view] = (manifest["cells"]["visible_cells"],
                      manifest["segmentation"].get("mask_plane"))
    assert seen == {"z1": (1, 1), "z5": (1, 5), None: (2, None)}
