"""Plugins bundled with Plexora.

Each subpackage here is a plugin: a directory holding its own server code,
client assets, templates and tests, exposing a module-level `PLUGIN` descriptor
(see plexora.api.plugin.Plugin).

They are discovered by scanning this package -- see plexora.server.plugins --
and are otherwise ordinary plugins. They get no privileges a pip-installed
third-party plugin lacks, and they consume only the public `plexora.api`
surface. That is deliberate: a gap in the API becomes a gap in the shipped
product rather than something only outside authors run into.
"""
