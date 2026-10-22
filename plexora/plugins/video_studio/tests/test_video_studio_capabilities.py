"""Video Studio for an agent: the ten `video.*` capabilities.

An agent works on the same documents the bar and the timeline do, through the
same schema, so it cannot make a track the UI would refuse. What is pinned:

* registration -- ten capabilities, tool names unique across everything any
  plugin registers, and each inheriting the plugin's `plugin:video_studio`
  entitlement at discovery (the capabilities themselves name none), so they
  are Pro with the plugin and refused on a licence without it;
* the handlers that work from the project record alone (everything but
  preview and export, which need a live viewer), invoked through the real
  pipeline -- validation, policy, licence, receipt;
* the undo contract: an edit's receipt carries `undo_hint` = `edit_video` with
  the inverse operations, and replaying it (directly or through core's
  `undo_operation`) restores the document exactly.
"""

import json

import pytest

from plexora.agent import AgentSession, registry
from plexora.agent.errors import AgentError
from plexora.agent.policy import Policy
from plexora.plugins.video_studio import capabilities as video_capabilities
from plexora.plugins.video_studio.server import repository
from tests.helpers import ALL_CONFIRMED, image_spec, project

NAMES = {"video.list", "video.get", "video.create", "video.add_keyframe", "video.set_track",
         "video.edit", "video.preview", "video.export", "video.duplicate", "video.delete"}
ENTITLEMENT = "plugin:video_studio"
IGNORED = ("revision", "updated_at")


@pytest.fixture
def demo(tmp_path):
    """A flat two-channel project in this test's data root, and a second one."""
    (tmp_path / "config.json").write_text(json.dumps({
        name: project(name, image=image_spec(channels=("DNA", "CD3"), width=400, height=300),
                      confirmed=ALL_CONFIRMED).to_entry()
        for name in ("demo", "other")
    }), encoding="utf-8")
    return "demo"


@pytest.fixture
def registered():
    registry._reset_for_tests()
    registry.discover(["video_studio"])
    yield
    registry._reset_for_tests()


@pytest.fixture
def licensed(license_issuer, registered, demo):
    license_issuer.install(license_issuer.issue(ent=["ai", ENTITLEMENT]))
    return AgentSession()


def _call(session, tool, arguments, **kwargs):
    return registry.invoke(session, tool, arguments, **kwargs)


def _ok(answer):
    assert answer["ok"], answer.get("error")
    return answer["result"]


def _create(session, **arguments):
    arguments = {"project": "demo", "preset": "zoom", "from_viewer": False, **arguments}
    return _ok(_call(session, "create_video", arguments))


def _document(video_id):
    return {k: v for k, v in repository.load(video_id).items() if k not in IGNORED}


# -- registration ------------------------------------------------------------------------


def test_the_plugin_declares_ten_capabilities_without_entitlements_of_their_own():
    caps = video_capabilities.capabilities()
    assert {cap.name for cap in caps} == NAMES
    assert len({cap.tool_name for cap in caps}) == 10
    assert all(cap.owner == "video_studio" for cap in caps)
    # The plugin's entitlement is applied at discovery, not repeated here.
    assert all(cap.entitlement is None for cap in caps)


def test_discovery_registers_them_pro_with_the_plugin():
    registry._reset_for_tests()
    try:
        names = registry.discover()
        assert NAMES <= set(names)
        everything = registry.all_capabilities()
        ours = [cap for cap in everything if cap.owner == "video_studio"]
        assert {cap.name for cap in ours} == NAMES
        assert {cap.entitlement for cap in ours} == {ENTITLEMENT}
        # No collision with any tool another owner registers (a collision
        # would have made discovery skip the plugin, so check both ways).
        theirs = {cap.tool_name for cap in everything if cap.owner != "video_studio"}
        assert not theirs & {cap.tool_name for cap in ours}
        tool_names = [cap.tool_name for cap in everything]
        assert len(tool_names) == len(set(tool_names))
        described = {row["name"]: row for row in registry.describe(sorted(NAMES))}
        assert {row["entitlement"] for row in described.values()} == {ENTITLEMENT}
        assert {row["plan"] for row in described.values()} == {"pro"}
        from plexora.mcp import tools

        for cap in ours:
            said = tools.description_for(cap)
            assert "Part of Plexora Pro" in said and "not part of" not in said, cap.name
    finally:
        registry._reset_for_tests()


