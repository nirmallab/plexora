"""Load tools/docs/manifest.yaml and check it against the model.

`classify()` is the heart of the coverage rule: every public name in the
model must land in exactly one place, and every name the manifest mentions
must exist. It returns problems as sentences rather than raising, so
`sync_docs.py` and tests/test_docs_coverage.py can print all of them at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tools.docs import config


@dataclass
class Page:
    """Where one documented symbol's page goes."""

    qualname: str
    section: dict
    category: dict
    slug: str

    @property
    def rel_dir(self) -> str:
        return f"{self.section['dir']}/{self.category['key']}"

    @property
    def path(self) -> str:
        """File path relative to content/docs."""
        return f"{self.rel_dir}/{self.slug}.mdx"

    @property
    def url(self) -> str:
        return f"/docs/{self.rel_dir}/{self.slug}"


@dataclass
class Manifest:
    data: dict
    pages: dict[str, Page] = field(default_factory=dict)

    @property
    def sections(self) -> list[dict]:
        return self.data.get("sections", [])

    def extras(self, qualname: str) -> dict:
        return (self.data.get("symbols") or {}).get(qualname) or {}

    @property
    def aliases(self) -> dict[str, str]:
        return self.data.get("aliases") or {}

    @property
    def excluded(self) -> dict[str, str]:
        return self.data.get("excluded") or {}

    @property
    def docstrings(self) -> dict:
        return self.data.get("docstrings") or {}

    @property
    def compare(self) -> list[dict]:
        return self.data.get("compare") or []

    @property
    def cli(self) -> dict:
        return self.data.get("cli") or {}

    def module_members(self, qualname: str) -> list[str]:
        return self.extras(qualname).get("members") or []


def load(path: Path = config.MANIFEST) -> Manifest:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    manifest = Manifest(data)
    used: dict[str, str] = {}
    for section in manifest.sections:
        for category in section.get("categories", []):
            for qualname in category.get("symbols", []):
                slug = manifest.extras(qualname).get("slug") or config.kebab(qualname.rsplit(".", 1)[1])
                page = Page(qualname, section, category, slug)
                manifest.pages[qualname] = page
                used.setdefault(page.path, qualname)
    return manifest


def auto_aliases(symbols: dict) -> dict[str, str]:
    """`plexora.api.plugin.X` -> `plexora.api.X` where plexora.api re-exports
    the same object (or an equal constant)."""
    out = {}
    for qualname, record in symbols.items():
        if not qualname.startswith("plexora.api.plugin."):
            continue
        name = qualname.rsplit(".", 1)[1]
        twin = f"plexora.api.{name}"
        if twin in symbols and (twin in record.get("same_as", [])
                                or (record["kind"] == "constant" and symbols[twin].get("value") == record.get("value"))):
            out[qualname] = twin
    return out


def classify(manifest: Manifest, symbols: dict) -> list[str]:
    """Problems with the manifest against the model, as sentences."""
    problems: list[str] = []
    placements: dict[str, list[str]] = {}

    def place(qualname, where):
        placements.setdefault(qualname, []).append(where)

    for qualname, page in manifest.pages.items():
        place(qualname, f"category {page.rel_dir}")
    for alias, target in manifest.aliases.items():
        place(alias, "aliases")
        if target not in manifest.pages:
            problems.append(f"alias {alias} points at {target}, which has no page")
        elif alias in symbols and target not in symbols[alias].get("same_as", []):
            problems.append(f"alias {alias} is not the same object as {target}")
    for alias, target in auto_aliases(symbols).items():
        if alias not in manifest.aliases:
            place(alias, "aliases (re-export)")
    for qualname in manifest.excluded:
        place(qualname, "excluded")
    for qualname, page in manifest.pages.items():
        members = manifest.module_members(qualname)
        for member in members:
            place(f"{qualname}.{member}", f"members of {qualname}")

    for qualname in sorted(symbols):
        where = placements.get(qualname)
        if not where:
            problems.append(f"{qualname} is public but not in manifest.yaml: add it to a category, "
                            f"`aliases` or `excluded` (with a reason)")
        elif len(where) > 1:
            problems.append(f"{qualname} is placed more than once: {', '.join(where)}")
    for qualname in sorted(placements):
        if qualname not in symbols:
            problems.append(f"manifest.yaml names {qualname}, which is not public (renamed or removed?)")

    for qualname, reason in manifest.excluded.items():
        if not str(reason or "").strip():
            problems.append(f"excluded {qualname} has no reason")

    slugs: dict[str, str] = {}
    for qualname, page in manifest.pages.items():
        key = page.path.lower()
        if key in slugs:
            problems.append(f"{qualname} and {slugs[key]} would share the page {page.path}; "
                            f"give one a `slug:` in manifest.yaml")
        slugs[key] = qualname

    known = set(manifest.pages) | set(manifest.aliases)
    for qualname, extras in (manifest.data.get("symbols") or {}).items():
        if qualname not in manifest.pages:
            problems.append(f"symbols: {qualname} has extras but no page")
            continue
        for related in extras.get("related") or []:
            if related not in known:
                problems.append(f"{qualname}: related {related} has no page")
        record = symbols.get(qualname, {})
        params = {p["name"] for p in record.get("params") or []}
        for group, names in (extras.get("param_groups") or {}).items():
            for name in names:
                if name not in params:
                    problems.append(f"{qualname}: param_groups[{group}] names {name}, not a parameter")
        members = {m["name"] for m in record.get("members") or []} | {f["name"] for f in record.get("fields") or []}
        for member in extras.get("exclude_members") or {}:
            if member not in members:
                problems.append(f"{qualname}: exclude_members names {member}, which it does not have")
    for entry in manifest.compare:
        for qualname in entry.get("symbols", []):
            if qualname not in manifest.pages:
                problems.append(f"compare '{entry.get('title')}' names {qualname}, which has no page")
    for key in ("waivers", "backlog", "allow_partial"):
        for item in manifest.docstrings.get(key) or []:
            symbol = item["symbol"] if isinstance(item, dict) else item
            base = symbol.split("#", 1)[0]
            if base not in manifest.pages and base.rsplit(".", 1)[0] not in manifest.pages:
                problems.append(f"docstrings.{key} names {symbol}, which has no page")
    return problems


def cli_problems(manifest: Manifest, subcommands) -> list[str]:
    include = set(manifest.cli.get("include") or [])
    exclude = set((manifest.cli.get("exclude") or {}).keys())
    expected = set(subcommands) | {""}
    problems = []
    for name in sorted(expected - include - exclude):
        problems.append(f"CLI command `plexora {name}` is neither in cli.include nor cli.exclude")
    for name in sorted((include | exclude) - expected):
        problems.append(f"cli lists `plexora {name}`, which is not a command")
    for name in sorted(include & exclude):
        problems.append(f"`plexora {name}` is in both cli.include and cli.exclude")
    return problems
