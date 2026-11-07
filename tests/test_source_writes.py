"""Saving into a picked file from the website (server/utils/source_writes.py):
the staging contract under CPython.

On the website a file the user picked is read-only to Python. A writer asks
`writable(path)` where to write; for a picked file that is a staged copy,
handed to the page once at the end of the request (or background step), which
saves it over the original. Here the "picked" folder is a test folder whose
files are made read-only as the website's mount is, `ask` is the page's
answer to "may I?", and `deliver` keeps what the page would be handed.

The real writers are run twice on identical files -- once as the desktop
writes (in place) and once as the website does (staged, delivered) -- and the
delivered bytes must be the desktop's, byte for byte, with the picked original
untouched.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from plexora.server.utils import source_writes


class Page:
    """The page's side: the setting, and what it was handed."""

    def __init__(self, allowed=True):
        self.allowed = allowed
        self.asked = 0
        self.saved = []

    def ask(self, kind, args):
        assert kind == "sourceWrites"
        self.asked += 1
        return self.allowed

    def deliver(self, source, staged):
        self.saved.append((source, Path(staged).read_bytes()))
        return True


@pytest.fixture
def website(tmp_path, monkeypatch):
    """A website: picked files under tmp_path/picked, read-only."""
    picked = tmp_path / "picked"
    picked.mkdir()
    page = Page()
    monkeypatch.setattr(source_writes, "on_website", lambda: True)
    source_writes.configure(root=str(picked), ask=page.ask, deliver=page.deliver)
    yield SimpleNamespace(page=page, picked=picked)
    for path in picked.rglob("*"):
        if path.is_file():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    source_writes.reset()


def _pick(website, source: Path) -> Path:
    """`source` as the user's pick: copied into the picked folder, read-only."""
    target = website.picked / "ab12" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    os.chmod(target, stat.S_IREAD)
    return target


def _append(path, text):
    with open(path, "ab") as handle:
        handle.write(text)


# -- the contract -------------------------------------------------------------------


def test_off_the_website_a_write_is_where_it_always_was(tmp_path):
    path = tmp_path / "cells.csv"
    path.write_bytes(b"a\n1\n")
    with source_writes.writable(path) as target:
        assert target == path
        _append(target, b"2\n")
    assert path.read_bytes() == b"a\n1\n2\n"
    assert source_writes.current(path) == path


def test_a_picked_file_is_never_written_unless_the_user_allowed_it(website, tmp_path):
    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n1\n")
    picked = _pick(website, original)
    website.page.allowed = False
    with pytest.raises(source_writes.SourceWriteRefused) as refused:
        with source_writes.writable(picked):
            raise AssertionError("not reached")
    assert refused.value.reason == source_writes.OFF
    assert "Settings › Data" in str(refused.value)
    # A ValueError too: every writer's handler says it as it is.
    assert isinstance(refused.value, ValueError)
    assert picked.read_bytes() == b"a\n1\n" and website.page.saved == []
    with pytest.raises(source_writes.SourceWriteRefused):
        source_writes.check(picked)


def test_without_a_page_to_ask_nothing_is_allowed(website, tmp_path, monkeypatch):
    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n")
    picked = _pick(website, original)
    monkeypatch.setattr(source_writes, "_ask", None)
    with pytest.raises(source_writes.SourceWriteRefused):
        with source_writes.writable(picked):
            pass


def test_a_file_the_user_did_not_pick_is_written_in_place(website, tmp_path):
    # The project's own files (the store) are Python's to write on the website.
    own = tmp_path / "data" / "notes.txt"
    own.parent.mkdir()
    own.write_bytes(b"x")
    with source_writes.writable(own) as target:
        assert target == own
    assert website.page.asked == 0 and website.page.saved == []


def test_a_staged_write_reaches_the_page_and_leaves_the_original(website, tmp_path):
    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n1\n")
    picked = _pick(website, original)
    with source_writes.writable(picked) as target:
        assert Path(target) != picked and Path(target).read_bytes() == b"a\n1\n"
        _append(target, b"2\n")
    assert picked.read_bytes() == b"a\n1\n"
    assert website.page.saved == [(str(picked).replace("\\", "/"), b"a\n1\n2\n")]
    [record] = source_writes.delivered
    assert record["name"] == "cells.csv" and record["bytes"] == 6 and record["outcome"] is True
    # The staged copy is gone once handed over.
    assert not Path(target).exists()


def test_a_batch_writes_one_copy_reads_it_meanwhile_and_hands_it_over_once(website, tmp_path):
    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n")
    picked = _pick(website, original)
    with source_writes.batch():
        with source_writes.writable(picked) as first:
            _append(first, b"1\n")
        # A read after the write reads what was written.
        assert Path(source_writes.current(picked)).read_bytes() == b"a\n1\n"
        with source_writes.writable(picked) as second:
            assert second == first
            _append(second, b"2\n")
        assert website.page.saved == []
    assert website.page.saved == [(str(picked).replace("\\", "/"), b"a\n1\n2\n")]
    assert website.page.asked == 1
    assert source_writes.current(picked) == picked


