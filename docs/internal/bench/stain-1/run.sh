#!/bin/bash
# run.sh <dir>: read until every channel settles.
set -u
W=~/worktrees/plexora-qcbench; S=/private/tmp/claude-503/-Users-aj-softwares-biocognia-scimappro/79ba1f7e-12c7-4c48-8acc-8d76504f4c8c/scratchpad/stainbench
export PYTHONPATH=$W PLEXORA_DATA_PATH=$S/data PLEXORA_LIBRARY_PATH=$S/projects BIOCOGNIA_MARKER_GRAPH=$S/graph
for round in 1 2 3 4; do
  $W/.venv/bin/python -m plexora.cli ai stain-bench --pending "$1" > "$1/pending.txt"
  n=$(wc -l < "$1/pending.txt"); echo "round $round: $n due"; [ "$n" -eq 0 ] && break
  xargs -P 6 -L 1 bash $S/read_one.sh < "$1/pending.txt"
done
$W/.venv/bin/python -m plexora.cli ai stain-bench --score "$1"
