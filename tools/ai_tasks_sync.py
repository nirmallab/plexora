#!/usr/bin/env python
"""Mirror the AI task registry into the licence Worker.

    python tools/ai_tasks_sync.py generate   # write licensing/src/ai/tasks.json
    python tools/ai_tasks_sync.py check      # fail if it is stale

plexora/ai/tasks.yaml is the one list of AI tasks. The Worker cannot read the
package, so it is given a JSON copy at build time; tests/test_ai_tasks.py runs
`check`, so an edited registry that was not regenerated fails the suite.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET = ROOT / "licensing" / "src" / "ai" / "tasks.json"


def rendered() -> str:
    from plexora.ai import tasks

    return tasks.to_json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args(argv)
    text = rendered()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
    if args.command == "check":
        if current != text:
            print(f"{TARGET.relative_to(ROOT)} is stale: run python tools/ai_tasks_sync.py generate", file=sys.stderr)
            return 1
        print("tasks.json is current")
        return 0
    if current == text:
        print("tasks.json unchanged")
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
