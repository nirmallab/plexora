#!/usr/bin/env python
"""Write `plexora_intelligence/guest.json`: the tools Plexora offers as a guest
in Biocognia Intelligence (the `biocognia` app).

    python tools/intelligence_manifest.py            write it
    python tools/intelligence_manifest.py --check    exit 1 when it is not what the registry says

Each tool `plexora_intelligence.GUEST_TOOLS` names is described from the
capability registry as a bridge peer sees it (`bridge_roles.describe`), its
permission always written out -- the app counts a tool without one as a
source-file write and never offers it -- with what the model is told when it
loads the tool (`plexora.mcp.tools.description_for`, Plexora's own words for
it). The version is not written: the registration reads Plexora's own.

A tool the list names is refused when a call through the bridge could not run
it for everyone: a source-file write or a delete (the bridge carries no
approval), an image in the result (an image does not cross the bridge yet),
or an entitlement beyond Free.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "plexora_intelligence" / "guest.json"


def refusals(capability) -> list[str]:
    """Why `capability` cannot be a guest tool; empty when it can."""
    problems = []
    if capability.permission in ("source_file_write", "destructive"):
        problems.append(f"it is a {capability.permission.replace('_', ' ')}, which needs an approval the "
                        "bridge cannot carry")
    if capability.visual_output:
        problems.append("it returns an image, which does not cross the bridge yet")
    if capability.entitlement not in (None, "free"):
        problems.append(f"it needs the {capability.entitlement} entitlement, beyond Free")
    return problems


def manifest() -> dict:
    """The manifest, from the registry as it is now."""
    from plexora.agent import bridge_roles, registry
    from plexora.mcp.tools import description_for

    from plexora_intelligence import GUEST_TOOLS

    registry.discover()
    tools, problems = [], []
    for name in GUEST_TOOLS:
        try:
            capability = registry.get(name)
        except Exception:  # noqa: BLE001 -- named below
            problems.append(f"{name}: no such capability")
            continue
        problems += [f"{name}: {problem}" for problem in refusals(capability)]
        descriptor = bridge_roles.describe(capability)
        descriptor["permission"] = capability.permission
        tools.append({"descriptor": descriptor, "description": description_for(capability)})
    if problems:
        raise ValueError("GUEST_TOOLS names tools a guest cannot offer:\n  " + "\n  ".join(problems))
    return {"contract": 1, "provider": "plexora", "name": "Plexora", "tools": tools}


def rendered() -> str:
    return json.dumps(manifest(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    text = rendered()
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
            print(f"{TARGET.relative_to(ROOT)} is stale: run python tools/intelligence_manifest.py",
                  file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}: {len(json.loads(text)['tools'])} tools, {len(text)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
