"""validate_scope: requests in tool words, purpose words and domain language."""

import pytest

from plexora.agent import AgentSession, Policy, registry
from plexora.agent.policy import classify_scope
from plexora.agent.tasks import marker_terms, task_for
from tests.agent_fixtures import make_synthetic_project

#: These exercise Paid (AI) capabilities, so they run with a test licence
#: installed; what Free refuses is tests/test_licensing_enforcement.py's job.
pytestmark = pytest.mark.paid


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating", "roi"])
    return AgentSession()


def _scope(session, request, **kwargs):
    return classify_scope(session, request, project="synth", **kwargs)


def test_a_domain_phrased_request_is_recommended_not_refused(session):
    answer = _scope(session, "are these T cells exhausted?")
    assert answer["state"] == "can_recommend"
    assert answer["matched_by"] == "task" and answer["task"] == "phenotyping"
    assert answer["capabilities"], answer
    assert [item["key"] for item in answer["missing"]["_task"]] == ["marker", "threshold"]
    assert "list_markers" in answer["missing"]["_task"][0]["label"]


def test_naming_a_marker_the_project_has_settles_which_marker(session):
    answer = _scope(session, "are the CD8 cells exhausted?")
    assert answer["state"] == "can_recommend"
    assert [item["key"] for item in answer["missing"]["_task"]] == ["threshold"]


def test_purpose_words_match_when_names_and_tags_do_not(session):
    # "outlines" is in render_region's purpose, not in any name or tag.
    answer = _scope(session, "draw outlines")
    assert answer["matched_by"] in ("tags", "purpose")
    answer = _scope(session, "outlines")
    assert answer["matched_by"] == "purpose", answer
    assert "render_region" in answer["capabilities"]


def test_what_plexora_does_not_do_stays_outside_its_domain(session):
    velocity = _scope(session, "run a single-cell RNA velocity analysis")
    assert velocity["state"] == "outside_domain"
    enrichment = _scope(session, "neighbourhood enrichment")
    assert enrichment["state"] == "outside_domain"
    assert enrichment["task"] == "neighbourhood" and "SCIMAP" in enrichment["reason"]


def test_tool_words_still_answer_first(session):
    gate = _scope(session, "gate CD8")
    assert gate["state"] == "can_execute" and gate["matched_by"] == "tags"
    markers = _scope(session, "what markers does it have")
    assert markers["state"] == "can_analyze"


def test_a_task_never_implies_a_write_the_server_forbids(session):
    answer = _scope(session, "are these T cells exhausted?",
                    policy=Policy(allow_source_writes=False))
    tools = {cap.tool_name: cap for cap in registry.all_capabilities()}
    assert all(tools[name].permission == "read" for name in answer["capabilities"])


def test_task_and_marker_helpers():
    assert task_for({"exhausted", "cells"}).name == "phenotyping"
    assert task_for({"neighbourhood"}).name == "neighbourhood"
    assert task_for({"velocity"}) is None
    assert marker_terms({"cd8", "exhausted"}, ["DNA", "CD8"]) == ["CD8"]


# -- route_to: a task another application serves ---------------------------------


def test_a_task_nothing_here_serves_names_where_it_goes(session):
    answer = _scope(session, "neighbourhood enrichment")
    route = answer["route_to"]
    assert route["provider"] == "scimappro" and route["role"] == "spatial.neighborhood"
    assert route["via"] == "bridge_handoff" and route["reachable"] is False
    assert route["hint"]                       # how to install or start it
    assert answer["state"] == "outside_domain"


def test_a_reachable_analysis_peer_makes_it_a_recommendation(session):
    from spatialbridge import client
    from spatialbridge.adapter import Adapter

    class Analysis(Adapter):
        provider = "scimappro"

    client.register_inprocess("scimappro", Analysis)
    answer = _scope(session, "neighbourhood enrichment")
    assert answer["state"] == "can_recommend"
    assert answer["route_to"]["reachable"] is True


def test_an_installed_analysis_plugin_answers_in_process(tmp_path, monkeypatch):
    from tests.scimappro_fixtures import install_fake_scimappro, make_anndata_project

    install_fake_scimappro(monkeypatch)
    make_anndata_project(tmp_path)
    registry._reset_for_tests()
    try:
        registry.discover(["gating", "roi", "scimappro"])
        answer = classify_scope(AgentSession(), "neighbourhood enrichment", project="tonsil")
        assert "route_to" not in answer
        assert "scimappro_sp_spatial_neighbors" in answer["capabilities"]
        # Its write into the table is kept, and the answer says it needs confirm.
        assert answer["confirm"] == ["scimappro_sp_spatial_neighbors"]
        assert answer["state"] == "can_recommend"          # the server has no source writes
        assert "scimappro_sp_spatial_neighbors" in answer["not_permitted"]
    finally:
        registry._reset_for_tests()
