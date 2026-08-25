"""What a plugin declares about itself.

A plugin package exposes a module-level `PLUGIN = Plugin(...)`. Plexora finds
it either through the `plexora.plugins` entry point group (how a third-party
distribution ships) or by scanning the bundled plugins directory (how
first-party ones ship). Both paths produce this same descriptor, so a bundled
plugin gets no privileges an installed one lacks.

The descriptor is data, not behaviour. Everything the host needs in order to
render a tool -- its label, its panels, its assets, what data it needs -- is
declared here, so core never has to name a specific plugin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from plexora.server.models.project import ROLE_LABELS, ROLE_NAMES, Project

#: Plugin names become URL segments and SQL identifiers, so they are
#: restricted rather than escaped. Matches plexora.api.store's rule.
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Requirement:
    """One thing a plugin needs that the project does not have yet.

    Data rather than a message, because core renders it: the requirements modal
    turns a list of these into a form without knowing which plugin asked or
    what it wants them for. `kind` picks the input widget; `key` is what the
    answer is posted back under.
    """

    key: str
    #: 'data' | 'segmentation' | 'classification' | 'features' | 'role'
    #: | 'coordinates'
    kind: str
    label: str
    #: Offered but not blocking -- the tool opens whether or not it is given.
    optional: bool = False

    @property
    def role(self) -> str | None:
        """The column role this asks for, for kind == 'role'."""
        return self.key.split(":", 1)[1] if self.key.startswith("role:") else None

    def describe(self) -> dict:
        # `role` is sent, not left for the client to parse back out of `key`.
        # Omitting it is not cosmetic: the requirements modal keys its answers
        # by `requirement.role`, so an absent field made every role select post
        # under the literal key "undefined" -- each field clobbering the last,
        # and the whole lot dropped server-side by with_role_answers' `role in
        # ROLE_NAMES` filter, while `confirm` (which reads `key`, and so worked)
        # marked the questions answered so they were never asked again. The
        # visible symptom was a project silently keeping the adapter's
        # positional row number as its cell id, which puts every segmentation
        # outline on the wrong cell.
        return {"key": self.key, "kind": self.kind, "label": self.label,
                "optional": self.optional, "role": self.role}


#: The acquirable inputs a plugin may name, and how each is described to the
#: user. Roles are generated from ROLE_NAMES so the vocabulary cannot drift
#: from what the project record can actually store.
_INPUT_LABELS = {
    "table": ("data", "Single-cell data"),
    "segmentation": ("segmentation", "Segmentation mask"),
    "markers": ("classification", "Marker and metadata columns"),
    "features": ("features", "Expression values"),
    # Stands in for the x/y role pair wherever the table is built from a read
    # spec -- see `_coordinate_keys`. Never named by a plugin directly: a
    # plugin declares the roles it reads, and core decides which question
    # actually answers them for this project's format.
    "coordinates": ("coordinates", "Cell coordinates"),
}

#: The roles the coordinate question answers, and the key it answers them with.
_COORDINATE_ROLES = ("x", "y")
COORDINATES_KEY = "coordinates"


def _coordinate_keys(project, roles) -> list[str]:
    """How this project's x/y roles are asked for.

    For a CSV they are two ordinary column roles -- the table is the file, and
    a role just names a column in it. For AnnData and SpatialData the table
    does not exist until the adapter builds it, and the coordinates may come
    from a single `obsm` array holding both axes, which no pair of
    single-column selects can express. There the two roles collapse into one
    `coordinates` question.
    """
    wanted = [role for role in _COORDINATE_ROLES if role in roles]
    if not wanted:
        return []
    if project.columns_are_structural:
        return [COORDINATES_KEY]
    return [f"role:{role}" for role in wanted]


def _requirement(key: str, optional: bool = False) -> Requirement:
    if key.startswith("role:"):
        role = key.split(":", 1)[1]
        return Requirement(key=key, kind="role",
                           label=ROLE_LABELS.get(role, role), optional=optional)
    kind, label = _INPUT_LABELS[key]
    return Requirement(key=key, kind=kind, label=label, optional=optional)


@dataclass(frozen=True)
class Requires:
    """What a datasource must offer before this plugin's tool is usable.

    Two different questions, deliberately kept apart:

    `applies_to` -- could this plugin EVER work here? A flat RGB image has no
    channels, and no amount of uploading changes that, so the tool is hidden.

    `satisfied_by` -- can it work RIGHT NOW? A project with the wrong image
    kind fails both; a project merely missing its feature table fails only this
    one, and that is a recoverable state: the tool stays listed and opening it
    asks for what is missing (see tool_routes.tool_panel).

    Collapsing the two hides a tool from a project that could have used it
    after one upload, which also hides the upload path itself.

    Image data is not listed because every plugin gets it -- that is the floor
    of the contract.
    """

    #: Needs a feature table (CSV/AnnData/SpatialData). Acquirable.
    table: bool = False
    #: Needs a segmentation mask. Acquirable.
    segmentation: bool = False
    #: Needs the marker/metadata split to have been established, so it can
    #: offer the user a marker list that is not a guess.
    markers: bool = False
    #: Reads the marker intensities themselves, and so depends on *which*
    #: numbers those are. A file can hold raw counts in `X` and a log-transformed
    #: copy in a layer, and nothing about the values says which is which: a
    #: threshold set on one is meaningless on the other. Declaring this is what
    #: puts that choice, and the log switch beside it, in front of the user once
    #: -- never asked for a CSV, which has only one table of numbers.
    features: bool = False
    #: Column roles this plugin resolves through `dataset.schema` -- any of
    #: ROLE_NAMES. Declaring them is what lets core ask for the ones a project
    #: never recorded, instead of the plugin growing its own "type the column
    #: name" box.
    roles: tuple[str, ...] = ()
    #: Inputs to offer but never block on, named the same way (`"segmentation"`,
    #: `"role:image_id"`). The tool opens without them; it just does less.
    optional: tuple[str, ...] = ()
    #: Image kinds this plugin cannot handle. 'rgb' is the flat quick-view
    #: path: no channels, so marker tools are meaningless there. Permanent.
    excluded_image_kinds: tuple[str, ...] = ("rgb",)

    def __post_init__(self):
        unknown = [r for r in self.roles if r not in ROLE_NAMES]
        if unknown:
            raise ValueError(
                f"unknown column role(s) {unknown!r}: expected any of {list(ROLE_NAMES)}"
            )
        for key in self.optional:
            if key not in _INPUT_LABELS and key.split(":", 1)[0] != "role":
                raise ValueError(f"unknown optional requirement {key!r}")
            if key.startswith("role:") and key.split(":", 1)[1] not in ROLE_NAMES:
                raise ValueError(f"unknown column role in optional requirement {key!r}")

    def applies_to(self, project) -> bool:
        """Whether this plugin is compatible with the datasource at all."""
        project = _as_project(project)
        return project.image.kind not in self.excluded_image_kinds

    def missing_from(self, project) -> list[Requirement]:
        """Which acquirable inputs this datasource still lacks, in the order
        they should be asked for: the file first, then what it contains.

        Roles and markers are reported only once there is a table -- asking
        which column holds the cell id before any columns exist is a question
        with no answers, and the table requirement already covers it.
        """
        project = _as_project(project)
        missing = []
        if self.table and not project.has_table:
            missing.append(_requirement("table"))
        if self.segmentation and not project.segmentation.requested:
            missing.append(_requirement("segmentation"))
        if project.has_table:
            if self.markers and not project.columns.classified:
                missing.append(_requirement("markers"))
            for key in self._column_keys(project):
                if not _answered(project, key):
                    missing.append(_requirement(key))
        return missing

    def _column_keys(self, project) -> list[str]:
        """Every question about this project's columns that this plugin's roles
        imply, in ask order -- with x/y already translated into whichever form
        this project's format can actually answer (see `_coordinate_keys`)."""
        keys = [f"role:{role}" for role in self.roles
                if role not in _COORDINATE_ROLES]
        return keys + _coordinate_keys(project, self.roles)

    def declared_keys(self, project) -> list[str]:
        """Every input this plugin names, required and optional, in ask order.

        Used to work out what has never been put in front of the user -- which
        is not the same question as what is absent, and needs the whole list
        rather than just the unmet part of it.
        """
        project = _as_project(project)
        keys = []
        if self.table:
            keys.append("table")
        if self.segmentation:
            keys.append("segmentation")
        # Ahead of the column questions because it is the consequential one:
        # which numbers are being read decides what every answer below it means.
        if self.features:
            keys.append("features")
        if self.markers:
            keys.append("markers")
        keys.extend(self._column_keys(project))
        keys.extend(key for key in self.optional if key not in keys)
        return keys

    def unconfirmed_from(self, project) -> list[Requirement]:
        """Inputs this project has an answer for that the user never gave.

        The column predictor fills in most of a conventionally-named table, and
        a guess that happens to be right is still a guess -- so the first time a
        tool opens, what it depends on is shown once for confirmation, prefilled.
        After that the answer is recorded and never asked for again.

        Absent inputs are deliberately not here: those are `missing_from`'s and
        `optional_missing_from`'s, and listing an input twice would render the
        same field twice in one form.
        """
        project = _as_project(project)
        return [
            _requirement(key, optional=key in self.optional)
            for key in project.unconfirmed(self.declared_keys(project))
            if not _never_confirmed(project, key) and _answered(project, key)
        ]

    def optional_missing_from(self, project) -> list[Requirement]:
        """The non-blocking inputs this datasource lacks, so the modal can
        offer them alongside the required ones.

        Offered once. A user who was shown an optional field and left it blank
        has answered it -- there may be no such column in their data -- and
        re-offering it every time a tool opens is worse than not offering it.
        A plugin that genuinely cannot proceed without one asks for it directly
        through `requested_from`.
        """
        project = _as_project(project)
        missing = []
        for key in project.unconfirmed(self.optional):
            if key == "table" and project.has_table:
                continue
            if key == "segmentation" and project.segmentation.requested:
                continue
            if key == "markers" and project.columns.classified:
                continue
            if key.startswith("role:") or key == COORDINATES_KEY:
                # Through `_answered` rather than reading the role directly, so
                # the states that are answers without being a named column --
                # "one image", a recorded coordinate source -- count here the
                # same way they do for a blocking requirement.
                if not project.has_table or _answered(project, key):
                    continue
            missing.append(_requirement(key, optional=True))
        return missing

    def requested_from(self, project, keys: Iterable[str]) -> list[Requirement]:
        """Descriptors for named inputs this project still cannot answer.

        For a plugin demanding something mid-session, after its panel is
        already open -- gating needs an image-id column only when the user
        chooses to write gates back to the source file, which may be an hour
        into a session or never.

        Ignores `confirmed` on purpose: the user may have been offered this as
        an optional field and skipped it, which is a fine answer right up until
        they ask for the one action that cannot proceed without it. Restricted
        to keys the plugin declared, so this cannot become a back door for
        asking about something it never said it used.
        """
        project = _as_project(project)
        declared = set(self.declared_keys(project))
        return [_requirement(key, optional=key in self.optional)
                for key in keys
                if key in declared and not _answered(project, key)]

    def satisfied_by(self, project) -> bool:
        """Whether the plugin can be opened as things stand."""
        project = _as_project(project)
        return self.applies_to(project) and not self.missing_from(project)


