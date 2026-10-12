"""Edits as operations, and the edit that undoes each one.

Every change to a saved video -- the timeline, the path editor, an agent -- is a
batch of operations applied all or nothing, and `invert_operations` returns the
batch that puts the document back. The client's undo stack keeps exactly that
inverse, and an agent's `undo_hint` is exactly that inverse, so an inverse that
restores almost everything is an undo that quietly loses work. Hence the shape
of this file: every op kind applies, every op kind round-trips through its
inverse to an identical document, a refused op leaves its input untouched, and
a seeded random walk of edit batches checks the round trip on combinations
nobody thought to write down.
"""

import copy
import random

import pytest

from plexora.plugins.video_studio.server import operations, schema
from plexora.plugins.video_studio.server.operations import (apply_operations, invert_operations,
                                                            with_ids)

SOURCE = {"datasource": "demo", "image_size": [800, 600], "depth": 12, "frames": 5,
          "channels": [{"key": "DNA", "name": "DNA"}, {"key": "CD3", "name": "CD3"}]}
CAMERA = {"q": [0.0, 0.0, 0.0, 1.0], "dist": 2.2, "pan": [0.0, 0.0]}


def _document():
    """A saved-looking video with one track of most kinds, every id fixed."""
    return schema.normalize_project({
        "video_id": "vid_0123456789ab", "revision": 4, "title": "Original",
        "created_at": "2026-10-11T00:00:00Z", "updated_at": "2026-10-11T00:00:00Z",
        "source": SOURCE, "duration_s": 8.0,
        "base": {"view": {"z": 2}, "channels": [{"key": "DNA"}, {"key": "CD3"}]},
        "tracks": [
            {"track_id": "trk_orbit", "kind": "camera3d", "mode": "procedural",
             "range": [0, 8], "procedural": {"generator": "orbit",
                                             "params": {"axis": "y", "turns": 1}}},
            {"track_id": "trk_z", "kind": "acquired_z", "range": [0, 8], "keyframes": [
                {"kf_id": "kf_z0", "t": 0.0, "value": 0},
                {"kf_id": "kf_z1", "t": 2.0, "value": 4},
                {"kf_id": "kf_z2", "t": 5.0, "value": 9}]},
            {"track_id": "trk_dna", "kind": "channel", "target": "DNA", "range": [0, 8],
             "keyframes": [{"kf_id": "kf_c0", "t": 0.5, "value": {"presence": 0.0}},
                           {"kf_id": "kf_c1", "t": 1.5, "value": {"presence": 1.0},
                            "ease": "sine_in_out"}]},
            {"track_id": "trk_cam", "kind": "camera3d", "range": [0, 8], "keyframes": [
                {"kf_id": "kf_p0", "t": 0.25, "value": CAMERA},
                {"kf_id": "kf_p1", "t": 3.75, "value": {**CAMERA, "dist": 1.1}}]},
            {"track_id": "trk_hud", "kind": "hud", "range": [0, 8], "keyframes": [
                {"kf_id": "kf_h0", "t": 1.25, "value": {"caption": "DNA"}, "ease": "step"}]},
        ],
        "viewpoints": {"home": {"camera3d": CAMERA, "z": 2}},
        "targets": {"cell_7": {"kind": "cell", "id": "7", "status": "ok",
                               "resolved": {"x": 10, "y": 20, "z": 3}}},
        "scenes": [{"scene_id": "scn_a", "label": "Intro", "start_s": 0, "duration_s": 2}],
        "notes": ["first note"],
    })


@pytest.fixture
def doc():
    return _document()


def _track(document, track_id):
    return next(t for t in document["tracks"] if t["track_id"] == track_id)


def _round_trip(document, ops):
    """Apply `ops`, then their inverse; return (after, restored)."""
    ops = with_ids(ops)
    inverse = invert_operations(document, ops)
    after = apply_operations(document, ops)
    restored = apply_operations(after, inverse)
    return after, restored


# -- every op applies -----------------------------------------------------------------


def test_the_fixture_is_already_normalised(doc):
    """The round-trip tests compare against it, so it must be a fixed point."""
    assert schema.normalize_project(doc) == doc


