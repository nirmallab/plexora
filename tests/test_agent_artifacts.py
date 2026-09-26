"""The artifact store: content-addressed, bounded, re-readable."""

import os
import time

from plexora.agent import artifacts


def test_put_is_content_addressed(tmp_path):
    a = artifacts.put("p", b"png-bytes", {"x": 1})
    b = artifacts.put("p", b"png-bytes", {"x": 1})
    c = artifacts.put("p", b"png-bytes", {"x": 2})
    assert a["id"] == b["id"] != c["id"]
    assert artifacts.ID_PATTERN.match(a["id"])
    png, sidecar = artifacts.get(a["id"])
    assert png == b"png-bytes" and sidecar["manifest"] == {"x": 1}
    assert str(tmp_path) in a["path"] and "/.agent/artifacts/" in a["path"]
    assert len(artifacts.list_artifacts("p")) == 2


def test_sweep_removes_old_then_oversized(tmp_path):
    old = artifacts.put("p", b"a" * 10, {"n": 1})
    new = artifacts.put("p", b"b" * 10, {"n": 2})
    past = time.time() - 30 * 86400
    os.utime(old["path"], (past, past))
    assert artifacts.sweep() == 1
    assert [e["id"] for e in artifacts.list_artifacts()] == [new["id"]]
    assert artifacts.sweep(budget=0) == 1


def test_a_render_can_become_a_figure_capture():
    scene = artifacts.as_capture_scene({
        "project": "p", "bounds_fullres": {"x": 1, "y": 2, "width": 3, "height": 4},
        "channels": [{"key": "CD8", "name": "CD8", "window": [0, 10], "color": "#ff0080"}],
        "overlays": ["segmentation"]})
    assert scene["viewport"] == {"x": 1, "y": 2, "w": 3, "h": 4}
    assert scene["channels"][0]["color"] == {"r": 255, "g": 0, "b": 128}
