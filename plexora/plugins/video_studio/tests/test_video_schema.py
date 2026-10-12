"""What a video document is allowed to say, and what each frame of it shows.

The schema is where Video Studio's one hard rule lives: acquired timepoints and
planes are never interpolated. A frame shows a plane that was imaged at a
timepoint that was imaged, so the track kinds that move through acquired data
take only whole values and change only in steps, and `normalize_track` refuses
anything else with an IntegrityError -- the error the routes answer 422 for
and the agent sees as a rule, not a typo. These tests pin that rule from every
side a document can come in: a keyframe, an easing, a sweep's bounds.

The rest is the arithmetic the manifest and the agent's summary rely on without
a browser: how many frames a video has, which content time each frame shows
(a loop never renders its seam frame), which plane a sweep shows at each
moment, and the display changes an export must declare.
"""

import math

import pytest

from plexora.plugins.video_studio.server import schema
from plexora.plugins.video_studio.server.schema import IntegrityError, UnreadableVideo

STACK = {"datasource": "demo", "image_size": [800, 600], "depth": 12, "frames": 5,
         "channels": [{"key": "DNA", "name": "DNA"}, {"key": "CD3", "name": "CD3"}]}
FLAT = {"datasource": "flat", "image_size": [800, 600], "depth": 1, "frames": 1}


def _project(tracks=(), *, source=STACK, duration=8.0, loop=False, **extra):
    return schema.normalize_project({"video_id": "vid_0123456789ab", "source": source,
                                     "duration_s": duration, "loop": loop,
                                     "tracks": list(tracks), **extra})


def _track(kind, keyframes=(), **extra):
    return schema.normalize_track({"kind": kind, "keyframes": list(keyframes), **extra},
                                  source=STACK, duration=8.0)


def _sweep(kind="acquired_z", *, start=0, end=11, bounce=False, repeat=1, range_=None):
    raw = {"kind": kind, "mode": "procedural",
           "procedural": {"generator": "sweep",
                          "params": {"from": start, "to": end, "bounce": bounce,
                                     "repeat": repeat}}}
    if range_ is not None:
        raw["range"] = list(range_)
    return raw


# -- defaults -------------------------------------------------------------------


def test_an_empty_document_normalises_to_a_complete_one():
    """Every field the client reads is present with its default, so neither
    side has to guess what a missing key means."""
    doc = schema.normalize_project({})
    assert doc["schema_version"] == schema.SCHEMA_VERSION
    assert doc["title"] == schema.DEFAULT_TITLE
    assert doc["revision"] == 0
    assert doc["duration_s"] == 8.0
    assert doc["loop"] is False
    assert doc["tracks"] == []
    assert doc["viewpoints"] == {} and doc["targets"] == {} and doc["scenes"] == []
    assert doc["provenance"] == {"created_by": "ui", "preset": None}
    out = doc["output"]
    assert (out["width"], out["height"], out["fps"]) == (1920, 1080, 30)
    assert (out["format"], out["quality"], out["bitrate_mbps"]) == ("mp4", "good", None)
    # The scale bar and label are on by default; nothing else is drawn over the data.
    assert {k for k, v in out["overlays"].items() if v} == {"scalebar", "label"}
    assert set(out["overlays"]) == set(schema.OVERLAYS)
    view = doc["base"]["view"]
    assert (view["t"], view["z"], view["mode"], view["slab"]) == (0, 0, "plane", None)
    assert view["clip"] == {"x": [0.0, 1.0], "y": [0.0, 1.0], "z": [0.0, 1.0]}
    assert doc["base"]["dims"] == "2d"
    assert doc["source"]["depth"] == 1 and doc["source"]["frames"] == 1


def test_normalising_twice_changes_nothing():
    """Operations renormalise after every batch, so a second pass that moved
    anything would make every save a silent edit."""
    doc = _project([_sweep(), {"kind": "channel", "target": "DNA",
                               "keyframes": [{"t": 0, "value": {"presence": 0}},
                                             {"t": 2, "value": {"presence": 1}}]}])
    assert schema.normalize_project(doc) == doc


