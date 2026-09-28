#!/usr/bin/env python
"""Generate or check Plexora's reference documentation.

    python tools/docs/sync_docs.py generate          # rewrite generated pages
    python tools/docs/sync_docs.py check             # fail if they are stale
    python tools/docs/sync_docs.py version --source-ref REF

`generate` dumps the public API in a child process (see
tools/docs/python_api.py for why), renders every generated page in memory,
and writes the ones that changed; files in generated folders that are no
longer produced are deleted. `check` renders the same tree and compares it to
what is committed, naming each stale page and the symbol behind it. It is
what CI and tests/test_docs_freshness.py run.

`version` only rewrites website/generated/version.json (stdlib only; the CI
build job calls it before installing anything, so source links point at the
deployed commit).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.docs import config  # noqa: E402


def version_json(source_ref: str | None = None) -> str:
    version = config.pyproject_version()
    ref = source_ref or config.source_ref(version)
    return json.dumps({"source_ref": ref, "version": version}, indent=2, sort_keys=True) + "\n"


def dump_model(model_json: Path | None = None) -> dict:
    """The public API model, from a child process with an isolated data root."""
    if model_json:
        return json.loads(Path(model_json).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="plexora-docs-") as tmp:
        root = Path(tmp)
        (root / "home").mkdir()
        out = root / "model.json"
        proc = subprocess.run(
            [sys.executable, "-m", "tools.docs.python_api", "--out", str(out)],
            cwd=config.REPO, env=config.child_env(root / "data"), capture_output=True, text=True,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit("sync_docs: the API model dump failed (output above).")
        return json.loads(out.read_text(encoding="utf-8"))


def render(model: dict) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """`(content files, generated/ files, problems)`."""
    from tools.docs import cli_api, envvars, formats, manifest, python_pages

    m = manifest.load()
    symbols = model["symbols"]
    problems = manifest.classify(m, symbols)
    content = python_pages.render_all(m, symbols)
    cli_files, cli_problems = cli_api.render_all(m)
    content.update(cli_files)
    problems += cli_problems
    content.update(formats.render(m, model.get("formats")))
    content.update(envvars.render())
    generated = {
        "aliases.json": aliases_json(m, symbols),
    }
    return content, generated, problems


def aliases_json(m, symbols) -> str:
    """Search aliases, for tooling and the site: {word: [page urls]}."""
    out: dict[str, list[str]] = {}
    for word, targets in sorted((m.data.get("search_aliases") or {}).items()):
        urls = []
        for target in targets:
            if target.startswith("/"):
                urls.append(target)
            elif target in m.pages:
                urls.append(m.pages[target].url)
        out[word] = urls
    for qualname, page in sorted(m.pages.items()):
        for word in m.extras(qualname).get("search") or []:
            out.setdefault(word, [])
            if page.url not in out[word]:
                out[word].append(page.url)
    return json.dumps(dict(sorted(out.items())), indent=2, ensure_ascii=False) + "\n"


def committed(content_root: Path, generated_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    content = {}
    for rel in config.GENERATED_DIRS:
        base = content_root / rel
        if base.exists():
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    content[path.relative_to(content_root).as_posix()] = path.read_text(encoding="utf-8")
    for rel in config.GENERATED_FILES:
        path = content_root / rel
        if path.exists():
            content[rel] = path.read_text(encoding="utf-8")
    generated = {}
    for name in ("aliases.json",):
        path = generated_root / name
        if path.exists():
            generated[name] = path.read_text(encoding="utf-8")
    return content, generated


def symbol_of(text: str) -> str:
    for line in text.splitlines()[:20]:
        if line.startswith("symbol:"):
            return json.loads(line.split(":", 1)[1])
    return ""


def compare(expected: dict[str, str], actual: dict[str, str], prefix: str) -> list[str]:
    report = []
    for path in sorted(set(expected) | set(actual)):
        if path not in actual:
            report.append(f"  missing  {prefix}{path}")
        elif path not in expected:
            report.append(f"  stale    {prefix}{path} (no longer generated; delete it)")
        elif expected[path] != actual[path]:
            symbol = symbol_of(expected[path])
            first = next((line for line in difflib.unified_diff(
                actual[path].splitlines(), expected[path].splitlines(), lineterm="", n=0)
                if line[:1] in "+-" and not line.startswith(("+++", "---"))), "")
            report.append(f"  changed  {prefix}{path}" + (f"  [{symbol}]" if symbol else "")
                          + (f"\n             {first[:110]}" if first else ""))
    return report


def write_tree(files: dict[str, str], root: Path, managed: list[Path]) -> list[str]:
    changed = []
    for rel, text in sorted(files.items()):
        path = root / rel
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        changed.append(rel)
    wanted = {(root / rel).resolve() for rel in files}
    for base in managed:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.resolve() not in wanted:
                path.unlink()
                changed.append(f"{path.relative_to(root).as_posix()} (deleted)")
        for directory in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()
    return changed


def cmd_generate(args) -> int:
    model = dump_model(args.model_json)
    content, generated, problems = render(model)
    if problems:
        print("sync_docs: manifest.yaml does not match the code:\n  " + "\n  ".join(problems), file=sys.stderr)
        if not args.force:
            return 1
    out = Path(args.out) if args.out else config.WEBSITE
    content_root = out / "content" / "docs" if args.out else config.CONTENT
    generated_root = out / "generated" if args.out else config.GENERATED
    managed = [content_root / rel for rel in config.GENERATED_DIRS]
    changed = write_tree(content, content_root, managed)
    changed += [f"generated/{c}" for c in write_tree(generated, generated_root, [])]
    version_path = generated_root / "version.json"
    current = json.loads(version_path.read_text()) if version_path.exists() else {}
    if current.get("version") != config.pyproject_version() or args.source_ref:
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_text(version_json(args.source_ref), encoding="utf-8")
        changed.append("generated/version.json")
    print(f"sync_docs: {len(content)} pages rendered, {len(changed)} file(s) changed.")
    for rel in changed:
        print(f"  {rel}")
    return 0


def cmd_check(args) -> int:
    model = dump_model(args.model_json)
    content, generated, problems = render(model)
    actual_content, actual_generated = committed(config.CONTENT, config.GENERATED)
    report = compare(content, actual_content, "website/content/docs/")
    report += compare(generated, actual_generated, "website/generated/")
    version_path = config.GENERATED / "version.json"
    current = json.loads(version_path.read_text()) if version_path.exists() else {}
    if current.get("version") != config.pyproject_version():
        report.append(f"  changed  website/generated/version.json (version {current.get('version')} "
                      f"!= pyproject {config.pyproject_version()})")
    ok = True
    if problems:
        ok = False
        print("sync_docs check: manifest.yaml does not match the code:\n  " + "\n  ".join(problems))
    if report:
        ok = False
        print(f"sync_docs check: {len(report)} generated file(s) are out of date:")
        print("\n".join(report))
        print("\nRun `python tools/docs/sync_docs.py generate` and commit the result.")
    if ok:
        print(f"sync_docs check: {len(content)} generated pages are up to date.")
    return 0 if ok else 1


def cmd_version(args) -> int:
    path = config.GENERATED / "version.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version_json(args.source_ref), encoding="utf-8")
    print(path.read_text(), end="")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate or check Plexora's reference documentation.")
    subs = parser.add_subparsers(dest="command", required=True)
    gen = subs.add_parser("generate", help="Rewrite the generated pages.")
    gen.add_argument("--out", help="Write into this directory instead of website/.")
    gen.add_argument("--model-json", help="Use a saved API model instead of dumping one.")
    gen.add_argument("--source-ref", help="Record this git ref in version.json.")
    gen.add_argument("--force", action="store_true", help="Write even when the manifest has problems.")
    chk = subs.add_parser("check", help="Fail if the generated pages are out of date.")
    chk.add_argument("--model-json", help="Use a saved API model instead of dumping one.")
    ver = subs.add_parser("version", help="Rewrite website/generated/version.json only.")
    ver.add_argument("--source-ref", help="The git ref source links should point at.")
    args = parser.parse_args(argv)
    return {"generate": cmd_generate, "check": cmd_check, "version": cmd_version}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
