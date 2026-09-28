"""An unexpected failure in an import route answers JSON, never Flask's HTML
500 page -- which the dialog could only report as a JSON parse error."""

import pytest


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


@pytest.mark.parametrize("route, target", [
    ("/import/inspect", "plexora.server.models.import_proposal.inspect_paths"),
    ("/import/sample", "plexora.server.models.import_sample.import_sample"),
])
def test_an_unexpected_failure_is_json_with_its_reason(client, monkeypatch,
                                                      route, target):
    module, name = target.rsplit(".", 1)
    import importlib

    def boom(*args, **kwargs):
        raise RuntimeError("the node went away mid-import")

    monkeypatch.setattr(importlib.import_module(module), name, boom)
    response = client.post(route, json={"paths": ["/x/slide"], "answers": {}})
    assert response.status_code == 500
    assert response.is_json
    assert response.get_json()["error"] == "the node went away mid-import"


def test_a_dicom_folder_on_a_node_is_an_image_not_a_run_folder(tmp_path,
                                                             monkeypatch):
    from plexora.server.node import resources
    from plexora.server.utils import dicom_wsi

    slide = tmp_path / "fc078201"
    slide.mkdir()
    monkeypatch.setattr(dicom_wsi, "is_dicom_path",
                        lambda path: str(path) == str(slide))
    detected = resources.detect_kind(str(slide))
    assert detected["kind"] == "image" and detected["reason"] == ""
    # Any other folder is still refused, with the sentence that says so.
    other = tmp_path / "run"
    other.mkdir()
    assert "is a folder" in resources.detect_kind(str(other))["reason"]


def test_a_node_failure_carries_its_sentence_not_a_bare_500(monkeypatch):
    """A node without pydicom raised an exception that names the exact pip
    install; the primary saw only `failed: 500`."""
    from plexora.server.node import resources as node_resources
    from plexora.server.node.app import create_node_app

    def refuse(path):
        raise RuntimeError("needs pydicom: pip install 'plexora[wsi]'")

    monkeypatch.setattr(node_resources, "detect_kind", refuse)
    app = create_node_app([], token="node-secret", dynamic=True,
                          log=lambda *a, **k: None)
    from plexora.server.node.api import TOKEN_HEADER

    response = app.test_client().post(
        "/node/v1/detect", json={"path": "/data/slide"},
        headers={TOKEN_HEADER: "node-secret"})
    assert response.status_code == 500
    assert "pip install 'plexora[wsi]'" in response.get_json()["error"]