def test_scalar_ops_set_what_they_name(doc):
    after = apply_operations(doc, [
        {"op": "set_title", "title": "  Renamed\x07 "},
        {"op": "set_duration", "duration_s": 12},
        {"op": "set_loop", "loop": 1},
        {"op": "set_notes", "notes": ["a", "b"]},
        {"op": "set_scenes", "scenes": [{"scene_id": "scn_b", "label": "B", "start_s": 1}]},
    ])
    assert after["title"] == "Renamed"
    assert after["duration_s"] == 12.0
    assert after["loop"] is True
    assert after["notes"] == ["a", "b"]
    assert [s["scene_id"] for s in after["scenes"]] == ["scn_b"]
    # Bookkeeping the repository owns is carried, not reset.
    assert (after["revision"], after["created_at"], after["video_id"]) == (
        4, "2026-10-11T00:00:00Z", "vid_0123456789ab")
    assert apply_operations(doc, [{"op": "set_title", "title": ""}])["title"] == \
        schema.DEFAULT_TITLE


def test_set_duration_can_stretch_every_track(doc):
    after = apply_operations(doc, [{"op": "set_duration", "duration_s": 16,
                                    "scale_tracks": True, "from_s": 8}])
    assert _track(after, "trk_z")["range"] == [0.0, 16.0]
    assert [kf["t"] for kf in _track(after, "trk_z")["keyframes"]] == [0.0, 4.0, 10.0]


def test_set_duration_scales_from_the_current_duration_when_from_s_is_omitted(doc):
    after = apply_operations(doc, [{"op": "set_duration", "duration_s": 16,
                                    "scale_tracks": True}])
    assert _track(after, "trk_z")["range"] == [0.0, 16.0]
    assert [kf["t"] for kf in _track(after, "trk_z")["keyframes"]] == [0.0, 4.0, 10.0]


def test_set_output_merges_and_keeps_the_other_overlays(doc):
    after = apply_operations(doc, [{"op": "set_output", "output": {
        "width": 641, "fps": 24, "overlays": {"axes": True}}}])
    assert after["output"]["width"] == 640
    assert after["output"]["height"] == doc["output"]["height"]
    assert after["output"]["fps"] == 24
    assert after["output"]["overlays"]["axes"] is True
    assert after["output"]["overlays"]["scalebar"] is True


def test_set_base_replaces_the_starting_scene(doc):
    after = apply_operations(doc, [{"op": "set_base", "base": {"view": {"z": 7, "mode": "volume"},
                                                              "camera3d": CAMERA}}])
    assert after["base"]["view"]["z"] == 7 and after["base"]["view"]["mode"] == "volume"
    assert after["base"]["camera3d"]["dist"] == 2.2


def test_track_ops_add_update_move_and_remove(doc):
    after = apply_operations(doc, [
        {"op": "add_track", "index": 0, "track": {"track_id": "trk_new", "kind": "clip",
                                                  "keyframes": []}},
        {"op": "update_track", "track_id": "trk_dna",
         "changes": {"label": "Nuclei", "muted": True, "range": [1, 6]}},
        {"op": "move_track", "track_id": "trk_hud", "index": 1},
        {"op": "remove_track", "track_id": "trk_orbit"},
    ])
    assert [t["track_id"] for t in after["tracks"]] == ["trk_new", "trk_hud", "trk_z",
                                                        "trk_dna", "trk_cam"]
    dna = _track(after, "trk_dna")
    assert (dna["label"], dna["muted"], dna["range"]) == ("Nuclei", True, [1.0, 6.0])


def test_keyframe_ops_add_update_and_remove(doc):
    after = apply_operations(doc, [
        {"op": "add_keyframe", "track_id": "trk_z",
         "keyframe": {"kf_id": "kf_z3", "t": 1.0, "value": 2}},
        {"op": "update_keyframe", "track_id": "trk_z", "kf_id": "kf_z2",
         "changes": {"t": 0.5, "value": 11}},
        {"op": "remove_keyframe", "track_id": "trk_z", "kf_id": "kf_z0"},
    ])
    frames = _track(after, "trk_z")["keyframes"]
    # Renormalised: sorted by time again after the batch.
    assert [(kf["kf_id"], kf["t"], kf["value"]) for kf in frames] == [
        ("kf_z2", 0.5, 11), ("kf_z3", 1.0, 2), ("kf_z1", 2.0, 4)]


