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
import os
import pkgutil

from plexora.api.plugin import Plugin

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
        print(f"WARNING: plugin {name!r} failed to load: {exc}")
        return None

    if not isinstance(plugin, Plugin):
        print(f"WARNING: plugin {name!r} exposes no PLUGIN descriptor; ignoring")
        return None
    if plugin.name != name:
        print(
            f"WARNING: plugin package {name!r} declares PLUGIN.name={plugin.name!r}; "
            "they must match, ignoring"
        )
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
            print(f"WARNING: {ENV_VAR} names unknown plugin {missing!r}; ignoring")

    chosen = []
    for name in wanted:
        plugin = load(name)
        if plugin is None:
            continue
        blueprint = plugin.load_blueprint()
        if blueprint is not None:
            app.register_blueprint(blueprint, url_prefix=plugin.url_prefix)
        chosen.append(plugin)

    app.config[_CONFIG_KEY] = chosen
    _warn_shortcut_clashes(chosen)
    return chosen


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
            print(f"WARNING: shortcut {spec!r} claimed by {' and '.join(owners)}; "
                  f"only the first will fire")


def installed(app) -> list[Plugin]:
    return app.config.get(_CONFIG_KEY, [])


def find(app, name) -> Plugin | None:
    return next((p for p in installed(app) if p.name == name), None)


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
    """
    return [p for p in installed(app) if p.requires.applies_to(project)]


def ready_tools(app, project) -> list[Plugin]:
    """Installed plugins this datasource can open right now -- everything in
    tools_for() that is not still missing an input."""
    return [p for p in installed(app) if p.requires.satisfied_by(project)]
