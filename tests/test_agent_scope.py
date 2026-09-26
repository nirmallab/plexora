"""validate_scope: requests in tool words, purpose words and domain language."""

import pytest

from plexora.agent import AgentSession, Policy, registry
from plexora.agent.policy import classify_scope
from plexora.agent.tasks import marker_terms, task_for
from tests.agent_fixtures import make_synthetic_project


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
