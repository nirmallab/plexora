"""What a plugin can see of a sample's scene, and what it can ask for.

The rule this file pins: **core knows KINDS, plugins know MODALITIES.** A kind
is a rendering strategy and there are four of them; a modality is what the data
means and core must never hold the list, or every new instrument is a change in
core.

The consequence worth testing is `Requires.layers`. Declaring a modality is how
a tool stops appearing on samples it has nothing to do with -- and, when the
layer IS there but still building, how it reports that it is waiting rather than
that something is missing. The default is empty, so no plugin that predates this
changes behaviour, which is the other half of the guarantee.
"""

import pytest

from plexora import api
from plexora.api.plugin import Requires
from plexora.server.models import import_proposal, manifest
from plexora.server.models.project import LayerSpec

from tests import helpers


def _sample(**layer):
    project = helpers.project("demo")
    if layer:
        project = project.with_layer(LayerSpec(**layer))
    return project


def test_a_plugin_declaring_nothing_is_unaffected():
    """The whole compatibility claim in one test. Every `Requires()` in the
    tree has `layers=()`, so this is the behaviour of every existing plugin."""
    plain = helpers.project("demo")
    with_tx = _sample(id="tx", kind="points", modality="transcripts")
    assert Requires().applies_to(plain) is True
    assert Requires().applies_to(with_tx) is True
    assert Requires().missing_from(with_tx) == []


def test_a_tool_is_hidden_from_a_sample_that_cannot_have_its_data():
    """Today a transcripts tool lists on every project and its panel says it
    has nothing to show. Naming the modality is what turns that into not
    offering it -- which is `applies_to`'s job: could this EVER work here."""
    with_tx = _sample(id="tx", kind="points", modality="transcripts")
    plain = helpers.project("demo")

    wants = Requires(layers=("transcripts",))
    assert wants.applies_to(with_tx) is True
    assert wants.applies_to(plain) is False


def test_a_layer_still_building_is_reported_as_something_to_wait_for():
    """Not as something missing. The tool stays listed -- the data is there --
    and what it reports is a state the user can watch finish."""
    building = _sample(id="tx", kind="points", modality="transcripts",
                       status="pending")
    wants = Requires(layers=("transcripts",))

    assert wants.applies_to(building) is True
    missing = wants.missing_from(building)
    assert [r.kind for r in missing] == ["layer"]
    assert missing[0].key == "layer:transcripts"
    assert "being prepared" in missing[0].label


def test_a_failed_layer_says_so_in_the_question():
    failed = _sample(id="tx", kind="points", modality="transcripts",
                     status="failed")
    missing = Requires(layers=("transcripts",)).missing_from(failed)
    assert "failed" in missing[0].label


def test_a_ready_layer_asks_for_nothing():
    ready = _sample(id="tx", kind="points", modality="transcripts")
    assert Requires(layers=("transcripts",)).missing_from(ready) == []


def test_a_layer_with_an_unanswered_question_is_still_outstanding():
    """`unresolved` is a question recorded rather than an import refused, and
    a tool that needs the layer needs the question answered too."""
    partial = _sample(id="tx", kind="points", modality="transcripts",
                      unresolved=("table",))
    missing = Requires(layers=("transcripts",)).missing_from(partial)
    assert missing and missing[0].key == "layer:transcripts"


def test_a_kind_can_be_asked_for_instead_of_a_modality():
    """Both questions are real: a tool that draws over ANY points layer wants
    the kind, and one that interprets transcripts wants the modality."""
    spots = _sample(id="spots", kind="points", modality="visium_spots")
    assert Requires(layers=("kind:points",)).applies_to(spots) is True
    assert Requires(layers=("transcripts",)).applies_to(spots) is False


def test_the_synthesized_layers_count():
    """A plugin asking "does this sample have a mask" should not have to know
    that the mask is stored as a SegmentationSpec rather than in the layer
    list. `all_layers` is what it sees, and the mask is in it."""
    project = helpers.project("demo", segmentation="/derived/mask.zarr")
    assert Requires(layers=("mask",)).applies_to(project) is True


def test_the_api_answers_the_same_question_server_side():
    project = _sample(id="tx", kind="points", modality="transcripts")
    assert [l.id for l in api.layers(project, modality="transcripts")] == ["tx"]
    assert [l.id for l in api.layers(project, kind="image")] == ["__image__"]
    assert api.layer(project, "tx").modality == "transcripts"
    assert api.layer(project, "nope") is None

    described = api.sample(project)
    assert described["name"] == "demo"
    assert "transcripts" in described["modalities"]
    assert described["reference"] == "__image__"
    assert described["blank"] is False


def test_the_manifest_flattens_layers_for_a_ui():
    project = _sample(id="tx", kind="points", modality="transcripts",
                      status="pending", unresolved=("table",))
    rows = {row["id"]: row for row in manifest.layers(project)}
    assert rows["tx"]["status"] == "pending"
    assert rows["tx"]["unresolved"] == ["table"]
    assert rows["__image__"]["kind"] == "image"
    assert manifest.summary(project)["layers"] == {
        "count": 1, "modalities": ["transcripts"]}


def test_a_plugin_can_contribute_a_detector(monkeypatch, tmp_path):
    """So the next vendor format is a file in a plugin and no change in core's
    importer -- the rule the transcripts plugin already states about readers."""
    def detector(path, ctx):
        if str(path).endswith(".merscope"):
            return [import_proposal.LayerProposal(
                id="merscope", kind="points", modality="merscope",
                label="MERSCOPE", src=str(path))]
        return None

    monkeypatch.setattr(import_proposal, "_DETECTORS", [])
    import_proposal.register_detector(detector)
    (tmp_path / "run.merscope").write_text("x", encoding="utf-8")

    proposal = import_proposal.inspect_paths([str(tmp_path / "run.merscope")])
    assert proposal.samples[0].layers[0].modality == "merscope"


def test_a_registered_detector_needs_a_callable():
    with pytest.raises(ValueError):
        import_proposal.register_detector(None)


def test_a_builder_is_looked_up_by_name_not_imported():
    """Core holds no reference to any plugin. It holds a NAME and calls what
    was registered under it -- which is what `test_plugin_boundary` pins from
    the other side."""
    from plexora.server.models import layer_jobs

    assert layer_jobs.builder_for("nothing-here") == (None, None)
    try:
        layer_jobs.register_builder("demo_modality", lambda *a: None)
        assert layer_jobs.builder_for("demo_modality")[0] is not None
    finally:
        layer_jobs._BUILDERS.pop("demo_modality", None)
