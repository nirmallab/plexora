"""What one pixel is worth, and the three states the viewer draws from it.

An image whose file states no physical size gets a scale bar counted in pixels
and a control to supply the missing number. These pin the server half of that:
where the value is stored, that it never invents one, and -- the load-bearing
part -- that `/get_ome_metadata` says which of the three states a project is in,
because that single field is what decides whether the scale bar shows microns,
whether the control appears at all, and whether a figure's provenance page may
claim the file said so.
"""

import json

import numpy as np
import pytest
import tifffile

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.models.project import Project, normalize_pixel_size
from tests.helpers import use_data_root


def _write_image(path, channels=3, size=64, microns=None):
    data = np.zeros((channels, size, size), dtype=np.uint8)
    if microns is None:
        tifffile.imwrite(path, data)
        return
    tifffile.imwrite(path, data, ome=True, metadata={
        "PhysicalSizeX": microns, "PhysicalSizeXUnit": "µm",
        "PhysicalSizeY": microns, "PhysicalSizeYUnit": "µm",
    })


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A registered, uncalibrated project. `make(microns=...)` for a file that
    states its own size."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)

    def make(name="sample", microns=None):
        image = tmp_path / f"{name}.tif"
        _write_image(image, microns=microns)
        datasource.register_image_datasource(
            name=name, image=image, data_dir=data_dir)
        return name, data_dir

    return make


# -- the normalizer ------------------------------------------------------


@pytest.mark.parametrize("raw", [
    None, {}, "0.5", {"value": None}, {"value": "abc"},
    {"value": 0}, {"value": -1}, {"value": float("nan")},
])
def test_nothing_that_is_not_a_length_becomes_a_calibration(raw):
    # The whole point of the field: a scale bar that is wrong looks exactly
    # like one that is right, so anything short of a positive number is no
    # calibration rather than a default one.
    assert normalize_pixel_size(raw) is None


def test_a_calibration_keeps_its_value_unit_and_source():
    assert normalize_pixel_size({"value": "0.325", "unit": "nm", "source": "metadata"}) == {
        "value": 0.325, "unit": "nm", "source": "metadata"}


def test_an_unrecognised_source_is_read_as_manual():
    # Only two sources mean anything, and "somebody typed it" is the safe
    # reading of an unknown one -- it makes no claim about the file.
    assert normalize_pixel_size({"value": 1, "source": "guessed"})["source"] == "manual"


# -- what gets stored ----------------------------------------------------


def test_setting_a_size_records_it_on_the_project(project):
    name, data_dir = project()

    stored = datasource.set_pixel_size(name, 0.3775, data_dir=data_dir)

    assert stored == {"value": 0.3775, "unit": "µm", "source": "manual"}
    assert Project.load(name, data_dir).image.pixel_size == stored


def test_a_stored_size_survives_a_round_trip_through_the_config(project):
    name, data_dir = project()
    datasource.set_pixel_size(name, 0.5, data_dir=data_dir)

    entry = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))[name]

    assert entry["pixelSize"] == {"value": 0.5, "unit": "µm", "source": "manual"}


def test_clearing_removes_the_key_rather_than_writing_a_null(project):
    # `pixelSize` has to be genuinely absent, not null: an entry that carries
    # the key is an entry a reader could take as an answer, and every project
    # registered before this existed has no key at all.
    name, data_dir = project()
    datasource.set_pixel_size(name, 0.5, data_dir=data_dir)

    assert datasource.set_pixel_size(name, None, data_dir=data_dir) is None

    entry = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))[name]
    assert "pixelSize" not in entry
    assert Project.load(name, data_dir).image.pixel_size is None


def test_a_project_with_no_calibration_writes_no_key(project):
    name, data_dir = project()

    entry = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))[name]

    assert "pixelSize" not in entry


def test_setting_a_size_leaves_the_rest_of_the_project_alone(project):
    name, data_dir = project()
    before = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))[name]

    datasource.set_pixel_size(name, 0.5, data_dir=data_dir)

    after = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))[name]
    assert {k: v for k, v in after.items() if k != "pixelSize"} == before


def test_an_unknown_project_is_refused_by_name(project):
    _, data_dir = project()

    with pytest.raises(ValueError, match="nope"):
        datasource.set_pixel_size("nope", 0.5, data_dir=data_dir)


# -- what the viewer is told ---------------------------------------------