def test_a_whole_file_write_needs_no_copy(website, tmp_path):
    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n1\n")
    picked = _pick(website, original)
    with source_writes.writable(picked, copy=False) as target:
        assert not Path(target).exists()
        Path(target).write_bytes(b"b\n")
    assert website.page.saved[-1][1] == b"b\n"


def test_a_write_that_fails_leaves_the_original_and_sends_nothing(website, tmp_path):
    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n")
    picked = _pick(website, original)
    with source_writes.batch():
        with pytest.raises(RuntimeError):
            with source_writes.writable(picked) as target:
                _append(target, b"half")
                raise RuntimeError("the write failed half way")
        assert source_writes.current(picked) == picked
    assert website.page.saved == [] and picked.read_bytes() == b"a\n"


def test_a_picked_folder_and_a_file_too_large_are_refused_with_a_sentence(website, tmp_path,
                                                                          monkeypatch):
    store = website.picked / "cd34" / "cells.zarr"
    store.mkdir(parents=True)
    with pytest.raises(source_writes.SourceWriteRefused) as folder:
        with source_writes.writable(store):
            pass
    assert folder.value.reason == source_writes.FOLDER and "desktop app" in str(folder.value)
    original = tmp_path / "big.h5ad"
    original.write_bytes(b"0" * 2048)
    picked = _pick(website, original)
    monkeypatch.setattr(source_writes, "LIMIT_BYTES", 1024)
    with pytest.raises(source_writes.SourceWriteRefused) as large:
        with source_writes.writable(picked):
            pass
    assert large.value.reason == source_writes.TOO_LARGE
    assert website.page.saved == []


def test_a_request_through_the_bridge_hands_its_writes_over_before_it_answers(website, tmp_path):
    """plexora/browser/bridge.py: the request is the batch."""
    from flask import Flask

    from plexora.browser import bridge

    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n")
    picked = _pick(website, original)
    app = Flask("source-writes-test")
    seen = []

    @app.route("/write", methods=["POST"])
    def write():
        for value in (b"1\n", b"2\n"):
            with source_writes.writable(picked) as target:
                _append(target, value)
        seen.append(list(website.page.saved))
        return "ok"

    answer = bridge.handle(app, {"method": "POST", "path": "/write"}, "https://viewer.plexoraapp.com")
    assert answer["status"] == 200
    assert seen == [[]]  # nothing handed over while the route ran
    assert website.page.saved == [(str(picked).replace("\\", "/"), b"a\n1\n2\n")]


def test_a_background_step_is_a_batch_too(website, tmp_path, monkeypatch):
    from plexora import _work
    from plexora.browser import cooperative

    original = tmp_path / "cells.csv"
    original.write_bytes(b"a\n")
    picked = _pick(website, original)

    seen = []

    def run_pending(**kwargs):
        if seen:
            return 0  # a later drain (a fixture's): nothing queued
        for value in (b"1\n", b"2\n"):
            with source_writes.writable(picked) as target:
                _append(target, value)
        seen.append(list(website.page.saved))
        return 1

    monkeypatch.setattr(_work, "run_pending", run_pending)
    monkeypatch.setattr(_work, "pending", lambda background=False: 0)
    cooperative.step()
    assert seen == [[]]
    assert website.page.saved == [(str(picked).replace("\\", "/"), b"a\n1\n2\n")]


# -- the real writers: the website's bytes are the desktop's ------------------------


def _desktop_and_website(website, tmp_path, original: Path, write):
    """`write(path)` run as the desktop runs it (a copy, in place) and as the
    website does (the pick, staged, delivered): (desktop bytes, delivered)."""
    desktop = tmp_path / "desktop" / original.name
    desktop.parent.mkdir(exist_ok=True)
    shutil.copyfile(original, desktop)
    picked = _pick(website, original)
    before = picked.read_bytes()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(source_writes, "on_website", lambda: False)
        write(desktop)
    with source_writes.batch():
        write(picked)
    assert picked.read_bytes() == before, "the picked original must not change"
    [(source, delivered)] = website.page.saved
    assert source == str(picked).replace("\\", "/")
    return desktop.read_bytes(), delivered


