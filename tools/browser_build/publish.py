"""Put the website on Cloudflare: the Python bundle on R2, the shell on Pages.

    python tools/browser_build/publish.py assets --bundle DIR --prefix py/<v>/ [--dev]
    python tools/browser_build/publish.py verify --bundle DIR --base URL
    python tools/browser_build/publish.py site --dir DIR --project NAME [--branch B]
    python tools/browser_build/publish.py prune --prefix py/dev/<sha>/
    python tools/browser_build/publish.py current [--set py/dev/<sha>/]
    python tools/browser_build/publish.py models --dir DIR
    python tools/browser_build/publish.py flags [--viewer on|preview|desktop-only]
        [--min-site-version X.Y.Z] [--message TEXT] [--model NAME=on|off] [--show]

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

`models` puts magic select's model files (`vision/sam_weights.py`
`MODEL_FILES`, from a folder that holds them -- the desktop's model folder)
into the bucket at `models/<WEB_DIR>`, the page's `--models` base: each file
checked against its pinned sha256 first, an object already there with the
pinned size left alone (a release tag's files never change), each one read
back from the public address and checked again.

`flags` sets the site's switches, `flags.json` at the bucket's root, which
the loader reads before anything else (services/shell.js `readFlags`;
`static_build --flags`): `viewer` "desktop-only" is the kill switch (the
website says it is paused and offers the desktop app; Python never loads),
"preview" marks the site, "on" is the normal state; `min_site_version` stops a
site older than it; `models.<name>.enabled` false switches a model off; an
optional `message` replaces the paused sentence. It reads the current file,
changes only what it is told, and stores it for five minutes
(`max-age=300`), so a switch reaches every visitor within five minutes.
`--show` prints the file and changes nothing.

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


#: The bucket's public address (`models` and `flags` read back through it).
PUBLIC = "https://assets.plexoraapp.com/"
#: The site's switches (`flags`): their key, how long a browser keeps them,
#: and what `viewer` may say.
FLAGS = "flags.json"
FLAGS_CACHE = "public, max-age=300"
VIEWER = ("on", "preview", "desktop-only")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def _model_pins() -> tuple:
    """(WEB_DIR, MODEL_FILES) from this checkout's vision/sam_weights.py."""
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from plexora.vision import sam_weights

    return sam_weights.WEB_DIR, sam_weights.MODEL_FILES


