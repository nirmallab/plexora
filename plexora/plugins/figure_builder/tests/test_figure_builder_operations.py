"""What may be done to a figure, and what may not.

Two rules carry most of this file, and both exist because their alternative is
silent data loss:

**Nothing is ever orphaned without being asked about.** Deleting a page says
what happens to the panels on it; deleting a source says what happens to the
panels that reference it. Both possible defaults are wrong -- destroying
captured scenes because somebody tidied up a page, or keeping panels that point
at nothing and cannot be drawn -- so there is no default.

**A batch is atomic.** An invalid operation anywhere leaves the document exactly
as it was. Half of a Split Composite landing is a figure in a state the user
never asked for and cannot name.
"""

import copy

import pytest

from plexora.plugins.figure_builder.server import operations, schema

SOURCE = {
    "source_id": "src_1",
    "kind": "plexora_project",
    "datasource": "demo",
    "image": {"width": 4000, "height": 3000},
    "channels": [{"key": "demo_0", "fullname_at_capture": "DAPI"}],
}


@pytest.fixture
def document():
    base = schema.new_document("fig_aaaaaaaaaaaa", title="Figure 1", created_at="2026-01-01T00:00:00Z")
    return operations.apply_operations(base, [{"op": "add_source", "source": SOURCE}])


def panel(panel_id, page_id="pg_1", **placement):
    body = {
        "panel_id": panel_id,
        "source_id": "src_1",
        "scene": {"viewport": {"x": 10, "y": 20, "w": 500, "h": 500}},
    }
    if page_id:
        body["placement"] = {"page_id": page_id, "x_mm": 10, "y_mm": 10,
                             "w_mm": 40, "h_mm": 40, "z": 0, **placement}
    return {"op": "add_panel", "panel": body}


# -- atomicity ----------------------------------------------------------

def test_a_batch_that_fails_anywhere_changes_nothing(document):
    before = copy.deepcopy(document)
    with pytest.raises(ValueError):
        operations.apply_operations(document, [
            panel("pnl_1"),
            {"op": "add_panel", "panel": {"panel_id": "pnl_2", "source_id": "src_missing"}},
        ])
    assert document == before


def test_an_unknown_operation_is_refused_by_name(document):
    with pytest.raises(ValueError, match="unknown operation"):
        operations.apply_operations(document, [{"op": "delete_everything"}])


def test_an_empty_batch_is_refused(document):
    """A save with nothing in it still bumps a revision, which would make every
    other tab stale for no reason at all."""
    with pytest.raises(ValueError, match="no operations"):
        operations.apply_operations(document, [])


def test_a_batch_longer_than_the_ceiling_is_refused(document):
    with pytest.raises(ValueError, match="too many"):
        operations.apply_operations(
            document, [{"op": "set_meta", "changes": {"title": "x"}}]
            * (operations.MAX_OPERATIONS + 1))


# -- panels -------------------------------------------------------------

def test_a_panel_must_name_a_source_that_exists(document):
    with pytest.raises(ValueError, match="unknown source"):
        operations.apply_operations(document, [
            {"op": "add_panel", "panel": {"panel_id": "pnl_1", "source_id": "src_nope"}}])


def test_a_panel_must_be_placed_on_a_page_that_exists(document):
    with pytest.raises(ValueError, match="unknown page"):
        operations.apply_operations(document, [panel("pnl_1", page_id="pg_nope")])


def test_a_captured_panel_keeps_its_viewport_in_image_pixels(document):
    updated = operations.apply_operations(document, [panel("pnl_1")])
    viewport = updated["panels"]["pnl_1"]["scene"]["viewport"]
    assert viewport == {"x": 10.0, "y": 20.0, "w": 500.0, "h": 500.0}


def test_a_panel_with_no_placement_is_in_the_tray(document):
    updated = operations.apply_operations(document, [panel("pnl_1", page_id=None)])
    assert updated["panels"]["pnl_1"]["placement"] is None


def test_moving_several_panels_is_one_operation(document):
    """Dragging a selection of five is one thing the user did, and must be one
    thing they can undo."""
    updated = operations.apply_operations(document, [panel("pnl_1"), panel("pnl_2")])
    updated = operations.apply_operations(updated, [{
        "op": "move_panels",
        "moves": [
            {"panel_id": "pnl_1", "placement": {"x_mm": 50}},
            {"panel_id": "pnl_2", "placement": {"x_mm": 90}},
        ],
    }])
    assert updated["panels"]["pnl_1"]["placement"]["x_mm"] == 50
    assert updated["panels"]["pnl_2"]["placement"]["x_mm"] == 90
    # The rest of the placement survives a partial move: a drag changes x and y
    # and must not silently reset the size.
    assert updated["panels"]["pnl_1"]["placement"]["w_mm"] == 40


