"""A project's display name: what the user calls it, beside the name it is
keyed by.

The name is the config.json key, the folder, the `.db` stem and every URL, and
none of that moves. The title is one optional field of the entry -- stored
through the ordinary edit route, cleared by an empty string or the name itself,
and shown wherever a project is listed.
"""

import json

import numpy as np
import pytest
import tifffile

import plexora
from plexora import paths
from plexora.agent import AgentSession, invoke
from plexora.server.models import viewer_sessions
from plexora.server.models.project import MAX_TITLE, Project, clean_title, write_config
from tests import helpers
from tests.helpers import entry, project


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------

def test_a_title_round_trips_and_is_absent_when_unset():
    titled = project("s01").with_title("Tonsil, section 3")

    stored = titled.to_entry()
    assert stored["title"] == "Tonsil, section 3"
    back = Project.from_entry("s01", stored)
    assert back.title == "Tonsil, section 3"
    assert back.display_name == "Tonsil, section 3"
    # Never asked: no key at all, and the name is what is shown.
    assert "title" not in project("s01").to_entry()
    assert project("s01").display_name == "s01"


def test_a_title_is_modelled_not_left_in_extra_and_unknown_keys_survive():
    stored = {**entry("s01"), "title": "Tonsil", "someFutureKey": {"a": 1}}

    record = Project.from_entry("s01", stored)

    assert "title" not in record.extra
    assert record.to_entry()["someFutureKey"] == {"a": 1}
    assert record.with_title("Lymph node").to_entry()["someFutureKey"] == {"a": 1}


@pytest.mark.parametrize("given", ["", "   ", None, "s01", "  s01 "])
def test_empty_or_the_name_itself_clears_the_title(given):
    record = project("s01").with_title("Tonsil").with_title(given)

    assert record.title is None
    assert record.display_name == "s01"
    assert "title" not in record.to_entry()


def test_a_title_is_trimmed_and_its_whitespace_folded():
    assert clean_title("  Tonsil \t section\n 3  ", "s01") == "Tonsil section 3"


@pytest.mark.parametrize("bad", ["bell\x07", "nul\x00", "del\x7f", "c1\x85"])
def test_control_characters_are_refused(bad):
    with pytest.raises(ValueError, match="control characters"):
        clean_title(bad, "s01")


def test_an_over_long_title_is_refused():
    assert clean_title("x" * MAX_TITLE, "s01") == "x" * MAX_TITLE
    with pytest.raises(ValueError, match="too long"):
        clean_title("x" * (MAX_TITLE + 1), "s01")


def test_a_hand_edited_bad_title_reads_as_the_name_rather_than_failing():
    record = Project.from_entry("s01", {**entry("s01"), "title": "x" * 500})

    assert record.title is None
    assert record.display_name == "s01"


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------

def _image(tmp_path):
    path = tmp_path / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint8))
    return path


@pytest.fixture
def client(tmp_path):
    client = plexora.app.test_client()
    response = client.post("/import/sample", json={
        "paths": [str(_image(tmp_path))], "name": "s01", "answers": {},
    })
    assert response.status_code == 200, response.get_data(as_text=True)
    return client


def _config():
    return json.loads(paths.config_path().read_text(encoding="utf-8"))


def test_the_edit_route_sets_a_title_and_nothing_else(client):
    before = _config()["s01"]

    response = client.post("/project/s01", json={"title": "  Tonsil   section 3 "})

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert (body["name"], body["title"], body["display_name"]) == (
        "s01", "Tonsil section 3", "Tonsil section 3")
    after = _config()
    # The key did not move, and the entry differs by exactly one field.
    assert list(after) == ["s01"]
    assert after["s01"] == {**before, "title": "Tonsil section 3"}
    assert (paths.project_dir("s01")).is_dir()


def test_the_edit_route_clears_a_title(client):
    client.post("/project/s01", json={"title": "Tonsil"})

    for clearing in ("", "s01"):
        client.post("/project/s01", json={"title": "Tonsil"})
        body = client.post("/project/s01", json={"title": clearing}).get_json()
        assert body["title"] is None and body["display_name"] == "s01"
        assert "title" not in _config()["s01"]