def test_a_keyframe_on_a_procedural_track_turns_it_into_a_keyframed_one(doc):
    after = apply_operations(doc, [{"op": "add_keyframe", "track_id": "trk_orbit",
                                    "keyframe": {"kf_id": "kf_o", "t": 1, "value": CAMERA}}])
    orbit = _track(after, "trk_orbit")
    assert orbit["mode"] == "keyframes" and orbit["procedural"] is None
    assert [kf["kf_id"] for kf in orbit["keyframes"]] == ["kf_o"]


def test_viewpoints_and_targets_are_set_and_removed_by_name(doc):
    after = apply_operations(doc, [
        {"op": "set_viewpoint", "name": "close", "viewpoint": {"camera3d": {**CAMERA,
                                                                            "dist": 0.5}}},
        {"op": "set_viewpoint", "name": "home", "viewpoint": None},
        {"op": "set_target", "key": "roi_1", "target": {"kind": "roi", "id": "1"}},
        {"op": "set_target", "key": "cell_7", "target": None},
    ])
    assert set(after["viewpoints"]) == {"close"}
    assert after["viewpoints"]["close"]["camera3d"]["dist"] == 0.5
    assert set(after["targets"]) == {"roi_1"}


def test_with_ids_names_every_added_track_and_keyframe():
    ops = with_ids([{"op": "add_track", "track": {"kind": "hud", "keyframes": [{"t": 0}]}},
                    {"op": "add_keyframe", "track_id": "x", "keyframe": {"t": 1}},
                    {"op": "add_keyframe", "track_id": "x",
                     "keyframe": {"t": 1, "kf_id": "kf_mine"}}])
    assert ops[0]["track"]["track_id"].startswith("trk_")
    assert ops[0]["track"]["keyframes"][0]["kf_id"].startswith("kf_")
    assert ops[1]["keyframe"]["kf_id"].startswith("kf_")
    assert ops[2]["keyframe"]["kf_id"] == "kf_mine"


# -- refusals leave the input untouched -----------------------------------------------


@pytest.mark.parametrize("ops", [
    [{"op": "explode"}],
    ["set_title"],
    [{"op": "remove_track", "track_id": "trk_nope"}],
    [{"op": "update_track", "track_id": "trk_z", "changes": {"kind": "acquired_t"}}],
    [{"op": "update_track", "track_id": "trk_z", "changes": "label"}],
    [{"op": "add_track", "track": {"track_id": "trk_z", "kind": "hud"}}],
    [{"op": "add_track", "track": "hud"}],
    [{"op": "add_keyframe", "track_id": "trk_z", "keyframe": {"kf_id": "kf_z1", "t": 1,
                                                              "value": 1}}],
    [{"op": "add_keyframe", "track_id": "trk_z", "keyframe": [1, 2]}],
    [{"op": "update_keyframe", "track_id": "trk_z", "kf_id": "kf_nope", "changes": {"t": 1}}],
    [{"op": "update_keyframe", "track_id": "trk_z", "kf_id": "kf_z1",
      "changes": {"kf_id": "kf_other"}}],
    [{"op": "remove_keyframe", "track_id": "trk_z", "kf_id": "kf_nope"}],
    [{"op": "set_output", "output": "4k"}],
    [{"op": "set_base", "base": None}],
    [{"op": "set_viewpoint", "name": "", "viewpoint": {}}],
    [{"op": "set_target", "key": None, "target": {}}],
    # A valid op followed by an invalid one: all or nothing.
    [{"op": "set_title", "title": "Half"}, {"op": "remove_track", "track_id": "trk_nope"}],
    # An interpolated plane, refused by the schema after the batch.
    [{"op": "add_keyframe", "track_id": "trk_z", "keyframe": {"t": 1, "value": 2.5}}],
    [{"op": "update_keyframe", "track_id": "trk_z", "kf_id": "kf_z1",
      "changes": {"ease": "linear"}}],
    [{"op": "update_track", "track_id": "trk_z", "changes": {"mode": "procedural"}}],
    [],
    "set_title",
])
def test_an_invalid_batch_raises_value_error_and_changes_nothing(doc, ops):
    before = copy.deepcopy(doc)
    with pytest.raises(ValueError):
        apply_operations(doc, ops)
    assert doc == before


