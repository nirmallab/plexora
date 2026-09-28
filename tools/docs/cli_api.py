"""Command-line reference, generated from the argparse definitions.

`plexora/cli.py` is loaded straight from its file (as tests/test_cli.py
does), so no Plexora package is imported and no server is built. The one
exception inside it -- the root parser's `--version`, which asks the
installed package for its version -- is stubbed before any parser is built.

Each command page has: purpose, usage, arguments, options, examples, related
commands and the Python equivalent. Examples and Python equivalents come
from tools/docs/cli_examples.yaml; everything else from argparse.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path

import yaml

from tools.docs import config, mdx

CLI_PATH = config.REPO / "plexora" / "cli.py"
#: Read by cli.py when it builds parsers; scrubbed so a developer's shell
#: cannot leak into documented defaults.
SCRUBBED_ENV = ("PLEXORA_HOST", "PLEXORA_NODE_HOST", "PLEXORA_NODE_TOKEN")


@contextmanager
def clean_env():
    saved = {k: os.environ.pop(k) for k in SCRUBBED_ENV if k in os.environ}
    columns = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "100"
    try:
        yield
    finally:
        os.environ.update(saved)
        if columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = columns


def load_cli():
    """`plexora/cli.py` as a standalone module, with `--version` stubbed."""
    spec = importlib.util.spec_from_file_location("plexora_cli_for_docs", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.version_string = lambda: "VERSION"
    return module


def build(cli, command: str) -> argparse.ArgumentParser:
    with clean_env():
        return cli.build_parser(command or None)


def subparsers(parser: argparse.ArgumentParser):
    """`[(name, help, parser)]` for a parser's subcommands, in definition order."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help for a in action._choices_actions}
            return [(name, helps.get(name), sub) for name, sub in action.choices.items()]
    return []


def walk(cli, include) -> list[dict]:
    """Every documented command as a record, parents before children."""
    records = []

    def visit(path: list[str], parser, help_text):
        records.append(describe(cli, " ".join(path), parser, help_text))
        for name, sub_help, sub in subparsers(parser):
            visit(path + [name], sub, sub_help)

    for command in include:
        parser = build(cli, command)
        visit([command] if command else [], parser, None)
    return records


def describe(cli, path: str, parser: argparse.ArgumentParser, help_text) -> dict:
    formatter = parser._get_formatter()
    args = []
    exclusive = []
    for group in parser._mutually_exclusive_groups:
        exclusive.append([a.option_strings[-1] for a in group._group_actions if a.option_strings])
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.help == argparse.SUPPRESS:
            continue
        help_ = formatter._expand_help(action) if action.help else ""
        if isinstance(action, argparse._VersionAction) and not action.help:
            help_ = "Print the installed version and exit."
        default = action.default
        show_default = default not in (None, False, [], argparse.SUPPRESS, "") and \
            not isinstance(action, (argparse._StoreTrueAction, argparse._VersionAction))
        args.append({
            "positional": not action.option_strings,
            "flags": list(action.option_strings),
            "invocation": formatter._format_action_invocation(action),
            "dest": action.dest,
            "metavar": action.metavar if isinstance(action.metavar, str) else None,
            "nargs": action.nargs if isinstance(action.nargs, (str, int)) else None,
            "choices": list(action.choices) if action.choices and not isinstance(action.choices, dict) else None,
            "default": repr(default) if show_default and not isinstance(default, str) else (default if show_default else None),
            "required": bool(action.required) and bool(action.option_strings),
            "optional_positional": not action.option_strings and action.nargs in ("?", "*"),
            "repeatable": isinstance(action, argparse._AppendAction),
            "help": " ".join(help_.split()),
        })
    with clean_env():
        usage = parser.format_usage().strip()
    return {
        "path": path,
        "prog": parser.prog,
        "usage": usage.removeprefix("usage: "),
        "description": " ".join((parser.description or "").split()),
        "epilog": " ".join((parser.epilog or "").split()),
        "help": " ".join((help_text or "").split()),
        "args": args,
        "exclusive": exclusive,
        "subcommands": [name for name, _h, _p in subparsers(parser)],
    }


