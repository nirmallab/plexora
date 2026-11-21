"""The panel context flags a marker as having no image channel only when the
looks' matcher finds none (`vocabulary.match_name`), not by exact name."""

from plexora.plugins.gating.server.autogate import context


def test_vendor_channel_names_are_channels():
    built = context.build(["CD45", "PDL1", "CD68"],
                          image_channels=["01_Nucleus_Hoechst", "03_CD45_ArgoFluor520",
                                          "12_PD-L1_ArgoFluor676", "16_CD68_ArgoFluor760"])
    assert not any(e.get("no_image_channel") for e in built["entries"].values())
    # PD-L1 is a state marker: with its channel found it wants carriers and
    # is reviewed (it was neither when every marker looked channel-less).
    assert context.review_markers(built, None, ["CD45", "PDL1", "CD68"])
    assert context._is_carrier(built, "CD68")


def test_a_marker_without_a_channel_is_still_flagged():
    built = context.build(["CD45", "FOXP3"], image_channels=["03_CD45_ArgoFluor520"])
    assert built["entries"]["FOXP3"].get("no_image_channel") is True
    assert not built["entries"]["CD45"].get("no_image_channel")
