"""The marker vocabulary's learned layer: what a model or the user said about
a marker the shipped file does not know, kept for every later panel."""

import pytest

from plexora.ai import vocabulary


@pytest.fixture(autouse=True)
def _fresh_vocabulary():
    vocabulary.clear_cache()
    yield
    vocabulary.clear_cache()


def _cdx2(**extra):
    return {"canonical": "CDX2", "role": "tumour_stromal", "compartment": "nuclear",
            "partners": [{"marker": "PanCK", "relation": "subset", "confidence": "moderate"}],
            **extra}


def test_a_learned_marker_is_known_under_its_spellings():
    assert vocabulary.canonical("CDX2") is None
    shipped = vocabulary.version()
    assert vocabulary.learn([_cdx2()], about={"agent": "test"}) == ["CDX2"]
    assert vocabulary.canonical("CDX2") == "CDX2"
    assert vocabulary.canonical("cdx2_c3") == "CDX2"            # a cycle suffix folds
    assert vocabulary.canonical("CDX2", learned=False) is None  # the shipped layer alone
    assert vocabulary.origin("CDX2") == "learned"
    assert vocabulary.origin("CD3") == "shipped"
    assert vocabulary.version() != shipped                      # contexts rebuild
    stored = vocabulary.learned()[0]
    assert stored["learned"]["source"] == "ai" and stored["learned"]["agent"] == "test"
    assert vocabulary.learned_path().is_file()


def test_an_edited_shipped_caveat_changes_the_version(tmp_path, monkeypatch):
    """A stored panel context rebuilds on a new version; an edit that left the
    file's `version` alone kept the old caveat (lsp11385 PCNA, 2026-10-20)."""
    edited = tmp_path / "markers.yaml"
    edited.write_text(vocabulary.PATH.read_text(encoding="utf-8").replace(
        "long-lived protein", "long-lived protein, edited"), encoding="utf-8")
    before = vocabulary.version()
    monkeypatch.setattr(vocabulary, "PATH", edited)
    vocabulary.clear_cache()
    try:
        assert vocabulary.version() != before
        assert vocabulary.version().split(".")[0] == before.split(".")[0]
    finally:
        monkeypatch.undo()
        vocabulary.clear_cache()


def test_the_first_account_stands_unless_the_user_says_otherwise():
    vocabulary.learn([_cdx2()])
    assert vocabulary.learn([_cdx2(role="state")], source="ai") == []
    assert vocabulary.load()["entries"]["CDX2"]["role"] == "tumour_stromal"
    assert vocabulary.learn([_cdx2(role="state")], source="user") == ["CDX2"]
    assert vocabulary.load()["entries"]["CDX2"]["role"] == "state"
    # A model never replaces the user's word either.
    assert vocabulary.learn([_cdx2()], source="ai") == []


def test_the_shipped_vocabulary_is_never_shadowed():
    assert vocabulary.learn([{"canonical": "CD3", "role": "state"}]) == []
    assert vocabulary.learn([{"canonical": "cd3", "role": "state"}]) == []
    assert vocabulary.load()["entries"]["CD3"]["role"] != "state"
    # Nor is an entry outside the schema written.
    with pytest.raises(ValueError):
        vocabulary.learn([{"canonical": "Odd1", "role": "lineage"}])


def test_forgetting_a_marker_has_it_asked_again():
    vocabulary.learn([_cdx2()])
    assert vocabulary.forget("cdx2")
    assert vocabulary.canonical("CDX2") is None
    assert not vocabulary.forget("CDX2")


def test_a_shared_folder_is_where_the_lab_learns(tmp_path, monkeypatch):
    shared = tmp_path / "lab" / "vocabulary"
    shared.mkdir(parents=True)
    monkeypatch.setenv(vocabulary.ENV_LEARNED, str(shared))
    vocabulary.clear_cache()
    vocabulary.learn([_cdx2()])
    assert (shared / "learned_markers.yaml").is_file()