#: Inputs that are a path the user typed or browsed to, never something the
#: app worked out. There is nothing to confirm about them -- showing a file
#: path back and asking "is this the file you chose?" is noise -- so they are
#: only ever asked for when absent.
_GIVEN_KEYS = frozenset({"table", "segmentation"})


def _never_confirmed(project: Project, key: str) -> bool:
    """Whether this input is one the user is never shown for confirmation.

    Either because they supplied it themselves (`_GIVEN_KEYS`), or because the
    answer is not a guess in the first place: an AnnData or SpatialData file
    states its own marker/metadata split, and putting `var` and `obs` in a
    drag-and-drop box asks the user to confirm what the file already says.

    `features` is asked for every format, which it was not: a CSV was skipped
    on the grounds that it has one table of numbers and no layer to prefer.
    True, and only half the question -- the other half is whether those numbers
    are raw counts, and that is exactly as open for a CSV as for an .h5ad with
    no layers. Skipping it left the log1p switch with nowhere to appear on the
    one format that most often arrives untransformed. The matrix picker still
    stands down on its own (`feature_options` is empty for a CSV, and the modal
    drops a select with nothing to choose between).
    """
    if key in _GIVEN_KEYS:
        return True
    if key == "markers":
        return project.columns_are_structural
    return False


