import json
import os
import pickle
import sqlite3

import pytest

from biological_autocontrast import labels
from biological_autocontrast.io import file_identity

CHANNELS = ["DNA", "CD3", "Dim", "Empty"]


def _fake_root(tmp_path, image, windows, blob=None):
    root = tmp_path / "plexora-root"
    (root / "fake").mkdir(parents=True)
    image_data = [{"name": "Area", "fullname": "Area", "src": "/x/"}] + [
        {"name": n, "fullname": n, "src": f"/generated/data/fake/img_{i}/"}
        for i, n in enumerate(CHANNELS)]
    (root / "config.json").write_text(json.dumps(
        {"fake": {"channelFile": image, "imageData": image_data}}))
    db = root / "fake" / "fake.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE channelList (id INTEGER PRIMARY KEY, datasource TEXT, "
                     "channelList BLOB, extra INTEGER)")
        conn.execute("INSERT INTO channelList VALUES (1, 'fake', ?, 0)",
                     (blob if blob is not None else pickle.dumps(windows),))
    return root, db


def _w(name, start, end):
    return {"channel": name, "start": start, "end": end, "r": 255, "g": 255, "b": 255,
            "opacity": 1, "channel_active": True}


def test_pull_reads_set_windows_and_skips_defaults(fx, tmp_path):
    root, db = _fake_root(tmp_path, fx["synth16"], [
        _w("DNA", 300, 20000), _w("CD3", 1200, 40000), _w("Dim", 0, 65536),
        _w("Empty", 0, 65535), _w("Ghost", 5, 10)])
    before = file_identity(db), file_identity(fx["synth16"])
    summary = labels.pull_plexora("fake", "tester", data_root=root)
    assert (file_identity(db), file_identity(fx["synth16"])) == before
    assert summary["labels"] == 2
    reasons = {s["channel"]: s["reason"] for s in summary["skipped"]}
    assert set(reasons) == {"Dim", "Empty", "Ghost"}
    frame = labels.load_all()
    assert set(frame["channel_name"]) == {"DNA", "CD3"}
    assert set(frame["source"]) == {labels.SOURCE_PLEXORA}
    cd3 = frame[frame["channel_name"] == "CD3"].iloc[0]
    assert cd3["channel_index"] == 1 and cd3["full_scale"] == 65535
    assert cd3["display_max_norm"] == pytest.approx(40000 / 65535)
    assert cd3["gamma"] == 1.0 and cd3["decision"] == "edit"
    # pulling again replaces, never duplicates
    labels.pull_plexora("fake", "tester", data_root=root)
    assert len(labels.load_all()) == 2


def test_pull_accepts_a_project_folder(fx, tmp_path):
    root, _db = _fake_root(tmp_path, fx["synth16"], [_w("CD3", 1200, 40000)])
    assert labels.pull_plexora(str(root / "fake"), "tester")["labels"] == 1


def test_a_blob_that_builds_objects_is_refused(fx, tmp_path):
    class Evil:
        def __reduce__(self):
            return (os.system, ("echo pwned",))

    root, _db = _fake_root(tmp_path, fx["synth16"], None, blob=pickle.dumps([Evil()]))
    with pytest.raises(pickle.UnpicklingError):
        labels.pull_plexora("fake", "tester", data_root=root)


def test_add_index_and_export(fx, tmp_path):
    labels.add_label(fx["synth8"], "CD3", 4, 170, annotator="me")
    labels.add_label(fx["synth8"], 3, None, None, annotator="me", decision="no_signal")
    path, frame = labels.build_index()
    assert len(frame) == 2 and set(frame["specimen"]) == {"synth8.ome.tif"}
    assert set(frame["decision"]) == {"edit", "no_signal"}
    out, n = labels.export_labels(tmp_path / "l.json", "json")
    assert n == 2 and json.loads(out.read_text())[0]["gamma"] == 1.0
    with pytest.raises(ValueError):
        labels.add_label(fx["synth8"], "CD3", 50, 10, annotator="me")


def test_default_yaml_is_the_default_config():
    from pathlib import Path

    from biological_autocontrast.config import Config

    path = Path(__file__).resolve().parents[1] / "configs" / "default.yaml"
    assert Config.load(path).digest() == Config().digest()


def test_a_node_project_reads_through_the_node(tmp_path):
    root = tmp_path / "root"
    (root / "remote").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"remote": {
        "channelFile": "", "imageData": [{"name": "DNA", "fullname": "DNA"}],
        "resources": {"image": {"provider": "node", "node": "HMS-O2", "resource_id": "img-1"}}}}))
    with sqlite3.connect(root / "remote" / "remote.db") as conn:
        conn.execute("CREATE TABLE channelList (id INTEGER PRIMARY KEY, datasource TEXT, "
                     "channelList BLOB, extra INTEGER)")
        conn.execute("INSERT INTO channelList VALUES (1, 'remote', ?, 0)",
                     (pickle.dumps([_w("DNA", 10, 900)]),))
    info = labels.read_plexora_project("remote", root)
    assert info["image_path"] == "node://HMS-O2/img-1"
    assert info["windows"][0]["end"] == 900
