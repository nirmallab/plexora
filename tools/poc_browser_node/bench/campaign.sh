#!/usr/bin/env bash
# The whole campaign, one POC after another (never two at once: they would
# measure each other), into docs/internal/bench/browser_node/.
#   bash bench/campaign.sh [OUT]
set -u
cd "$(dirname "$0")/.."
OUT="${1:-../../docs/internal/bench/browser_node}"
node build.mjs > /dev/null
run() {
    echo "== $* $(date +%H:%M:%S)"
    node bench/run.mjs --out "$OUT" "$@" 2>&1 | grep -E "^wrote|Error|error" | tail -3
}
run --poc P0 --fixture S
run --poc P0 --fixture M
run --poc P1 --fixture M
run --poc P1V --fixture M
run --poc P1R --fixture none
run --poc P2 --fixture M
run --poc P2 --fixture L
run --poc P2B --fixture S
run --poc P2B --fixture M
run --poc P3 --fixture S
run --poc P3 --fixture M
run --poc P3 --fixture L
run --poc P3 --fixture M --label parquet --params '{"store":"parquet"}'
run --poc P3 --fixture L --label parquet --params '{"store":"parquet"}'
run --poc P3B --fixture L --params '{"workers":[8]}'
run --poc P5 --fixture M
run --poc P5LNA --fixture S
node bench/p0_audit.mjs --out "$OUT/p0_audit-2026-11-05.json" > /dev/null
echo "== done $(date +%H:%M:%S)"
