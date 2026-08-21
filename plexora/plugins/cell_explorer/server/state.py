"""What Cell Explorer remembers between sessions, and who may overwrite whom.

Only display preferences: which column was showing, what colour each category
got, which were hidden, the palette and range for a numeric one, the opacity.
Never values. The table is the source of truth, and caching a copy of it here
would mean a project whose data changed underneath still painted the old
picture -- silently, and looking entirely plausible.

The store writes one opaque blob per plugin per datasource: no partial updates,
no revisions of its own. So the revision lives INSIDE the document and is
checked here, which is also why routes never touch the store directly.

Why a revision at all for a single-user desktop app: two tabs on the same
project is not exotic, and neither is a tab left open from yesterday. Both hold
a full copy of these preferences and both autosave. Last-writer-wins means the
stale tab quietly reinstates its whole world -- every recoloured category
reverted, with no error and nothing to notice. The check costs an integer
comparison.

Unknown keys are preserved rather than dropped (see `normalize`). A newer
Plexora writing a field this one does not model must not have it stripped by the
first save from an older tab.
"""

from __future__ import annotations

import json
import threading

from plexora import api

PLUGIN_NAME = "cell_explorer"

#: Bumped only for a change this version cannot read. A field that is merely
#: new does not need it -- `normalize` fills defaults and keeps what it does not
#: recognise, so additive changes are already safe both ways.
SCHEMA_VERSION = 1

#: Matches ImageViewer.DEFAULT_CELL_LAYER_OPACITY. Filled masks hide the tissue
#: at full strength and outlines read poorly when faint, and one control serves
#: both.
DEFAULT_OPACITY = 0.7

DEFAULT_PALETTE = "viridis"
PALETTES = ("viridis", "magma", "cividis", "coolwarm", "custom")
MODES = ("none", "centroids", "outlines", "filled")


class ConflictError(Exception):
    """Somebody else wrote since the caller last read.

    Carries the current revision so the client can say what happened rather
    than just failing.
    """

    def __init__(self, current_revision):
        super().__init__("Cell Explorer settings changed in another session")
        self.current_revision = current_revision


class UnreadableState(Exception):
    """The stored document is from a version this one cannot read.

    Deliberately not "start fresh". Overwriting a newer tab's settings because
    this build does not understand them destroys work to avoid showing a
    banner.
    """

    def __init__(self, schema_version):
        super().__init__(
            f"stored settings use schema version {schema_version}, "
            f"which this version of Plexora cannot read"
        )
        self.schema_version = schema_version


#: One lock per datasource, held across the read-modify-write in `save`.
#: Plexora serves on waitress, which is multi-threaded, so two tabs saving at
#: the same moment really do land in two threads -- and without this they can
#: both read revision 41, both write revision 42, and one of them vanishes
#: without ever being told it conflicted.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(datasource):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(datasource)
        if lock is None:
            lock = _LOCKS[datasource] = threading.Lock()
        return lock


def default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        # Which column is showing. None means "decide for me" -- the panel then
        # picks the best candidate rather than opening on nothing.
        "selected": None,
        "display": {"mode": None, "opacity": DEFAULT_OPACITY},
        # column -> "categorical" | "continuous", where the user disagreed with
        # the inference. Per column, because the answer is about that column.
        "overrides": {},
        # column -> {"colors": {category: "#rrggbb"}, "hidden": [category, ...]}
        "categorical": {},
        # column -> {"palette", "custom": {"low", "high"}, "range": {...},
        #            "hidden": bool}
        "continuous": {},
    }


class CellExplorerRepository:
    """One project's display preferences."""

    def __init__(self, datasource):
        self.datasource = datasource
        self._store = api.store(datasource, PLUGIN_NAME)

    def load(self) -> dict:
        """The stored document, or the default for a project nobody has opened.

        The default is not persisted: opening the panel on a fresh project
        should leave the project exactly as it found it.
        """
        blob = self._store.get_state()
        if not blob:
            return default_state()
        try:
            raw = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            # Deliberately loud. Quietly handing back an empty document presents
            # "your settings are gone" as "this project has no settings", and
            # the next autosave makes it true.
            raise ValueError(f"stored Cell Explorer settings could not be read: {exc}") from exc
        return normalize(raw)

    def save(self, base_revision, settings) -> dict:
        """Store the document if the caller was up to date, and bump the revision.

        Returns the stored document. Raises ConflictError when somebody else
        wrote first (nothing is stored), ValueError when the payload is not a
        document.
        """
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            raise ValueError("base_revision must be an integer")
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")

        with _lock_for(self.datasource):
            current = self.load()
            if current["revision"] != base_revision:
                raise ConflictError(current["revision"])
            updated = normalize(settings)
            updated["revision"] = current["revision"] + 1
            self._store.put_state(
                json.dumps(updated, separators=(",", ":")).encode("utf-8"))
            return updated