def test_an_interpolated_plane_is_an_integrity_error(doc):
    with pytest.raises(schema.IntegrityError):
        apply_operations(doc, [{"op": "update_keyframe", "track_id": "trk_z",
                                "kf_id": "kf_z1", "changes": {"value": 4.5}}])


# -- every op round-trips through its inverse ---------------------------------------------


ROUND_TRIPS = {
    "set_title": [{"op": "set_title", "title": "New title"}],
    "set_duration": [{"op": "set_duration", "duration_s": 12.5}],
    "set_duration_scaled": [{"op": "set_duration", "duration_s": 16, "scale_tracks": True,
                             "from_s": 8}],
    "set_loop": [{"op": "set_loop", "loop": True}],
    "set_output": [{"op": "set_output", "output": {"width": 640, "height": 360, "fps": 60,
                                                   "format": "webm",
                                                   "overlays": {"axes": True}}}],
    "set_base": [{"op": "set_base", "base": {"view": {"z": 9, "mode": "volume"},
                                             "camera3d": CAMERA, "channels": []}}],
    "add_track": [{"op": "add_track", "track": {"kind": "channel", "target": "CD3",
                                                "keyframes": [{"t": 1, "value": {
                                                    "presence": 0.5}}]}}],
    "add_track_at_index": [{"op": "add_track", "index": 1, "track": {
        "kind": "acquired_t", "keyframes": [{"t": 0, "value": 3}]}}],
    "remove_track": [{"op": "remove_track", "track_id": "trk_dna"}],
    "remove_first_track": [{"op": "remove_track", "track_id": "trk_orbit"}],
    "update_track": [{"op": "update_track", "track_id": "trk_dna",
                      "changes": {"label": "x", "muted": True, "range": [2, 3]}}],
    "update_track_mode": [{"op": "update_track", "track_id": "trk_orbit", "changes": {
        "mode": "keyframes", "procedural": None,
        "keyframes": [{"kf_id": "kf_new", "t": 1, "value": CAMERA}]}}],
    "move_track": [{"op": "move_track", "track_id": "trk_orbit", "index": 3}],
    "move_track_to_front": [{"op": "move_track", "track_id": "trk_hud", "index": 0}],
    "add_keyframe": [{"op": "add_keyframe", "track_id": "trk_z",
                      "keyframe": {"t": 3.0, "value": 6}}],
    "add_keyframe_on_procedural": [{"op": "add_keyframe", "track_id": "trk_orbit",
                                    "keyframe": {"t": 2.0, "value": CAMERA}}],
    "update_keyframe": [{"op": "update_keyframe", "track_id": "trk_z", "kf_id": "kf_z1",
                         "changes": {"t": 6.0, "value": 11}}],
    "update_keyframe_ease": [{"op": "update_keyframe", "track_id": "trk_dna",
                              "kf_id": "kf_c0", "changes": {"ease": "expo_in",
                                                            "label": "fade"}}],
    "remove_keyframe": [{"op": "remove_keyframe", "track_id": "trk_z", "kf_id": "kf_z1"}],
    "set_viewpoint_new": [{"op": "set_viewpoint", "name": "close",
                           "viewpoint": {"camera3d": CAMERA, "z": 4}}],
    "set_viewpoint_remove": [{"op": "set_viewpoint", "name": "home", "viewpoint": None}],
    "set_target_new": [{"op": "set_target", "key": "roi_1",
                        "target": {"kind": "roi", "id": "1"}}],
    "set_target_remove": [{"op": "set_target", "key": "cell_7", "target": None}],
    "set_scenes": [{"op": "set_scenes", "scenes": []}],
    "set_notes": [{"op": "set_notes", "notes": ["one", "two"]}],
    "a_mixed_batch": [
        {"op": "set_title", "title": "Batch"},
        # A whole track; a bare one is normalised on the way in (see below).
        {"op": "add_track", "track": {"track_id": "trk_b", "kind": "hud", "mode": "keyframes",
                                      "keyframes": []}},
        {"op": "add_keyframe", "track_id": "trk_b", "keyframe": {"t": 1, "value": {}}},
        {"op": "move_track", "track_id": "trk_b", "index": 0},
        {"op": "update_keyframe", "track_id": "trk_z", "kf_id": "kf_z2",
         "changes": {"value": 1}},
        {"op": "remove_track", "track_id": "trk_cam"},
    ],
}


