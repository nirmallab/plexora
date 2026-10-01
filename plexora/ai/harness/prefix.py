"""The cached prefix every gating worker starts from.

Identical bytes for every worker, every session and every user of one
Plexora build: an identity paragraph, then the session's reading guide as
canonical JSON, with the only explicit cache breakpoint on the last block.
Nothing volatile may enter it -- no session, packet, project, image or
account id, no time, no biology (that travels in each packet's evidence).
`tests/test_ai_harness.py` fails if a timestamp sneaks in.

The guide is the one `gating_session_start(reading="once")` would hand an
external agent: what each packet kind means and how to answer it. A worker
never re-reads it; it is in the prefix, read from cache at a tenth of the
input price after the first worker writes it.
"""

from __future__ import annotations

from plexora.ai.harness.wire import canonical, text_block

IDENTITY = (
    "You are a Plexora gating worker. Plexora is an image viewer for multiplexed tissue images; "
    "it is gating markers (choosing the intensity threshold above which a cell is positive) "
    "and asks you one decision at a time. Each user turn is one decision packet: up to two "
    "images and a JSON packet with its kind, the units it concerns and its evidence. Answer "
    "with ONE JSON object for that packet's kind, exactly as the answer schema for that kind "
    "says, and nothing else. Judge from the images and the evidence; never invent a number "
    "the evidence does not offer. When you cannot tell, use the answer the guide gives for "
    "that case rather than guessing. The reading guide below is the authority on every kind."
)


def gating_prefix() -> list:
    from plexora.plugins.gating.server.autogate import packets

    return [text_block(IDENTITY),
            text_block("READING GUIDE (version " + packets.guide_version() + ")\n"
                       + canonical(packets.reading_guide()), cache=True)]


def guide_version() -> str:
    from plexora.plugins.gating.server.autogate import packets

    return packets.guide_version()