def _answered(project: Project, key: str) -> bool:
    """Whether the project currently holds a value for this input.

    Says nothing about who supplied it -- the predictor's guess counts as an
    answer here, which is exactly why `unconfirmed_from` needs this as well as
    the `confirmed` list to tell a guess from a decision.
    """
    if key == "table":
        return project.has_table
    if key == "segmentation":
        return project.segmentation.requested
    if key == "markers":
        return project.columns.classified
    if key == "features":
        # Never absent: a table is always being read from some matrix, so this
        # is only ever a value nobody has looked at rather than a gap. It
        # reaches the user through `unconfirmed_from`, never `missing_from`.
        return project.has_table
    if key == COORDINATES_KEY:
        # The recorded read spec, not the roles: `roles.x`/`roles.y` are the
        # literal "X"/"Y" the adapter emits and are set the moment a table
        # exists, so they say nothing about whether anyone chose a source.
        return bool(project.has_table and project.dataset
                    and project.dataset.coordinates)
    if key == "role:cell_id" and project.columns_are_structural:
        # The role is not the answer for these formats. It names a column of
        # the table the adapter EMITS, and the importer sets it to the
        # adapter's own positional "id" the moment a table loads -- so reading
        # it here would report every AnnData and SpatialData project as having
        # answered a question nobody was asked, which is exactly what left a
        # project drawing gates against row numbers while its mask carried the
        # label values from obs.
        #
        # The read spec is the answer: a named obs column, or the explicit
        # "number the rows" that names none. See DataSpec.row_number_ids.
        return bool(project.has_table and project.dataset
                    and (project.dataset.obs_id_field
                         or project.dataset.row_number_ids))
    if key == "role:image_id":
        # "This table covers one image" is an answer, and the only one some
        # files have -- so it counts here, while a bare absent role does not.
        # See DataSpec.single_image for why it is not stored as a blank role.
        return bool(project.has_table and project.dataset
                    and (project.roles.image_id or project.dataset.single_image))
    if key.startswith("role:"):
        return bool(project.has_table and project.roles.get(key.split(":", 1)[1]))
    return False


