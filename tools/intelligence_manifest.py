#!/usr/bin/env python
"""Write `plexora_intelligence/guest.json`: the tools Plexora offers as a guest
in Biocognia Intelligence (the `biocognia` app).

    python tools/intelligence_manifest.py            write it
    python tools/intelligence_manifest.py --check    exit 1 when it is not what the registry says

Each tool `plexora_intelligence.GUEST_TOOLS` names is described from the
capability registry as a bridge peer sees it (`bridge_roles.describe`), its
permission always written out -- the app counts a tool without one as a
source-file write and never offers it -- with what the model is told when it
loads the tool (`description`). The version is not written: the registration
reads Plexora's own.

A tool the list names is refused when it writes a source file or deletes:
the bridge carries no approval. A Pro tool is listed with its entitlement,
and the app offers it only on a device whose licence unlocks it; Plexora
still checks every call. A tool that returns an image says so: the bridge
carries images (spatialbridge 0.2.3).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "plexora_intelligence" / "guest.json"


def refusals(capability) -> list[str]:
    """Why `capability` cannot be a guest tool; empty when it can."""
    if capability.permission in ("source_file_write", "destructive"):
        return [(f"it is a {capability.permission.replace('_', ' ')}, which needs an approval the "
                 "bridge cannot carry")]
    return []


def description(capability) -> str:
    """What the model is told when it loads `capability` as a guest tool: Plexora's
    own words for it (`plexora.mcp.tools.description_for`) less the sentence
    about external MCP access, which a bridge call does not need."""
    from plexora.licensing import manifest
    from plexora.mcp.tools import PERMISSION_NOTES

    parts = [capability.purpose, PERMISSION_NOTES[capability.permission]]
    if capability.viewer_required:
        parts.append("Needs an open Plexora viewer.")
    if capability.visual_output:
        parts.append("Returns an image plus a JSON manifest of exactly what was drawn.")
    entitlement = capability.entitlement
    if entitlement not in (None, "free"):
        if manifest.third_party(entitlement):
            parts.append("From a plugin's maker, not part of Plexora Pro; without a licence that includes "
                         "it, it answers license_required.")
        elif manifest.off_by_default(entitlement):
            parts.append("Off by default, not part of Plexora Pro; until an administrator turns it on for "
                         "the licence, it answers license_required.")
        else:
            parts.append("Part of Plexora Pro; on Free it answers license_required.")
    return " ".join(parts)


def manifest() -> dict:
    """The manifest, from the registry as it is now (the app's `manifest.build`)."""
    from biocognia_intelligence.manifest import build

    from plexora.agent import bridge_roles, registry
    from plexora_intelligence import GUEST_TOOLS

    registry.discover()
    descriptors, descriptions, problems = [], {}, []
    for name in GUEST_TOOLS:
        try:
            capability = registry.get(name)
        except Exception:  # noqa: BLE001 -- named below
            problems.append(f"{name}: no such capability")
            continue
        problems += [f"{name}: {problem}" for problem in refusals(capability)]
        descriptor = bridge_roles.describe(capability)
        descriptor["permission"] = capability.permission
        descriptors.append(descriptor)
        descriptions[name] = description(capability)
    if problems:
        raise ValueError("GUEST_TOOLS names tools a guest cannot offer:\n  " + "\n  ".join(problems))
    return build("plexora", "Plexora", descriptors, descriptions=descriptions)


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
