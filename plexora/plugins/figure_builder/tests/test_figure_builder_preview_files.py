"""Previews: kept in files of their own, cached for good at their revision, and
no edit costs a rewrite of the pictures.

Two costs a figure used to pay on every edit, felt most on the website (where
the page's own Python answers one request at a time and the project store is
copied to the user's folder after each write):

- every panel's preview was a BLOB in `figure.db`, and the database is
  rewritten by every PATCH -- moving one panel rewrote every picture;
- every preview was served `no-cache`, so each time the canvas drew a panel the
  browser asked for it again.

Now the bytes are files under `previews/` and the database holds which revision
is current, and a preview asked for at the revision it holds may be kept for
good. What makes "for good" true is that a render revision is never reused for
different bytes -- including across an undo, which is the case pinned last.
"""

import json
import sqlite3

import pytest

import plexora
from plexora.plugins.figure_builder.server import operations, repository
from plexora.server import plugins as plugin_registry
from tests.helpers import use_data_root

API = "/plugins/figure_builder/api"


@pytest.fixture
def figures(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture
def client(figures):
    if plugin_registry.find(plexora.app, "figure_builder") is None:  # pragma: no cover
        pytest.skip("figure_builder is not installed")
    return plexora.app.test_client()


def _db(figure_id):
    return repository.figure_dir(figure_id) / repository.DB_FILENAME


def _panel_document(figure_id, revision=1):
    document = repository.load(figure_id)
    page = document["pages"][0]["page_id"]
    return [
        {"op": "add_source", "source": {"source_id": "src_1", "kind": "plexora_project",
                                        "datasource": "demo", "channels": [{"key": "DNA_0"}]}},
        {"op": "add_panel", "panel": {
            "panel_id": "pnl_1", "source_id": "src_1", "render_revision": revision,
            "scene": {"viewport": {"x": 0, "y": 0, "w": 100, "h": 80}},
            "placement": {"page_id": page, "x_mm": 10, "y_mm": 10, "w_mm": 40, "h_mm": 30, "z": 0}}},
    ]


# -- files, not blobs -----------------------------------------------------

def test_a_preview_is_a_file_and_the_database_stays_small(figures):
    figure_id = repository.create("One")
    picture = b"\x00webp" * 40_000
    for index in range(10):
        assert repository.put_preview(figure_id, f"pnl_{index}", 1, picture) is True
    assert repository.get_preview(figure_id, "pnl_3") == (picture, "webp", 1)
    files = list((repository.figure_dir(figure_id) / repository.PREVIEWS_DIRNAME).iterdir())
    assert len(files) == 10
    # Ten 200 KB pictures, and the database is still the size of a document.
    assert _db(figure_id).stat().st_size < 200_000


def test_a_newer_render_replaces_the_older_file(figures):
    figure_id = repository.create("One")
    repository.put_preview(figure_id, "pnl_1", 1, b"first")
    repository.put_preview(figure_id, "pnl_1", 2, b"second")
    assert repository.get_preview(figure_id, "pnl_1") == (b"second", "webp", 2)
    assert len(list((repository.figure_dir(figure_id) / repository.PREVIEWS_DIRNAME).iterdir())) == 1
    # And an older render arriving late changes nothing, file or row.
    assert repository.put_preview(figure_id, "pnl_1", 1, b"late") is False
    assert repository.get_preview(figure_id, "pnl_1") == (b"second", "webp", 2)


def test_a_preview_from_before_is_still_read_from_the_database(figures):
    """Figures saved before previews moved out keep their pictures until each
    panel is next rendered."""
    figure_id = repository.create("One")
    connection = sqlite3.connect(str(_db(figure_id)))
    with connection:
        connection.execute(
            "INSERT INTO previews (panel_id, render_revision, width, height, format, data, updated_at) "
            "VALUES ('pnl_old', 4, 10, 8, 'webp', ?, '')", (sqlite3.Binary(b"blob-bytes"),))
    connection.close()
    assert repository.get_preview(figure_id, "pnl_old") == (b"blob-bytes", "webp", 4)
    repository.put_preview(figure_id, "pnl_old", 5, b"file-bytes")
    assert repository.get_preview(figure_id, "pnl_old") == (b"file-bytes", "webp", 5)


def test_an_id_a_filename_could_not_hold_is_still_one_file(figures):
    figure_id = repository.create("One")
    repository.put_preview(figure_id, "pnl:A", 1, b"upper")
    repository.put_preview(figure_id, "pnl:a", 1, b"lower")
    assert repository.get_preview(figure_id, "pnl:A")[0] == b"upper"
    assert repository.get_preview(figure_id, "pnl:a")[0] == b"lower"
    for path in (repository.figure_dir(figure_id) / repository.PREVIEWS_DIRNAME).iterdir():
        assert ":" not in path.name


def test_a_duplicate_takes_the_preview_files_with_it(figures):
    figure_id = repository.create("One")
    repository.put_preview(figure_id, "pnl_1", 3, b"pixels")
    copy = repository.duplicate(figure_id)
    assert repository.get_preview(copy, "pnl_1") == (b"pixels", "webp", 3)


def test_moving_a_panel_does_not_rewrite_its_picture(figures):
    figure_id = repository.create("One")
    revision = repository.apply(figure_id, 0, _panel_document(figure_id))
    repository.put_preview(figure_id, "pnl_1", 1, b"\x01" * 500_000)
    picture = next((repository.figure_dir(figure_id) / repository.PREVIEWS_DIRNAME).iterdir())
    written = picture.stat().st_mtime_ns
    for step in range(5):
        revision = repository.apply(figure_id, revision, [{"op": "move_panels", "moves": [
            {"panel_id": "pnl_1", "placement": {"x_mm": 20 + step}}]}])
    assert picture.stat().st_mtime_ns == written
    assert _db(figure_id).stat().st_size < 200_000


# -- the PATCH path ---------------------------------------------------------

def test_a_patch_edits_the_document_it_loaded_and_nothing_else():
    document = {"panels": {}, "pages": [], "annotations": {}}
    same = operations.apply_operations(document, [{"op": "set_meta", "changes": {"title": "x"}}],
                                       in_place=True)
    assert same is document
    # Everyone else still gets a copy.
    other = {"panels": {}, "pages": [], "annotations": {}, "title": "a"}
    copied = operations.apply_operations(other, [{"op": "set_meta", "changes": {"title": "b"}}])
    assert copied is not other and other["title"] == "a"


def test_a_failed_patch_still_stores_nothing(figures):
    figure_id = repository.create("One")
    revision = repository.apply(figure_id, 0, _panel_document(figure_id))
    before = repository.load(figure_id)
    with pytest.raises(ValueError):
        repository.apply(figure_id, revision, [
            {"op": "move_panels", "moves": [{"panel_id": "pnl_1", "placement": {"x_mm": 99}}]},
            {"op": "no_such_op"}])
    assert repository.load(figure_id) == before


# -- caching ------------------------------------------------------------------

def test_a_preview_asked_for_at_its_revision_is_kept_for_good(client):
    figure_id = client.post(f"{API}/figures", json={"title": "F"}).get_json()["figure_id"]
    client.post(f"{API}/figures/{figure_id}/previews/pnl_1?render_revision=3",
                data=b"x", content_type="image/webp")
    kept = client.get(f"{API}/figures/{figure_id}/previews/pnl_1?v=3")
    assert kept.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    # Any other revision names other bytes, and is not kept.
    for asked in ("?v=2", "?v=4", "?v=abc", ""):
        response = client.get(f"{API}/figures/{figure_id}/previews/pnl_1{asked}")
        assert response.status_code == 200
        assert "no-cache" in response.headers["Cache-Control"], asked


def test_a_thumbnail_is_still_revalidated(client):
    figure_id = client.post(f"{API}/figures", json={"title": "F"}).get_json()["figure_id"]
    client.put(f"{API}/figures/{figure_id}/thumbnail", data=b"t", content_type="image/webp")
    response = client.get(f"{API}/figures/{figure_id}/thumbnail?v=0")
    assert "no-cache" in response.headers["Cache-Control"]


def test_an_undo_never_takes_a_render_revision_back(figures):
    """The case that would make a kept preview wrong: Quick Edit re-renders a
    panel at revision 2, the user undoes (the document from before says 1), and
    a later edit bumps 1 to 2 again -- a second picture under a URL the browser
    keeps. The replace keeps the revision at 2, so the next edit is 3."""
    figure_id = repository.create("One")
    revision = repository.apply(figure_id, 0, _panel_document(figure_id))
    before_edit = json.loads(json.dumps(repository.load(figure_id)))
    revision = repository.apply(figure_id, revision, [{"op": "update_panel", "panel_id": "pnl_1",
                                                       "changes": {"render_revision": 2}}])
    repository.put_preview(figure_id, "pnl_1", 2, b"edited")
    revision = repository.replace(figure_id, revision, before_edit)
    assert repository.load(figure_id)["panels"]["pnl_1"]["render_revision"] == 2
