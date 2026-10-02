"""The Context Interpreter (plexora/ai/context.py) and the harness's use of it.

What is pinned: the model's reading is settled against the user's words and
the panel -- a restriction stands only with an explicit cue and only to the
panel's markers; a tissue, disease or species the note did not state is
dropped; hedges are kept; the original text always rides along. In the
harness: a note is one cheap `text_routine` call before the session (no
images, no run, a small output budget), becomes the session's `biology`, and
narrows the markers only when it says so; no note, no call.
"""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.ai import context
from plexora.ai.context import Term, settle
from plexora.ai.harness.decision import GATING, GatingOptions, GatingRun
from plexora.ai.harness.trace import TraceStore
from tests.ai_harness_fixtures import FakeGateway
from tests.autogate_fixtures import make_gating_project
from tests.test_ai_harness import client
from tests.test_gating_session import Oracle

PANEL = [Term("CD3", "CD3", "T cells"), Term("CD8", "CD8", "cytotoxic T cells"),
         Term("SOX10", "SOX10", "melanocytes; melanoma"), Term("NCAM", "CD56", "NK cells"),
         Term("CD45", "CD45", "immune (all leukocytes)")]


def reading(**overrides):
    raw = {"normalized_text": "", "context": {}, "markers_mentioned": [],
           "gating_scope": {"mode": "all_markers", "requested_markers": []},
           "corrected_terms": {}, "ambiguities": [], "confidence": "high"}
    for key, value in overrides.items():
        if key in ("tissue", "disease", "species", "populations_of_interest"):
            raw["context"][key] = value
        elif key in ("mode", "requested_markers", "excluded_markers"):
            raw["gating_scope"][key] = value
        else:
            raw[key] = value
    return raw


# -- settle: the guards ------------------------------------------------------------


def test_a_misspelt_note_is_normalised_and_still_gates_every_marker():
    text = "this is melnoma skn sample gate imune and tumor cells"
    out = settle(reading(normalized_text="This is a melanoma skin tissue sample. The user is interested "
                                         "in immune and tumour populations.",
                         tissue="skin", disease="melanoma", populations_of_interest=["immune", "tumor"],
                         corrected_terms={"melnoma": "melanoma", "skn": "skin", "imune": "immune"}),
                 text, PANEL)
    assert (out.tissue, out.disease) == ("skin", "melanoma")
    assert out.populations_of_interest == ["immune", "tumor"]
    assert out.scope == "all_markers" and out.requested == [] and out.ambiguities == []
    assert out.original_text == text
    record = out.as_dict()
    assert record["original_text"] == text
    assert record["gating_scope"] == {"mode": "all_markers", "requested_markers": []}


def test_naming_populations_never_narrows_the_run_even_if_the_model_says_so():
    text = "melanoma, gate immune and tumor cells"
    out = settle(reading(mode="selected_markers", requested_markers=["CD45", "SOX10"]), text, PANEL)
    assert out.scope == "all_markers" and out.requested == []
    assert any("does not restrict" in a for a in out.ambiguities)


def test_an_explicit_restriction_names_panel_markers_only():
    out = settle(reading(mode="selected_markers", requested_markers=["CD3", "cd8", "SOX10", "CD99"]),
                 "Only gate CD3, CD8, SOX10 and CD99", PANEL)
    assert out.scope == "selected_markers" and out.requested == ["CD3", "CD8", "SOX10"]
    assert any("'CD99'" in a for a in out.ambiguities)


def test_a_restriction_resolves_canonical_names_to_the_panels_columns():
    out = settle(reading(mode="selected_markers", requested_markers=["CD56"]), "just gate CD56", PANEL)
    assert out.requested == ["NCAM"]


def test_a_restriction_to_nothing_in_the_panel_is_kept_as_a_restriction():
    out = settle(reading(mode="selected_markers", requested_markers=["CD99"]), "only gate CD99", PANEL)
    assert out.scope == "selected_markers" and out.requested == []
    with pytest.raises(context.ContextRefused):
        GATING.context_arguments(out, PANEL)