def test_a_stack_starts_in_3d_and_its_view_is_clamped_to_what_was_acquired():
    doc = schema.normalize_project({"source": STACK,
                                    "base": {"view": {"z": 99, "t": 42, "mode": "nonsense"}}})
    assert doc["base"]["dims"] == "3d"
    assert doc["base"]["view"]["z"] == 11
    assert doc["base"]["view"]["t"] == 4
    assert doc["base"]["view"]["mode"] == "plane"


def test_a_slab_in_the_base_view_is_ordered_and_clamped():
    doc = schema.normalize_project({"source": STACK, "base": {
        "view": {"slab": {"z0": 30, "z1": 2, "op": "median"}}}})
    assert doc["base"]["view"]["slab"] == {"z0": 2, "z1": 11, "op": "max"}


def test_duplicate_track_ids_are_made_unique():
    doc = _project([{**_sweep(), "track_id": "trk_same"},
                    {**_sweep("acquired_t", end=4), "track_id": "trk_same"}])
    ids = [t["track_id"] for t in doc["tracks"]]
    assert len(set(ids)) == 2 and "trk_same" in ids


def test_malformed_tracks_are_value_errors_not_integrity_errors():
    """A typo is the caller's mistake; IntegrityError is reserved for asking to
    show what was never acquired, which the client explains differently."""
    for raw in ({"kind": "hologram"}, {"kind": "channel"},
                {"kind": "acquired_z", "mode": "procedural",
                 "procedural": {"generator": "orbit"}}):
        with pytest.raises(ValueError) as info:
            schema.normalize_track(raw, source=STACK)
        assert not isinstance(info.value, IntegrityError), raw
    with pytest.raises(ValueError):
        schema.normalize_project({"tracks": [_sweep()] * (schema.MAX_TRACKS + 1)})
    with pytest.raises(ValueError):
        schema.normalize_project({"tracks": {"not": "a list"}})


def test_video_ids_are_safe_directory_names():
    assert schema.validate_video_id("vid_0123456789ab") == "vid_0123456789ab"
    assert schema.VIDEO_ID_PATTERN.match(schema.new_video_id())
    for bad in ("..", "vid_../../etc", "vid_0123456789AB", "vid_0123", None, 7):
        with pytest.raises(ValueError):
            schema.validate_video_id(bad)


# -- acquired data is never interpolated ---------------------------------------


STEP_VALUES = {"acquired_z": 3, "acquired_t": 1, "slab": {"z0": 1, "z1": 3},
               "view_mode": "volume"}


@pytest.mark.parametrize("kind", sorted(STEP_VALUES))
def test_step_kinds_refuse_any_easing_but_step(kind):
    """An eased plane change would ask the renderer for plane 3.4 at some
    frame: a picture of nothing that was ever imaged."""
    for ease in ("linear", "cubic_in_out", "cinematic"):
        with pytest.raises(IntegrityError):
            _track(kind, [{"t": 0, "value": STEP_VALUES[kind], "ease": ease}])
    for ease in (None, "step", "hold"):
        track = _track(kind, [{"t": 0, "value": STEP_VALUES[kind], "ease": ease}])
        assert track["keyframes"][0]["ease"] == "step"


@pytest.mark.parametrize("kind, value", [
    ("acquired_z", 3.5), ("acquired_z", "3"), ("acquired_z", True), ("acquired_z", None),
    ("acquired_t", 0.5),
    ("slab", {"z0": 1.5, "z1": 3}), ("slab", {"z0": 1, "z1": 2.25}), ("slab", "1-3"),
    ("view_mode", "hologram"),
])
def test_fractional_or_unwhole_acquired_values_are_refused(kind, value):
    with pytest.raises(IntegrityError):
        _track(kind, [{"t": 0, "value": value}])


def test_a_whole_float_is_a_whole_index_and_an_index_is_clamped_to_the_stack():
    track = _track("acquired_z", [{"t": 0, "value": 3.0}, {"t": 1, "value": 99},
                                  {"t": 2, "value": -4}])
    values = [kf["value"] for kf in track["keyframes"]]
    assert values == [3, 11, 0]
    assert all(type(v) is int for v in values)


