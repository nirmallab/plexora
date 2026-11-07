"""One project open in two apps at once is said so (server/models/open_marks.py)."""

import json
import time

import pytest

from plexora import paths
from plexora.server.models import open_marks, sources


@pytest.fixture
def project(tmp_path):
    folder = paths.project_state_dir("slide")
    folder.mkdir(parents=True)
    return folder


def test_another_apps_fresh_mark_is_reported_and_its_own_never(project, monkeypatch):
    monkeypatch.setattr(sources, "app_key", lambda: "website")
    open_marks.beat("slide")
    assert open_marks.others("slide") == []
    monkeypatch.setattr(sources, "app_key", lambda: "host:LAB")
    open_marks.beat("slide")
    found = open_marks.others("slide")
    assert [other["app"] for other in found] == ["website"] and found[0]["where"] == "the website"
    monkeypatch.setattr(sources, "app_key", lambda: "website")
    assert [other["where"] for other in open_marks.others("slide")] == ["Plexora on LAB"]


def test_a_stale_mark_is_an_app_that_closed(project, monkeypatch):
    (project / ".open-website.json").write_text(json.dumps({"app": "website",
                                                            "beat": time.time() - open_marks.FRESH_SECONDS - 1}))
    monkeypatch.setattr(sources, "app_key", lambda: "host:LAB")
    assert open_marks.others("slide") == []


def test_the_route_marks_and_answers(project, monkeypatch):
    import plexora

    client = plexora.app.test_client()
    monkeypatch.setattr(sources, "app_key", lambda: "host:LAB")
    (project / ".open-website.json").write_text(json.dumps({"app": "website", "beat": time.time()}))
    answer = client.get("/project/slide/open_elsewhere").get_json()
    assert [other["app"] for other in answer["open"]] == ["website"]
    assert (project / ".open-host_LAB.json").is_file()
