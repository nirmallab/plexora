"""Environment variable reference, rendered from tools/docs/env_vars.yaml.

Only the `public` list reaches the site. tests/test_docs_envvars.py keeps the
YAML honest against the source tree (see `scan`).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tools.docs import config, mdx

NAME = re.compile(r"PLEXORA_[A-Z0-9_]+")

#: Where Plexora's own code reads or sets environment variables.
SCAN_GLOBS = (
    ("plexora", "**/*.py"),
    ("plexora/client/src", "**/*.js"),
    ("plexora/client/templates", "**/*.html"),
    ("desktop/src-tauri/src", "**/*.rs"),
    (".", "run.py"),
    (".", "Dockerfile"),
)


def load() -> dict:
    return yaml.safe_load(config.ENV_VARS.read_text(encoding="utf-8")) or {}


def scan(root: Path = config.REPO) -> dict[str, list[str]]:
    """`{name: [files]}` for every PLEXORA_* name in the scanned sources."""
    found: dict[str, list[str]] = {}
    for base, pattern in SCAN_GLOBS:
        for path in sorted((root / base).glob(pattern)):
            if "node_modules" in path.parts or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in set(NAME.findall(text)):
                found.setdefault(name, []).append(path.relative_to(root).as_posix())
    return found


def render() -> dict[str, str]:
    data = load()
    groups: dict[str, list[dict]] = {}
    for entry in data.get("public") or []:
        groups.setdefault(entry.get("group", "Other"), []).append(entry)
    blocks = [
        mdx.frontmatter({
            "title": "Environment variables",
            "description": "Every environment variable Plexora reads, what it changes and its default.",
            "generated": True,
            "aliases": ["env", "environment", "configuration"],
        }),
        "{/* GENERATED from tools/docs/env_vars.yaml. Run: python tools/docs/sync_docs.py generate */}",
        mdx.escape(
            "Environment variables change Plexora for one process or shell session. Most have a "
            "command-line flag or a setting with the same effect; where they differ, the order is "
            "flag, then environment variable, then the settings file (`plexora config`), then the "
            "built-in default. The data directory has its own order, described in "
            "[Where your data lives](/docs/data/data-locations)."),
    ]
    for group, entries in groups.items():
        rows = []
        for entry in entries:
            body = [mdx.escape(entry["description"])]
            if entry.get("example"):
                body.append(mdx.code_block(f"export {entry['name']}={entry['example']}", "bash"))
            rows.append(mdx.component("Param", {"name": entry["name"], "default": entry.get("default")},
                                      "\n\n".join(body)))
        blocks.append(mdx.join(f"## {group}", mdx.component("ParamTable", {}, "\n\n".join(rows))))
    return {"reference/environment-variables.mdx": mdx.join(*blocks)}