def test_markers_named_for_context_are_not_a_scope():
    out = settle(reading(markers_mentioned=["SOX10", "MART1"], disease="melanoma"),
                 "This is melanoma; SOX10 and MART1 may help identify tumor cells.", PANEL)
    assert out.markers_mentioned == ["SOX10"]
    assert out.scope == "all_markers"
    assert any("'MART1'" in a for a in out.ambiguities)


def test_a_tissue_the_note_did_not_state_is_dropped():
    out = settle(reading(disease="melanoma", tissue="skin"), "melanoma", PANEL)
    assert out.disease == "melanoma" and out.tissue is None
    assert any("tissue 'skin' was not stated" in a for a in out.ambiguities)


def test_uncertainty_is_kept_not_resolved():
    out = settle(reading(disease="possible melanoma"), "possibly melanoma, lymph node", PANEL)
    assert out.disease == "possible melanoma"


def test_an_unlisted_one_letter_correction_still_counts_as_stated():
    out = settle(reading(tissue="skin", disease="melanoma"), "melnoma skn", PANEL)
    assert (out.tissue, out.disease) == ("skin", "melanoma")


def test_an_exclusion_needs_its_words_and_leaves_the_rest():
    said = settle(reading(excluded_markers=["CD45"]), "melanoma; skip CD45", PANEL)
    assert said.excluded == ["CD45"]
    assert GATING.context_arguments(said, PANEL)["markers"] == ["CD3", "CD8", "SOX10", "NCAM"]
    unsaid = settle(reading(excluded_markers=["CD45"]), "melanoma with CD45 infiltrate", PANEL)
    assert unsaid.excluded == [] and "markers" not in GATING.context_arguments(unsaid, PANEL)


def test_garbage_from_the_model_settles_to_the_safe_default():
    out = settle({"context": "skin", "gating_scope": [], "confidence": "very"}, "skin", PANEL)
    assert out.scope == "all_markers" and out.tissue is None and out.confidence == "low"


def test_gating_arguments_carry_the_original_beside_the_interpretation():
    out = settle(reading(normalized_text="A melanoma from the skin.", tissue="skin", disease="melanoma"),
                 "melanoma from the skn", PANEL)
    arguments = GATING.context_arguments(out, PANEL)
    assert "markers" not in arguments
    biology = arguments["biology"]
    assert biology["tissue"] == "skin" and biology["disease"] == "melanoma"
    assert biology["original"] == "melanoma from the skn"
    assert biology["notes"].startswith("A melanoma from the skin.")
    assert biology["interpretation"]["gating_scope"]["mode"] == "all_markers"


def test_the_prompt_is_small_and_names_the_panel():
    text = context.prompt("melanoma", PANEL)
    assert "NCAM (CD56): NK cells" in text and text.endswith("NOTE\nmelanoma")
    assert len(context.SYSTEM) < 2400 and context.MAX_TOKENS <= 1000


def test_no_note_is_no_call():
    class Refuses:
        def messages(self, *a, **k):
            raise AssertionError("an empty note must not reach the gateway")

    assert context.interpret("   ", PANEL, gateway=Refuses(), feature="gating",
                             idempotency_key="k") == (None, None)


# -- the harness -------------------------------------------------------------------------


MARKERS = ("CD3", "CD8", "CD20")


@pytest.fixture
def gating(tmp_path):
    registry.discover(["gating"])
    return make_gating_project(tmp_path, markers=MARKERS)


def brain_with(interpretation, info):
    oracle = Oracle(info)

    def brain(packet, body):
        if body["capability"] == context.CAPABILITY:
            return interpretation
        return oracle.answer(packet)
    return brain


def _session(session_id):
    status = invoke(AgentSession(), "gating_session_status", {"session_id": session_id})
    assert status["ok"], status
    return status["result"]


