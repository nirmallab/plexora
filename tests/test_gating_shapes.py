"""What a marker's own stain looks like (`SessionOptions.shapes`).

markers.yaml names the shapes of each marker's stain (`shapes`); a panel's
model fills them in for markers the vocabulary does not know. With shapes
on, a look is told them and how own stain looks for each (guide
`shape:<name>`), and a stain seen in another shape caps confidence. Off,
the looks are as they were.
"""

from types import SimpleNamespace

import pytest

from plexora.agent import registry

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def test_one_vocabulary_of_shapes():
    from plexora.ai import vocabulary
    from plexora.plugins.gating.server.autogate import library, schemas

    assert set(vocabulary.SHAPES) == set(schemas.STAIN_SHAPES)
    assert set(schemas.STAIN_SHAPES) <= set(schemas.PATTERNS)
    assert set(schemas.STAIN_SHAPES) <= set(schemas.OBSERVED_PATTERNS)
    assert set(schemas.SHAPE_READING) == set(schemas.STAIN_SHAPES) == set(schemas.SHAPE_GLOSS)
    assert library.SHAPES == schemas.STAIN_SHAPES + ("unknown",)
    for policy in schemas.COMPARTMENT_POLICY.values():
        assert set(policy["patterns"]) <= set(schemas.PATTERNS)
    for text in schemas.SHAPE_READING.values():
        assert not any(ch.isdigit() for ch in text)


def test_every_shipped_marker_has_shapes_its_compartment_allows():
    from plexora.ai import vocabulary
    from plexora.plugins.gating.server.autogate import schemas

    vocabulary.clear_cache()
    entries = vocabulary.load()["entries"]
    shipped = [e for e in entries.values() if e.get("origin") == "shipped"]
    assert len(shipped) >= 50
    for entry in shipped:
        shapes = entry.get("shapes")
        assert shapes, entry["canonical"]
        kind = schemas.OBSERVED_KINDS.get(shapes[0])
        fits = schemas.OBSERVED_FITS.get(entry.get("compartment"))
        if kind is not None and fits is not None:
            assert kind in fits, (entry["canonical"], shapes[0], entry["compartment"])


def test_shape_fits():
    from plexora.plugins.gating.server.autogate.schemas import shape_fits

    assert shape_fits("fibrous", None) is None                       # no opinion
    assert shape_fits("membrane", ["membrane"]) is True
    assert shape_fits("cytoplasmic", ["membrane"]) is True           # a small cell's ring
    assert shape_fits("fibrous", ["membrane"]) is False
    assert shape_fits("nuclear", ["membrane"]) is False
    assert shape_fits("dendritic", ["membrane", "dendritic"]) is True
    assert shape_fits("fibrous", ["fibrous"]) is True
    assert shape_fits("punctate", ["membrane"]) is None              # no kind: compartment
    assert shape_fits("none", ["nuclear"]) is True
    assert shape_fits("membrane", ["diffuse"]) is None               # nothing to hold it to


def test_a_learned_entry_keeps_only_known_shapes(tmp_path, monkeypatch):
    from plexora.ai import vocabulary

    monkeypatch.setenv(vocabulary.ENV_LEARNED, str(tmp_path / "learned.yaml"))
    vocabulary.clear_cache()
    try:
        written = vocabulary.learn([{"canonical": "XYZ1", "role": "state",
                                     "compartment": "cytoplasmic",
                                     "shapes": ["granular", "sparkly", "granular"]}])
        assert written == ["XYZ1"]
        assert vocabulary.entry("XYZ1")["shapes"] == ["granular"]
        # A hand edit gone wrong drops the words, not the entry.
        path = tmp_path / "learned.yaml"
        path.write_text(path.read_text().replace("- granular", "- glittery"))
        vocabulary.clear_cache()
        entry = vocabulary.entry("XYZ1")
        assert entry is not None and entry.get("shapes") is None
        with pytest.raises(ValueError):
            vocabulary.check({"canonical": "Q", "shapes": ["sparkly"]})
    finally:
        vocabulary.clear_cache()