def test_plain_name_drops_position_and_fluorophore():
    assert vocabulary.plain_name("03_CDX2_AF488") == "CDX2"
    assert vocabulary.plain_name("CDX2_ArgoFluor555") == "CDX2"
    assert vocabulary.plain_name("HLADRA") == "HLADRA"


def test_a_panel_keeps_its_stored_context_when_a_marker_is_learned():
    from plexora.plugins.gating.server.autogate import context

    panel = ["CD3", "CDX2", "Mystery1"]
    before = context.panel_hash(panel)
    vocabulary.learn([_cdx2()])
    assert context.panel_hash(panel) == before


def test_a_confirmed_panel_answer_teaches_every_later_panel():
    from plexora.plugins.gating.server.autogate import context

    first = context.build(["PanCK", "CDX2", "Mystery1"])
    assert first["unresolved"] == ["CDX2", "Mystery1"]
    fields = {"role": "tumour_stromal", "compartment": "nuclear",
              "partners": [{"marker": "PanCK", "relation": "subset",
                            "confidence": "moderate"}]}
    context.apply_entry(first, "CDX2", dict(fields), source="ai")
    # A reader's account stays with its project ...
    assert context.learn_from(first, ["CDX2", "Mystery1"], source="ai") == []
    # ... until a person confirms it.
    context.apply_entry(first, "CDX2", dict(fields), source="user")
    assert context.learn_from(first, ["CDX2", "Mystery1"], source="user") == ["CDX2"]
    # Another panel: CDX2 is known now (from the learned layer), its partner
    # named by the panel's own column.
    other = context.build(["CDX2_c2", "PanCK", "CD3"])
    assert other["unresolved"] == []
    entry = other["entries"]["CDX2_c2"]
    assert entry["source"] == "learned" and entry["compartment"] == "nuclear"
    assert entry["partners"] == [{"marker": "PanCK", "relation": "subset",
                                  "confidence": "moderate"}]
    # A later model's account of it is kept as a suggestion, not used.
    context.apply_entry(other, "CDX2_c2", {"role": "state"}, source="ai")
    assert other["entries"]["CDX2_c2"]["role"] == "tumour_stromal"
    assert "CDX2_c2" in other["ai_suggestions"]


def test_a_marker_says_whether_its_stain_is_fibrous():
    """markers.yaml `morphology`: smooth muscle and vessel walls cross the
    masks beside them; a classifier check reads such a marker strictly."""
    from plexora.plugins.gating.server.autogate import context

    entries = vocabulary.load()["entries"]
    assert entries["aSMA"]["morphology"] == "fibrous"
    assert entries["CD31"]["morphology"] == "fibrous"
    assert "morphology" not in entries["CD3"]
    panel = context.build(["aSMA", "CD3", "CDX2"])
    assert panel["entries"]["aSMA"]["morphology"] == "fibrous"
    assert panel["entries"]["CD3"]["morphology"] == "cell"
    assert panel["entries"]["CDX2"]["morphology"] is None
    # The upfront step fills it for a marker the vocabulary does not know,
    # and is kept to the vocabulary's words.
    context.apply_entry(panel, "CDX2", {"role": "tumour_stromal", "morphology": "fibrous"},
                        source="ai")
    assert panel["entries"]["CDX2"]["morphology"] == "fibrous"
    with pytest.raises(ValueError):
        context.apply_entry(panel, "CDX2", {"morphology": "stringy"}, source="user")
    # ... and, once a person confirms it, teaches it to the learned layer.
    assert context.learn_from(panel, ["CDX2"], source="ai") == []
    context.apply_entry(panel, "CDX2", {"role": "tumour_stromal", "morphology": "fibrous"},
                        source="user")
    assert context.learn_from(panel, ["CDX2"], source="user") == ["CDX2"]
    assert vocabulary.load()["entries"]["CDX2"]["morphology"] == "fibrous"
    # An agent's word on a marker the vocabulary knows is not used.
    context.apply_entry(panel, "aSMA", {"role": "tumour_stromal", "morphology": "cell"},
                        source="ai")
    assert panel["entries"]["aSMA"]["morphology"] == "fibrous"
