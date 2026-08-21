"""Where ROI annotations live, and who is allowed to overwrite whom.

Everything goes through `plexora.api.store`, whatever the project was imported
from. That is the decision the rest of the plugin rests on: an image-only
project, a CSV one and a SpatialData one all read and write the same document,
so drawing behaves identically and the source format only matters when the user
asks to export. Storing annotations in whichever object the project happened to
come with would mean every edit path had to understand every format.

The store writes an opaque blob per plugin per datasource -- no partial updates,
no revisions of its own. So the revision lives INSIDE the document and is
checked here, and this class exists so that stays true: routes never touch the
store, and a later move to row-level persistence changes this file only.

Why a revision at all, for what is nominally a single-user desktop app: two
browser tabs on the same project is not exotic, and neither is a tab left open
from yesterday. Both hold a full copy of the annotations and both autosave.
Last-writer-wins there means the stale tab's next save silently reinstates its
whole world -- deleting every region drawn in the other one, with no error and
nothing to notice. The check costs an integer comparison.
"""

from __future__ import annotations

import json
import threading

from plexora import api
from plexora.plugins.roi.server import operations, schema

PLUGIN_NAME = "roi"


class ConflictError(Exception):
    """Somebody else wrote since the caller last read.

    Carries the current revision so the client can say what happened rather
    than just failing.
    """

    def __init__(self, current_revision):
        super().__init__("ROI annotations changed in another session")
        self.current_revision = current_revision


class ImageMismatch(Exception):
    """The stored annotations were drawn on a different image than the one this
    project now holds. See `ROIRepository.status`."""

    def __init__(self, stored, current):
        super().__init__("ROI annotations belong to a different image")
        self.stored = stored
        self.current = current


#: One lock per datasource, held across the read-modify-write in `apply`.
#: Plexora serves on waitress, which is multi-threaded, so two tabs saving at
#: the same moment really do land in two threads -- and without this they can
#: both read revision 41, both write revision 42, and one of them vanishes
#: without ever being told it conflicted. The revision check is only as good as
#: the atomicity of the section it guards.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(datasource):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(datasource)
        if lock is None:
            lock = _LOCKS[datasource] = threading.Lock()
        return lock


class ROIRepository:
    """One project's annotations."""

    def __init__(self, datasource):
        self.datasource = datasource
        self._store = api.store(datasource, PLUGIN_NAME)

    # -- reading --------------------------------------------------------

    def image_size(self):
        """(width, height) of the image these annotations are drawn on.

        From the project record via the public dataset handle -- the same
        numbers the viewer lays its tiles out with.
        """
        try:
            return api.dataset(self.datasource).image.size
        except KeyError:
            return (None, None)

    def load(self):
        """The stored document, or the default one for a project with no ROIs.

        The default is not persisted: opening the panel on a project nobody has
        annotated should leave the project exactly as it found it.
        """
        width, height = self.image_size()
        blob = self._store.get_state()
        if not blob:
            return schema.default_state(width, height)
        try:
            raw = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            # Deliberately loud. The alternative -- quietly handing back an
            # empty document -- presents "your annotations are gone" as "this
            # project has no annotations", and the next autosave makes it true.
            raise ValueError(f"stored ROI annotations could not be read: {exc}") from exc
        return schema.normalize_state(raw, width, height)

    def status(self, state=None):
        """What the client needs before it draws anything.

        `dimension_mismatch` is the guard against a datasource whose image has
        been swapped underneath it. Regions from the old slide will render
        perfectly happily on the new one, in the wrong places, and look right --
        which is why this is reported rather than shrugged off, and why the
        client draws nothing until it is resolved.
        """
        state = self.load() if state is None else state
        width, height = self.image_size()
        stored = (state.get("images", {}).get(schema.DEFAULT_IMAGE, {})
                  .get("coordinate_space") or {})
        stored_size = (stored.get("width"), stored.get("height"))
        has_features = any(entry.get("features")
                           for entry in state.get("images", {}).values())
        mismatch = bool(
            has_features
            and all(stored_size)
            and all((width, height))
            and stored_size != (width, height)
        )
        return {
            "image_size": [width, height],
            "stored_image_size": list(stored_size),
            "dimension_mismatch": mismatch,
        }

    def destination(self, state=None):
        """Where this project last exported to, or "" if it never has.

        Not folded into `status()`: that runs on every apply, and this is only
        ever wanted by the one route that also has to list what names are
        already taken in the user's file.
        """
        state = self.load() if state is None else state
        return state.get("settings", {}).get("destination", "")

    # -- writing --------------------------------------------------------

    def apply(self, base_revision, ops):
        """Apply operations if the caller was up to date, and bump the revision.

        Returns the new revision. Raises ConflictError when somebody else wrote
        first, ValueError when an operation is invalid (nothing is stored in
        either case).
        """
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            raise ValueError("base_revision must be an integer")

        with _lock_for(self.datasource):
            state = self.load()
            if state["revision"] != base_revision:
                raise ConflictError(state["revision"])
            self._require_matching_image(state)

            updated = operations.apply_operations(state, ops)
            updated["revision"] = state["revision"] + 1
            self._write(updated)
            return updated["revision"]

    def replace(self, base_revision, state):
        """Store a whole document, for the paths that build one rather than edit
        one (import). Same revision check, same lock."""
        with _lock_for(self.datasource):
            current = self.load()
            if current["revision"] != base_revision:
                raise ConflictError(current["revision"])
            state = dict(state)
            state["revision"] = current["revision"] + 1
            self._write(state)
            return state["revision"]

    def remember_destination(self, name):
        """Record where this project's annotations were last exported to.

        Deliberately does NOT bump the revision. The revision answers "have the
        annotations changed under me", and every open tab acts on the answer --
        bumping it here would greet a colleague's second tab with a conflict
        banner because somebody chose a filename. Nothing about the shapes has
        changed, so no client is stale.

        Still inside the lock: it is a read-modify-write of the same blob an
        `apply` writes, and the two interleaving would lose one of them.

        Only reachable after a successful native export, which needs regions,
        which needs a stored document -- so this never conjures a blob for a
        project nobody has annotated.
        """
        with _lock_for(self.datasource):
            state = self.load()
            state.setdefault("settings", schema.default_settings())
            state["settings"]["destination"] = schema.clean_text(name)
            self._write(state)
            return state["settings"]["destination"]

    def _write(self, state):
        self._store.put_state(json.dumps(state, separators=(",", ":")).encode("utf-8"))

    def _require_matching_image(self, state):
        report = self.status(state)
        if report["dimension_mismatch"]:
            raise ImageMismatch(report["stored_image_size"], report["image_size"])
