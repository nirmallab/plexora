"""Plexora's own agent harness: AI features that run inside Plexora, on the
user's machine, with no external agent.

The harness drives the same capability registry and decision sessions an
external agent (Claude Code, Codex) drives over MCP, but the loop is
deterministic Python: each decision packet becomes ONE structured model call,
validated locally before it is answered. Model calls go to the BioCognia AI
gateway (`ai.biocognia.com`), which holds the provider keys, meters the
provider's usage and bills the organisation's AI credits; nothing about a
user's data is stored there.

The runtime underneath is shared with SCIMAP Pro: `spatialagents` holds the
streamed model call and its wire types, the cache-hit monitor, the scheduler,
the trace, the conversational tool loop with its approvals, cache, offloading
and stores, and `biocognia.gateway` the token and the transport. What is here
is Plexora:

    gateway.py       the model call on this device's token, naming Plexora's tasks
    prefix.py        the byte-stable cached prefixes: gating and QC workers, conversations
    schema.py        Plexora's answer models -> structured-output JSON schema, per kind and task
    decision.py      the decision loop, gating and QC (rolling workers)
    capabilities.py  ai.run_session / run_status / run_control / balance (in-app runs)
    backend.py       the capability registry as the conversation's tool backend
    conversations.py Plexora AI conversations: spatialagents' chat bound to Plexora, and the service
    chat_capabilities.py  ai.chat_* (the HTTP routes and the CLI call these)
    toolcache.py     what the registry may cache, where, under which version
    trace.py         the trace at <data_root>/.agent/ai/trace.sqlite
    cache_plan.py    the cache monitor, logging to plexora.ai.harness
    cli.py           the `plexora ai` commands
    route_bench.py   the routing bench for a gateway route

`plexora ai run gating|qc <project>` and `plexora ai chat` are the command-line
entries, `/ai/v1` the viewer's; `--dev` uses the
gateway's dev route (internal testing accounts, billed at provider cost).
"""

from __future__ import annotations
