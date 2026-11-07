"""Put the website on Cloudflare: the Python bundle on R2, the shell on Pages.

    python tools/browser_build/publish.py assets --bundle DIR --prefix py/<v>/ [--dev]
    python tools/browser_build/publish.py verify --bundle DIR --base URL
    python tools/browser_build/publish.py site --dir DIR --project NAME [--branch B]
    python tools/browser_build/publish.py prune --prefix py/dev/<sha>/
    python tools/browser_build/publish.py current [--set py/dev/<sha>/]

Everything goes through wrangler, logged in as the account that holds the
zone, or with `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in CI. No
token is read or passed here. `--wrangler` (or `PLEXORA_WRANGLER`) says which:
its `bin/wrangler.js` (run with node), a command line that fetches it --
`npx --yes wrangler@3.114.17`, which must name an exact version, never
`latest` or a range -- or an executable; `wrangler` on PATH when neither is
given. Wrangler 3 is assumed: wrangler 4's `r2 object` commands write to local
storage unless told `--remote`, which 3 does not accept (`verify` would fail).

`assets` uploads a bundle that `build.py` wrote (`<out>/py/<version>/`) to the
bucket under `--prefix`, one object per file. A file with a `.br` beside it is
stored as those brotli bytes under its own name with `Content-Encoding: br`, so
the browser inflates it natively; every object is `immutable` for a year, since
a prefix is never rewritten. `manifest.json` goes last, once every other object
is in: a prefix that holds a manifest holds the whole bundle, and an upload
that stopped can run again. A release prefix (`py/<X.Y.Z>/`) that already
holds a manifest is refused -- unless it is this very bundle's manifest, when
there is nothing to do (a release job run again); `--dev` prefixes
(`py/dev/<sha>/`) are not.

`prune` deletes a development prefix: every object its own manifest lists.
A release prefix is never deleted here.

`verify` reads every file of the bundle back from the public address with an
`Origin` and `Accept-Encoding: br`, and checks the bytes against the manifest,
the encoding, the cache header and CORS: what a page on another origin with
`require-corp` needs.

`site` deploys a static build (`python -m plexora.client.static_build`) to a
Pages project.

`current` prints the development prefix the preview site loads (recorded in
the bucket at `py/dev/current.json`), or nothing; `--set` records a new one.
The preview deploy (web.yml) reads it first, deploys, records its own and
prunes the one before, so the bucket holds one development bundle at a time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
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
#: What a runner that fetches wrangler (`npx`) must be told to fetch: one version.
PINNED = re.compile(r"^wrangler@\d+\.\d+\.\d+$")
#: What wrangler says when an object is not there (and nothing else went wrong).
ABSENT = "The specified key does not exist"
#: Where the preview records the development prefix it loads.
CURRENT = "py/dev/current.json"
DEV = re.compile(r"^py/dev/[A-Za-z0-9._-]+/$")


def wrangler(spec: str | None) -> list:
    """The command that runs wrangler: `[node, wrangler.js]`, a runner's
    command line (`npx --yes wrangler@X.Y.Z`), or an executable."""
    spec = (spec or os.environ.get("PLEXORA_WRANGLER") or "wrangler").strip()
    if spec.endswith(".js") and (" " not in spec or Path(spec).is_file()):
        return [shutil.which("node") or "node", spec]
    if " " in spec and not Path(spec).exists():
        words = [word.strip('"') for word in shlex.split(spec, posix=os.name != "nt")]
    else:
        words = [spec]
    found = shutil.which(words[0])
    if not found:
        raise SystemExit(f"{words[0]} not found: pass --wrangler (its bin/wrangler.js, or "
                         f"`npx --yes wrangler@X.Y.Z`) or put wrangler on PATH")
    if Path(words[0]).stem.lower() == "npx":
        named = [word for word in words[1:] if word.startswith("wrangler")]
        if len(named) != 1 or not PINNED.match(named[0]):
            raise SystemExit(f"{spec!r}: npx must fetch one exact version (wrangler@X.Y.Z), "
                             "never `latest` or a range")
    return [found, *words[1:]]


def held_manifest(command: list, bucket: str, prefix: str) -> dict | None:
    """The manifest a prefix of the bucket holds, or None when it holds none.
    Anything else wrangler says (no login, no network) stops here: an upload
    must never take "could not ask" for "not there"."""
    held = run(command + ["r2", "object", "get", f"{bucket}/{prefix}manifest.json", "--pipe"])
    said = f"{held.stdout or ''}{held.stderr or ''}"
    if held.returncode != 0:
        if ABSENT in said:
            return None
        raise SystemExit(f"could not tell whether {prefix} holds a bundle: {said[-400:]}")
    try:
        return json.loads(held.stdout)
    except ValueError:
        raise SystemExit(f"{prefix}manifest.json is there but is not JSON: {held.stdout[:200]!r}")


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
        held = held_manifest(command, args.bucket, prefix)
        if held is not None:
            ours = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            if held == ours:
                print(json.dumps({"bucket": args.bucket, "prefix": prefix, "objects": 0,
                                  "already": "this bundle is published there; nothing to do"}))
                return 0
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

    def record(key, size, code, said):
        nonlocal total
        total += size
        if code:
            failed.append({"key": key, "said": said})
        print(f"{'FAILED' if code else 'ok'} {key} {size}", flush=True)

    # The manifest last, and only once everything else is in: it is what
    # says the prefix holds a bundle.
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for done in pool.map(put, [item for item in items if item[0] != "manifest.json"]):
            record(*done)
    manifest = "not uploaded: an object failed (run again)"
    if not failed:
        for item in items:
            if item[0] == "manifest.json":
                record(*put(item))
        manifest = "uploaded" if not failed else "failed (run again)"
    print(json.dumps({"bucket": args.bucket, "prefix": prefix, "objects": len(items),
                      "bytes": total, "failed": failed, "manifest": manifest}, indent=1))
    return 1 if failed else 0


def verify(args) -> int:
    bundle = Path(args.bundle)
    base = args.base if args.base.endswith("/") else args.base + "/"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    files = manifest["files"]
    problems = []

    def check(item):
        key, source, encoding = item
        # Cloudflare refuses urllib's own User-Agent. An object stored as it is
        # is asked for as it is: on a custom domain Cloudflare compresses JSON
        # on the fly for a client that takes brotli (the browser decodes it).
        request = urllib.request.Request(base + key, headers={
            "Origin": ORIGIN, "Accept-Encoding": "br" if encoding else "identity",
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


def test_build(folder: Path) -> str:
    """Why a static build must not be published, or "": a build that trusts a
    throwaway licence key (static_build --licence-test-key) would let anyone
    holding that key's private half sign a Pro licence for the site."""
    try:
        shell = json.loads((folder / "client" / "shell.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "it has no readable client/shell.json"
    if (shell.get("licence") or {}).get("test_keys"):
        return "it trusts a test licence key"
    return ""


def site(args) -> int:
    folder = Path(args.dir)
    if not (folder / "index.html").is_file() or not (folder / "_headers").is_file():
        raise SystemExit(f"{folder}: not a static build (python -m plexora.client.static_build)")
    refused = test_build(folder)
    if refused:
        raise SystemExit(f"{folder}: {refused}; build it again without --licence-test-key")
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


def prune(args) -> int:
    prefix = args.prefix if args.prefix.endswith("/") else args.prefix + "/"
    if not prefix.startswith("py/dev/") or prefix.count("/") != 3:
        raise SystemExit(f"{prefix}: only a development prefix (py/dev/<sha>/) is pruned")
    command = wrangler(args.wrangler)
    held = held_manifest(command, args.bucket, prefix)
    if not held or "files" not in held:
        raise SystemExit(f"{prefix}: no manifest there")
    files = held["files"]
    keys = sorted({name[:-3] if name.endswith(".br") else name for name in files} - {"manifest.json"})
    keys.append("manifest.json")  # last: a prune that stops can be run again

    def delete(key):
        return key, run(command + ["r2", "object", "delete", f"{args.bucket}/{prefix}{key}"]).returncode

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        failed = [key for key, code in pool.map(delete, keys[:-1]) if code]
    if not failed:
        failed = [key for key, code in [delete(keys[-1])] if code]
    print(json.dumps({"prefix": prefix, "deleted": len(keys) - len(failed), "failed": failed}, indent=1))
    return 1 if failed else 0


def current(args) -> int:
    """The preview's development prefix: printed, or recorded with --set."""
    command = wrangler(args.wrangler)
    key = f"{args.bucket}/{CURRENT}"
    if args.set:
        prefix = args.set if args.set.endswith("/") else args.set + "/"
        if not DEV.match(prefix):
            raise SystemExit(f"{prefix}: the preview loads a development prefix (py/dev/<sha>/)")
        with tempfile.TemporaryDirectory(prefix="plexora-current-") as scratch:
            record = Path(scratch) / "current.json"
            record.write_text(json.dumps({"prefix": prefix}), encoding="utf-8")
            done = run(command + ["r2", "object", "put", key, "--file", str(record),
                                  "--content-type", "application/json", "--cache-control", "no-store"])
        if done.returncode:
            sys.stderr.write((done.stderr or done.stdout)[-400:])
        return done.returncode
    held = run(command + ["r2", "object", "get", key, "--pipe"])
    if held.returncode != 0:
        if ABSENT in f"{held.stdout or ''}{held.stderr or ''}":
            return 0
        raise SystemExit(f"could not read {CURRENT}: {(held.stderr or held.stdout)[-400:]}")
    try:
        prefix = json.loads(held.stdout).get("prefix") or ""
    except (ValueError, AttributeError):
        raise SystemExit(f"{CURRENT} is not what --set writes: {held.stdout[:200]!r}")
    if prefix and not DEV.match(prefix):
        raise SystemExit(f"{CURRENT} names {prefix!r}, not a development prefix: never pruned from here")
    print(prefix)
    return 0


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
    drop = commands.add_parser("prune", help="delete a development prefix from the bucket")
    drop.add_argument("--prefix", required=True)
    drop.add_argument("--bucket", default=BUCKET)
    drop.add_argument("--jobs", type=int, default=6)
    drop.set_defaults(fn=prune)
    here = commands.add_parser("current", help="the development prefix the preview loads")
    here.add_argument("--set", default=None, metavar="PREFIX", help="record py/dev/<sha>/ as it")
    here.add_argument("--bucket", default=BUCKET)
    here.set_defaults(fn=current)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
