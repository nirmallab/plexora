"""Put the website on Cloudflare: the Python bundle on R2, the shell on Pages.

    python tools/browser_build/publish.py assets --bundle DIR --prefix py/<v>/ [--dev]
    python tools/browser_build/publish.py verify --bundle DIR --base URL
    python tools/browser_build/publish.py site --dir DIR --project NAME [--branch B]

Everything goes through wrangler (`--wrangler`, default `wrangler` on PATH;
`PLEXORA_WRANGLER` may name its `bin/wrangler.js`, run with node), logged in
as the account that holds the zone, or with `CLOUDFLARE_API_TOKEN` in CI. No
token is read or passed here.

`assets` uploads a bundle that `build.py` wrote (`<out>/py/<version>/`) to the
bucket under `--prefix`, one object per file. A file with a `.br` beside it is
stored as those brotli bytes under its own name with `Content-Encoding: br`, so
the browser inflates it natively; every object is `immutable` for a year, since
a prefix is never rewritten. A release prefix (`py/<X.Y.Z>/`) that already
holds a manifest is refused; `--dev` prefixes (`py/dev/<sha>/`) are not, and
are pruned by hand.

`verify` reads every file of the bundle back from the public address with an
`Origin` and `Accept-Encoding: br`, and checks the bytes against the manifest,
the encoding, the cache header and CORS: what a page on another origin with
`require-corp` needs.

`site` deploys a static build (`python -m plexora.client.static_build`) to a
Pages project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BUCKET = "plexora-assets"
IMMUTABLE = "public, max-age=31536000, immutable"
TYPES = {".whl": "application/zip", ".zip": "application/zip", ".json": "application/json",
         ".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm",
         ".tar": "application/x-tar", ".txt": "text/plain; charset=utf-8"}
RELEASE = re.compile(r"^py/\d+\.\d+\.\d+/$")
#: The origin `verify` asks with (any origin is answered: the bucket's CORS is `*`).
ORIGIN = "https://viewer.plexoraapp.com"


def wrangler(path: str | None) -> list:
    path = path or os.environ.get("PLEXORA_WRANGLER")
    if path and path.endswith(".js"):
        return [shutil.which("node") or "node", path]
    found = shutil.which(path or "wrangler")
    if not found:
        raise SystemExit("wrangler not found: pass --wrangler (its bin/wrangler.js) or put it on PATH")
    return [found]


def run(command: list, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "WRANGLER_SEND_METRICS": "false"}
    return subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kwargs)


def objects(bundle: Path) -> list:
    """(key below the prefix, the file to store, its encoding or None)."""
    out = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.suffix == ".br":
            continue
        encoded = path.with_name(path.name + ".br")
        out.append((path.relative_to(bundle).as_posix(), encoded if encoded.exists() else path,
                    "br" if encoded.exists() else None))
    return out


def assets(args) -> int:
    bundle = Path(args.bundle)
    prefix = args.prefix if args.prefix.endswith("/") else args.prefix + "/"
    if not (bundle / "manifest.json").is_file():
        raise SystemExit(f"{bundle}: no manifest.json (build it with build.py)")
    command = wrangler(args.wrangler)
    if RELEASE.match(prefix):
        held = run(command + ["r2", "object", "get", f"{args.bucket}/{prefix}manifest.json", "--pipe"])
        if held.returncode == 0 and held.stdout.strip():
            raise SystemExit(f"{prefix} is a released prefix and already holds a bundle: never rewritten")
    elif not args.dev:
        raise SystemExit(f"{prefix} is not py/<X.Y.Z>/: pass --dev for a development prefix")

    def put(item):
        key, source, encoding = item
        command_line = command + ["r2", "object", "put", f"{args.bucket}/{prefix}{key}",
                                  "--file", str(source), "--cache-control", IMMUTABLE,
                                  "--content-type", TYPES.get(Path(key).suffix, "application/octet-stream")]
        if encoding:
            command_line += ["--content-encoding", encoding]
        done = run(command_line)
        return key, source.stat().st_size, done.returncode, (done.stderr or done.stdout)[-400:]

    items = objects(bundle)
    failed = []
    total = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for key, size, code, said in pool.map(put, items):
            total += size
            if code:
                failed.append({"key": key, "said": said})
            print(f"{'FAILED' if code else 'ok'} {key} {size}", flush=True)
    print(json.dumps({"bucket": args.bucket, "prefix": prefix, "objects": len(items),
                      "bytes": total, "failed": failed}, indent=1))
    return 1 if failed else 0


def verify(args) -> int:
    bundle = Path(args.bundle)
    base = args.base if args.base.endswith("/") else args.base + "/"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    files = manifest["files"]
    problems = []

    def check(item):
        key, source, encoding = item
        # Cloudflare refuses urllib's own User-Agent.
        request = urllib.request.Request(base + key, headers={"Origin": ORIGIN, "Accept-Encoding": "br",
                                                              "User-Agent": "plexora-publish"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
                headers = response.headers
        except urllib.error.URLError as error:
            return key, [f"not served: {error}"]
        found = []
        stored = (key + ".br") if encoding else key
        want = files.get(stored, {}).get("sha256") if key != "manifest.json" else None
        if want and hashlib.sha256(body).hexdigest() != want:
            found.append(f"bytes differ from {stored} ({len(body)} served)")
        if key == "manifest.json" and hashlib.sha256(body).digest() != hashlib.sha256(
                (bundle / key).read_bytes()).digest():
            found.append("manifest differs")
        if (headers.get("Content-Encoding") or None) != encoding:
            found.append(f"Content-Encoding {headers.get('Content-Encoding')!r}, want {encoding!r}")
        if headers.get("Access-Control-Allow-Origin") not in ("*", ORIGIN):
            found.append(f"Access-Control-Allow-Origin {headers.get('Access-Control-Allow-Origin')!r}")
        if "immutable" not in (headers.get("Cache-Control") or ""):
            found.append(f"Cache-Control {headers.get('Cache-Control')!r}")
        return key, found

    with ThreadPoolExecutor(max_workers=8) as pool:
        for key, found in pool.map(check, objects(bundle)):
            if found:
                problems.append({"key": key, "problems": found})
    print(json.dumps({"base": base, "checked": len(objects(bundle)), "problems": problems}, indent=1))
    return 1 if problems else 0


def site(args) -> int:
    folder = Path(args.dir)
    if not (folder / "index.html").is_file() or not (folder / "_headers").is_file():
        raise SystemExit(f"{folder}: not a static build (python -m plexora.client.static_build)")
    command = wrangler(args.wrangler) + ["pages", "deploy", str(folder), "--project-name", args.project,
                                         "--branch", args.branch, "--commit-dirty=true"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[2])
    if head.returncode == 0:
        command += ["--commit-hash", head.stdout.strip()]
    done = run(command)
    # wrangler's emoji do not fit a Windows console.
    sys.stdout.write(done.stdout[-3000:].encode("ascii", "replace").decode())
    sys.stderr.write(done.stderr[-3000:].encode("ascii", "replace").decode())
    return done.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--wrangler", default=None, help="wrangler, or its bin/wrangler.js")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("assets", help="upload a Python bundle to the bucket")
    up.add_argument("--bundle", required=True, help="build.py's py/<version>/ folder")
    up.add_argument("--prefix", required=True, help="py/<X.Y.Z>/ for a release, py/dev/<sha>/ otherwise")
    up.add_argument("--bucket", default=BUCKET)
    up.add_argument("--dev", action="store_true", help="a development prefix")
    up.add_argument("--jobs", type=int, default=6)
    up.set_defaults(fn=assets)
    check = commands.add_parser("verify", help="read a published bundle back")
    check.add_argument("--bundle", required=True)
    check.add_argument("--base", required=True, help="the public address of the prefix")
    check.set_defaults(fn=verify)
    pages = commands.add_parser("site", help="deploy a static build to Pages")
    pages.add_argument("--dir", required=True)
    pages.add_argument("--project", required=True)
    pages.add_argument("--branch", default="main")
    pages.set_defaults(fn=site)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
