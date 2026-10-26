"""Which cells fall in which regions, and what that reads as.

Driven against hand-built geometry rather than a file, because every rule here
is a choice somebody could reasonably have made differently: overlaps join
rather than one winning, categories deduplicate and names do not, a hole is
outside. A test that only checked "a point in a square is in the square" would
pass for an implementation that got all four wrong.
"""

import pytest

from plexora.plugins.roi.server import mapping

pytest.importorskip("shapely")


def square(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [
        [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


CATEGORIES = [
    {"id": "c-tumor", "label": "Tumor", "color": "#e04c4c"},
    {"id": "c-necrosis", "label": "Necrosis", "color": "#38bdf8"},
]


def feature(fid, category, name, geometry):
    return {"id": fid, "category_id": category, "name": name, "geometry": geometry}


def test_a_cell_inside_one_region_gets_its_category_and_name():
    features = [feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10))]
    labels, names = mapping.assign(features, CATEGORIES, [5], [5])
    assert labels == ["Tumor"]
    assert names == ["Tumor 1"]


def test_a_cell_outside_every_region_gets_nothing_in_either_column():
    """Both columns, never one. A row with a name and no category would read as
    a region whose category was deleted, which is a different thing."""
    features = [feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10))]
    labels, names = mapping.assign(features, CATEGORIES, [50], [50])
    assert labels == [mapping.UNASSIGNED]
    assert names == [mapping.UNASSIGNED]


def test_overlapping_regions_join_both_columns():
    features = [
        feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10)),
        feature("r-2", "c-necrosis", "Necrosis 1", square(5, 5, 15, 15)),
    ]
    labels, names = mapping.assign(features, CATEGORIES, [7], [7])
    assert labels == ["Tumor_Necrosis"]
    assert names == ["Tumor 1_Necrosis 1"]


def test_two_regions_of_one_category_repeat_the_name_but_not_the_category():
    """The asymmetry is the point. The name column says which regions a cell is
    in and there really are two; the category column says what kind of place it
    is in, and 'Tumor_Tumor' answers that question twice."""
    features = [
        feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10)),
        feature("r-2", "c-tumor", "Tumor 2", square(5, 5, 15, 15)),
    ]
    labels, names = mapping.assign(features, CATEGORIES, [7], [7])
    assert labels == ["Tumor"]
    assert names == ["Tumor 1_Tumor 2"]


def test_the_join_follows_document_order_not_match_order():
    """So two exports of one project agree, and so the column matches what the
    panel lists. The spatial index returns matches in whatever order suits it."""
    features = [
        feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 100, 100)),
        feature("r-2", "c-necrosis", "Necrosis 1", square(0, 0, 50, 50)),
        feature("r-3", "c-tumor", "Tumor 2", square(0, 0, 20, 20)),
    ]
    labels, names = mapping.assign(features, CATEGORIES, [5], [5])
    assert names == ["Tumor 1_Necrosis 1_Tumor 2"]
    assert labels == ["Tumor_Necrosis"]


def test_a_cell_in_a_hole_is_outside_the_region():
    """A donut drawn around a necrotic core means the core is not in it. This is
    the whole reason containment goes through shapely rather than a ray cast
    over the outer ring."""
    donut = {"type": "Polygon", "coordinates": [
        [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
        [[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]],
    ]}
    features = [feature("r-1", "c-tumor", "Ring", donut)]
    labels, names = mapping.assign(features, CATEGORIES, [50, 10], [50, 10])
    assert labels == [mapping.UNASSIGNED, "Tumor"]
    assert names == [mapping.UNASSIGNED, "Ring"]


def test_a_multipolygon_counts_as_one_region():
    features = [feature("r-1", "c-tumor", "Split", {
        "type": "MultiPolygon",
        "coordinates": [square(0, 0, 10, 10)["coordinates"],
                        square(50, 50, 60, 60)["coordinates"]],
    })]
    labels, names = mapping.assign(features, CATEGORIES, [5, 55, 30], [5, 55, 30])
    assert names == ["Split", "Split", mapping.UNASSIGNED]
    assert labels == ["Tumor", "Tumor", mapping.UNASSIGNED]


def test_no_regions_and_no_cells_are_both_ordinary():
    assert mapping.assign([], CATEGORIES, [1, 2], [1, 2]) == (["", ""], ["", ""])
    features = [feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10))]
    assert mapping.assign(features, CATEGORIES, [], []) == ([], [])


def test_a_region_whose_category_was_deleted_still_names_itself():
    """Deleting a category can leave features pointing at it (the operation
    offers reassignment, and the user can decline). Dropping those rows would
    lose real annotation; a blank category and a real name is the honest shape."""
    features = [feature("r-1", "c-gone", "Orphan 1", square(0, 0, 10, 10))]
    labels, names = mapping.assign(features, CATEGORIES, [5], [5])
    assert names == ["Orphan 1"]
    assert labels == [""]


def test_a_hidden_region_is_still_mapped():
    """The eye is a viewing aid, not a filter. Somebody who hid half a crowded
    slide to see the other half did not ask for those regions to stop existing
    -- and a Map to cells that silently dropped them would write a column that
    is wrong in a way nothing on the screen would ever show."""
    features = [feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10))]
    features[0]["visible"] = False
    labels, names = mapping.assign(features, CATEGORIES, [5], [5])
    assert labels == ["Tumor"]
    assert names == ["Tumor 1"]


def test_mismatched_coordinate_lengths_are_refused():
    features = [feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10))]
    with pytest.raises(ValueError):
        mapping.assign(features, CATEGORIES, [1, 2], [1])


# -- the preview's summary -------------------------------------------------
#
# What the Map to cells confirmation shows. Totals, never a list per category:
# the category list has no upper bound, and only a few empty regions are named.


def test_the_summary_counts_cells_regions_overlaps_and_empty_regions():
    features = [
        feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10)),
        feature("r-2", "c-tumor", "Tumor 2", square(5, 5, 15, 15)),
        feature("r-3", "c-necrosis", "Necrosis 1", square(100, 100, 110, 110)),
    ]
    summary = {}
    labels, _ = mapping.assign(features, CATEGORIES, [2, 7, 50], [2, 7, 50],
                               summary=summary)
    assert labels == ["Tumor", "Tumor", mapping.UNASSIGNED]
    assert summary == {
        "n_cells": 3, "n_assigned": 2, "n_overlap": 1,
        "n_regions": 3, "n_regions_used": 2,
        "n_categories": 2, "n_categories_used": 1,
        "n_empty_regions": 1, "empty_regions": ["Necrosis 1"],
    }


def test_the_summary_names_only_a_few_empty_regions():
    features = [feature(f"r-{i}", "c-tumor", f"Tumor {i}", square(i * 20, 0, i * 20 + 5, 5))
                for i in range(6)]
    summary = {}
    mapping.assign(features, CATEGORIES, [1000], [1000], summary=summary)
    assert summary["n_empty_regions"] == 6
    assert len(summary["empty_regions"]) == mapping.EMPTY_EXAMPLES


def test_no_cells_still_fills_the_summary():
    summary = {}
    mapping.assign([feature("r-1", "c-tumor", "Tumor 1", square(0, 0, 10, 10))],
                   CATEGORIES, [], [], summary=summary)
    assert summary["n_cells"] == 0 and summary["n_assigned"] == 0
    assert summary["n_empty_regions"] == 1
