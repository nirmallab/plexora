#!/usr/bin/env python
"""Upload Plexora's product manifest and AI task registry to the BioCognia platform.

    python tools/bioc_sync.py                # upload both
    python tools/bioc_sync.py --check        # diff both against the platform; exit 1 on drift
    python tools/bioc_sync.py --print        # print what would be uploaded, touch nothing

The editable sources stay in this repository (invariant 15):

- the manifest is `plexora/licensing/manifest.py` (`product_manifest()`), sent
  to `PUT {core}/admin/api/products/plexora/manifest`;
- the task registry is `plexora/ai/tasks.yaml` (`tasks.fragment()`), sent to
  `PUT {ai}/admin/api/ai/registry/plexora`.

This tool is the only writer of the platform's copies; admin shows them
read-only. Both requests carry `Authorization: Bearer $BIOC_ADMIN_TOKEN`
(the gateway's `$BIOC_AI_ADMIN_TOKEN` instead, when set).
`BIOC_CORE_URL` and `BIOC_AI_URL` (or `--core` / `--ai`) say where the two
Workers are; staging is whatever they point at.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRODUCT = "plexora"
#: Both Workers serve their admin API under admin.biocognia.com (/admin/api and
#: /admin/api/ai); staging is two workers.dev hosts, given by the variables.
# Not admin.biocognia.com: Cloudflare Access guards that host for people, and a
# tool holding the break-glass token reaches the same routes on these.
DEFAULT_CORE = "https://api.biocognia.com"
DEFAULT_AI = "https://ai.biocognia.com"
TOKEN_ENV = "BIOC_ADMIN_TOKEN"


def manifest() -> dict:
    from biocognia import validate_manifest

    from plexora.licensing import manifest as source

    data = source.product_manifest()
    problems = validate_manifest(data)
    if problems:
        raise SystemExit("The product manifest is invalid: " + "; ".join(problems))
    return data


def fragment() -> dict:
    from plexora.ai import tasks

    return tasks.fragment()


def canonical(value) -> str:
    """The platform's canonical JSON (sorted keys, no whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fragment_hash(data: dict) -> str:
    """The hash the gateway stores for a fragment: sha256 of canonical `{modules}`."""
    return "sha256:" + hashlib.sha256(canonical({"modules": data["modules"]}).encode()).hexdigest()


def _request(method: str, url: str, token: str, body=None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": f"Bearer {token}", "User-Agent": "plexora-bioc-sync"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise SystemExit(f"{method} {url}: HTTP {exc.code} {detail}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"{method} {url}: {exc}") from None


def _diff(label: str, ours, theirs) -> list[str]:
    if canonical(ours) == canonical(theirs):
        return []
    import difflib

    left = json.dumps(theirs, indent=2, sort_keys=True).splitlines()
    right = json.dumps(ours, indent=2, sort_keys=True).splitlines()
    return [f"{label} differs from the platform's copy:",
            *difflib.unified_diff(left, right, "platform", "plexora", lineterm="")]


def check(core: str, ai: str, token: str, ai_token: str | None = None) -> int:
    problems: list[str] = []
    product = _request("GET", f"{core}/admin/api/products/{PRODUCT}", token)
    problems += _diff("The manifest", manifest(), product.get("manifest"))
    registry = _request("GET", f"{ai}/admin/api/ai/registry/{PRODUCT}", ai_token or token)
    ours = fragment()
    if registry.get("hash") != fragment_hash(ours):
        problems.append(f"The task registry differs from the gateway's (ours {fragment_hash(ours)}, "
                        f"the gateway's {registry.get('hash')}).")
        theirs = sorted(task.get("id", "") for task in registry.get("tasks") or [])
        mine = sorted(f"{PRODUCT}.{module}.{name}" for module, spec in ours["modules"].items()
                      for name in spec["tasks"])
        problems += [f"  only here: {task}" for task in sorted(set(mine) - set(theirs))]
        problems += [f"  only on the gateway: {task}" for task in sorted(set(theirs) - set(mine))]
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print("Run python tools/bioc_sync.py to upload.", file=sys.stderr)
        return 1
    print("The platform's manifest and task registry match this repository.")
    return 0


def upload(core: str, ai: str, token: str, ai_token: str | None = None) -> int:
    answer = _request("PUT", f"{core}/admin/api/products/{PRODUCT}/manifest", token, manifest())
    print(f"manifest: {'uploaded' if answer.get('changed') else 'unchanged'} ({answer.get('hash')})")
    answer = _request("PUT", f"{ai}/admin/api/ai/registry/{PRODUCT}", ai_token or token, fragment())
    print(f"tasks: {'uploaded' if answer.get('changed') else 'unchanged'} ({answer.get('hash')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="diff against the platform; upload nothing")
    parser.add_argument("--print", action="store_true", dest="show", help="print both payloads; no network")
    parser.add_argument("--core", default=os.environ.get("BIOC_CORE_URL") or DEFAULT_CORE)
    parser.add_argument("--ai", default=os.environ.get("BIOC_AI_URL") or DEFAULT_AI)
    args = parser.parse_args(argv)
    if args.show:
        print(json.dumps({"manifest": manifest(), "tasks": fragment()}, indent=2, ensure_ascii=False))
        return 0
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        print(f"Set {TOKEN_ENV} (the platform's admin token).", file=sys.stderr)
        return 2
    core, ai = args.core.rstrip("/"), args.ai.rstrip("/")
    # Each Worker has its own break-glass token; the gateway's, when set.
    ai_token = (os.environ.get("BIOC_AI_ADMIN_TOKEN") or "").strip() or None
    return check(core, ai, token, ai_token) if args.check else upload(core, ai, token, ai_token)


if __name__ == "__main__":
    sys.exit(main())
