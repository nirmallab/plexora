"""A section tile reads no more than a budget's worth of chunks.

A section through a stack stored plane by plane decodes a chunk-row of every
plane it crosses (server/volume/plane_view.py). Past
SECTION_READ_BUDGET a served tile is computed at the finest coarser level that
fits and drawn up -- and the route keeps it out of every cache, so the sharp
tile replaces it once the area has been prepared in 3-D. Under the budget a
section is exact and cached as it always was.
"""

import io

import numpy as np
import pytest
from PIL import Image

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.volume import plane_view
from tests import volume_fixtures as vf


@pytest.fixture()
def client():
    return plexora.app.test_client()


def _png16(payload):
    return np.asarray(Image.open(io.BytesIO(payload)))


def _stack(tmp_path):
    path, data = vf.write_ngff_volume(tmp_path / "deep.zarr", channels=1, depth=40,
                                      height=128, width=128, levels=3, chunks_z=1)
    entry = register_image_datasource("deep", path)
    data_model.load_datasource("deep")
    src = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]
    return data, f"/generated/data/deep/{src}"


def test_a_section_over_budget_is_drawn_up_from_a_coarser_level_and_never_kept(tmp_path, client,
                                                                              monkeypatch):
    data, base = _stack(tmp_path)
    exact = client.get(base + "/0/0_0.png?q=hd&view=xzy20")
    assert exact.status_code == 200 and "X-Plexora-Served-Steps" not in exact.headers
    assert exact.headers["Cache-Control"] == "private, max-age=31536000"
    # Every plane's chunk-row at level 0 is 40 x 64 x 64 x 2 bytes x 2 chunks;
    # a budget of a quarter of that leaves level 1 (half the chunk-rows).
    monkeypatch.setattr(plane_view, "SECTION_READ_BUDGET", 40 * 64 * 64 * 2 * 2 // 2)
    from plexora.server.routes import data_routes

    data_routes._tile_png_cache.clear()
    from plexora.server.models import views

    views._views.clear()
    drawn = client.get(base + "/0/0_0.png?q=hd&view=xzy21")
    assert drawn.headers.get("X-Plexora-Served-Steps") == "1"
    assert drawn.headers["Cache-Control"] == "no-store"
    pixels = _png16(drawn.data)
    anisotropy = data_model.volume_source.volume.spacing.anisotropy()
    rows0 = plane_view.section_rows(40, anisotropy, 0)
    rows1 = plane_view.section_rows(40, anisotropy, 1)
    planes1 = [plane_view.section_plane_of(r, 40, anisotropy, 1) for r in range(rows1)]
    # level 1 holds every other row and column: its row 21 * 64 // 128 is row 20 here
    coarse = data[0][planes1, 2 * (21 * 64 // 128), ::2]
    expected = np.repeat(np.repeat(coarse, 2, axis=0), 2, axis=1)[:rows0, :128]
    assert np.array_equal(pixels[:rows0, :128], expected), "each coarse pixel repeated"
    again = client.get(base + "/0/0_0.png?q=hd&view=xzy21")
    assert again.headers.get("X-Plexora-Served-Steps") == "1", "a drawn-up tile is never cached"


def test_a_section_under_budget_is_exact(tmp_path, client):
    data, base = _stack(tmp_path)
    pixels = _png16(client.get(base + "/0/0_0.png?q=hd&view=xzy33").data)
    rows = plane_view.section_rows(40, data_model.volume_source.volume.spacing.anisotropy(), 0)
    planes = [plane_view.section_plane_of(r, 40, data_model.volume_source.volume.spacing.anisotropy(), 0)
              for r in range(rows)]
    assert np.array_equal(pixels[:rows, :128], data[0][planes, 33, :])


def test_a_tile_the_browser_has_is_answered_before_a_pixel_is_read(tmp_path, client, monkeypatch):
    data, base = _stack(tmp_path)
    first = client.get(base + "/0/0_0.png?view=z7")
    tag = first.headers["ETag"]
    from plexora.server.models import data_model as model

    def boom(*args, **kwargs):
        raise AssertionError("a revalidation encoded the tile")

    monkeypatch.setattr(model, "encode_tile", boom)
    again = client.get(base + "/0/0_0.png?view=z7", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.headers["ETag"] == tag
