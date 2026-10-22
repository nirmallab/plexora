"""Finding, installing and querying plugins.

Two discovery paths, both producing `plexora.api.Plugin` descriptors:

- the `plexora.plugins` entry point group, which is how a third-party
  distribution advertises itself;
- a scan of the bundled plugins package, which is how the plugins shipped in
  this repository advertise themselves.

Both exist on purpose. Entry points are the real extension mechanism, but they
only appear once package metadata has been regenerated, so relying on them
alone would put `pip install -e .` between editing a bundled plugin and being
able to run it. The scan keeps working-from-source honest. Names are
deduplicated, so declaring a bundled plugin in pyproject.toml as well is
harmless.

**Discovery never imports a plugin it was not asked for.** Names come from
directory entries and entry-point metadata, both of which are readable without
executing the plugin's code; only the chosen ones are then imported. This is
what keeps a core-only build free of an addon's dependencies -- importing
gating's descriptor would drag in anndata and h5py, which is precisely the cost
the boundary exists to avoid. A plugin's package name must therefore match its
declared `PLUGIN.name`, since the name is known before the descriptor is.

Which plugins are active is controlled by PLEXORA_PLUGINS:

    unset      every discovered plugin (installing one is what enables it)
    ""         none -- a core-only build
    "a,b"      exactly those, in that order

Any number can be active at once. Only the cell-layer claim is exclusive, and
that is arbitrated client-side.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil

from plexora.api.plugin import Plugin
from plexora.server import core_tools

_log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "plexora.plugins"

ENV_VAR = "PLEXORA_PLUGINS"

#: Package scanned for bundled plugins. Each subpackage exposing a module-level
#: PLUGIN is one.
BUNDLED_PACKAGE = "plexora.plugins"

#: Where install() records what it mounted, for later lookup by the routes.
_CONFIG_KEY = "PLEXORA_INSTALLED_PLUGINS"


def _bundled_names() -> list[str]:
    """Subpackage names under BUNDLED_PACKAGE, without importing any of them.

    Scans `__path__` rather than a filesystem path built from `__file__`. The
    two are the same thing when running from source, but only `__path__` goes
    through the importer that actually loaded the package -- which is what makes
    this work in a PyInstaller build, where the submodules live in the archive
    and the directory `__file__` points at holds no .py files at all.
    """
    try:
        package = importlib.import_module(BUNDLED_PACKAGE)
    except ImportError:
        return []
    search_path = list(getattr(package, "__path__", []))
    return [info.name for info in pkgutil.iter_modules(search_path) if info.ispkg]


def _entry_points_by_name() -> dict:
    from importlib.metadata import entry_points

    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def available_names() -> list[str]:
    """Every plugin name visible to this process. Imports nothing."""
    names = list(_bundled_names())
    for name in _entry_points_by_name():
        if name not in names:
            names.append(name)
    return names


def load(name: str) -> Plugin | None:
    """Import one plugin and return its descriptor, or None if unusable.

    A plugin that fails to import is reported and skipped: a broken or
    half-installed third-party package must not make the app unstartable.
    """
    try:
        if name in _bundled_names():
            module = importlib.import_module(f"{BUNDLED_PACKAGE}.{name}")
            plugin = getattr(module, "PLUGIN", None)
        else:
            entry_point = _entry_points_by_name().get(name)
            plugin = entry_point.load() if entry_point is not None else None
    except Exception as exc:
        _log.warning("plugin %r failed to load: %s", name, exc)
        return None

    if not isinstance(plugin, Plugin):
        _log.warning("plugin %r exposes no PLUGIN descriptor; ignoring", name)
        return None
    if plugin.name != name:
        _log.warning("plugin package %r declares PLUGIN.name=%r; they must match, ignoring",
                     name, plugin.name)
        return None
    return plugin


def requested(env=None) -> list[str] | None:
    """Plugin names PLEXORA_PLUGINS asks for; None means "everything found".

    An empty string is distinct from unset: it means a deliberate core-only
    build and must not fall through to the default.
    """
    env = os.environ if env is None else env
    raw = env.get(ENV_VAR)
    if raw is None:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]


def install(app, names=None) -> list[Plugin]:
    """Mount the requested plugins on `app` and record them."""
    if names is None:
        names = requested()

    available = available_names()
    if names is None:
        wanted = available
    else:
        wanted = [name for name in names if name in available]
        for missing in [name for name in names if name not in available]:
            _log.warning("%s names unknown plugin %r; ignoring", ENV_VAR, missing)

    chosen = []
    for name in wanted:
        plugin = load(name)
        if plugin is None:
            continue
        blueprint = plugin.load_blueprint()
        if blueprint is not None:
            _guard_licensed_routes(plugin, blueprint)
            app.register_blueprint(blueprint, url_prefix=plugin.url_prefix)
        chosen.append(plugin)

    app.config[_CONFIG_KEY] = chosen
    _warn_shortcut_clashes(list(CORE_TOOLS) + chosen)
    return chosen


def _guard_licensed_routes(plugin: Plugin, blueprint) -> None:
    """A Pro plugin's routes, or a mixed plugin's Pro endpoints, answer 403
    without the entitlement. Before registration: Flask refuses a
    before_request added to a blueprint that is already registered. A Free
    plugin gets nothing added at all. Every requested plugin is installed
    whatever the licence; `unlocked` decides, per request, whether a page
    shows it."""
    if not plugin.entitlement and not plugin.endpoint_entitlements:
        return
    from plexora.licensing import guards

    if plugin.entitlement:
        guards.guard_blueprint(blueprint, plugin.entitlement)
    by_entitlement: dict = {}
    for endpoint, needed in (plugin.endpoint_entitlements or {}).items():
        by_entitlement.setdefault(needed, []).append(endpoint)
    for needed, endpoints in by_entitlement.items():
        guards.guard_blueprint(blueprint, needed, endpoints=endpoints)


def _warn_shortcut_clashes(chosen: list[Plugin]) -> None:
    """Report two plugins that claimed the same keystroke.

    A warning rather than a startup error, and only here: a descriptor can
    validate its OWN shortcut in isolation, but whether it collides is a
    question about the installed set, which nothing knows until this point. An
    error would let one third-party plugin stop the app from starting by
    choosing an unlucky letter, so instead both are reported by name and the
    client resolves it deterministically -- see keyboardShortcuts.js, where the
    first registration wins and the loser is left with its menu label and no
    key rather than silently stealing the other's.
    """
    claims: dict[str, list[str]] = {}
    for plugin in chosen:
        if plugin.shortcut:
            claims.setdefault(plugin.shortcut, []).append(f"tool {plugin.name!r}")
        for item in plugin.nav_items:
            if item.shortcut:
                claims.setdefault(item.shortcut, []).append(
                    f"{plugin.name!r}'s {item.label!r}")
    for spec, owners in sorted(claims.items()):
        if len(owners) > 1:
            _log.warning("shortcut %r claimed by %s; only the first will fire",
                         spec, " and ".join(owners))


#: Core's own tools (Rotate, Flip). Module-level and read at call time, never
#: bound as a default argument, so a test can blank it with monkeypatch and
#: keep asserting on exactly the plugins it installed.
CORE_TOOLS = core_tools.CORE_TOOLS


def installed(app) -> list[Plugin]:
    """The plugins install() mounted -- plugins only, never core's tools."""
    return app.config.get(_CONFIG_KEY, [])


