"""Plexora's own agent harness: AI features that run inside Plexora, on the
user's machine, with no external agent.

The harness drives the same capability registry and decision sessions an
external agent (Claude Code, Codex) drives over MCP, but the loop is
deterministic Python: each decision packet becomes ONE structured model call,
validated locally before it is answered. Model calls go to the Plexora AI
gateway (`/v1/ai/*` on the licence service), which holds the provider key,
meters the provider's usage and bills Plexora AI credits; nothing about a
user's data is stored there.

    gateway.py       token from the licence certificate; streamed calls; runs
    wire.py          the request envelope, canonical JSON, usage
    prefix.py        the byte-stable cached prefix a worker starts from
    cache_plan.py    prefix fingerprints and the cache-hit monitor
    schema.py        answer models -> structured-output JSON schema
    decision.py      the decision loop, gating and QC (rolling workers)
    capabilities.py  ai.run_session / run_status / run_control / balance (in-app runs)
    orchestrator.py  task graph, parallel scheduler, shared blackboard
    trace.py         the local trace: runs, model calls, cache verdicts, tool calls
    runner.py        the conversational agent: a tool loop, sub-agents, compaction
    tools.py         the registry as deferred tools (load_tool), executed through the cache
    approvals.py     source-file writes and deletes wait for the user (control.json)
    toolcache.py     read results remembered per project revision (Layer 3)
    offload.py       large tool results as stubs, read back in slices
    conversations.py conversations on disk, and the service that runs their turns
    chat_capabilities.py  ai.chat_* (the HTTP routes and the CLI call these)

`plexora ai run gating|qc <project>` and `plexora ai chat` are the command-line
entries, `/ai/v1` the viewer's; `--dev` uses the
gateway's dev route (internal testing accounts, billed at provider cost).
"""

from __future__ import annotations