def test_a_save_that_does_not_mention_the_title_keeps_it(client):
    client.post("/project/s01", json={"title": "Tonsil"})

    client.post("/project/s01", json={"cellLayer": ""})

    assert Project.load("s01").title == "Tonsil"


@pytest.mark.parametrize("bad", ["bad\x07title", "x" * (MAX_TITLE + 1), 42])
def test_a_refused_title_is_a_400_and_writes_nothing(client, bad):
    before = _config()

    response = client.post("/project/s01", json={"title": bad})

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert response.get_json()["error"]
    assert _config() == before


def test_an_unknown_project_is_a_404(client):
    response = client.post("/project/nobody", json={"title": "Anything"})

    assert response.status_code == 404


def test_a_shared_project_cannot_be_retitled(tmp_path, monkeypatch):
    shared, = helpers.use_shared_roots(monkeypatch, tmp_path / "site")
    write_config(paths.config_path(shared), {"atlas": entry("atlas")})

    response = plexora.app.test_client().post("/project/atlas", json={"title": "Atlas"})

    assert response.status_code == 403
    assert "title" not in json.loads(paths.config_path(shared).read_text())["atlas"]


def test_open_viewers_of_the_project_are_told(client, monkeypatch):
    sent = []
    monkeypatch.setattr(viewer_sessions, "publish",
                        lambda *args, **kwargs: sent.append((args, kwargs)) or 1)

    client.post("/project/s01", json={"title": "Tonsil"})

    (args, kwargs), = sent
    assert args == ("s01", "core", "project.renamed",
                    {"name": "s01", "title": "Tonsil", "display_name": "Tonsil"})
    assert kwargs == {"origin": "server"}


# --------------------------------------------------------------------------
# Where projects are listed
# --------------------------------------------------------------------------

def test_the_library_and_recent_list_show_the_title(client):
    from plexora.server.routes.page_routes import _recent_rows

    client.post("/project/s01", json={"title": "Tonsil"})

    card, = client.get("/projects").get_json()
    assert (card["name"], card["title"], card["displayName"]) == ("s01", "Tonsil", "Tonsil")
    with plexora.app.test_request_context():
        row, = _recent_rows()
    assert (row["name"], row["title"]) == ("s01", "Tonsil")


def test_the_dataset_listing_carries_member_titles(client):
    client.post("/datasets", json={"name": "Cohort", "projects": ["s01"]})
    client.post("/project/s01", json={"title": "Tonsil"})

    body = client.get("/datasets").get_json()

    assert body["titles"] == {"s01": "Tonsil"}
    assert body["datasets"][0]["projects"] == ["s01"]


def test_the_viewer_and_edit_pages_say_the_title(client):
    client.post("/project/s01", json={"title": "Tonsil <3"})

    viewer = client.get("/s01").get_data(as_text=True)
    assert "<title>Tonsil &lt;3</title>" in viewer
    assert 'data-plexora-datasource="s01"' in viewer
    assert 'data-plexora-title="Tonsil &lt;3"' in viewer
    edit = client.get("/edit_config/s01").get_data(as_text=True)
    assert "<h1>Tonsil &lt;3</h1>" in edit
    assert 'id="edit_title"' in edit


def test_list_projects_reports_and_matches_the_title(client):
    from plexora.agent import registry

    client.post("/project/s01", json={"title": "Tonsil section 3"})
    registry.discover([])
    session = AgentSession()

    everything = invoke(session, "list_projects", {})
    by_title = invoke(session, "list_projects", {"query": "tonsil"})
    by_name = invoke(session, "list_projects", {"query": "s0"})
    neither = invoke(session, "list_projects", {"query": "spleen"})

    assert everything["ok"], everything
    card, = everything["result"]["projects"]
    assert (card["name"], card["title"]) == ("s01", "Tonsil section 3")
    assert [c["name"] for c in by_title["result"]["projects"]] == ["s01"]
    assert [c["name"] for c in by_name["result"]["projects"]] == ["s01"]
    assert neither["result"]["projects"] == []