def test_the_panel_carries_shapes_and_an_agent_fills_a_known_markers_gap():
    from plexora.plugins.gating.server.autogate import context

    panel = context.build(["CD3", "MYSTERY1", "aSMA"])
    assert panel["entries"]["CD3"]["shapes"] == ["membrane"]
    assert panel["entries"]["aSMA"]["shapes"] == ["fibrous"]
    assert panel["entries"]["MYSTERY1"]["shapes"] is None
    context.apply_entry(panel, "MYSTERY1", {"role": "state", "shapes": ["dendritic"]},
                        source="ai")
    assert panel["entries"]["MYSTERY1"]["shapes"] == ["dendritic"]
    with pytest.raises(ValueError):
        context.apply_entry(panel, "MYSTERY1", {"shapes": ["sparkly"]}, source="ai")
    # A known marker whose entry lacks shapes takes an agent's, and only them.
    panel["entries"]["CD3"]["shapes"] = None
    context.apply_entry(panel, "CD3", {"role": "context", "shapes": ["membrane"]},
                        source="ai")
    entry = panel["entries"]["CD3"]
    assert entry["shapes"] == ["membrane"] and entry["shapes_source"] == "ai"
    assert entry["role"] != "context"                                 # the rest not taken


def _engine(shapes):
    return SimpleNamespace(options={"shapes": shapes, "reading": "once"},
                           shapes_on=lambda: shapes)


def test_looks_name_the_shapes_only_with_shapes_on():
    from plexora.plugins.gating.server.autogate import packets

    unit = {"marker": "aSMA", "context": {"compartment": "cytoplasmic",
                                          "shapes": ["fibrous"]}}
    assert packets._stain_words(_engine(False), unit) == "cytoplasmic aSMA"
    assert packets._shape_keys(_engine(False), unit) == []
    assert packets._context_evidence(_engine(False), unit) == {"compartment": "cytoplasmic"}
    assert packets._stain_words(_engine(True), unit) == "fibrous aSMA"
    assert packets._shape_keys(_engine(True), unit) == ["shape:fibrous"]
    assert packets._context_evidence(_engine(True), unit)["shapes"] == ["fibrous"]
    assert packets._context_brief(unit) == {"compartment": "cytoplasmic"}
    assert packets._context_brief(unit, shapes=True)["shapes"] == ["fibrous"]
    guide = packets.reading_guide()
    assert all(f"shape:{s}" in guide for s in ("fibrous", "dendritic", "membrane"))


def test_a_look_is_held_to_the_markers_shapes():
    from plexora.plugins.gating.server.autogate import engine, transitions

    answer = SimpleNamespace(rows={"near": "own_stain"}, pattern="fibrous")
    # A compartment that has no fibres (nuclear) holds the look to its rule...
    asma = {"marker": "aSMA", "context": {"compartment": "nuclear", "shapes": ["fibrous"]}}
    transitions._read_look(asma, answer, "about_right", "x")
    assert "pattern_mismatch" in asma["look_flags"]
    # ...unless the look is held to the marker's own shapes.
    asma = {"marker": "aSMA", "context": {"compartment": "nuclear", "shapes": ["fibrous"]}}
    transitions._read_look(asma, answer, "about_right", "x", shapes=True)
    assert "pattern_mismatch" not in asma["look_flags"] and asma["look"]["fits"] is True
    cd3 = {"marker": "CD3", "context": {"compartment": "membrane", "shapes": ["membrane"]}}
    transitions._read_look(cd3, answer, "about_right", "x", shapes=True)
    assert "pattern_mismatch" in cd3["look_flags"] and cd3["look"]["fits"] is False
    assert engine.biology_agrees(cd3) is False


def test_a_scripted_look_answers_the_first_shape():
    from plexora.ai import bench

    evidence = {"rows": {"below": "x", "near": "x", "above": "x"},
                "context": {"compartment": "membrane", "shapes": ["dendritic", "membrane"]}}
    assert bench.rows_for("about_right", evidence)["pattern"] == "dendritic"
    evidence["context"].pop("shapes")
    assert bench.rows_for("about_right", evidence)["pattern"] == "membrane"
