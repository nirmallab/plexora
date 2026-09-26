"""Server-driven decision sessions: the server does the work, the agent decides.

Plexora has no model of its own and the MCP clients it serves cannot be called
back, so a long workflow cannot be a loop the server runs with a model inside
it. It is a session instead: state on disk, a deterministic engine that does
every step code can do, and ONE outstanding decision packet at a time that an
agent fetches (`next`) and answers (`answer`) with a typed reply. The engine
then moves on by itself. That makes the loop cheap (the agent sees only
decisions), auditable (every packet and answer is logged), resumable (a new
conversation, or a restarted MCP server, picks up where the last left off) and
vendor-neutral (any client that can call two tools can drive it).

Generic: `store` (the on-disk session), `budget` (what a session may spend),
`mirror` (showing an open viewer what the session is looking at). Workflows
supply the engine; automatic gating's is `plugins/gating/server/autogate/engine.py`.
"""
