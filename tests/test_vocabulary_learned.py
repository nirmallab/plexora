"""The marker vocabulary's learned layer: what a model or the user said about
a marker the shipped marker graph does not let Plexora gate, kept in the
graph's overlay for every later panel (and for SCIMAP Pro)."""

import json

import pytest

from plexora.ai import vocabulary


@pytest.fixture(autouse=True)
def _fresh_vocabulary():
    vocabulary.clear_cache()
    yield
    vocabulary.clear_cache()


def _satb2(**extra):
    return {"canonical": "SATB2", "role": "tumour_stromal", "compartment": "nuclear",
            "partners": [{"marker": "PanCK", "relation": "subset", "confidence": "moderate"}],
            **extra}


def test_a_learned_marker_is_known_under_its_spellings():
    assert vocabulary.canonical("SATB2") is None
    shipped = vocabulary.version()
    assert vocabulary.learn([_satb2()], about={"agent": "test"}) == ["SATB2"]
    assert vocabulary.canonical("SATB2") == "SATB2"
    assert vocabulary.canonical("satb2_c3") == "SATB2"            # a cycle suffix folds
    assert vocabulary.canonical("SATB2", learned=False) is None  # the shipped layer alone
    assert vocabulary.origin("SATB2") == "learned"
    assert vocabulary.origin("CD3") == "shipped"
    assert vocabulary.version() != shipped                      # contexts rebuild
    stored = vocabulary.learned()[0]
    assert stored["learned"]["source"] == "ai" and stored["learned"]["agent"] == "test"
    assert vocabulary.learned_path().is_file()


def test_an_edited_shipped_caveat_changes_the_version(monkeypatch):
    """A stored panel context rebuilds on a new version; an edit that left the
    graph's `version` alone kept the old caveat (lsp11385 PCNA, 2026-10-20)."""
    from biocognia_markers import graph

    text = graph._shipped_text()
    before = vocabulary.version()
    monkeypatch.setattr(graph, "_shipped_text", lambda: text.replace(
        "long-lived protein", "long-lived protein, edited"))
    vocabulary.clear_cache()
    try:
        assert vocabulary.version() != before
        assert vocabulary.version().rsplit(".", 1)[0] == before.rsplit(".", 1)[0]
        assert "edited" in " ".join(vocabulary.entry("PCNA")["caveats"])
    finally:
        monkeypatch.setattr(graph, "_shipped_text", graph._shipped_text)
        vocabulary.clear_cache()


def test_the_first_account_stands_unless_the_user_says_otherwise():
    vocabulary.learn([_satb2()])
    assert vocabulary.learn([_satb2(role="state")], source="ai") == []
    assert vocabulary.load()["entries"]["SATB2"]["role"] == "tumour_stromal"
    assert vocabulary.learn([_satb2(role="state")], source="user") == ["SATB2"]
    assert vocabulary.load()["entries"]["SATB2"]["role"] == "state"
    # A model never replaces the user's word either.
    assert vocabulary.learn([_satb2()], source="ai") == []


def test_the_shipped_vocabulary_is_never_shadowed():
    assert vocabulary.learn([{"canonical": "CD3", "role": "state"}]) == []
    assert vocabulary.learn([{"canonical": "cd3", "role": "state"}]) == []
    assert vocabulary.load()["entries"]["CD3"]["role"] != "state"
    # Nor is an entry outside the schema written.
    with pytest.raises(ValueError):
        vocabulary.learn([{"canonical": "Odd1", "role": "lineage"}])


def test_forgetting_a_marker_has_it_asked_again():
    vocabulary.learn([_satb2()])
    assert vocabulary.forget("satb2")
    assert vocabulary.canonical("SATB2") is None
    assert not vocabulary.forget("SATB2")


def test_a_shared_folder_is_where_the_lab_learns(tmp_path, monkeypatch):
    shared = tmp_path / "lab" / "markers"
    monkeypatch.setenv("BIOCOGNIA_MARKER_GRAPH", str(shared))
    vocabulary.clear_cache()
    vocabulary.learn([_satb2()])
    assert (shared / "overlay.json").is_file()


