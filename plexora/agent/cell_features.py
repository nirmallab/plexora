"""What plugins can say about one cell's surroundings, without core importing them.

`explain_cell` describes a cell from the table: its markers, its regions, its
nearest neighbours. A plugin may know more -- automatic gating's spatial
evidence knows how crowded the cell's neighbourhood is, how high the local
background sits under it, and whether a bright neighbour touches it -- and
offers it through `Plugin.cell_features_factory`, the way QC offers its
exclusions (`cell_exclusions`): a zero-argument callable returning
`provider(session, data, cell_id, marker) -> dict`. Core asks every loaded
plugin's provider and merges what comes back; a provider that fails is
skipped, never a reason `explain_cell` fails.
"""

from __future__ import annotations

import threading

_PROVIDERS = None
_LOCK = threading.Lock()


def _load() -> list:
    from plexora.server import plugins as plugin_registry

    names = plugin_registry.requested()
    if names is None:
        names = plugin_registry.available_names()
    found = []
    for name in names:
        if name not in plugin_registry.available_names():
            continue
        plugin = plugin_registry.load(name)
        factory = getattr(plugin, "cell_features_factory", None) if plugin else None
        if factory is None:
            continue
        try:
            provider = factory()
        except Exception as exc:  # pragma: no cover - a broken third-party plugin
            print(f"WARNING: plugin {name!r} cell features failed to load: {exc}")
            continue
        if provider is not None:
            found.append((name, provider))
    return found


def providers() -> list:
    global _PROVIDERS
    with _LOCK:
        if _PROVIDERS is None:
            _PROVIDERS = _load()
        return list(_PROVIDERS)


def _reset_for_tests():
    global _PROVIDERS
    with _LOCK:
        _PROVIDERS = None


def describe(session, data, cell_id, marker) -> dict:
    """{plugin name: what it says about the cell} -- {} when no plugin says
    anything (or none is asked: no marker)."""
    if not marker:
        return {}
    out = {}
    for name, provider in providers():
        try:
            found = provider(session, data, int(cell_id), marker)
        except Exception:  # a plugin's view is extra; the cell is explained without it
            continue
        if found:
            out[name] = found
    return out
