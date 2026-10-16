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


def test_a_panel_answer_teaches_every_later_panel():
    from plexora.plugins.gating.server.autogate import context

    first = context.build(["PanCK", "CDX2", "Mystery1"])
    assert first["unresolved"] == ["CDX2", "Mystery1"]
    context.apply_entry(first, "CDX2", {"role": "tumour_stromal", "compartment": "nuclear",
                                        "partners": [{"marker": "PanCK", "relation": "subset",
                                                      "confidence": "moderate"}]}, source="ai")
    assert context.learn_from(first, ["CDX2", "Mystery1"], source="ai") == ["CDX2"]
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
