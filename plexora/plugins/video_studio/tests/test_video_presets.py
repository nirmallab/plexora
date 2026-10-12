"""Quick Capture presets: a few parameters in, a whole editable project out.

The same `build` serves the bar (the live viewer's facts and scene) and an
agent's `create_video` (the project record alone), so both must get the same
storyboard for the same request -- which is only true if a build is
deterministic and every default is read from the image's facts rather than
from whatever the last caller passed. These tests pin that, the availability
rules (a rotation needs a z-stack; the bar shows it disabled with a reason
rather than building a video of nothing), parameter clamping, and the timing
promises a scientist relies on: a z-sweep at one plane per frame is exactly
one frame per plane, in order, never a plane skipped or shown twice.
"""

import copy

import pytest

from plexora.plugins.video_studio.server import presets, schema

CHANNELS = [{"key": "DNA", "name": "Hoechst"}, {"key": "CD3", "name": "CD3"},
            {"key": "CD8", "name": "CD8a"}]
STACK = {"datasource": "demo", "image_size": [800, 600], "depth": 12, "frames": 6,
         "channels": CHANNELS, "pixel_size": 0.5,
         "spacing": {"x": {"value": 0.5, "unit": "µm"}, "z": {"value": 2.0, "unit": "µm"}},
         "fingerprint": {"image_width": 800, "image_height": 600}}
FLAT = {"datasource": "flat", "image_size": [800, 600], "depth": 1, "frames": 1,
        "channels": [{"key": "DNA", "name": "DNA"}]}
PLANE_SCENE = {"view": {"mode": "plane", "z": 3},
               "channels": [{"key": "CD3", "visible": True}, {"key": "DNA", "visible": True},
                            {"key": "CD8", "visible": False}]}

ALL = sorted(presets.PRESETS)


def _without_ids(value):
    """A document with every generated id removed, for comparing two builds."""
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k not in ("track_id", "kf_id")}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


def test_there_are_fifteen_presets_five_of_them_storyboards():
    assert len(presets.PRESETS) == 15
    stories = sorted(name for name, p in presets.PRESETS.items() if p.story)
    assert stories == ["story_markers", "story_orthogonal", "story_overview",
                       "story_structure", "story_timeseries"]
    assert {p.group for p in presets.PRESETS.values()} == {g for g, _ in presets.GROUPS}


@pytest.mark.parametrize("name", ALL)
def test_every_preset_builds_a_valid_project_for_a_multichannel_stack(name):
    doc = presets.build(name, STACK, PLANE_SCENE)
    # A normalised project: what the repository will store, unchanged.
    assert schema.normalize_project(doc) == doc
    assert doc["tracks"]
    assert doc["provenance"]["preset"]["name"] == name
    assert doc["provenance"]["created_by"] == "ui"
    assert doc["source"]["datasource"] == "demo"
    assert doc["source"]["depth"] == 12 and doc["source"]["frames"] == 6
    assert doc["duration_s"] > 0
    # Nothing a preset makes is something the schema would warn about.
    assert [w["code"] for w in schema.validate(doc)] == []
    # Every track sits inside the video.
    for track in doc["tracks"]:
        assert 0 <= track["range"][0] <= track["range"][1] <= doc["duration_s"] + 1e-9


@pytest.mark.parametrize("name", ALL)
def test_a_build_is_deterministic(name):
    first = presets.build(name, copy.deepcopy(STACK), copy.deepcopy(PLANE_SCENE))
    second = presets.build(name, copy.deepcopy(STACK), copy.deepcopy(PLANE_SCENE))
    assert _without_ids(first) == _without_ids(second)


def test_build_does_not_modify_the_facts_or_scene_it_was_given():
    facts, scene = copy.deepcopy(STACK), copy.deepcopy(PLANE_SCENE)
    for name in ALL:
        presets.build(name, facts, scene)
    assert facts == STACK and scene == PLANE_SCENE


def test_build_records_who_asked():
    assert presets.build("orbit", STACK, created_by="agent")["provenance"]["created_by"] == \
        "agent"


# -- availability ----------------------------------------------------------------------


NEEDS = {name: preset.needs for name, preset in presets.PRESETS.items()}


@pytest.mark.parametrize("name", ALL)
def test_a_preset_the_image_cannot_support_is_refused(name):
    needs = NEEDS[name]
    if not needs:
        # Zoom, pan and the overview need nothing but an image.
        assert presets.build(name, FLAT)["tracks"]
        return
    with pytest.raises(ValueError) as info:
        presets.build(name, FLAT)
    assert presets.PRESETS[name].label in str(info.value)


