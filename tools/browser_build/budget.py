"""Hold the website to its size bars (plan Phase 5): what a first visit costs,
and what Cloudflare Pages accepts.

    python tools/browser_build/budget.py [--bundle DIR] [--site DIR] [--shell FILE]

- `--bundle` (build.py's `py/<version>/`): the first visit's Python -- what
  the worker fetches before Python is ready (client/src/js/workers/
  pythonWorker.js): `stages.json`, Pyodide's loader, wasm, standard library
  and lockfile, and the base stage's packages and wheels, each counted as it
  is stored (a `.br` beside a file is what the bucket serves) -- at most
  `--max-first-visit-mb` (50). manifest.json's own `first_visit_bytes`
  (build.py's count, which takes pyodide.js and package.json instead of the
  lockfile) is printed beside it.
- `--site` (static_build.py's output): what Pages takes -- no file over
  `--max-pages-file-mib` (25 MiB), at most `--max-pages-files` (20,000),
  `_headers` at most 100 rules and 2,000 characters a header, `_redirects` at
  most 2,000 static and 100 dynamic rules.
- `--shell` (tests/browser/smoke.mjs --shell-out): the bytes a first visit
  took from the site's own origin outside /py/, gzipped (Pages compresses
  text on the fly; gzip is the larger estimate), at most `--max-shell-mb` (4).

MB is 10^6 bytes, as build.py reports; Pages' file limit is in MiB, as
Cloudflare states it. Prints JSON, one entry per bar; exits 1 when one is
exceeded or cannot be measured.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MB = 1_000_000
MIB = 1 << 20
#: What the worker fetches from the bundle before the base stage.
BOOT_FILES = ("stages.json", "pyodide/pyodide.mjs", "pyodide/pyodide.asm.mjs", "pyodide/pyodide.asm.wasm",
              "pyodide/python_stdlib.zip", "pyodide/pyodide-lock.json")
#: Cloudflare Pages' limits for `_headers` and `_redirects`.
HEADER_RULES, HEADER_CHARS = 100, 2000
STATIC_REDIRECTS, DYNAMIC_REDIRECTS = 2000, 100


def _bar(name, measured, limit, unit, **extra) -> dict:
    return {"bar": name, "measured": measured, "limit": limit, "unit": unit,
            "ok": measured is not None and measured <= limit, **extra}


def first_visit(bundle: Path) -> dict:
    """The bytes the worker fetches before Python is ready, and what was missing."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    stages = json.loads((bundle / "stages.json").read_text(encoding="utf-8"))
    lock = json.loads((bundle / "pyodide" / "pyodide-lock.json").read_text(encoding="utf-8"))
    files = manifest["files"]
    wanted = list(BOOT_FILES)
    wanted += [f"pyodide/{lock['packages'][name]['file_name']}" for name in stages["base"]["pyodide"]]
    wanted += [f"wheels/{name}" for name in stages["base"]["wheels"]]
    total, missing = 0, []
    for name in wanted:
        stored = files.get(name + ".br") or files.get(name)
        if stored is None:
            missing.append(name)
        else:
            total += stored["bytes"]
    return {"bytes": total, "files": len(wanted), "missing": missing,
            "manifest_first_visit_bytes": manifest.get("first_visit_bytes"),
            "plexora": manifest.get("plexora")}


def check_bundle(bundle: Path, max_mb: float) -> list:
    if not (bundle / "manifest.json").is_file():
        return [_bar("first_visit", None, max_mb * MB, "bytes", error=f"{bundle}: no manifest.json")]
    found = first_visit(bundle)
    bar = _bar("first_visit", found["bytes"], max_mb * MB, "bytes", files=found["files"],
               mb=round(found["bytes"] / MB, 2), plexora=found["plexora"],
               manifest_first_visit_bytes=found["manifest_first_visit_bytes"])
    if found["missing"]:
        bar.update(ok=False, error=f"not in manifest.json: {', '.join(found['missing'])}")
    return [bar]


def _header_rules(text: str) -> tuple:
    rules, longest = 0, 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            longest = max(longest, len(line.strip()))
        else:
            rules += 1
    return rules, longest


def _redirect_rules(text: str) -> tuple:
    static = dynamic = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        source = line.split()[0]
        if "*" in source or any(part.startswith(":") for part in source.split("/")):
            dynamic += 1
        else:
            static += 1
    return static, dynamic


def check_site(site: Path, max_file_mib: float, max_files: int) -> list:
    if not (site / "index.html").is_file():
        return [_bar("pages_largest_file", None, max_file_mib * MIB, "bytes",
                     error=f"{site}: not a static build (no index.html)")]
    files = [path for path in site.rglob("*") if path.is_file()]
    largest = max(files, key=lambda path: path.stat().st_size)
    over = sorted(path.relative_to(site).as_posix() for path in files
                  if path.stat().st_size > max_file_mib * MIB)
    bars = [_bar("pages_largest_file", largest.stat().st_size, max_file_mib * MIB, "bytes",
                 file=largest.relative_to(site).as_posix(), over=over),
            _bar("pages_files", len(files), max_files, "files",
                 total_bytes=sum(path.stat().st_size for path in files))]
    headers = site / "_headers"
    if headers.is_file():
        rules, longest = _header_rules(headers.read_text(encoding="utf-8"))
        bars.append(_bar("pages_header_rules", rules, HEADER_RULES, "rules"))
        bars.append(_bar("pages_header_chars", longest, HEADER_CHARS, "characters"))
    redirects = site / "_redirects"
    if redirects.is_file():
        static, dynamic = _redirect_rules(redirects.read_text(encoding="utf-8"))
        bars.append(_bar("pages_static_redirects", static, STATIC_REDIRECTS, "rules"))
        bars.append(_bar("pages_dynamic_redirects", dynamic, DYNAMIC_REDIRECTS, "rules"))
    return bars


def check_shell(record: Path, max_mb: float) -> list:
    try:
        shell = json.loads(record.read_text(encoding="utf-8"))
        measured = int(shell["gzipBytes"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [_bar("shell", None, max_mb * MB, "bytes", error=f"{record}: {exc}")]
    if not shell.get("requests"):
        return [_bar("shell", None, max_mb * MB, "bytes", error=f"{record}: no request was counted")]
    return [_bar("shell", measured, max_mb * MB, "bytes", mb=round(measured / MB, 2),
                 as_served_bytes=shell.get("bytes"), requests=shell.get("requests"))]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bundle", type=Path, help="build.py's py/<version>/ folder")
    parser.add_argument("--site", type=Path, help="static_build.py's output")
    parser.add_argument("--shell", type=Path, help="smoke.mjs --shell-out's record")
    parser.add_argument("--max-first-visit-mb", type=float, default=50)
    parser.add_argument("--max-pages-file-mib", type=float, default=25)
    parser.add_argument("--max-pages-files", type=int, default=20_000)
    parser.add_argument("--max-shell-mb", type=float, default=4)
    args = parser.parse_args(argv)
    if not (args.bundle or args.site or args.shell):
        parser.error("nothing to measure: give --bundle, --site or --shell")
    bars = []
    if args.bundle:
        bars += check_bundle(args.bundle, args.max_first_visit_mb)
    if args.site:
        bars += check_site(args.site, args.max_pages_file_mib, args.max_pages_files)
    if args.shell:
        bars += check_shell(args.shell, args.max_shell_mb)
    failed = [bar["bar"] for bar in bars if not bar["ok"]]
    print(json.dumps({"bars": bars, "failed": failed}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
