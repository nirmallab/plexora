"""The one nuclear-channel rule (presets.is_nuclear_name / nuclear_channels)."""

import pytest

from plexora.agent import presets
from plexora.plugins.qc.server import cycles

NUCLEAR = ["DAPI", "DAPI_1", "DAPI-cycle-2", "DNA3", "DNA_1", "Hoechst_04", "Hoechst 33342",
           "Hoechst33258", "H33342", "cycle2_DAPI", "R1_DNA", "191Ir_DNA1", "Ir191", "Nuclear",
           "nuclei", "Nucleus stain", "SYTO13", "Histone H3"]
NOT_NUCLEAR = ["CD3", "CD31", "CD33", "Vimentin", "pDNA", "cDNA", "DNase", "DNMT1", "Nucleolin",
               "DNA-PKcs", "Ki67", "H3K27me3"]


@pytest.mark.parametrize("name", NUCLEAR)
def test_nuclear_names(name):
    assert presets.is_nuclear_name(name)
    assert cycles.is_nuclear(name)


@pytest.mark.parametrize("name", NOT_NUCLEAR)
def test_not_nuclear_names(name):
    assert not presets.is_nuclear_name(name)
    assert not cycles.is_nuclear(name)


def test_nuclear_channels_keep_channel_order_once():
    names = ["CD3", "DAPI_1", "CD8", "DNA-cycle-2", "DAPI_1"]
    assert presets.nuclear_channels(names) == ["DAPI_1", "DNA-cycle-2"]
    assert presets.nuclear_channels(["CD3", "CD8"]) == []


def test_nuclear_channel_prefers_the_vocabulary():
    # The vocabulary's DNA entry wins over a name that merely says so.
    assert presets.nuclear_channel(["c2_DAPI", "DAPI"]) == "DAPI"
    assert presets.nuclear_channel(["CD3", "c2_DAPI"]) == "c2_DAPI"
    assert presets.nuclear_channel(["CD3"]) is None