def test_moving_a_panel_to_no_page_returns_it_to_the_tray(document):
    updated = operations.apply_operations(document, [panel("pnl_1")])
    updated = operations.apply_operations(updated, [{
        "op": "move_panels", "moves": [{"panel_id": "pnl_1", "placement": None}]}])
    assert updated["panels"]["pnl_1"]["placement"] is None


def test_a_move_naming_a_panel_that_is_gone_refuses_the_whole_batch(document):
    updated = operations.apply_operations(document, [panel("pnl_1")])
    with pytest.raises(ValueError, match="unknown panel"):
        operations.apply_operations(updated, [{
            "op": "move_panels", "moves": [
                {"panel_id": "pnl_1", "placement": {"x_mm": 1}},
                {"panel_id": "pnl_gone", "placement": {"x_mm": 1}},
            ]}])


def test_a_render_revision_never_goes_backwards(document):
    """A stale preview upload is refused by comparing against this number, which
    only works while it is monotonic."""
    updated = operations.apply_operations(document, [panel("pnl_1")])
    updated = operations.apply_operations(updated, [
        {"op": "update_panel", "panel_id": "pnl_1", "changes": {"render_revision": 5}}])
    updated = operations.apply_operations(updated, [
        {"op": "update_panel", "panel_id": "pnl_1", "changes": {"render_revision": 2}}])
    assert updated["panels"]["pnl_1"]["render_revision"] == 5


# -- pages --------------------------------------------------------------

def test_deleting_a_page_needs_a_decision_about_its_panels(document):
    updated = operations.apply_operations(document, [
        {"op": "add_page", "page": {"page_id": "pg_2"}}, panel("pnl_1", page_id="pg_2")])
    with pytest.raises(ValueError, match="panels="):
        operations.apply_operations(updated, [{"op": "remove_page", "page_id": "pg_2"}])


def test_a_deleted_pages_panels_can_be_kept_in_the_tray(document):
    updated = operations.apply_operations(document, [
        {"op": "add_page", "page": {"page_id": "pg_2"}}, panel("pnl_1", page_id="pg_2")])
    updated = operations.apply_operations(updated, [
        {"op": "remove_page", "page_id": "pg_2", "panels": "tray"}])

    assert "pnl_1" in updated["panels"]
    assert updated["panels"]["pnl_1"]["placement"] is None


def test_a_deleted_pages_panels_can_be_destroyed_when_that_is_said(document):
    updated = operations.apply_operations(document, [
        {"op": "add_page", "page": {"page_id": "pg_2"}}, panel("pnl_1", page_id="pg_2")])
    updated = operations.apply_operations(updated, [
        {"op": "remove_page", "page_id": "pg_2", "panels": "delete"}])
    assert updated["panels"] == {}


def test_the_last_page_cannot_be_deleted(document):
    with pytest.raises(ValueError, match="at least one page"):
        operations.apply_operations(document, [
            {"op": "remove_page", "page_id": "pg_1", "panels": "tray"}])


def test_deleting_a_page_takes_its_annotations_with_it(document):
    updated = operations.apply_operations(document, [
        {"op": "add_page", "page": {"page_id": "pg_2"}},
        {"op": "add_annotation", "annotation": {
            "annotation_id": "ann_1", "type": "text", "page_id": "pg_2", "text": "hello"}}])
    updated = operations.apply_operations(updated, [
        {"op": "remove_page", "page_id": "pg_2", "panels": "tray"}])
    assert updated["annotations"] == {}


def test_pages_can_be_reordered_but_not_invented(document):
    updated = operations.apply_operations(document, [
        {"op": "add_page", "page": {"page_id": "pg_2"}}])
    reordered = operations.apply_operations(updated, [
        {"op": "reorder_pages", "page_ids": ["pg_2", "pg_1"]}])
    assert [page["page_id"] for page in reordered["pages"]] == ["pg_2", "pg_1"]

    with pytest.raises(ValueError, match="every page"):
        operations.apply_operations(updated, [{"op": "reorder_pages", "page_ids": ["pg_2"]}])


# -- sources ------------------------------------------------------------

def test_deleting_a_source_needs_a_decision_about_its_panels(document):
    updated = operations.apply_operations(document, [panel("pnl_1")])
    with pytest.raises(ValueError, match="panels="):
        operations.apply_operations(updated, [{"op": "remove_source", "source_id": "src_1"}])


def test_a_source_can_be_dropped_while_its_panels_stay_as_cached_previews(document):
    """Often the right answer for a finished figure: the panels still draw, still
    lay out and still export their vector furniture -- they simply cannot be
    re-edited until the source is relinked."""
    updated = operations.apply_operations(document, [panel("pnl_1")])
    updated = operations.apply_operations(updated, [
        {"op": "remove_source", "source_id": "src_1", "panels": "keep"}])
    assert "pnl_1" in updated["panels"]
    assert updated["sources"] == {}


