"""Route-level check of the quick-view landing page backend: POST /quick_view
registers a datasource from a bare local path (no CSV/segmentation) and
returns a redirect into the viewer; GET /generated/rgb/<name> serves the
flat image for an RGB datasource. Isolated from the real app data directory
via monkeypatching plexora.data_path/config_json_path, matching the
pattern in test_datasource_config_routes.py.
"""

import json

import numpy as np
import tifffile
from PIL import Image

import plexora


def _write_image(path, size=64, channels=2):
    rng = np.random.default_rng(0)
    tifffile.imwrite(path, rng.integers(1, 255, size=(channels, size, size), dtype=np.uint8))


def _write_png(path, size=32):
    Image.fromarray(np.zeros((size, size, 3), dtype=np.uint8)).save(path)


def test_quick_view_registers_ome_tiff_and_redirects(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(image_path)})

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["name"] == "sample"
    assert data["redirect"] == "/sample"

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["sample"]["image_kind"] == "ome_tiff"


def test_quick_view_dedupes_name_on_repeat_registration(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()

    first = client.post("/quick_view", json={"path": str(image_path)}).get_json()
    second = client.post("/quick_view", json={"path": str(image_path)}).get_json()

    assert first["name"] == "sample"
    assert second["name"] == "sample_2"


def test_quick_view_rejects_missing_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(tmp_path / "nope.tif")})

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_quick_view_rejects_unsupported_extension(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    bogus = tmp_path / "notes.csv"
    bogus.write_text("a,b\n1,2\n", encoding="utf-8")
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(bogus)})

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_quick_view_registers_rgb_and_serves_image(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    png_path = tmp_path / "photo.png"
    _write_png(png_path)
    client = plexora.app.test_client()

    response = client.post("/quick_view", json={"path": str(png_path)})
    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "photo"

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["photo"]["image_kind"] == "rgb"

    image_response = client.get("/generated/rgb/photo")
    assert image_response.status_code == 200
    assert image_response.mimetype == "image/png"


def test_generated_rgb_rejects_non_rgb_datasource(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image_path = tmp_path / "sample.ome.tif"
    _write_image(image_path)
    client = plexora.app.test_client()
    client.post("/quick_view", json={"path": str(image_path)})

    response = client.get("/generated/rgb/sample")
    assert response.status_code == 404
