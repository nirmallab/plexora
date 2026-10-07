"""A launch link opens the project showing what it asked for -- and only that.

`GET /<project>?launch=<json>` (and `/desktop/open {context}`) carries one page
view's state: channels, a colour-by column, cells to point at, regions,
a viewport, a tool. The server validates every field and resolves cell ids to
positions; the page applies the rest (tests/js/launch_context_probe.mjs).
Nothing of it is saved over the project's own arrangement.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest

import plexora
from plexora.server.routes.page_routes import (LAUNCH_MAX_HIGHLIGHT, LAUNCH_MAX_REGIONS,
                                               _parse_launch, launch_from_dict)
from tests.scimappro_fixtures import make_anndata_project

REPO = Path(__file__).resolve().parent.parent
SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}


def test_every_key_survives_validation():
    launch = launch_from_dict({
        "channels": [{"name": "DNA", "color": "#0000ff"}],
        "color_by": "phenotype",
        "highlight_ids": [1, "2", 3.0],
        "regions": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": SQUARE,
             "properties": {"name": "edge", "category_color": "#ff0000"}}]},
        "viewport": {"x": 1, "y": 2, "width": 30, "height": 40},
        "tool": "cell_explorer",
    })
    assert launch["channels"] == [{"name": "DNA", "color": "#0000ff"}]
    assert launch["overlay"] == "phenotype" and "color_by" not in launch
    assert launch["highlight_ids"] == ["1", "2", "3.0"]
    assert launch["regions"] == [{"id": "launch_0", "geometry": SQUARE, "label": "edge",
                                  "color": "#ff0000"}]
    assert launch["viewport"] == {"x": 1.0, "y": 2.0, "width": 30.0, "height": 40.0}
    assert launch["tool"] == "cell_explorer"


def test_overlay_still_means_colour_by():
    assert launch_from_dict({"overlay": "cluster"}) == {"overlay": "cluster"}


def test_anything_unrecognised_or_hostile_is_dropped():
    launch = launch_from_dict({
        "color_by": "x" * 300, "tool": "../etc", "viewport": {"x": 0, "y": 0, "width": -1,
                                                             "height": 3},
        "regions": [{"type": "Feature", "geometry": {"type": "Point",
                                                     "coordinates": [1, 2]}}],
        "highlight_ids": [True, {"a": 1}, "y" * 500], "script": "<script>",
    })
    assert launch == {}
    assert _parse_launch("not json") == {} and _parse_launch('["a"]') == {}


def test_lists_are_capped():
    launch = launch_from_dict({
        "highlight_ids": list(range(LAUNCH_MAX_HIGHLIGHT + 50)),
        "regions": [{"type": "Feature", "geometry": SQUARE}] * (LAUNCH_MAX_REGIONS + 5)})
    assert len(launch["highlight_ids"]) == LAUNCH_MAX_HIGHLIGHT
    assert len(launch["regions"]) == LAUNCH_MAX_REGIONS


def test_cell_ids_are_resolved_to_positions_on_the_page(tmp_path):
    made = make_anndata_project(tmp_path)
    cells = {c["id"]: c for c in made["cells"]}
    wanted = sorted(cells)[:3]
    payload = {"highlight_ids": wanted + [999999], "color_by": "phenotype"}
    page = plexora.app.test_client().get(
        f"/{made['name']}?launch={quote(json.dumps(payload))}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    start = html.index('"launch"')
    launch = json.loads(html[html.index("{", start):_matching(html, html.index("{", start))])
    resolved = {int(c["id"]): (c["x"], c["y"]) for c in launch["highlight"]}
    assert set(resolved) == set(wanted)           # the unknown id is dropped
    for cell_id, (x, y) in resolved.items():
        assert (x, y) == pytest.approx((cells[cell_id]["x"], cells[cell_id]["y"]), abs=1e-3)
    assert launch["overlay"] == "phenotype"


def _matching(text, start):
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise AssertionError("unbalanced launch block")


def test_the_viewer_page_loads_the_launch_script(tmp_path):
    made = make_anndata_project(tmp_path)
    html = plexora.app.test_client().get(f"/{made['name']}").get_data(as_text=True)
    assert html.index("services/agentBridge.js") < html.index("services/launchContext.js")


def test_desktop_open_carries_the_context_in_its_url(tmp_path, monkeypatch):
    made = make_anndata_project(tmp_path)
    from plexora.server.routes import desktop_routes

    monkeypatch.setattr(desktop_routes, "_project_at", lambda path: made["name"])
    answer = plexora.app.test_client().post("/desktop/open", json={
        "paths": [made["image_path"]],
        "context": {"color_by": "phenotype", "tool": "cell_explorer",
                    "highlight_ids": [1, 2], "evil": "<script>"}})
    assert answer.status_code == 200, answer.get_json()
    url = urlparse(answer.get_json()["url"])
    assert url.path == f"/{made['name']}"
    query = parse_qs(url.query)
    assert query["tool"] == ["cell_explorer"]
    launch = json.loads(query["launch"][0])
    assert launch == {"overlay": "phenotype", "highlight_ids": ["1", "2"]}


def test_desktop_open_without_context_is_unchanged(tmp_path, monkeypatch):
    made = make_anndata_project(tmp_path)
    from plexora.server.routes import desktop_routes

    monkeypatch.setattr(desktop_routes, "_project_at", lambda path: made["name"])
    answer = plexora.app.test_client().post("/desktop/open",
                                            json={"paths": [made["image_path"]]})
    assert answer.get_json()["url"] == f"/{made['name']}"


def test_the_page_applies_it_through_the_bridge():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(REPO / "tests" / "js" / "launch_context_probe.mjs")],
                          capture_output=True, text=True, cwd=REPO, timeout=60)
    report = json.loads(proc.stdout)
    assert not report["problems"], report
    assert proc.returncode == 0
    assert report["cases"]["full"] == ["set_color_by", "fit_region", "highlight_cells",
                                       "show_shapes"]