def normalize(raw) -> dict:
    """A stored document, cleaned into the shape the panel expects.

    Everything is bounds-checked rather than trusted: this blob has been on disk
    across upgrades, and a palette name or an opacity that no longer means
    anything should fall back to a default rather than reach the renderer.

    Per-column entries are NOT filtered against the current table. A column that
    is temporarily absent -- a data file swapped, a different image loaded --
    comes back, and dropping its colours the one time it was missing loses work
    a user did deliberately. The panel ignores entries it cannot match.
    """
    if not isinstance(raw, dict):
        raise ValueError("stored settings are not an object")

    version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise UnreadableState(version)

    state = dict(raw)
    state["schema_version"] = SCHEMA_VERSION
    state["revision"] = _int(raw.get("revision"), 0)
    state["selected"] = _text_or_none(raw.get("selected"))
    state["display"] = _display(raw.get("display"))
    state["overrides"] = {
        str(column): kind
        for column, kind in _mapping(raw.get("overrides")).items()
        if kind in ("categorical", "continuous")
    }
    state["categorical"] = {
        str(column): _categorical_entry(entry)
        for column, entry in _mapping(raw.get("categorical")).items()
    }
    state["continuous"] = {
        str(column): _continuous_entry(entry)
        for column, entry in _mapping(raw.get("continuous")).items()
    }
    return state


def _display(raw) -> dict:
    raw = _mapping(raw)
    mode = raw.get("mode")
    return {
        "mode": mode if mode in MODES else None,
        "opacity": _clamped(raw.get("opacity"), DEFAULT_OPACITY),
    }


def _categorical_entry(raw) -> dict:
    raw = _mapping(raw)
    colors = {
        str(category): color
        for category, color in _mapping(raw.get("colors")).items()
        if _is_hex(color)
    }
    hidden = sorted({str(category) for category in raw.get("hidden") or ()
                     if isinstance(category, (str, int, float, bool))})
    return {"colors": colors, "hidden": hidden}


def _continuous_entry(raw) -> dict:
    raw = _mapping(raw)
    palette = raw.get("palette")
    custom = _mapping(raw.get("custom"))
    range_raw = _mapping(raw.get("range"))
    low = _number_or_none(range_raw.get("min"))
    high = _number_or_none(range_raw.get("max"))
    manual = (range_raw.get("mode") == "manual"
              and low is not None and high is not None and low < high)
    return {
        "palette": palette if palette in PALETTES else DEFAULT_PALETTE,
        "custom": {
            "low": custom.get("low") if _is_hex(custom.get("low")) else None,
            "high": custom.get("high") if _is_hex(custom.get("high")) else None,
        },
        # Whether the overlay is drawn at all. A boolean here and a list of
        # labels on a categorical entry: a ramp has no rows to hide one of, so
        # the whole column is the unit. Listed explicitly because these entries
        # are rebuilt key by key rather than passed through -- an unmodelled
        # field on one of them is dropped by the first save.
        "hidden": bool(raw.get("hidden")),
        # A manual range that no longer makes sense (min >= max after the source
        # changed) reverts to auto rather than being kept and refusing to draw.
        "range": ({"mode": "manual", "min": low, "max": high} if manual
                  else {"mode": "auto"}),
    }


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value, fallback) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _text_or_none(value):
    return value if isinstance(value, str) and value else None


def _number_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _clamped(value, fallback) -> float:
    number = _number_or_none(value)
    if number is None:
        return fallback
    return max(0.0, min(1.0, number))


def _is_hex(value) -> bool:
    """#rrggbb only. Anything else reaches a canvas fillStyle, where an
    unrecognised string is silently ignored and the cell keeps whatever colour
    was set last -- a wrong picture rather than a missing one."""
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value[1:])
