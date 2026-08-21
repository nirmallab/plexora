"""Annotations survive a trip out to a file and back.

The round trip is the point of the format: a project exported on one machine
must reconstruct on another, without a feature table and without Plexora having
to be the thing that opens it. So these test both halves -- that a strict
GeoJSON reader gets valid geometry, and that Plexora gets its categories back.

The awkward cases are the ones worth having: shapes this editor cannot author
(holes, multi-part regions), categories that already exist under a different id,
and files that arrive with no Plexora metadata at all.
"""

import pytest

from plexora.plugins.roi.server import geojson, schema
from plexora.plugins.roi.server.operations import apply_operations

TRIANGLE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]}
WITH_HOLE = {"type": "Polygon", "coordinates": [
    [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
    [[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]],
]}
MULTI = {"type": "MultiPolygon", "coordinates": [
    [[[0, 0], [10, 0], [10, 10], [0, 0]]],
    [[[50, 50], [60, 50], [60, 60], [50, 50]]],
]}


def project(*geometries, label="Tumor"):
    state = schema.default_state(1000, 800)
    state = apply_operations(state, [{
        "op": "category.create",
        "category": {"id": "c-1", "label": label, "color": "#e04c4c", "sort_order": 1}}])
    return apply_operations(state, [
        {"op": "roi.create", "image": "default",
         "feature": {"id": f"r-{i}", "category_id": "c-1",
                     "name": f"{label} {i}", "geometry": geometry}}
        for i, geometry in enumerate(geometries)
    ])


def export(state):
    return geojson.export_document(state, "demo", "test-version")


def reimport(state, document):
    operation, report = geojson.import_features(state, document)
    return apply_operations(state, [operation]), report


def features(state):
    return state["images"]["default"]["features"]


# -- the document itself -------------------------------------------------

def test_the_export_is_valid_geojson_on_its_own():
    """A reader that ignores foreign members -- which is every strict GeoJSON
    reader -- still gets usable geometry out of it."""
    document = export(project(TRIANGLE))
    assert document["type"] == "FeatureCollection"
    feature = document["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"] == TRIANGLE


def test_what_a_region_is_survives_without_the_foreign_member():
    """Category name and colour are flattened onto each feature as well as
    living in the `plexora` block, so a tool that drops foreign members still
    knows what it is looking at."""
    properties = export(project(TRIANGLE))["features"][0]["properties"]
    assert properties["category"] == "Tumor"
    assert properties["category_color"] == "#e04c4c"
    assert properties["name"] == "Tumor 0"


def test_the_document_says_what_its_coordinates_mean():
    """GeoJSON positions are longitude/latitude unless a document says
    otherwise, and these are image pixels with y increasing downward. Stating it
    is the difference between a readable file and a misleading one."""
    space = export(project(TRIANGLE))["plexora"]["coordinate_space"]
    assert space == {"type": "image_pixels", "width": 1000, "height": 800,
                     "origin": "top-left", "axis_order": "xy"}
    # And never claims a geographic CRS.
    assert "crs" not in export(project(TRIANGLE))


# -- round trips ---------------------------------------------------------

def test_geometry_comes_back_exactly():
    """Not approximately: a coordinate that shifts on every export/import cycle
    would walk an annotation away from what was drawn."""
    state = project(TRIANGLE)
    restored, _ = reimport(schema.default_state(1000, 800), export(state))
    assert features(restored)[0]["geometry"] == features(state)[0]["geometry"]


@pytest.mark.parametrize("geometry", [WITH_HOLE, MULTI], ids=["hole", "multipolygon"])
def test_shapes_the_editor_cannot_draw_still_round_trip(geometry):
    """v1 cannot author a hole or a multi-part region. That is a limit of the
    drawing tools and not a reason to destroy one on the way through -- the
    geometry would silently come to cover pixels its author excluded."""
    restored, _ = reimport(schema.default_state(1000, 800), export(project(geometry)))
    assert features(restored)[0]["geometry"] == geometry


def test_categories_are_restored_into_an_empty_project():
    restored, report = reimport(schema.default_state(1000, 800), export(project(TRIANGLE)))
    assert report["created_categories"] == 1
    tumor = next(c for c in restored["categories"] if c["label"] == "Tumor")
    assert tumor["color"] == "#e04c4c"


def test_importing_into_a_project_that_already_has_the_category_reuses_it():
    """Matched by id first -- a Plexora export carries the same ones -- so
    re-importing does not leave two categories called Tumor."""
    existing = project(TRIANGLE)
    restored, report = reimport(existing, export(existing))
    assert report["created_categories"] == 0
    assert len([c for c in restored["categories"] if c["label"] == "Tumor"]) == 1


def test_a_category_with_the_same_name_but_a_different_id_is_matched_by_name():
    """Two projects annotated independently have different ids for the same
    label. Matching on the label is what makes importing one into the other land
    in the category the user means."""
    source = project(TRIANGLE)
    target = schema.default_state(1000, 800)
    target = apply_operations(target, [{
        "op": "category.create",
        "category": {"id": "c-other", "label": "tumor", "color": "#111111"}}])

    restored, report = reimport(target, export(source))
    assert report["created_categories"] == 0
    assert features(restored)[0]["category_id"] == "c-other"


def test_imported_regions_get_new_ids_and_remember_the_old_ones():
    """The original id is the only thread back to where a region came from once
    ids are regenerated, and somebody reconciling two exports will want it."""
    source = project(TRIANGLE)
    restored, _ = reimport(schema.default_state(1000, 800), export(source))
    imported = features(restored)[0]
    assert imported["id"] != "r-0"
    assert imported["source_roi_id"] == "r-0"


def test_importing_the_same_file_twice_duplicates_visibly():
    """Additive by design. The alternative -- overwriting by id -- destroys
    whichever copy loses, silently."""
    document = export(project(TRIANGLE))
    state, _ = reimport(schema.default_state(1000, 800), document)
    state, _ = reimport(state, document)
    assert len(features(state)) == 2
    assert features(state)[0]["id"] != features(state)[1]["id"]


# -- validation ----------------------------------------------------------

def test_a_document_from_a_newer_plexora_is_refused():
    document = export(project(TRIANGLE))
    document["plexora"]["schema_version"] = schema.SCHEMA_VERSION + 3
    errors, _ = geojson.validate_document(document, image_size=(1000, 800))
    assert any("newer version" in error for error in errors)


@pytest.mark.parametrize("document, expected", [
    ("not a document", "GeoJSON object"),
    ({"type": "Feature"}, "FeatureCollection"),
    ({"type": "FeatureCollection", "features": []}, "not exported by Plexora"),
])
def test_documents_that_cannot_be_trusted_are_refused(document, expected):
    errors, _ = geojson.validate_document(document)
    assert any(expected in error for error in errors)


def test_a_dimension_mismatch_is_a_warning_and_not_an_error():
    """Importing anyway is legitimate when the two images really are the same
    field of view. It is just never the right default."""
    document = export(project(TRIANGLE))
    errors, warnings = geojson.validate_document(document, image_size=(2000, 1600))
    assert errors == []
    assert warnings["dimension_mismatch"] == {"found": [1000, 800], "expected": [2000, 1600]}


def test_a_malformed_geometry_inside_a_valid_document_is_refused():
    """Structural validation passes -- it is a FeatureCollection with Plexora
    metadata -- and the geometry still has to survive the same check every
    drawn shape does."""
    document = export(project(TRIANGLE))
    document["features"][0]["geometry"] = {"type": "Polygon", "coordinates": [[[0, 0]]]}
    with pytest.raises(ValueError, match="three distinct"):
        geojson.import_features(schema.default_state(1000, 800), document)


def test_a_feature_with_no_category_at_all_still_arrives():
    """Rather than being dropped. Some intermediate tool stripping properties is
    not a reason to lose the shape.

    It needs somewhere to go, and since a project has no reserved catch-all the
    import makes one named for how these got here -- which is at least true of
    what is in it."""
    document = {
        "type": "FeatureCollection",
        "plexora": {"schema_version": 1, "categories": [],
                    "coordinate_space": {"width": 1000, "height": 800}},
        "features": [{"type": "Feature", "id": "x", "geometry": TRIANGLE}],
    }
    restored, _ = reimport(schema.default_state(1000, 800), document)

    landed = features(restored)[0]["category_id"]
    assert [c["label"] for c in restored["categories"]] == [geojson.IMPORTED_LABEL]
    assert landed == restored["categories"][0]["id"]


def test_two_such_features_share_one_category_rather_than_making_two():
    document = {
        "type": "FeatureCollection",
        "plexora": {"schema_version": 1, "categories": [],
                    "coordinate_space": {"width": 1000, "height": 800}},
        "features": [{"type": "Feature", "id": "x", "geometry": TRIANGLE},
                     {"type": "Feature", "id": "y", "geometry": MULTI}],
    }
    restored, _ = reimport(schema.default_state(1000, 800), document)
    assert len(restored["categories"]) == 1
    assert len({f["category_id"] for f in features(restored)}) == 1


# -- which image these regions belong to ---------------------------------
#
# A file of polygons that does not say which slide it was drawn on is a file
# whose regions can be applied to the wrong slide with nothing looking wrong.
# An AnnData holding a dozen images makes that a routine mistake rather than an
# exotic one, which is why the id is written in both places the category is.


def test_the_image_id_is_written_in_the_foreign_member_and_on_every_feature():
    document = geojson.export_document(project(TRIANGLE, MULTI), "proj", "v1",
                                       image_id="slide_07")
    assert document["plexora"]["image_id"] == "slide_07"
    assert [f["properties"]["image_id"] for f in document["features"]] == [
        "slide_07", "slide_07"]


def test_a_project_with_one_image_writes_no_image_id_at_all():
    """Absent rather than null. A single-image project has no such column, and
    `"image_id": null` reads as a question that was asked and came back empty."""
    document = geojson.export_document(project(TRIANGLE), "proj", "v1")
    assert "image_id" not in document["plexora"]
    assert "image_id" not in document["features"][0]["properties"]


def test_an_image_id_on_an_incoming_document_is_ignored():
    """Geometry is what an import restores. Which image somebody ELSE's project
    was drawn on is not a fact about this one, and adopting it would let a
    borrowed file quietly re-label where these regions belong."""
    document = geojson.export_document(project(TRIANGLE), "other", "v1",
                                       image_id="slide_99")
    state = schema.default_state(1000, 800)
    operation, report = geojson.import_features(state, document)
    assert report["imported"] == 1
    imported = operation["features"][0]
    assert "image_id" not in imported
    assert operation["image"] == schema.DEFAULT_IMAGE