def test_gates_and_the_classifier_into_an_h5ad(website, tmp_path):
    """gating's Save Gates to Source File: the gates, their provenance and the
    classifier's calls, several opens of one file, handed over once."""
    import anndata as ad

    from plexora.plugins.gating.server import anndata_gates
    from plexora.plugins.gating.server.classifier import source_write as sw
    from plexora.plugins.gating.tests.test_anndata_gates import (
        _feature_config, _make_multi_image_adata)
    from plexora.plugins.gating.tests.test_classifier_source_write import _payload

    original = tmp_path / "multi.h5ad"
    _make_multi_image_adata().write_h5ad(original)
    # The save's id is random per save: fixed, so two saves can be compared.
    payload = {**_payload(["marker_1", "marker_3"]), "save_id": "fixed"}

    def write(path):
        source = _feature_config(path, "B")
        anndata_gates.save_gates_to_anndata(source, "ds_b", {"marker_0": 0.25, "marker_2": 0.5},
                                            imageid_column="imageid")
        anndata_gates.save_gate_provenance(source, "ds_b", [{"marker": "marker_0", "how": "manual"}],
                                           imageid_column="imageid")
        sw.save_classifier(source, "ds_b", payload, "imageid")

    desktop, delivered = _desktop_and_website(website, tmp_path, original, write)
    assert delivered == desktop
    staged = tmp_path / "delivered.h5ad"
    staged.write_bytes(delivered)
    back = ad.read_h5ad(staged)
    assert "gates" in back.uns and "plexora_binary" in back.layers


def test_roi_columns_onto_a_csv_table(website, tmp_path):
    """roi.map_to_cells' write on a flat table: the whole file, replaced."""
    from plexora.plugins.roi.server import adapters

    original = tmp_path / "cells.csv"
    original.write_text("CellID,X_centroid,Y_centroid,CD45\n1,10.5,20,0.25\n2,30,40.75,1.5\n"
                        "3,50,60,2\n", encoding="utf-8")

    def write(path):
        dataset = SimpleNamespace(source_kind="csv",
                                  table=SimpleNamespace(source=SimpleNamespace(path=str(path))))
        adapters.write_cell_columns(dataset, ["Tumor", "", "Stroma"], ["t1", "", "s1"], prefix="rois")

    desktop, delivered = _desktop_and_website(website, tmp_path, original, write)
    assert delivered == desktop
    assert delivered.decode("utf-8").splitlines()[0].endswith("rois_category,rois_name")


def test_roi_columns_onto_an_h5ad_obs(website, tmp_path):
    """roi.map_to_cells' write on an AnnData: obs rewritten in the file."""
    from plexora.plugins.roi.server import adapters
    from plexora.plugins.gating.tests.test_anndata_gates import _make_multi_image_adata

    original = tmp_path / "cells.h5ad"
    _make_multi_image_adata(images=("A",), per_image=3).write_h5ad(original)

    def write(path):
        frame = None
        dataset = SimpleNamespace(
            source_kind="anndata", name="ds",
            schema=SimpleNamespace(cell_id=None, image_id=None),
            project=SimpleNamespace(dataset=None),
            table=SimpleNamespace(source=SimpleNamespace(path=str(path), subset={}, kind="anndata"),
                                  frame=lambda: frame, available=False))
        adapters._write_obs_columns(dataset, ["Tumor", "", "Stroma"], ["t1", "", "s1"],
                                    "rois_category", "rois_name", False)

    desktop, delivered = _desktop_and_website(website, tmp_path, original, write)
    assert delivered == desktop


def test_qc_calls_into_a_csv_table(website, tmp_path):
    """QC's "Save into file" (write_qc_to_source, the `cells/write` route's
    capability) on a CSV project, refused while the setting is off."""
    from plexora.agent import AgentSession, invoke, registry
    from plexora.agent.policy import Policy
    from tests.qc_fixtures import make_qc_project

    registry.discover(["roi", "qc"])
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    box = {"type": "Polygon", "coordinates": [[[100, 100], [300, 100], [300, 300], [100, 300],
                                               [100, 100]]]}
    assert invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                          "geometry": box})["ok"]
    assert invoke(session, "refresh_qc", {"project": "qcsynth"})["ok"]
    path = tmp_path / "_qcsynth_files" / "cells.csv"
    before = path.read_bytes()
    writes = Policy.from_flags(allow_source_writes=True)
    arguments = {"project": "qcsynth", "confirm": True}

    # The website: the project's table is the user's pick.
    source_writes.configure(root=str(path.parent))
    os.chmod(path, stat.S_IREAD)
    website.page.allowed = False
    refused = invoke(session, "write_qc_to_source", arguments, policy=writes)
    assert not refused["ok"] and "Settings › Data" in str(refused.get("error"))
    assert website.page.saved == []
    website.page.allowed = True
    with source_writes.batch():
        assert invoke(session, "write_qc_to_source", arguments, policy=writes)["ok"]
    assert path.read_bytes() == before
    [(source, delivered)] = website.page.saved

    # The desktop, on the same file: the bytes it writes are the ones delivered.
    # (The website's backup copied the read-only bit this test gave the pick;
    # the website's own file system ignores it.)
    for made in tmp_path.rglob("*"):
        if made.is_file():
            os.chmod(made, stat.S_IWRITE | stat.S_IREAD)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(source_writes, "on_website", lambda: False)
        desktop = invoke(session, "write_qc_to_source", arguments, policy=writes)
        assert desktop["ok"], desktop.get("error")
    assert path.read_bytes() == delivered and delivered != before