def test_a_sweep_refuses_easing_and_fractional_bounds():
    """A sweep steps through acquired data at an even pace: an eased sweep
    would give the middle planes less time than the ends."""
    raw = _sweep()
    raw["procedural"]["ease"] = "cubic_in_out"
    with pytest.raises(IntegrityError):
        schema.normalize_track(raw, source=STACK)
    with pytest.raises(IntegrityError):
        schema.normalize_track(_sweep(start=2.5), source=STACK)
    with pytest.raises(IntegrityError):
        schema.normalize_track(_sweep(end=7.25), source=STACK)
    ok = schema.normalize_track(_sweep(), source=STACK)
    assert ok["procedural"]["ease"] == "linear"
    assert ok["procedural"]["params"] == {"from": 0, "to": 11, "bounce": False, "repeat": 1}


def test_a_whole_document_with_an_interpolated_plane_is_refused():
    with pytest.raises(IntegrityError):
        _project([{"kind": "acquired_z", "keyframes": [{"t": 0, "value": 0},
                                                       {"t": 1, "value": 2.5}]}])


def test_integrity_errors_are_value_errors():
    """Callers that catch ValueError for "the request is wrong" catch this too;
    the routes catch it first to answer 422 rather than 400."""
    assert issubclass(IntegrityError, ValueError)


def test_smooth_kinds_keep_their_easing():
    track = _track("clip", [{"t": 0, "value": {"z": [0, 1]}, "ease": "cubic_in_out"},
                            {"t": 1, "value": {"z": [0.2, 0.8]}, "ease": "made_up"}])
    assert [kf["ease"] for kf in track["keyframes"]] == ["cubic_in_out", "linear"]


# -- colour is held -------------------------------------------------------------


def test_channel_colour_is_a_held_field_normalised_to_hex():
    """Blending red into green paints yellow, and yellow means co-expression:
    colour is a step field, never one the timeline interpolates."""
    assert schema.CHANNEL_FIELDS["color"] == "step"
    assert schema.CHANNEL_FIELDS["presence"] == "lerp"
    track = _track("channel", [{"t": 0, "value": {"color": "#FF0000"}},
                               {"t": 1, "value": {"color": {"r": 0, "g": 255, "b": 0}}},
                               {"t": 2, "value": {"color": "green"}}], target="DNA")
    assert [kf["value"]["color"] for kf in track["keyframes"]] == ["#ff0000", "#00ff00",
                                                                    "#ffffff"]


def test_a_colour_change_is_not_listed_as_a_display_transition():
    """The manifest lists what changes smoothly; a colour switch is a cut, and
    listing it as a fade would describe a blend that never happens."""
    doc = _project([{"kind": "channel", "target": "DNA", "keyframes": [
        {"t": 0, "value": {"color": "#ff0000", "presence": 1}},
        {"t": 2, "value": {"color": "#00ff00", "presence": 1}}]}])
    assert schema.display_transitions(doc) == []


# -- output ----------------------------------------------------------------------


def test_output_sides_are_even_and_within_bounds():
    """H.264 4:2:0 refuses odd sides; a size that works for one format and not
    another would silently change on export."""
    out = schema.normalize_output({"width": 1921, "height": 1081})
    assert (out["width"], out["height"]) == (1920, 1080)
    out = schema.normalize_output({"width": 10, "height": 65})
    assert (out["width"], out["height"]) == (schema.MIN_OUTPUT_SIDE, 64)
    out = schema.normalize_output({"width": 99999, "height": 7681})
    assert (out["width"], out["height"]) == (schema.MAX_OUTPUT_SIDE, schema.MAX_OUTPUT_SIDE)
    for raw in ({"width": 333, "height": 777}, {"width": "641", "height": None}):
        out = schema.normalize_output(raw)
        assert out["width"] % 2 == 0 and out["height"] % 2 == 0


def test_output_choices_fall_back_to_defaults_and_numbers_are_clamped():
    out = schema.normalize_output({"fps": 0, "format": "avi", "quality": "ultra",
                                   "bitrate_mbps": 0, "transparent": 1,
                                   "overlays": {"axes": True, "scalebar": False, "x": True}})
    assert out["fps"] == 1
    assert (out["format"], out["quality"], out["bitrate_mbps"]) == ("mp4", "good", None)
    assert out["transparent"] is True
    assert out["overlays"]["axes"] is True and out["overlays"]["scalebar"] is False
    assert "x" not in out["overlays"]
    assert schema.normalize_output({"fps": 500})["fps"] == 120
    assert schema.normalize_output({"bitrate_mbps": 12.5})["bitrate_mbps"] == 12.5


# -- versions --------------------------------------------------------------------


