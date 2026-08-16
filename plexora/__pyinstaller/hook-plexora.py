"""Make plugins survive a frozen build.

Plugins are the one part of the app that static analysis cannot see. Nothing
imports `plexora.plugins.gating` -- it is resolved by name at runtime, from a
directory scan or from entry-point metadata -- so PyInstaller's dependency
graph has no reason to include it and would produce a build where the viewer
works and every tool has vanished.

Three things are collected here:

- **submodules** of `plexora.plugins`, because the import is dynamic;
- **data files** for `plexora` (templates, shaders, client JS/CSS) and for each
  plugin (its own templates/ and static/), because a plugin's Blueprint serves
  those out of its own directory;
- **distribution metadata**, because third-party plugins advertise themselves
  through the `plexora.plugins` entry point group, and `entry_points()` reads
  that from dist-info -- which is not bundled unless asked for.
"""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_entry_point,
    collect_submodules,
)

# Bundled plugins: imported by name, so nothing references them statically.
hiddenimports = collect_submodules("plexora.plugins")

# Third-party plugins: their modules AND the metadata that advertises them.
_ep_datas, _ep_hiddenimports = collect_entry_point("plexora.plugins")
hiddenimports += _ep_hiddenimports

# Frontend assets and every plugin's own templates/static tree. include_py_files
# is off: the .py files are already in the archive as modules, and copying them
# again would ship readable source alongside the bytecode.
datas = _ep_datas + collect_data_files("plexora", include_py_files=False)