def tools(app) -> list[Plugin]:
    """Everything that opens as a tool card: core's tools, then the plugins.

    Core's first so a plugin can never shadow `rotate` or `flip` by name --
    `find` returns the first match. Menus, layer sections and asset loading
    keep reading `installed()`, because core's tools have none of those.
    """
    return list(CORE_TOOLS) + list(installed(app))


def find(app, name) -> Plugin | None:
    return next((p for p in tools(app) if p.name == name), None)


def unlocked(plugin) -> bool:
    """Whether this licence unlocks `plugin`, so the page may show it.

    A plugin with no `entitlement` -- every basic one -- returns before the
    licence is looked at. A Pro plugin the licence does not unlock is still
    installed (its routes refuse, its `layer_jobs` builders keep serving what
    it made) but absent from every page-facing list below: no menu row, no
    launcher, no layer section, no scripts. Asked per request, so a licence
    activated in Settings shows the plugin on the next page load without a
    restart. Licensing misbehaving reads as locked, never as a broken page.
    `endpoint_entitlements` never hide a plugin: a mixed plugin stays, and its
    panel leaves out the controls it cannot use (`licensed()` in templates).
    """
    if not getattr(plugin, "entitlement", None):
        return True
    try:
        from plexora import licensing

        return bool(licensing.allows(plugin.entitlement))
    except Exception:
        return False