def test_each_need_is_checked_on_its_own():
    only_depth = {**STACK, "frames": 1, "channels": CHANNELS[:1]}
    only_frames = {**STACK, "depth": 1, "channels": CHANNELS[:1]}
    only_channels = {**STACK, "depth": 1, "frames": 1}
    for name, needs in NEEDS.items():
        for facts, need in ((only_depth, "depth"), (only_frames, "frames"),
                            (only_channels, "channels")):
            ok, reason = presets.PRESETS[name].available(facts)
            if needs and set(needs) <= {need}:
                assert ok and reason == "", (name, need)
            elif need not in needs and needs:
                assert not ok and reason, (name, need)


def test_an_unknown_preset_is_a_key_error():
    with pytest.raises(KeyError):
        presets.build("spin_really_fast", STACK)


def test_describe_all_marks_unavailable_presets_with_a_reason():
    rows = {row["name"]: row for row in presets.describe_all(FLAT)}
    assert set(rows) == set(presets.PRESETS)
    assert rows["orbit"]["available"] is False and rows["orbit"]["reason"] == "Needs a z-stack."
    assert rows["time_lapse"]["reason"] == "Needs a time series."
    assert rows["channel_reveal"]["reason"] == "Needs two or more channels."
    for name in ("zoom", "xy_pan", "story_overview"):
        assert rows[name]["available"] is True and rows[name]["reason"] == ""
    for row in rows.values():
        assert set(row) >= {"label", "group", "icon", "summary", "story", "fields"}
        assert all({"name", "label", "kind", "default"} <= set(f) for f in row["fields"])
    assert all(row["available"] for row in presets.describe_all(STACK))
    # No facts at all: nothing that needs depth, time or channels is offered.
    bare = {row["name"]: row for row in presets.describe_all(None)}
    assert bare["orbit"]["available"] is False and bare["zoom"]["available"] is True


def test_fields_describe_their_options_bounds_and_hints():
    fields = {f["name"]: f for f in presets.PRESETS["z_sweep"].describe(STACK)["fields"]}
    # "depth" resolved against the image: the browser draws numbers, and an
    # unresolved name left the To field empty on a real stack.
    assert fields["to"]["max"] == STACK["depth"] - 1
    assert fields["to"]["default"] == STACK["depth"] - 1
    assert fields["ms_per_plane"]["unit"] == "ms" and fields["ms_per_plane"]["min"] == 10
    assert [o["value"] for o in fields["direction"]["options"]] == ["up", "down", "bounce"]
    orbit = {f["name"]: f for f in presets.PRESETS["orbit"].describe()["fields"]}
    assert orbit["loop"]["hint"]


@pytest.mark.parametrize("facts, scene, expected", [
    (STACK, {"view": {"mode": "volume"}}, "orbit"),
    (STACK, PLANE_SCENE, "z_sweep"),
    (STACK, None, "z_sweep"),
    ({**STACK, "depth": 1}, None, "time_lapse"),
    (FLAT, None, "zoom"),
    ({}, None, "zoom"),
])
def test_the_default_preset_follows_the_image_and_the_view(facts, scene, expected):
    assert presets.default_preset(facts, scene) == expected


# -- defaults come from the facts ---------------------------------------------------------


def test_a_z_sweep_defaults_to_every_plane_of_this_stack():
    doc = presets.build("z_sweep", STACK)
    assert doc["provenance"]["preset"]["params"]["to"] == 11
    assert doc["tracks"][0]["procedural"]["params"]["from"] == 0
    assert doc["tracks"][0]["procedural"]["params"]["to"] == 11
    deeper = presets.build("z_sweep", {**STACK, "depth": 40})
    assert deeper["tracks"][0]["procedural"]["params"]["to"] == 39


def test_a_time_lapse_defaults_to_every_timepoint():
    doc = presets.build("time_lapse", STACK)
    assert doc["tracks"][0]["kind"] == "acquired_t"
    assert doc["tracks"][0]["procedural"]["params"]["to"] == 5
    assert doc["base"]["view"]["t"] == 0


