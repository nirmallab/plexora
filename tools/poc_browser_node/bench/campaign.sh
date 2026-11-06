#!/usr/bin/env bash
# The whole campaign, one POC after another (never two at once: they would
# measure each other), into docs/internal/bench/browser_node/.
#   bash bench/campaign.sh [OUT]
#   HEADED=1 bash bench/campaign.sh   # also P5LNAH, which opens a visible Chrome window
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
# P2b: the parallel builder, and the worker-count sweep.
run --poc P2B --fixture S
run --poc P2B --fixture M
run --poc P2B --fixture M --label w4 --params '{"workers":4}'
run --poc P2B --fixture M --label w8 --params '{"workers":8}'
# P3: the JavaScript parser (default), and what it replaced.
run --poc P3 --fixture S
run --poc P3 --fixture M
run --poc P3 --fixture L
run --poc P3 --fixture L --label duckdb --params '{"parser":"duckdb","parallel":1}'
run --poc P3 --fixture L --label duckdb-par4 --params '{"parser":"duckdb","parallel":4}'
run --poc P3 --fixture M --label parquet --params '{"store":"parquet"}'
run --poc P3 --fixture L --label parquet --params '{"store":"parquet"}'
run --poc P3B --fixture L --params '{"workers":[4,12]}'
run --poc P4 --fixture S
run --poc P4 --fixture L
run --poc P5 --fixture M
run --poc P5LNA --fixture S
if [ "${HEADED:-0}" = 1 ]; then run --poc P5LNAH --fixture S; fi
run --poc P6 --fixture M
run --poc P7 --fixture S
run --poc P7 --fixture M
run --poc P7 --fixture M --label w8 --params '{"layouts":["strips"],"workers":8}'
run --poc P8 --fixture S
node bench/p0_audit.mjs --out "$OUT/p0_audit-2026-11-05.json" > /dev/null
echo "== done $(date +%H:%M:%S)"
