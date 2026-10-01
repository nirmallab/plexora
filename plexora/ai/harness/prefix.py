"""The cached prefix every decision worker starts from (gating, QC).

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

QC's prefix (`qc_prefix`) is the same shape with one more block: the
`qc-image` skill (its placeholders filled from code constants, so it is as
stable as the build), whose judgement guidance -- what an artifact is, when
to say `cannot_tell` -- the reading guide does not repeat. Its tool-calling
steps are for an external agent; the identity says the harness does those.
"""

from __future__ import annotations

from plexora.ai.harness.wire import canonical, text_block

#: Maps are sent as {key, value} lists (schema.py); both identities say so.
MAP_RULE = (
    "Where the guide describes an object keyed by name (verdicts by marker or channel, say) "
    "and the answer schema asks for a list of {key, value} entries, give each key once as "
    "one entry.")

IDENTITY = (
    "You are a Plexora gating worker. Plexora is an image viewer for multiplexed tissue images; "
    "it is gating markers (choosing the intensity threshold above which a cell is positive) "
    "and asks you one decision at a time. Each user turn is one decision packet: up to two "
    "images and a JSON packet with its kind, the units it concerns and its evidence. Answer "
    "with ONE JSON object for that packet's kind, exactly as the answer schema for that kind "
    "says, and nothing else. Judge from the images and the evidence; never invent a number "
    "the evidence does not offer. When you cannot tell, use the answer the guide gives for "
    "that case rather than guessing. The reading guide below is the authority on every kind. "
    + MAP_RULE
)

QC_IDENTITY = (
    "You are a Plexora QC worker. Plexora is an image viewer for multiplexed tissue images; it "
    "is quality-controlling one image (finding imaging and staining artifacts and the cells they "
    "affect) and asks you one decision at a time. Each user turn is one decision packet: up to "
    "two images and a JSON packet with its kind, the units it concerns and its evidence. Answer "
    "with ONE JSON object for that packet's kind, exactly as the answer schema for that kind "
    "says, and nothing else. You never type a coordinate or a threshold. Plexora itself starts, "
    "drives and finishes the session: wherever the skill below tells an agent to call a tool, "
    "that is already done, and your only part is the answer to the packet in front of you. "
    "Judge from the images and the evidence; when you cannot tell, say so with the answer the "
    "guide gives for that case rather than guessing. The reading guide is the authority on "
    "every kind. " + MAP_RULE
)


def gating_prefix() -> list:
    from plexora.plugins.gating.server.autogate import packets

    return [text_block(IDENTITY),
            text_block("READING GUIDE (version " + packets.guide_version() + ")\n"
                       + canonical(packets.reading_guide()), cache=True)]


def guide_version() -> str:
    from plexora.plugins.gating.server.autogate import packets

    return packets.guide_version()


QC_SKILL = "qc-image"


def qc_prefix() -> list:
    from plexora.ai import skills
    from plexora.plugins.qc.server import packets

    return [text_block(QC_IDENTITY),
            text_block("SKILL (" + QC_SKILL + ")\n" + skills.read_skill(QC_SKILL)),
            text_block("READING GUIDE (version " + packets.guide_version() + ")\n"
                       + canonical(packets.reading_guide()), cache=True)]


def qc_guide_version() -> str:
    from plexora.plugins.qc.server import packets

    return packets.guide_version()