def _as_project(project) -> Project:
    """Accept a Project or a raw config entry.

    Callers inside core hold a Project. Tests and a few older call sites hold
    the entry dict, and rejecting those would make this contract annoying to
    exercise without buying any safety.
    """
    if isinstance(project, Project):
        return project
    return Project.from_entry("", project or {})


#: Core menus a plugin may add an entry to. Named here so a typo is a startup
#: error rather than an entry that renders nowhere and cannot be found.
#:
#: 'file'          the File dropdown, on every page.
#: 'open_project'  the tab strip on the Open Project page.
NAV_MENUS = ("file", "open_project")

#: Modifier tokens a shortcut spec may carry, in the order they are printed.
#:
#: `mod` is Cmd on a Mac and Ctrl everywhere else, and it stays unresolved here
#: on purpose: one descriptor is served to every client, and the server has no
#: idea what the reader is typing on. keyboardShortcuts.js resolves and prints
#: it, which is also the only place that knows whether to draw "⌘E" or "Ctrl+E".
SHORTCUT_MODIFIERS = ("mod", "shift", "alt")

#: Keys the browser keeps for itself even when the page calls preventDefault().
#: Declaring one of these buys a shortcut that opens a tab instead of the tool,
#: which is worse than having no shortcut at all -- so it is a startup error
#: here rather than a mystery for whoever presses it.
#:
#: `h` and `m` are the Mac half of the list (Hide and Minimise belong to the
#: window manager, not the page) and are rejected everywhere, because a shortcut
#: that works on one platform and silently does something else on another is
#: exactly the sort of thing a shared descriptor must not express.
UNRELEASED_KEYS = frozenset({"t", "n", "w", "q", "h", "m"})

