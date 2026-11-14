"""A multi-page raster export hands the user every page, not page 1 alone.

PNG and TIFF have no pages, so a figure of N pages is written as N files
(`<stem>-1.png` ... `<stem>-N.png`) beside its provenance manifest. The Download
button is one link to one route, so whatever that route answers is all the user
gets: if it answers the first file, pages 2..N sit in the server's exports
folder where nobody can reach them, while the dialog says the export is done.
"""

import io
import json
import time
import zipfile

import pytest

import plexora
from plexora.server import plugins as plugin_registry
from tests.helpers import ALL_CONFIRMED, image_spec, project, use_data_root

API = "/plugins/figure_builder/api"


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "pagesdemo": project("pagesdemo", image=image_spec(channels=("DNA", "CD3"),
                                                           width=4000, height=3000),
                             confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")
    if plugin_registry.find(plexora.app, "figure_builder") is None:  # pragma: no cover
        pytest.skip("figure_builder is not installed")
    yield plexora.app.test_client()
    from plexora.plugins.figure_builder.server import export_jobs
    export_jobs.drain()


def _two_page_figure(client):
    figure_id = client.post(f"{API}/figures", json={"title": "Figure 1"}).get_json()["figure_id"]
    response = client.patch(f"{API}/figures/{figure_id}", json={
        "base_revision": 0,
        "operations": [
            {"op": "add_page", "page": {"page_id": "pg_2"}},
            {"op": "add_source", "source": {
                "source_id": "src_1", "kind": "plexora_project", "datasource": "pagesdemo",
                "image": {"width": 4000, "height": 3000}}},
            {"op": "add_panel", "panel": {
                "panel_id": "pnl_1", "source_id": "src_1",
                "scene": {"viewport": {"x": 0, "y": 0, "w": 400, "h": 300}, "channels": []},
                "placement": {"page_id": "pg_1", "x_mm": 10, "y_mm": 10,
                              "w_mm": 40, "h_mm": 30, "z": 0}}},
            {"op": "add_panel", "panel": {
                "panel_id": "pnl_2", "source_id": "src_1",
                "scene": {"viewport": {"x": 400, "y": 300, "w": 400, "h": 300}, "channels": []},
                "placement": {"page_id": "pg_2", "x_mm": 10, "y_mm": 10,
                              "w_mm": 40, "h_mm": 30, "z": 0}}},
        ]})
    assert response.status_code == 200, response.get_data(as_text=True)
    return figure_id


def _finished(client, figure_id, job_id):
    deadline = time.time() + 60
    while time.time() < deadline:
        body = client.get(f"{API}/figures/{figure_id}/export/{job_id}").get_json()
        if body["job"]["status"] != "running":
            return body["job"]
        time.sleep(0.05)
    raise AssertionError("export did not finish")


def test_downloading_a_two_page_png_export_delivers_both_pages(client):
    """Two pages placed, exported as PNG: the one Download link must carry both
    pages (a zip of them is the obvious shape), not a lone PNG of page 1."""
    figure_id = _two_page_figure(client)
    started = client.post(f"{API}/figures/{figure_id}/export",
                          json={"format": "png", "dpi": 96})
    assert started.status_code == 200
    job = _finished(client, figure_id, started.get_json()["job_id"])
    assert job["status"] == "done", job.get("error")
    pages_written = [p for p in job["result"]["files"] if p.endswith(".png")]
    assert len(pages_written) == 2, job["result"]["files"]

    download = client.get(f"{API}/figures/{figure_id}/export/{job['job_id']}/download")
    assert download.status_code == 200
    data = download.get_data()

    if data[:8].startswith(b"\x89PNG"):
        delivered = 1
    else:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            delivered = sum(1 for name in archive.namelist() if name.endswith(".png"))
    assert delivered == 2, (
        f"the download delivered {delivered} of the 2 pages written "
        f"({download.headers.get('Content-Disposition')})")