def test_without_the_entitlement_every_capability_is_refused(registered, demo,
                                                             license_issuer):
    license_issuer.install(license_issuer.issue(ent=["ai"]))
    session = AgentSession()
    for tool in ("list_videos", "get_video", "create_video", "edit_video"):
        arguments = {"list_videos": {}, "create_video": {"project": "demo", "preset": "zoom"},
                     "edit_video": {"video_id": "vid_000000000000", "operations": []}
                     }.get(tool, {"video_id": "vid_000000000000"})
        answer = _call(session, tool, arguments)
        assert not answer["ok"]
        assert answer["error"]["code"] == "license_required", tool
        assert answer["error"]["detail"]["entitlement"] == ENTITLEMENT
    assert repository.list_videos() == []


# -- read and create -----------------------------------------------------------------------


def test_create_list_and_get(licensed):
    created = _create(licensed, title="Agent zoom")
    video = created["video"]
    video_id = video["video_id"]
    assert video["title"] == "Agent zoom" and video["project"] == "demo"
    assert video["revision"] == 0
    assert video["preset"]["name"] == "zoom"
    assert video["frames_at_output_fps"] == round(video["duration_s"] * 30)
    assert [t["kind"] for t in video["tracks"]] == ["camera2d"]
    receipt = created["receipt"]
    assert receipt["changed"] is True and receipt["project"] == "demo"
    assert receipt["undo_hint"] == {"tool": "delete_video",
                                    "arguments": {"video_id": video_id, "confirm": True}}
    assert repository.load(video_id)["provenance"]["created_by"] == "agent"

    listed = _ok(_call(licensed, "list_videos", {}))["videos"]
    assert [v["video_id"] for v in listed] == [video_id]
    assert _ok(_call(licensed, "list_videos", {"project": "other"}))["videos"] == []
    got = _ok(_call(licensed, "get_video", {"video_id": video_id}))
    assert got["video"]["video_id"] == video_id and got["exports"] == []


def test_create_refuses_a_preset_the_image_cannot_support(licensed):
    for preset in ("orbit", "time_lapse", "warp_speed"):
        answer = _call(licensed, "create_video", {"project": "demo", "preset": preset,
                                                  "from_viewer": False})
        assert not answer["ok"] and answer["error"]["code"] == "invalid_input", preset
    assert repository.list_videos() == []


def test_create_on_an_unknown_project_is_refused(licensed):
    answer = _call(licensed, "create_video", {"project": "nope", "preset": "zoom",
                                              "from_viewer": False})
    assert not answer["ok"]
    assert answer["error"]["code"] == "unknown_project"


def test_an_unknown_video_says_so(licensed):
    answer = _call(licensed, "get_video", {"video_id": "vid_000000000000"})
    assert not answer["ok"]
    assert "unknown agent error code" not in answer["error"]["message"]
    assert "vid_000000000000" in answer["error"]["message"]
    assert answer["error"]["code"] != "internal_error"


def test_an_invalid_video_id_is_invalid_input(licensed):
    answer = _call(licensed, "get_video", {"video_id": "../../etc"})
    assert not answer["ok"] and answer["error"]["code"] == "invalid_input"


# -- edits and their undo -------------------------------------------------------------------