@pytest.mark.parametrize("depth", [3, 5, 12, 31, 100])
def test_a_z_sweep_at_one_plane_per_frame_is_exactly_one_frame_per_plane(depth):
    """The promise the preset's name makes: every plane, at an even pace. At
    1000/30 ms per plane and 30 fps that is exactly `depth` frames, each a new
    plane, in order -- a skipped or doubled plane would be invisible in the
    video and wrong in the figure made from it."""
    doc = presets.build("z_sweep", {**STACK, "depth": depth},
                        params={"ms_per_plane": 1000 / 30, "hold_ends_s": 0, "loop": False})
    plan = schema.acquired_plan(doc, 30)
    assert len(plan) == depth
    zs = [f["z"] for f in plan]
    assert zs == list(range(depth))
    assert all(b > a for a, b in zip(zs, zs[1:]))


def test_a_z_sweep_downward_and_with_holds():
    down = presets.build("z_sweep", STACK, params={"direction": "down",
                                                   "ms_per_plane": 1000 / 30,
                                                   "hold_ends_s": 0})
    assert [f["z"] for f in schema.acquired_plan(down, 30)] == list(range(11, -1, -1))
    assert down["base"]["view"]["z"] == 11
    held = presets.build("z_sweep", STACK, params={"ms_per_plane": 100, "hold_ends_s": 1})
    assert held["tracks"][0]["range"] == [1.0, pytest.approx(2.2)]
    assert held["duration_s"] == pytest.approx(3.2)
    plan = schema.acquired_plan(held, 10)
    # Held on the first plane before the sweep, on the last after it.
    assert {f["z"] for f in plan[:10]} == {0}
    assert {f["z"] for f in plan[-10:]} == {11}


def test_a_bouncing_z_sweep_turns_without_repeating_the_top_plane():
    doc = presets.build("z_sweep", {**STACK, "depth": 4},
                        params={"direction": "bounce", "ms_per_plane": 100, "hold_ends_s": 0,
                                "loop": True})
    # 0,1,2,3,2,1 at 10 fps: six frames, the loop closes on its own.
    assert [f["z"] for f in schema.acquired_plan(doc, 10)] == [0, 1, 2, 3, 2, 1]


def test_a_z_sweep_starts_from_the_scene_and_keeps_its_flat_mode():
    doc = presets.build("z_sweep", STACK, {"view": {"mode": "slab", "z": 7},
                                           "channels": [{"key": "DNA"}]})
    assert doc["base"]["view"]["mode"] == "slab"
    assert [c["key"] for c in doc["base"]["channels"]] == ["DNA"]
    assert doc["base"]["view"]["z"] == 0  # the sweep's first plane, not the viewer's


# -- 3-D -----------------------------------------------------------------------------------


def test_an_orbit_loop_is_linear_and_marked_as_a_loop():
    """A loop must turn at a constant rate: an eased loop slows to a stop at
    the seam it is supposed to hide."""
    doc = presets.build("orbit", STACK, PLANE_SCENE, {"loop": True})
    track = doc["tracks"][0]
    assert doc["loop"] is True
    assert track["kind"] == "camera3d" and track["mode"] == "procedural"
    assert track["procedural"]["generator"] == "orbit"
    assert track["procedural"]["ease"] == "linear"
    once = presets.build("orbit", STACK, PLANE_SCENE, {"loop": False})
    assert once["loop"] is False and once["tracks"][0]["procedural"]["ease"] == "sine_in_out"


def test_an_orbit_switches_a_plane_view_to_3d_and_says_so():
    doc = presets.build("orbit", STACK, PLANE_SCENE)
    assert doc["base"]["view"]["mode"] == "volume"
    assert doc["base"]["dims"] == "3d"
    assert doc["base"]["camera3d"]["q"] == pytest.approx(presets.ISO_Q)
    assert any("3-D view" in note for note in doc["notes"])


def test_an_orbit_starts_from_the_viewers_camera():
    camera = {"q": [0.0, 0.0, 0.0, 1.0], "dist": 1.5, "pan": [0.1, 0.0]}
    doc = presets.build("orbit", STACK, {"view": {"mode": "volume"}, "camera3d": camera})
    assert doc["base"]["camera3d"]["dist"] == 1.5
    assert doc["notes"] == []
    assert doc["tracks"][0]["procedural"]["params"]["from"] == "$current"


def test_a_rock_lasts_its_period_times_its_cycles():
    doc = presets.build("rock", STACK, params={"period_s": 3, "cycles": 4})
    assert doc["duration_s"] == 12.0 and doc["loop"] is True
    assert doc["tracks"][0]["procedural"]["params"]["cycles"] == 4


