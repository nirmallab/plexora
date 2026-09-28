"""Paths, the package version, and the environment the model dump runs in.

Stdlib only: `sync_docs.py version` runs in the CI build job before any
dependency is installed.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools" / "docs"
WEBSITE = REPO / "website"
CONTENT = WEBSITE / "content" / "docs"
GENERATED = WEBSITE / "generated"
MANIFEST = TOOLS / "manifest.yaml"
CLI_EXAMPLES = TOOLS / "cli_examples.yaml"
ENV_VARS = TOOLS / "env_vars.yaml"
BUILD = TOOLS / ".build"

#: Generated trees, relative to CONTENT. `check` compares exactly these (and
#: the generated/ JSON files); everything else under content/docs is authored.
GENERATED_DIRS = ("python-api", "plugin-api/descriptor", "plugin-api/runtime", "cli")
GENERATED_FILES = ("reference/supported-formats.mdx", "reference/environment-variables.mdx")
#: plugin-api/browser is generated too, but by website/scripts/
#: generate_browser_api.mjs (it reads JavaScript), so Node owns it.


def pyproject_version() -> str:
    """The version pyproject.toml declares.

    Not importlib.metadata: an editable install's metadata is whatever was
    current at the last `pip install -e`, and on a developer machine that lags
    the tree (0.0.24 installed against 0.0.25 declared, when this was written).
    """
    with open(REPO / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def source_ref(version: str) -> str:
    """The git ref source links point at.

    PLEXORA_DOCS_SOURCE_REF wins (CI passes the deployed commit); otherwise
    the release tag if it exists; otherwise main.
    """
    ref = os.environ.get("PLEXORA_DOCS_SOURCE_REF")
    if ref:
        return ref
    tag = f"v{version}"
    try:
        found = subprocess.run(
            ["git", "tag", "--list", tag], cwd=REPO, capture_output=True, text=True, timeout=10,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        found = []
    return tag if tag in found else "main"


def child_env(data_root: Path) -> dict[str, str]:
    """The environment the model dump (and doc examples) run under.

    Every inherited PLEXORA_* variable is dropped, because `import plexora`
    builds the whole Flask app and a stray PLEXORA_DATA_PATH or node token
    would point it at somebody's real install. The data root is always a
    throwaway directory; plugins are off so the dump does not import anndata
    or plugin dependencies; telemetry is off; hashing and terminal width are
    pinned so argparse and set ordering cannot vary between machines.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLEXORA_")}
    env.update(
        PLEXORA_DATA_PATH=str(data_root),
        PLEXORA_TELEMETRY="off",
        PLEXORA_TESTING="1",
        PLEXORA_PLUGINS="",
        COLUMNS="100",
        PYTHONHASHSEED="0",
        PYTHONIOENCODING="utf-8",
        HOME=str(data_root / "home"),
    )
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO), os.environ.get("PYTHONPATH", "")) if p)
    return env


_SLUG_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def kebab(name: str) -> str:
    """`create_project` -> `create-project`, `PluginStore` -> `plugin-store`,
    `PROJECT_SPEC_KEYS` -> `project-spec-keys`."""
    if name.isupper():
        return name.lower().replace("_", "-")
    return _SLUG_SPLIT.sub("-", name).replace("_", "-").lower()