def test_an_edits_undo_hint_restores_the_document(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    before = _document(video_id)
    edited = _ok(_call(licensed, "edit_video", {"video_id": video_id, "operations": [
        {"op": "set_title", "title": "Edited"}, {"op": "set_loop", "loop": True},
        {"op": "set_output", "output": {"fps": 24, "format": "gif"}}]}))
    assert edited["video"]["title"] == "Edited" and edited["video"]["revision"] == 1
    receipt = edited["receipt"]
    assert (receipt["revision_before"], receipt["revision_after"]) == (0, 1)
    assert receipt["persistent_state"] == "plugin_document"
    hint = receipt["undo_hint"]
    assert hint["tool"] == "edit_video"
    assert hint["arguments"]["video_id"] == video_id
    assert hint["arguments"]["base_revision"] == 1
    assert [op["op"] for op in hint["arguments"]["operations"]] == [
        "set_output", "set_loop", "set_title"]
    undone = _ok(_call(licensed, hint["tool"], hint["arguments"]))
    assert undone["video"]["revision"] == 2
    assert _document(video_id) == before


def test_core_undo_operation_replays_the_hint(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    before = _document(video_id)
    edited = _call(licensed, "edit_video", {"video_id": video_id, "operations": [
        {"op": "set_duration", "duration_s": 12}]})
    assert _ok(edited)["video"]["duration_s"] == 12.0
    undone = _ok(_call(licensed, "undo_operation", {"operation_id": edited["operation_id"]}))
    assert undone["undone"] == edited["operation_id"]
    assert _document(video_id) == before
    # And not twice.
    again = _call(licensed, "undo_operation", {"operation_id": edited["operation_id"]})
    assert not again["ok"] and again["error"]["code"] == "conflict"


def test_an_edit_against_a_stale_revision_is_a_conflict(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    _ok(_call(licensed, "edit_video", {"video_id": video_id,
                                       "operations": [{"op": "set_loop", "loop": True}]}))
    stale = _call(licensed, "edit_video", {"video_id": video_id, "base_revision": 0,
                                           "operations": [{"op": "set_title", "title": "x"}]})
    assert not stale["ok"]
    assert stale["error"]["code"] == "conflict"
    assert stale["error"]["detail"] == {"revision": 1}
    assert repository.load(video_id)["title"] != "x"


def test_an_interpolated_plane_is_refused_with_the_rule(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    answer = _call(licensed, "add_video_keyframe", {"video_id": video_id, "kind": "acquired_z",
                                                    "t": 1.0, "value": 2.5, "ease": "step"})
    assert not answer["ok"]
    assert answer["error"]["code"] == "invalid_input"
    assert "whole" in answer["error"]["detail"]["rule"]
    eased = _call(licensed, "add_video_keyframe", {"video_id": video_id, "kind": "acquired_z",
                                                   "t": 1.0, "value": 0})
    assert not eased["ok"] and eased["error"]["code"] == "invalid_input"
    assert repository.load(video_id)["revision"] == 0


def test_add_keyframe_makes_a_track_then_adds_to_it_and_undoes(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    before = _document(video_id)
    added = _ok(_call(licensed, "add_video_keyframe", {
        "video_id": video_id, "kind": "channel", "target": "DNA", "t": 0.5,
        "value": {"presence": 0.0}}))
    channel = next(t for t in added["video"]["tracks"] if t["kind"] == "channel")
    assert channel["target"] == "DNA" and channel["keyframes"] == 1
    more = _ok(_call(licensed, "add_video_keyframe", {
        "video_id": video_id, "track_id": channel["track_id"], "t": 2.0,
        "value": {"presence": 1.0}, "ease": "sine_in_out"}))
    track = next(t for t in repository.load(video_id)["tracks"]
                 if t["track_id"] == channel["track_id"])
    assert [(kf["t"], kf["value"]["presence"]) for kf in track["keyframes"]] == [(0.5, 0.0),
                                                                                 (2.0, 1.0)]
    # Undo newest first. Each hint is pinned to the revision its edit left, so
    # the older one is re-pinned to the revision the newer undo left -- what an
    # agent does after reading get_video.
    for result in (more, added):
        hint = result["receipt"]["undo_hint"]
        current = repository.load(video_id)["revision"]
        _ok(_call(licensed, hint["tool"], {**hint["arguments"], "base_revision": current}))
    assert _document(video_id) == before
    missing = _call(licensed, "add_video_keyframe", {"video_id": video_id, "t": 1,
                                                     "value": {}})
    assert not missing["ok"] and missing["error"]["code"] == "invalid_input"


def test_set_track_replaces_a_track_by_id_or_adds_a_new_one(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    before = _document(video_id)
    camera = repository.load(video_id)["tracks"][0]
    replaced = _ok(_call(licensed, "set_video_track", {
        "video_id": video_id, "track": {**camera, "label": "Slow zoom", "muted": True}}))
    assert replaced["video"]["tracks"][0]["muted"] is True
    assert repository.load(video_id)["tracks"][0]["label"] == "Slow zoom"
    hint = replaced["receipt"]["undo_hint"]
    assert hint["arguments"]["operations"][0]["op"] == "update_track"
    _ok(_call(licensed, hint["tool"], hint["arguments"]))
    assert _document(video_id) == before

    added = _ok(_call(licensed, "set_video_track", {"video_id": video_id, "track": {
        "kind": "hud", "keyframes": [{"t": 0, "value": {"caption": "Tonsil"}}]}}))
    assert [t["kind"] for t in added["video"]["tracks"]] == ["camera2d", "hud"]
    hint = added["receipt"]["undo_hint"]
    assert hint["arguments"]["operations"][0]["op"] == "remove_track"


# -- duplicate and delete ----------------------------------------------------------------------


def test_duplicate_copies_and_its_undo_deletes_the_copy(licensed):
    video_id = _create(licensed, title="Original")["video"]["video_id"]
    copied = _ok(_call(licensed, "duplicate_video", {"video_id": video_id}))
    copy_id = copied["video"]["video_id"]
    assert copy_id != video_id and copied["video"]["title"] == "Original copy"
    assert copied["receipt"]["undo_hint"] == {
        "tool": "delete_video", "arguments": {"video_id": copy_id, "confirm": True}}
    titled = _ok(_call(licensed, "duplicate_video", {"video_id": video_id, "title": "Try 2"}))
    assert titled["video"]["title"] == "Try 2"


def test_delete_needs_the_server_to_allow_it_and_confirm(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    # The default policy refuses a destructive call outright...
    refused = _call(licensed, "delete_video", {"video_id": video_id, "confirm": True})
    assert not refused["ok"] and refused["error"]["code"] == "permission_required"
    allowed = Policy(allow_destructive=True)
    # ...and a server that allows it still wants confirm: true.
    unconfirmed = _call(licensed, "delete_video", {"video_id": video_id}, policy=allowed)
    assert not unconfirmed["ok"] and unconfirmed["error"]["code"] == "permission_required"
    assert repository.exists(video_id)
    deleted = _ok(_call(licensed, "delete_video", {"video_id": video_id, "confirm": True},
                        policy=allowed))
    assert deleted["deleted"] == video_id
    assert deleted["receipt"]["reversible"] is False
    assert deleted["receipt"]["before"]["video_id"] == video_id
    assert not repository.exists(video_id)


def test_the_delete_handler_itself_refuses_without_confirm(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    with pytest.raises(AgentError) as info:
        video_capabilities.delete_video(None, video_capabilities.DeleteInput(video_id=video_id))
    assert "confirm" in info.value.message
    assert repository.exists(video_id)


def test_preview_and_export_need_a_viewer(licensed):
    video_id = _create(licensed)["video"]["video_id"]
    for tool in ("preview_video", "export_video"):
        answer = _call(licensed, tool, {"video_id": video_id})
        assert not answer["ok"]
        assert answer["error"]["code"] == "viewer_not_available", tool


def test_the_preset_choice_offered_to_an_agent_is_the_preset_table():
    """`create_video`'s `preset` is an enum in the tool's schema; it must name
    exactly the presets the server builds."""
    from plexora.plugins.video_studio import capabilities as caps
    from plexora.plugins.video_studio.server import presets as preset_table

    assert set(caps.PRESET_NAMES) == set(preset_table.PRESETS)