def test_every_op_kind_has_a_round_trip():
    covered = {op["op"] for ops in ROUND_TRIPS.values() for op in ops}
    assert covered == set(operations.OPS)


@pytest.mark.parametrize("name", sorted(ROUND_TRIPS))
def test_applying_an_op_then_its_inverse_restores_the_document(doc, name):
    after, restored = _round_trip(doc, ROUND_TRIPS[name])
    assert after != doc, "the op should have changed something"
    assert restored == doc


def test_the_inverse_of_a_keyframe_on_a_procedural_track_restores_the_generator(doc):
    ops = with_ids(ROUND_TRIPS["add_keyframe_on_procedural"])
    inverse = invert_operations(doc, ops)
    assert inverse[0]["op"] == "update_track"
    assert inverse[0]["changes"]["mode"] == "procedural"
    assert inverse[0]["changes"]["procedural"]["generator"] == "orbit"
    restored = apply_operations(apply_operations(doc, ops), inverse)
    assert _track(restored, "trk_orbit") == _track(doc, "trk_orbit")


def test_a_keyframe_can_be_added_to_a_track_added_earlier_in_the_same_batch(doc):
    after = apply_operations(doc, [
        {"op": "add_track", "track": {"track_id": "trk_b", "kind": "hud"}},
        {"op": "add_keyframe", "track_id": "trk_b",
         "keyframe": {"kf_id": "kf_b", "t": 1, "value": {"caption": "x"}}}])
    assert [kf["kf_id"] for kf in _track(after, "trk_b")["keyframes"]] == ["kf_b"]


def test_inverses_come_in_reverse_order(doc):
    ops = with_ids([{"op": "set_title", "title": "A"}, {"op": "set_loop", "loop": True}])
    assert [op["op"] for op in invert_operations(doc, ops)] == ["set_loop", "set_title"]


def test_undoing_a_shortened_duration_restores_the_clamped_tracks(doc):
    after, restored = _round_trip(doc, [{"op": "set_duration", "duration_s": 4}])
    assert _track(after, "trk_z")["range"] == [0.0, 4.0]
    assert restored == doc


# -- a random walk of edit batches -------------------------------------------------------


STEP_VALUE = {"acquired_z": lambda r: r.randint(0, 11), "acquired_t": lambda r: r.randint(0, 4)}
SMOOTH_VALUE = {
    "camera3d": lambda r: {"q": [0.0, round(r.uniform(-1, 1), 3), 0.0, 1.0],
                           "dist": round(r.uniform(0.5, 4), 3), "pan": [0.0, 0.0]},
    "channel": lambda r: {"presence": round(r.random(), 3)},
    "hud": lambda r: {"caption": r.choice(["DNA", "CD3", ""])},
}


def _value(r, kind):
    return (STEP_VALUE.get(kind) or SMOOTH_VALUE[kind])(r)


def _ease(r, kind):
    return "step" if kind in STEP_VALUE else r.choice(schema.EASINGS)


class _Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_r{self.n:05d}"