def _public(url: str, method: str = "GET") -> tuple:
    """(status, headers, body) of the public address; body only for GET."""
    request = urllib.request.Request(url, method=method, headers={
        "Origin": ORIGIN, "Accept-Encoding": "identity", "User-Agent": "plexora-publish"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.headers, response.read() if method == "GET" else b""
    except urllib.error.HTTPError as error:
        return error.code, error.headers, b""


def models(args) -> int:
    folder = Path(args.dir)
    web_dir, files = _model_pins()
    prefix = f"models/{web_dir}"
    base = (args.base if args.base.endswith("/") else args.base + "/") + prefix
    for name, pin in files.items():
        found = hashlib.sha256((folder / name).read_bytes()).hexdigest() if (folder / name).is_file() else None
        if found != pin["sha256"]:
            raise SystemExit(f"{folder / name}: sha256 {found}, not the pinned {pin['sha256']}")
    command = wrangler(args.wrangler)
    report = {}
    for name, pin in files.items():
        status, headers, _ = _public(base + name, "HEAD")
        if status == 200 and int(headers.get("Content-Length") or -1) == pin["bytes"]:
            report[name] = "already there"
            continue
        if status not in (404, 403):
            raise SystemExit(f"{base}{name}: {status} with {headers.get('Content-Length')} bytes; "
                             "not the pinned file and not absent -- look before replacing it")
        done = run(command + ["r2", "object", "put", f"{args.bucket}/{prefix}{name}",
                              "--file", str(folder / name), "--cache-control", IMMUTABLE,
                              "--content-type", "application/octet-stream"])
        if done.returncode:
            raise SystemExit(f"{name}: {(done.stderr or done.stdout)[-400:]}")
        report[name] = "uploaded"
    problems = []
    for name, pin in files.items():
        status, headers, body = _public(base + name)
        if status != 200 or hashlib.sha256(body).hexdigest() != pin["sha256"]:
            problems.append(f"{name}: {status}, {len(body)} bytes")
        elif headers.get("Access-Control-Allow-Origin") not in ("*", ORIGIN):
            problems.append(f"{name}: no CORS for {ORIGIN}")
    print(json.dumps({"base": base, "files": report, "problems": problems}, indent=1))
    return 1 if problems else 0


def changed_flags(current: dict, viewer=None, min_site_version=None, message=None, models=()) -> dict:
    """`current` with what was asked changed: `viewer` one of VIEWER;
    `min_site_version` X.Y.Z, or "" to drop it; `message` a sentence, or "" to
    drop it; `models` NAME=on|off."""
    out = json.loads(json.dumps(current))
    if viewer is not None:
        if viewer not in VIEWER:
            raise SystemExit(f"--viewer is one of {', '.join(VIEWER)}, not {viewer!r}")
        out["viewer"] = viewer
    if min_site_version is not None:
        if min_site_version == "":
            out.pop("min_site_version", None)
        elif not VERSION.match(min_site_version):
            raise SystemExit(f"--min-site-version is X.Y.Z, not {min_site_version!r}")
        else:
            out["min_site_version"] = min_site_version
    if message is not None:
        if message == "":
            out.pop("message", None)
        else:
            out["message"] = message
    for spec in models or ():
        name, _, state = str(spec).partition("=")
        if not name or state not in ("on", "off"):
            raise SystemExit(f"--model is NAME=on|off, not {spec!r}")
        out.setdefault("models", {})[name] = {"enabled": state == "on"}
    return out


def flags(args) -> int:
    url = (args.base if args.base.endswith("/") else args.base + "/") + FLAGS
    status, _, body = _public(url)
    if status == 200:
        try:
            current = json.loads(body)
        except ValueError:
            raise SystemExit(f"{url} is there but is not JSON: {body[:200]!r}")
        if not isinstance(current, dict):
            raise SystemExit(f"{url} is not an object: {body[:200]!r}")
    elif status in (403, 404):
        current = {}
    else:
        raise SystemExit(f"could not read {url}: {status}")
    if args.show:
        print(json.dumps(current, indent=1))
        return 0
    wanted = changed_flags(current, args.viewer, args.min_site_version, args.message, args.model)
    if wanted == current:
        print(json.dumps({"flags": current, "changed": False}, indent=1))
        return 0
    command = wrangler(args.wrangler)
    with tempfile.TemporaryDirectory(prefix="plexora-flags-") as scratch:
        record = Path(scratch) / FLAGS
        record.write_bytes(json.dumps(wanted, indent=1).encode("utf-8"))
        done = run(command + ["r2", "object", "put", f"{args.bucket}/{FLAGS}", "--file", str(record),
                              "--content-type", "application/json", "--cache-control", FLAGS_CACHE])
    if done.returncode:
        raise SystemExit(f"{FLAGS}: {(done.stderr or done.stdout)[-400:]}")
    status, headers, body = _public(url)
    read = json.loads(body) if status == 200 else None
    print(json.dumps({"flags": wanted, "changed": True, "read_back": read == wanted,
                      "cache": headers.get("Cache-Control") if status == 200 else None}, indent=1))
    return 0 if read == wanted else 1


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
    weights = commands.add_parser("models", help="put magic select's model files in the bucket")
    weights.add_argument("--dir", required=True, help="a folder holding vision/sam_weights.py's files")
    weights.add_argument("--bucket", default=BUCKET)
    weights.add_argument("--base", default=PUBLIC, help="the bucket's public address")
    weights.set_defaults(fn=models)
    switches = commands.add_parser("flags", help="the site's switches (the kill switch)")
    switches.add_argument("--viewer", default=None, choices=VIEWER)
    switches.add_argument("--min-site-version", default=None, help="X.Y.Z, or '' to drop it")
    switches.add_argument("--message", default=None, help="the paused sentence, or '' to drop it")
    switches.add_argument("--model", action="append", default=[], metavar="NAME=on|off")
    switches.add_argument("--show", action="store_true", help="print the flags, change nothing")
    switches.add_argument("--bucket", default=BUCKET)
    switches.add_argument("--base", default=PUBLIC, help="the bucket's public address")
    switches.set_defaults(fn=flags)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
