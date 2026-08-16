"""PyInstaller hook directory, found automatically.

Declared through the `pyinstaller40` entry point (see pyproject.toml), so a
frozen build picks these hooks up with no spec file and no --additional-hooks-dir
argument. That matters because the thing most likely to break in a frozen build
is plugin discovery, and whoever writes the spec is not necessarily the person
who added the plugin.
"""

import os


def get_hook_dirs():
    return [os.path.dirname(__file__)]
