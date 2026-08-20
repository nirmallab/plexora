"""Which columns are markers, and which are metadata.

This used to be answered in five places that disagreed -- three hardcoded
denylists and two independent "numeric column with a histogram" derivations.
The predictor is deliberately blunt and biased towards metadata: a metadata
column wrongly offered as a marker shows up as a nonsense histogram in every
plugin, while a marker wrongly filed as metadata is one drag away on the
classification screen.
"""

from plexora.server.models.adapters.classify import (
    classify_columns,
    classify_from_inspection,
    guess_roles,
    is_likely_image_identifier_name,
    looks_like_metadata,
)

# A real mcmicro-shaped quantification header set.
MCMICRO = [
    {"name": "CellID", "dtype": "Int64"},
    {"name": "DNA1", "dtype": "Float64"},
    {"name": "CD3", "dtype": "Float64"},
    {"name": "PD-L1", "dtype": "Float64"},
    {"name": "X_centroid", "dtype": "Float64"},
    {"name": "Y_centroid", "dtype": "Float64"},
    {"name": "Area", "dtype": "Float64"},
    {"name": "MajorAxisLength", "dtype": "Float64"},
    {"name": "Eccentricity", "dtype": "Float64"},
    {"name": "Solidity", "dtype": "Float64"},
    {"name": "phenotype", "dtype": "String"},
    {"name": "imageid", "dtype": "String"},
]


def test_a_real_header_set_splits_the_way_a_person_would():
    result = classify_columns(MCMICRO)

    assert result["markers"] == ["DNA1", "CD3", "PD-L1"]
    assert "Area" in result["metadata"]
    assert "MajorAxisLength" in result["metadata"]
    assert "Eccentricity" in result["metadata"]


def test_input_order_is_preserved_within_each_group():
    """A table's own column order is usually meaningful to whoever produced it,
    and reordering makes the classification screen harder to check."""
    result = classify_columns(MCMICRO)
    assert result["markers"] == [c["name"] for c in MCMICRO
                                 if c["name"] in set(result["markers"])]


def test_a_non_numeric_column_is_metadata_whatever_it_is_called():
    """A marker is an intensity. Anything non-numeric is an annotation."""
    result = classify_columns([{"name": "CD3", "dtype": "String"}])
    assert result["metadata"] == ["CD3"]


def test_a_derived_measurement_of_a_marker_is_metadata():
    """"CD3_area" measures the cell, not the stain."""
    assert looks_like_metadata("CD3_area", "Float64")
    assert looks_like_metadata("DNA1_orientation", "Float64")
    assert not looks_like_metadata("CD3", "Float64")


def test_separator_and_case_do_not_matter():
    for name in ("X_centroid", "x centroid", "XCentroid", "x-centroid"):
        assert looks_like_metadata(name, "Float64"), name


def test_a_marker_with_digits_and_punctuation_stays_a_marker():
    result = classify_columns([
        {"name": "PD-1", "dtype": "Float64"},
        {"name": "HLA-DR", "dtype": "Float64"},
        {"name": "Ki67", "dtype": "Float64"},
    ])
    assert result["markers"] == ["PD-1", "HLA-DR", "Ki67"]


def test_roles_are_guessed_from_the_conventional_names():
    roles = classify_columns(MCMICRO)["roles"]
    assert roles["cell_id"] == "CellID"
    assert roles["x"] == "X_centroid"
    assert roles["y"] == "Y_centroid"
    assert roles["celltype"] == "phenotype"
    assert roles["image_id"] == "imageid"


def test_the_older_naming_convention_still_resolves():
    roles = guess_roles(["ID", "X Position", "Y Position", "cellType", "Sample"])
    assert roles["cell_id"] == "ID"
    assert roles["x"] == "X Position"
    assert roles["y"] == "Y Position"


def test_a_specific_name_beats_a_bare_one():
    """"X_centroid" and a bare "X" in the same table: the specific one wins."""
    assert guess_roles(["X", "Y", "X_centroid", "Y_centroid"])["x"] == "X_centroid"


def test_one_column_never_fills_two_roles():
    roles = guess_roles(["id", "X", "Y"])
    assert len({v for v in roles.values()}) == len(roles)


def test_an_unresolvable_role_is_simply_absent():
    """Absent rather than guessed -- the project stores it as None and whatever
    needs it asks the user."""
    assert "image_id" not in guess_roles(["CellID", "X_centroid", "Y_centroid"])


def test_bare_names_work_without_dtypes():
    """The client sends dtypes as strings and sometimes has none at all."""
    result = classify_columns(["CellID", "CD3", "X_centroid", "Area"])
    assert result["markers"] == ["CD3"]


def test_image_identifier_names_are_recognised_without_separators():
    assert is_likely_image_identifier_name("imageid")
    assert is_likely_image_identifier_name("Image ID")
    assert is_likely_image_identifier_name("ROI")
    assert not is_likely_image_identifier_name("cell_type")


def test_an_anndata_split_comes_from_the_file_structure():
    """var is markers and obs is metadata by construction -- the file already
    draws the line the CSV screen exists to draw, so nobody is asked."""
    result = classify_from_inspection({
        "var_names": ["CD3", "CD8"],
        "obs_columns": [{"name": "cell_id"}, {"name": "X_centroid"},
                        {"name": "Y_centroid"}, {"name": "imageid"}],
    })

    assert result["markers"] == ["CD3", "CD8"]
    assert result["metadata"] == ["cell_id", "X_centroid", "Y_centroid", "imageid"]
    assert result["roles"]["cell_id"] == "cell_id"
    assert result["roles"]["image_id"] == "imageid"


def test_empty_input_is_not_an_error():
    assert classify_columns([]) == {"markers": [], "metadata": [], "roles": {}}
    assert classify_columns(None)["markers"] == []
