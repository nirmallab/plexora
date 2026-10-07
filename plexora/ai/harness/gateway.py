"""Talking to the BioCognia AI gateway: Plexora's model call.

Two shared layers do the work, and this module only binds them to Plexora:

- `biocognia.gateway` is the transport: the 30-minute `BIOCAI1` token
  obtained against this device's certificate (`TokenSource(LICENSING)`), the
  gateway URL, one token refresh, server-sent event parsing, and the account
  endpoints (balance, usage, pricing, runs);
- `spatialagent.gateway` is the harness's call: `messages()` -- one streamed
  call, its retries, and reading Anthropic-shaped events between
  `biocognia.accepted` and `biocognia.usage` into a `ModelResponse` -- over the
  wire types in `spatialagent.wire`.

What is Plexora's is the token source (this device's licence) and the task
names on the wire, which come from `plexora/ai/tasks.py` (`tasks.wire_id`:
`plexora.<module>.<task>`).

`BIOCOGNIA_AI_TOKEN` supplies a token directly (a CI job, a test);
`BIOCOGNIA_AI_GATEWAY` overrides the gateway's address; `BIOCOGNIA_AI_DEV=1`
(or `dev=True`) uses the dev route, which only internal testing accounts may
call and which bills at provider cost. `BIOCOGNIA_OFFLINE=1` refuses before
any socket opens.

Retries: a refusal that happened before anything reached a provider (rate
limit, provider outage, unreachable gateway) is retried with the SAME
idempotency key, which the gateway frees in exactly those cases; an expired
token is refreshed once. Credit, cap and envelope refusals are never retried:
they are surfaced so the session can pause (`spatialagent.gateway.PAUSE_CODES`).
"""

from __future__ import annotations

import time

from biocognia import gateway as _shared
from biocognia.gateway import ENV_DEV, ENV_GATEWAY, ENV_TOKEN, RETRYABLE, GatewayError, dev_default, iter_sse
from spatialagent import gateway as _agent
from spatialagent.gateway import CALL_TIMEOUT, CIRCUIT_ATTEMPTS

__all__ = ["CALL_TIMEOUT", "CIRCUIT_ATTEMPTS", "ENV_DEV", "ENV_GATEWAY", "ENV_TOKEN", "RETRYABLE",
           "GatewayClient", "GatewayError", "TokenSource", "dev_default", "gateway_url", "iter_sse",
           "token_source"]


def TokenSource(token: str | None = None) -> _shared.TokenSource:  # noqa: N802 - a constructor
    """The gateway token for this device: `biocognia.gateway.TokenSource(LICENSING)`.
    `token` (or `BIOCOGNIA_AI_TOKEN`) short-circuits it."""
    from plexora.licensing import LICENSING

    return _shared.TokenSource(LICENSING, token)


token_source = TokenSource


def gateway_url() -> str:
    return _shared.gateway_url()


class GatewayClient(_agent.GatewayClient):
    """spatialagent's streamed call, on this device's token, naming Plexora's tasks."""

    def __init__(self, base_url: str | None = None, *, tokens: _shared.TokenSource | None = None,
                 dev: bool | None = None, max_attempts: int = 6, sleep=time.sleep):
        super().__init__(tokens or TokenSource(), base_url or None, dev=dev, max_attempts=max_attempts,
                         sleep=sleep)

    def wire_task(self, task: str | None, *, module: str | None = None) -> str:
        """`gating.planning` as the gateway knows it: `plexora.gating.planning`
        (`tasks.wire_id`, the one place Plexora names its tasks)."""
        from plexora.ai import tasks

        return tasks.wire_id(task, module=module)