def test_a_document_from_a_newer_build_is_unreadable_not_reinterpreted():
    """Reading it with today's rules would drop what the newer schema added,
    and the next save would write that loss back."""
    with pytest.raises(UnreadableVideo):
        schema.normalize_project({"schema_version": schema.SCHEMA_VERSION + 1})
    assert not issubclass(UnreadableVideo, ValueError)
    assert schema.normalize_project({"schema_version": 0})["schema_version"] == 1


# -- frames and content time -------------------------------------------------------


def test_frame_count_is_duration_times_fps_and_at_least_one():
    assert schema.frame_count(8.0, 30) == 240
    assert schema.frame_count(0.5, 24) == 12
    assert schema.frame_count(0.1, 24) == 2
    assert schema.frame_count(0.01, 30) == 1


def test_a_loop_never_renders_its_seam_frame():
    """Frame N of a loop would equal frame 0; sampling k*D/N means it is never
    rendered, and the loop plays without a doubled frame."""
    n, duration = 240, 8.0
    times = [schema.content_time(k, n, duration, True) for k in range(n)]
    assert times[0] == 0.0
    assert times[1] == pytest.approx(duration / n)
    assert max(times) < duration
    assert schema.content_time(n, n, duration, True) == pytest.approx(duration)
    gaps = {round(b - a, 9) for a, b in zip(times, times[1:])}
    assert gaps == {round(duration / n, 9)}


def test_a_non_loop_shows_both_its_first_and_last_moment():
    n, duration = 240, 8.0
    times = [schema.content_time(k, n, duration, False) for k in range(n)]
    assert times[0] == 0.0
    assert times[-1] == pytest.approx(duration)
    assert times[1] == pytest.approx(duration / (n - 1))
    assert schema.content_time(0, 1, duration, False) == 0.0
    assert schema.content_time(0, 1, duration, True) == 0.0


# -- sweeps ------------------------------------------------------------------------


def _samples(params, count=1200):
    return [schema.sweep_index(params, (i + 0.5) / count) for i in range(count)]


def test_a_sweep_shows_every_plane_once_for_an_equal_share_of_the_time():
    shown = _samples({"from": 0, "to": 9})
    assert shown == sorted(shown)
    assert set(shown) == set(range(10))
    assert {shown.count(z) for z in range(10)} == {120}


def test_a_downward_sweep_runs_the_other_way():
    shown = _samples({"from": 9, "to": 0})
    assert shown == sorted(shown, reverse=True)
    assert set(shown) == set(range(10))


def test_a_bounce_does_not_show_the_turning_plane_twice():
    """0,1,2,3,2,1 -- and not 0 again at the end, so a bouncing loop does not
    stutter at the bottom either."""
    params = {"from": 0, "to": 3, "bounce": True}
    assert [schema.sweep_index(params, (i + 0.5) / 6) for i in range(6)] == [0, 1, 2, 3, 2, 1]
    shown = _samples(params)
    assert {shown.count(z) for z in (1, 2)} == {400}
    assert shown.count(3) == 200 and shown.count(0) == 200


def test_a_repeat_runs_the_sweep_again():
    params = {"from": 0, "to": 2, "repeat": 2}
    assert [schema.sweep_index(params, (i + 0.5) / 6) for i in range(6)] == [0, 1, 2, 0, 1, 2]


def test_sweep_progress_is_clamped_and_a_single_plane_is_held():
    params = {"from": 2, "to": 6}
    assert schema.sweep_index(params, -1.0) == 2
    assert schema.sweep_index(params, 1.0) == 6
    assert schema.sweep_index(params, 7.0) == 6
    single = {"from": 4, "to": 4, "bounce": True, "repeat": 3}
    assert {schema.sweep_index(single, u / 10) for u in range(11)} == {4}


def test_sweep_indices_are_always_integers():
    params = {"from": 0, "to": 7, "bounce": True, "repeat": 3}
    assert all(type(schema.sweep_index(params, u / 997)) is int for u in range(998))


# -- the acquired plan -----------------------------------------------------------------