def test_an_uncalibrated_project_claims_no_scale(project):
    name, _ = project()
    data_model.load_datasource(name, reload=True)

    payload = data_model.get_ome_metadata(name)

    assert not payload.get("physical_size_x")
    assert payload.get("pixel_size_source") is None


def test_a_file_that_states_its_own_size_is_reported_as_metadata(project):
    name, _ = project(name="stated", microns=0.65)
    data_model.load_datasource(name, reload=True)

    payload = data_model.get_ome_metadata(name)

    assert payload["physical_size_x"] == pytest.approx(0.65)
    assert payload["pixel_size_source"] == "metadata"


def test_a_typed_size_is_reported_as_manual(project):
    name, data_dir = project()
    datasource.set_pixel_size(name, 0.3775, data_dir=data_dir)
    data_model.load_datasource(name, reload=True)

    payload = data_model.get_ome_metadata(name)

    assert payload["physical_size_x"] == pytest.approx(0.3775)
    assert payload["physical_size_x_unit"] == "µm"
    assert payload["pixel_size_source"] == "manual"


def test_a_typed_size_overrides_what_the_file_says(project):
    # The override has to win, or correcting a wrong PhysicalSizeX would do
    # nothing -- and it has to say it is manual, because the provenance page
    # prints that word next to the number.
    name, data_dir = project(name="stated", microns=0.65)
    datasource.set_pixel_size(name, 0.2, data_dir=data_dir)
    data_model.load_datasource(name, reload=True)

    payload = data_model.get_ome_metadata(name)

    assert payload["physical_size_x"] == pytest.approx(0.2)
    assert payload["pixel_size_source"] == "manual"


def test_clearing_an_override_falls_back_to_the_file(project):
    name, data_dir = project(name="stated", microns=0.65)
    datasource.set_pixel_size(name, 0.2, data_dir=data_dir)
    datasource.set_pixel_size(name, None, data_dir=data_dir)
    data_model.load_datasource(name, reload=True)

    payload = data_model.get_ome_metadata(name)

    assert payload["physical_size_x"] == pytest.approx(0.65)
    assert payload["pixel_size_source"] == "metadata"


def test_both_axes_are_reported_so_a_reader_of_y_is_not_left_behind(project):
    name, data_dir = project()
    datasource.set_pixel_size(name, 0.25, data_dir=data_dir)
    data_model.load_datasource(name, reload=True)

    payload = data_model.get_ome_metadata(name)

    assert payload["physical_size_y"] == pytest.approx(0.25)
    assert payload["physical_size_y_unit"] == "µm"


# -- the route -----------------------------------------------------------


def _post(client, **payload):
    return client.post("/set_pixel_size", json=payload)


def test_the_route_saves_and_answers_with_what_it_stored(project):
    import plexora

    name, _ = project()
    client = plexora.app.test_client()

    response = _post(client, datasource=name, value=0.3775)

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["pixel_size"] == {"value": 0.3775, "unit": "µm", "source": "manual"}

    served = client.get(f"/get_ome_metadata?datasource={name}").get_json()
    assert served["physical_size_x"] == pytest.approx(0.3775)
    assert served["pixel_size_source"] == "manual"


def test_the_route_clears_on_an_empty_value(project):
    import plexora

    name, data_dir = project()
    datasource.set_pixel_size(name, 0.5, data_dir=data_dir)
    client = plexora.app.test_client()

    body = _post(client, datasource=name, value="").get_json()

    assert body["success"] is True
    assert body["pixel_size"] is None
    served = client.get(f"/get_ome_metadata?datasource={name}").get_json()
    assert served.get("pixel_size_source") is None


def test_the_route_refuses_text_rather_than_storing_nothing_quietly(project):
    import plexora

    name, _ = project()
    client = plexora.app.test_client()

    response = _post(client, datasource=name, value="wide")

    # A rejection, not a silent clear: "wide" is a typo, and treating it as
    # "remove the calibration" would throw away a number without saying so.
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_the_route_refuses_a_negative_size(project):
    import plexora

    name, _ = project()
    client = plexora.app.test_client()

    assert _post(client, datasource=name, value=-2).status_code == 400


def test_the_route_refuses_a_project_it_does_not_have(project):
    import plexora

    project()
    client = plexora.app.test_client()

    assert _post(client, datasource="nope", value=1).status_code == 422