def test_the_structure_storyboard_reveals_first_then_turns_then_rocks():
    """The stack is built up plane by plane before it is ever shown whole, so it
    is never cut back to one plane after being seen complete."""
    doc = presets.build("story_structure", STACK, params={"turn_s": 6, "reveal_s": 4,
                                                          "rock_s": 2})
    kinds = [(t["kind"], (t["procedural"] or {}).get("generator"), t["range"])
             for t in doc["tracks"]]
    assert kinds == [("slab", "reveal", [0.0, 4.0]), ("camera3d", "orbit", [4.0, 10.0]),
                     ("camera3d", "rock", [10.0, 12.0])]
    assert doc["duration_s"] == 12.0
    plan = schema.acquired_plan(doc, 10)
    assert plan[0]["slab"] == {"z0": 11, "z1": 11, "op": "max"}
    assert plan[-1]["slab"] == {"z0": 0, "z1": 11, "op": "max"}
    assert schema.acquired_at(doc, 4.0)["slab"] == {"z0": 0, "z1": 11, "op": "max"}


def test_a_slice_reveal_holds_the_whole_stack_at_the_end():
    doc = presets.build("slice_reveal", STACK, params={"duration_s": 3, "hold_end_s": 2})
    assert doc["duration_s"] == 5.0
    assert doc["tracks"][0]["range"] == [0.0, 3.0]
    assert schema.acquired_at(doc, 4.5)["slab"] == {"z0": 0, "z1": 11, "op": "max"}


# -- plane presets ---------------------------------------------------------------------------


def test_a_zoom_in_ends_at_a_quarter_of_the_width():
    doc = presets.build("zoom", FLAT, params={"factor": 4.0, "duration_s": 2, "hold_end_s": 1})
    track = doc["tracks"][0]
    assert track["kind"] == "camera2d"
    start, end = (kf["value"] for kf in track["keyframes"])
    assert start["w"] == 800.0 and end["w"] == 200.0
    assert (start["cx"], start["cy"]) == (400.0, 300.0)
    assert [kf["t"] for kf in track["keyframes"]] == [0.0, 2.0]
    assert doc["duration_s"] == 3.0
    out = presets.build("zoom", FLAT, params={"factor": 4.0, "direction": "out"})
    first, last = (kf["value"] for kf in out["tracks"][0]["keyframes"])
    assert (first["w"], last["w"]) == (200.0, 800.0)
    assert out["base"]["viewport"]["w"] == 200.0


def test_a_pan_moves_by_speed_times_duration_views():
    doc = presets.build("xy_pan", FLAT, params={"direction": "left", "speed": 0.25,
                                                "duration_s": 2})
    start, end = (kf["value"] for kf in doc["tracks"][0]["keyframes"])
    assert end["cx"] == pytest.approx(start["cx"] - 0.5 * start["w"])
    assert end["cy"] == start["cy"]


# -- channels ------------------------------------------------------------------------------


def _presence(track):
    return [(kf["t"], kf["value"].get("presence")) for kf in track["keyframes"]]


def test_a_channel_reveal_adds_one_channel_after_another():
    doc = presets.build("channel_reveal", STACK, None, {"mode": "add", "per_channel_s": 2,
                                                        "fade_s": 0.5, "hold_s": 1})
    tracks = [t for t in doc["tracks"] if t["kind"] == "channel"]
    # No viewer: the image's channels in order.
    assert [t["target"] for t in tracks] == ["DNA", "CD3", "CD8"]
    # The first is there from the first frame; the others fade in on their turn.
    assert _presence(tracks[0]) == [(0.0, 1.0)]
    assert _presence(tracks[1]) == [(0.0, 0.0), (2.0, 0.0), (2.5, 1.0)]
    assert _presence(tracks[2]) == [(0.0, 0.0), (4.0, 0.0), (4.5, 1.0)]
    assert doc["duration_s"] == 7.0
    assert {c["key"] for c in doc["base"]["channels"] if c["visible"]} == {"DNA", "CD3", "CD8"}
    # Each fade is declared in the manifest's display transitions.
    faded = [(row["target"], row["fields"]) for row in schema.display_transitions(doc)]
    assert ("CD3", ["presence"]) in faded and ("CD8", ["presence"]) in faded


def test_a_channel_reveal_follows_the_viewers_visible_channels_or_a_chosen_order():
    doc = presets.build("channel_reveal", STACK, PLANE_SCENE)
    assert [t["target"] for t in doc["tracks"] if t["kind"] == "channel"] == ["CD3", "DNA"]
    chosen = presets.build("channel_reveal", STACK, PLANE_SCENE,
                           {"order": ["CD8", "nope", "DNA"]})
    assert [t["target"] for t in chosen["tracks"]] == ["CD8", "DNA"]


