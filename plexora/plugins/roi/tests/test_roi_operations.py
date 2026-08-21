"""The rules an edit has to obey, tested without a server or a store.

`apply_operations` is a pure function from (state, operations) to state, which
is what lets the rules that matter -- locking, orphan handling, label
uniqueness, geometry validity -- be asserted directly rather than inferred from
what a route happened to return.

The theme running through these: an operation that cannot be applied changes
NOTHING. A user's batch of edits either lands or does not, and half of a delete
is worse than none of it.
"""

import pytest

from plexora.plugins.roi.server import schema
from plexora.plugins.roi.server.operations import apply_operations

TRIANGLE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]}
SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [8, 0], [8, 8], [0, 8], [0, 0]]]}


def state():
    return schema.default_state(1000, 800)


def category(state_, label="Tumor", cat_id="c-1", **extra):
    return apply_operations(state_, [{
        "op": "category.create",
        "category": {"id": cat_id, "label": label, "color": "#e04c4c", **extra},
    }])


def roi(state_, roi_id="r-1", category_id="c-1", geometry=None, **extra):
    return apply_operations(state_, [{
        "op": "roi.create", "image": "default",
        "feature": {"id": roi_id, "category_id": category_id,
                    "geometry": geometry or TRIANGLE, **extra},
    }])


def features(state_):
    return state_["images"]["default"]["features"]


# -- categories ---------------------------------------------------------

def test_a_new_project_has_no_categories():
    """The names a pathologist works in are theirs to choose. Shipping an
    "Uncategorized" they can neither rename nor delete puts a label nobody
    picked at the top of every project's list -- and quietly collects the shapes
    they meant to file properly."""
    assert state()["categories"] == []


def test_a_region_cannot_be_drawn_before_there_is_somewhere_to_put_it():
    """The cost of the decision above, and the reason the panel asks for a
    category before the draw tools come alive."""
    with pytest.raises(ValueError, match="unknown category"):
        roi(state(), category_id="c-1")


def test_two_categories_cannot_share_a_label():
    """The label is the only thing that tells them apart in the list, the
    dropdown and every export."""
    after = category(state())
    with pytest.raises(ValueError, match="already exists"):
        category(after, label="tumor", cat_id="c-2")


def test_renaming_does_not_change_a_category_s_identity():
    after = category(state())
    after = roi(after)
    after = apply_operations(after, [
        {"op": "category.update", "id": "c-1", "changes": {"label": "Tumor core"}}])

    assert after["categories"][0]["label"] == "Tumor core"
    # The whole reason ids are not derived from labels.
    assert features(after)[0]["category_id"] == "c-1"


def test_no_category_is_reserved():
    """Every row of the list behaves like its neighbours, including the last
    one. Nothing is protected from being renamed or deleted, because nothing
    depends on a particular category still being there."""
    after = category(state())
    after = apply_operations(after, [{"op": "category.update", "id": "c-1",
                                      "changes": {"label": "Anything"}}])
    after = apply_operations(after, [{"op": "category.delete", "id": "c-1",
                                      "orphans": "delete"}])
    assert after["categories"] == []


def test_deleting_a_category_requires_saying_what_happens_to_its_rois():
    """No default, on purpose. Both plausible defaults are bad: one silently
    deletes annotations, the other leaves shapes pointing at a category that no
    longer exists."""
    after = roi(category(state()))
    with pytest.raises(ValueError, match="orphans"):
        apply_operations(after, [{"op": "category.delete", "id": "c-1"}])
    with pytest.raises(ValueError, match="orphans"):
        apply_operations(after, [{"op": "category.delete", "id": "c-1", "orphans": "maybe"}])


def test_keeping_the_rois_means_naming_where_they_go():
    """There is no catch-all to default to, so a reassign that does not say
    where would leave the shapes pointing at a category that no longer exists
    -- shapes that cannot be drawn, listed or deleted."""
    after = roi(category(state()))
    with pytest.raises(ValueError, match="reassign_to"):
        apply_operations(after, [{"op": "category.delete", "id": "c-1",
                                  "orphans": "reassign"}])


def test_deleting_a_category_can_keep_its_rois():
    after = roi(category(state()))
    after = category(after, label="Stroma", cat_id="c-2")
    after = apply_operations(after, [{"op": "category.delete", "id": "c-1",
                                      "orphans": "reassign", "reassign_to": "c-2"}])
    assert len(features(after)) == 1
    assert features(after)[0]["category_id"] == "c-2"


