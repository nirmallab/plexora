"""Identifying a source image, and noticing when it has changed underneath.

A figure keeps a reference, not a copy. That is what makes it small and what
makes it fragile in one specific way: the project it points at can be re-imported
with a different slide, have its channels renamed, or be deleted. A panel drawn
on the old image renders perfectly plausibly on the new one, in the wrong place,
and nothing about the pixels says otherwise.

So every source carries a fingerprint taken at capture time, and it is compared
on open. The comparison never rerenders anything: it produces `ok`, `changed`
or `missing`, and a `changed` source keeps its cached preview until the user
says what should happen. Silently re-rendering a scientifically established
panel from materially different data is the one outcome this whole module
exists to prevent.

**Nothing here loads a datasource.** Everything is read from the project record
(config.json), which is why this can be asked about eight sources in a row
without evicting the one the user is actually looking at -- `data_model` holds a
single loaded datasource behind a lock, and touching it here would make opening
a figure library unload the viewer.

The one fact that is NOT in the record is the physical pixel size, which lives
in the OME metadata and only comes back through a loader. It is therefore
captured client-side, at the moment of capture, for the datasource already on
screen -- and stored on the source. See `figureSceneSnapshot.js`.
"""

from __future__ import annotations

from plexora import api


def channel_key(channel) -> str:
    """The stable id of one image channel.

    The last segment of the tile URL (`/generated/data/<ds>/<key>/`), which is
    generated at import from the file name and the channel's position. It
    survives renaming, which `fullname` is precisely what does not: a user who
    relabels "LSP20209_2" as "CD8a" has not changed which channel it is, and a
    figure that identified it by name would lose it.
    """
    src = (channel or {}).get("src") or ""
    return src.strip("/").rsplit("/", 1)[-1]


def describe(datasource) -> dict:
    """What a figure records about a project image, ready to store as a source.

    Raises KeyError if there is no such project.
    """
    handle = api.dataset(datasource)
    width, height = handle.image.size
    channels = [
        {"key": channel_key(channel),
         "fullname_at_capture": channel.get("fullname") or channel.get("name") or ""}
        for channel in handle.image.channels
        if channel_key(channel)
    ]
    return {
        "kind": "plexora_project",
        "datasource": datasource,
        "display_name": datasource,
        "image": {"width": int(width or 0), "height": int(height or 0)},
        "channels": channels,
        "fingerprint": fingerprint(datasource),
        "status": "ok",
    }


def fingerprint(datasource) -> dict:
    """The cheap identity check: dimensions, channel keys, mask present.

    Deliberately not a hash of the pixels. Hashing a 40 GB pyramid to answer
    "is this still the same slide" is a check nobody runs twice, and these four
    facts catch every re-import that would put a panel in the wrong place.
    """
    handle = api.dataset(datasource)
    width, height = handle.image.size
    return {
        "image_width": int(width or 0),
        "image_height": int(height or 0),
        "channel_keys": [channel_key(c) for c in handle.image.channels if channel_key(c)],
        "has_segmentation": bool(handle.segmentation.available),
    }


def status_of(source) -> dict:
    """Compare a stored source against the project it names.

    Returns `{status, reasons, fingerprint}`. `reasons` is a list of short codes
    -- never prose -- because the client branches on them and wording is not an
    API.
    """
    if source.get("kind") != "plexora_project":
        # An imported asset lives inside the figure's own directory; nothing
        # outside can change it.
        return {"status": "ok", "reasons": [], "fingerprint": source.get("fingerprint") or {}}

    datasource = source.get("datasource") or ""
    try:
        current = fingerprint(datasource)
    except KeyError:
        return {"status": "missing", "reasons": ["no_such_project"], "fingerprint": {}}

    stored = source.get("fingerprint") or {}
    reasons = []
    if stored.get("image_width") and (stored.get("image_width"), stored.get("image_height")) != (
            current["image_width"], current["image_height"]):
        reasons.append("dimensions_changed")
    stored_keys = set(stored.get("channel_keys") or [])
    if stored_keys and not stored_keys.issubset(set(current["channel_keys"])):
        reasons.append("channels_removed")
    if stored.get("has_segmentation") and not current["has_segmentation"]:
        reasons.append("segmentation_removed")

    return {
        "status": "changed" if reasons else "ok",
        "reasons": reasons,
        "fingerprint": current,
    }
