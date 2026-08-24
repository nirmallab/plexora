"""Entry point for the double-clickable PyInstaller builds.

The frozen executables used to be built off `run.py`, which is the thinnest of
Plexora's entry points and the one nobody runs on purpose: it binds a port,
prints a line, and serves. Everything a person double-clicking an icon actually
needs -- the browser opening by itself, moving off a port another copy already
holds, `--version`, `plexora where` when they cannot find their data -- lives in
`plexora.cli`, so the executables are built off that instead and gain all of it.

Its own file rather than pointing PyInstaller at `plexora/cli.py` directly:
PyInstaller compiles the entry script as `__main__`, and a module that is
simultaneously `plexora.cli` (imported by the package) and `__main__` is
imported twice under two names, which duplicates every module-level object in
it.
"""

import multiprocessing

# Must run before anything spawns a process. In a onefile build the child
# re-executes the bundled executable, and without this it would re-run the
# whole application instead of the worker -- the classic frozen fork bomb.
# `plexora/__init__.py` calls it too; calling it twice is documented as safe,
# and here it happens before that import rather than during it.
multiprocessing.freeze_support()


if __name__ == "__main__":
    from plexora.cli import main

    raise SystemExit(main())