def test_a_z_sweep_plan_is_monotonic_and_reaches_every_plane():
    doc = _project([_sweep()], duration=2.0)
    plan = schema.acquired_plan(doc, 30)
    assert [f["k"] for f in plan] == list(range(60))
    zs = [f["z"] for f in plan]
    assert zs == sorted(zs)
    assert zs[0] == 0 and zs[-1] == 11
    assert set(zs) == set(range(12))
    assert all(f["t"] == 0 and f["mode"] == "plane" for f in plan)


def test_a_looping_sweep_of_one_frame_per_plane_shows_each_plane_exactly_once():
    for depth in (12, 13, 17):
        source = {**STACK, "depth": depth}
        doc = _project([_sweep(end=depth - 1)], source=source, duration=depth / 30, loop=True)
        assert [f["z"] for f in schema.acquired_plan(doc, 30)] == list(range(depth)), depth


def test_a_keyframed_plane_track_steps_and_never_shows_a_plane_between():
    doc = _project([{"kind": "acquired_z", "keyframes": [
        {"t": 0.0, "value": 2}, {"t": 1.0, "value": 5}, {"t": 1.5, "value": "$current"}]}],
        duration=2.0, base={"view": {"z": 7}})
    plan = schema.acquired_plan(doc, 10)
    by_time = [(schema.content_time(f["k"], len(plan), 2.0, False), f["z"]) for f in plan]
    assert {z for _, z in by_time} == {2, 5, 7}
    assert all(z == 2 for t, z in by_time if t < 1.0 - 1e-9)
    assert all(z == 5 for t, z in by_time if 1.0 <= t < 1.5 - 1e-9)
    # `$current` is the base view's plane, not a value of its own.
    assert all(z == 7 for t, z in by_time if t >= 1.5)


def test_a_procedural_track_holds_the_base_value_before_its_range_starts():
    doc = _project([_sweep(start=3, end=8, range_=(1.0, 2.0))], duration=3.0,
                   base={"view": {"z": 10}})
    assert schema.acquired_at(doc, 0.0)["z"] == 10
    assert schema.acquired_at(doc, 0.999)["z"] == 10
    assert schema.acquired_at(doc, 1.0)["z"] == 3
    assert schema.acquired_at(doc, 2.0)["z"] == 8
    # After the range it holds its last plane.
    assert schema.acquired_at(doc, 2.9)["z"] == 8


def test_muted_tracks_change_nothing_and_timepoints_and_modes_step_too():
    doc = _project([{**_sweep(), "muted": True},
                    {"kind": "acquired_t", "keyframes": [{"t": 0, "value": 1},
                                                         {"t": 4, "value": 3}]},
                    {"kind": "view_mode", "keyframes": [{"t": 2, "value": "volume"}]}])
    early, late = schema.acquired_at(doc, 1.0), schema.acquired_at(doc, 5.0)
    assert early["z"] == 0 and late["z"] == 0
    assert (early["t"], late["t"]) == (1, 3)
    # Before its first keyframe a step track holds that keyframe's value.
    assert (early["mode"], late["mode"]) == ("volume", "volume")


def test_a_slab_reveal_grows_from_its_fixed_edge():
    doc = _project([{"kind": "slab", "mode": "procedural", "range": [0, 4],
                     "procedural": {"generator": "reveal",
                                    "params": {"from": 11, "to": 0, "op": "mean"}}}])
    first, last = schema.acquired_at(doc, 0.0)["slab"], schema.acquired_at(doc, 4.0)["slab"]
    assert first == {"z0": 11, "z1": 11, "op": "mean"}
    assert last == {"z0": 0, "z1": 11, "op": "mean"}
    sizes = [schema.acquired_at(doc, t / 10)["slab"] for t in range(41)]
    assert all(s["z1"] == 11 for s in sizes)
    assert [s["z0"] for s in sizes] == sorted((s["z0"] for s in sizes), reverse=True)


# -- display transitions ------------------------------------------------------------------


