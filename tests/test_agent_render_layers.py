"""render_region draws the scene's other layers, or says why it did not."""

import io
import json

import numpy as np
import pytest
import tifffile

from plexora.agent import AgentSession
from plexora.agent import render as agent_render
from plexora.agent.render_spec import RenderInput
from plexora.server.models.project import LayerSpec, Project
from tests.agent_fixtures import make_synthetic_project

DX, DY = 200, 100


@pytest.fixture
def made(tmp_path):
    return make_synthetic_project(tmp_path)


def _save(tmp_path, record):
    config = json.loads((tmp_path / "config.json").read_text())
    config[record.name] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))


def _column_layer(tmp_path, transform=(1.0, 0.0, 0.0, 1.0, DX, DY), layer_id="second"):
    """A 128x128 one-channel slide, dark but for a bright column at x 10..14."""
    plane = np.zeros((1, 128, 128), dtype=np.uint16)
    plane[0, :, 10:14] = 5000
    path = tmp_path / f"{layer_id}.ome.tif"
    tifffile.imwrite(path, plane)
    return LayerSpec(id=layer_id, kind="image", label="Second slide", src=str(path),
                     width=128, height=128, max_level=1, tile_width=128, tile_height=128,
                     channels=({"name": "S", "fullname": "S", "src": f"/t/{layer_id}/0"},),
                     transform=transform,
                     render={"channels": [{"index": 0, "color": "#ff0000",
                                           "range": [0, 5000]}]})


def _render(**kwargs):
    spec = RenderInput(project="synth", bounds={"x": 0, "y": 0, "width": 512, "height": 512},
                       channels=[{"name": "DNA", "color": "#ffffff", "window": [0, 60000]}],
                       segmentation="none", output={"width": 512}, scale_bar=False, **kwargs)
    return agent_render.render_region(AgentSession(), spec, store=False)


def _pixels(result):
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(result["png"])).convert("RGB")).astype(int)


def test_a_translated_image_layer_lands_where_its_transform_says(made, tmp_path):
    _save(tmp_path, Project.load("synth").with_layer(_column_layer(tmp_path)))
    drawn = _render()
    rgb = _pixels(drawn)
    entry = drawn["manifest"]["layers_rendered"][0]
    assert entry["layer"] == "second" and entry["blend"] == "add"
    assert entry["channels"][0]["window_source"] == "layer"
    assert "layers" in drawn["manifest"]["overlays"]
    # The column is at layer x 10..14, so reference x 210..214, rows 100..228.
    red = rgb[:, :, 0] - rgb[:, :, 1]
    assert red[150, DX + 11] > 150
    assert red[150, DX + 30] < 20 and red[150, 11] < 20
    assert red[DY - 20, DX + 11] < 20 and red[DY + 150, DX + 11] < 20


def test_none_reproduces_the_picture_without_layers(made, tmp_path):
    before = _render()
    _save(tmp_path, Project.load("synth").with_layer(_column_layer(tmp_path)))
    assert _render(layers="none")["png"] == before["png"]
    assert _render(layers=["second"])["png"] != before["png"]


def test_a_sheared_layer_is_reported_not_drawn(made, tmp_path):
    layer = _column_layer(tmp_path, transform=(1.0, 0.2, 0.0, 1.0, 0.0, 0.0))
    _save(tmp_path, Project.load("synth").with_layer(layer))
    drawn = _render()
    assert drawn["manifest"]["layers_rendered"] == []
    skipped = drawn["manifest"]["not_rendered"][0]
    assert skipped["layer"] == "second" and "shear" in skipped["reason"]


def test_a_points_layer_draws_and_counts(made, tmp_path):
    from plexora.server.models import transcript_tiles as tt

    rng = np.random.default_rng(3)
    n = 300
    x = rng.uniform(0, 512, size=n).astype(np.float32)
    y = rng.uniform(0, 512, size=n).astype(np.float32)
    source = tmp_path / "tx.parquet"
    source.write_bytes(b"only its mtime is read")
    expected = tt.expected_manifest(source, width=512, height=512, tile_size=128,
                                    layer_id="tx")
    tt.build("synth", "tx", genes=["EPCAM", "CD3E"],
             gene_index=rng.integers(0, 2, size=n).astype(np.uint16), x=x, y=y,
             q=np.full(n, 30, dtype=np.uint8), expected=expected)
    layer = LayerSpec(id="tx", kind="points", label="Transcripts", width=512, height=512,
                      modality="transcripts", render={"color": "#00ff00"})
    _save(tmp_path, Project.load("synth").with_layer(layer))
    drawn = _render()
    entry = drawn["manifest"]["layers_rendered"][0]
    assert entry["kind"] == "points" and entry["points_drawn"] == n
    assert entry["truncated"] is False
    rgb = _pixels(drawn)
    px, py = int(x[0]), int(y[0])
    assert rgb[py, px, 1] > rgb[py, px, 0]


def test_compositing_never_loads_a_datasource(made, tmp_path, monkeypatch):
    from plexora.server.models import data_model

    _save(tmp_path, Project.load("synth").with_layer(_column_layer(tmp_path)))
    calls = []
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda *a, **k: calls.append(a) or None)
    assert _render()["manifest"]["layers_rendered"]
    assert calls == []