def _random_op(r, working, new_id):
    """One plausible edit of `working`, or None when the pick does not apply."""
    tracks = working["tracks"]
    kind = r.choice(["set_title", "set_duration", "set_loop", "set_output", "add_track",
                     "remove_track", "update_track", "move_track", "add_keyframe",
                     "add_keyframe", "update_keyframe", "remove_keyframe", "set_viewpoint",
                     "set_target", "set_scenes", "set_notes", "set_base"])
    duration = working["duration_s"]
    if kind == "set_title":
        return {"op": kind, "title": r.choice(["A", "B", "Rotation", ""])}
    if kind == "set_duration":
        # Shorter as well as longer: undoing a shortening restores the tracks
        # it clamped.
        return {"op": kind, "duration_s": r.choice([4.0, 6.5, 8.0, 9.5, 12.0, 20.0])}
    if kind == "set_loop":
        return {"op": kind, "loop": r.random() < 0.5}
    if kind == "set_output":
        return {"op": kind, "output": {"width": r.choice([640, 1280, 1920]),
                                       "fps": r.choice(schema.FPS_CHOICES),
                                       "overlays": {r.choice(schema.OVERLAYS): r.random() < 0.5}}}
    if kind == "set_base":
        return {"op": kind, "base": {"view": {"z": r.randint(0, 11)}}}
    if kind == "add_track":
        track_kind = r.choice(["camera3d", "acquired_z", "acquired_t", "channel", "hud"])
        track = {"track_id": new_id("trk"), "kind": track_kind, "keyframes": [
            {"kf_id": new_id("kf"), "t": round(r.uniform(0, duration), 3),
             "value": _value(r, track_kind), "ease": _ease(r, track_kind)}]}
        if track_kind == "channel":
            track["target"] = r.choice(["DNA", "CD3"])
        return {"op": kind, "track": track, "index": r.randint(0, len(tracks))}
    if kind == "set_viewpoint":
        viewpoint = r.choice([None, {"camera3d": SMOOTH_VALUE["camera3d"](r),
                                     "z": r.randint(0, 11)}])
        return {"op": kind, "name": r.choice(["home", "close"]), "viewpoint": viewpoint}
    if kind == "set_target":
        target = r.choice([None, {"kind": "point", "resolved": {"x": r.randint(0, 99)}}])
        return {"op": kind, "key": r.choice(["cell_7", "roi_2"]), "target": target}
    if kind == "set_scenes":
        return {"op": kind, "scenes": r.choice([[], [{"scene_id": new_id("scn"),
                                                       "label": "S", "start_s": 1.0}]])}
    if kind == "set_notes":
        return {"op": kind, "notes": r.choice([[], ["x"], ["a", "b"]])}
    if not tracks:
        return None
    track = r.choice(tracks)
    if kind == "remove_track":
        return {"op": kind, "track_id": track["track_id"]}
    if kind == "move_track":
        return {"op": kind, "track_id": track["track_id"], "index": r.randint(0, len(tracks))}
    if kind == "update_track":
        changes = r.choice([{"label": r.choice(["one", "two"])}, {"muted": r.random() < 0.5},
                            {"range": sorted([round(r.uniform(0, duration), 3)
                                              for _ in range(2)])}])
        return {"op": kind, "track_id": track["track_id"], "changes": changes}
    if track["kind"] not in STEP_VALUE and track["kind"] not in SMOOTH_VALUE:
        return None
    if kind == "add_keyframe":
        return {"op": kind, "track_id": track["track_id"], "keyframe": {
            "kf_id": new_id("kf"), "t": round(r.uniform(0, duration), 3),
            "value": _value(r, track["kind"]), "ease": _ease(r, track["kind"])}}
    if not track["keyframes"]:
        return None
    kf = r.choice(track["keyframes"])
    if kind == "remove_keyframe":
        return {"op": kind, "track_id": track["track_id"], "kf_id": kf["kf_id"]}
    changes = r.choice([{"t": round(r.uniform(0, duration), 3)},
                        {"value": _value(r, track["kind"])},
                        {"ease": _ease(r, track["kind"])}])
    return {"op": "update_keyframe", "track_id": track["track_id"], "kf_id": kf["kf_id"],
            "changes": changes}


def test_fifty_random_edit_batches_each_undo_to_the_original(doc):
    """Seeded, so a failure reproduces: the batch number is in the message."""
    r = random.Random(20261011)
    new_id = _Ids()
    kinds_seen = set()
    for batch_number in range(50):
        working, batch = doc, []
        for _ in range(r.randint(1, 5)):
            op = _random_op(r, working, new_id)
            if op is None:
                continue
            try:
                working = apply_operations(working, [op])
            except ValueError:
                continue
            batch.append(op)
            kinds_seen.add(op["op"])
        if not batch:
            continue
        inverse = invert_operations(doc, batch)
        after = apply_operations(doc, batch)
        restored = apply_operations(after, inverse)
        assert restored == doc, f"batch {batch_number}: {batch}"
    # The walk is only worth something if it visited most of the vocabulary.
    assert len(kinds_seen) >= 14, kinds_seen