def test_a_lab_folder_from_before_the_graph_still_holds_what_it_learns(tmp_path, monkeypatch):
    shared = tmp_path / "lab" / "vocabulary"
    shared.mkdir(parents=True)
    monkeypatch.delenv("BIOCOGNIA_MARKER_GRAPH")
    monkeypatch.setenv(vocabulary.ENV_LEARNED, str(shared))
    vocabulary.clear_cache()
    vocabulary.learn([_satb2()])
    assert (shared / "overlay.json").is_file()


def test_the_learned_file_from_before_the_graph_moves_into_the_overlay(tmp_path):
    legacy = tmp_path / "vocabulary" / "learned_markers.yaml"
    legacy.parent.mkdir()
    legacy.write_text(
        "version: '1'\nmarkers:\n"
        "- canonical: SATB2\n  role: tumour_stromal\n  compartment: nuclear\n"
        "  partners:\n  - {marker: PanCK, relation: subset, confidence: moderate}\n"
        "  learned: {source: user, at: '2026-10-18T14:34:00Z', project: p1}\n"
        "- canonical: CDH17\n  role: tumour_stromal\n  compartment: membrane\n"
        "  learned: {source: ai, agent: planner}\n", encoding="utf-8")
    vocabulary.clear_cache()
    assert vocabulary.canonical("SATB2") == "SATB2"
    assert not legacy.exists() and legacy.with_name("learned_markers.yaml.migrated").is_file()
    said = {e["canonical"]: e["learned"] for e in vocabulary.learned()}
    assert said["SATB2"]["source"] == "user" and said["SATB2"]["project"] == "p1"
    assert said["CDH17"] == {**said["CDH17"], "source": "ai", "agent": "planner"}
    assert vocabulary.entry("SATB2")["partners"] == [{"marker": "PanCK", "relation": "subset",
                                                      "confidence": "moderate"}]
    # SCIMAP Pro reads the same overlay.
    import biocognia_markers

    graph = biocognia_markers.load()
    assert graph.markers["m:satb2"]["class"] == "lineage"
    assert graph.markers["m:cdh17"]["compartment"] == "membrane"


def test_the_graph_keeps_plexoras_names_and_roles():
    entries = vocabulary.load()["entries"]
    assert {"PanCK", "Ki67", "PD1", "aSMA", "DNA"} <= set(entries)
    assert entries["FOXP3"]["role"] == "state"          # a lineage marker in the graph
    assert entries["aSMA"]["role"] == "tumour_stromal"
    assert entries["CD57"]["role"] == "lineage_other"
    assert vocabulary.canonical("Pan-cytokeratin") == "PanCK"
    assert vocabulary.canonical("PTPRC") == "CD45"      # the gene of CD45RA and CD45RO too
    # Every marker the graph places in the cell is known (MPO came from SCIMAP Pro's library); one the
    # graph does not hold is asked about.
    assert vocabulary.entry("MPO")["compartment"] == "cytoplasmic"
    assert vocabulary.canonical("SATB2") is None


def test_an_agents_unchecked_relation_is_not_a_partner_until_a_person_states_it(tmp_path):
    from biocognia_markers import overlay

    edge = {"id": "subset:m:tigit:m:cd8", "type": "subset", "from": "m:tigit", "to": "m:cd8",
            "confidence": "strong"}
    overlay.write({"edges": [edge]}, source="agent")
    vocabulary.clear_cache()
    assert ("CD8", "subset") not in {(p["marker"], p["relation"])
                                      for p in vocabulary.entry("TIGIT")["partners"]}
    overlay.write({"edges": [edge]}, source="user")
    vocabulary.clear_cache()
    assert {"marker": "CD8", "relation": "subset", "confidence": "high"} \
        in vocabulary.entry("TIGIT")["partners"]


def test_plain_name_drops_position_and_fluorophore():
    assert vocabulary.plain_name("03_SATB2_AF488") == "SATB2"
    assert vocabulary.plain_name("SATB2_ArgoFluor555") == "SATB2"
    assert vocabulary.plain_name("HLADRA") == "HLADRA"


def test_a_panel_keeps_its_stored_context_when_a_marker_is_learned():
    from plexora.plugins.gating.server.autogate import context

    panel = ["CD3", "SATB2", "Mystery1"]
    before = context.panel_hash(panel)
    vocabulary.learn([_satb2()])
    assert context.panel_hash(panel) == before