#: The same question for `mod+shift+<key>`, which is a smaller list but not the
#: empty one it looks like: shift does not hand these back, it just moves them
#: to a different browser command. Reopen-closed-tab, new-incognito-window and
#: close-window are as unreachable from a page as their unshifted originals.
UNRELEASED_SHIFT_KEYS = frozenset({"t", "n", "w"})

#: Punctuation a shortcut may name, spelled as the character `KeyboardEvent.key`
#: reports. Chosen rather than open, so a spec cannot name something no keyboard
#: can produce, and so the printed form stays one glyph wide.
_PUNCTUATION_KEYS = frozenset({",", ".", "/", "\\", "[", "]", "'", ";", "`", "-", "="})


def normalize_shortcut(spec: str, owner: str = "") -> str:
    """Validate a shortcut spec and return it in canonical order.

    The spec is written `"mod+shift+e"`: modifiers from SHORTCUT_MODIFIERS in
    any order, then exactly one key. Returned with the modifiers sorted into
    SHORTCUT_MODIFIERS order and lowercased, so two descriptors that mean the
    same chord compare equal and the duplicate check downstream is a set lookup
    rather than a parser.

    At least one modifier is required. A bare letter would be a global shortcut
    competing with the ones plugins already bind against the canvas while their
    own panel is up -- ROI's v/p/f/r, Figure Builder's C and S -- and the tool
    that opens on a keystroke must never be decided by which panel happens to be
    listening.
    """
    where = f" for {owner}" if owner else ""
    parts = [part.strip().lower() for part in str(spec).split("+")]
    if len(parts) < 2 or not all(parts):
        raise ValueError(
            f"invalid shortcut {spec!r}{where}: expected at least one modifier "
            f"and a key, e.g. 'mod+e'"
        )
    *modifiers, key = parts
    unknown = [m for m in modifiers if m not in SHORTCUT_MODIFIERS]
    if unknown:
        raise ValueError(
            f"unknown shortcut modifier(s) {unknown!r}{where}: expected any of "
            f"{list(SHORTCUT_MODIFIERS)}"
        )
    if len(set(modifiers)) != len(modifiers):
        raise ValueError(f"repeated modifier in shortcut {spec!r}{where}")
    # `isascii` as well as `isalnum`: "é".isalnum() is True in Python, and a
    # chord only reachable on some keyboard layouts is not one a descriptor
    # shipped to every user should be able to name.
    if not (len(key) == 1
            and ((key.isalnum() and key.isascii()) or key in _PUNCTUATION_KEYS)):
        raise ValueError(
            f"invalid shortcut key {key!r}{where}: expected one letter, digit or "
            f"one of {sorted(_PUNCTUATION_KEYS)}"
        )
    # Both mod-based forms are checked, against different lists. Adding alt
    # takes the chord out of the browser's menu space entirely, so `mod+alt+t`
    # is fine where neither `mod+t` nor `mod+shift+t` is.
    held = set(modifiers)
    reserved = (UNRELEASED_KEYS if held == {"mod"}
                else UNRELEASED_SHIFT_KEYS if held == {"mod", "shift"}
                else frozenset())
    if key in reserved:
        raise ValueError(
            f"shortcut {spec!r}{where} is reserved by the browser and cannot be "
            f"intercepted by a page; pick another key, or add 'alt'"
        )
    ordered = [m for m in SHORTCUT_MODIFIERS if m in modifiers]
    return "+".join([*ordered, key])