def load_examples() -> dict:
    data = yaml.safe_load(config.CLI_EXAMPLES.read_text(encoding="utf-8")) if config.CLI_EXAMPLES.exists() else {}
    return data or {}


def slug_path(path: str) -> str:
    """File path under cli/ for a command path."""
    if not path:
        return "plexora"
    return path.replace(" ", "/")


def page_url(path: str, records_by_path: dict) -> str:
    rel = slug_path(path)
    if records_by_path.get(path, {}).get("subcommands"):
        return f"/docs/cli/{rel}"
    return f"/docs/cli/{rel}"


def render_command(record: dict, records_by_path: dict, examples: dict, internal: dict) -> str:
    path = record["path"]
    title = f"plexora {path}".strip()
    entry = examples.get("commands", {}).get(path or "plexora", {}) or {}
    purpose = entry.get("purpose") or record["description"] or record["help"]
    front = mdx.frontmatter({
        "title": title,
        "description": record["help"] or record["description"] or None,
        "generated": True,
        "symbol": f"cli:{title}",
        "aliases": entry.get("search") or None,
        "source": {"path": "plexora/cli.py"},
    })
    blocks = [front, "{/* GENERATED from the argparse definitions in plexora/cli.py and "
                     "tools/docs/cli_examples.yaml. Run: python tools/docs/sync_docs.py generate */}"]
    if purpose and purpose != (record["help"] or record["description"]):
        blocks.append(mdx.escape(purpose))
    elif record["description"] and record["help"] and record["description"] != record["help"]:
        blocks.append(mdx.escape(record["description"]))
    blocks.append(mdx.join("## Usage", mdx.code_block(record["usage"], "bash")))
    if record["epilog"] and path:
        blocks.append(mdx.escape(record["epilog"]))
    if record["subcommands"]:
        rows = []
        for name in record["subcommands"]:
            child = records_by_path[f"{path} {name}".strip()]
            rows.append((f"{title} {name}", page_url(child["path"], records_by_path), child["help"]))
        blocks.append(mdx.join("## Subcommands", mdx.component("Cards", {}, "\n".join(
            mdx.component("Card", {"title": t, "href": h, "description": d or None}) for t, h, d in rows))))
    positional = [a for a in record["args"] if a["positional"]]
    options = [a for a in record["args"] if not a["positional"]]
    internal_flags = set(internal.get(path, []) or [])
    if positional:
        rows = [mdx.component("Param", {
            "name": a["metavar"] or a["dest"],
            "type": ("choice: " + ", ".join(a["choices"])) if a["choices"] else None,
            "required": not a["optional_positional"],
            "kind": "repeatable" if a["nargs"] in ("+", "*") else None,
        }, mdx.escape(a["help"])) for a in positional]
        blocks.append(mdx.join("## Arguments", mdx.component("ParamTable", {}, "\n\n".join(rows))))
    if options:
        rows = []
        for a in options:
            name = a["invocation"]
            kind = "internal" if set(a["flags"]) & internal_flags else ("repeatable" if a["repeatable"] else None)
            type_ = ("one of: " + ", ".join(a["choices"])) if a["choices"] else None
            rows.append(mdx.component("Param", {
                "name": name, "type": type_, "default": a["default"], "required": a["required"], "kind": kind,
            }, mdx.escape(a["help"])))
        blocks.append(mdx.join("## Options", mdx.component("ParamTable", {}, "\n\n".join(rows))))
        for group in record["exclusive"]:
            blocks.append(f"Only one of {', '.join(f'`{g}`' for g in group)} may be given.")
    if entry.get("examples"):
        parts = ["## Examples"]
        for example in entry["examples"]:
            if example.get("title"):
                parts.append(f"**{mdx.inline(example['title'])}**")
            parts.append(mdx.code_block(example["command"].strip(), "bash"))
            if example.get("description"):
                parts.append(mdx.escape(example["description"].strip()))
        blocks.append(mdx.join(*parts))
    python = entry.get("python")
    if python:
        parts = ["## Python equivalent"]
        if python.get("text"):
            parts.append(mdx.escape(python["text"].strip(), linker=_python_linker()))
        if python.get("code"):
            parts.append(mdx.code_block(python["code"].strip(), "python"))
        blocks.append(mdx.join(*parts))
    related = entry.get("related") or []
    if related:
        cards = []
        for other in related:
            key = "" if other == "plexora" else other
            if key in records_by_path:
                cards.append(mdx.component("Card", {"title": f"plexora {key}".strip(),
                                                    "href": page_url(key, records_by_path),
                                                    "description": records_by_path[key]["help"] or
                                                    records_by_path[key]["description"] or None}))
            elif other.startswith("/docs/"):
                cards.append(mdx.component("Card", {"title": other.rsplit("/", 1)[-1].replace("-", " ").capitalize(),
                                                    "href": other}))
        if cards:
            blocks.append(mdx.join("## Related", mdx.component("Cards", {}, "\n".join(cards))))
    return mdx.join(*blocks)


