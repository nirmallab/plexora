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

    Core hides a tool whose needs the project cannot meet, rather than letting
    the user open a panel that cannot work. Image data is not listed because
    every plugin gets it -- that is the floor of the contract.
    """

    #: Needs a feature table (CSV/AnnData/SpatialData).
    table: bool = False
    #: Needs a segmentation mask.
    segmentation: bool = False
    #: Image kinds this plugin cannot handle. 'rgb' is the flat quick-view
    #: path: no channels, no feature data, so marker tools are meaningless.
    excluded_image_kinds: tuple[str, ...] = ("rgb",)

    def satisfied_by(self, entry: Mapping[str, Any]) -> bool:
        entry = entry or {}
        if entry.get("image_kind") in self.excluded_image_kinds:
            return False
        if self.table and not _has_feature_table(entry):
            return False
        if self.segmentation and not entry.get("segmentation"):
            return False
        return True


def _has_feature_table(entry: Mapping[str, Any]) -> bool:
    """True when the project has a real feature table.

    `has_feature_data` is authoritative when present. The legacy fallback
    covers projects registered before that flag existed, which recorded a
    featureData entry and nothing else.
    """
    if not entry.get("featureData"):
        return False
    return bool(entry.get("has_feature_data", True))


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

    #: Client assets, as URLs relative to the app root. Cache-busted with
    #: `version` rather than a hand-typed string that has to be kept in sync
    #: in two places, which is how the two copies previously drifted.
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

    def asset_urls(self, kind: str) -> list[str]:
        """Cache-busted URLs for this plugin's scripts or styles."""
        assets = self.scripts if kind == "scripts" else self.styles
        return [f"{url}?v={self.version}" for url in assets]

    def describe(self) -> dict:
        """The shape core hands the client for the Tools menu."""
        return {"name": self.name, "label": self.label}