def test_deleting_a_category_can_take_its_rois_with_it():
    after = roi(category(state()))
    after = category(after, label="Stroma", cat_id="c-2")
    after = roi(after, roi_id="r-2", category_id="c-2")
    after = apply_operations(after, [{"op": "category.delete", "id": "c-1",
                                      "orphans": "delete"}])
    assert [f["id"] for f in features(after)] == ["r-2"]


# -- ROIs ---------------------------------------------------------------

def test_an_roi_needs_a_category_that_exists():
    with pytest.raises(ValueError, match="unknown category"):
        roi(state(), category_id="c-nope")


def test_ids_are_not_reused():
    after = roi(category(state()))
    with pytest.raises(ValueError, match="already exists"):
        roi(after)


def test_rois_may_overlap():
    """Tumor and Invasive margin describe the same pixels on purpose. Any rule
    about which one a cell belongs to is a decision for whatever consumes these,
    not something to enforce while somebody is drawing."""
    after = roi(category(state()))
    after = roi(after, roi_id="r-2", geometry=SQUARE)
    assert len(features(after)) == 2


def test_timestamps_are_set_by_the_server():
    after = roi(category(state()))
    feature = features(after)[0]
    assert feature["created_at"] and feature["updated_at"]
    assert feature["created_at"].endswith("Z")


def test_unknown_fields_do_not_survive():
    """A feature is normalized on the way in, so nothing a client invents ends
    up stored and read back as if Plexora meant it."""
    after = apply_operations(category(state()), [{
        "op": "roi.create", "image": "default",
        "feature": {"id": "r-1", "category_id": "c-1", "geometry": TRIANGLE,
                    "evil": "payload"},
    }])
    assert "evil" not in features(after)[0]


# -- locking ------------------------------------------------------------

def test_a_locked_roi_keeps_its_shape():
    after = roi(category(state()), locked=True)
    with pytest.raises(ValueError, match="locked"):
        apply_operations(after, [{"op": "roi.update_geometry", "image": "default",
                                  "id": "r-1", "geometry": SQUARE}])
    with pytest.raises(ValueError, match="locked"):
        apply_operations(after, [{"op": "roi.delete", "image": "default", "id": "r-1"}])


def test_a_locked_roi_can_still_be_renamed_and_reclassified():
    """The lock is on the geometry -- it stops an accidental drag. What the
    region MEANS is a separate question and stays editable."""
    after = roi(category(state()), locked=True)
    after = category(after, label="Stroma", cat_id="c-2")
    after = apply_operations(after, [{
        "op": "roi.update_properties", "image": "default", "id": "r-1",
        "changes": {"name": "Renamed", "category_id": "c-2"}}])
    assert features(after)[0]["name"] == "Renamed"
    assert features(after)[0]["category_id"] == "c-2"


def test_locking_a_category_locks_everything_in_it():
    after = roi(category(state(), locked=True))
    with pytest.raises(ValueError, match="locked"):
        apply_operations(after, [{"op": "roi.delete", "image": "default", "id": "r-1"}])


# -- batches ------------------------------------------------------------

def test_a_batch_that_fails_anywhere_changes_nothing():
    """The property every caller depends on: an edit either lands whole or not
    at all, so there is never half of a user's action to reconcile."""
    before = roi(category(state()))
    with pytest.raises(ValueError):
        apply_operations(before, [
            {"op": "roi.delete", "image": "default", "id": "r-1"},
            {"op": "roi.delete", "image": "default", "id": "r-does-not-exist"},
        ])
    assert [f["id"] for f in features(before)] == ["r-1"]


def test_operations_do_not_mutate_the_state_they_are_given():
    before = category(state())
    after = roi(before)
    assert features(before) == []
    assert len(features(after)) == 1


def test_bulk_delete_refuses_the_whole_set_if_one_is_locked():
    after = roi(category(state()))
    after = roi(after, roi_id="r-2", locked=True)
    with pytest.raises(ValueError, match="locked"):
        apply_operations(after, [{"op": "roi.bulk_delete", "image": "default",
                                  "ids": ["r-1", "r-2"]}])
    assert len(features(after)) == 2


def test_an_unknown_operation_is_refused_rather_than_ignored():
    """Silently skipping one would mean a client and a server that disagree
    about what just happened, with the client's optimistic copy the only one
    holding the edit."""
    with pytest.raises(ValueError, match="unknown operation"):
        apply_operations(state(), [{"op": "roi.obliterate"}])


def test_only_the_images_this_project_has_can_be_written_to():
    with pytest.raises(ValueError, match="unknown image"):
        apply_operations(category(state()), [{
            "op": "roi.create", "image": "some_other_slide",
            "feature": {"id": "r-1", "category_id": "c-1", "geometry": TRIANGLE}}])