@pytest.mark.paid
def test_a_note_is_one_cheap_call_before_the_run_and_becomes_the_sessions_biology(gating, tmp_path):
    answer = reading(normalized_text="A tonsil sample; T cells are of interest.", tissue="tonsil",
                     populations_of_interest=["T cells"])
    with FakeGateway(brain_with(answer, gating)) as gateway:
        events = []
        summary = GatingRun(GatingOptions(project="gsynth", context="tonsil smaple, T cells matter"),
                            gateway=client(gateway), trace=TraceStore(tmp_path / "t.sqlite"),
                            on_event=events.append).run()
    assert summary["status"] == "done", summary
    first, rest = gateway.calls[0], gateway.calls[1:]
    body = first["body"]
    assert body["capability"] == "text_routine" and "model" not in body
    assert "run_id" not in body["context"] and body["context"]["agent"] == "context_interpreter"
    assert body["request"]["max_tokens"] == context.MAX_TOKENS
    assert body["request"]["output_schema"] == context.SCHEMA
    assert not any(b.get("type") == "image" for m in body["request"]["messages"] for b in m["content"])
    assert "NOTE\ntonsil smaple, T cells matter" in body["request"]["messages"][0]["content"][0]["text"]
    assert all(c["body"]["capability"] == "vision_judgement" for c in rest)
    # Every marker is still gated: naming a population is not a scope.
    assert {u["marker"] for u in _session(summary["session_id"])["units"]} == set(MARKERS)
    told = [e for e in events if e["event"] == "context"]
    assert len(told) == 1 and told[0]["session_id"] == summary["session_id"]
    assert told[0]["interpretation"]["original_text"] == "tonsil smaple, T cells matter"
    assert told[0]["units"] == len(MARKERS)
    status = _session(summary["session_id"])
    assert status["biology"]["original"] == "tonsil smaple, T cells matter"
    assert status["biology"]["said"]["tissue"] == "tonsil"


@pytest.mark.paid
def test_an_explicit_restriction_gates_only_those_markers(gating, tmp_path):
    answer = reading(mode="selected_markers", requested_markers=["CD3", "CD8"])
    with FakeGateway(brain_with(answer, gating)) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth", context="only gate cd3 and cd8"),
                            gateway=client(gateway), trace=TraceStore(tmp_path / "t.sqlite")).run()
    assert summary["status"] == "done", summary
    assert {u["marker"] for u in _session(summary["session_id"])["units"]} == {"CD3", "CD8"}
    assert gateway.runs["run_1"]["units"] == 2


@pytest.mark.paid
def test_an_unreadable_reply_passes_the_note_on_as_written(gating, tmp_path):
    with FakeGateway(brain_with("not json at all", gating)) as gateway:
        events = []
        summary = GatingRun(GatingOptions(project="gsynth", context="only gate CD3"),
                            gateway=client(gateway), trace=TraceStore(tmp_path / "t.sqlite"),
                            on_event=events.append).run()
    assert summary["status"] == "done", summary
    # Never a narrower run on a reading that failed.
    assert {u["marker"] for u in _session(summary["session_id"])["units"]} == set(MARKERS)
    told = next(e for e in events if e["event"] == "context")
    assert told["interpretation"]["source"] == "unprocessed"


@pytest.mark.paid
def test_a_restriction_to_markers_the_panel_lacks_stops_before_the_session(gating, tmp_path):
    answer = reading(mode="selected_markers", requested_markers=["SOX10"])
    with FakeGateway(brain_with(answer, gating)) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth", context="only gate SOX10"),
                            gateway=client(gateway), trace=TraceStore(tmp_path / "t.sqlite")).run()
    assert summary["status"] == "failed"
    assert "none of them is in this panel" in summary["reason"]
    assert len(gateway.calls) == 1 and not gateway.runs


def test_the_run_input_takes_the_note_and_the_route_passes_it(monkeypatch):
    from plexora.ai.harness.capabilities import RunInput

    assert RunInput(kind="gating", project="p", context="melanoma").context == "melanoma"
    with pytest.raises(Exception):
        RunInput(kind="gating", project="p", context="x" * 1001)
    import inspect

    from plexora.server.routes import ai_routes
    assert '"context"' in inspect.getsource(ai_routes.start_run)
    assert json.dumps(context.SCHEMA)
