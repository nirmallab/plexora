"""A stand-in BioCognia AI gateway for harness tests: no network, no model.

`FakeGateway` is spatialagents' (`spatialagents.testing.FakeGateway`): a real
HTTP server on a loopback port that speaks the gateway's wire --
`/v1/ai/messages` takes only `task`, `context`, `request` and `model`, as the
real one does, and streams Anthropic-shaped events between
`biocognia.accepted` and `biocognia.usage`, and `/v1/ai/runs` quotes and
closes runs. The "model" is a brain function given the decision packet the
harness sent (and the whole request body); it answers with JSON or text, or
with a `Reply` that streams `tool_use` blocks the way a provider does, which
is what the conversational agent is tested with. The usage is a prompt-cache
emulator, so cache verdicts and prefix stability can be tested.

Plexora's binds it: every task must be `plexora.<module>.<task>`, and runs are
priced as the real gateway's catalogue prices Plexora's features.
"""

from __future__ import annotations

from spatialagents.testing import (CHARS_PER_TOKEN, IMAGE_TOKENS, UNIT, WIRE_KEYS, WIRE_TASK, Reply, _marked,
                                  _tokens, _unmarked, last_packet, last_user_text, result_text, tool_results)
from spatialagents.testing import FakeGateway as _FakeGateway

__all__ = ["CHARS_PER_TOKEN", "IMAGE_TOKENS", "PRICES", "UNIT", "WIRE_KEYS", "WIRE_TASK", "FakeGateway", "Reply",
           "_marked", "_tokens", "_unmarked", "last_packet", "last_user_text", "result_text", "tool_results"]

#: The run prices the real gateway's catalogue (workers/ai/src/ai/catalog.ts in the platform) quotes.
PRICES = {"gating": ("marker", 25), "qc": ("channel", 12)}


class FakeGateway(_FakeGateway):
    PRODUCT = "plexora"
    PRICES = PRICES
