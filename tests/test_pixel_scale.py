"""What a pixel is worth, read without loading the project."""

import dataclasses

import pytest

from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.server.utils import pixel_scale
from tests.agent_fixtures import PIXEL_SIZE_UM, make_synthetic_project


def test_the_file_states_it(tmp_path, monkeypatch):
    make_synthetic_project(tmp_path)
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda *a, **k: pytest.fail("loaded a datasource"))
    size = pixel_scale.pixel_size(Project.load("synth"))
    assert size["value"] == pytest.approx(PIXEL_SIZE_UM)
    assert size["source"] == "metadata" and size["unit"] == "µm"


def test_manual_wins_and_is_converted(tmp_path):
    make_synthetic_project(tmp_path)
    record = Project.load("synth")
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, pixel_size={"value": 325, "unit": "nm", "source": "manual"}))
    size = pixel_scale.pixel_size(record)
    assert size["source"] == "manual"
    assert size["value"] == pytest.approx(0.325)


def test_uncalibrated_is_none_never_a_default(tmp_path):
    make_synthetic_project(tmp_path, calibrated=False)
    assert pixel_scale.pixel_size(Project.load("synth")) is None
    assert "uncalibrated" in pixel_scale.describe(None)


def test_units():
    assert pixel_scale.to_microns(1, "mm") == 1000
    assert pixel_scale.to_microns(0, "µm") is None
    assert pixel_scale.to_microns(1, "parsec") is None