def test_a_project_source_must_name_a_datasource(document):
    with pytest.raises(ValueError, match="needs a datasource"):
        operations.apply_operations(document, [
            {"op": "add_source", "source": {"source_id": "src_2", "kind": "plexora_project"}}])


def test_an_uncalibrated_source_keeps_no_pixel_size(document):
    """Never a default. A scale bar drawn from an assumed pixel size is wrong and
    looks exactly like one that is right."""
    updated = operations.apply_operations(document, [
        {"op": "add_source", "source": {**SOURCE, "source_id": "src_2",
                                        "pixel_size": {"value": 0, "unit": "µm"}}}])
    assert updated["sources"]["src_2"]["pixel_size"] is None


def test_a_calibrated_source_records_where_the_number_came_from(document):
    updated = operations.apply_operations(document, [
        {"op": "add_source", "source": {**SOURCE, "source_id": "src_2",
                                        "pixel_size": {"value": 0.325, "source": "manual"}}}])
    assert updated["sources"]["src_2"]["pixel_size"] == {
        "value": 0.325, "unit": "µm", "source": "manual"}


# -- linked groups ------------------------------------------------------

def test_panels_can_be_linked_and_the_link_is_recorded_on_both_sides(document):
    updated = operations.apply_operations(document, [panel("pnl_1"), panel("pnl_2")])
    updated = operations.apply_operations(updated, [
        {"op": "link_panels", "group": {"group_id": "grp_1",
                                        "panel_ids": ["pnl_1", "pnl_2"],
                                        "sync": ["viewport", "size"]}}])
    assert updated["panels"]["pnl_1"]["link_group"] == "grp_1"
    assert updated["link_groups"]["grp_1"]["sync"] == ["viewport", "size"]


def test_a_group_of_one_is_refused(document):
    updated = operations.apply_operations(document, [panel("pnl_1")])
    with pytest.raises(ValueError, match="at least two"):
        operations.apply_operations(updated, [
            {"op": "link_panels", "group": {"group_id": "grp_1", "panel_ids": ["pnl_1"]}}])


def test_deleting_a_panel_dissolves_a_group_that_drops_below_two(document):
    """A group of one synchronises with nothing, and leaving it behind would put
    a linked-panel badge on a panel with no partner."""
    updated = operations.apply_operations(document, [panel("pnl_1"), panel("pnl_2")])
    updated = operations.apply_operations(updated, [
        {"op": "link_panels", "group": {"group_id": "grp_1", "panel_ids": ["pnl_1", "pnl_2"]}}])
    updated = operations.apply_operations(updated, [
        {"op": "remove_panels", "panel_ids": ["pnl_2"]}])

    assert updated["link_groups"] == {}
    assert updated["panels"]["pnl_1"]["link_group"] is None


def test_a_generic_update_cannot_change_group_membership(document):
    """Otherwise a panel can end up in a group the group has never heard of."""
    updated = operations.apply_operations(document, [panel("pnl_1")])
    updated = operations.apply_operations(updated, [
        {"op": "update_panel", "panel_id": "pnl_1", "changes": {"link_group": "grp_invented"}}])
    assert updated["panels"]["pnl_1"]["link_group"] is None


# -- visual groups ------------------------------------------------------
#
# A different feature from linked groups, sharing nothing but the word. A link
# group propagates an edit between split-channel panels; a visual group makes
# an image and its caption behave as one object under the pointer and
# propagates nothing. These tests exist mostly to hold the two apart.


def annotation(annotation_id, page_id="pg_1"):
    return {"op": "add_annotation", "annotation": {
        "annotation_id": annotation_id, "type": "text", "page_id": page_id,
        "text": "caption"}}


def test_a_panel_and_an_annotation_can_be_one_object(document):
    """Image-plus-caption is the commonest reason to want a group, and it is
    exactly the case link_panels cannot serve: an annotation has no viewport to
    synchronise."""
    updated = operations.apply_operations(document, [panel("pnl_1"), annotation("ann_1")])
    updated = operations.apply_operations(updated, [
        {"op": "group_items", "group": {"group_id": "grp_v",
                                        "member_ids": ["pnl_1", "ann_1"]}}])
    assert updated["groups"]["grp_v"]["member_ids"] == ["pnl_1", "ann_1"]
    # And it has not touched the link machinery, which is the other half of the
    # claim: a grouped panel is not a linked panel.
    assert updated["panels"]["pnl_1"]["link_group"] is None
    assert updated["link_groups"] == {}


def test_a_group_of_one_thing_is_refused(document):
    """It selects one thing, which is what a click already does."""
    updated = operations.apply_operations(document, [panel("pnl_1")])
    with pytest.raises(ValueError, match="at least two"):
        operations.apply_operations(updated, [
            {"op": "group_items", "group": {"group_id": "grp_v", "member_ids": ["pnl_1"]}}])


