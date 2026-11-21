"""The marker graph's context kinds reach a gating session.

Graph 2026-11-21.2 added `anatomical_site` and `process` contexts beside
`tissue` and `disease`; Plexora refused them, so every gating session start
failed ("marker graph context 'extramedullary haematopoiesis': kind is one of
('tissue', 'disease')"). Every kind the graph's schema accepts now loads and
is read like the others."""

from __future__ import annotations

import copy

from biocognia_markers import graph as graphmod

from plexora.plugins.gating.server.autogate import biology


def _graph_with(monkeypatch, contexts):
    real = graphmod.load

    def load(*args, **kwargs):
        g = copy.copy(real(*args, **kwargs))
        g.contexts = {**g.contexts, **contexts}
        return g

    monkeypatch.setattr(graphmod, "load", load)
    biology._load.cache_clear()


def test_every_kind_the_graph_accepts_is_accepted():
    assert set(getattr(graphmod, "CONTEXT_KINDS", ())) <= set(biology.KINDS)
    assert {"tissue", "disease", "anatomical_site", "process"} <= set(biology.KINDS)


def test_a_process_and_an_anatomical_site_load_and_are_matched(monkeypatch):
    _graph_with(monkeypatch, {
        "extramedullary haematopoiesis": {"kind": "process",
                                          "synonyms": ["extramedullary hematopoiesis", "EMH"],
                                          "affects": ["liver"]},
        "inner ear": {"kind": "anatomical_site", "synonyms": ["cochlea"]}})
    try:
        contexts = biology.load()["contexts"]
        assert contexts["extramedullary haematopoiesis"]["kind"] == "process"
        assert contexts["inner ear"]["kind"] == "anatomical_site"
        record = biology.resolve(tissue="cochlea", notes="extramedullary hematopoiesis")
        assert {"inner ear", "extramedullary haematopoiesis"} <= set(record["contexts"])
    finally:
        biology._load.cache_clear()


def test_the_shipped_graph_loads():
    biology._load.cache_clear()
    assert biology.load()["contexts"]


def test_a_context_is_named_by_whole_words_never_inside_one():
    """The graph's short names (ES, CHB, APL) were found inside other words
    once the text was folded: "primary melanoma of the skin" also said Ewing
    sarcoma, and its relations were laid over the panel."""
    biology._load.cache_clear()
    assert biology.match("primary melanoma of the skin") == ["melanoma", "skin"]
    assert biology.match("skin punch biopsy") == ["skin"]
    assert biology.match("tonsil smaple, T cells matter") == ["lymph node"]
    assert biology.match("primary cutaneous melanoma") == ["cutaneous melanoma"]
    assert biology.match("melanomas from the skin") == ["melanoma", "skin"]   # a plural
    assert "ewing sarcoma" in biology.match("Ewing sarcoma (ES) of bone")
    # An acronym is said in capitals: "as stated" is not AS (angiosarcoma).
    assert biology.match("primary melanoma of the skin, as stated by the user") == ["melanoma", "skin"]
    assert biology.match("AS of the scalp") == ["angiosarcoma"]
    assert biology.match("extramedullary hematopoiesis (EMH)") == ["extramedullary haematopoiesis"]
