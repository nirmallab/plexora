"""The edits a video accepts, and the edit that undoes each one.

Every change to a saved video -- from the timeline, the path editor or an agent
-- is a list of operations applied in order, all or nothing:

    [{"op": "update_keyframe", "track_id": "trk_1", "kf_id": "kf_2",
      "changes": {"t": 3.5}}]

The document is renormalised after the whole batch, so an edit that would make
a track interpolate acquired data is refused by the same rule that refuses it
anywhere else (`schema.normalize_track`), and nothing is stored.

`invert_operations(document, ops)` returns the batch that undoes `ops` on that
document. The client's undo stack and the agent's `undo_hint` both use it: an
agent's edit can be reversed by core's `undo_operation` without either side
remembering what the video looked like before.
"""

from __future__ import annotations

import copy

from plexora.plugins.video_studio.server import schema

OPS = ("set_title", "set_duration", "set_loop", "set_output", "set_base", "add_track",
       "remove_track", "update_track", "move_track", "add_keyframe", "update_keyframe",
       "remove_keyframe", "set_viewpoint", "set_target", "set_scenes", "set_notes")

#: Track fields an `update_track` may change. The id and kind are identity.
TRACK_FIELDS = ("label", "range", "muted", "mode", "procedural", "interp", "target",
                "keyframes")
KEYFRAME_FIELDS = ("t", "value", "ease", "hold", "label")


def _track_index(document, track_id):
    for index, track in enumerate(document["tracks"]):
        if track["track_id"] == track_id:
            return index
    raise ValueError(f"no track {track_id!r}")


def _keyframe_index(track, kf_id):
    for index, kf in enumerate(track["keyframes"]):
        if kf["kf_id"] == kf_id:
            return index
    raise ValueError(f"no keyframe {kf_id!r} in track {track['track_id']!r}")


def _apply_one(document, op):
    kind = op.get("op") if isinstance(op, dict) else None
    if kind not in OPS:
        raise ValueError(f"unknown operation {kind!r}")
    if kind == "set_title":
        document["title"] = schema.clean_text(op.get("title")) or schema.DEFAULT_TITLE
    elif kind == "set_duration":
        previous = document["duration_s"]
        document["duration_s"] = op.get("duration_s")
        if op.get("scale_tracks"):
            _scale_tracks(document, op.get("from_s") or previous, op.get("duration_s"))
    elif kind == "set_loop":
        document["loop"] = bool(op.get("loop"))
    elif kind == "set_output":
        output = op.get("output")
        if not isinstance(output, dict):
            raise ValueError("set_output needs an output object")
        merged = dict(document["output"])
        merged.update(output)
        if isinstance(output.get("overlays"), dict):
            merged["overlays"] = {**document["output"]["overlays"], **output["overlays"]}
        document["output"] = merged
    elif kind == "set_base":
        if not isinstance(op.get("base"), dict):
            raise ValueError("set_base needs a scene")
        document["base"] = op["base"]
    elif kind == "add_track":
        track = op.get("track")
        if not isinstance(track, dict):
            raise ValueError("add_track needs a track")
        if track.get("track_id") and any(t["track_id"] == track["track_id"]
                                         for t in document["tracks"]):
            raise ValueError(f"a track {track['track_id']!r} already exists")
        index = op.get("index")
        index = len(document["tracks"]) if not isinstance(index, int) else max(
            0, min(len(document["tracks"]), index))
        # Normalised on the way in, so a later operation in the same batch
        # (add_keyframe on this track) finds every field it reads.
        document["tracks"].insert(index, schema.normalize_track(
            copy.deepcopy(track), source=schema.normalize_source(document.get("source")),
            duration=document.get("duration_s")))
    elif kind == "remove_track":
        document["tracks"].pop(_track_index(document, op.get("track_id")))
    elif kind == "update_track":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        changes = op.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("update_track needs changes")
        for field, value in changes.items():
            if field not in TRACK_FIELDS:
                raise ValueError(f"a track has no editable field {field!r}")
            track[field] = copy.deepcopy(value)
    elif kind == "move_track":
        index = _track_index(document, op.get("track_id"))
        track = document["tracks"].pop(index)
        to = op.get("index")
        to = max(0, min(len(document["tracks"]), to if isinstance(to, int) else index))
        document["tracks"].insert(to, track)
    elif kind == "add_keyframe":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        keyframe = op.get("keyframe")
        if not isinstance(keyframe, dict):
            raise ValueError("add_keyframe needs a keyframe")
        if keyframe.get("kf_id") and any(kf["kf_id"] == keyframe["kf_id"]
                                         for kf in track["keyframes"]):
            raise ValueError(f"a keyframe {keyframe['kf_id']!r} already exists")
        if track["mode"] != "keyframes":
            track["mode"] = "keyframes"
            track["procedural"] = None
        track["keyframes"].append(copy.deepcopy(keyframe))
    elif kind == "update_keyframe":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        kf = track["keyframes"][_keyframe_index(track, op.get("kf_id"))]
        changes = op.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("update_keyframe needs changes")
        for field, value in changes.items():
            if field not in KEYFRAME_FIELDS:
                raise ValueError(f"a keyframe has no editable field {field!r}")
            kf[field] = copy.deepcopy(value)
    elif kind == "remove_keyframe":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        track["keyframes"].pop(_keyframe_index(track, op.get("kf_id")))
    elif kind == "set_viewpoint":
        name = schema.clean_text(op.get("name"), 64)
        if not name:
            raise ValueError("a viewpoint needs a name")
        if op.get("viewpoint") is None:
            document["viewpoints"].pop(name, None)
        else:
            document["viewpoints"][name] = op["viewpoint"]
    elif kind == "set_target":
        key = schema.clean_text(op.get("key"), 64)
        if not key:
            raise ValueError("a target needs a key")
        if op.get("target") is None:
            document["targets"].pop(key, None)
        else:
            document["targets"][key] = op["target"]
    elif kind == "set_scenes":
        document["scenes"] = op.get("scenes") or []
    elif kind == "set_notes":
        document["notes"] = op.get("notes") or []


