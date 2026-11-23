"""A relation true of normal cells that tumour cells break (`exclude_contexts`) or keep only in part
(`weak_contexts`), as Plexora reads it (graph 2026-11-23.1, the cell-cycle round). With no tissue named the
vocabulary leaves the first out and grades the second low; a named context lays each on at its grade there."""

import pytest

from plexora.ai import vocabulary


@pytest.fixture(autouse=True)
def _fresh():
    vocabulary.clear_cache()
    yield
    vocabulary.clear_cache()


def _partner(entry, other, relation):
    return next((p for p in entry.get("partners") or () if p["marker"] == other and p["relation"] == relation),
                None)


def _relation(context, a, b, relation):
    return next((r for r in context["relations"]
                 if r["relation"] == relation and {r["a"], r["b"]} == {a, b}), None)


def test_the_vocabulary_leaves_out_what_tumours_break_and_weakens_what_they_keep_in_part():
    from plexora.plugins.gating.server.autogate import biology

    p16, ki67, prb = (vocabulary.canonical(n) for n in ("p16", "Ki67", "pRB"))
    # p16 and Ki-67 share cells in HPV-transformed and RB-deficient tumours: not stated with no context.
    assert _partner(vocabulary.entry(p16), ki67, "exclusive") is None
    # pRB within Ki-67 holds in tumours only weakly: low with no context.
    assert _partner(vocabulary.entry(prb), ki67, "subset")["confidence"] == "low"
    contexts = biology.load()["contexts"]
    assert _relation(contexts["lymph node"], p16, ki67, "exclusive")["confidence"] == "high"
    assert _relation(contexts["carcinoma"], p16, ki67, "exclusive") is None
    assert _relation(contexts["colorectal adenocarcinoma"], prb, ki67, "subset")["confidence"] == "low"
    assert _relation(contexts["lymph node"], prb, ki67, "subset")["confidence"] == "moderate"