def test_a_member_cannot_be_in_two_groups(document):
    """Refused rather than silently moved: moving it would take it out of a
    group the user cannot see from where they are standing."""
    updated = operations.apply_operations(
        document, [panel("pnl_1"), panel("pnl_2"), panel("pnl_3")])
    updated = operations.apply_operations(updated, [
        {"op": "group_items", "group": {"group_id": "grp_a",
                                        "member_ids": ["pnl_1", "pnl_2"]}}])
    with pytest.raises(ValueError, match="already in a group"):
        operations.apply_operations(updated, [
            {"op": "group_items", "group": {"group_id": "grp_b",
                                            "member_ids": ["pnl_2", "pnl_3"]}}])


def test_a_group_naming_something_that_does_not_exist_is_refused(document):
    updated = operations.apply_operations(document, [panel("pnl_1")])
    with pytest.raises(ValueError, match="unknown group member"):
        operations.apply_operations(updated, [
            {"op": "group_items", "group": {"group_id": "grp_v",
                                            "member_ids": ["pnl_1", "pnl_ghost"]}}])


def test_deleting_a_member_dissolves_a_group_that_drops_below_two(document):
    updated = operations.apply_operations(document, [panel("pnl_1"), annotation("ann_1")])
    updated = operations.apply_operations(updated, [
        {"op": "group_items", "group": {"group_id": "grp_v",
                                        "member_ids": ["pnl_1", "ann_1"]}}])
    updated = operations.apply_operations(updated, [
        {"op": "remove_annotations", "annotation_ids": ["ann_1"]}])
    assert updated["groups"] == {}


def test_deleting_a_page_takes_its_groups_with_it(document):
    """A page carries its annotations away with it, and a group left holding a
    deleted annotation would be a group pointing at nothing."""
    updated = operations.apply_operations(document, [
        {"op": "add_page", "page": {"page_id": "pg_2", "name": "Page 2"}}])
    updated = operations.apply_operations(updated, [
        panel("pnl_1", page_id="pg_1"), annotation("ann_1", page_id="pg_1")])
    updated = operations.apply_operations(updated, [
        {"op": "group_items", "group": {"group_id": "grp_v",
                                        "member_ids": ["pnl_1", "ann_1"]}}])
    updated = operations.apply_operations(updated, [
        {"op": "remove_page", "page_id": "pg_1", "panels": "delete"}])
    assert updated["groups"] == {}


def test_ungrouping_leaves_the_members_alone(document):
    updated = operations.apply_operations(document, [panel("pnl_1"), panel("pnl_2")])
    updated = operations.apply_operations(updated, [
        {"op": "group_items", "group": {"group_id": "grp_v",
                                        "member_ids": ["pnl_1", "pnl_2"]}}])
    updated = operations.apply_operations(updated, [
        {"op": "ungroup_items", "group_id": "grp_v"}])
    assert updated["groups"] == {}
    assert set(updated["panels"]) == {"pnl_1", "pnl_2"}


def test_a_figure_written_before_groups_existed_still_opens():
    """The key is additive on purpose -- no schema bump -- so an older document
    gains an empty one rather than being refused."""
    older = schema.new_document("fig_aaaaaaaaaaaa", title="Figure 1")
    older.pop("groups")
    assert schema.normalize_document(older)["groups"] == {}


def test_a_stored_group_whose_members_have_gone_is_dropped_on_read():
    """Belt and braces for the operations above: whatever wrote the file, a
    group that survives to load time still has to name at least two things that
    are actually there."""
    stored = schema.new_document("fig_aaaaaaaaaaaa", title="Figure 1")
    stored["groups"] = {"grp_v": {"group_id": "grp_v", "member_ids": ["pnl_1", "pnl_2"]}}
    assert schema.normalize_document(stored)["groups"] == {}


# -- one apply, one undo step -------------------------------------------

def test_a_split_is_expressible_as_one_batch(document):
    """The flagship operation's whole shape: N derived panels and the link
    between them arrive together, so undoing a split is one keystroke rather
    than five."""
    batch = [panel(f"pnl_{i}") for i in range(1, 5)]
    batch.append({"op": "link_panels", "group": {
        "group_id": "grp_1", "panel_ids": [f"pnl_{i}" for i in range(1, 5)],
        "sync": ["viewport", "size"]}})

    updated = operations.apply_operations(document, batch)
    assert len(updated["panels"]) == 4
    assert len(updated["link_groups"]["grp_1"]["panel_ids"]) == 4


def test_every_handler_is_reachable_by_name():
    """A handler added to the dict but never named, or named and never added, is
    an operation the client can send and the server silently rejects."""
    assert set(operations.OPERATION_NAMES) == set(operations._HANDLERS)
    assert "add_panel" in operations.OPERATION_NAMES