def _scale_tracks(document, from_s, to_s):
    """Stretch every keyframe and range when the duration changes with
    `scale_tracks`, so a 10 s orbit lengthened to 20 s is still one orbit."""
    try:
        ratio = float(to_s) / float(from_s)
    except (TypeError, ValueError, ZeroDivisionError):
        return
    for track in document["tracks"]:
        track["range"] = [v * ratio for v in track["range"]]
        for kf in track["keyframes"]:
            kf["t"] *= ratio
            kf["hold"] = kf.get("hold", 0.0) * ratio


def apply_operations(document, ops) -> dict:
    """A new document with `ops` applied, normalised. The input is untouched;
    a failure anywhere raises and nothing is half-applied."""
    if not isinstance(ops, list) or not ops:
        raise ValueError("expected a non-empty list of operations")
    working = copy.deepcopy(document)
    for op in ops:
        _apply_one(working, op)
    normalized = schema.normalize_project(working, video_id=document.get("video_id"))
    normalized["revision"] = document.get("revision", 0)
    normalized["created_at"] = document.get("created_at", "")
    return normalized


def _invert_one(document, op):
    kind = op.get("op")
    if kind == "set_title":
        return {"op": "set_title", "title": document["title"]}
    if kind == "set_duration":
        inverse = {"op": "set_duration", "duration_s": document["duration_s"]}
        if op.get("scale_tracks"):
            inverse.update(scale_tracks=True, from_s=op.get("duration_s"))
            return inverse
        # Shortening clamps ranges and keyframe times; undoing it puts every
        # track it touched back as it was, after the duration.
        after = apply_operations(document, [op])
        restore = [{"op": "update_track", "track_id": before["track_id"],
                    "changes": {"range": copy.deepcopy(before["range"]),
                                "keyframes": copy.deepcopy(before["keyframes"])}}
                   for before, now in zip(document["tracks"], after["tracks"])
                   if before["range"] != now["range"] or before["keyframes"] != now["keyframes"]]
        return [inverse, *restore]
    if kind == "set_loop":
        return {"op": "set_loop", "loop": document["loop"]}
    if kind == "set_output":
        return {"op": "set_output", "output": copy.deepcopy(document["output"])}
    if kind == "set_base":
        return {"op": "set_base", "base": copy.deepcopy(document["base"])}
    if kind == "add_track":
        after = apply_operations(document, [op])
        added = {t["track_id"] for t in after["tracks"]} - {t["track_id"]
                                                            for t in document["tracks"]}
        track_id = op["track"].get("track_id") or next(iter(added), None)
        return {"op": "remove_track", "track_id": track_id}
    if kind == "remove_track":
        index = _track_index(document, op.get("track_id"))
        return {"op": "add_track", "track": copy.deepcopy(document["tracks"][index]),
                "index": index}
    if kind == "update_track":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        return {"op": "update_track", "track_id": track["track_id"],
                "changes": {field: copy.deepcopy(track.get(field))
                            for field in op.get("changes", {})}}
    if kind == "move_track":
        return {"op": "move_track", "track_id": op.get("track_id"),
                "index": _track_index(document, op.get("track_id"))}
    if kind == "add_keyframe":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        if track["mode"] != "keyframes":
            # Adding a keyframe turned a procedural track into a keyframed one;
            # undoing it puts the generator back.
            return {"op": "update_track", "track_id": track["track_id"],
                    "changes": {"mode": track["mode"],
                                "procedural": copy.deepcopy(track["procedural"]),
                                "keyframes": copy.deepcopy(track["keyframes"])}}
        return {"op": "remove_keyframe", "track_id": track["track_id"],
                "kf_id": op["keyframe"].get("kf_id")}
    if kind == "update_keyframe":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        kf = track["keyframes"][_keyframe_index(track, op.get("kf_id"))]
        return {"op": "update_keyframe", "track_id": track["track_id"], "kf_id": kf["kf_id"],
                "changes": {field: copy.deepcopy(kf.get(field))
                            for field in op.get("changes", {})}}
    if kind == "remove_keyframe":
        track = document["tracks"][_track_index(document, op.get("track_id"))]
        kf = track["keyframes"][_keyframe_index(track, op.get("kf_id"))]
        return {"op": "add_keyframe", "track_id": track["track_id"], "keyframe": copy.deepcopy(kf)}
    if kind == "set_viewpoint":
        name = schema.clean_text(op.get("name"), 64)
        return {"op": "set_viewpoint", "name": name,
                "viewpoint": copy.deepcopy(document["viewpoints"].get(name))}
    if kind == "set_target":
        key = schema.clean_text(op.get("key"), 64)
        return {"op": "set_target", "key": key,
                "target": copy.deepcopy(document["targets"].get(key))}
    if kind == "set_scenes":
        return {"op": "set_scenes", "scenes": copy.deepcopy(document["scenes"])}
    if kind == "set_notes":
        return {"op": "set_notes", "notes": list(document["notes"])}
    raise ValueError(f"unknown operation {kind!r}")


def with_ids(ops) -> list:
    """`ops` with an id filled into every added track and keyframe, so the
    inverse can name what it removes."""
    if not isinstance(ops, list):
        raise ValueError("expected a list of operations")
    out = []
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError("an operation is an object with an `op`")
        op = copy.deepcopy(op)
        if op.get("op") == "add_track" and isinstance(op.get("track"), dict):
            op["track"].setdefault("track_id", schema.new_id("trk"))
            for kf in op["track"].get("keyframes") or []:
                if isinstance(kf, dict):
                    kf.setdefault("kf_id", schema.new_id("kf"))
        if op.get("op") == "add_keyframe" and isinstance(op.get("keyframe"), dict):
            op["keyframe"].setdefault("kf_id", schema.new_id("kf"))
        out.append(op)
    return out


def invert_operations(document, ops) -> list:
    """The batch that undoes `ops` applied to `document`, in the order to apply
    it. `ops` must already carry their ids (`with_ids`)."""
    inverses = []
    working = document
    for op in ops:
        inverse = _invert_one(working, op)
        inverses.append(inverse if isinstance(inverse, list) else [inverse])
        working = apply_operations(working, [op])
    return [step for batch in reversed(inverses) for step in batch]