def test_a_channel_reveal_of_one_visible_channel_reveals_the_first_channels():
    """One channel on screen is nothing to reveal: the image's first
    channels are used instead (found on a real flat image with only DNA on)."""
    scene = {"view": {"mode": "plane"}, "channels": [{"key": "DNA", "visible": True},
                                                      {"key": "CD3", "visible": False}]}
    doc = presets.build("channel_reveal", STACK, scene)
    targets = [t["target"] for t in doc["tracks"] if t["kind"] == "channel"]
    assert len(targets) >= 2
    assert targets == [c["key"] for c in STACK["channels"]][:len(targets)]


def test_captions_name_each_channel_on_its_turn():
    doc = presets.build("channel_reveal", STACK, None, {"captions": True, "per_channel_s": 1})
    hud = next(t for t in doc["tracks"] if t["kind"] == "hud")
    assert [(kf["t"], kf["value"]["caption"]) for kf in hud["keyframes"]] == [
        (0.0, "Hoechst"), (1.0, "CD3"), (2.0, "CD8a"), (3.0, "")]


def test_the_marker_storyboard_shows_each_marker_alone_then_all():
    doc = presets.build("story_markers", STACK, None, {"per_channel_s": 2})
    assert doc["provenance"]["preset"]["name"] == "story_markers"
    channel_tracks = [t for t in doc["tracks"] if t["kind"] == "channel"]
    # At the end of the hold every marker is fully present.
    for track in channel_tracks:
        assert track["keyframes"][-1]["value"]["presence"] == 1.0
    assert any(t["kind"] == "hud" for t in doc["tracks"])


def test_the_timeseries_storyboard_prints_the_label_and_scale_bar():
    doc = presets.build("story_timeseries", STACK)
    assert doc["output"]["overlays"]["label"] is True
    assert doc["output"]["overlays"]["scalebar"] is True
    assert {f["t"] for f in schema.acquired_plan(doc, 30)} == set(range(6))


def test_the_orthogonal_storyboard_sweeps_every_plane_in_sections_mode():
    doc = presets.build("story_orthogonal", STACK, PLANE_SCENE)
    assert doc["base"]["view"]["mode"] == "sections"
    assert {f["z"] for f in schema.acquired_plan(doc, 30)} == set(range(12))


# -- parameters ---------------------------------------------------------------------------


def test_parameters_are_clamped_and_unknown_ones_dropped():
    orbit = presets.PRESETS["orbit"]
    p = orbit.params({"turns": 99, "duration_s": -5, "axis": "w", "direction": 7,
                      "loop": "yes", "bogus": 1, "pivot": [0, 0, 0.1], "title": "Mine"}, STACK)
    assert p["turns"] == 10.0
    assert p["duration_s"] == 0.5
    assert p["axis"] == "y"
    assert p["direction"] == 1
    assert p["loop"] is True
    assert "bogus" not in p
    # Two plain values a builder reads are passed through for an agent.
    assert p["pivot"] == [0, 0, 0.1] and p["title"] == "Mine"
    assert presets.build("orbit", STACK, params={"title": "Mine"})["title"] == "Mine"


def test_numbers_that_are_not_numbers_take_the_default():
    p = presets.PRESETS["orbit"].params({"turns": "abc", "duration_s": float("nan")}, STACK)
    assert p["turns"] == 1.0 and p["duration_s"] == 8.0
    assert presets.PRESETS["orbit"].params({"turns": "3"}, STACK)["turns"] == 3.0


def test_bounds_named_after_the_image_are_resolved_from_its_facts():
    z = presets.PRESETS["z_sweep"]
    assert z.params({"to": 999, "from": -3, "ms_per_plane": 1}, STACK) == {
        "from": 0.0, "to": 11.0, "direction": "up", "ms_per_plane": 10.0,
        "hold_ends_s": 0.5, "loop": False}
    assert z.params({}, {**STACK, "depth": 30})["to"] == 29.0


def test_channel_and_view_parameters_are_checked_against_the_image():
    reveal = presets.PRESETS["channel_reveal"]
    assert reveal.params({"order": ["CD8", "X", "DNA"]}, STACK)["order"] == ["CD8", "DNA"]
    assert reveal.params({"order": "DNA"}, STACK)["order"] == []
    assert reveal.params({"mode": "shuffle"}, STACK)["mode"] == "add"
    zoom = presets.PRESETS["zoom"]
    assert zoom.params({"end_view": "here"}, STACK)["end_view"] is None
    assert zoom.params({"factor": 3.0}, STACK)["factor"] == 4.0