@dataclass(frozen=True)
class NavItem:
    """One entry a plugin contributes to a core menu.

    Data, not markup, and deliberately so. A plugin whose home is a page of its
    own -- Figure Builder's library is not about any one datasource, so it
    cannot be a tool panel -- still needs a way in, and the alternatives were
    both worse: core naming the plugin in a template, or core JavaScript probing
    a plugin route to decide whether to unhide a hidden link (which
    tests/test_datalayer_requests.py rules out, because core must not know a
    plugin's addresses).

    Rendering stays core's: it emits a plain link with its own classes, so a
    plugin cannot style, script or restructure a core menu by contributing to
    it.
    """

    menu: str
    label: str
    #: Appended to this plugin's own url_prefix. A plugin can only ever link
    #: into its own namespace, which is what stops a nav entry becoming a way
    #: to point a core menu at an arbitrary URL.
    path: str = ""
    #: Sort key within the menu. Ties break on label, so the order is stable
    #: whatever sequence plugins were discovered in.
    order: int = 0
    #: A Font Awesome name without the `fa-` prefix, e.g. "images". Core renders
    #: it into the menu's icon gutter; a plugin still cannot supply markup, only
    #: name a glyph, so this stays as much data as `label` is.
    icon: str = ""
    #: Keystroke that follows this link, written as in `normalize_shortcut`.
    #: Optional -- an entry with no shortcut simply prints none, and the gutter
    #: stays aligned either way.
    shortcut: str = ""

    def __post_init__(self):
        if self.menu not in NAV_MENUS:
            raise ValueError(
                f"unknown nav menu {self.menu!r}: expected any of {list(NAV_MENUS)}"
            )
        # object.__setattr__ because the dataclass is frozen: normalising in
        # __post_init__ is what lets every consumer downstream -- the duplicate
        # check, the client's lookup table -- compare specs as plain strings.
        if self.shortcut:
            object.__setattr__(self, "shortcut",
                               normalize_shortcut(self.shortcut, self.label))


