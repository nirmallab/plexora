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
    decision.py      the gating decision loop (rolling workers)
    orchestrator.py  task graph, parallel scheduler, shared blackboard
    trace.py         the local trace: runs, model calls, cache verdicts

`plexora ai run gating <project>` is the command-line entry; `--dev` uses the
gateway's dev route (internal testing accounts, billed at provider cost).
"""

from __future__ import annotations
