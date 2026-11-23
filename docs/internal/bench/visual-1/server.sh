#!/bin/bash
# server.sh <root> <port>: a Plexora MCP server on the bench branch's code, in root's isolated data.
R=$1; PORT=$2; W=$HOME/worktrees/plexora-qcbench
export PYTHONPATH=$W PLEXORA_DATA_PATH=$R/data PLEXORA_LIBRARY_PATH=$R/projects PLEXORA_SHARED_PATH="" BIOCOGNIA_MARKER_GRAPH=$R/graph PLEXORA_TESTING=1
cd "$R"
nohup $W/.venv/bin/python -m plexora.cli mcp serve --transport http --port $PORT --no-auth > "$R/server.log" 2>&1 < /dev/null &
for _ in $(seq 1 120); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PORT/mcp 2>/dev/null)
  case "$code" in 2*|3*|4*) echo "up $PORT"; exit 0;; esac; sleep 1
done; echo "no server"; exit 1