_LINKER = None


def _python_linker():
    global _LINKER
    if _LINKER is None:
        from tools.docs import manifest
        from tools.docs.python_pages import Linker

        _LINKER = Linker(manifest.load())
    return _LINKER


def render_index(records: list[dict], records_by_path: dict, examples: dict) -> str:
    rows = ["| Command | What it does |", "|---|---|"]
    for record in records:
        title = f"plexora {record['path']}".strip()
        rows.append(f"| [`{title}`]({page_url(record['path'], records_by_path)}) | "
                    f"{mdx.inline(record['help'] or record['description'])} |")
    intro = examples.get("intro", "")
    return mdx.join(
        mdx.frontmatter({"title": "Command line", "description": "Every plexora command, its options and examples.",
                         "generated": True}),
        "{/* GENERATED from plexora/cli.py and tools/docs/cli_examples.yaml. "
        "Run: python tools/docs/sync_docs.py generate */}",
        mdx.escape(intro.strip()) if intro else "",
        "\n".join(rows),
    )


def render_all(manifest) -> tuple[dict[str, str], list[str]]:
    from tools.docs.manifest import cli_problems

    cli = load_cli()
    problems = cli_problems(manifest, cli.SUBCOMMANDS)
    include = manifest.cli.get("include") or []
    internal = manifest.cli.get("internal") or {}
    examples = load_examples()
    records = walk(cli, include)
    by_path = {r["path"]: r for r in records}
    documented = {r["path"] or "plexora" for r in records}
    for key in (examples.get("commands") or {}):
        if key not in documented:
            problems.append(f"cli_examples.yaml has an entry for `{key}`, which is not a documented command")
    out: dict[str, str] = {}
    top_pages = []
    for record in records:
        path = record["path"]
        rel = slug_path(path)
        if record["subcommands"]:
            out[f"cli/{rel}/index.mdx"] = render_command(record, by_path, examples, internal)
            out[f"cli/{rel}/meta.json"] = _meta(f"plexora {path}".strip(), [*record["subcommands"]])
        else:
            out[f"cli/{rel}.mdx"] = render_command(record, by_path, examples, internal)
        if " " not in path:
            top_pages.append(rel)
    out["cli/index.mdx"] = render_index(records, by_path, examples)
    out["cli/meta.json"] = _meta("Command line", [*top_pages], icon="Terminal")
    return out, problems


def _meta(title, pages, **extra):
    import json

    return json.dumps({"title": title, **extra, "pages": pages}, indent=2) + "\n"