def test_display_transitions_list_presence_and_window_changes():
    doc = _project([
        {"kind": "channel", "target": "DNA", "track_id": "trk_dna", "keyframes": [
            {"t": 0, "value": {"presence": 0}}, {"t": 1, "value": {"presence": 1}},
            {"t": 3, "value": {"window": [0.1, 0.6]}}]},
        {"kind": "channel", "target": "CD3", "muted": True, "keyframes": [
            {"t": 0, "value": {"presence": 0}}, {"t": 1, "value": {"presence": 1}}]},
        {"kind": "transition", "track_id": "trk_x", "keyframes": [
            {"t": 5, "value": {"alpha": 0}}, {"t": 6, "value": {"alpha": 1}}]},
        {"kind": "emphasis", "target": "CD3", "keyframes": [{"t": 5, "value": {}}]},
    ])
    rows = schema.display_transitions(doc)
    assert rows == [
        {"track_id": "trk_dna", "kind": "channel", "target": "DNA", "fields": ["presence"],
         "from_s": 0.0, "to_s": 1.0},
        {"track_id": "trk_dna", "kind": "channel", "target": "DNA", "fields": ["window"],
         "from_s": 1.0, "to_s": 3.0},
        {"track_id": "trk_x", "kind": "transition", "target": None, "from_s": 5.0,
         "to_s": 6.0},
    ]


# -- warnings ---------------------------------------------------------------------------------


def _turn(degrees):
    half = math.radians(degrees) / 2
    return {"q": [0.0, math.sin(half), 0.0, math.cos(half)], "dist": 2.2, "pan": [0, 0]}


def test_a_camera_turn_over_150_degrees_is_warned_about():
    """Past 150° the shorter way round is ambiguous to the eye: the warning
    asks for a position in between."""
    doc = _project([{"kind": "camera3d", "track_id": "trk_cam", "keyframes": [
        {"t": 0, "value": _turn(0)}, {"t": 2, "value": _turn(170)},
        {"t": 4, "value": _turn(260)}]}])
    codes = [(w["code"], w["track_id"]) for w in schema.validate(doc)]
    assert codes == [("large_turn", "trk_cam")]
    calm = _project([{"kind": "camera3d", "keyframes": [
        {"t": 0, "value": _turn(0)}, {"t": 2, "value": _turn(90)},
        {"t": 4, "value": _turn(180)}]}])
    assert schema.validate(calm) == []


def test_depth_and_time_tracks_on_a_flat_image_are_warned_about():
    doc = _project([{**_sweep(), "track_id": "trk_z"},
                    {"kind": "acquired_t", "track_id": "trk_t",
                     "keyframes": [{"t": 0, "value": 0}]},
                    {"kind": "clip", "track_id": "trk_clip", "keyframes": []},
                    {"kind": "camera3d", "track_id": "trk_cam", "keyframes": []}],
                   source=FLAT)
    codes = sorted((w["code"], w["track_id"]) for w in schema.validate(doc))
    assert codes == [("flat_image", "trk_cam"), ("no_depth", "trk_clip"),
                     ("no_depth", "trk_z"), ("no_time", "trk_t")]
    assert schema.validate(_project([_sweep()])) == []


def test_a_channel_track_for_a_channel_the_image_lacks_is_warned_about():
    base = {"channels": [{"key": "DNA"}, {"key": "CD3"}]}
    doc = _project([{"kind": "channel", "target": "CD99", "track_id": "trk_99",
                     "keyframes": []},
                    {"kind": "channel", "target": "DNA", "keyframes": []}], base=base)
    assert [(w["code"], w["track_id"]) for w in schema.validate(doc)] == [
        ("unknown_channel", "trk_99")]
    # With no channels in the base scene there is nothing to check against.
    bare = _project([{"kind": "channel", "target": "CD99", "keyframes": []}])
    assert schema.validate(bare) == []


def test_manifest_tracks_summarise_generators_and_keyframe_counts():
    doc = _project([{**_sweep(), "track_id": "trk_z"},
                    {"kind": "channel", "target": "DNA", "track_id": "trk_c",
                     "keyframes": [{"t": 0, "value": {}}, {"t": 1, "value": {}}]}])
    rows = {row["track_id"]: row for row in schema.manifest_tracks(doc)}
    assert rows["trk_z"]["generator"] == "sweep" and rows["trk_z"]["params"]["to"] == 11
    assert rows["trk_c"]["keyframes"] == 2 and "generator" not in rows["trk_c"]


def test_frame_counts_round_halves_up_like_the_browser():
    """5.5 s at 15 fps is 82.5 frames: the browser renders 83 (Math.round),
    so the server must expect 83 too -- found when an export of exactly that
    length was refused."""
    assert schema.frame_count(5.5, 15) == 83
    assert schema.frame_count(0.5, 5) == 3
    assert schema.frame_count(2.0, 30) == 60
