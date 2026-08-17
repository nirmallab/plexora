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
from typing import Any, Mapping

#: Plugin names become URL segments and SQL identifiers, so they are
#: restricted rather than escaped. Matches plexora.api.store's rule.
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Requires:
    """What a datasource must offer before this plugin's tool is usable.

    Two different questions, deliberately kept apart:

    `applies_to` -- could this plugin EVER work here? A flat RGB image has no
    channels, and no amount of uploading changes that, so the tool is hidden.

    `satisfied_by` -- can it work RIGHT NOW? A project with the wrong image
    kind fails both; a project merely missing its feature table fails only this
    one, and that is a recoverable state: the tool stays listed and opening it
    routes the user to attach what is missing (see tool_routes.open_tool).

    Collapsing the two hides a tool from a project that could have used it
    after one upload, which also hides the upload path itself.

    Image data is not listed because every plugin gets it -- that is the floor
    of the contract.
    """

    #: Needs a feature table (CSV/AnnData/SpatialData). Acquirable.
    table: bool = False
    #: Needs a segmentation mask. Acquirable.
    segmentation: bool = False
    #: Image kinds this plugin cannot handle. 'rgb' is the flat quick-view
    #: path: no channels, so marker tools are meaningless there. Permanent.
    excluded_image_kinds: tuple[str, ...] = ("rgb",)

    def applies_to(self, entry: Mapping[str, Any]) -> bool:
        """Whether this plugin is compatible with the datasource at all."""
        entry = entry or {}
        return entry.get("image_kind") not in self.excluded_image_kinds

    def missing_from(self, entry: Mapping[str, Any]) -> list[str]:
        """Which acquirable inputs this datasource still lacks."""
        entry = entry or {}
        missing = []
        if self.table and not _has_feature_table(entry):
            missing.append("table")
        if self.segmentation and not entry.get("segmentation"):
            missing.append("segmentation")
        return missing

    def satisfied_by(self, entry: Mapping[str, Any]) -> bool:
        """Whether the plugin can be opened as things stand."""
        return self.applies_to(entry) and not self.missing_from(entry)


def _has_feature_table(entry: Mapping[str, Any]) -> bool:
    """True when the project has a REAL feature table, as opposed to none at
    all or the stub a quick-view registration used to write.

    `has_feature_data` is authoritative when present, and every registration
    path writes it now. Projects registered before that flag existed have no
    such key, and for those the only way to tell a real table from a quick-view
    stub is the stub's fixed filename.
    """
    feature_data = entry.get("featureData")
    if not feature_data:
        return False
    if "has_feature_data" in entry:
        return bool(entry["has_feature_data"])
    src = (feature_data[0] or {}).get("src", "")
    return not src.endswith("quick_view_points.csv")


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

    #: Whether this plugin colours cells in the viewer. At most one plugin may
    #: do so at a time -- the shader holds a single range table -- so the
    #: client treats this as a claim, not a guarantee.
    owns_cell_layer: bool = False

    def __post_init__(self):
        if not _SAFE_NAME.match(self.name or ""):
            raise ValueError(
                f"invalid plugin name {self.name!r}: expected lowercase letters, "
                "digits and underscores, starting with a letter"
            )

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
        tags still use `../client/...`, which silently resolves wrong there --
        plugin assets do not inherit that bug.
        """
        assets = self.scripts if kind == "scripts" else self.styles
        return [f"{base_url}{self.url_prefix}/static/{name}?v={self.version}" for name in assets]

    def describe(self) -> dict:
        """The shape core hands the client for the Tools menu."""
        return {"name": self.name, "label": self.label}