def nav_items(app, base_url="") -> list[dict]:
    """Every installed plugin's core-menu entries, in a stable order.

    Sorted rather than left in discovery order: which plugins are found first
    depends on entry-point metadata and directory listing, and a File menu whose
    items move between machines is a File menu nobody can be told how to use.

    Independent of any datasource, unlike `tools_for`. These entries lead to
    pages that are not about one project -- Figure Builder's library spans them
    -- so gating them on the current project would hide the way in from the
    state the user is most often in when they want it, which is having nothing
    open.
    """
    items = []
    for plugin in installed(app):
        if unlocked(plugin):
            items.extend(plugin.describe_nav(base_url))
    items.sort(key=lambda item: (item["menu"], item["order"], item["label"]))
    return items


def tools_for(app, project) -> list[Plugin]:
    """Installed plugins compatible with this datasource -- what the Tools menu
    offers.

    Deliberately compatibility, not readiness. A plugin whose feature table the
    project has not got yet still belongs in the menu: opening it is how the
    user gets asked for one. Filtering these out here hid the tool AND the only
    route to making it work.

    A LAYER section is not a tool and is not offered here. It is already on
    screen -- see `layer_sections_for` -- and a Tools entry that opened
    something already open would be a second way to reach one panel with no
    way to tell which one you got.

    A Pro plugin this licence does not unlock is not offered (`unlocked`).

    In menu order (`Plugin.order`); the sort is stable, so ties keep discovery
    order.
    """
    offered = [p for p in tools(app)
               if not p.is_layer_section and p.requires.applies_to(project) and unlocked(p)]
    return sorted(offered, key=lambda p: p.order)


def layer_sections_for(app, project) -> list[Plugin]:
    """Installed plugins that are viewer LAYERS for this datasource.

    Mounted on page load rather than opened, so the gate is the same one
    `tools_for` uses -- `applies_to`, not `satisfied_by`. A transcript layer
    whose tile cache is still building APPLIES: the run has transcripts in it.
    Requiring readiness would mean the section vanished for exactly as long as
    it had something to say, and the user would watch a sample finish
    importing with no sign that anything was coming.

    A Pro layer plugin this licence does not unlock is not mounted; the
    layer's card and saved tiles stay, since what it made is a read.
    """
    return [p for p in installed(app)
            if p.is_layer_section and p.requires.applies_to(project) and unlocked(p)]


def ready_tools(app, project) -> list[Plugin]:
    """Installed plugins this datasource can open right now -- everything in
    tools_for() that is not still missing an input.

    A layer section is not openable, so it is not here either. It matters
    because `?tool=<name>` is a URL somebody can still have bookmarked: left
    in, a stale link would "activate" a layer as a tool and load its scripts
    a second time on a page that had already loaded them for the section.
    """
    return [p for p in tools(app)
            if not p.is_layer_section and p.requires.satisfied_by(project) and unlocked(p)]