def test_a_confirmed_panel_answer_teaches_every_later_panel():
    from plexora.plugins.gating.server.autogate import context

    first = context.build(["PanCK", "SATB2", "Mystery1"])
    assert first["unresolved"] == ["SATB2", "Mystery1"]
    fields = {"role": "tumour_stromal", "compartment": "nuclear",
              "partners": [{"marker": "PanCK", "relation": "subset",
                            "confidence": "moderate"}]}
    context.apply_entry(first, "SATB2", dict(fields), source="ai")
    # A reader's account stays with its project ...
    assert context.learn_from(first, ["SATB2", "Mystery1"], source="ai") == []
    # ... until a person confirms it.
    context.apply_entry(first, "SATB2", dict(fields), source="user")
    assert context.learn_from(first, ["SATB2", "Mystery1"], source="user") == ["SATB2"]
    # Another panel: SATB2 is known now (from the learned layer), its partner
    # named by the panel's own column.
    other = context.build(["SATB2_c2", "PanCK", "CD3"])
    assert other["unresolved"] == []
    entry = other["entries"]["SATB2_c2"]
    assert entry["source"] == "learned" and entry["compartment"] == "nuclear"
    assert entry["partners"] == [{"marker": "PanCK", "relation": "subset",
                                  "confidence": "moderate"}]
    # A later model's account of it is kept as a suggestion, not used.
    context.apply_entry(other, "SATB2_c2", {"role": "state"}, source="ai")
    assert other["entries"]["SATB2_c2"]["role"] == "tumour_stromal"
    assert "SATB2_c2" in other["ai_suggestions"]


def test_a_marker_says_whether_its_stain_is_fibrous():
    """The graph's `morphology`: smooth muscle and vessel walls cross the
    masks beside them; a classifier check reads such a marker strictly."""
    from plexora.plugins.gating.server.autogate import context

    entries = vocabulary.load()["entries"]
    assert entries["aSMA"]["morphology"] == "fibrous"
    assert entries["CD31"]["morphology"] == "fibrous"
    assert "morphology" not in entries["CD3"]
    panel = context.build(["aSMA", "CD3", "SATB2"])
    assert panel["entries"]["aSMA"]["morphology"] == "fibrous"
    assert panel["entries"]["CD3"]["morphology"] == "cell"
    assert panel["entries"]["SATB2"]["morphology"] is None
    # The upfront step fills it for a marker the vocabulary does not know,
    # and is kept to the vocabulary's words.
    context.apply_entry(panel, "SATB2", {"role": "tumour_stromal", "morphology": "fibrous"},
                        source="ai")
    assert panel["entries"]["SATB2"]["morphology"] == "fibrous"
    with pytest.raises(ValueError):
        context.apply_entry(panel, "SATB2", {"morphology": "stringy"}, source="user")
    # ... and, once a person confirms it, teaches it to the learned layer.
    assert context.learn_from(panel, ["SATB2"], source="ai") == []
    context.apply_entry(panel, "SATB2", {"role": "tumour_stromal", "morphology": "fibrous"},
                        source="user")
    assert context.learn_from(panel, ["SATB2"], source="user") == ["SATB2"]
    assert vocabulary.load()["entries"]["SATB2"]["morphology"] == "fibrous"
    # An agent's word on a marker the vocabulary knows is not used.
    context.apply_entry(panel, "aSMA", {"role": "tumour_stromal", "morphology": "cell"},
                        source="ai")
    assert panel["entries"]["aSMA"]["morphology"] == "fibrous"


def test_the_users_account_replaces_a_models_relations_too():
    vocabulary.learn([_satb2(partners=[{"marker": "CD45", "relation": "exclusive",
                                       "confidence": "high"}])])
    assert any(p["marker"] == "CD45" for p in vocabulary.entry("SATB2")["partners"])
    assert vocabulary.learn([_satb2()], source="user") == ["SATB2"]
    assert [p["marker"] for p in vocabulary.entry("SATB2")["partners"]] == ["PanCK"]
    assert not any(p["marker"] == "SATB2" for p in vocabulary.entry("CD45")["partners"])