@dataclass(frozen=True)
class Plugin:
    """A plugin's self-description."""

    name: str
    label: str
    version: str = "0"

    #: Zero-argument callable returning this plugin's Flask Blueprint, mounted
    #: under /plugins/<name>/ so a plugin can never shadow a core route or
    #: another plugin's, whatever it names its endpoints.
    #:
    #: A factory rather than the Blueprint itself so that importing the
    #: descriptor stays cheap. The descriptor module is what discovery reads;
    #: if it had to build a Blueprint, importing it would drag in the plugin's
    #: whole dependency tree, and a core-only build would pay for addons it
    #: never installs.
    blueprint_factory: Any = None

    #: DOM slot id -> template path, rendered into the page for this tool.
    panels: Mapping[str, str] = field(default_factory=dict)

    #: Client assets, as filenames within the plugin's own static/ directory.
    #: Core turns them into full URLs, so a plugin never writes a path that
    #: assumes where the app is mounted. Cache-busted with `version` rather
    #: than a hand-typed string kept in sync in two places, which is how the
    #: two copies previously drifted apart.
    scripts: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()

    requires: Requires = field(default_factory=Requires)

    #: One sentence shown in the requirements modal when nothing is blocking:
    #: what these inputs would buy the user, in the plugin's own words.
    #:
    #: Core owns the wording for the blocking case -- "this tool needs a little
    #: more about this project" is true of every plugin. It is the non-blocking
    #: case that cannot be written generically: a form made entirely of optional
    #: fields has to say why anyone would fill it in, and only the plugin knows.
    #: Carried on the descriptor rather than looked up by name, so core still
    #: renders a form without knowing which plugin asked.
    intro: str = ""

    #: Entries this plugin adds to core menus. See NavItem: a plugin whose home
    #: is a page rather than a tool panel has no other way in.
    nav_items: tuple[NavItem, ...] = ()

    #: Whether this plugin colours cells in the viewer. At most one plugin may
    #: do so at a time -- the shader holds a single range table -- so the
    #: client treats this as a claim, not a guarantee.
    owns_cell_layer: bool = False

    #: A Font Awesome name without the `fa-` prefix, drawn in the Tools menu's
    #: icon gutter beside this tool's label. See NavItem.icon.
    icon: str = ""

    #: Keystroke that opens this tool, written as in `normalize_shortcut`.
    #:
    #: Opening is all it can be bound to. A plugin binding its own keys against
    #: the canvas is a separate business it already handles for itself (ROI's
    #: keyDown, Figure Builder's dock), and those fire only while the plugin is
    #: on screen; this one has to work from anywhere in the app, which is
    #: precisely why it is declared to core rather than taken by the plugin.
    shortcut: str = ""

    def __post_init__(self):
        if not _SAFE_NAME.match(self.name or ""):
            raise ValueError(
                f"invalid plugin name {self.name!r}: expected lowercase letters, "
                "digits and underscores, starting with a letter"
            )
        if self.shortcut:
            object.__setattr__(self, "shortcut",
                               normalize_shortcut(self.shortcut, self.name))

    @property
    def url_prefix(self) -> str:
        return f"/plugins/{self.name}"

    def load_blueprint(self):
        """Build the Blueprint. Called once, at install time, only for plugins
        this process actually activates."""
        return self.blueprint_factory() if self.blueprint_factory is not None else None

    def asset_urls(self, kind: str, base_url: str = "") -> list[str]:
        """Cache-busted, base-URL-safe URLs for this plugin's assets.

        `base_url` is prepended so plugin assets resolve under a mounted
        deployment (Jupyter's proxy sets PLEXORA_BASE_URL). Core's own template
        tags are built the same way, for the same reason: they used to be
        written relative to the page (`../client/...`), which resolves against
        whatever URL the page was served at and so broke on any page that was
        not exactly one segment deep -- see tests/test_page_assets.py.
        """
        assets = self.scripts if kind == "scripts" else self.styles
        return [f"{base_url}{self.url_prefix}/static/{name}?v={self.version}" for name in assets]

    def describe(self) -> dict:
        """The shape core hands the client for the Tools menu."""
        return {"name": self.name, "label": self.label,
                "icon": self.icon, "shortcut": self.shortcut}

    def describe_nav(self, base_url: str = "") -> list[dict]:
        """This plugin's menu entries, with hrefs already resolved.

        `id` is stamped from the plugin name and the path so the element can be
        found by a test or a stylesheet without core having to invent a naming
        scheme per plugin. Built here, next to `asset_urls`, for the same
        reason: a URL a plugin writes itself is a URL that assumes where the app
        is mounted.
        """
        return [
            {
                "menu": item.menu,
                "label": item.label,
                "href": f"{base_url}{self.url_prefix}{item.path}",
                "id": f"nav_{self.name}{item.path.replace('/', '_').rstrip('_')}",
                "order": item.order,
                "plugin": self.name,
                "icon": item.icon,
                "shortcut": item.shortcut,
            }
            for item in self.nav_items
        ]
