"""Write the telemetry allowlist the Worker validates against, from the client's.

    python scripts/sync_telemetry_vectors.py          # write both files
    python scripts/sync_telemetry_vectors.py --check  # exit 1 on drift

`plexora/telemetry/schema.py` is canonical. This writes
`backend/vectors/plexora-vectors.json` (every event, field, type and mode) and
`backend/test/fixtures/sample-batch.json` (one of every event, which the
Worker's fixture test must accept), so a schema change cannot reach one end
without the other.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plexora.telemetry import sample, schema  # noqa: E402

VECTORS = ROOT / "backend" / "vectors" / "plexora-vectors.json"
FIXTURE = ROOT / sample.FIXTURE


def rendered():
    return {
        VECTORS: json.dumps(schema.vectors(), indent=1, sort_keys=True) + "\n",
        FIXTURE: json.dumps(sample.body(), indent=1, sort_keys=True) + "\n",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="Fail if the files on disk differ from the schema.")
    args = parser.parse_args(argv)
    drift = []
    for path, text in rendered().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        if args.check:
            drift.append(path.relative_to(ROOT))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    if drift:
        print("telemetry vectors are out of date; run scripts/sync_telemetry_vectors.py:")
        for path in drift:
            print(f"  {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
